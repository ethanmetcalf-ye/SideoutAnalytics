"""
Loads one game's parsed records into Postgres, so loading the same game
twice leaves the same rows (W2-04).

Decisions:
- One transaction per game. A game that fails rolls back completely and
  the games loaded before it stay loaded.
- Rows the game shares with other games (season, conference, team,
  team_season, player, roster, match) are upserted with ON CONFLICT on their
  natural keys (docs/schema.md decision 6).
- Rows the game owns (match_team, box_score_line, team_set_stats, play,
  play_player) are deleted and inserted again. A rally or player dropped
  from a corrected feed can't linger, and plays have no source key to
  upsert on. Plays and their players go in with COPY.
- Players are matched by team and name_key in the same, previous or next
  season, and are new players otherwise (decision 7), so the result doesn't
  depend on the order seasons are loaded in. If the previous and next
  seasons have the name under two players, they're the same player split by
  load order and are merged.
- A box score line with no name is matched by jersey number to the team's
  roster for the season. It's skipped when no roster player, or more than
  one, has the number. That depends on the named matches being loaded
  first, so a game with skipped_lines should be loaded again at the end of
  the run.
- Names in the play text are matched to the box score lines of this match,
  on the team the role puts them on. A name with no line is skipped and
  counted, the play itself is kept.
- A kill's "(from B)" names the scoring team's setter, except about twice a
  game B is on the other team: the last touch before the kill, usually an
  overpass. Box score assists count the same-team ones and not these
  (issue #75), so B is linked with role last_touch instead of setter.
- match.pbp_reconciled and the play-by-play columns of team_set_stats are
  left for reconciliation, the loader writes NULL.
- Conferences are the scoreboard's seo names (big-ten), the scoreboard's
  conferenceName is blank.
- Games that share a team must not load at the same time: both could create
  the same new player, and one would then fail on roster's (team_id, season,
  name_key) key. Load serially.
- Column lists in the SQL are written by hand rather than read from
  tables.py, since COPY needs them spelled out. The tests build their schema
  from tables.py, so a column that drifts fails there.
"""

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass

import psycopg
from sqlalchemy.engine import make_url

from ..pipeline import parse
from .tables import BOX_SCORE_COUNTS, MatchState, PlayRole, PlayType


class LoadError(Exception):
    """
    a game whose records can't be loaded. The transaction is rolled back, a
    database error is chained as __cause__.
    """


@dataclass(frozen=True)
class ParsedGame:
    """
    everything parsed for one game. box_score is None and plays is empty
    when the API doesn't have them yet (pre and some live games) or at all.
    """

    match: parse.Match
    scoreboard: parse.ScoreboardGame
    box_score: parse.BoxScore | None = None
    plays: tuple[parse.Play, ...] = ()


@dataclass(frozen=True)
class LoadResult:
    """
    what load_game did with one game, for the run log. skipped_lines are
    box score lines with no name and no jersey number match, worth a reload
    once more of the season is in. unmatched_names are names in the play
    text with no box score line.
    """

    match_id: int
    skipped_lines: int
    unmatched_names: int


def connect(database_url: str) -> psycopg.Connection:
    """
    a psycopg connection for DATABASE_URL. The URL is written for SQLAlchemy
    (postgresql+psycopg://) and psycopg wants plain postgresql://.
    Autocommit, so each load_game transaction commits on its own.
    """
    url = make_url(database_url).set(drivername="postgresql")
    return psycopg.connect(url.render_as_string(hide_password=False), autocommit=True)


def name_key(first_name: str | None, last_name: str | None) -> str:
    """
    the normalized name players are matched on (schema.md decision 7):
    lowercase, accents and punctuation dropped, whitespace collapsed. First
    and last are joined before normalizing since some box scores put the
    whole name in lastName, and play text only has "First Last".
    """
    decomposed = unicodedata.normalize("NFKD", f"{first_name or ''} {last_name or ''}")
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w\s]", "", without_accents.casefold()).split())


