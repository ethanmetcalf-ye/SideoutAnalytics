import logging

import psycopg
import pydantic
import pytest
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


def test_readiness_makes_a_single_attempt_and_does_not_leak_the_error(monkeypatch, caplog) -> None:
    attempts = []

    def connect(database_url: str, **kwargs) -> None:
        attempts.append((database_url, kwargs))
        raise psycopg.OperationalError("connection to server at db.internal (10.0.0.5) failed")

    fake_settings(monkeypatch)
    monkeypatch.setattr(api, "connect", connect)

    with caplog.at_level(logging.ERROR, logger=api.logger.name):
        response = TestClient(api.app).get("/health/readiness")

    assert response.status_code == 503
    assert response.json() == {"status": "service unavailable"}
    assert "db.internal" not in response.text
    assert "db.internal" in caplog.text
    assert attempts == [("db-url", {"timeout_seconds": api.READINESS_TIMEOUT_SECONDS})]


def test_startup_fails_when_settings_are_missing(isolated_env) -> None:
    with pytest.raises(pydantic.ValidationError), TestClient(api.app):
        pass


def test_startup_succeeds_when_settings_are_present(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIRECTORY", str(isolated_env))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:password@localhost/test")

    with TestClient(api.app) as client:
        assert client.get("/health/liveness").status_code == 200
