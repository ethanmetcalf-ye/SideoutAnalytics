import anyio
import httpx
import pytest

from sideout_analytics.pipeline import ingestion

# runs every async test in this file on the anyio pytest plugin
pytestmark = pytest.mark.anyio

# settings the client needs. base url is fake since no request leaves the
# machine
TEST_ENV = {
    "DATA_DIRECTORY": "./data",
    "DATABASE_URL": "postgresql://user:password@localhost:5432/testdb",
    "NCAA_BASE_API_URL": "https://api.test",
}


# Mock of the NCAA API that replays scripted responses in order
# Records each request and each retry sleep so tests can check them
class MockApi:
    # responses can be an httpx.Response or an exception to raise instead,
    # ex. a timeout
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests = []
        self.sleeps = []

    # handler for httpx.MockTransport. returns the next scripted response
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    # stands in for tenacity's sleep so retries record the wait time instead
    # of actually waiting
    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    # builds a Client wired to this mock. rate limit is set high by default so
    # the limiter never slows a test down
    def client(self, **kwargs: object) -> ingestion.Client:
        kwargs.setdefault("rate_limit_per_sec", 1000)
        client = ingestion.Client(
            transport=httpx.MockTransport(self.handle), **kwargs
        )
        client.retry_params["sleep"] = self.sleep
        return client


# Stands in for AsyncLimiter and counts how often it is entered
# Lets tests check which requests go through the limiter without waiting on
# real time
class CountingLimiter:
    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *exc_info: object) -> None:
        pass


# aiolimiter only supports asyncio, so skip the trio backend
@pytest.fixture
def anyio_backend():
    return "asyncio"


# applies to every test. starts from a clean environment then sets TEST_ENV
@pytest.fixture(autouse=True)
def api_env(monkeypatch, isolated_env):
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)


# 200 returns the raw body on the first try with no retry waits
# body is checked byte for byte since it is cached as is
async def test_client_returns_raw_body_on_200() -> None:
    raw_body = b'{"games": [], "updated_at": "2026-11-01"}'
    mock_api = MockApi(httpx.Response(200, content=raw_body))

    async with mock_api.client() as client:
        body = await client.get_requests_async("/scoreboard")

    assert body == raw_body
    assert len(mock_api.requests) == 1
    assert mock_api.sleeps == []


# path is joined to the base url and the user agent identifies this repo
async def test_client_request_url_and_user_agent() -> None:
    mock_api = MockApi(httpx.Response(200))

    async with mock_api.client() as client:
        await client.get_requests_async("/game/6480202/boxscore")

    request = mock_api.requests[0]
    assert str(request.url) == "https://api.test/game/6480202/boxscore"
    assert request.headers["User-Agent"].startswith("sideout-analytics/")


# timeouts match the values set in Client
async def test_client_timeouts() -> None:
    async with MockApi().client() as client:
        timeout = client._http.timeout

    assert timeout.connect == 5
    assert timeout.read == 15


# each retryable code fails twice then succeeds on the third attempt
@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
async def test_client_retries_unavailable(status_code) -> None:
    mock_api = MockApi(
        httpx.Response(status_code),
        httpx.Response(status_code),
        httpx.Response(200, content=b"ok"),
    )

    async with mock_api.client() as client:
        body = await client.get_requests_async("/scoreboard")

    assert body == b"ok"
    assert len(mock_api.requests) == 3
    assert len(mock_api.sleeps) == 2


# default of 4 attempts, then raises FailedFetchError with the last
# retryable error kept as the cause
async def test_client_stops_after_max_retries() -> None:
    mock_api = MockApi(*[httpx.Response(503)] * 4)

    async with mock_api.client() as client:
        with pytest.raises(ingestion.FailedFetchError) as exc_info:
            await client.get_requests_async("/scoreboard")

    assert len(mock_api.requests) == 4
    assert isinstance(exc_info.value.__cause__, ingestion.RetryableFetchError)


# max_retries passed in replaces the default
async def test_client_max_retries_override() -> None:
    mock_api = MockApi(httpx.Response(503), httpx.Response(503))

    async with mock_api.client(max_retries=2) as client:
        with pytest.raises(ingestion.FailedFetchError):
            await client.get_requests_async("/scoreboard")

    assert len(mock_api.requests) == 2


