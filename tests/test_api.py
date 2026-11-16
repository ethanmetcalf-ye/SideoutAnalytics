import logging

import psycopg
from fastapi.testclient import TestClient

from sideout_analytics.api import main as api
from sideout_analytics.db import loader


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def execute(self, query):
        assert query == "SELECT 1"


def fake_settings(monkeypatch) -> None:
    monkeypatch.setattr(api, "get_settings", lambda: type("Settings", (), {"database_url": "db-url"})())


def test_connect_applies_connection_and_statement_timeouts(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(loader.psycopg, "connect", lambda *args, **kwargs: calls.append((args, kwargs)))

    loader.connect("postgresql+psycopg://user:password@localhost/test", timeout_seconds=2)

    assert calls == [
        (
            ("postgresql://user:password@localhost/test",),
            {"autocommit": True, "connect_timeout": 2, "options": "-c statement_timeout=2000"},
        )
    ]


def test_connect_has_no_timeouts_by_default(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(loader.psycopg, "connect", lambda *args, **kwargs: calls.append((args, kwargs)))

    loader.connect("postgresql+psycopg://user:password@localhost/test")

    assert calls == [(("postgresql://user:password@localhost/test",), {"autocommit": True})]


def test_liveness_route_returns_ok() -> None:
    response = TestClient(api.app).get("/health/liveness")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_route_returns_ok_when_database_answers(monkeypatch) -> None:
    fake_settings(monkeypatch)
    monkeypatch.setattr(api, "connect", lambda database_url, **kwargs: FakeConnection())

    response = TestClient(api.app).get("/health/readiness")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_retries_database_errors(monkeypatch) -> None:
    attempts = []
    sleeps = []

    def connect(database_url: str, **kwargs) -> FakeConnection:
        attempts.append((database_url, kwargs))
        if len(attempts) == 1:
            raise psycopg.OperationalError("temporary failure")
        return FakeConnection()

    fake_settings(monkeypatch)
    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)

    response = TestClient(api.app).get("/health/readiness")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    timeouts = {"timeout_seconds": api.READINESS_TIMEOUT_SECONDS}
    assert attempts == [("db-url", timeouts), ("db-url", timeouts)]
    assert sleeps == [api.READINESS_RETRY_DELAY_SECONDS]


def test_readiness_returns_unavailable_without_leaking_the_error(monkeypatch, caplog) -> None:
    attempts = []
    sleeps = []

    def connect(database_url: str, **kwargs) -> None:
        attempts.append(database_url)
        raise psycopg.OperationalError("connection to server at db.internal (10.0.0.5) failed")

    fake_settings(monkeypatch)
    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)

    with caplog.at_level(logging.ERROR, logger=api.logger.name):
        response = TestClient(api.app).get("/health/readiness")

    assert response.status_code == 503
    assert response.json() == {"status": "service unavailable"}
    assert "db.internal" not in response.text
    assert "db.internal" in caplog.text
    assert len(attempts) == api.READINESS_MAX_ATTEMPTS
    assert len(sleeps) == api.READINESS_MAX_ATTEMPTS - 1
