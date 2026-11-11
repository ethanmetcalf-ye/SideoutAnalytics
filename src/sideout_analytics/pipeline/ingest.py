"""
Runs the pipeline end to end for the CLI: fetch, then parse and load the
games whose files changed, with one ingest_run row per run (W2-07).

Decisions:
- raw_response is the record of what has been loaded. A game's files get
  their rows (path and hash) only after load_game commits, and a game is
  loaded when any of its files has no row or a different hash. A rerun with
  nothing new loads nothing, and a game that failed has no rows, so the
  next run tries it again.
- The rows are written after load_game's own transaction. A crash in
  between means the game loads again on the next run, which load_game makes
  harmless.
- fetched_at is the cache file's mtime. The cache writes each file with an
  atomic replace, so the mtime is when it was last fetched.
- Only game files are indexed. A scoreboard that changes on its own (a
  conference or round corrected later) doesn't reload its games, and
  neither does a parser or loader change. reload_all loads every game the
  fetcher returned for those cases.
- Games load one at a time, the loader isn't safe for two games with a
  team in common. Games with skipped box score lines load a second time at
  the end of the run, once the rest of the rosters are in (loader
  docstring). A game that still has skipped lines after that gets no
  raw_response rows, so every later run loads it again until a roster
  arrives that matches its jersey numbers.
- Games are parsed from the files on disk, FetchedFile has no content.
- One run at a time, across every job: a run takes a Postgres advisory lock
  on its connection before it starts and gives up if another run has it.
  Postgres drops the lock when the connection closes, crash or not.
- Holding the lock means no other run is going, so any ingest_run row still
  `running` is from a run that was killed (SIGKILL, out of memory, power).
  start_run closes those as failed with error "abandoned".
- A game that fails is recorded in load_failure and cleared when it loads.
  Only a game's first failure fails the run, later ones are logged and
  counted (games_failed) but don't, so one broken game doesn't make every
  run exit 1 until the parser is fixed. Its first failure did.
- A run fails on a request that failed for good, or a new game failure.
  NotAvailableYetError is a game that hasn't been played yet, not a failure.
- A few final games make requests on every run: details that say there's
  no box score or play-by-play are refetched each time (ADR-004 amendment).
"""

import asyncio
import datetime as dt
import logging
import pathlib
from dataclasses import dataclass

import psycopg

from ..db.loader import LoadError, LoadResult, ParsedGame, load_game
from ..db.tables import INGEST_RUN_COUNTS, IngestStatus
from . import parse
from .cache import RawStore
from .client import Client, NotAvailableYetError
from .fetch import FailedRequest, FetchedFile, Fetcher, Game

logger = logging.getLogger(__name__)

# the advisory lock key every pipeline run takes, any constant that nothing
# else in the database uses
RUN_LOCK_KEY = 520_207

RAW_RESPONSE_UPSERT = """
INSERT INTO raw_response (endpoint, file_path, content_hash, match_id, fetched_at, ingest_run_id)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (file_path) DO UPDATE SET
    endpoint = EXCLUDED.endpoint,
    content_hash = EXCLUDED.content_hash,
    match_id = EXCLUDED.match_id,
    fetched_at = EXCLUDED.fetched_at,
    ingest_run_id = EXCLUDED.ingest_run_id
"""

# xmax is 0 on a row the statement inserted and set on one it updated, so
# this returns whether the game had no failure on record
LOAD_FAILURE_UPSERT = """
INSERT INTO load_failure (source_game_id, first_failed_at, last_failed_at, error, ingest_run_id)
VALUES (%s, now(), now(), %s, %s)
ON CONFLICT (source_game_id) DO UPDATE SET
    last_failed_at = EXCLUDED.last_failed_at,
    error = EXCLUDED.error,
    ingest_run_id = EXCLUDED.ingest_run_id
RETURNING xmax = 0
"""


class RunInProgressError(Exception):
    """
    another pipeline run holds the run lock.
    """


@dataclass(frozen=True)
class FailedGame:
    """
    a game that couldn't be parsed or loaded. It has no raw_response rows,
    so the next run tries it again. new is false when it had already failed
    on an earlier run.
    """

    game_id: str
    error: Exception
    new: bool = True


@dataclass(frozen=True)
class LoadSummary:
    """
    what load_games did, for the run log.
    """

    loaded: int
    unchanged: int
    failed_games: tuple[FailedGame, ...]

    @property
    def new_failures(self) -> tuple[FailedGame, ...]:
        """
        the games that failed for the first time this run.
        """
        return tuple(failed for failed in self.failed_games if failed.new)


