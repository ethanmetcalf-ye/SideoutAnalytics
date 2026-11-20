import contextlib
import datetime as dt
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


NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.UTC)


def fake_last_ingest(monkeypatch, finished_at: dt.datetime | None, stale_after_minutes: int = 120) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: type(
            "Settings", (), {"database_url": "db-url", "freshness_stale_after_minutes": stale_after_minutes}
        )(),
    )
    monkeypatch.setattr(api, "_last_successful_ingest", lambda: (finished_at, NOW))


def test_freshness_route_reports_a_recent_ingest_as_not_stale(monkeypatch) -> None:
    finished_at = NOW - dt.timedelta(minutes=30)
    fake_last_ingest(monkeypatch, finished_at)

    response = TestClient(api.app).get("/health/freshness")

    assert response.status_code == 200
    assert response.json() == {
        "last_successful_ingest": finished_at.isoformat(),
        "stale": False,
        "stale_after_minutes": 120,
    }


@pytest.mark.parametrize(
    ("minutes_ago", "stale"),
    [(120, False), (121, True), (24 * 60, True)],
)
def test_freshness_route_goes_stale_after_the_configured_minutes(monkeypatch, minutes_ago, stale) -> None:
    fake_last_ingest(monkeypatch, NOW - dt.timedelta(minutes=minutes_ago))

    response = TestClient(api.app).get("/health/freshness")

    assert response.status_code == 200
    assert response.json()["stale"] is stale


def test_freshness_route_uses_the_stale_after_setting(monkeypatch) -> None:
    fake_last_ingest(monkeypatch, NOW - dt.timedelta(minutes=45), stale_after_minutes=30)

    body = TestClient(api.app).get("/health/freshness").json()

    assert body["stale"] is True
    assert body["stale_after_minutes"] == 30


def test_freshness_route_is_stale_when_nothing_has_ever_succeeded(monkeypatch) -> None:
    fake_last_ingest(monkeypatch, None)

    response = TestClient(api.app).get("/health/freshness")

    assert response.status_code == 200
    assert response.json()["last_successful_ingest"] is None
    assert response.json()["stale"] is True


def test_stale_data_does_not_fail_readiness(monkeypatch) -> None:
    fake_last_ingest(monkeypatch, NOW - dt.timedelta(days=3))
    monkeypatch.setattr(api, "connect", lambda database_url, **kwargs: FakeConnection())
    client = TestClient(api.app)

    assert client.get("/health/freshness").json()["stale"] is True
    assert client.get("/health/readiness").status_code == 200


def test_freshness_route_does_not_leak_the_database_error(monkeypatch, caplog) -> None:
    fake_last_ingest(monkeypatch, None)

    def unavailable() -> None:
        raise psycopg.OperationalError("connection to host=secret-host user=sideout failed")

    monkeypatch.setattr(api, "_last_successful_ingest", unavailable)

    with caplog.at_level(logging.ERROR):
        response = TestClient(api.app).get("/health/freshness")

    assert response.status_code == 503
    assert response.json() == {"status": "service unavailable"}
    assert "secret-host" not in response.text


def test_last_successful_ingest_counts_only_succeeded_game_data_runs(monkeypatch, database) -> None:
    monkeypatch.setattr(api, "get_settings", lambda: type("Settings", (), {"database_url": "db-url"})())
    monkeypatch.setattr(api, "connect", lambda database_url, **kwargs: contextlib.nullcontext(database))
    runs = [
        # job, status, finished_at (hours before an arbitrary start)
        ("poll", "succeeded", dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.UTC)),
        ("backfill", "succeeded", dt.datetime(2026, 11, 20, 8, 0, tzinfo=dt.UTC)),
        # newer, but they don't count: failed, still running, and a snapshot
        ("poll", "failed", dt.datetime(2026, 11, 20, 11, 0, tzinfo=dt.UTC)),
        ("poll", "running", None),
        ("snapshot", "succeeded", dt.datetime(2026, 11, 20, 11, 30, tzinfo=dt.UTC)),
    ]
    for job, status, finished_at in runs:
        database.execute(
            "INSERT INTO ingest_run (job, started_at, finished_at, status) VALUES (%s, %s, %s, %s)",
            (job, dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.UTC), finished_at, status),
        )

    finished_at, now = api._last_successful_ingest()

    assert finished_at == dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.UTC)
    assert now.tzinfo is not None


def test_last_successful_ingest_is_none_without_a_succeeded_run(monkeypatch, database) -> None:
    monkeypatch.setattr(api, "get_settings", lambda: type("Settings", (), {"database_url": "db-url"})())
    monkeypatch.setattr(api, "connect", lambda database_url, **kwargs: contextlib.nullcontext(database))

    finished_at, _now = api._last_successful_ingest()

    assert finished_at is None