def on_scoring_team(play_type: PlayType, role: PlayRole) -> bool:
    """
    whether a player named in a play is on the team that won the rally.
    Kill attackers and setters and every blocker are, the attacker who made
    an error, the setter of a bad set and an aced receiver aren't.
    """
    return role == PlayRole.BLOCKER or play_type == PlayType.KILL


def load_game(connection: psycopg.Connection, game: ParsedGame, ingest_run_id: int) -> LoadResult:
    """
    loads one game in one transaction. Raises LoadError when the records
    don't fit the schema: a box score or play for a team that isn't in the
    details, a team missing from the scoreboard, a box score that doesn't
    cover the linescores, two teammates with the same name, or a row the
    database rejects. Not safe to run for two games with a team in common at
    the same time, see the module docstring.
    """
    match = game.match
    try:
        check_teams(game)
        with connection.transaction(), connection.cursor() as cursor:
            upsert_season(cursor, match)
            team_ids = {team.seoname: upsert_team(cursor, team, match.season, game.scoreboard) for team in match.teams}
            match_id = upsert_match(cursor, match, game.scoreboard, ingest_run_id)

            # play_player cascades from play, box_score_line and
            # team_set_stats cascade from match_team
            cursor.execute("DELETE FROM play WHERE match_id = %s", (match_id,))
            cursor.execute("DELETE FROM match_team WHERE match_id = %s", (match_id,))
            insert_match_teams(cursor, match_id, match, team_ids)

            players = {}
            skipped_lines = 0
            if game.box_score is not None:
                players, skipped_lines = insert_box_score(cursor, match_id, match.season, game.box_score, team_ids)
                insert_team_set_stats(cursor, match_id, match, game.box_score, team_ids)
            unmatched_names = insert_plays(cursor, match_id, game.plays, team_ids, players)

            return LoadResult(match_id=match_id, skipped_lines=skipped_lines, unmatched_names=unmatched_names)
    except (psycopg.IntegrityError, psycopg.DataError) as e:
        raise LoadError(f"Can't load game {match.source_game_id}: {e}") from e
    except LoadError as e:
        raise LoadError(f"Can't load game {match.source_game_id}: {e}") from e


def check_teams(game: ParsedGame) -> None:
    """
    raises LoadError if the box score or play-by-play names a team that
    isn't one of the match's two, so bad data fails the game instead of
    escaping as a KeyError.
    """
    seonames = {team.seoname for team in game.match.teams}
    named = set()
    if game.box_score is not None:
        named |= {line.seoname for line in game.box_score.lines}
        named |= {attack.seoname for attack in game.box_score.set_attacks}
    for play in game.plays:
        named |= {play.scoring_seoname, play.serving_seoname} - {None}
    unknown = named - seonames
    if unknown:
        raise LoadError(f"teams {sorted(unknown)} aren't in the details")


def upsert_season(cursor: psycopg.Cursor, match: parse.Match) -> None:
    """
    makes sure the match's season exists and its dates cover the match. The
    end date is the day after the latest start, for matches that run past
    midnight.
    """
    day = match.start_time.date()
    cursor.execute(
        """
        INSERT INTO season (year, start_date, end_date)
        VALUES (%(year)s, %(start_date)s, %(end_date)s)
        ON CONFLICT (year) DO UPDATE SET
            start_date = LEAST(season.start_date, EXCLUDED.start_date),
            end_date = GREATEST(season.end_date, EXCLUDED.end_date)
        """,
        {"year": match.season, "start_date": day, "end_date": day + dt.timedelta(days=1)},
    )


