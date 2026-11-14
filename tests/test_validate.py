import datetime as dt
import json
import pathlib
import shutil

import pytest

from sideout_analytics.db import validate
from sideout_analytics.db.tables import MatchState
from sideout_analytics.pipeline import ingest
from sideout_analytics.pipeline.cache import RawStore, hash_content
from sideout_analytics.pipeline.fetch import FetchedFile, Game
from sideout_analytics.pipeline.parse import StatLeader

KNOWN = ("set_points_last_play", "each set's points equal its last play's score", ("game", "set"), (0, 1))

RAW_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "raw"
GAME_ID = "6500656"
SCOREBOARD_ENDPOINT = "/scoreboard/volleyball-women/d1/2025/12/05"
GAME_ENDPOINTS = [f"/game/{GAME_ID}", f"/game/{GAME_ID}/boxscore", f"/game/{GAME_ID}/play-by-play"]


@pytest.fixture
def raw_store(tmp_path) -> RawStore:
    """
    the fixtures as a raw cache, with a 2025 schedule listing the fixture
    scoreboard's date.
    """
    shutil.copytree(RAW_FIXTURES, tmp_path / "raw")
    store = RawStore(tmp_path / "raw")
    schedule = {"data": {"schedules": {"games": [{"contestDate": "12/05/2025", "count": 25}]}}}
    store.write_cache_to_disc(
        json.dumps(schedule).encode(), store.map_endpoint_cache_file("/schedule-alt/volleyball-women/d1/2025")
    )
    return store


@pytest.fixture
def loaded_game(database, raw_store) -> None:
    """
    the fixture game loaded the way a run loads it.
    """
    files = {}
    for endpoint in GAME_ENDPOINTS:
        cache_file = raw_store.map_endpoint_cache_file(endpoint)
        files[endpoint] = FetchedFile(endpoint, cache_file, hash_content(cache_file.read_bytes()))
    games = {GAME_ID: Game(GAME_ID, 0, MatchState.FINAL, scoreboard_endpoint=SCOREBOARD_ENDPOINT)}
    run_id = ingest.start_run(database, "test")
    assert ingest.load_games(database, raw_store, games, files, run_id, raw_store.cache_directory.parent).loaded == 1


def leader(rank: int, name: str, team: str, kills: int) -> StatLeader:
    return StatLeader(rank=rank, name=name, team=team, value=kills)


# no database


def test_compare_leaders_agrees_whatever_the_order_of_ties():
    published = [leader(1, "Allie Sweet", "UNC Greensboro", 543), leader(2, "Ana Paz", "LSU", 400)]
    published += [leader(2, "Bea Ruiz", "LSU", 400)]
    database = [("Allie Sweet", "UNC Greensboro", 543), ("Bea Ruiz", "LSU", 400), ("Ana  Paz", "LSU", 400)]
    assert validate.compare_leaders(published, database, top=3) == ()


def test_compare_leaders_lists_a_kills_difference_and_an_unpublished_player():
    published = [leader(1, "Allie Sweet", "UNC Greensboro", 543)]
    database = [("Allie Sweet", "UNC Greensboro", 540), ("Zoe Ott", "Iowa", 539)]
    assert validate.compare_leaders(published, database, top=2) == (
        (1, "Allie Sweet", "UNC Greensboro", 543, 540),
        ("not published, database #2", "Zoe Ott", "Iowa", "-", 539),
    )


def test_compare_leaders_missing_player_is_not_found():
    rows = validate.compare_leaders([leader(1, "Allie Sweet", "UNC Greensboro", 543)], [], top=1)
    assert rows == ((1, "Allie Sweet", "UNC Greensboro", 543, "not found"),)


def test_check_schedule_counts_lists_dates_that_differ(raw_store):
    # the fixture scoreboard has 25 games, the schedule says 25
    assert validate.check_schedule_counts(raw_store, 2025).passed

    schedule = {"data": {"schedules": {"games": [{"contestDate": "12/05/2025", "count": 26}]}}}
    raw_store.write_cache_to_disc(
        json.dumps(schedule).encode(), raw_store.map_endpoint_cache_file("/schedule-alt/volleyball-women/d1/2025")
    )
    assert validate.check_schedule_counts(raw_store, 2025).rows == (("2025-12-05", 26, 25),)


def test_check_schedule_counts_uncached_scoreboard_is_none(tmp_path):
    store = RawStore(tmp_path / "raw")
    schedule = {"data": {"schedules": {"games": [{"contestDate": "09/01/2025", "count": 3}]}}}
    store.write_cache_to_disc(
        json.dumps(schedule).encode(), store.map_endpoint_cache_file("/schedule-alt/volleyball-women/d1/2025")
    )
    assert validate.check_schedule_counts(store, 2025).rows == (("2025-09-01", 3, None),)


