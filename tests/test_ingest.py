import dataclasses
import datetime as dt
import json
import pathlib
import shutil
import signal

import httpx
import pytest

from sideout_analytics.db.loader import LoadError, connect
from sideout_analytics.db.tables import MatchState
from sideout_analytics.pipeline import cli, ingest
from sideout_analytics.pipeline.cache import RawStore, hash_content
from sideout_analytics.pipeline.client import Client, FailedFetchError, NotAvailableYetError
from sideout_analytics.pipeline.fetch import FailedRequest, FetchedFile, Game

RAW_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "raw"
GAME_ID = "6500656"
SCOREBOARD_ENDPOINT = "/scoreboard/volleyball-women/d1/2025/12/05"
GAME_ENDPOINTS = [f"/game/{GAME_ID}", f"/game/{GAME_ID}/boxscore", f"/game/{GAME_ID}/play-by-play"]


@pytest.fixture
def data_directory(tmp_path) -> pathlib.Path:
    """
    a data directory whose raw cache is a copy of the fixtures, so a test
    can change a file without touching the fixtures.
    """
    shutil.copytree(RAW_FIXTURES, tmp_path / "raw")
    return tmp_path


@pytest.fixture
def raw_store(data_directory) -> RawStore:
    return RawStore(data_directory / "raw")


def fetched_files(raw_store: RawStore, endpoints: list[str]) -> dict[str, FetchedFile]:
    """
    the files the fetcher would return for these endpoints.
    """
    files = {}
    for endpoint in endpoints:
        cache_file = raw_store.map_endpoint_cache_file(endpoint)
        files[endpoint] = FetchedFile(endpoint, cache_file, hash_content(cache_file.read_bytes()))
    return files


def fixture_game(scoreboard_endpoint: str | None = SCOREBOARD_ENDPOINT) -> dict[str, Game]:
    return {GAME_ID: Game(GAME_ID, 0, MatchState.FINAL, scoreboard_endpoint=scoreboard_endpoint)}


def fetch_value(connection, query: str, *params):
    """
    the first column of the first row of a query.
    """
    return connection.execute(query, params).fetchone()[0]


@pytest.fixture
def ingest_run_id(database) -> int:
    return ingest.start_run(database, "test")


# helpers


def test_game_id_for_reads_any_game_endpoint():
    assert ingest.game_id_for("/game/123") == "123"
    assert ingest.game_id_for("/game/123/play-by-play") == "123"


def test_files_by_game_groups_on_game_id(raw_store):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    grouped = ingest.files_by_game(files)
    assert list(grouped) == [GAME_ID]
    assert sorted(grouped[GAME_ID]) == sorted(GAME_ENDPOINTS)


def test_is_changed_compares_hashes(raw_store, data_directory):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    loaded = {
        ingest.relative_path(fetched.cache_file, data_directory): fetched.content_hash for fetched in files.values()
    }
    assert not ingest.is_changed(files, loaded, data_directory)

    # one file never loaded
    del loaded[ingest.relative_path(files[GAME_ENDPOINTS[2]].cache_file, data_directory)]
    assert ingest.is_changed(files, loaded, data_directory)


def test_relative_path_is_posix_under_data_directory(raw_store, data_directory):
    cache_file = raw_store.map_endpoint_cache_file(f"/game/{GAME_ID}/boxscore")
    assert ingest.relative_path(cache_file, data_directory) == f"raw/game/{GAME_ID}/boxscore.json"


def test_run_summary_ignores_unplayed_games():
    """
    a game that isn't played yet is expected, a request that failed for
    good fails the run.
    """
    loads = ingest.LoadSummary(loaded=0, unchanged=0, failed_games=())
    unplayed = FailedRequest("/game/1", NotAvailableYetError())
    assert ingest.RunSummary(1, 0, loads, (unplayed,)).error is None

    failed = FailedRequest("/game/2", FailedFetchError())
    assert ingest.RunSummary(1, 0, loads, (unplayed, failed)).error == "1 requests failed"


# loading


