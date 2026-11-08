"""
Parsers for the cached game files, raw bytes in and typed records out.
Each parser takes the bytes of one /game/{id} file and does no I/O, so the
loader decides where the bytes come from and the tests read fixtures.

Records use source keys (gameID, seoname, player names), never database
IDs, the loader resolves those (docs/schema.md decisions 6 and 7).

Decisions (W2-03):
- A bad file fails as a whole: a missing field or a count that isn't a
  number raises ParseError, and the caller records the game as failed. One
  bad row never silently drops out of a match.
- Play text that matches no known pattern isn't bad, it becomes
  PlayType.OTHER with no players.
- Play-by-play is cleaned here so the output is one Play per rally: starters
  and subs lines, plays without a score, and rows repeated back to back are
  skipped.
- Some feeds have homeScore and visitorScore swapped for the whole match.
  If every set's last score is the reverse of the linescores the scores are
  swapped back, otherwise they're kept and reconciliation catches it.
- Box score rows with blank names are kept with the names as None, the
  loader decides what to do with a player it can't match.
"""

import datetime as dt
import json
import pprint
import re
import sys
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from ..pipeline.cache import RawStore
from .tables import Division, MatchState, PlayRole, PlayType

# details use one letter codes, the scoreboard spells them out. same
# mapping as the API's own normalizeGameState, minus its default to pre
DETAILS_STATES = {"F": MatchState.FINAL, "P": MatchState.PRE, "I": MatchState.LIVE}

SetNumber = Annotated[int, Field(ge=1, le=5)]

# play text patterns, matched against the text with whitespace collapsed.
# names are lazy so the optional (from ...) or (block by ...) and the final
# period aren't swallowed into the name
PLAY_PATTERNS = [
    (PlayType.KILL, re.compile(r"Kill by (?P<attacker>.+?)(?: \(from (?P<setter>.*)\))?\.?")),
    (PlayType.ATTACK_ERROR, re.compile(r"Attack error by (?P<attacker>.+?)(?: \(block by (?P<blockers>.*)\))?\.?")),
    (PlayType.SERVICE_ACE, re.compile(r"Service ace \((?P<aced_receiver>.+)\)\.?")),
    (PlayType.SERVICE_ERROR, re.compile(r"Service error\.?")),
    (PlayType.BAD_SET, re.compile(r"Bad set by (?P<setter>.+?)\.?")),
    # no role in the schema fits the player who made the error
    (PlayType.BALL_HANDLING_ERROR, re.compile(r"Ball handling error by .+?\.?")),
]
LINEUP_PATTERN = re.compile(r".+ (?:starters|subs):.*")


class ParseError(Exception):
    """
    a cached file that can't be turned into records. the original error is
    chained as __cause__.
    """


