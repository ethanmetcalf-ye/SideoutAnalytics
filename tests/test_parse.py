import datetime as dt
import json
import pathlib

import pytest

from sideout_analytics.db.tables import Division, MatchState, PlayRole, PlayType
from sideout_analytics.pipeline.parse import (
    ParseError,
    PlayPlayer,
    SetScore,
    classify_play,
    parse_boxscore,
    parse_game_details,
    parse_play_by_play,
    parse_scoreboard,
    scores_are_swapped,
)

RAW_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "raw"
GAME_FIXTURES = RAW_FIXTURES / "game"
SCOREBOARD_FIXTURE = RAW_FIXTURES / "scoreboard" / "volleyball-women" / "d1" / "2025" / "12" / "05.json"
ALL_GAMES = ["6625808", "6628152", "6639889", "6624785", "6482569", "6500656", "6479084", "6479354"]
PLAY_BY_PLAY_GAMES = ["6625808", "6628152", "6639889", "6482569", "6500656"]
# 6482569 lists set 1's last two rallies out of order (25 before 24), so
# it's left out of the tests that follow the score rally by rally
SCORE_ORDERED_GAMES = ["6625808", "6628152", "6639889", "6500656"]


def details_fixture(game_id: str) -> bytes:
    """
    the raw /game/{id} bytes for a fixture game, see tests/fixtures/README.md.
    """
    return (GAME_FIXTURES / f"{game_id}.json").read_bytes()


def game_file_fixture(game_id: str, name: str) -> bytes:
    """
    the raw bytes of a fixture game's boxscore.json or play-by-play.json.
    """
    return (GAME_FIXTURES / game_id / name).read_bytes()


def parsed_plays(game_id: str):
    """
    a fixture game's plays, parsed with its own details like the loader will.
    """
    match = parse_game_details(details_fixture(game_id))
    return match, parse_play_by_play(game_file_fixture(game_id, "play-by-play.json"), match)


def set_scores(*scores: tuple[int, int]) -> tuple[SetScore, ...]:
    """
    linescores for scores_are_swapped, one (home, away) per set.
    """
    return tuple(
        SetScore(set_number=number, home_points=home, away_points=away)
        for number, (home, away) in enumerate(scores, start=1)
    )


# game details


@pytest.mark.parametrize("game_id", ALL_GAMES)
def test_parse_game_details_reads_every_fixture(game_id):
    match = parse_game_details(details_fixture(game_id))

    assert match.source_game_id == game_id


def test_parse_game_details_returns_match_fields():
    match = parse_game_details(details_fixture("6625808"))

    assert match.source_game_id == "6625808"
    assert match.season == 2026
    assert match.start_time == dt.datetime.fromtimestamp(1787925600, dt.UTC)
    assert match.playbyplay_available is True
    assert match.boxscore_available is True


def test_parse_game_details_maps_f_to_final():
    # details say gameState "F", the schema and the scoreboard say "final"
    match = parse_game_details(details_fixture("6625808"))

    assert match.state == MatchState.FINAL


def test_parse_game_details_reads_venue():
    match = parse_game_details(details_fixture("6628152"))

    assert match.venue == "Liberty Arena"
    assert match.venue_city == "Evansville"
    assert match.venue_state == "IN"


def test_parse_game_details_null_location_has_no_venue():
    # location is null on most details, every venue column is None then
    match = parse_game_details(details_fixture("6625808"))

    assert match.venue is None
    assert match.venue_city is None
    assert match.venue_state is None


def test_parse_game_details_no_play_by_play():
    match = parse_game_details(details_fixture("6624785"))

    assert match.playbyplay_available is False


def test_parse_game_details_teams_home_first():
    match = parse_game_details(details_fixture("6625808"))
    home, away = match.teams

    assert home.seoname == "kennesaw-st"
    assert home.is_home is True
    assert home.is_winner is True
    assert home.sets_won == 3
    assert home.source_team_id == "45832"
    assert home.team_name == "Kennesaw St."
    assert home.division == "d1"
    assert away.seoname == "citadel"
    assert away.is_winner is False
    assert away.sets_won == 0
    assert away.rank_at_match is None
    assert away.seed is None


