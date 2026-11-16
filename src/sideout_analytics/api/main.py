import time

import psycopg
import tenacity
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url

from ..settings import get_settings

app = FastAPI()

READINESS_TIMEOUT_SECONDS = 2
READINESS_MAX_ATTEMPTS = 3
READINESS_RETRY_DELAY_SECONDS = 0.1


def connect(database_url: str, *, timeout_seconds: int = READINESS_TIMEOUT_SECONDS) -> psycopg.Connection:
    """
    a psycopg connection for DATABASE_URL. The URL is written for SQLAlchemy
    (postgresql+psycopg://) and psycopg wants plain postgresql://.
    """
    url = make_url(database_url).set(drivername="postgresql")
    return psycopg.connect(
        url.render_as_string(hide_password=False),
        connect_timeout=timeout_seconds,
        options=f"-c statement_timeout={timeout_seconds * 1000}",
    )


def _check_database() -> None:
    with connect(get_settings().database_url) as connection:
        connection.execute("SELECT 1;")


@app.get("/health/liveness")
def liveness_check() -> JSONResponse:
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/health/readiness")
def readiness_probe() -> JSONResponse:
    payload = {"status": "ok"}
    status_code = 200
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
    except psycopg.Error as exc:
        payload["status"] = "service unavailable"
        payload["database_exception"] = str(exc)
        status_code = 503

    return JSONResponse(payload, status_code=status_code)
