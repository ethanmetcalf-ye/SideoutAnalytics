import asyncio
import datetime as dt
import json
import logging
import pathlib
import time
from dataclasses import dataclass, field, replace

from ..db.tables import MatchState
from .cache import CachedResponse, RawStore
from .client import Client, FailedFetchError, NotAvailableYetError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Game:
    """
    One game from a scoreboard. state is pre, live or final, the game
    fetcher uses it to decide which games still need a refresh.
    scoreboard_endpoint is the scoreboard the game was found on, set by
    get_season_index so the loader can parse the game's conferences and
    round from it. Left out of equality, it's where the game came from,
    not part of the game.
    """

    game_id: str
    start_epoch: int
    state: MatchState
    scoreboard_endpoint: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FailedRequest:
    """
    A request that failed after the client gave up on it. Kept with its
    endpoint so the run can report it and a rerun knows what to retry.
    """

    endpoint: str
    error: Exception


@dataclass(frozen=True)
class FetchedFile:
    """
    A game file that is on disk, without its content. The game fetcher
    returns these instead of CachedResponse so a season's worth of files
    isn't held in memory, the loader reads them back from disk anyway.
    """

    endpoint: str
    cache_file: pathlib.Path
    content_hash: str


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
        # 3 already keeps the client at its 3 req/s rate limit. timed 24 games
        # (72 files) at 3 and at 6 on 2026-11-07, both ran at 2.9 files/s, so
        # more workers only add open connections. revisit if latency goes up
        max_concurrent_workers: int = 3,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.current_date = current_date
        self.years = years
        self.sport = "volleyball-women"
        self.division = "d1"
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

    def scoreboard_endpoint(self, date: dt.date) -> str:
        """
        the scoreboard endpoint for one date.
        """
        return f"/scoreboard/{self.sport}/{self.division}/{date:%Y/%m/%d}"

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

    def has_missing_game_files(self, details_endpoint: str) -> bool:
        """
        true if the cached details for a game say its boxscore or
        play-by-play doesn't exist. details fetched just after a match can
        say that before the stats are posted, and since final games never
        refresh the files would never be requested (ADR-004). A few matches
        really have no play-by-play, those refetch their details every run.
        Cached details that can't be read count as missing too so a bad file
        fixes itself. False when nothing is cached, get fetches a miss anyway.
        """
        cached_content = self.raw_store.read_cached_file(self.raw_store.map_endpoint_cache_file(details_endpoint))
        if cached_content is None:
            return False
        try:
            details = json.loads(cached_content)["contests"][0]
            return not (details["hasBoxscore"] and details["hasPbp"])
        except (IndexError, KeyError, json.JSONDecodeError):
            return True

    def needs_requests(self, game: Game) -> bool:
        """
        true if processing a final game will hit the API, its details aren't
        cached yet or are being refreshed. the rest come straight from disk
        in milliseconds, so progress only counts these.
        """
        details_endpoint = f"/game/{game.game_id}"
        details_file = self.raw_store.map_endpoint_cache_file(details_endpoint)
        return not details_file.is_file() or self.has_missing_game_files(details_endpoint)

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
        endpoint_dates = {self.scoreboard_endpoint(date): date for date in dates}
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
                games_index[game.game_id] = replace(game, scoreboard_endpoint=self.scoreboard_endpoint(date))
        return games_index, schedule_errors + scoreboard_errors

    async def process_game(self, game: Game) -> tuple[dict[str, FetchedFile], list[FailedRequest]]:
        """
        gets one final game's details, then the boxscore and play-by-play the
        details say it has. a final game's files don't change so they come
        from the cache, except the details get refreshed while they still say
        a file is missing (has_missing_game_files).
        If the details fail or can't be read the rest of the game is skipped
        and reported, there's nothing to say what else exists. Returns files
        without their content so nothing big is held in memory.
        """
        details_endpoint = f"/game/{game.game_id}"
        details_responses, details_errors = await self.make_requests(
            [(details_endpoint, self.has_missing_game_files(details_endpoint))]
        )
        if details_errors:
            logger.warning("Could not get details for game %s, skipping game: %s", game.game_id, details_errors)
            return {}, details_errors

        details_response = details_responses[details_endpoint]
        details_file = {details_endpoint: self.fetched_file(details_endpoint, details_response)}
        # a malformed response is a problem with this one game, so it gets
        # reported instead of cancelling every other game in the run
        try:
            details = json.loads(details_response.content)["contests"][0]
            has_boxscore = details["hasBoxscore"]
            has_playbyplay = details["hasPbp"]
        except (IndexError, KeyError, json.JSONDecodeError) as e:
            logger.warning("Unexpected details shape for game %s, skipping game: %r", game.game_id, e)
            return details_file, [FailedRequest(details_endpoint, e)]

        detailed_requests = []
        if has_boxscore:
            detailed_requests.append((f"/game/{game.game_id}/boxscore", False))
        if has_playbyplay:
            detailed_requests.append((f"/game/{game.game_id}/play-by-play", False))
        detailed_responses, detailed_errors = await self.make_requests(detailed_requests)
        if detailed_errors:
            logger.warning("Could not get every file for game %s: %s", game.game_id, detailed_errors)

        detailed_files = {
            endpoint: self.fetched_file(endpoint, response) for endpoint, response in detailed_responses.items()
        }
        return details_file | detailed_files, detailed_errors

    def fetched_file(self, endpoint: str, response: CachedResponse) -> FetchedFile:
        """
        drops the content from a response, keeping what the loader needs to
        find and check the file.
        """
        return FetchedFile(endpoint, response.cache_file, response.content_hash)

    async def fetch_one_game(
        self, game: Game, semaphore: asyncio.Semaphore, progress: dict[str, float], counts_toward_progress: bool
    ) -> tuple[dict[str, FetchedFile], list[FailedRequest]]:
        """
        runs process_game for one game once the semaphore lets it in, and
        logs progress for the games that hit the API. cached games don't
        count, they'd race the log to most of the way done in seconds.
        progress is shared by every task, safe without a lock since the
        count never spans an await.
        """
        async with semaphore:
            logger.debug("Game %s starting", game.game_id)
            results = await self.process_game(game=game)
            logger.debug("Game %s finished", game.game_id)
            if counts_toward_progress:
                progress["completed"] += 1
                completed, total = int(progress["completed"]), int(progress["total"])
                # every 10% of the games that hit the API, plus the last one.
                # rate and eta come from the time since the first game started,
                # so a slow start or end evens out over the run
                step = max(1, total // 10)
                if completed % step == 0 or completed == total:
                    elapsed = time.monotonic() - progress["started"]
                    rate = completed / elapsed if elapsed > 0 else 0.0
                    eta = (total - completed) / rate if rate > 0 else 0.0
                    logger.info(
                        "%s of %s games processed, %.1f games/s, about %s left",
                        completed,
                        total,
                        rate,
                        dt.timedelta(seconds=round(eta)),
                    )
            return results

    async def process_games_index(self, games: list[Game]) -> tuple[dict[str, FetchedFile], list[FailedRequest]]:
        """
        fetches every final game in the index, at most max_concurrent_workers
        games at a time on top of the client's rate limit. pre and live games
        are skipped, a later run picks them up once they're final.
        Returns every file keyed by endpoint and every failed request, same
        shape as get_season_index so the run log can add them up.
        """
        semaphore = asyncio.Semaphore(self.max_concurrent_workers)

        task_list = []
        final_games = [game for game in games if game.state == MatchState.FINAL]
        # decided up front, before any task can write a details file and
        # change the answer
        game_needs_requests = {game.game_id: self.needs_requests(game) for game in final_games}
        to_fetch = sum(game_needs_requests.values())
        logger.info(
            "%s final games, %s need requests, %s from cache", len(final_games), to_fetch, len(final_games) - to_fetch
        )
        progress = {"total": to_fetch, "completed": 0, "started": time.monotonic()}

        async with asyncio.TaskGroup() as tg:
            for game in final_games:
                task = tg.create_task(self.fetch_one_game(game, semaphore, progress, game_needs_requests[game.game_id]))
                task_list.append(task)

        files, errors = {}, []
        for task in task_list:
            game_files, game_errors = task.result()
            files |= game_files
            errors += game_errors
        return files, errors
