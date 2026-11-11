import datetime as dt
import logging
import signal
import types
from collections.abc import Callable
from typing import Annotated
from zoneinfo import ZoneInfo

import psycopg
import typer

from ..db.loader import connect
from ..settings import get_settings
from .cache import RawStore
from .client import Client
from .fetch import season_for
from .ingest import RunInProgressError, RunSummary, run
from .snapshot import FAILED_WEEK_STATUSES, SnapshotSummary, WeekStatus, run_snapshot

app = typer.Typer(name="pipeline")
logger = logging.getLogger(__name__)

# exit codes a scheduler can tell apart. 75 is EX_TEMPFAIL from sysexits.h,
# "try again later": another run has the lock, nothing went wrong
EXIT_RUN_FAILED = 1
EXIT_RUN_IN_PROGRESS = 75

ReloadAll = Annotated[
    bool,
    typer.Option(
        "--reload-all",
        help="Load every game, not only the ones with new or changed files. Use after a parser or loader change.",
    ),
]


def exit_on_sigterm(signum: int, _frame: types.FrameType | None) -> None:
    """
    turns SIGTERM (docker stop, systemd, a scheduler's timeout) into
    SystemExit, so the run's except path closes its ingest_run row the same
    way it does for Ctrl-C. 128 + signal number is the usual exit code.
    """
    raise SystemExit(128 + signum)


@app.callback()
def setup() -> None:
    """
    Sideout Analytics data pipeline: fetch from the NCAA API and load into Postgres.
    """
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, exit_on_sigterm)


@app.command()
def backfill(
    season: Annotated[int, typer.Option("--season", help="The season to load, named by the year it starts.")],
    reload_all: ReloadAll = False,
) -> None:
    """
    Fetch and load one season, past or current.
    """
    fetch_and_load("backfill", [season], reload_all)


@app.command()
def poll(reload_all: ReloadAll = False) -> None:
    """
    Fetch and load the current season, as often as the scheduler likes.

    Games whose files haven't changed are skipped, and a run that finds another one going exits with code 75.
    """
    fetch_and_load("poll", [season_for(today())], reload_all)


@app.command()
def snapshot() -> None:
    """
    Save the current AVCA poll and load every saved week into team_rank.

    Safe to run daily: a week that's already loaded is skipped. Exits 1 when a poll school matches no team, add a
    team_alias row for it and the next run loads the week.
    """
    report_snapshot(
        execute(lambda connection: run_snapshot(connection, Client.from_settings(), RawStore.from_settings()))
    )


def today() -> dt.date:
    """
    the date in the API's timezone.
    """
    return dt.datetime.now(tz=ZoneInfo("America/New_York")).date()


def execute[T](start: Callable[[psycopg.Connection], T]) -> T:
    """
    opens the database, runs one job on the connection and returns its
    result. Exits 75 if another run has the lock.
    """
    with connect(get_settings().database_url) as connection:
        try:
            return start(connection)
        except RunInProgressError:
            logger.warning("another pipeline run is in progress, exiting")
            raise typer.Exit(code=EXIT_RUN_IN_PROGRESS) from None


def fetch_and_load(job: str, season_years: list[int], reload_all: bool) -> None:
    """
    a fetch and load run built from settings, then its report.
    """
    settings = get_settings()
    summary = execute(
        lambda connection: run(
            connection,
            Client.from_settings(),
            RawStore.from_settings(),
            job,
            season_years,
            today(),
            settings.data_directory,
            reload_all=reload_all,
        )
    )
    report(summary)


def report(summary: RunSummary) -> None:
    """
    logs what the run did and exits 1 if it failed, so a scheduler sees it.
    """
    loads = summary.loads
    logger.info(
        "ingest run %s: %s games in index, %s loaded, %s unchanged, %s failed to load (%s new), %s requests failed",
        summary.ingest_run_id,
        summary.games_in_index,
        loads.loaded,
        loads.unchanged,
        len(loads.failed_games),
        len(loads.new_failures),
        len(summary.failed_requests),
    )
    for failed_request in summary.failed_requests:
        logger.warning("failed request %s: %r", failed_request.endpoint, failed_request.error)
    for failed_game in loads.failed_games:
        logger.warning(
            "failed game %s%s: %s", failed_game.game_id, "" if failed_game.new else " (known)", failed_game.error
        )
    if summary.error:
        raise typer.Exit(code=EXIT_RUN_FAILED)


def report_snapshot(summary: SnapshotSummary) -> None:
    """
    logs what the snapshot did and exits 1 if it failed.
    """
    if summary.current_poll_date is None:
        current = "current poll unreadable"
    else:
        current = f"poll through {summary.current_poll_date} ({'new week' if summary.new_week else 'already saved'})"
    logger.info(
        "ingest run %s: %s, %s saved weeks loaded",
        summary.ingest_run_id,
        current,
        sum(week.status == WeekStatus.LOADED for week in summary.weeks),
    )
    for week in summary.weeks:
        if week.status == WeekStatus.WAITING:
            logger.info("poll %s waits for its season to be loaded", week.poll_date)
        elif week.status in FAILED_WEEK_STATUSES:
            logger.warning("not loaded: %s", week.describe())
    if summary.error:
        raise typer.Exit(code=EXIT_RUN_FAILED)
