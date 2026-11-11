import datetime as dt
import json
import pathlib

import httpx
import pytest

from sideout_analytics.db.loader import connect
from sideout_analytics.pipeline import ingest, snapshot
from sideout_analytics.pipeline.cache import RawStore
from sideout_analytics.pipeline.client import Client
from sideout_analytics.pipeline.parse import parse_poll

RANKINGS_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "raw" / "rankings" / "volleyball-women" / "d1"
POLL_FIXTURE = RANKINGS_FIXTURES / "avca-coaches.json"
# a week the prototype saved, ending with a row of blanks
OCT_4_FIXTURE = RANKINGS_FIXTURES / "avca-coaches" / "2026-10-04.json"
POLL_SCHOOLS = [entry.school for entry in parse_poll(POLL_FIXTURE.read_bytes()).entries]
OCT_4_SCHOOLS = [entry.school for entry in parse_poll(OCT_4_FIXTURE.read_bytes()).entries]


class PollApi:
    """
    a mock API that answers the poll endpoint with body and records every
    request.
    """

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requested_paths = []

    def client(self) -> Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requested_paths.append(request.url.path)
            if request.url.path != snapshot.POLL_ENDPOINT:
                return httpx.Response(404)
            return httpx.Response(200, content=self.body)

        return Client(
            base_url="https://api.test",
            user_agent="sideout-analytics-tests/0.1",
            transport=httpx.MockTransport(handle),
            rate_limit_per_sec=1000,
        )


def week_of(updated: str) -> bytes:
    """
    the fixture poll with another `updated` date, for an earlier week.
    """
    content = json.loads(POLL_FIXTURE.read_bytes())
    content["updated"] = updated
    return json.dumps(content).encode()


@pytest.fixture
def raw_store(tmp_path) -> RawStore:
    return RawStore(tmp_path / "raw")


def add_season(connection, year: int = 2026) -> None:
    connection.execute(
        "INSERT INTO season (year, start_date, end_date) VALUES (%s, %s, %s)",
        (year, dt.date(year, 8, 28), dt.date(year, 12, 21)),
    )


def add_team(connection, team_name: str, seoname: str | None = None) -> int:
    seoname = seoname or team_name.lower().replace(" ", "-").replace(".", "")
    return connection.execute(
        "INSERT INTO team (seoname, team_name) VALUES (%s, %s) RETURNING team_id", (seoname, team_name)
    ).fetchone()[0]


def add_poll_teams(connection, leave_out: tuple[str, ...] = ()) -> None:
    """
    a team for every school in the fixture polls, named the way the API's
    short names are ("Penn St."), so the State -> St. rule is covered too.
    """
    for school in dict.fromkeys(POLL_SCHOOLS + OCT_4_SCHOOLS):
        if school not in leave_out:
            add_team(connection, snapshot.short_form(school))


def count_ranks(connection) -> int:
    return connection.execute("SELECT count(*) FROM team_rank").fetchone()[0]


def run(connection, raw_store, body: bytes | None = None) -> snapshot.SnapshotSummary:
    api = PollApi(body if body is not None else POLL_FIXTURE.read_bytes())
    return snapshot.run_snapshot(connection, api.client(), raw_store)


# matching


@pytest.mark.parametrize(
    ("school", "expected"), [("Penn State", "Penn St."), ("Nebraska", "Nebraska"), ("State College", "State College")]
)
def test_short_form_shortens_trailing_state(school, expected):
    assert snapshot.short_form(school) == expected


# snapshot runs


def test_snapshot_loads_the_current_week(database, raw_store):
    add_season(database)
    add_poll_teams(database)
    summary = run(database, raw_store)

    assert summary.new_week
    assert summary.weeks == (snapshot.WeekResult(dt.date(2026, 11, 8), snapshot.WeekStatus.LOADED),)
    assert summary.error is None
    assert count_ranks(database) == 25
    assert (snapshot.dated_directory(raw_store) / "2026-11-08.json").read_bytes() == POLL_FIXTURE.read_bytes()

    penn_state = database.execute(
        "SELECT r.ranking, r.season FROM team_rank r JOIN team t USING (team_id) WHERE t.team_name = 'Penn St.'"
    ).fetchone()
    assert penn_state == (POLL_SCHOOLS.index("Penn State") + 1, 2026)
    row = database.execute("SELECT job, status, games_loaded FROM ingest_run").fetchone()
    assert row == ("snapshot", "succeeded", None)


def test_snapshot_skips_a_saved_week(database, raw_store):
    add_season(database)
    add_poll_teams(database)
    run(database, raw_store)
    second = run(database, raw_store)

    assert not second.new_week
    assert second.weeks == ()
    assert count_ranks(database) == 25


