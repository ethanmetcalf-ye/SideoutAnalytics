import datetime as dt
import logging
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from ..settings import get_settings
from .fetch import season_for
from .ingest import RunSummary, run

app = typer.Typer(name="pipeline")
logger = logging.getLogger(__name__)

ReloadAll = Annotated[
    bool,
    typer.Option(
        "--reload-all",
        help="Load every game, not only the ones with new or changed files. Use after a parser or loader change.",
    ),
]


# sets up logging once for every command, before the command runs
@app.callback()
def configure_logging() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# fetches and loads one season, any season, past or current
@app.command()
def backfill(
    season: Annotated[int, typer.Option("--season", help="The season to load, named by the year it starts.")],
    reload_all: ReloadAll = False,
) -> None:
    report(run("backfill", [season], reload_all=reload_all))


# fetches and loads the current season, the job that runs every day
@app.command()
def daily(reload_all: ReloadAll = False) -> None:
    today = dt.datetime.now(tz=ZoneInfo("America/New_York")).date()
    report(run("daily", [season_for(today)], reload_all=reload_all))


# logs what the run did and exits 1 if it failed, so a scheduler sees it
def report(summary: RunSummary) -> None:
    loads = summary.loads
    logger.info(
        "ingest run %s: %s games in index, %s loaded, %s unchanged, %s failed to load, %s requests failed",
        summary.ingest_run_id,
        summary.games_in_index,
        loads.loaded,
        loads.unchanged,
        len(loads.failed_games),
        len(summary.failed_requests),
    )
    for failed_request in summary.failed_requests:
        logger.warning("failed request %s: %r", failed_request.endpoint, failed_request.error)
    for failed_game in loads.failed_games:
        logger.warning("failed game %s: %s", failed_game.game_id, failed_game.error)
    if summary.error:
        raise typer.Exit(code=1)
