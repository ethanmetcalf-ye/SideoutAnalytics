import datetime as dt
import json

import httpx
import pytest

from sideout_analytics.db.tables import MatchState
from sideout_analytics.pipeline import cache, fetch
from sideout_analytics.pipeline import client as api_client

# runs every async test in this file on the anyio pytest plugin
pytestmark = pytest.mark.anyio

# mid season, so 2025 is the current season and 2024 is settled
TODAY = dt.date(2025, 10, 15)

SCHEDULE_2024 = "/schedule-alt/volleyball-women/d1/2024"
SCHEDULE_2025 = "/schedule-alt/volleyball-women/d1/2025"


def schedule_body(*contest_dates: str) -> bytes:
    """
    schedule-alt payload listing each date that has games, shaped like the
    real API. contest_dates are MM/DD/YYYY.
    """
    games = [{"contestDate": contest_date, "count": 1} for contest_date in contest_dates]
    return json.dumps({"data": {"schedules": {"games": games, "today": ""}}}).encode()


def scoreboard_body(*games: tuple[str, int, str]) -> bytes:
    """
    scoreboard payload with one entry per (game_id, start_epoch, state).
    """
    entries = [
        {"game": {"gameID": game_id, "startTimeEpoch": str(start_epoch), "gameState": state}}
        for game_id, start_epoch, state in games
    ]
    return json.dumps({"games": entries}).encode()


def scoreboard_path(day: dt.date) -> str:
    """
    scoreboard endpoint for a date.
    """
    return f"/scoreboard/volleyball-women/d1/{day:%Y/%m/%d}"


class MockApi:
    """
    Mock of the NCAA API that answers each path with a scripted body.
    Unknown paths get a 404. Records every requested path so tests can check
    what hit the network and what came from the cache.
    """

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.requested_paths = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """
        handler for httpx.MockTransport.
        """
        self.requested_paths.append(request.url.path)
        if request.url.path not in self.bodies:
            return httpx.Response(404)
        return httpx.Response(200, content=self.bodies[request.url.path])

    def client(self) -> api_client.Client:
        """
        builds a Client wired to this mock. rate limit is set high so the
        limiter never slows a test down.
        """
        return api_client.Client(
            base_url="https://api.test",
            user_agent="sideout-analytics-tests/0.1",
            transport=httpx.MockTransport(self.handle),
            rate_limit_per_sec=1000,
        )


@pytest.fixture
def anyio_backend() -> str:
    """
    run the async tests on asyncio only.
    """
    return "asyncio"


@pytest.fixture
def raw_store(tmp_path) -> cache.RawStore:
    """
    store pointed at an empty cache directory in tmp_path.
    """
    return cache.RawStore(tmp_path / "raw")


def make_fetcher(raw_store, client, years=(2025,), today=TODAY) -> fetch.Fetcher:
    """
    builds a Fetcher for the given seasons with a fixed today, so no test
    depends on the real date.
    """
    return fetch.Fetcher(list(years), today, client=client, raw_store=raw_store)


def write_cached(raw_store, endpoint: str, content: bytes) -> None:
    """
    puts a response in the cache as if an earlier run had fetched it.
    """
    raw_store.write_cache_to_disc(content, raw_store.map_endpoint_cache_file(endpoint))


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (dt.date(2025, 10, 15), 2025),
        (dt.date(2025, 12, 31), 2025),
        (dt.date(2026, 1, 15), 2025),
        (dt.date(2026, 6, 30), 2025),
        (dt.date(2026, 7, 1), 2026),
    ],
)
def test_fetch_season_for(today, expected) -> None:
    """
    the season is named by the year it starts. Jan to Jun still belongs to
    last year's season since late matches run into January.
    """
    assert fetch.season_for(today) == expected


def test_fetch_current_season_uses_current_date(raw_store) -> None:
    """
    current_season is the season the fetcher's current_date falls in.
    """
    fetcher = make_fetcher(raw_store, client=None, today=dt.date(2026, 1, 15))
    assert fetcher.current_season() == 2025


def test_fetch_parse_date(raw_store) -> None:
    """
    contestDate comes as MM/DD/YYYY and becomes a date.
    """
    fetcher = make_fetcher(raw_store, client=None)
    assert fetcher.parse_date("09/05/2025") == dt.date(2025, 9, 5)


