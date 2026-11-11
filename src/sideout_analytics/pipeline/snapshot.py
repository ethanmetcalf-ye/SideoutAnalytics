"""
The weekly poll snapshot (W2-08): saves the current AVCA poll and loads every
saved week into team_rank. Runs as `pipeline snapshot` through
ingest.run_job, so it takes the same run lock and gets an ingest_run row like
the other jobs.

Decisions (ADR-005):
- The API serves only the current week, so every run refreshes the poll and
  saves a dated copy, rankings/volleyball-women/d1/avca-coaches/{poll_date}.json,
  the first time it sees that week. The undated file is the latest response,
  like any undated endpoint (ADR-003). The dated copies are the history.
- A poll whose date can't be read is saved as undated-{hash}.json before the
  run fails, so a format change doesn't lose the week. Once the parser
  handles it, the next run loads it.
- Every run loads each saved week that isn't in team_rank yet, so a week
  that couldn't load before loads on a later run, and team_rank can be
  rebuilt from the files. A week already in team_rank is skipped: a poll
  isn't revised after it's published.
- A week loads all or nothing. Schools are matched by team_alias first, then
  by exact team_name, then by team_name with a trailing "State" written
  "St.", the API's short form ("Penn State" -> "Penn St."). A school with no
  match, a name two teams share, or two schools on one team fails the run
  and loads none of the week. Add a team_alias row and the next run loads
  it.
- A week whose season has no season row yet (a preseason poll before any
  match is loaded) waits without failing the run. season rows come from the
  match loader.
- RECORD and PREVIOUS RANK aren't stored, they follow from the matches and
  the previous week (schema.md decision 2).
"""

import asyncio
import datetime as dt
import logging
import pathlib
import re
from dataclasses import dataclass

import psycopg

from . import parse
from .cache import RawStore, hash_content
from .client import Client
from .fetch import season_for
from .ingest import run_job

logger = logging.getLogger(__name__)

POLL = "avca-coaches"
POLL_ENDPOINT = f"/rankings/volleyball-women/d1/{POLL}"

