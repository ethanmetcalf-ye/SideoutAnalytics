import json
import logging
import re

import pytest
import structlog

from sideout_analytics import structlogconfig

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


@pytest.fixture
def clean_logging(monkeypatch, isolated_env):
    """
    settings for a run, and the root and uvicorn loggers put back as they were
    after the test, since configure_logging changes process-wide state.
    """
    monkeypatch.setenv("DATA_DIRECTORY", "./data")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost:5432/testdb")
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in structlogconfig.UVICORN_LOGGERS)]
    saved = [(logger, list(logger.handlers), logger.level, logger.propagate) for logger in loggers]
    yield
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
    structlog.reset_defaults()


def our_handlers() -> list[logging.Handler]:
    return [handler for handler in logging.getLogger().handlers if handler.get_name() == structlogconfig.HANDLER_NAME]


def last_json_line(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_configure_logging_adds_one_handler_even_when_called_twice(clean_logging) -> None:
    structlogconfig.configure_logging()
    structlogconfig.configure_logging()

    assert len(our_handlers()) == 1


def test_configure_logging_uses_the_log_level_setting(monkeypatch, clean_logging) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    structlogconfig.configure_logging()

    assert logging.getLogger().level == logging.WARNING


def test_production_logs_json_with_an_iso_utc_timestamp(monkeypatch, clean_logging, capsys) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    structlogconfig.configure_logging()

    logging.getLogger("sideout_analytics.test").info("hello")

    line = last_json_line(capsys)
    assert line["event"] == "hello"
    assert line["level"] == "info"
    assert line["logger"] == "sideout_analytics.test"
    assert ISO_UTC.match(line["timestamp"]), line["timestamp"]


def test_production_json_includes_the_traceback_of_a_logged_exception(monkeypatch, clean_logging, capsys) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    structlogconfig.configure_logging()

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("sideout_analytics.test").exception("it broke")

    line = last_json_line(capsys)
    assert line["level"] == "error"
    assert "ValueError: boom" in line["exception"]


def test_structlog_loggers_get_the_same_format(monkeypatch, clean_logging, capsys) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    structlogconfig.configure_logging()

    structlog.get_logger("sideout_analytics.native").info("from structlog", game_id="123")

    line = last_json_line(capsys)
    assert line["event"] == "from structlog"
    assert line["game_id"] == "123"
    assert ISO_UTC.match(line["timestamp"]), line["timestamp"]


def test_development_logs_for_people_not_json(clean_logging, capsys) -> None:
    structlogconfig.configure_logging()

    logging.getLogger("sideout_analytics.test").info("hello")

    output = capsys.readouterr().out
    assert "hello" in output
    assert not output.lstrip().startswith("{")


def test_uvicorn_logs_go_through_the_same_handler(monkeypatch, clean_logging, capsys) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    # what uvicorn does to its own loggers
    for name in structlogconfig.UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.addHandler(logging.StreamHandler())
        uvicorn_logger.propagate = False

    structlogconfig.configure_logging()

    for name in structlogconfig.UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    logging.getLogger("uvicorn.access").info('%s - "%s %s" %d', "1.2.3.4", "GET", "/health/liveness", 200)
    line = last_json_line(capsys)
    assert line["event"] == '1.2.3.4 - "GET /health/liveness" 200'
    assert line["logger"] == "uvicorn.access"