def test_load_games_loads_and_indexes_files(database, raw_store, data_directory, ingest_run_id):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    summary = ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)

    assert summary == ingest.LoadSummary(loaded=1, unchanged=0, failed_games=())
    assert fetch_value(database, "SELECT count(*) FROM match WHERE source_game_id = %s", GAME_ID) == 1
    assert fetch_value(database, "SELECT count(*) FROM raw_response WHERE match_id IS NOT NULL") == 3


def test_load_games_skips_unchanged_games(database, raw_store, data_directory, ingest_run_id):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    summary = ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    assert summary == ingest.LoadSummary(loaded=0, unchanged=1, failed_games=())


def test_load_games_reloads_a_changed_file(database, raw_store, data_directory, ingest_run_id):
    """
    a play-by-play that shows up after the game was loaded without one
    reloads the game.
    """
    ingest.load_games(
        database, raw_store, fixture_game(), fetched_files(raw_store, GAME_ENDPOINTS[:2]), ingest_run_id, data_directory
    )
    assert fetch_value(database, "SELECT count(*) FROM play") == 0

    files = fetched_files(raw_store, GAME_ENDPOINTS)
    summary = ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    assert summary.loaded == 1
    assert fetch_value(database, "SELECT count(*) FROM play") > 0


def test_load_games_reload_all_ignores_hashes(database, raw_store, data_directory, ingest_run_id):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    summary = ingest.load_games(
        database, raw_store, fixture_game(), files, ingest_run_id, data_directory, reload_all=True
    )
    assert summary.loaded == 1


def test_load_games_failed_game_is_not_indexed(database, raw_store, data_directory, ingest_run_id):
    """
    a game that can't be loaded keeps no raw_response rows, so the next run
    tries it again.
    """
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    summary = ingest.load_games(
        database, raw_store, fixture_game(scoreboard_endpoint=None), files, ingest_run_id, data_directory
    )

    assert summary.loaded == 0
    assert [failed.game_id for failed in summary.failed_games] == [GAME_ID]
    assert fetch_value(database, "SELECT count(*) FROM raw_response") == 0


def test_load_games_failed_parse_is_reported(database, raw_store, data_directory, ingest_run_id):
    raw_store.map_endpoint_cache_file(f"/game/{GAME_ID}/boxscore").write_bytes(b"{}")
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    summary = ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    assert [failed.game_id for failed in summary.failed_games] == [GAME_ID]


# runs


def test_finish_run_records_status(database):
    succeeded = ingest.start_run(database, "test")
    ingest.finish_run(database, succeeded, None)
    failed = ingest.start_run(database, "test")
    ingest.finish_run(database, failed, "2 games failed to load")

    query = "SELECT status, error, finished_at IS NOT NULL FROM ingest_run WHERE ingest_run_id = %s"
    assert database.execute(query, (succeeded,)).fetchone() == ("succeeded", None, True)
    assert database.execute(query, (failed,)).fetchone() == ("failed", "2 games failed to load", True)


def test_finish_run_records_counts(database):
    loads = ingest.LoadSummary(loaded=3, unchanged=10, failed_games=(ingest.FailedGame("9", ValueError()),))
    summary = ingest.RunSummary(1, 14, loads, (FailedRequest("/game/2", FailedFetchError()),))
    ingest_run_id = ingest.start_run(database, "test")
    ingest.finish_run(database, ingest_run_id, summary.error, summary.counts)

    row = database.execute(
        "SELECT games_in_index, games_loaded, games_unchanged, games_failed, requests_failed "
        "FROM ingest_run WHERE ingest_run_id = %s",
        (ingest_run_id,),
    ).fetchone()
    assert row == (14, 3, 10, 1, 1)


def test_finish_run_without_counts_leaves_them_null(database):
    """
    a run that stopped before counting has no counts, not zeros.
    """
    ingest_run_id = ingest.start_run(database, "test")
    ingest.finish_run(database, ingest_run_id, "KeyboardInterrupt: ")
    assert fetch_value(database, "SELECT games_loaded FROM ingest_run WHERE ingest_run_id = %s", ingest_run_id) is None