def test_season_dates_without_a_schedule_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="schedule-alt"):
        validate.season_dates(RawStore(tmp_path / "raw"), 2025)


# against the database


@pytest.mark.usefixtures("loaded_game")
def test_sql_checks_pass_on_a_clean_game(database):
    assert [(result.name, result.rows) for result in validate.check_sql(database, 2025) if result.rows] == []


@pytest.mark.usefixtures("loaded_game")
def test_sql_checks_find_a_wrong_set_score(database):
    """
    one set's points changed: the last play and sets_won both disagree.
    """
    database.execute(
        """
        UPDATE team_set_stats SET points = 30
        WHERE set_number = 1 AND team_id = (SELECT team_id FROM match_team WHERE NOT is_home)
        """
    )
    failing = {result.name: result.rows for result in validate.check_sql(database, 2025) if result.rows}

    last_play = failing["each set's points equal its last play's score"]
    assert [(game, set_number) for game, set_number, *_ in last_play] == [(GAME_ID, 1)]
    assert len(failing["sets_won and is_winner agree with the set points"]) == 2


@pytest.mark.usefixtures("loaded_game")
def test_sql_checks_find_box_score_that_differs(database):
    database.execute(
        "UPDATE box_score_line SET kills = kills + 1 WHERE player_id = (SELECT min(player_id) FROM box_score_line)"
    )
    failing = {result.name: result.rows for result in validate.check_sql(database, 2025) if result.rows}
    assert list(failing) == ["team kills, attack errors and attempts over sets equal the box score"]


@pytest.mark.usefixtures("loaded_game")
def test_check_final_games_loaded_lists_the_others(database, raw_store):
    result = validate.check_final_games_loaded(database, raw_store, 2025)

    missing = [game for game, _date, _state in result.rows]
    assert GAME_ID not in missing
    assert len(missing) == 24
    assert {state for *_, state in result.rows} == {"no match row"}


@pytest.mark.usefixtures("loaded_game")
def test_database_kills_leaders_stops_at_the_date(database):
    assert validate.database_kills_leaders(database, 2025, dt.date(2025, 12, 4)) == []
    leaders = validate.database_kills_leaders(database, 2025, dt.date(2025, 12, 5), top=3)
    assert [kills for *_, kills in leaders] == sorted((kills for *_, kills in leaders), reverse=True)


# known exceptions


def result_with(*rows: tuple) -> validate.CheckResult:
    return validate.CheckResult(*KNOWN, rows)


def exceptions_file(tmp_path, body: str) -> pathlib.Path:
    path = tmp_path / "exceptions.toml"
    path.write_text(body)
    return path


def test_checked_in_exceptions_load():
    exceptions = validate.load_exceptions()
    assert sum(len(rows) for rows in exceptions.values()) == 89
    assert set(exceptions) <= validate.CHECK_IDS


def test_check_key_lengths_match_the_sql_checks():
    for check_id, _name, _columns, key_columns, _query in validate.SQL_CHECKS:
        assert validate.CHECK_KEY_LENGTHS[check_id] == len(key_columns)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[[exception]]\ncheck = "set_points"\nkey = ["1", "1"]\nreason = "x"\n', "unknown check"),
        ('[[exception]]\ncheck = "set_points_last_play"\nkey = ["1"]\nreason = "x"\n', "needs 2 key parts"),
        ('[[exception]]\ncheck = "set_points_last_play"\nkey = ["1", "1"]\nreason = " "\n', "has no reason"),
        ('[[exception]]\ncheck = "schedule_counts"\nkey = ["2026-08-29"]\nreason = "x"\n' * 2, "listed twice"),
    ],
)
def test_load_exceptions_rejects_a_bad_entry(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        validate.load_exceptions(exceptions_file(tmp_path, body))


def test_apply_exceptions_splits_known_new_and_resolved():
    exceptions = {"set_points_last_play": {("6624379", "1"): "feed short", ("1", "9"): "gone now"}}
    result = validate.apply_exceptions(result_with(("6624379", 1), ("7", 2)), exceptions)

    assert result.known_rows == (("6624379", 1),)
    assert result.new_rows == (("7", 2),)
    assert result.resolved == (("1", "9"),)
    assert not result.passed


def test_only_known_rows_pass():
    exceptions = {"set_points_last_play": {("6624379", "1"): "feed short"}}
    assert validate.apply_exceptions(result_with(("6624379", 1)), exceptions).passed
    assert not result_with(("6624379", 1)).passed