def test_parse_game_details_reads_linescores():
    match = parse_game_details(details_fixture("6628152"))

    assert match.set_scores == set_scores((24, 26), (25, 16), (18, 25), (25, 23), (13, 15))


def test_parse_game_details_unknown_state_raises():
    details = json.loads(details_fixture("6625808"))
    details["contests"][0]["gameState"] = "X"

    with pytest.raises(ParseError, match="unknown gameState 'X'"):
        parse_game_details(json.dumps(details).encode())


def test_parse_game_details_not_final_has_no_winner():
    details = json.loads(details_fixture("6625808"))
    details["contests"][0]["gameState"] = "I"

    match = parse_game_details(json.dumps(details).encode())

    assert match.state == MatchState.LIVE
    assert [team.is_winner for team in match.teams] == [None, None]


@pytest.mark.parametrize("content", [b"not json", b"{}", b'{"contests": []}', b"[]"])
def test_parse_game_details_malformed_raises(content):
    with pytest.raises(ParseError):
        parse_game_details(content)


def test_parse_game_details_ordinal_set_labels():
    # 2025 linescores sometimes say "1st", "2nd", ... instead of "1", "2"
    match = parse_game_details(details_fixture("6482569"))

    assert match.set_scores == set_scores((19, 25), (23, 25), (13, 25))


def test_parse_game_details_unknown_set_label_raises():
    details = json.loads(details_fixture("6625808"))
    details["contests"][0]["linescores"][0]["period"] = "OT"

    with pytest.raises(ParseError, match="unknown set label 'OT'"):
        parse_game_details(json.dumps(details).encode())


def test_parse_game_details_non_ncaa_team_is_other():
    # Tougaloo is NAIA, its divisionName is null
    match = parse_game_details(details_fixture("6482569"))
    home, away = match.teams

    assert (home.seoname, home.division) == ("tougaloo", Division.OTHER)
    assert (away.seoname, away.division) == ("mississippi-val", Division.D1)


def test_parse_game_details_unknown_division_raises():
    details = json.loads(details_fixture("6625808"))
    details["contests"][0]["teams"][0]["divisionName"] = "d4"

    with pytest.raises(ParseError):
        parse_game_details(json.dumps(details).encode())


def test_parse_game_details_error_names_the_game():
    details = json.loads(details_fixture("6625808"))
    details["contests"][0]["seasonYear"] = "abc"

    with pytest.raises(ParseError, match="game 6625808"):
        parse_game_details(json.dumps(details).encode())


# box score


def test_parse_boxscore_maps_player_counts():
    box_score = parse_boxscore(game_file_fixture("6625808", "boxscore.json"))
    line = next(line for line in box_score.lines if line.last_name == "Anderson")

    assert line.seoname == "kennesaw-st"
    assert line.first_name == "Ashlyn"
    assert line.number == "16"
    assert line.position == "OH"
    assert line.started is True
    assert line.sets_played == 3
    assert line.kills == 8
    assert line.attack_errors == 1
    assert line.attack_attempts == 10
    assert line.blocks_assist == 3
    assert line.digs == 1


def test_parse_boxscore_maps_serving_counts():
    box_score = parse_boxscore(game_file_fixture("6625808", "boxscore.json"))
    line = next(line for line in box_score.lines if line.last_name == "Sowers")

    assert line.aces == 4
    assert line.serve_errors == 2
    assert line.serve_attempts == 16
    assert line.reception_attempts == 11
    assert line.assists == 2
    assert line.set_attempts == 4
    assert line.started is False


def test_parse_boxscore_reads_every_player():
    box_score = parse_boxscore(game_file_fixture("6625808", "boxscore.json"))

    assert len(box_score.lines) == 25
    assert {line.seoname for line in box_score.lines} == {"kennesaw-st", "citadel"}