def test_finish_run_rejects_unknown_counts():
    with pytest.raises(ValueError, match="games_skipped"):
        ingest.finish_run(None, 1, None, {"games_skipped": 1})


# failures across runs


def test_run_summary_ignores_known_failures():
    """
    a game that was already failing doesn't fail the run again.
    """
    known = ingest.FailedGame(GAME_ID, ValueError(), new=False)
    summary = ingest.RunSummary(1, 1, ingest.LoadSummary(loaded=0, unchanged=0, failed_games=(known,)), ())
    assert summary.error is None
    assert summary.counts["games_failed"] == 1

    new = ingest.FailedGame("2", ValueError())
    summary = ingest.RunSummary(1, 2, ingest.LoadSummary(loaded=0, unchanged=0, failed_games=(known, new)), ())
    assert summary.error == "1 games failed to load for the first time"


def test_load_games_failure_is_new_only_once(database, raw_store, data_directory, ingest_run_id):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    broken = fixture_game(scoreboard_endpoint=None)
    first = ingest.load_games(database, raw_store, broken, files, ingest_run_id, data_directory)
    second = ingest.load_games(database, raw_store, broken, files, ingest_run_id, data_directory)

    assert [failed.new for failed in first.failed_games] == [True]
    assert [failed.new for failed in second.failed_games] == [False]
    assert fetch_value(database, "SELECT count(*) FROM load_failure WHERE source_game_id = %s", GAME_ID) == 1


def test_load_games_success_clears_failure(database, raw_store, data_directory, ingest_run_id):
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    ingest.load_games(database, raw_store, fixture_game(scoreboard_endpoint=None), files, ingest_run_id, data_directory)
    ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    assert fetch_value(database, "SELECT count(*) FROM load_failure") == 0


# skipped lines


def skipping_load_game(monkeypatch, results: list) -> None:
    """
    wraps load_game so each call loads for real, then returns the next of
    results: a skipped_lines count, or an exception to raise instead.
    """
    real_load_game = ingest.load_game
    remaining = iter(results)

    def fake_load_game(connection, game, ingest_run_id):
        result = real_load_game(connection, game, ingest_run_id)
        outcome = next(remaining)
        if isinstance(outcome, Exception):
            raise outcome
        return dataclasses.replace(result, skipped_lines=outcome)

    monkeypatch.setattr(ingest, "load_game", fake_load_game)


def test_load_games_game_with_skipped_lines_is_retried_next_run(
    database, raw_store, data_directory, ingest_run_id, monkeypatch
):
    """
    a game that still has skipped lines after the second pass isn't marked
    loaded, so the next run loads it again, and once it loads clean it's
    marked.
    """
    skipping_load_game(monkeypatch, [1, 1, 1, 1, 0])
    files = fetched_files(raw_store, GAME_ENDPOINTS)

    first = ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    assert first.loaded == 1
    assert fetch_value(database, "SELECT count(*) FROM raw_response") == 0

    # skipped in both passes again, then clean in the third run's first pass
    assert ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory).loaded == 1
    assert ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory).loaded == 1
    assert fetch_value(database, "SELECT count(*) FROM raw_response") == 3
    assert ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory).loaded == 0


def test_load_games_reload_pass_failure_counts_once(database, raw_store, data_directory, ingest_run_id, monkeypatch):
    """
    a game that loads in the first pass and fails in the second is failed,
    not loaded and failed.
    """
    skipping_load_game(monkeypatch, [1, LoadError("broken on reload")])
    files = fetched_files(raw_store, GAME_ENDPOINTS)
    summary = ingest.load_games(database, raw_store, fixture_game(), files, ingest_run_id, data_directory)
    assert summary.loaded == 0
    assert [failed.game_id for failed in summary.failed_games] == [GAME_ID]


# run lifecycle


def test_start_run_closes_abandoned_runs(database):
    """
    a run that was killed leaves its row running. The next run holds the
    lock, so that row can't be a run still going.
    """
    killed = ingest.start_run(database, "backfill")
    ingest.start_run(database, "poll")
    row = database.execute(
        "SELECT status, error, finished_at IS NOT NULL FROM ingest_run WHERE ingest_run_id = %s", (killed,)
    )
    assert row.fetchone() == ("failed", "abandoned", True)


