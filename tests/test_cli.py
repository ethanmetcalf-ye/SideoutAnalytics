import contextlib
import datetime as dt
import logging

import pytest
import typer
from typer.testing import CliRunner

from sideout_analytics.pipeline import cli
from sideout_analytics.pipeline.client import NotAvailableYetError
from sideout_analytics.pipeline.fetch import FailedRequest
from sideout_analytics.pipeline.ingest import FailedGame, LoadSummary, RunSummary
from sideout_analytics.pipeline.snapshot import SnapshotSummary, WeekResult, WeekStatus

POLL_DATE = dt.date(2026, 11, 2)


def run_summary(failed_games: tuple[FailedGame, ...] = (), fetch_errors: tuple[FailedRequest, ...] = ()) -> RunSummary:
    return RunSummary(
        ingest_run_id=7,
        games_in_index=10,
        loads=LoadSummary(loaded=9, unchanged=0, failed_games=failed_games),
        fetch_errors=fetch_errors,
    )


def snapshot_summary(*weeks: WeekResult) -> SnapshotSummary:
    return SnapshotSummary(ingest_run_id=8, current_poll_date=POLL_DATE, new_week=True, weeks=weeks)


def error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_report_clean_run_logs_no_error(caplog) -> None:
    with caplog.at_level(logging.INFO):
        cli.report(run_summary())

    assert error_records(caplog) == []


def test_report_known_failure_doesnt_fail_the_run_or_log_an_error(caplog) -> None:
    known = FailedGame("123", ValueError("bad file"), new=False)

    with caplog.at_level(logging.INFO):
        cli.report(run_summary(failed_games=(known,)))

    assert error_records(caplog) == []
    assert any(record.levelno == logging.WARNING and "(known)" in record.getMessage() for record in caplog.records)


def test_report_games_not_played_yet_dont_log_an_error(caplog) -> None:
    not_yet = FailedRequest("/game/123", NotAvailableYetError("not available yet"))

    with caplog.at_level(logging.INFO):
        cli.report(run_summary(fetch_errors=(not_yet,)))

    assert error_records(caplog) == []


def test_report_new_failure_logs_one_error_and_exits_1(caplog) -> None:
    new = [FailedGame("123", ValueError("bad file")), FailedGame("456", ValueError("bad file"))]

    with caplog.at_level(logging.INFO), pytest.raises(typer.Exit) as exit_info:
        cli.report(run_summary(failed_games=tuple(new)))

    assert exit_info.value.exit_code == cli.EXIT_RUN_FAILED
    # one event for the run, not one per game
    assert [record.getMessage() for record in error_records(caplog)] == [
        "ingest run 7 failed: 2 games failed to load for the first time"
    ]


def test_report_failed_request_logs_an_error_and_exits_1(caplog) -> None:
    failed = FailedRequest("/game/123", RuntimeError("boom"))

    with caplog.at_level(logging.INFO), pytest.raises(typer.Exit):
        cli.report(run_summary(fetch_errors=(failed,)))

    assert [record.getMessage() for record in error_records(caplog)] == ["ingest run 7 failed: 1 requests failed"]


def test_report_snapshot_waiting_week_logs_no_error(caplog) -> None:
    with caplog.at_level(logging.INFO):
        cli.report_snapshot(snapshot_summary(WeekResult(POLL_DATE, WeekStatus.WAITING)))

    assert error_records(caplog) == []


@pytest.mark.parametrize(
    "week",
    [
        WeekResult(POLL_DATE, WeekStatus.UNMATCHED, unmatched=("Nowhere State",)),
        WeekResult(None, WeekStatus.UNREADABLE, file="undated-abc.json", error="bad shape"),
    ],
)
def test_report_snapshot_failed_week_logs_an_error_and_exits_1(caplog, week) -> None:
    with caplog.at_level(logging.INFO), pytest.raises(typer.Exit) as exit_info:
        cli.report_snapshot(snapshot_summary(week))

    assert exit_info.value.exit_code == cli.EXIT_RUN_FAILED
    assert [record.getMessage() for record in error_records(caplog)] == [f"snapshot run 8 failed: {week.describe()}"]


def test_run_in_progress_exits_75_with_a_warning_not_an_error(monkeypatch, isolated_env, caplog) -> None:
    monkeypatch.setenv("DATA_DIRECTORY", "./data")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost:5432/testdb")

    @contextlib.contextmanager
    def fake_connect(_database_url: str):
        yield object()

    def locked(_connection: object) -> None:
        raise cli.RunInProgressError

    monkeypatch.setattr(cli, "connect", fake_connect)

    with caplog.at_level(logging.INFO), pytest.raises(typer.Exit) as exit_info:
        cli.execute(locked)

    assert exit_info.value.exit_code == cli.EXIT_RUN_IN_PROGRESS
    assert error_records(caplog) == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_help_needs_no_settings(isolated_env) -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def test_setup_starts_logging_and_sentry_for_the_pipeline(monkeypatch) -> None:
    started = []
    monkeypatch.setattr(cli, "configure_logging", lambda: started.append("logging"))
    monkeypatch.setattr(cli, "configure_observability", lambda service: started.append(f"sentry:{service}"))
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    cli.setup()

    assert started == ["logging", "sentry:pipeline"]