def test_parse_boxscore_reads_set_attacks():
    box_score = parse_boxscore(game_file_fixture("6625808", "boxscore.json"))
    first_set = next(
        attack for attack in box_score.set_attacks if attack.seoname == "kennesaw-st" and attack.set_number == 1
    )

    assert len(box_score.set_attacks) == 6
    assert first_set.kills == 12
    assert first_set.attack_errors == 5
    assert first_set.attack_attempts == 26


def test_parse_boxscore_blank_names_are_none():
    # every row in this box score has empty names, only the number is set
    box_score = parse_boxscore(game_file_fixture("6624785", "boxscore.json"))

    assert len(box_score.lines) == 22
    assert all(line.first_name is None and line.last_name is None for line in box_score.lines)
    assert all(line.number is not None for line in box_score.lines)
    assert box_score.lines[0].kills == 7


# "" is only allowed for setAttempts, any other blank count fails
@pytest.mark.parametrize("bad_value", ["abc", "-1", None, ""])
def test_parse_boxscore_bad_count_raises(bad_value):
    box_score = json.loads(game_file_fixture("6625808", "boxscore.json"))
    box_score["teamBoxscore"][0]["playerStats"][0]["kills"] = bad_value

    with pytest.raises(ParseError):
        parse_boxscore(json.dumps(box_score).encode())


def test_parse_boxscore_blank_set_attempts_are_none():
    # setAttempts is "" for every player in this match, the other counts
    # are filled in
    box_score = parse_boxscore(game_file_fixture("6500656", "boxscore.json"))

    assert len(box_score.lines) == 33
    assert all(line.set_attempts is None for line in box_score.lines)
    assert all(line.assists is not None for line in box_score.lines)


def test_parse_boxscore_error_names_the_game():
    box_score = json.loads(game_file_fixture("6625808", "boxscore.json"))
    box_score["teamBoxscore"][0]["playerStats"][0]["kills"] = "abc"

    with pytest.raises(ParseError, match="game 6625808"):
        parse_boxscore(json.dumps(box_score).encode())


def test_parse_boxscore_missing_field_raises():
    box_score = json.loads(game_file_fixture("6625808", "boxscore.json"))
    del box_score["teamBoxscore"][0]["playerStats"][0]["digs"]

    with pytest.raises(ParseError, match="digs"):
        parse_boxscore(json.dumps(box_score).encode())


# scoreboard


def scoreboard_entry(bracket_round: object = "", home_conference: str = "big-ten") -> bytes:
    """
    a one game scoreboard with only the fields parse_scoreboard reads.
    """
    return json.dumps(
        {
            "games": [
                {
                    "game": {
                        "gameID": "1",
                        "bracketRound": bracket_round,
                        "home": {"names": {"seo": "home-u"}, "conferences": [{"conferenceSeo": home_conference}]},
                        "away": {"names": {"seo": "away-u"}, "conferences": [{"conferenceSeo": "sec"}]},
                    }
                }
            ]
        }
    ).encode()


def test_parse_scoreboard_reads_tournament_rounds():
    # the first two rounds of the 2025 NCAA tournament were on 2025-12-05
    games = parse_scoreboard(SCOREBOARD_FIXTURE.read_bytes())

    assert games["6500656"].bracket_round == "1"
    assert sorted({game.bracket_round for game in games.values()}) == ["1", "2"]


def test_parse_scoreboard_reads_conferences():
    games = parse_scoreboard(SCOREBOARD_FIXTURE.read_bytes())

    assert games["6500656"].conferences == {"nebraska": "big-ten", "long-island": "nec"}


def test_parse_scoreboard_outside_tournament_is_none():
    assert parse_scoreboard(scoreboard_entry())["1"].bracket_round is None


def test_parse_scoreboard_blank_conference_is_none():
    games = parse_scoreboard(scoreboard_entry(home_conference=""))

    assert games["1"].conferences == {"home-u": None, "away-u": "sec"}


def test_parse_scoreboard_missing_field_raises():
    content = json.dumps({"games": [{"game": {"gameID": "1"}}]}).encode()

    with pytest.raises(ParseError, match="bracketRound"):
        parse_scoreboard(content)


# play text