def upsert_team(cursor: psycopg.Cursor, team: parse.MatchTeam, season: int, scoreboard: parse.ScoreboardGame) -> int:
    """
    upserts a team, its conference and its team_season row, returns the
    team_id. Raises LoadError if the scoreboard doesn't list the team.
    """
    if team.seoname not in scoreboard.conferences:
        raise LoadError(f"team {team.seoname} isn't on the scoreboard")
    conference = scoreboard.conferences[team.seoname]

    conference_id = None
    if conference is not None:
        # DO UPDATE with no real change, so RETURNING also gives the id of a
        # conference that's already there
        cursor.execute(
            """
            INSERT INTO conference (name) VALUES (%s)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING conference_id
            """,
            (conference,),
        )
        conference_id = cursor.fetchone()[0]

    cursor.execute(
        """
        INSERT INTO team (seoname, team_name) VALUES (%s, %s)
        ON CONFLICT (seoname) DO UPDATE SET team_name = EXCLUDED.team_name
        RETURNING team_id
        """,
        (team.seoname, team.team_name),
    )
    team_id = cursor.fetchone()[0]

    # head_coach and assistant_coaches are entered by hand, so they're left alone
    cursor.execute(
        """
        INSERT INTO team_season (team_id, season, source_team_id, division, conference_id)
        VALUES (%(team_id)s, %(season)s, %(source_team_id)s, %(division)s, %(conference_id)s)
        ON CONFLICT (team_id, season) DO UPDATE SET
            source_team_id = EXCLUDED.source_team_id,
            division = EXCLUDED.division,
            conference_id = EXCLUDED.conference_id
        """,
        {
            "team_id": team_id,
            "season": season,
            "source_team_id": team.source_team_id,
            "division": team.division.value,
            "conference_id": conference_id,
        },
    )
    return team_id


def upsert_match(
    cursor: psycopg.Cursor, match: parse.Match, scoreboard: parse.ScoreboardGame, ingest_run_id: int
) -> int:
    """
    upserts the match row and returns its match_id. result_first_seen_at is
    set the first time the match is loaded as final and kept after that.
    is_neutral_site and the reconciliation columns aren't the loader's, so
    they're left alone.
    """
    cursor.execute(
        """
        INSERT INTO match (
            source_game_id, season, start_time, state, bracket_round, venue, venue_city, venue_state,
            playbyplay_available, result_first_seen_at, ingest_run_id
        )
        VALUES (
            %(source_game_id)s, %(season)s, %(start_time)s, %(state)s, %(bracket_round)s, %(venue)s,
            %(venue_city)s, %(venue_state)s, %(playbyplay_available)s,
            CASE WHEN %(is_final)s THEN now() END, %(ingest_run_id)s
        )
        ON CONFLICT (source_game_id) DO UPDATE SET
            season = EXCLUDED.season,
            start_time = EXCLUDED.start_time,
            state = EXCLUDED.state,
            bracket_round = EXCLUDED.bracket_round,
            venue = EXCLUDED.venue,
            venue_city = EXCLUDED.venue_city,
            venue_state = EXCLUDED.venue_state,
            playbyplay_available = EXCLUDED.playbyplay_available,
            result_first_seen_at = COALESCE(match.result_first_seen_at, EXCLUDED.result_first_seen_at),
            updated_at = now(),
            ingest_run_id = EXCLUDED.ingest_run_id
        RETURNING match_id
        """,
        {
            "source_game_id": match.source_game_id,
            "season": match.season,
            "start_time": match.start_time,
            "state": match.state.value,
            "is_final": match.state == MatchState.FINAL,
            "bracket_round": scoreboard.bracket_round,
            "venue": match.venue,
            "venue_city": match.venue_city,
            "venue_state": match.venue_state,
            "playbyplay_available": match.playbyplay_available,
            "ingest_run_id": ingest_run_id,
        },
    )
    return cursor.fetchone()[0]


