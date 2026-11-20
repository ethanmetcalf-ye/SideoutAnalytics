import pytest
import sentry_sdk

from sideout_analytics import observability

REQUIRED_ENV = {
    "DATA_DIRECTORY": "./data",
    "DATABASE_URL": "postgresql://user:password@localhost:5432/testdb",
}
DSN = "https://publickey@o0.ingest.us.sentry.io/1"


@pytest.fixture
def init_calls(monkeypatch, isolated_env) -> list[dict]:
    """Settings with the required values, and sentry_sdk.init replaced by a recorder so nothing is sent."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    calls: list[dict] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    return calls


def test_no_dsn_leaves_sentry_off(init_calls) -> None:
    assert observability.configure_observability("api") is False
    assert init_calls == []


@pytest.mark.parametrize(
    ("service", "variable"),
    [("api", "SENTRY_API_DSN"), ("pipeline", "SENTRY_PIPELINE_DSN"), ("API", "SENTRY_API_DSN")],
)
def test_each_service_uses_its_own_dsn(monkeypatch, init_calls, service, variable) -> None:
    monkeypatch.setenv(variable, DSN)

    assert observability.configure_observability(service) is True
    assert [call["dsn"] for call in init_calls] == [DSN]


def test_other_services_dsn_is_ignored(monkeypatch, init_calls) -> None:
    monkeypatch.setenv("SENTRY_PIPELINE_DSN", DSN)

    assert observability.configure_observability("api") is False
    assert init_calls == []


@pytest.mark.parametrize(
    ("environment", "traces_sample_rate"),
    [("development", 1.0), ("production", 0.1), ("Production", 0.1)],
)
def test_environment_sets_name_and_sample_rate(monkeypatch, init_calls, environment, traces_sample_rate) -> None:
    monkeypatch.setenv("SENTRY_API_DSN", DSN)
    monkeypatch.setenv("ENVIRONMENT", environment)

    observability.configure_observability("api")

    assert init_calls[0]["environment"] == environment
    assert init_calls[0]["traces_sample_rate"] == traces_sample_rate
    assert init_calls[0]["send_default_pii"] is False


def test_unknown_service_raises(init_calls) -> None:
    with pytest.raises(ValueError, match="apii"):
        observability.configure_observability("apii")


def test_malformed_dsn_raises(monkeypatch, isolated_env) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SENTRY_API_DSN", "not-a-dsn")

    with pytest.raises(ValueError, match="SENTRY_API_DSN"):
        observability.configure_observability("api")
