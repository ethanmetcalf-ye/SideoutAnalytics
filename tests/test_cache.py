import pathlib
import stat

import httpx
import pytest

from sideout_analytics.pipeline import cache
from sideout_analytics.pipeline import client as api_client

# runs every async test in this file on the anyio pytest plugin
pytestmark = pytest.mark.anyio

# settings needed for from_settings. only the from_settings test uses these
SETTINGS_ENV = {
    "DATA_DIRECTORY": "./data",
    "DATABASE_URL": "postgresql://user:password@localhost:5432/testdb",
}

# endpoints shaped like the real API and the file each one should land in,
# relative to the cache directory
ENDPOINT_FILES = [
    ("/game/123/boxscore", "game/123/boxscore.json"),
    ("/game/123", "game/123.json"),
    ("/game/123/", "game/123.json"),
    (
        "/scoreboard/volleyball-women/d1/2025/10/01",
        "scoreboard/volleyball-women/d1/2025/10/01.json",
    ),
    (
        "/stats/volleyball-women/d1/current/team/45?page=2",
        "stats/volleyball-women/d1/current/team/45__page=2.json",
    ),
    ("/logo/stanford.svg", "logo/stanford.svg"),
    ("/logo/stanford.svg?dark=true", "logo/stanford__dark=true.svg"),
    ("/scoreboard?", "scoreboard.json"),
    ("/game/1?dark", "game/1__dark=.json"),
    ("/game/%2e%2e/x", "game/%2e%2e/x.json"),
    # a decoded / in a query value stays in the file name instead of
    # pointing at another file
    ("/game/1?x=a%2Fb", "game/1__x=a%2Fb.json"),
    # a value ending in / keeps it, only the path gets stripped
    ("/x/y?a=/", "x/y__a=%2F.json"),
    # : and // are just path characters, not a url scheme or host
    ("/game:1/boxscore", "game:1/boxscore.json"),
    ("//game/1", "game/1.json"),
    (
        "/game/1?x=..%2F..%2F2%2Fboxscore",
        "game/1__x=..%2F..%2F2%2Fboxscore.json",
    ),
]

# endpoints that would escape the cache directory, or map to nothing
BAD_ENDPOINTS = [
    "",
    "/",
    "/../../.env",
    "/game/../../x",
]


class MockApi:
    """
    Mock of the NCAA API that answers every request with the same body,
    or a scripted status code. Counts requests so tests can check when the
    network is skipped.
    """

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """
        handler for httpx.MockTransport. the body includes the path so each
        endpoint gets different bytes.
        """
        self.requests.append(request)
        return httpx.Response(
            self.status_code, content=self.body(request.url.path)
        )

    def body(self, endpoint: str) -> bytes:
        """
        the bytes the mock returns for an endpoint.
        """
        return f'{{"endpoint": "{endpoint}"}}'.encode()

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


def failing_handler(request: httpx.Request) -> httpx.Response:
    """
    handler that fails the test if any request reaches the network.
    """
    pytest.fail(f"unexpected request to {request.url.path}")


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


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_FILES)
def test_cache_maps_endpoint_to_file(raw_store, endpoint, expected) -> None:
    """
    each endpoint maps to a predictable absolute file inside the cache
    directory.
    """
    cache_file = raw_store.map_endpoint_cache_file(endpoint)
    root = raw_store.cache_directory.resolve()
    assert cache_file.is_absolute()
    assert cache_file == root / expected


def test_cache_query_order_maps_to_same_file(raw_store) -> None:
    """
    the same query in a different order is the same cache file.
    """
    assert raw_store.map_endpoint_cache_file(
        "/s?b=2&a=1"
    ) == raw_store.map_endpoint_cache_file("/s?a=1&b=2")


def test_cache_game_file_and_folder_do_not_collide(raw_store) -> None:
    """
    /game/123 is a file and /game/123/boxscore lives in a folder of the same
    name. the .json suffix lets both exist side by side.
    """
    game_file = raw_store.map_endpoint_cache_file("/game/123")
    boxscore_file = raw_store.map_endpoint_cache_file("/game/123/boxscore")
    raw_store.write_cache_to_disc(b"game", game_file)
    raw_store.write_cache_to_disc(b"boxscore", boxscore_file)
    assert game_file.read_bytes() == b"game"
    assert boxscore_file.read_bytes() == b"boxscore"


@pytest.mark.parametrize("endpoint", BAD_ENDPOINTS)
def test_cache_rejects_bad_endpoint(raw_store, endpoint) -> None:
    """
    empty endpoints and anything that resolves outside the cache directory
    raise ValueError instead of mapping to a file.
    """
    with pytest.raises(ValueError):
        raw_store.map_endpoint_cache_file(endpoint)


def test_cache_read_missing_file_returns_none(raw_store) -> None:
    """
    reading an endpoint that was never cached returns None.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1")
    assert raw_store.read_cached_file(cache_file) is None


def test_cache_write_creates_nested_file(raw_store) -> None:
    """
    writing creates any missing folders and returns the file and the sha256
    of its content.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    written_file, content_hash = raw_store.write_cache_to_disc(
        b"{}", cache_file
    )
    assert written_file == cache_file
    assert raw_store.read_cached_file(cache_file) == b"{}"
    assert content_hash == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )


