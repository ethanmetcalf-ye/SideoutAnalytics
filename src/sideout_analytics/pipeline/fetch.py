import asyncio
import datetime as dt
import json
import time
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
        max_concurrent_workers: int = 3,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.current_date = current_date
        self.years = years
        self.sport = "volleyball-women"
        self.division = "d1"
        self.games_index = []
        self.max_concurrent_workers = max_concurrent_workers

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
        self.games_index = games_index
        return games_index, schedule_errors + scoreboard_errors

    async def process_game(self, game: Game) -> tuple[dict[str, CachedResponse], list[FailedRequest]]:
        """
        gets one final game's details, then the boxscore and play-by-play the
        details say it has. never refreshes since a final game's files don't
        change, so a rerun gets everything from the cache.
        If the details fail or have no contests the rest of the game is
        skipped and reported, there's nothing to say what else exists.
        """
        refresh_game = False
        game_details_request = (f"/game/{game.game_id}", refresh_game)
        game_boxscore_request = (f"/game/{game.game_id}/boxscore", refresh_game)
        game_playbyplay_request = (f"/game/{game.game_id}/play-by-play", refresh_game)

        detailed_requests = []
        game_details_responses, game_details_errors = await self.make_requests([game_details_request])
        if game_details_errors != []:
            print(f"Could not process game {game.game_id}: {game_details_errors}. Skipping game...")
            return game_details_responses, game_details_errors
        try:
            game_details_response = json.loads(game_details_responses[game_details_request[0]].content)["contests"][0]
        except IndexError as e:
            print(f"Could not process game {game.game_id}: unexpected response shape received. Skipping game...")
            return game_details_responses, [FailedRequest(game_details_request[0], e)]
        game_has_boxscore = game_details_response["hasBoxscore"]
        game_has_playbyplay = game_details_response["hasPbp"]

        if game_has_boxscore:
            detailed_requests.append(game_boxscore_request)
        if game_has_playbyplay:
            detailed_requests.append(game_playbyplay_request)

        detailed_responses, detailed_response_errors = await self.make_requests(detailed_requests)

        return game_details_responses | detailed_responses, game_details_errors + detailed_response_errors

    async def worker(
        self, game: Game, semaphore: asyncio.Semaphore, num_tasks: dict[str, int]
    ) -> tuple[dict[str, CachedResponse], list[FailedRequest]]:
        """
        runs process_game for one game once the semaphore lets it in, and
        prints progress every 10% of the run. progress is shared by every
        worker, safe without a lock since the count never spans an await.
        """
        async with semaphore:
            print(f"[{time.strftime('%X')}] Game {game.game_id} acquired permit. Starting work...")
            results = await self.process_game(game=game)
            print(f"[{time.strftime('%X')}] Game {game.game_id} finished. Releasing permit.")
            num_tasks["completed"] += 1
            step = max(1, num_tasks["total"] // 10)
            if num_tasks["completed"] % step == 0 or num_tasks["completed"] == num_tasks["total"]:
                print(f"{num_tasks['completed']} of total {num_tasks['total']} games have been processed.")
            return results

    async def process_games_index(self, games: list[Game]) -> tuple[dict[str, CachedResponse], list[FailedRequest]]:
        """
        fetches every final game in the index, at most max_concurrent_workers
        games at a time on top of the client's rate limit. pre and live games
        are skipped, a later run picks them up once they're final.
        Returns every response keyed by endpoint and every failed request,
        same shape as get_season_index so the run log can add them up.
        """
        my_semaphore = asyncio.Semaphore(self.max_concurrent_workers)

        task_list = []
        final_games = [game for game in games if game.state == MatchState.FINAL]
        progress = {"total": len(final_games), "completed": 0}

        async with asyncio.TaskGroup() as tg:
            for game in final_games:
                task = tg.create_task(self.worker(game, my_semaphore, progress))
                task_list.append(task)

        print("All games have been processed")

        responses, errors = {}, []
        for task in task_list:
            game_responses, game_errors = task.result()
            responses |= game_responses
            errors += game_errors
        return responses, errors


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

        games, errors = await fetcher.get_season_index()  # games = {"6639890": Game(...), ...}
        processed_games, game_errors = await fetcher.process_games_index(list(games.values()))
        print(f"games index length: {len(games)}")
        print(f"files fetched: {len(processed_games)}")
        print(f"games index errors: {errors}")
        print(f"games processing errors: {game_errors}")


if __name__ == "__main__":
    asyncio.run(main())
