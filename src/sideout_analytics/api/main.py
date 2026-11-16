import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url

from ..settings import get_settings

app = FastAPI()


def connect(database_url: str) -> psycopg.Connection:
    """
    a psycopg connection for DATABASE_URL. The URL is written for SQLAlchemy
    (postgresql+psycopg://) and psycopg wants plain postgresql://.
    """
    url = make_url(database_url).set(drivername="postgresql")
    return psycopg.connect(url.render_as_string(hide_password=False))


@app.get("/health/liveness")
def liveness_check() -> JSONResponse:
    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/health/readiness")
def readiness_probe() -> JSONResponse:
    payload = {"status": "ok"}
    status_code = 200
    try:
        with connect(get_settings().database_url) as connection:
            connection.execute("SELECT 1;")
    except psycopg.Error as exc:
        payload["status"] = "service unavailable"
        payload["database_exception"] = str(exc)
        status_code = 503

    return JSONResponse(payload, status_code=status_code)
