"""
Checks the loaded season against the raw API files and the NCAA's published
stats (W2-06), as `pipeline validate`. Each check returns the rows that break
its rule, and zero rows passes. The results, and the reason for every
failure, are in docs/validation.md.

Decisions:
- The checks are against the source, not the prototype database: the
  prototype was never verified, so matching it proves little.
- Every check but one is read-only. The top-kills check refreshes
  /stats/.../current/individual/879 into the raw cache, since the
  leaderboard changes daily and the comparison has to use the copy it ran
  against.
- The leaderboard counts games through the date in its `updated` field, so
  the database's side of that check stops at the same date, in US Eastern.
  It ranks Division I players only, so the database's top 5 is limited to
  Division I teams.
- The rules on game data cover final matches. A set or a match the API
  hasn't finished can't break a rule about results.
- A set with no plays isn't compared with its last play, so a match without
  play-by-play can't fail that check.
- Rows explained in docs/validation.md are listed in
  validation_exceptions.toml, next to this module, by check id and row key.
  validate reports them as known and fails only on new rows, so its exit
  code changes when something new breaks, not on every run. The list is a
  checked-in file rather than a table, so adding an exception is a reviewed
  change with its reason. A listed row that no longer fails is reported as
  resolved, to be removed. An exception is matched on its key alone, so a
  known row whose values change stays known.
"""

import asyncio
import datetime as dt
import json
import pathlib
import tomllib
from dataclasses import dataclass, field, replace

import psycopg

from ..pipeline import parse
from ..pipeline.cache import RawStore
from ..pipeline.client import Client
from ..pipeline.fetch import Game, games_from_scoreboard, season_for
from .loader import name_key
from .tables import MatchState

KILLS_LEADERS_ENDPOINT = "/stats/volleyball-women/d1/current/individual/879"
TOP_KILLS = 5
EXCEPTIONS_FILE = pathlib.Path(__file__).with_name("validation_exceptions.toml")

RowKey = tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    """
    one rule and the rows that break it, with their column names.
    check_id names the check in validation_exceptions.toml, and key_columns
    are the positions of the columns that identify a row there. known are
    the keys of rows listed as exceptions, resolved the listed keys whose
    rows no longer fail.
    """

    check_id: str
    name: str
    columns: tuple[str, ...]
    key_columns: tuple[int, ...]
    rows: tuple[tuple, ...]
    known: frozenset[RowKey] = field(default=frozenset())
    resolved: tuple[RowKey, ...] = ()

    def row_key(self, row: tuple) -> RowKey:
        """
        the row's key as text, the way the exceptions file writes it.
        """
        return tuple(str(row[index]) for index in self.key_columns)

    @property
    def new_rows(self) -> tuple[tuple, ...]:
        return tuple(row for row in self.rows if self.row_key(row) not in self.known)

    @property
    def known_rows(self) -> tuple[tuple, ...]:
        return tuple(row for row in self.rows if self.row_key(row) in self.known)

    @property
    def passed(self) -> bool:
        """
        no rows break the rule except known ones.
        """
        return not self.new_rows