@dataclass(frozen=True)
class RunSummary:
    """
    one pipeline run, what the CLI reports and exits on.
    """

    ingest_run_id: int
    games_in_index: int
    loads: LoadSummary
    fetch_errors: tuple[FailedRequest, ...]

    @property
    def failed_requests(self) -> tuple[FailedRequest, ...]:
        """
        the requests that failed for good, games that aren't played yet
        don't count.
        """
        return tuple(error for error in self.fetch_errors if not isinstance(error.error, NotAvailableYetError))

    @property
    def counts(self) -> dict[str, int]:
        """
        the run's ingest_run count columns.
        """
        return {
            "games_in_index": self.games_in_index,
            "games_loaded": self.loads.loaded,
            "games_unchanged": self.loads.unchanged,
            "games_failed": len(self.loads.failed_games),
            "requests_failed": len(self.failed_requests),
        }

    @property
    def error(self) -> str | None:
        """
        the ingest_run.error text, None when the run succeeded. Games that
        were already failing don't fail the run, see the module docstring.
        """
        problems = []
        if self.failed_requests:
            problems.append(f"{len(self.failed_requests)} requests failed")
        if self.loads.new_failures:
            problems.append(f"{len(self.loads.new_failures)} games failed to load for the first time")
        return ", ".join(problems) or None


def game_id_for(endpoint: str) -> str:
    """
    the gameID in a game endpoint, /game/123 and /game/123/boxscore -> 123.
    """
    return endpoint.split("/")[2]


def files_by_game(files: dict[str, FetchedFile]) -> dict[str, dict[str, FetchedFile]]:
    """
    groups the fetcher's files by gameID, each game's files keyed by
    endpoint.
    """
    grouped: dict[str, dict[str, FetchedFile]] = {}
    for endpoint, fetched_file in files.items():
        grouped.setdefault(game_id_for(endpoint), {})[endpoint] = fetched_file
    return grouped


def relative_path(cache_file: pathlib.Path, data_directory: pathlib.Path) -> str:
    """
    raw_response.file_path, the cache file relative to the data directory.
    """
    return cache_file.relative_to(data_directory.resolve()).as_posix()


def loaded_hashes(connection: psycopg.Connection) -> dict[str, str]:
    """
    the hash of every file that has been loaded, keyed by file_path.
    """
    return dict(connection.execute("SELECT file_path, content_hash FROM raw_response").fetchall())


def is_changed(game_files: dict[str, FetchedFile], loaded: dict[str, str], data_directory: pathlib.Path) -> bool:
    """
    true if any of a game's files is new or differs from what was loaded.
    """
    return any(
        loaded.get(relative_path(fetched_file.cache_file, data_directory)) != fetched_file.content_hash
        for fetched_file in game_files.values()
    )


def try_run_lock(connection: psycopg.Connection) -> bool:
    """
    takes the run lock for this connection's session, false if another run
    has it. Postgres releases it when the connection closes.
    """
    return connection.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_KEY,)).fetchone()[0]


def release_run_lock(connection: psycopg.Connection) -> None:
    """
    gives the run lock back before the connection closes.
    """
    connection.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK_KEY,))


def start_run(connection: psycopg.Connection, job: str) -> int:
    """
    opens the run's ingest_run row and returns its id. Call it holding the
    run lock: any row still running is then from a run that was killed, and
    is closed as abandoned first.
    """
    with connection.transaction():
        abandoned = connection.execute(
            "UPDATE ingest_run SET status = %s, finished_at = now(), error = 'abandoned' "
            "WHERE status = %s RETURNING ingest_run_id",
            (IngestStatus.FAILED.value, IngestStatus.RUNNING.value),
        ).fetchall()
        if abandoned:
            logger.warning("closed abandoned ingest runs %s", [row[0] for row in abandoned])
        return connection.execute(
            "INSERT INTO ingest_run (job, started_at, status) VALUES (%s, now(), %s) RETURNING ingest_run_id",
            (job, IngestStatus.RUNNING.value),
        ).fetchone()[0]


def finish_run(
    connection: psycopg.Connection, ingest_run_id: int, error: str | None, counts: dict[str, int] | None = None
) -> None:
    """
    closes the run's ingest_run row, failed when there's an error. counts
    are keyed by column, a count that's left out stays NULL. Raises
    ValueError on a key that isn't a count column.
    """
    counts = counts or {}
    unknown = set(counts) - set(INGEST_RUN_COUNTS)
    if unknown:
        raise ValueError(f"Not ingest_run count columns: {sorted(unknown)}")
    status = IngestStatus.FAILED if error else IngestStatus.SUCCEEDED
    # the column list is fixed, so a count left out is written as NULL
    # rather than the SQL being built from the keys
    connection.execute(
        f"UPDATE ingest_run SET finished_at = now(), status = %s, error = %s, "  # noqa: S608
        f"{', '.join(f'{column} = %s' for column in INGEST_RUN_COUNTS)} WHERE ingest_run_id = %s",
        (status.value, error, *(counts.get(column) for column in INGEST_RUN_COUNTS), ingest_run_id),
    )


