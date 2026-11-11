import datetime as dt
import logging
import signal
import types
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from ..db.loader import connect
from ..settings import get_settings
from .cache import RawStore
from .client import Client
from .fetch import season_for
from .ingest import RunInProgressError, RunSummary, run

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
    execute("backfill", [season], reload_all)


@app.command()
def poll(reload_all: ReloadAll = False) -> None:
    """
    Fetch and load the current season, as often as the scheduler likes.

    Games whose files haven't changed are skipped, and a run that finds another one going exits with code 75.
    """
    execute("poll", [season_for(today())], reload_all)


def today() -> dt.date:
    """
    the date in the API's timezone.
    """
    return dt.datetime.now(tz=ZoneInfo("America/New_York")).date()


def execute(job: str, season_years: list[int], reload_all: bool) -> None:
    """
    builds what a run needs from settings, runs it, and reports.
    """
    settings = get_settings()
    with connect(settings.database_url) as connection:
        try:
            summary = run(
                connection,
                Client.from_settings(),
                RawStore.from_settings(),
                job,
                season_years,
                today(),
                settings.data_directory,
                reload_all=reload_all,
            )
        except RunInProgressError:
            logger.warning("another pipeline run is in progress, exiting")
            raise typer.Exit(code=EXIT_RUN_IN_PROGRESS) from None
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
