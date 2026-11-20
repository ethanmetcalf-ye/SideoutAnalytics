import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..db.loader import connect
from ..db.tables import IngestStatus
from ..observability import configure_observability
from ..settings import get_settings
from ..structlogconfig import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast: load settings now so missing config crashes the container at
    # startup (visible in `docker compose logs api`, retried by the restart
    # policy) instead of surfacing as an error on the first request.
    get_settings()
    yield


sentry_on = configure_observability("api")

app = FastAPI(lifespan=lifespan)

READINESS_TIMEOUT_SECONDS = 2
# The jobs that load game data. A snapshot succeeding says nothing about whether
# the scores are current, so it doesn't count.
FRESHNESS_JOBS = ("poll", "backfill")


def _check_database() -> None:
    with connect(get_settings().database_url, timeout_seconds=READINESS_TIMEOUT_SECONDS) as connection:
        connection.execute("SELECT 1")


@app.get("/health/liveness")
def liveness_probe() -> JSONResponse:
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/health/readiness")
def readiness_probe() -> JSONResponse:
    # One attempt: whoever calls the probe (Docker, a monitor, a load
    # balancer) has its own retries and timeout, and the probe should report
    # what it sees right now.
    try:
        _check_database()
    except psycopg.Error:
        # The error can name the host, user and database, so it goes to the
        # log and the response says only that the database is unavailable.
        logger.exception("readiness check failed: database unavailable")
        return JSONResponse({"status": "service unavailable"}, status_code=503)

    return JSONResponse({"status": "ok"}, status_code=200)


def _last_successful_ingest() -> tuple[dt.datetime | None, dt.datetime]:
    """
    finished_at of the last succeeded game-data run (None if there never was
    one) and the database's own clock, so the API server's clock doesn't matter.
    """
    with connect(get_settings().database_url, timeout_seconds=READINESS_TIMEOUT_SECONDS) as connection:
        return connection.execute(
            "SELECT max(finished_at), now() FROM ingest_run WHERE status = %s AND job = ANY(%s)",
            (IngestStatus.SUCCEEDED.value, list(FRESHNESS_JOBS)),
        ).fetchone()


@app.get("/health/freshness")
def freshness_probe() -> JSONResponse:
    # Kept apart from readiness on purpose. If readiness failed whenever the data
    # was stale, a broken NCAA API would make this healthy api look down. Stale
    # is a 200 with stale: true; the status is 503 only when the database
    # can't be read. Nothing that restarts or routes to the api should use this.
    try:
        finished_at, now = _last_successful_ingest()
    except psycopg.Error:
        logger.exception("freshness check failed: database unavailable")
        return JSONResponse({"status": "service unavailable"}, status_code=503)

    stale_after_minutes = get_settings().freshness_stale_after_minutes
    stale = finished_at is None or now - finished_at > dt.timedelta(minutes=stale_after_minutes)
    return JSONResponse(
        {
            "last_successful_ingest": finished_at.isoformat() if finished_at else None,
            "stale": stale,
            "stale_after_minutes": stale_after_minutes,
        },
        status_code=200,
    )
