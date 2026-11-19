import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..db.loader import connect
from ..settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast: load settings now so missing config crashes the container at
    # startup (visible in `docker compose logs api`, retried by the restart
    # policy) instead of surfacing as an error on the first request.
    get_settings()
    yield


app = FastAPI(lifespan=lifespan)

READINESS_TIMEOUT_SECONDS = 2


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