def insert_match_teams(cursor: psycopg.Cursor, match_id: int, match: parse.Match, team_ids: dict[str, int]) -> None:
    """
    inserts both match_team rows, after the old ones were deleted.
    """
    cursor.executemany(
        """
        INSERT INTO match_team (match_id, team_id, is_home, is_winner, sets_won, seed, rank_at_match)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                match_id,
                team_ids[team.seoname],
                team.is_home,
                team.is_winner,
                team.sets_won,
                team.seed,
                team.rank_at_match,
            )
            for team in match.teams
        ],
    )


def merge_player(cursor: psycopg.Cursor, keep: int, drop: int) -> None:
    """
    moves every row of player drop onto player keep and deletes drop. Used
    when two seasons of one player were loaded before the season between
    them, their seasons don't overlap so no key collides.
    """
    for table in ("roster", "box_score_line", "play_player"):
        cursor.execute(f"UPDATE {table} SET player_id = %s WHERE player_id = %s", (keep, drop))  # noqa: S608
    cursor.execute("DELETE FROM player WHERE player_id = %s", (drop,))


def resolve_players(
    cursor: psycopg.Cursor, season: int, lines: list[tuple[int, str, parse.BoxScoreLine]]
) -> dict[tuple[int, str], int]:
    """
    the player_id for each (team_id, name_key) in a box score, from the
    team's roster in the previous, same or next season, else a new player.
    Upserts each player's roster row with the latest number and position.
    """
    team_ids = list({team_id for team_id, _, _ in lines})
    cursor.execute(
        """
        SELECT team_id, season, name_key, player_id FROM roster
        WHERE team_id = ANY(%s) AND season BETWEEN %s AND %s
        """,
        (team_ids, season - 1, season + 1),
    )
    known = {(team_id, row_season, key): player_id for team_id, row_season, key, player_id in cursor.fetchall()}

    players = {}
    for team_id, key, line in lines:
        # previous season first, so a merge keeps the oldest player
        found = [known.get((team_id, nearby, key)) for nearby in (season - 1, season, season + 1)]
        candidates = list(dict.fromkeys(player_id for player_id in found if player_id is not None))
        if candidates:
            player_id = candidates[0]
            for other in candidates[1:]:
                merge_player(cursor, player_id, other)
        else:
            cursor.execute(
                "INSERT INTO player (first_name, last_name, name_key) VALUES (%s, %s, %s) RETURNING player_id",
                (line.first_name or "", line.last_name or "", key),
            )
            player_id = cursor.fetchone()[0]
        players[(team_id, key)] = player_id

    # a match without a number or position keeps the one already listed
    cursor.executemany(
        """
        INSERT INTO roster (player_id, team_id, season, name_key, number, position)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (player_id, team_id, season) DO UPDATE SET
            number = COALESCE(EXCLUDED.number, roster.number),
            position = COALESCE(EXCLUDED.position, roster.position)
        """,
        [(players[(team_id, key)], team_id, season, key, line.number, line.position) for team_id, key, line in lines],
    )
    return players


def match_by_number(
    cursor: psycopg.Cursor, season: int, lines: list[tuple[int, parse.BoxScoreLine]]
) -> dict[tuple[int, str], tuple[int, str]]:
    """
    the (player_id, name_key) for each nameless line's (team_id, number),
    from the team's roster this season. Numbers that no roster player or
    more than one has are left out.
    """
    cursor.execute(
        """
        SELECT team_id, number, min(player_id), min(name_key) FROM roster
        WHERE team_id = ANY(%s) AND season = %s AND number IS NOT NULL
        GROUP BY team_id, number
        HAVING count(*) = 1
        """,
        (list({team_id for team_id, _ in lines}), season),
    )
    return {(team_id, number): (player_id, key) for team_id, number, player_id, key in cursor.fetchall()}


def insert_box_score(
    cursor: psycopg.Cursor, match_id: int, season: int, box_score: parse.BoxScore, team_ids: dict[str, int]
) -> tuple[dict[tuple[int, str], int], int]:
    """
    inserts box_score_line rows and returns the player_ids by (team_id,
    name_key) for the plays, and the number of nameless lines skipped
    because their number didn't match one roster player. Raises LoadError
    when two teammates have the same name_key, since they'd be one player.
    """
    named = []
    nameless = []
    for line in box_score.lines:
        team_id = team_ids[line.seoname]
        if line.first_name is None and line.last_name is None:
            nameless.append((team_id, line))
            continue
        key = name_key(line.first_name, line.last_name)
        if any(seen_team == team_id and seen_key == key for seen_team, seen_key, _ in named):
            raise LoadError(f"two players on {line.seoname} are both {key!r}")
        named.append((team_id, key, line))

    players = resolve_players(cursor, season, named) if named else {}
    rows = [(players[(team_id, key)], team_id, line) for team_id, key, line in named]

    skipped = 0
    by_number = match_by_number(cursor, season, nameless) if nameless else {}
    for team_id, line in nameless:
        matched = by_number.get((team_id, line.number))
        # a number already on a named line in this match would be the same
        # player twice
        if matched is None or matched[0] in players.values():
            skipped += 1
            continue
        player_id, key = matched
        players[(team_id, key)] = player_id
        rows.append((player_id, team_id, line))

    columns = ["match_id", "player_id", "team_id", "position", "started", "sets_played", *BOX_SCORE_COUNTS]
    cursor.executemany(
        f"INSERT INTO box_score_line ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",  # noqa: S608
        [
            (
                match_id,
                player_id,
                team_id,
                line.position,
                line.started,
                line.sets_played,
                *(getattr(line, count) for count in BOX_SCORE_COUNTS),
            )
            for player_id, team_id, line in rows
        ],
    )
    return players, skipped


def insert_team_set_stats(
    cursor: psycopg.Cursor, match_id: int, match: parse.Match, box_score: parse.BoxScore, team_ids: dict[str, int]
) -> None:
    """
    inserts one team_set_stats row per team per set: points from the
    linescores, attacking from the box score (schema.md decision 3). Raises
    LoadError when the box score is missing a set the linescores have.
    """
    attacks = {(attack.seoname, attack.set_number): attack for attack in box_score.set_attacks}
    rows = []
    for score in match.set_scores:
        for team in match.teams:
            attack = attacks.get((team.seoname, score.set_number))
            if attack is None:
                raise LoadError(f"box score has no set {score.set_number} for {team.seoname}")
            points = score.home_points if team.is_home else score.away_points
            rows.append(
                (
                    match_id,
                    score.set_number,
                    team_ids[team.seoname],
                    points,
                    attack.kills,
                    attack.attack_errors,
                    attack.attack_attempts,
                )
            )
    cursor.executemany(
        """
        INSERT INTO team_set_stats (match_id, set_number, team_id, points, kills, attack_errors, attack_attempts)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )


