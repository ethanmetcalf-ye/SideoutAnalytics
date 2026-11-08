import pathlib

import pytest

from sideout_analytics.db.loader import LoadError, ParsedGame, load_game, name_key, on_scoring_team
from sideout_analytics.db.tables import Base, PlayRole, PlayType
from sideout_analytics.pipeline.parse import (
    ScoreboardGame,
    parse_boxscore,
    parse_game_details,
    parse_play_by_play,
    parse_scoreboard,
)

RAW_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "raw"
GAME_FIXTURES = RAW_FIXTURES / "game"
SCOREBOARD_FIXTURE = RAW_FIXTURES / "scoreboard" / "volleyball-women" / "d1" / "2025" / "12" / "05.json"
ALL_GAMES = ["6625808", "6628152", "6639889", "6624785", "6482569", "6500656"]


def fixture_game(game_id: str) -> ParsedGame:
    """
    a fixture game parsed the way the pipeline will. 6500656 is on the
    fixture scoreboard, the others get a made-up conference per team, except
    NAIA Tougaloo, which has none like on the real scoreboard.
    """
    game_directory = GAME_FIXTURES / game_id
    match = parse_game_details((GAME_FIXTURES / f"{game_id}.json").read_bytes())
    if game_id == "6500656":
        scoreboard = parse_scoreboard(SCOREBOARD_FIXTURE.read_bytes())[game_id]
    else:
        conferences = {team.seoname: None if team.seoname == "tougaloo" else "conference" for team in match.teams}
        scoreboard = ScoreboardGame(bracket_round=None, conferences=conferences)
    play_by_play = game_directory / "play-by-play.json"
    return ParsedGame(
        match=match,
        scoreboard=scoreboard,
        box_score=parse_boxscore((game_directory / "boxscore.json").read_bytes()),
        plays=parse_play_by_play(play_by_play.read_bytes(), match) if play_by_play.exists() else (),
    )


def table_counts(connection) -> dict[str, int]:
    """
    the row count of every table.
    """
    return {
        table.name: connection.execute(f"SELECT count(*) FROM {table.name}").fetchone()[0]  # noqa: S608
        for table in Base.metadata.sorted_tables
    }


def fetch_value(connection, query: str, *params):
    """
    the first column of the first row of a query.
    """
    return connection.execute(query, params).fetchone()[0]


@pytest.fixture
def ingest_run_id(database) -> int:
    return fetch_value(
        database,
        "INSERT INTO ingest_run (job, started_at, status) VALUES ('test', now(), 'running') RETURNING ingest_run_id",
    )


# names


@pytest.mark.parametrize(
    ("first_name", "last_name", "expected"),
    [
        ("Ava", "Grevengoed", "ava grevengoed"),
        ("Ava", "  Grevengoed ", "ava grevengoed"),
        ("Zoë", "Muñoz", "zoe munoz"),
        ("Kaylee", "O'Brien-Smith", "kaylee obriensmith"),
        # some box scores put the whole name in lastName
        (None, "Caroline Przystup", "caroline przystup"),
        ("", "Cherrish  Robinson", "cherrish robinson"),
    ],
)
def test_name_key_normalizes(first_name, last_name, expected):
    assert name_key(first_name, last_name) == expected


def test_name_key_matches_play_text_name():
    assert name_key("Ava", "Grevengoed") == name_key(None, "Ava Grevengoed")


@pytest.mark.parametrize(
    ("play_type", "role", "expected"),
    [
        (PlayType.KILL, PlayRole.ATTACKER, True),
        (PlayType.KILL, PlayRole.SETTER, True),
        (PlayType.ATTACK_ERROR, PlayRole.ATTACKER, False),
        (PlayType.ATTACK_ERROR, PlayRole.BLOCKER, True),
        (PlayType.SERVICE_ACE, PlayRole.ACED_RECEIVER, False),
        (PlayType.BAD_SET, PlayRole.SETTER, False),
    ],
)
def test_on_scoring_team(play_type, role, expected):
    assert on_scoring_team(play_type, role) is expected