class GameLoader:
    """
    parses and loads games from the cache for one run. Keeps each parsed
    scoreboard so a date's scoreboard is read once, not once per game.
    """

    def __init__(
        self, connection: psycopg.Connection, raw_store: RawStore, ingest_run_id: int, data_directory: pathlib.Path
    ) -> None:
        self.connection = connection
        self.raw_store = raw_store
        self.ingest_run_id = ingest_run_id
        self.data_directory = data_directory
        self.scoreboards: dict[str, dict[str, parse.ScoreboardGame]] = {}

    def scoreboard_for(self, game: Game) -> parse.ScoreboardGame:
        """
        the scoreboard's part of a game. Raises ParseError if the game has
        no scoreboard or isn't on it, the loader can't do without it.
        """
        endpoint = game.scoreboard_endpoint
        if endpoint is None:
            raise parse.ParseError(f"Game {game.game_id} has no scoreboard")
        if endpoint not in self.scoreboards:
            content = self.raw_store.read_cached_file(self.raw_store.map_endpoint_cache_file(endpoint))
            if content is None:
                raise parse.ParseError(f"Scoreboard {endpoint} isn't cached")
            self.scoreboards[endpoint] = parse.parse_scoreboard(content)
        try:
            return self.scoreboards[endpoint][game.game_id]
        except KeyError:
            raise parse.ParseError(f"Game {game.game_id} isn't on {endpoint}") from None

    def parse_game(self, game: Game, game_files: dict[str, FetchedFile]) -> ParsedGame:
        """
        parses one game from its cached files. The box score and
        play-by-play are optional, the details say whether they exist and
        the fetcher only returns the ones that do.
        """
        details_file = game_files.get(f"/game/{game.game_id}")
        if details_file is None:
            raise parse.ParseError(f"Game {game.game_id} has no details file")
        match = parse.parse_game_details(details_file.cache_file.read_bytes())

        box_score = None
        boxscore_file = game_files.get(f"/game/{game.game_id}/boxscore")
        if boxscore_file is not None:
            box_score = parse.parse_boxscore(boxscore_file.cache_file.read_bytes())

        plays: tuple[parse.Play, ...] = ()
        playbyplay_file = game_files.get(f"/game/{game.game_id}/play-by-play")
        if playbyplay_file is not None:
            plays = parse.parse_play_by_play(playbyplay_file.cache_file.read_bytes(), match)

        return ParsedGame(match=match, scoreboard=self.scoreboard_for(game), box_score=box_score, plays=plays)

    def record_loaded(
        self, game_id: str, game_files: dict[str, FetchedFile], match_id: int, mark_files_loaded: bool
    ) -> None:
        """
        after a load: clears the game's failure, and upserts its
        raw_response rows when mark_files_loaded, which marks its files as
        loaded so later runs skip it.
        """
        rows = [
            (
                endpoint,
                relative_path(fetched_file.cache_file, self.data_directory),
                fetched_file.content_hash,
                match_id,
                dt.datetime.fromtimestamp(fetched_file.cache_file.stat().st_mtime, tz=dt.UTC),
                self.ingest_run_id,
            )
            for endpoint, fetched_file in game_files.items()
        ]
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM load_failure WHERE source_game_id = %s", (game_id,))
            if mark_files_loaded:
                cursor.executemany(RAW_RESPONSE_UPSERT, rows)

    def record_failure(self, game_id: str, error: Exception) -> bool:
        """
        records a failed game in load_failure, true if it hadn't failed
        before.
        """
        return self.connection.execute(LOAD_FAILURE_UPSERT, (game_id, str(error), self.ingest_run_id)).fetchone()[0]

    def load(self, game: Game, game_files: dict[str, FetchedFile]) -> LoadResult:
        """
        parses and loads one game, then records it. A game with skipped
        lines isn't marked loaded, so the next run loads it again. Raises
        ParseError, LoadError or OSError, and records nothing if it does.
        """
        result = load_game(self.connection, self.parse_game(game, game_files), self.ingest_run_id)
        self.record_loaded(game.game_id, game_files, result.match_id, mark_files_loaded=not result.skipped_lines)
        return result

    def try_load(self, game: Game, game_files: dict[str, FetchedFile]) -> LoadResult | FailedGame:
        """
        load, with a failed game recorded and returned instead of raised so
        one bad game doesn't stop the run.
        """
        try:
            return self.load(game, game_files)
        except (parse.ParseError, LoadError, OSError) as e:
            new = self.record_failure(game.game_id, e)
            logger.warning("Could not load game %s%s: %s", game.game_id, "" if new else " (failing since before)", e)
            return FailedGame(game.game_id, e, new=new)