# (check id, name, columns, key columns, query). Each takes %(season)s and
# returns the rows that break the rule
SQL_CHECKS = [
    (
        "set_points_last_play",
        "each set's points equal its last play's score",
        ("game", "set", "home points", "away points", "last play home", "last play away"),
        (0, 1),
        """
        WITH last_play AS (
            SELECT DISTINCT ON (p.match_id, p.set_number) p.match_id, p.set_number, p.home_score, p.away_score
            FROM play p JOIN match m USING (match_id)
            WHERE m.season = %(season)s AND m.state = 'final'
            ORDER BY p.match_id, p.set_number, p.sequence DESC
        ),
        set_points AS (
            SELECT s.match_id, s.set_number,
                   max(s.points) FILTER (WHERE mt.is_home) AS home_points,
                   max(s.points) FILTER (WHERE NOT mt.is_home) AS away_points
            FROM team_set_stats s JOIN match_team mt USING (match_id, team_id)
            GROUP BY s.match_id, s.set_number
        )
        SELECT m.source_game_id, sp.set_number, sp.home_points, sp.away_points, lp.home_score, lp.away_score
        FROM set_points sp
        JOIN last_play lp USING (match_id, set_number)
        JOIN match m USING (match_id)
        WHERE (sp.home_points, sp.away_points) IS DISTINCT FROM (lp.home_score, lp.away_score)
        ORDER BY 1, 2
        """,
    ),
    (
        "set_attacks_box_score",
        "team kills, attack errors and attempts over sets equal the box score",
        ("game", "team", "set kills", "box kills", "set errors", "box errors", "set attempts", "box attempts"),
        (0, 1),
        """
        WITH by_set AS (
            SELECT match_id, team_id,
                   sum(kills) AS kills, sum(attack_errors) AS errors, sum(attack_attempts) AS attempts
            FROM team_set_stats GROUP BY match_id, team_id
        ),
        box AS (
            SELECT match_id, team_id,
                   sum(kills) AS kills, sum(attack_errors) AS errors, sum(attack_attempts) AS attempts
            FROM box_score_line GROUP BY match_id, team_id
        )
        SELECT m.source_game_id, t.seoname, s.kills, b.kills, s.errors, b.errors, s.attempts, b.attempts
        FROM by_set s
        JOIN match m USING (match_id)
        JOIN team t USING (team_id)
        LEFT JOIN box b USING (match_id, team_id)
        WHERE m.season = %(season)s AND m.state = 'final'
          AND (s.kills, s.errors, s.attempts) IS DISTINCT FROM (b.kills, b.errors, b.attempts)
        ORDER BY 1, 2
        """,
    ),
    (
        "sets_won_winner",
        "sets_won and is_winner agree with the set points",
        ("game", "team", "sets_won", "sets won on points", "is_winner", "winner on points"),
        (0, 1),
        """
        WITH won AS (
            SELECT s.match_id, s.team_id, count(*) FILTER (WHERE s.points > o.points) AS sets_won
            FROM team_set_stats s
            JOIN team_set_stats o
              ON o.match_id = s.match_id AND o.set_number = s.set_number AND o.team_id <> s.team_id
            GROUP BY s.match_id, s.team_id
        )
        SELECT m.source_game_id, t.seoname, mt.sets_won, w.sets_won, mt.is_winner, w.sets_won > opponent.sets_won
        FROM match_team mt
        JOIN match m USING (match_id)
        JOIN team t USING (team_id)
        LEFT JOIN won w ON w.match_id = mt.match_id AND w.team_id = mt.team_id
        LEFT JOIN won opponent ON opponent.match_id = mt.match_id AND opponent.team_id <> mt.team_id
        WHERE m.season = %(season)s AND m.state = 'final'
          AND (mt.sets_won IS DISTINCT FROM w.sets_won
               OR mt.is_winner IS DISTINCT FROM (w.sets_won > opponent.sets_won))
        ORDER BY 1, 2
        """,
    ),
]


def season_dates(raw_store: RawStore, season: int) -> list[tuple[dt.date, int]]:
    """
    each date in the season's cached /schedule-alt with its game count, the
    dates the season index is built from. Raises FileNotFoundError if the
    schedule isn't cached.
    """
    endpoint = f"/schedule-alt/volleyball-women/d1/{season}"
    content = raw_store.read_cached_file(raw_store.map_endpoint_cache_file(endpoint))
    if content is None:
        raise FileNotFoundError(f"{endpoint} isn't cached, run the pipeline for {season} first")
    dates = []
    for entry in json.loads(content)["data"]["schedules"]["games"]:
        month, day, year = map(int, entry["contestDate"].split("/"))
        dates.append((dt.date(year, month, day), int(entry["count"])))
    return dates


def scoreboard_games(raw_store: RawStore, date: dt.date) -> list[Game] | None:
    """
    the games on a date's cached scoreboard, None if it isn't cached.
    """
    content = raw_store.read_cached_file(
        raw_store.map_endpoint_cache_file(f"/scoreboard/volleyball-women/d1/{date:%Y/%m/%d}")
    )
    return None if content is None else games_from_scoreboard(content)