# loading


def test_load_game_loads_match(database, ingest_run_id):
    game = fixture_game("6625808")

    load_game(database, game, ingest_run_id)

    counts = table_counts(database)
    assert counts["match"] == 1
    assert counts["match_team"] == 2
    assert counts["team"] == 2
    assert counts["team_season"] == 2
    assert counts["season"] == 1
    assert counts["box_score_line"] == len(game.box_score.lines)
    assert counts["player"] == len(game.box_score.lines)
    assert counts["roster"] == len(game.box_score.lines)
    assert counts["team_set_stats"] == 2 * len(game.match.set_scores)
    assert counts["play"] == len(game.plays)
    assert counts["play_player"] > 0


@pytest.mark.parametrize("game_id", ALL_GAMES)
def test_load_game_twice_changes_no_counts(database, ingest_run_id, game_id):
    game = fixture_game(game_id)

    load_game(database, game, ingest_run_id)
    first = table_counts(database)
    load_game(database, game, ingest_run_id)

    assert table_counts(database) == first


def test_load_game_twice_keeps_ids(database, ingest_run_id):
    game = fixture_game("6625808")

    first = load_game(database, game, ingest_run_id)
    second = load_game(database, game, ingest_run_id)

    assert second.match_id == first.match_id
    assert fetch_value(database, "SELECT max(player_id) FROM player") == len(game.box_score.lines)


def test_load_game_replaces_plays(database, ingest_run_id):
    game = fixture_game("6625808")
    load_game(database, game, ingest_run_id)

    shorter = ParsedGame(match=game.match, scoreboard=game.scoreboard, box_score=game.box_score, plays=game.plays[:10])
    load_game(database, shorter, ingest_run_id)

    assert table_counts(database)["play"] == 10


def test_load_game_reads_scoreboard(database, ingest_run_id):
    load_game(database, fixture_game("6500656"), ingest_run_id)

    assert fetch_value(database, "SELECT bracket_round FROM match") == "1"
    conferences = database.execute(
        "SELECT team.seoname, conference.name FROM team_season JOIN team USING (team_id) JOIN conference USING "
        "(conference_id) ORDER BY team.seoname"
    ).fetchall()
    assert conferences == [("long-island", "nec"), ("nebraska", "big-ten")]


def test_load_game_blank_set_attempts_are_null(database, ingest_run_id):
    load_game(database, fixture_game("6500656"), ingest_run_id)

    assert fetch_value(database, "SELECT count(*) FROM box_score_line WHERE set_attempts IS NOT NULL") == 0


def test_load_game_non_ncaa_team_has_no_conference(database, ingest_run_id):
    load_game(database, fixture_game("6482569"), ingest_run_id)

    row = database.execute(
        "SELECT division, conference_id FROM team_season JOIN team USING (team_id) WHERE seoname = 'tougaloo'"
    ).fetchone()
    assert row == ("other", None)


def test_load_game_skips_lines_without_names(database, ingest_run_id):
    game = fixture_game("6624785")

    result = load_game(database, game, ingest_run_id)

    assert result.skipped_lines == len(game.box_score.lines)
    counts = table_counts(database)
    assert counts["box_score_line"] == 0
    assert counts["match_team"] == 2
    assert counts["team_set_stats"] == 2 * len(game.match.set_scores)


def test_load_game_links_play_players(database, ingest_run_id):
    result = load_game(database, fixture_game("6625808"), ingest_run_id)

    # one kill is "from" a player on the other team (Kill by Ashlyn Anderson
    # (from Greta Pleuss)), likely an overpass, so there's no setter to link
    assert result.unmatched_names == 1
    # every kill has its attacker, on the team that scored
    unlinked = fetch_value(
        database,
        """
        SELECT count(*) FROM play
        WHERE play_type = 'kill' AND NOT EXISTS (
            SELECT 1 FROM play_player JOIN box_score_line USING (player_id)
            WHERE play_player.play_id = play.play_id AND role = 'attacker'
              AND box_score_line.match_id = play.match_id AND box_score_line.team_id = play.scoring_team_id
        )
        """,
    )
    assert unlinked == 0


