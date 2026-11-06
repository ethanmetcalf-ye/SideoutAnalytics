import asyncio
import datetime as dt
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

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
    state: str


@dataclass(frozen=True)
class FailedRequest:
    """
    A request that failed after the client gave up on it. Kept with its
    endpoint so the run can report it and a rerun knows what to retry.
    """

    endpoint: str
    error: Exception


class Fetcher:
    """
    Decides which endpoints to request and when to refresh them (ADR-003),
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

    def current_season(self, today: dt.date) -> int:
        """
        season is named by the year it starts. it runs Aug through Jan, so
        Jan to Jun still belongs to last year's season.
        """
        return today.year if today.month >= 7 else today.year - 1

    def parse_date(self, contest_date: str) -> dt.date:
        """
        turns the schedule's contestDate (MM/DD/YYYY) into a date.
        """
        month, day, year = map(int, contest_date.split("/"))
        return dt.date(year, month, day)

    async def make_requests(
        self, api_calls: list[tuple[str, bool]]
    ) -> tuple[list[CachedResponse], list[FailedRequest]]:
        """
        sends each (endpoint, refresh) through the cache. a request the client
        gives up on is collected as a FailedRequest and the rest keep going,
        anything else (bad endpoint, failed cache write) still raises.
        """
        responses = []
        errors = []
        for endpoint, refresh in api_calls:
            try:
                responses.append(await self.raw_store.get(self.client, endpoint, refresh=refresh))
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
                self.current_season(self.current_date) <= year,
            )
            for year in self.years
        ]
        schedule_response, schedule_errors = await self.make_requests(schedule_calls)
        return schedule_response, schedule_errors

    async def get_date_scoreboards(self, dates: list[dt.date]) -> tuple[list[CachedResponse], list[FailedRequest]]:
        """
        gets the scoreboard for each date. scoreboards from yesterday on are
        refreshed since games on them can still finish, older ones come from
        the cache.
        """
        date_scoreboard_calls = [
            (
                f"/scoreboard/{self.sport}/{self.division}/{date:%Y/%m/%d}",
                date >= self.current_date - dt.timedelta(days=1),
            )
            for date in dates
        ]
        date_scoreboard_response, date_scoreboard_errors = await self.make_requests(date_scoreboard_calls)
        return date_scoreboard_response, date_scoreboard_errors

    async def get_season_index(self) -> tuple[dict[str, Game], list[FailedRequest]]:
        """
        builds every game in the seasons: schedule -> dates -> scoreboards ->
        games, deduped by gameID. returns the games along with any failed
        requests, so the caller has to check the errors to know if the index
        is complete.
        """
        schedule_responses, schedule_errors = await self.get_season_schedule()
        schedule_dates = []
        for schedule_response in schedule_responses:
            content = json.loads(schedule_response.content)
            schedule_dates += [self.parse_date(date["contestDate"]) for date in content["data"]["schedules"]["games"]]

        games_index = []
        scoreboard_response, scoreboard_errors = await self.get_date_scoreboards(schedule_dates)
        for response in scoreboard_response:
            content = json.loads(response.content)
            games_index += [
                Game(
                    game["game"]["gameID"],
                    int(game["game"]["startTimeEpoch"]),
                    game["game"]["gameState"],
                )
                for game in content["games"]
            ]
        return {game.game_id: game for game in games_index}, schedule_errors + scoreboard_errors


async def main() -> None:
    """
    runs the season index against the real API for a quick check.
    """
    async with Client.from_settings() as my_client:
        my_rawstore = RawStore.from_settings()
        fetcher = Fetcher(
            [2024, 2025],
            dt.datetime.now(tz=ZoneInfo("America/New_York")).date(),
            client=my_client,
            raw_store=my_rawstore,
        )

        games, errors = await fetcher.get_season_index()
        print(f"games index length: {len(games)}")
        print(f"errors: {errors}")


if __name__ == "__main__":
    asyncio.run(main())