def check_schedule_counts(raw_store: RawStore, season: int) -> CheckResult:
    """
    dates where /schedule-alt's game count isn't the number of games on that
    date's cached scoreboard.
    """
    rows = []
    for date, count in season_dates(raw_store, season):
        games = scoreboard_games(raw_store, date)
        on_scoreboard = None if games is None else len({game.game_id for game in games})
        if on_scoreboard != count:
            rows.append((date.isoformat(), count, on_scoreboard))
    return CheckResult(
        "schedule_counts",
        "season index game counts per date match /schedule-alt",
        ("date", "schedule", "scoreboard"),
        (0,),
        tuple(rows),
    )


def check_final_games_loaded(connection: psycopg.Connection, raw_store: RawStore, season: int) -> CheckResult:
    """
    final games on the cached scoreboards with no match row, or a match row
    that isn't final.
    """
    final_games = {}
    for date, _count in season_dates(raw_store, season):
        for game in scoreboard_games(raw_store, date) or []:
            if game.state == MatchState.FINAL:
                final_games[game.game_id] = date
    states = dict(
        connection.execute(
            "SELECT source_game_id, state FROM match WHERE source_game_id = ANY(%s)", (sorted(final_games),)
        ).fetchall()
    )
    rows = tuple(
        (game_id, date.isoformat(), states.get(game_id, "no match row"))
        for game_id, date in sorted(final_games.items(), key=lambda item: (item[1], item[0]))
        if states.get(game_id) != MatchState.FINAL
    )
    return CheckResult(
        "final_games_loaded",
        "every final game on the scoreboards has a final match row",
        ("game", "date", "match state"),
        (0,),
        rows,
    )


def check_sql(connection: psycopg.Connection, season: int) -> list[CheckResult]:
    """
    the rules written as queries over the loaded tables.
    """
    return [
        CheckResult(
            check_id,
            name,
            columns,
            key_columns,
            tuple(tuple(row) for row in connection.execute(query, {"season": season}).fetchall()),
        )
        for check_id, name, columns, key_columns, query in SQL_CHECKS
    ]


def database_kills_leaders(
    connection: psycopg.Connection, season: int, through: dt.date, top: int = TOP_KILLS
) -> list[tuple[str, str, int]]:
    """
    the top Division I players by kills in final matches through a date, as
    (name, team_name, kills).
    """
    rows = connection.execute(
        """
        SELECT p.first_name, p.last_name, t.team_name, sum(b.kills) AS kills
        FROM box_score_line b
        JOIN match m USING (match_id)
        JOIN player p USING (player_id)
        JOIN team t ON t.team_id = b.team_id
        JOIN team_season ts ON ts.team_id = b.team_id AND ts.season = m.season
        WHERE m.season = %s AND m.state = 'final' AND ts.division = 'd1'
          AND (m.start_time AT TIME ZONE 'America/New_York')::date <= %s
        GROUP BY p.player_id, p.first_name, p.last_name, t.team_name
        ORDER BY kills DESC, p.last_name, p.first_name
        LIMIT %s
        """,
        (season, through, top),
    ).fetchall()
    return [(" ".join(part for part in (first, last) if part), team, int(kills)) for first, last, team, kills in rows]


def compare_leaders(
    published: list[parse.StatLeader], database: list[tuple[str, str, int]], top: int = TOP_KILLS
) -> tuple[tuple, ...]:
    """
    the players where the published top and the database's disagree. Every
    published player ranked in the top (ties included) needs the same kills
    in the database, and every player in the database's top needs to be on
    the published list. Players are compared, not places, since the API
    orders tied players its own way. Names match on name_key, so spacing
    and accents don't count.
    """
    database_kills = {(name_key(None, name), team): kills for name, team, kills in database}
    published_top = [leader for leader in published if leader.rank <= top]
    rows = []
    for leader in published_top:
        kills = database_kills.get((name_key(None, leader.name), leader.team))
        if kills != leader.value:
            rows.append((leader.rank, leader.name, leader.team, leader.value, "not found" if kills is None else kills))
    published_players = {(name_key(None, leader.name), leader.team) for leader in published_top}
    for place, (name, team, kills) in enumerate(database[:top], start=1):
        if (name_key(None, name), team) not in published_players:
            rows.append((f"not published, database #{place}", name, team, "-", kills))
    return tuple(rows)