def test_load_game_counts_unmatched_names(database, ingest_run_id):
    game = fixture_game("6625808")
    renamed = tuple(
        play.model_copy(update={"players": tuple(p.model_copy(update={"name": "Nobody Here"}) for p in play.players)})
        for play in game.plays
    )
    named = sum(len(play.players) for play in game.plays)

    result = load_game(database, ParsedGame(game.match, game.scoreboard, game.box_score, renamed), ingest_run_id)

    assert result.unmatched_names == named
    assert table_counts(database)["play_player"] == 0


def test_load_game_reuses_players_in_the_same_season(database, ingest_run_id):
    game = fixture_game("6625808")
    load_game(database, game, ingest_run_id)
    rematch = game.match.model_copy(update={"source_game_id": "rematch"})

    load_game(database, ParsedGame(rematch, game.scoreboard, game.box_score, game.plays), ingest_run_id)

    counts = table_counts(database)
    assert counts["match"] == 2
    assert counts["player"] == len(game.box_score.lines)
    assert counts["roster"] == len(game.box_score.lines)
    assert counts["box_score_line"] == 2 * len(game.box_score.lines)


def test_load_game_reuses_players_from_the_previous_season(database, ingest_run_id):
    game = fixture_game("6625808")
    load_game(database, game, ingest_run_id)
    next_season = game.match.model_copy(update={"source_game_id": "next", "season": game.match.season + 1})

    load_game(database, ParsedGame(next_season, game.scoreboard, game.box_score, game.plays), ingest_run_id)

    counts = table_counts(database)
    assert counts["player"] == len(game.box_score.lines)
    assert counts["roster"] == 2 * len(game.box_score.lines)


def test_load_game_keeps_result_first_seen_at(database, ingest_run_id):
    game = fixture_game("6625808")
    load_game(database, game, ingest_run_id)
    first_seen = fetch_value(database, "SELECT result_first_seen_at FROM match")

    load_game(database, game, ingest_run_id)

    assert first_seen is not None
    assert fetch_value(database, "SELECT result_first_seen_at FROM match") == first_seen


def test_load_game_without_box_score(database, ingest_run_id):
    game = fixture_game("6625808")

    load_game(database, ParsedGame(game.match, game.scoreboard), ingest_run_id)

    counts = table_counts(database)
    assert counts["match_team"] == 2
    assert counts["box_score_line"] == 0
    assert counts["team_set_stats"] == 0
    assert counts["play"] == 0


def test_load_game_team_missing_from_scoreboard_raises(database, ingest_run_id):
    game = fixture_game("6625808")
    scoreboard = ScoreboardGame(bracket_round=None, conferences={})

    with pytest.raises(LoadError, match=r"6625808.*isn't on the scoreboard"):
        load_game(database, ParsedGame(game.match, scoreboard, game.box_score, game.plays), ingest_run_id)

    # rolled back, only the ingest run is left
    assert sum(table_counts(database).values()) == 1


def test_load_game_same_named_teammates_raise(database, ingest_run_id):
    game = fixture_game("6625808")
    lines = (*game.box_score.lines, game.box_score.lines[0].model_copy(update={"number": "99"}))
    box_score = game.box_score.model_copy(update={"lines": lines})

    with pytest.raises(LoadError, match="two players"):
        load_game(database, ParsedGame(game.match, game.scoreboard, box_score, game.plays), ingest_run_id)

    assert table_counts(database)["match"] == 0


def test_load_game_box_score_missing_a_set_raises(database, ingest_run_id):
    game = fixture_game("6625808")
    box_score = game.box_score.model_copy(update={"set_attacks": game.box_score.set_attacks[1:]})

    with pytest.raises(LoadError, match="box score has no set"):
        load_game(database, ParsedGame(game.match, game.scoreboard, box_score, game.plays), ingest_run_id)

    assert table_counts(database)["match"] == 0