@pytest.mark.parametrize(
    ("text", "play_type", "players"),
    [
        (
            "Kill by Elizabeth Miller (from Greta Pleuss).",
            PlayType.KILL,
            [("Elizabeth Miller", PlayRole.ATTACKER), ("Greta Pleuss", PlayRole.SETTER)],
        ),
        ("Kill by Olivia Burrage.", PlayType.KILL, [("Olivia Burrage", PlayRole.ATTACKER)]),
        (
            "Attack error by Emma Pastusic (block by Anna Ofenheusle, Olivia Burrage).",
            PlayType.ATTACK_ERROR,
            [
                ("Emma Pastusic", PlayRole.ATTACKER),
                ("Anna Ofenheusle", PlayRole.BLOCKER),
                ("Olivia Burrage", PlayRole.BLOCKER),
            ],
        ),
        ("Attack error by Cameron Reese.", PlayType.ATTACK_ERROR, [("Cameron Reese", PlayRole.ATTACKER)]),
        (
            "Attack error by Audrey Flanagan (block by ).",
            PlayType.ATTACK_ERROR,
            [("Audrey Flanagan", PlayRole.ATTACKER)],
        ),
        (
            "Attack error by Aisha Aiono (block by Jovana Zelenovic; Milla Lugo).",
            PlayType.ATTACK_ERROR,
            [
                ("Aisha Aiono", PlayRole.ATTACKER),
                ("Jovana Zelenovic", PlayRole.BLOCKER),
                ("Milla Lugo", PlayRole.BLOCKER),
            ],
        ),
        ("Service ace (Haley Patterson).", PlayType.SERVICE_ACE, [("Haley Patterson", PlayRole.ACED_RECEIVER)]),
        ("Service ace (Team).", PlayType.SERVICE_ACE, []),
        ("Service ace (TEAM).", PlayType.SERVICE_ACE, []),
        # no space before "("
        (
            "Attack error by Bella Correia(block by Bergen Reilly; Andi Jackson).",
            PlayType.ATTACK_ERROR,
            [
                ("Bella Correia", PlayRole.ATTACKER),
                ("Bergen Reilly", PlayRole.BLOCKER),
                ("Andi Jackson", PlayRole.BLOCKER),
            ],
        ),
        (
            "Attack error by Taylor Landfair (from Bergen Reilly).",
            PlayType.ATTACK_ERROR,
            [("Taylor Landfair", PlayRole.ATTACKER), ("Bergen Reilly", PlayRole.SETTER)],
        ),
        # no attacker recorded
        ("Attack error by (block by Ally Chandler)", PlayType.ATTACK_ERROR, [("Ally Chandler", PlayRole.BLOCKER)]),
        ("Service error.", PlayType.SERVICE_ERROR, []),
        ("Bad set by Kaeley Walz.", PlayType.BAD_SET, [("Kaeley Walz", PlayRole.SETTER)]),
        ("Ball handling error by Lynney Tarnow", PlayType.BALL_HANDLING_ERROR, []),
        (
            "Kill by Albertine van der Goot (from Payton Rolfsen).",
            PlayType.KILL,
            [("Albertine van der Goot", PlayRole.ATTACKER), ("Payton Rolfsen", PlayRole.SETTER)],
        ),
        ("Timeout Wisconsin.", PlayType.OTHER, []),
    ],
)
def test_classify_play(text, play_type, players):
    assert classify_play(text) == (play_type, [PlayPlayer(name=name, role=role) for name, role in players])


# swapped scores


def test_scores_are_swapped_matching_scores():
    assert scores_are_swapped({1: (25, 19), 2: (25, 16)}, set_scores((25, 19), (25, 16))) is False


def test_scores_are_swapped_reversed_scores():
    assert scores_are_swapped({1: (19, 25), 2: (16, 25)}, set_scores((25, 19), (25, 16))) is True


def test_scores_are_swapped_mixed_sets_not_swapped():
    # one set reversed and one not is a broken feed, not a swap
    assert scores_are_swapped({1: (19, 25), 2: (25, 16)}, set_scores((25, 19), (25, 16))) is False


