import pathlib
import shutil

import pytest

from sideout_analytics.db.tables import MatchState
from sideout_analytics.pipeline import ingest
from sideout_analytics.pipeline.cache import RawStore, hash_content
from sideout_analytics.pipeline.client import FailedFetchError, NotAvailableYetError
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