async def test_fetch_season_index_walks_schedule(raw_store) -> None:
    """
    every date in the schedule gets its scoreboard requested, and every game
    on those scoreboards ends up in the index with its state.
    """
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("09/05/2025", "09/06/2025"),
            scoreboard_path(dt.date(2025, 9, 5)): scoreboard_body(("1", 100, "final"), ("2", 200, "final")),
            scoreboard_path(dt.date(2025, 9, 6)): scoreboard_body(("3", 300, "final")),
        }
    )
    async with mock_api.client() as client:
        games, errors = await make_fetcher(raw_store, client).get_season_index()

    assert errors == []
    assert games == {
        "1": fetch.Game("1", 100, "final"),
        "2": fetch.Game("2", 200, "final"),
        "3": fetch.Game("3", 300, "final"),
    }


async def test_fetch_season_index_dedupes_by_game_id(raw_store) -> None:
    """
    the scoreboard can list the same gameID twice on one day. the index keeps
    one entry per game.
    """
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("09/05/2025"),
            scoreboard_path(dt.date(2025, 9, 5)): scoreboard_body(
                ("1", 100, "final"), ("2", 200, "final"), ("1", 100, "final")
            ),
        }
    )
    async with mock_api.client() as client:
        games, errors = await make_fetcher(raw_store, client).get_season_index()

    assert errors == []
    assert list(games) == ["1", "2"]


async def test_fetch_season_index_keeps_game_states(raw_store) -> None:
    """
    pre, live and final games all make it into the index with their state,
    so the game fetcher can decide what to refresh.
    """
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("10/15/2025"),
            scoreboard_path(dt.date(2025, 10, 15)): scoreboard_body(
                ("1", 100, "final"), ("2", 200, "live"), ("3", 300, "pre")
            ),
        }
    )
    async with mock_api.client() as client:
        games, errors = await make_fetcher(raw_store, client).get_season_index()

    assert errors == []
    assert {game_id: game.state for game_id, game in games.items()} == {
        "1": "final",
        "2": "live",
        "3": "pre",
    }


async def test_fetch_empty_scoreboard_adds_no_games(raw_store) -> None:
    """
    a day with no games returns an empty games list and adds nothing.
    """
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("09/05/2025"),
            scoreboard_path(dt.date(2025, 9, 5)): scoreboard_body(),
        }
    )
    async with mock_api.client() as client:
        games, errors = await make_fetcher(raw_store, client).get_season_index()

    assert errors == []
    assert games == {}


async def test_fetch_refreshes_current_season_schedule_only(raw_store) -> None:
    """
    the current season's schedule can still gain dates so it is refetched.
    a past season's schedule comes from the cache.
    """
    write_cached(raw_store, SCHEDULE_2024, schedule_body())
    write_cached(raw_store, SCHEDULE_2025, schedule_body())
    mock_api = MockApi({SCHEDULE_2024: schedule_body(), SCHEDULE_2025: schedule_body()})

    async with mock_api.client() as client:
        await make_fetcher(raw_store, client, years=(2024, 2025)).get_season_schedule()

    assert mock_api.requested_paths == [SCHEDULE_2025]


async def test_fetch_refreshes_scoreboards_from_yesterday_on(raw_store) -> None:
    """
    scoreboards from yesterday on can still change so they are refetched.
    older ones come from the cache.
    """
    days = [TODAY - dt.timedelta(days=offset) for offset in (7, 2, 1, 0)]
    for day in days:
        write_cached(raw_store, scoreboard_path(day), scoreboard_body())
    mock_api = MockApi({scoreboard_path(day): scoreboard_body() for day in days})

    async with mock_api.client() as client:
        await make_fetcher(raw_store, client).get_date_scoreboards(days)

    assert mock_api.requested_paths == [
        scoreboard_path(TODAY - dt.timedelta(days=1)),
        scoreboard_path(TODAY),
    ]


async def test_fetch_past_season_rerun_makes_zero_requests(raw_store) -> None:
    """
    once a past season is cached, building its index again never touches
    the network.
    """
    bodies = {
        SCHEDULE_2024: schedule_body("09/05/2024"),
        scoreboard_path(dt.date(2024, 9, 5)): scoreboard_body(("1", 100, "final")),
    }
    first_api = MockApi(bodies)
    async with first_api.client() as client:
        first_run = await make_fetcher(raw_store, client, years=(2024,)).get_season_index()

    second_api = MockApi({})
    async with second_api.client() as client:
        second_run = await make_fetcher(raw_store, client, years=(2024,)).get_season_index()

    assert second_api.requested_paths == []
    assert second_run == first_run