# network errors with no response at all are retried like a 503
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("reset"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_client_retries_transport_error(error) -> None:
    mock_api = MockApi(error, httpx.Response(200, content=b"ok"))

    async with mock_api.client() as client:
        body = await client.get_requests_async("/scoreboard")

    assert body == b"ok"
    assert len(mock_api.requests) == 2


# the original httpx error stays attached so the real cause is visible
# FailedFetchError <- RetryableFetchError <- httpx.ReadTimeout
async def test_client_transport_error_keeps_cause() -> None:
    mock_api = MockApi(*[httpx.ReadTimeout("slow")] * 4)

    async with mock_api.client() as client:
        with pytest.raises(ingestion.FailedFetchError) as exc_info:
            await client.get_requests_async("/scoreboard")

    retryable_error = exc_info.value.__cause__
    assert isinstance(retryable_error, ingestion.RetryableFetchError)
    assert isinstance(retryable_error.__cause__, httpx.ReadTimeout)


# 502 on match endpoints means the match is not played yet, so no retry
# and NotAvailableYetError instead of a failure
@pytest.mark.parametrize(
    "path",
    [
        "/game/6480202/boxscore",
        "/game/6480202/play-by-play",
        "/game/6480202/team-stats",
    ],
)
async def test_client_no_retry_unplayed_match(path) -> None:
    mock_api = MockApi(httpx.Response(502))

    async with mock_api.client() as client:
        with pytest.raises(
            ingestion.NotAvailableYetError, match="not been played"
        ):
            await client.get_requests_async(path)

    assert len(mock_api.requests) == 1


# bad requests fail on the first try since retrying would not help
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 405, 410, 422])
async def test_client_no_retry_bad_request(status_code) -> None:
    mock_api = MockApi(httpx.Response(status_code))

    async with mock_api.client() as client:
        with pytest.raises(ingestion.FailedFetchError):
            await client.get_requests_async("/scoreboard")

    assert len(mock_api.requests) == 1


# codes not in either list also fail with no retry
@pytest.mark.parametrize("status_code", [204, 301, 418])
async def test_client_no_retry_unexpected_status(status_code) -> None:
    mock_api = MockApi(httpx.Response(status_code))

    async with mock_api.client() as client:
        with pytest.raises(ingestion.FailedFetchError):
            await client.get_requests_async("/scoreboard")

    assert len(mock_api.requests) == 1


# with no Retry-After, waits grow with exponential backoff
async def test_client_backoff_grows() -> None:
    mock_api = MockApi(*[httpx.Response(503)] * 4)

    async with mock_api.client() as client:
        with pytest.raises(ingestion.FailedFetchError):
            await client.get_requests_async("/scoreboard")

    # 2**n seconds plus up to 1 second of jitter, so check a range
    first_wait, second_wait, third_wait = mock_api.sleeps
    assert 1 <= first_wait <= 2
    assert 2 <= second_wait <= 3
    assert 4 <= third_wait <= 5


# Retry-After longer than the backoff is used as the wait time
async def test_client_honors_retry_after() -> None:
    mock_api = MockApi(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200),
    )

    async with mock_api.client() as client:
        await client.get_requests_async("/scoreboard")

    assert mock_api.sleeps == [7.0]


# Retry-After is capped at max_wait_secs so the host can't stall the run
async def test_client_caps_retry_after() -> None:
    mock_api = MockApi(
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(200),
    )

    async with mock_api.client(max_wait_secs=30) as client:
        await client.get_requests_async("/scoreboard")

    assert mock_api.sleeps == [30]


# Retry-After of 0, an http date, or junk falls back to the backoff
@pytest.mark.parametrize(
    "retry_after", ["0", "Wed, 21 Oct 2026 07:28:00 GMT", "soon"]
)
async def test_client_bad_retry_after_uses_backoff(retry_after) -> None:
    mock_api = MockApi(
        httpx.Response(503, headers={"Retry-After": retry_after}),
        httpx.Response(200),
    )

    async with mock_api.client() as client:
        await client.get_requests_async("/scoreboard")

    assert len(mock_api.sleeps) == 1
    assert 1 <= mock_api.sleeps[0] <= 2


# every attempt goes through the limiter, retries included, since the API
# counts each request against the rate limit
async def test_client_limits_every_attempt() -> None:
    mock_api = MockApi(
        httpx.Response(503), httpx.Response(503), httpx.Response(200)
    )
    limiter = CountingLimiter()

    async with mock_api.client() as client:
        client._limiter = limiter
        await client.get_requests_async("/scoreboard")

    assert limiter.entered == 3


# concurrent requests on one client all share the same limiter
async def test_client_shares_limiter_across_requests() -> None:
    mock_api = MockApi(*[httpx.Response(200)] * 5)
    limiter = CountingLimiter()

    async with mock_api.client() as client:
        client._limiter = limiter
        async with anyio.create_task_group() as task_group:
            for game_id in range(5):
                task_group.start_soon(
                    client.get_requests_async, f"/game/{game_id}/boxscore"
                )

    assert limiter.entered == 5


# connection pool is open inside async with and closed after
async def test_client_closes_on_exit() -> None:
    async with MockApi(httpx.Response(200)).client() as client:
        await client.get_requests_async("/scoreboard")
        assert not client._http.is_closed

    assert client._http.is_closed


# callers only ever see NotAvailableYetError or FailedFetchError, never the
# internal RetryableFetchError, whatever the failure
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.ConnectError("refused"),
        httpx.Response(404),
        httpx.Response(418),
    ],
    ids=["503", "429", "connect_error", "404", "418"],
)
async def test_client_never_raises_retryable_error(response) -> None:
    mock_api = MockApi(*[response] * 4)

    async with mock_api.client() as client:
        with pytest.raises(ingestion.FailedFetchError) as exc_info:
            await client.get_requests_async("/scoreboard")

    assert not isinstance(exc_info.value, ingestion.RetryableFetchError)


# connection pool is still closed when a request raises inside async with
async def test_client_closes_on_error() -> None:
    with pytest.raises(ingestion.FailedFetchError):
        async with MockApi(httpx.Response(404)).client() as client:
            await client.get_requests_async("/scoreboard")

    assert client._http.is_closed
