import json

import psycopg

from sideout_analytics.api import main as api


def test_connect_applies_connection_and_statement_timeouts(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(api.psycopg, "connect", lambda *args, **kwargs: calls.append((args, kwargs)))

    api.connect("postgresql+psycopg://user:password@localhost/test", timeout_seconds=2)

    assert calls == [
        (
            ("postgresql://user:password@localhost/test",),
            {"connect_timeout": 2, "options": "-c statement_timeout=2000"},
        )
    ]


def test_readiness_retries_database_errors(monkeypatch) -> None:
    attempts = []
    sleeps = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def execute(self, query):
            assert query == "SELECT 1;"

    def connect(database_url: str) -> Connection:
        attempts.append(database_url)
        if len(attempts) == 1:
            raise psycopg.OperationalError("temporary failure")
        return Connection()

    monkeypatch.setattr(api, "get_settings", lambda: type("Settings", (), {"database_url": "db-url"})())
    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)

    response = api.readiness_probe()

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok"}
    assert attempts == ["db-url", "db-url"]
    assert sleeps == [api.READINESS_RETRY_DELAY_SECONDS]


def test_readiness_returns_unavailable_after_max_attempts(monkeypatch) -> None:
    attempts = []
    sleeps = []

    def connect(database_url: str) -> None:
        attempts.append(database_url)
        raise psycopg.OperationalError("database is down")

    monkeypatch.setattr(api, "get_settings", lambda: type("Settings", (), {"database_url": "db-url"})())
    monkeypatch.setattr(api, "connect", connect)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)

    response = api.readiness_probe()

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "service unavailable",
        "database_exception": "database is down",
    }
    assert len(attempts) == api.READINESS_MAX_ATTEMPTS
    assert len(sleeps) == api.READINESS_MAX_ATTEMPTS - 1