def load_games(
    connection: psycopg.Connection,
    raw_store: RawStore,
    games: dict[str, Game],
    files: dict[str, FetchedFile],
    ingest_run_id: int,
    data_directory: pathlib.Path,
    reload_all: bool = False,
) -> LoadSummary:
    """
    loads every game with a new or changed file, or every game with files
    when reload_all is set. Serial, see the module docstring.
    """
    loaded = {} if reload_all else loaded_hashes(connection)
    game_files = files_by_game(files)
    to_load = [game_id for game_id, fetched in game_files.items() if is_changed(fetched, loaded, data_directory)]
    unchanged = len(game_files) - len(to_load)
    logger.info("%s games to load, %s unchanged", len(to_load), unchanged)

    loader = GameLoader(connection, raw_store, ingest_run_id, data_directory)
    failed: list[FailedGame] = []
    reload_later: list[str] = []
    loaded_count = 0
    step = max(1, len(to_load) // 10)
    for count, game_id in enumerate(to_load, start=1):
        result = loader.try_load(games[game_id], game_files[game_id])
        if isinstance(result, FailedGame):
            failed.append(result)
        else:
            loaded_count += 1
            if result.skipped_lines:
                reload_later.append(game_id)
        if count % step == 0 or count == len(to_load):
            logger.info("%s of %s games loaded", count, len(to_load))

    # a second pass for unnamed box score lines, their jersey numbers can
    # match roster players from games loaded after them. A game that still
    # has skipped lines stays unmarked so the next run tries again
    still_skipped = 0
    for game_id in reload_later:
        result = loader.try_load(games[game_id], game_files[game_id])
        if isinstance(result, FailedGame):
            # loaded in the first pass, but it's the failure that counts
            failed.append(result)
            loaded_count -= 1
        else:
            still_skipped += result.skipped_lines
    if reload_later:
        logger.info("reloaded %s games with skipped lines, %s lines still skipped", len(reload_later), still_skipped)

    return LoadSummary(loaded=loaded_count, unchanged=unchanged, failed_games=tuple(failed))


async def fetch(
    client: Client, season_years: list[int], today: dt.date, raw_store: RawStore
) -> tuple[dict[str, Game], dict[str, FetchedFile], list[FailedRequest]]:
    """
    the season index and every final game's files, with the failed
    requests from both. Opens the client and closes it when done.
    """
    async with client:
        fetcher = Fetcher(season_years, today, client=client, raw_store=raw_store)
        games, index_errors = await fetcher.get_season_index()
        files, game_errors = await fetcher.process_games_index(list(games.values()))
    return games, files, index_errors + game_errors


def run(
    connection: psycopg.Connection,
    client: Client,
    raw_store: RawStore,
    job: str,
    season_years: list[int],
    today: dt.date,
    data_directory: pathlib.Path,
    reload_all: bool = False,
) -> RunSummary:
    """
    one pipeline run: takes the run lock, opens an ingest_run row, fetches,
    loads, and closes the row as succeeded or failed. Everything it uses is
    passed in, the CLI builds it from settings. client isn't open yet, the
    run opens it.
    Raises RunInProgressError, without writing a row, when another run has
    the lock. An exception that stops the run (a bad scoreboard, the
    database going away, Ctrl-C, SIGTERM through the CLI's handler) marks
    the row failed and is raised again.
    """
    if not try_run_lock(connection):
        raise RunInProgressError("another pipeline run is in progress")
    try:
        ingest_run_id = start_run(connection, job)
        logger.info("ingest run %s started: %s for %s", ingest_run_id, job, season_years)
        try:
            games, files, fetch_errors = asyncio.run(fetch(client, season_years, today, raw_store))
            logger.info("%s games in index, %s files, %s failed requests", len(games), len(files), len(fetch_errors))
            loads = load_games(connection, raw_store, games, files, ingest_run_id, data_directory, reload_all)
        except BaseException as e:
            # if the database is what failed, closing the row fails too, and
            # that error shouldn't hide the one that stopped the run
            try:
                finish_run(connection, ingest_run_id, f"{type(e).__name__}: {e}")
            except Exception:
                logger.exception("Could not close ingest run %s", ingest_run_id)
            raise

        summary = RunSummary(
            ingest_run_id=ingest_run_id, games_in_index=len(games), loads=loads, fetch_errors=tuple(fetch_errors)
        )
        finish_run(connection, ingest_run_id, summary.error, summary.counts)
        return summary
    finally:
        # skipped if the connection is broken, closing it releases the lock
        if not connection.closed and not connection.broken:
            release_run_lock(connection)