async def test_fetch_failed_scoreboard_is_reported(raw_store) -> None:
    """
    a scoreboard that fails doesn't stop the run. the other dates still make
    it into the index and the failed one comes back as a FailedRequest.
    """
    missing_day = dt.date(2025, 9, 6)
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("09/05/2025", "09/06/2025", "09/07/2025"),
            scoreboard_path(dt.date(2025, 9, 5)): scoreboard_body(("1", 100, "final")),
            scoreboard_path(dt.date(2025, 9, 7)): scoreboard_body(("3", 300, "final")),
        }
    )
    async with mock_api.client() as client:
        games, errors = await make_fetcher(raw_store, client).get_season_index()

    assert list(games) == ["1", "3"]
    assert [error.endpoint for error in errors] == [scoreboard_path(missing_day)]
    assert isinstance(errors[0].error, api_client.FailedFetchError)


async def test_fetch_failed_schedule_is_reported(raw_store) -> None:
    """
    a season whose schedule fails is reported and skipped, the other seasons
    still get indexed.
    """
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("09/05/2025"),
            scoreboard_path(dt.date(2025, 9, 5)): scoreboard_body(("1", 100, "final")),
        }
    )
    async with mock_api.client() as client:
        games, errors = await make_fetcher(raw_store, client, years=(2024, 2025)).get_season_index()

    assert list(games) == ["1"]
    assert [error.endpoint for error in errors] == [SCHEDULE_2024]


async def test_fetch_cache_write_error_is_raised(raw_store, monkeypatch) -> None:
    """
    only failed requests get collected. a cache write that fails is a
    different problem and still stops the run.
    """

    def failing_write(response_content, cache_file) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(raw_store, "write_cache_to_disc", failing_write)
    mock_api = MockApi({SCHEDULE_2025: schedule_body()})
    async with mock_api.client() as client:
        with pytest.raises(OSError, match="disk full"):
            await make_fetcher(raw_store, client).get_season_index()


def test_fetch_games_from_scoreboard() -> None:
    """
    raw scoreboard bytes become Games, with gameState as a MatchState.
    """
    games = fetch.games_from_scoreboard(scoreboard_body(("1", 100, "final"), ("2", 200, "pre")))
    assert games == [
        fetch.Game("1", 100, MatchState.FINAL),
        fetch.Game("2", 200, MatchState.PRE),
    ]
    assert isinstance(games[0].state, MatchState)


def test_fetch_unknown_game_state_raises() -> None:
    """
    a gameState that isn't pre, live or final fails here with the gameID,
    instead of later as a CHECK violation in the loader.
    """
    with pytest.raises(ValueError, match="Game 7 has unknown gameState 'canceled'"):
        fetch.games_from_scoreboard(scoreboard_body(("7", 100, "canceled")))


async def test_fetch_refreshes_past_scoreboard_with_unplayed_games(raw_store) -> None:
    """
    a past scoreboard cached before its games were played still says pre.
    it gets refreshed even though the date is old, so a missed day doesn't
    freeze those games as unplayed. a past scoreboard that is all final
    stays cached.
    """
    stale_day = TODAY - dt.timedelta(days=5)
    settled_day = TODAY - dt.timedelta(days=6)
    write_cached(raw_store, scoreboard_path(stale_day), scoreboard_body(("1", 100, "pre")))
    write_cached(raw_store, scoreboard_path(settled_day), scoreboard_body(("2", 200, "final")))
    mock_api = MockApi(
        {
            scoreboard_path(stale_day): scoreboard_body(("1", 100, "final")),
            scoreboard_path(settled_day): scoreboard_body(("2", 200, "final")),
        }
    )

    async with mock_api.client() as client:
        scoreboards, errors = await make_fetcher(raw_store, client).get_date_scoreboards([settled_day, stale_day])

    assert errors == []
    assert mock_api.requested_paths == [scoreboard_path(stale_day)]
    assert fetch.games_from_scoreboard(scoreboards[stale_day].content)[0].state == MatchState.FINAL


async def test_fetch_game_on_two_dates_raises(raw_store) -> None:
    """
    the same gameID on two different dates has never happened, so it raises
    instead of quietly keeping one. same day repeats are still deduped.
    """
    mock_api = MockApi(
        {
            SCHEDULE_2025: schedule_body("09/05/2025", "09/06/2025"),
            scoreboard_path(dt.date(2025, 9, 5)): scoreboard_body(("1", 100, "final")),
            scoreboard_path(dt.date(2025, 9, 6)): scoreboard_body(("1", 100, "final")),
        }
    )
    async with mock_api.client() as client:
        with pytest.raises(ValueError, match="Game 1 is on the scoreboards for both 2025-09-05 and 2025-09-06"):
            await make_fetcher(raw_store, client).get_season_index()