def insert_plays(
    cursor: psycopg.Cursor,
    match_id: int,
    plays: tuple[parse.Play, ...],
    team_ids: dict[str, int],
    players: dict[tuple[int, str], int],
) -> int:
    """
    copies the plays and the players named in them, returns how many names
    had no box score line. Plays have no source key, so their play_ids are
    read back by (set_number, sequence) for play_player.
    """
    if not plays:
        return 0
    with cursor.copy(
        """
        COPY play (match_id, set_number, sequence, play_type, scoring_team_id, serving_team_id, play_text,
                   home_score, away_score)
        FROM STDIN
        """
    ) as copy:
        for play in plays:
            copy.write_row(
                (
                    match_id,
                    play.set_number,
                    play.sequence,
                    play.play_type.value,
                    team_ids[play.scoring_seoname],
                    None if play.serving_seoname is None else team_ids[play.serving_seoname],
                    play.play_text,
                    play.home_score,
                    play.away_score,
                )
            )

    cursor.execute("SELECT set_number, sequence, play_id FROM play WHERE match_id = %s", (match_id,))
    play_ids = {(set_number, sequence): play_id for set_number, sequence, play_id in cursor.fetchall()}

    # the other team is whichever one didn't score
    other_team = {seoname: next(t for s, t in team_ids.items() if s != seoname) for seoname in team_ids}
    unmatched = 0
    # a set, since "block by A, A" would be the same row twice
    rows = set()
    for play in plays:
        scoring_team_id = team_ids[play.scoring_seoname]
        for named in play.players:
            on_scoring = on_scoring_team(play.play_type, named.role)
            team_id = scoring_team_id if on_scoring else other_team[play.scoring_seoname]
            key = name_key(None, named.name)
            player_id = players.get((team_id, key))
            role = named.role
            if player_id is None and play.play_type == PlayType.KILL and role == PlayRole.SETTER:
                player_id = players.get((other_team[play.scoring_seoname], key))
                role = PlayRole.LAST_TOUCH
            if player_id is None:
                unmatched += 1
                continue
            rows.add((play_ids[(play.set_number, play.sequence)], player_id, role.value))

    if rows:
        with cursor.copy("COPY play_player (play_id, player_id, role) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
    return unmatched