async def fetch_kills_leaders(client: Client, raw_store: RawStore) -> bytes:
    """
    today's Total Kills leaderboard, refreshed into the raw cache.
    """
    async with client:
        return (await raw_store.get(client, KILLS_LEADERS_ENDPOINT, refresh=True)).content


def check_top_kills(connection: psycopg.Connection, season: int, content: bytes) -> CheckResult:
    """
    the published top 5 in total kills against the database's, through the
    leaderboard's date.
    """
    leaders = parse.parse_stat_leaders(content, "Kills")
    # enough of the database's leaders to find every published top player
    database = database_kills_leaders(connection, season, leaders.through, top=50)
    return CheckResult(
        "top_kills",
        f"top {TOP_KILLS} total kills match {KILLS_LEADERS_ENDPOINT} (through {leaders.through})",
        ("published rank", "name", "team", "published kills", "database kills"),
        (1, 2),
        compare_leaders(list(leaders.leaders), database),
    )


# how many key columns each check's rows have, for checking the exceptions
# file. Kept next to the checks' own key_columns, a test holds them equal
CHECK_KEY_LENGTHS = {
    "final_games_loaded": 1,
    "schedule_counts": 1,
    **{check[0]: len(check[3]) for check in SQL_CHECKS},
    "top_kills": 2,
}
CHECK_IDS = set(CHECK_KEY_LENGTHS)


def load_exceptions(path: pathlib.Path = EXCEPTIONS_FILE) -> dict[str, dict[RowKey, str]]:
    """
    the known exceptions by check id, each row key with its reason. Raises
    ValueError on an unknown check id, a key with the wrong number of parts,
    an entry without a reason, or a key listed twice, so a typo can't
    quietly hide a row or quietly fail to.
    """
    exceptions: dict[str, dict[RowKey, str]] = {}
    for entry in tomllib.loads(path.read_text())["exception"]:
        check_id, key, reason = entry["check"], tuple(str(part) for part in entry["key"]), entry.get("reason", "")
        if check_id not in CHECK_IDS:
            raise ValueError(f"{path.name}: unknown check {check_id!r}")
        if len(key) != CHECK_KEY_LENGTHS[check_id]:
            raise ValueError(f"{path.name}: {check_id} {list(key)} needs {CHECK_KEY_LENGTHS[check_id]} key parts")
        if not reason.strip():
            raise ValueError(f"{path.name}: {check_id} {list(key)} has no reason")
        if key in exceptions.setdefault(check_id, {}):
            raise ValueError(f"{path.name}: {check_id} {list(key)} is listed twice")
        exceptions[check_id][key] = reason
    return exceptions


def apply_exceptions(result: CheckResult, exceptions: dict[str, dict[RowKey, str]]) -> CheckResult:
    """
    the result with its listed rows marked known, and the listed keys that
    no longer fail as resolved.
    """
    listed = exceptions.get(result.check_id, {})
    failing = {result.row_key(row) for row in result.rows}
    return replace(
        result,
        known=frozenset(key for key in listed if key in failing),
        resolved=tuple(sorted(key for key in listed if key not in failing)),
    )


def validate(
    connection: psycopg.Connection,
    client: Client,
    raw_store: RawStore,
    season: int,
    today: dt.date,
    exceptions: dict[str, dict[RowKey, str]] | None = None,
) -> list[CheckResult]:
    """
    every check for the season, in the order docs/validation.md reports
    them, with the known exceptions applied (the checked-in file unless
    exceptions is given). The top-kills check only runs for the season
    today is in, the leaderboard serves no other. client isn't open yet,
    validate opens it.
    """
    exceptions = load_exceptions() if exceptions is None else exceptions
    results = [
        check_final_games_loaded(connection, raw_store, season),
        check_schedule_counts(raw_store, season),
        *check_sql(connection, season),
    ]
    if season == season_for(today):
        results.append(check_top_kills(connection, season, asyncio.run(fetch_kills_leaders(client, raw_store))))
    return [apply_exceptions(result, exceptions) for result in results]