def test_scores_are_swapped_truncated_feed_not_swapped():
    # a feed that stops mid set matches neither way
    assert scores_are_swapped({1: (8, 6)}, set_scores((25, 19))) is False


def test_scores_are_swapped_no_plays_not_swapped():
    assert scores_are_swapped({}, set_scores((25, 19))) is False


# play-by-play


@pytest.mark.parametrize("game_id", PLAY_BY_PLAY_GAMES)
def test_parse_play_by_play_one_play_per_point(game_id):
    # every rally scores one point, so after dropping lineups, duplicates and
    # scoreless rows the count matches the linescores exactly
    match, plays = parsed_plays(game_id)

    assert len(plays) == sum(score.home_points + score.away_points for score in match.set_scores)


@pytest.mark.parametrize("game_id", SCORE_ORDERED_GAMES)
def test_parse_play_by_play_last_rally_matches_linescores(game_id):
    # 6628152 and 6639889 have home and away swapped in the raw feed
    match, plays = parsed_plays(game_id)
    last_scores = {play.set_number: (play.home_score, play.away_score) for play in plays}

    assert last_scores == {score.set_number: (score.home_points, score.away_points) for score in match.set_scores}


@pytest.mark.parametrize("game_id", SCORE_ORDERED_GAMES)
def test_parse_play_by_play_scoring_team_gains_the_point(game_id):
    match, plays = parsed_plays(game_id)
    home = match.teams[0].seoname
    previous = {}

    for play in plays:
        home_before, away_before = previous.get(play.set_number, (0, 0))
        if play.scoring_seoname == home:
            assert (play.home_score, play.away_score) == (home_before + 1, away_before)
        else:
            assert (play.home_score, play.away_score) == (home_before, away_before + 1)
        previous[play.set_number] = (play.home_score, play.away_score)


def test_parse_play_by_play_first_rallies():
    _, plays = parsed_plays("6625808")
    first, second, third = plays[:3]

    assert (first.set_number, first.sequence) == (1, 1)
    assert first.play_type == PlayType.SERVICE_ERROR
    assert first.scoring_seoname == "kennesaw-st"
    assert first.serving_seoname is None
    assert (first.home_score, first.away_score) == (1, 0)
    assert second.play_type == PlayType.ATTACK_ERROR
    assert second.serving_seoname == "kennesaw-st"
    assert [player.name for player in second.players] == ["Emma Pastusic", "Anna Ofenheusle", "Olivia Burrage"]
    assert third.scoring_seoname == "citadel"
    assert third.serving_seoname == "kennesaw-st"


def test_parse_play_by_play_sequence_restarts_each_set():
    _, plays = parsed_plays("6625808")

    for set_number in (1, 2, 3):
        sequences = [play.sequence for play in plays if play.set_number == set_number]
        assert sequences == list(range(1, len(sequences) + 1))
    assert next(play for play in plays if play.set_number == 2).serving_seoname is None


def test_parse_play_by_play_skips_lineups():
    _, plays = parsed_plays("6625808")

    assert not any("starters:" in play.play_text or "subs:" in play.play_text for play in plays)


def test_parse_play_by_play_drops_back_to_back_duplicates():
    _, plays = parsed_plays("6639889")
    keys = [(play.set_number, play.home_score, play.away_score) for play in plays]

    assert len(keys) == len(set(keys))


def test_parse_play_by_play_collapses_double_spaces():
    # the feed writes "Ava  Grevengoed", the box score "Ava Grevengoed"
    _, plays = parsed_plays("6628152")
    names = {player.name for play in plays for player in play.players}

    assert "Ava Grevengoed" in names
    assert not any("  " in name for name in names)


def test_parse_play_by_play_bad_score_raises():
    match = parse_game_details(details_fixture("6625808"))
    play_by_play = json.loads(game_file_fixture("6625808", "play-by-play.json"))
    play_by_play["periods"][0]["playbyplayStats"][2]["plays"][0]["homeScore"] = "abc"

    with pytest.raises(ParseError, match="6625808"):
        parse_play_by_play(json.dumps(play_by_play).encode(), match)