def test_snapshot_loads_older_saved_weeks(database, raw_store):
    """
    every saved week that isn't in team_rank loads, which is also how
    team_rank is rebuilt from the files.
    """
    add_season(database)
    add_poll_teams(database)
    snapshot.save_week(raw_store, week_of("Through Games NOV. 1, 2026"))
    summary = run(database, raw_store)

    assert [week.poll_date for week in summary.weeks] == [dt.date(2026, 11, 1), dt.date(2026, 11, 8)]
    assert count_ranks(database) == 50


def test_snapshot_unmatched_school_loads_nothing_until_aliased(database, raw_store):
    add_season(database)
    add_poll_teams(database, leave_out=("Illinois",))
    summary = run(database, raw_store)

    assert summary.weeks[0].unmatched == ("Illinois",)
    assert "Illinois" in summary.error
    assert count_ranks(database) == 0
    assert database.execute("SELECT status FROM ingest_run").fetchone()[0] == "failed"

    team_id = add_team(database, "Illinois Fighting Illini", seoname="illinois")
    database.execute("INSERT INTO team_alias (alias, team_id) VALUES ('Illinois', %s)", (team_id,))
    assert run(database, raw_store).weeks[0].status == snapshot.WeekStatus.LOADED
    assert count_ranks(database) == 25


def test_snapshot_name_two_teams_share_needs_an_alias(database, raw_store):
    add_season(database)
    add_poll_teams(database)
    add_team(database, "Nebraska", seoname="nebraska-other")
    summary = run(database, raw_store)

    assert summary.weeks[0].unmatched == ("Nebraska",)
    assert count_ranks(database) == 0


def test_snapshot_waits_for_its_season(database, raw_store):
    """
    a poll before any match of its season is loaded waits on disk without
    failing the run, and loads once the season is there.
    """
    add_poll_teams(database)
    summary = run(database, raw_store)

    assert summary.weeks == (snapshot.WeekResult(dt.date(2026, 11, 8), snapshot.WeekStatus.WAITING),)
    assert summary.error is None
    assert count_ranks(database) == 0

    add_season(database)
    assert run(database, raw_store).weeks[0].status == snapshot.WeekStatus.LOADED


def test_snapshot_loads_a_week_the_prototype_saved(database, raw_store):
    """
    the old snapshot files are copied into the dated folder by hand
    (ADR-005), and the next run loads them. Oct. 4 ends with a row of
    blanks, which is skipped.
    """
    add_season(database)
    add_poll_teams(database)
    raw_store.write_cache_to_disc(OCT_4_FIXTURE.read_bytes(), snapshot.dated_directory(raw_store) / "2026-10-04.json")
    summary = run(database, raw_store)

    assert [(week.poll_date, week.status) for week in summary.weeks] == [
        (dt.date(2026, 10, 4), snapshot.WeekStatus.LOADED),
        (dt.date(2026, 11, 8), snapshot.WeekStatus.LOADED),
    ]
    assert count_ranks(database) == 50


def test_snapshot_unreadable_file_fails_only_its_own_week(database, raw_store):
    """
    a current poll the parser can't read is saved as undated-..., reported
    as unreadable and fails the run, and the weeks already saved still load.
    """
    add_season(database)
    add_poll_teams(database)
    snapshot.save_week(raw_store, POLL_FIXTURE.read_bytes())
    summary = run(database, raw_store, body=week_of("Preseason"))

    assert summary.current_poll_date is None
    statuses = {week.status: week for week in summary.weeks}
    assert statuses[snapshot.WeekStatus.UNREADABLE].file.startswith("undated-")
    assert statuses[snapshot.WeekStatus.LOADED].poll_date == dt.date(2026, 11, 8)
    assert "Preseason" in summary.error
    assert count_ranks(database) == 25
    assert database.execute("SELECT status FROM ingest_run").fetchone()[0] == "failed"


def test_saved_weeks_keeps_one_poll_per_date(raw_store):
    """
    an undated file the parser can read now holds the same week as the
    dated copy, and loading both would hit team_rank's primary key.
    """
    content = POLL_FIXTURE.read_bytes()
    snapshot.save_week(raw_store, content)
    raw_store.write_cache_to_disc(content, snapshot.dated_directory(raw_store) / "undated-abc.json")
    polls, unreadable = snapshot.read_saved_weeks(raw_store)
    assert [poll.poll_date for poll in polls] == [dt.date(2026, 11, 8)]
    assert unreadable == []


def test_snapshot_takes_the_run_lock(database, database_schema, raw_store):
    database_url, _schema = database_schema
    with connect(database_url) as other_run:
        assert ingest.try_run_lock(other_run)
        with pytest.raises(ingest.RunInProgressError):
            run(database, raw_store)
    assert database.execute("SELECT count(*) FROM ingest_run").fetchone()[0] == 0
