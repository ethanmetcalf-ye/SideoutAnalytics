import asyncio
import datetime as dt
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..db.tables import MatchState
from .cache import CachedResponse, RawStore
from .client import Client, FailedFetchError, NotAvailableYetError


@dataclass(frozen=True)
class Game:
    """
    One game from a scoreboard. state is pre, live or final, the game
    fetcher uses it to decide which games still need a refresh.
    """

    game_id: str
    start_epoch: int
    state: MatchState


@dataclass(frozen=True)
class FailedRequest:
    """
    A request that failed after the client gave up on it. Kept with its
    endpoint so the run can report it and a rerun knows what to retry.
    """

    endpoint: str
    error: Exception


def season_for(day: dt.date) -> int:
    """
    season is named by the year it starts. it runs Aug through Jan, so
    Jan to Jun still belongs to last year's season.
    """
    return day.year if day.month >= 7 else day.year - 1


def games_from_scoreboard(content: bytes) -> list[Game]:
    """
    turns a raw scoreboard response into its games, bytes in and rows out.
    Raises ValueError on a gameState that isn't a MatchState so an API change
    fails here with the gameID, not later as a CHECK violation in the loader.
    """
    games = []
    for entry in json.loads(content)["games"]:
        game = entry["game"]
        try:
            state = MatchState(game["gameState"])
        except ValueError as e:
            raise ValueError(f"Game {game['gameID']} has unknown gameState {game['gameState']!r}") from e
        games.append(Game(game["gameID"], int(game["startTimeEpoch"]), state))
    return games


class Fetcher:
    """
    Decides which endpoints to request and when to refresh them (ADR-004),
    and builds the season index from the schedule and daily scoreboards.
    Doesn't open the client itself, the caller opens it once and passes it
    in along with the store. current_date is passed in too so tests don't
    depend on the real date.
    """

    def __init__(
        self,
        years: list[int],
        current_date: dt.date,
        client: Client,
        raw_store: RawStore,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.current_date = current_date
        self.years = years
        self.sport = "volleyball-women"
        self.division = "d1"

    def current_season(self) -> int:
        """
        the season current_date falls in.
        """
        return season_for(self.current_date)

    def parse_date(self, contest_date: str) -> dt.date:
        """
        turns the schedule's contestDate (MM/DD/YYYY) into a date.
        """
        month, day, year = map(int, contest_date.split("/"))
        return dt.date(year, month, day)

    def has_unsettled_games(self, endpoint: str) -> bool:
        """
        true if the cached scoreboard for an endpoint has any game that isn't
        final. a scoreboard fetched before its games were played is still all
        pre, so it needs a refresh no matter how old the date is (ADR-004).
        False when nothing is cached, get fetches a miss anyway.
        """
        cached_content = self.raw_store.read_cached_file(self.raw_store.map_endpoint_cache_file(endpoint))
        if cached_content is None:
            return False
        return any(game.state != MatchState.FINAL for game in games_from_scoreboard(cached_content))

    async def make_requests(
        self, api_calls: list[tuple[str, bool]]
    ) -> tuple[dict[str, CachedResponse], list[FailedRequest]]:
        """
        sends each (endpoint, refresh) through the cache and returns the
        responses keyed by endpoint. a request the client gives up on is
        collected as a FailedRequest and the rest keep going, anything else
        (bad endpoint, failed cache write) still raises.
        """
        responses = {}
        errors = []
        for endpoint, refresh in api_calls:
            try:
                responses[endpoint] = await self.raw_store.get(self.client, endpoint, refresh=refresh)
            except (FailedFetchError, NotAvailableYetError) as e:
                errors.append(FailedRequest(endpoint, e))
        return responses, errors

    async def get_season_schedule(self) -> tuple[list[CachedResponse], list[FailedRequest]]:
        """
        gets the schedule for each season, the list of every date with games.
        the current season's schedule is refreshed since it can still gain
        dates, past seasons come from the cache.
        """
        schedule_calls = [
            (
                f"/schedule-alt/{self.sport}/{self.division}/{year}",
                self.current_season() <= year,
            )
            for year in self.years
        ]
        schedule_responses, schedule_errors = await self.make_requests(schedule_calls)
        return list(schedule_responses.values()), schedule_errors

    async def get_date_scoreboards(
        self, dates: list[dt.date]
    ) -> tuple[dict[dt.date, CachedResponse], list[FailedRequest]]:
        """
        gets the scoreboard for each date, keyed by date. scoreboards from
        yesterday on are refreshed since games on them can still finish.
        older ones are refreshed only if the cached copy still has a game
        that isn't final, otherwise they come from the cache (ADR-004).
        """
        yesterday = self.current_date - dt.timedelta(days=1)
        endpoint_dates = {f"/scoreboard/{self.sport}/{self.division}/{date:%Y/%m/%d}": date for date in dates}
        date_scoreboard_calls = [
            (endpoint, date >= yesterday or self.has_unsettled_games(endpoint))
            for endpoint, date in endpoint_dates.items()
        ]
        date_scoreboard_responses, date_scoreboard_errors = await self.make_requests(date_scoreboard_calls)
        scoreboards = {endpoint_dates[endpoint]: response for endpoint, response in date_scoreboard_responses.items()}
        return scoreboards, date_scoreboard_errors

    async def get_season_index(self) -> tuple[dict[str, Game], list[FailedRequest]]:
        """
        builds every game in the seasons: schedule -> dates -> scoreboards ->
        games, deduped by gameID. returns the games along with any failed
        requests, so the caller has to check the errors to know if the index
        is complete.
        Raises ValueError if a game shows up on two different dates. same day
        repeats are a known API quirk, a game on two days would mean something
        we don't understand yet so it shouldn't be quietly resolved.
        """
        schedule_responses, schedule_errors = await self.get_season_schedule()
        schedule_dates = []
        for schedule_response in schedule_responses:
            content = json.loads(schedule_response.content)
            schedule_dates += [self.parse_date(date["contestDate"]) for date in content["data"]["schedules"]["games"]]

        games_index = {}
        game_dates = {}
        scoreboards, scoreboard_errors = await self.get_date_scoreboards(schedule_dates)
        for date, response in scoreboards.items():
            for game in games_from_scoreboard(response.content):
                first_date = game_dates.setdefault(game.game_id, date)
                if first_date != date:
                    raise ValueError(f"Game {game.game_id} is on the scoreboards for both {first_date} and {date}")
                games_index[game.game_id] = game
        return games_index, schedule_errors + scoreboard_errors


async def main() -> None:
    """
    runs the season index for the current season against the real API for a
    quick check.
    """
    today = dt.datetime.now(tz=ZoneInfo("America/New_York")).date()
    async with Client.from_settings() as my_client:
        my_rawstore = RawStore.from_settings()
        fetcher = Fetcher(
            [season_for(today)],
            today,
            client=my_client,
            raw_store=my_rawstore,
        )

        games, errors = await fetcher.get_season_index()
        print(f"games index length: {len(games)}")
        print(f"errors: {errors}")


if __name__ == "__main__":
    asyncio.run(main())