TEAM_RANK_INSERT = """
INSERT INTO team_rank (team_id, poll, poll_date, season, ranking, total_points, first_place_votes)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class WeekResult:
    """
    what happened to one saved week that wasn't in team_rank yet. status is
    loaded, waiting (no season row yet) or unmatched (schools that need a
    team_alias row, nothing loaded).
    """

    poll_date: dt.date
    status: str
    unmatched: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotSummary:
    """
    one snapshot run, what the CLI reports and exits on.
    """

    ingest_run_id: int
    current_poll_date: dt.date
    new_week: bool
    weeks: tuple[WeekResult, ...]

    @property
    def counts(self) -> dict[str, int]:
        """
        the ingest_run count columns are per game, none apply here.
        """
        return {}

    @property
    def error(self) -> str | None:
        """
        the ingest_run.error text, None when the run succeeded. Only
        unmatched schools fail it, a waiting week loads by itself later.
        """
        problems = [
            f"poll {week.poll_date}: no team for {', '.join(week.unmatched)}"
            for week in self.weeks
            if week.status == "unmatched"
        ]
        return "; ".join(problems) or None


def dated_directory(raw_store: RawStore) -> pathlib.Path:
    """
    the folder of saved weeks, next to the undated poll file.
    """
    return raw_store.map_endpoint_cache_file(POLL_ENDPOINT).with_suffix("")


async def fetch_poll(client: Client, raw_store: RawStore) -> bytes:
    """
    the current poll, always refreshed since the API replaces it each week.
    Opens the client and closes it when done.
    """
    async with client:
        return (await raw_store.get(client, POLL_ENDPOINT, refresh=True)).content


def save_week(raw_store: RawStore, content: bytes) -> tuple[parse.Poll, bool]:
    """
    saves the poll as its week's dated copy unless that week is saved
    already, and returns it with whether it was new. Raises ParseError if
    the poll can't be read, after saving it as undated-{hash}.json.
    """
    try:
        poll = parse.parse_poll(content)
    except parse.ParseError:
        undated = dated_directory(raw_store) / f"undated-{hash_content(content)[:12]}.json"
        raw_store.write_cache_to_disc(content, undated)
        logger.warning("Could not read the poll, saved it as %s", undated)
        raise

    dated = raw_store.map_endpoint_cache_file(f"{POLL_ENDPOINT}/{poll.poll_date:%Y-%m-%d}")
    if dated.exists():
        return poll, False
    raw_store.write_cache_to_disc(content, dated)
    return poll, True


def saved_weeks(raw_store: RawStore) -> list[parse.Poll]:
    """
    every saved week, oldest first, one poll per date: an undated file can
    hold the same week as a dated one once the parser reads it. Raises
    ParseError on a file that can't be read, an undated one included.
    """
    # sorted so the dated copy (digits) wins over undated-... for a date
    polls: dict[dt.date, parse.Poll] = {}
    for path in sorted(dated_directory(raw_store).glob("*.json")):
        poll = parse.parse_poll(path.read_bytes())
        polls.setdefault(poll.poll_date, poll)
    return [polls[poll_date] for poll_date in sorted(polls)]


def short_form(school: str) -> str:
    """
    the API's short name for a poll school, "Penn State" -> "Penn St.".
    """
    return re.sub(r" State$", " St.", school)


class TeamMatcher:
    """
    matches poll schools to team_ids, see the module docstring for the
    order. Reads team_alias and team once per run.
    """

    def __init__(self, connection: psycopg.Connection) -> None:
        self.aliases: dict[str, int] = dict(connection.execute("SELECT alias, team_id FROM team_alias").fetchall())
        self.names: dict[str, list[int]] = {}
        for team_name, team_id in connection.execute("SELECT team_name, team_id FROM team").fetchall():
            self.names.setdefault(team_name, []).append(team_id)

    def team_id(self, school: str) -> int | None:
        """
        the school's team, None if no team or more than one has the name.
        """
        if school in self.aliases:
            return self.aliases[school]
        for name in dict.fromkeys([school, short_form(school)]):
            team_ids = self.names.get(name, [])
            if team_ids:
                return team_ids[0] if len(team_ids) == 1 else None
        return None


def load_week(connection: psycopg.Connection, poll: parse.Poll, matcher: TeamMatcher) -> WeekResult:
    """
    loads one week into team_rank in one transaction, or nothing if its
    season isn't loaded yet or a school has no team of its own.
    """
    season = season_for(poll.poll_date)
    if connection.execute("SELECT 1 FROM season WHERE year = %s", (season,)).fetchone() is None:
        return WeekResult(poll.poll_date, "waiting")

    team_ids = {entry.school: matcher.team_id(entry.school) for entry in poll.entries}
    seen: dict[int, str] = {}
    unmatched = []
    for school, team_id in team_ids.items():
        if team_id is None or team_id in seen:
            unmatched.append(school)
        else:
            seen[team_id] = school
    if unmatched:
        return WeekResult(poll.poll_date, "unmatched", tuple(unmatched))

    rows = [
        (
            team_ids[entry.school],
            POLL,
            poll.poll_date,
            season,
            entry.ranking,
            entry.total_points,
            entry.first_place_votes,
        )
        for entry in poll.entries
    ]
    with connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(TEAM_RANK_INSERT, rows)
    return WeekResult(poll.poll_date, "loaded")


def loaded_weeks(connection: psycopg.Connection) -> set[dt.date]:
    """
    the weeks of this poll already in team_rank.
    """
    rows = connection.execute("SELECT DISTINCT poll_date FROM team_rank WHERE poll = %s", (POLL,)).fetchall()
    return {row[0] for row in rows}


def run_snapshot(connection: psycopg.Connection, client: Client, raw_store: RawStore) -> SnapshotSummary:
    """
    one snapshot run under run_job: saves the current poll, then loads every
    saved week that isn't in team_rank. client isn't open yet, the run
    opens it.
    """

    def work(ingest_run_id: int) -> SnapshotSummary:
        current, new_week = save_week(raw_store, asyncio.run(fetch_poll(client, raw_store)))
        logger.info("poll through %s, %s", current.poll_date, "new week saved" if new_week else "week already saved")

        loaded = loaded_weeks(connection)
        matcher = TeamMatcher(connection)
        weeks = tuple(
            load_week(connection, poll, matcher) for poll in saved_weeks(raw_store) if poll.poll_date not in loaded
        )
        return SnapshotSummary(ingest_run_id, current.poll_date, new_week, weeks)

    return run_job(connection, "snapshot", work)