def test_cache_write_leaves_no_temp_file(raw_store) -> None:
    """
    after a write the folder holds only the cache file, the temp file is
    renamed into place.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"{}", cache_file)
    assert list(cache_file.parent.iterdir()) == [cache_file]


def test_cache_write_file_is_world_readable(raw_store) -> None:
    """
    cache files are 0644 instead of the temp file's owner only 0600, so a
    backup job or container running as another user can read them.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"{}", cache_file)
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o644


def test_cache_failed_write_keeps_old_file(raw_store, monkeypatch) -> None:
    """
    if the write fails the old file is untouched, the temp file is cleaned
    up and the OSError reaches the caller.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"old", cache_file)

    def failing_replace(self, target) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        raw_store.write_cache_to_disc(b"new", cache_file)
    assert cache_file.read_bytes() == b"old"
    assert list(cache_file.parent.iterdir()) == [cache_file]


def test_cache_interrupted_write_cleans_up(raw_store, monkeypatch) -> None:
    """
    a Ctrl-C mid write still removes the temp file and keeps the old file,
    and the KeyboardInterrupt still reaches the caller.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"old", cache_file)

    def interrupted_replace(self, target) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pathlib.Path, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        raw_store.write_cache_to_disc(b"new", cache_file)
    assert cache_file.read_bytes() == b"old"
    assert list(cache_file.parent.iterdir()) == [cache_file]


async def test_cache_miss_fetches_and_writes(raw_store) -> None:
    """
    an endpoint that isn't cached makes one request and saves the response.
    """
    mock_api = MockApi()
    async with mock_api.client() as client:
        response = await raw_store.get(client, "/game/1/boxscore")

    body = mock_api.body("/game/1/boxscore")
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    assert len(mock_api.requests) == 1
    assert response == cache.CachedResponse(
        body, cache_file, cache.hash_content(body)
    )
    assert cache_file.read_bytes() == body


async def test_cache_hit_skips_network(raw_store) -> None:
    """
    an endpoint that is already cached returns the file without a request,
    still with its path and hash so the loader gets the same result either
    way.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"cached", cache_file)

    transport = httpx.MockTransport(failing_handler)
    async with api_client.Client(
        "https://api.test", "sideout-analytics-tests/0.1", transport
    ) as client:
        response = await raw_store.get(client, "/game/1/boxscore")
    assert response == cache.CachedResponse(
        b"cached", cache_file, cache.hash_content(b"cached")
    )


async def test_cache_rerun_makes_zero_requests(tmp_path) -> None:
    """
    a second run pointed at the same data dir gets every response from disk
    and never touches the network.
    """
    endpoints = [
        "/scoreboard/volleyball-women/d1/2025/10/01",
        "/game/1",
        "/game/1/boxscore",
        "/game/1/play-by-play",
        "/stats/volleyball-women/d1/current/team/45?page=2",
    ]
    mock_api = MockApi()
    first_store = cache.RawStore(tmp_path / "raw")
    async with mock_api.client() as client:
        first_run = [
            await first_store.get(client, endpoint) for endpoint in endpoints
        ]
    assert len(mock_api.requests) == len(endpoints)

    second_store = cache.RawStore(tmp_path / "raw")
    transport = httpx.MockTransport(failing_handler)
    async with api_client.Client(
        "https://api.test", "sideout-analytics-tests/0.1", transport
    ) as client:
        second_run = [
            await second_store.get(client, endpoint) for endpoint in endpoints
        ]
    assert second_run == first_run


async def test_cache_refresh_overwrites(raw_store) -> None:
    """
    refresh=True fetches even when cached and replaces the old file, for
    responses that can still change like live games.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"partial", cache_file)

    mock_api = MockApi()
    async with mock_api.client() as client:
        response = await raw_store.get(
            client, "/game/1/boxscore", refresh=True
        )
    body = mock_api.body("/game/1/boxscore")
    assert len(mock_api.requests) == 1
    assert response.content == body
    assert response.content_hash == cache.hash_content(body)
    assert cache_file.read_bytes() == body


async def test_cache_failed_refresh_keeps_old_file(raw_store) -> None:
    """
    a refresh that fails raises FailedFetchError and keeps the old file.
    """
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    raw_store.write_cache_to_disc(b"partial", cache_file)

    mock_api = MockApi(status_code=404)
    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError):
            await raw_store.get(client, "/game/1/boxscore", refresh=True)
    assert cache_file.read_bytes() == b"partial"


async def test_cache_not_available_writes_nothing(raw_store) -> None:
    """
    an unplayed match raises NotAvailableYetError and nothing is cached, so
    the next run asks again.
    """
    mock_api = MockApi(status_code=502)
    async with mock_api.client() as client:
        with pytest.raises(api_client.NotAvailableYetError):
            await raw_store.get(client, "/game/1/boxscore")
    cache_file = raw_store.map_endpoint_cache_file("/game/1/boxscore")
    assert not cache_file.exists()


def test_cache_from_settings(monkeypatch, isolated_env) -> None:
    """
    from_settings puts the cache in a raw folder under data_directory.
    """
    for name, value in SETTINGS_ENV.items():
        monkeypatch.setenv(name, value)

    raw_store = cache.RawStore.from_settings()
    assert raw_store.cache_directory == pathlib.Path("./data/raw")