def test_exit_on_sigterm_raises_system_exit():
    with pytest.raises(SystemExit) as raised:
        cli.exit_on_sigterm(signal.SIGTERM, None)
    assert raised.value.code == 128 + signal.SIGTERM


@pytest.fixture
def fetch_cache(data_directory) -> None:
    """
    completes the fixture cache for a run of the 2025 season with one game:
    a schedule with its one date, and the scoreboard cut down to that game.
    Everything is final and cached, so a run makes no requests.
    """
    raw = data_directory / "raw"
    schedule = raw / "schedule-alt" / "volleyball-women" / "d1" / "2025.json"
    schedule.parent.mkdir(parents=True)
    games = [{"contestDate": "12/05/2025", "count": 1}]
    schedule.write_text(json.dumps({"data": {"schedules": {"games": games, "today": ""}}}))

    scoreboard = raw / "scoreboard" / "volleyball-women" / "d1" / "2025" / "12" / "05.json"
    content = json.loads(scoreboard.read_bytes())
    content["games"] = [entry for entry in content["games"] if entry["game"]["gameID"] == GAME_ID]
    scoreboard.write_text(json.dumps(content))


class RecordingApi:
    """
    a client transport that records every request and answers 404, for
    runs that shouldn't touch the network.
    """

    def __init__(self) -> None:
        self.requested_paths = []

    def client(self) -> Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requested_paths.append(request.url.path)
            return httpx.Response(404)

        return Client(
            base_url="https://api.test",
            user_agent="sideout-analytics-tests/0.1",
            transport=httpx.MockTransport(handle),
            rate_limit_per_sec=1000,
        )


def run_2025(connection, api: RecordingApi, raw_store, data_directory) -> ingest.RunSummary:
    return ingest.run(connection, api.client(), raw_store, "test", [2025], dt.date(2026, 11, 11), data_directory)


@pytest.mark.usefixtures("fetch_cache")
def test_run_loads_from_cache_then_skips(database, raw_store, data_directory):
    api = RecordingApi()
    first = run_2025(database, api, raw_store, data_directory)
    second = run_2025(database, api, raw_store, data_directory)

    assert api.requested_paths == []
    assert (first.loads.loaded, second.loads.loaded, second.loads.unchanged) == (1, 0, 1)
    row = database.execute(
        "SELECT status, games_in_index, games_loaded, games_unchanged FROM ingest_run WHERE ingest_run_id = %s",
        (second.ingest_run_id,),
    ).fetchone()
    assert row == ("succeeded", 1, 0, 1)


@pytest.mark.usefixtures("fetch_cache")
def test_run_refuses_while_another_run_holds_the_lock(database, database_schema, raw_store, data_directory):
    database_url, _schema = database_schema
    with connect(database_url) as other_run:
        assert ingest.try_run_lock(other_run)
        with pytest.raises(ingest.RunInProgressError):
            run_2025(database, RecordingApi(), raw_store, data_directory)
    assert fetch_value(database, "SELECT count(*) FROM ingest_run") == 0


@pytest.mark.usefixtures("fetch_cache")
def test_run_interrupted_closes_its_row_and_lock(database, database_schema, raw_store, data_directory, monkeypatch):
    """
    an exception that stops the run, here a SIGTERM turned into SystemExit,
    still closes the row as failed and gives the lock back.
    """

    def interrupted(*_args, **_kwargs):
        raise SystemExit(143)

    monkeypatch.setattr(ingest, "load_games", interrupted)
    with pytest.raises(SystemExit):
        run_2025(database, RecordingApi(), raw_store, data_directory)

    row = database.execute("SELECT status, error, games_loaded FROM ingest_run").fetchone()
    assert row == ("failed", "SystemExit: 143", None)
    database_url, _schema = database_schema
    with connect(database_url) as next_run:
        assert ingest.try_run_lock(next_run)
