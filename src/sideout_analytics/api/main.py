import logging
import time

import psycopg
import tenacity
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..db.loader import connect
from ..settings import get_settings

logger = logging.getLogger(__name__)

app = FastAPI()

READINESS_TIMEOUT_SECONDS = 2
READINESS_MAX_ATTEMPTS = 3
READINESS_RETRY_DELAY_SECONDS = 0.1


def _check_database() -> None:
    with connect(get_settings().database_url, timeout_seconds=READINESS_TIMEOUT_SECONDS) as connection:
        connection.execute("SELECT 1")


@app.get("/health/liveness")
def liveness_probe() -> JSONResponse:
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/health/readiness")
def readiness_probe() -> JSONResponse:
    try:
        for attempt in tenacity.Retrying(
            stop=tenacity.stop_after_attempt(READINESS_MAX_ATTEMPTS),
            wait=tenacity.wait_fixed(READINESS_RETRY_DELAY_SECONDS),
            retry=tenacity.retry_if_exception_type(psycopg.Error),
            reraise=True,
            sleep=time.sleep,
        ):
            with attempt:
                _check_database()
    except psycopg.Error:
        # The error can name the host, user and database, so it goes to the
        # log and the response says only that the database is unavailable.
        logger.exception("readiness check failed: database unavailable")
        return JSONResponse({"status": "service unavailable"}, status_code=503)

    return JSONResponse({"status": "ok"}, status_code=200)