class Record(BaseModel):
    """
    base for every parsed record. frozen so a record can't change after
    parsing, and extra fields are an error so a typo in a mapping fails here
    instead of being dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class MatchTeam(Record):
    """
    one team's side of a match, for the match_team, team and team_season
    rows.
    """

    source_team_id: str
    seoname: str
    team_name: str
    division: Division
    is_home: bool
    is_winner: bool | None
    sets_won: Annotated[int, Field(ge=0, le=3)] | None
    seed: PositiveInt | None
    rank_at_match: PositiveInt | None


class SetScore(Record):
    """
    one set's points from the details linescores, team_set_stats.points.
    """

    set_number: SetNumber
    home_points: NonNegativeInt
    away_points: NonNegativeInt


class Match(Record):
    """
    a /game/{id} details file, for the match row and its two match_team
    rows. teams is (home, away). bracket_round isn't in the details, it
    comes from the scoreboard.
    """

    source_game_id: str
    season: int
    start_time: dt.datetime
    state: MatchState
    venue: str | None
    venue_city: str | None
    venue_state: str | None
    boxscore_available: bool
    playbyplay_available: bool
    teams: tuple[MatchTeam, MatchTeam]
    set_scores: tuple[SetScore, ...]


class BoxScoreLine(Record):
    """
    one player's row in a box score, box_score_line. Names are None when the
    source leaves them blank. number is text because roster.number is.
    points, hittingPercentage and totalBlocks aren't kept since they're
    calculated from these counts.
    """

    seoname: str
    first_name: str | None
    last_name: str | None
    number: str | None
    position: str | None
    started: bool
    sets_played: Annotated[int, Field(ge=0, le=5)]
    kills: NonNegativeInt
    attack_errors: NonNegativeInt
    attack_attempts: NonNegativeInt
    assists: NonNegativeInt
    set_attempts: NonNegativeInt
    set_errors: NonNegativeInt
    aces: NonNegativeInt
    serve_errors: NonNegativeInt
    serve_attempts: NonNegativeInt
    digs: NonNegativeInt
    reception_attempts: NonNegativeInt
    reception_errors: NonNegativeInt
    blocks_solo: NonNegativeInt
    blocks_assist: NonNegativeInt
    blocking_errors: NonNegativeInt
    ball_handling_errors: NonNegativeInt


class TeamSetAttack(Record):
    """
    one team's attacking in one set from the box score, the kills and
    attack columns of team_set_stats.
    """

    seoname: str
    set_number: SetNumber
    kills: NonNegativeInt
    attack_errors: NonNegativeInt
    attack_attempts: NonNegativeInt


class BoxScore(Record):
    """
    a /game/{id}/boxscore file.
    """

    lines: tuple[BoxScoreLine, ...]
    set_attacks: tuple[TeamSetAttack, ...]


class PlayPlayer(Record):
    """
    a player named in a play's text, play_player.
    """

    name: str
    role: PlayRole


class Play(Record):
    """
    one rally, play. sequence counts from 1 within the set after cleaning.
    serving_seoname is the previous rally's winner, so None for a set's
    first rally. Scores are the home and away scores after the rally.
    """

    set_number: SetNumber
    sequence: PositiveInt
    play_type: PlayType
    scoring_seoname: str
    serving_seoname: str | None
    play_text: str
    home_score: NonNegativeInt
    away_score: NonNegativeInt
    players: tuple[PlayPlayer, ...]


def blank_to_none[T](value: T) -> T | None:
    """
    the API writes missing values as "" or null depending on the field,
    this makes them all None.
    """
    return None if value in ("", None) else value


def collapse_whitespace(text: str) -> str:
    """
    play-by-play names sometimes have double spaces (Ava  Grevengoed) where
    the box score has one, this makes them match.
    """
    return " ".join(text.split())


def parse_game_details(content: bytes) -> Match:
    """
    parses a /game/{id} file. Raises ParseError on a missing field, a bad
    value, or a gameState that isn't F, P or I.
    """
    try:
        contest = json.loads(content)["contests"][0]
        game_state = contest["gameState"]
        if game_state not in DETAILS_STATES:
            raise ValueError(f"unknown gameState {game_state!r}")
        state = DETAILS_STATES[game_state]
        location = contest["location"] or {}

        teams = [
            MatchTeam(
                source_team_id=team["teamId"],
                seoname=team["seoname"],
                team_name=team["nameShort"],
                division=team["divisionName"],
                is_home=team["isHome"],
                # only settled once the match is over, before that the API
                # says false for both
                is_winner=team["isWinner"] if state == MatchState.FINAL else None,
                sets_won=blank_to_none(team["score"]),
                seed=blank_to_none(team["seed"]),
                rank_at_match=blank_to_none(team["teamRank"]),
            )
            for team in contest["teams"]
        ]
        # home first, the play scores and set_scores are home then away
        teams.sort(key=lambda team: not team.is_home)

        set_scores = [
            SetScore(set_number=line["period"], home_points=line["home"], away_points=line["visit"])
            for line in contest["linescores"] or []
        ]

        return Match(
            source_game_id=contest["id"],
            season=contest["seasonYear"],
            start_time=dt.datetime.fromtimestamp(contest["startTimeEpoch"], dt.UTC),
            state=state,
            venue=blank_to_none(location.get("venue")),
            venue_city=blank_to_none(location.get("city")),
            venue_state=blank_to_none(location.get("stateUsps")),
            boxscore_available=contest["hasBoxscore"],
            playbyplay_available=contest["hasPbp"],
            teams=teams,
            set_scores=set_scores,
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ParseError(f"Can't parse game details: {e}") from e


def parse_boxscore(content: bytes) -> BoxScore:
    """
    parses a /game/{id}/boxscore file. Raises ParseError on a missing field
    or a count that isn't a non negative number.
    """
    try:
        data = json.loads(content)
        seonames = {str(team["teamId"]): team["seoname"] for team in data["teams"]}

        lines = []
        set_attacks = []
        for team_box in data["teamBoxscore"]:
            seoname = seonames[str(team_box["teamId"])]
            for player in team_box["playerStats"]:
                number = blank_to_none(player["number"])
                lines.append(
                    BoxScoreLine(
                        seoname=seoname,
                        first_name=blank_to_none(player["firstName"]),
                        last_name=blank_to_none(player["lastName"]),
                        number=None if number is None else str(number),
                        position=blank_to_none(player["position"]),
                        started=player["starter"],
                        sets_played=player["gamesPlayed"],
                        kills=player["kills"],
                        attack_errors=player["attackErrors"],
                        attack_attempts=player["attackAttempts"],
                        assists=player["assists"],
                        set_attempts=player["setAttempts"],
                        set_errors=player["setErrors"],
                        aces=player["serviceAces"],
                        serve_errors=player["serviceErrors"],
                        serve_attempts=player["serveAttempts"],
                        digs=player["digs"],
                        reception_attempts=player["receptionAttempts"],
                        reception_errors=player["receptionErrors"],
                        blocks_solo=player["blockSolos"],
                        blocks_assist=player["blockAssists"],
                        blocking_errors=player["blockingErrors"],
                        ball_handling_errors=player["ballHandlingErrors"],
                    )
                )
            set_attacks += [
                TeamSetAttack(
                    seoname=seoname,
                    set_number=set_stats["game"],
                    kills=set_stats["kills"],
                    attack_errors=set_stats["attackErrors"],
                    attack_attempts=set_stats["attackAttempts"],
                )
                for set_stats in team_box["teamStats"]["sets"]
            ]

        return BoxScore(lines=lines, set_attacks=set_attacks)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ParseError(f"Can't parse box score: {e}") from e


def classify_play(text: str) -> tuple[PlayType, list[PlayPlayer]]:
    """
    the play type and the players named in a play's text. text that matches
    no pattern is OTHER with no players.
    """
    for play_type, pattern in PLAY_PATTERNS:
        found = pattern.fullmatch(text)
        if found is None:
            continue
        players = []
        for group, names in found.groupdict().items():
            if not names:
                continue
            if group == "blockers":
                # "block by )" happens, so empty names are dropped
                players += [PlayPlayer(name=name, role=PlayRole.BLOCKER) for name in names.split(", ") if name]
            else:
                players.append(PlayPlayer(name=names, role=PlayRole(group)))
        return play_type, players
    return PlayType.OTHER, []


def scores_are_swapped(final_scores: dict[int, tuple[int, int]], set_scores: tuple[SetScore, ...]) -> bool:
    """
    whether a feed's homeScore and visitorScore are swapped for the whole
    match. final_scores is each set's last (homeScore, visitorScore) in the
    play-by-play, set_scores are the linescores from the details.
    """
    # an empty feed would pass all() below, and it has nothing to swap
    if not final_scores:
        return False
    linescores = {score.set_number: (score.home_points, score.away_points) for score in set_scores}
    # every set has to be reversed, one that matches or a truncated set
    # means the feed is broken some other way and reconciliation decides
    return all(linescores.get(set_number) == (away, home) for set_number, (home, away) in final_scores.items())


def parse_play_by_play(content: bytes, match: Match) -> tuple[Play, ...]:
    """
    parses a /game/{id}/play-by-play file into one Play per rally. Needs the
    match's details for the linescores, to catch swapped scores. Raises
    ParseError on a missing field or a bad value.
    """
    try:
        data = json.loads(content)
        seonames = {str(team["teamId"]): team["seoname"] for team in data["teams"]}

        # first pass drops the noise and keeps (set, seoname, text, home, away)
        rallies = []
        previous = None
        for period in data["periods"]:
            for entry in period["playbyplayStats"]:
                for play in entry["plays"]:
                    # messy feeds repeat rows back to back with the same
                    # score and clock, so it's the same rally
                    row = (
                        period["periodNumber"],
                        entry["teamId"],
                        play["playText"],
                        play["homeScore"],
                        play["visitorScore"],
                        play["clock"],
                    )
                    if row == previous:
                        continue
                    previous = row
                    text = collapse_whitespace(play["playText"])
                    if play["homeScore"] is None or LINEUP_PATTERN.fullmatch(text):
                        continue
                    seoname = seonames[str(entry["teamId"])]
                    rallies.append((period["periodNumber"], seoname, text, play["homeScore"], play["visitorScore"]))

        # later rallies overwrite earlier ones, so this keeps each set's last
        final_scores = {set_number: (home, away) for set_number, _, _, home, away in rallies}
        swapped = scores_are_swapped(final_scores, match.set_scores)

        # second pass numbers the rallies and works out who served
        plays = []
        sequence = 0
        current_set = None
        last_winner = None
        for set_number, seoname, text, home, away in rallies:
            if set_number != current_set:
                current_set = set_number
                sequence = 0
                last_winner = None
            sequence += 1
            play_type, players = classify_play(text)
            plays.append(
                Play(
                    set_number=set_number,
                    sequence=sequence,
                    play_type=play_type,
                    scoring_seoname=seoname,
                    serving_seoname=last_winner,
                    play_text=text,
                    home_score=away if swapped else home,
                    away_score=home if swapped else away,
                    players=players,
                )
            )
            last_winner = seoname

        return tuple(plays)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ParseError(f"Can't parse play-by-play for game {match.source_game_id}: {e}") from e


if __name__ == "__main__":
    # debug helper until the W2-07 CLI: parses one cached game's details,
    # python -i -m sideout_analytics.db.parser 6625808 keeps match in a REPL
    raw_store = RawStore.from_settings()
    match = parse_game_details(raw_store.map_endpoint_cache_file(f"/game/{sys.argv[1]}").read_bytes())
    pprint.pprint(match.model_dump())
