import anyio
import httpx
import pytest

from sideout_analytics.pipeline import client as api_client

# runs every async test in this file on the anyio pytest plugin
pytestmark = pytest.mark.anyio

# values passed straight to Client. base url is fake since no request leaves
# the machine
TEST_BASE_URL = "https://api.test"
TEST_USER_AGENT = "sideout-analytics-tests/0.1"

# settings needed to build a client with from_settings. only the
# from_settings tests use these
SETTINGS_ENV = {
    "DATA_DIRECTORY": "./data",
    "DATABASE_URL": "postgresql://user:password@localhost:5432/testdb",
    "NCAA_BASE_API_URL": "https://settings.test",
}


class MockApi:
    """
    Mock of the NCAA API that replays scripted responses in order.
    Records each request and each retry sleep so tests can check them.
    """

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        """
        responses can be an httpx.Response or an exception to raise instead,
        ex. a timeout.
        """
        self.responses = list(responses)
        self.requests = []
        self.sleeps = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """
        handler for httpx.MockTransport. returns the next scripted response.
        """
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def sleep(self, seconds: float) -> None:
        """
        stands in for tenacity's sleep so retries record the wait time instead
        of actually waiting.
        """
        self.sleeps.append(seconds)

    def client(self, **kwargs: object) -> api_client.Client:
        """
        builds a Client wired to this mock. rate limit is set high by default
        so the limiter never slows a test down.
        """
        kwargs.setdefault("rate_limit_per_sec", 1000)
        client = api_client.Client(
            base_url=TEST_BASE_URL,
            user_agent=TEST_USER_AGENT,
            transport=httpx.MockTransport(self.handle),
            **kwargs,
        )
        client.retry_params["sleep"] = self.sleep
        return client


class CountingLimiter:
    """
    Stands in for AsyncLimiter and counts how often it is entered.
    Lets tests check which requests go through the limiter without waiting on
    real time.
    """

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *exc_info: object) -> None:
        pass


@pytest.fixture
def anyio_backend():
    """
    aiolimiter only supports asyncio, so skip the trio backend.
    """
    return "asyncio"


async def test_client_returns_raw_body_on_200() -> None:
    """
    200 returns the raw body on the first try with no retry waits.
    body is checked byte for byte since it is cached as is.
    """
    raw_body = b'{"games": [], "updated_at": "2026-11-01"}'
    mock_api = MockApi(httpx.Response(200, content=raw_body))

    async with mock_api.client() as client:
        body = await client.get_bytes("/scoreboard")

    assert body == raw_body
    assert len(mock_api.requests) == 1
    assert mock_api.sleeps == []


async def test_client_request_url_and_user_agent() -> None:
    """
    path is joined to the base url and the user agent passed in is sent.
    """
    mock_api = MockApi(httpx.Response(200))

    async with mock_api.client() as client:
        await client.get_bytes("/game/6480202/boxscore")

    request = mock_api.requests[0]
    assert str(request.url) == "https://api.test/game/6480202/boxscore"
    assert request.headers["User-Agent"] == TEST_USER_AGENT


async def test_client_timeouts() -> None:
    """
    timeouts match the values set in Client.
    """
    async with MockApi().client() as client:
        timeout = client._http.timeout

    assert timeout.connect == 5
    assert timeout.read == 15


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
async def test_client_retries_unavailable(status_code) -> None:
    """
    each retryable code fails twice then succeeds on the third attempt.
    """
    mock_api = MockApi(
        httpx.Response(status_code),
        httpx.Response(status_code),
        httpx.Response(200, content=b"ok"),
    )

    async with mock_api.client() as client:
        body = await client.get_bytes("/scoreboard")

    assert body == b"ok"
    assert len(mock_api.requests) == 3
    assert len(mock_api.sleeps) == 2


async def test_client_stops_after_max_attempts() -> None:
    """
    default of 4 attempts (3 retries), then raises FailedFetchError with the
    last retryable error kept as the cause.
    """
    mock_api = MockApi(*[httpx.Response(503)] * 4)

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError) as exc_info:
            await client.get_bytes("/scoreboard")

    assert len(mock_api.requests) == 4
    assert len(mock_api.sleeps) == 3
    assert isinstance(exc_info.value.__cause__, api_client.RetryableFetchError)


async def test_client_max_attempts_override() -> None:
    """
    max_attempts passed in replaces the default and counts the first try.
    """
    mock_api = MockApi(httpx.Response(503), httpx.Response(503))

    async with mock_api.client(max_attempts=2) as client:
        with pytest.raises(api_client.FailedFetchError, match="2 attempts"):
            await client.get_bytes("/scoreboard")

    assert len(mock_api.requests) == 2


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
    """
    network errors with no response at all are retried like a 503.
    """
    mock_api = MockApi(error, httpx.Response(200, content=b"ok"))

    async with mock_api.client() as client:
        body = await client.get_bytes("/scoreboard")

    assert body == b"ok"
    assert len(mock_api.requests) == 2


async def test_client_transport_error_keeps_cause() -> None:
    """
    the original httpx error stays attached so the real cause is visible.
    FailedFetchError <- RetryableFetchError <- httpx.ReadTimeout
    """
    mock_api = MockApi(*[httpx.ReadTimeout("slow")] * 4)

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError) as exc_info:
            await client.get_bytes("/scoreboard")

    retryable_error = exc_info.value.__cause__
    assert isinstance(retryable_error, api_client.RetryableFetchError)
    assert isinstance(retryable_error.__cause__, httpx.ReadTimeout)


@pytest.mark.parametrize(
    "path",
    [
        "/game/6480202/boxscore",
        "/game/6480202/play-by-play",
        "/game/6480202/team-stats",
    ],
)
async def test_client_no_retry_unplayed_match(path) -> None:
    """
    502 on match endpoints means the match is not played yet, so no retry
    and NotAvailableYetError instead of a failure.
    """
    mock_api = MockApi(httpx.Response(502))

    async with mock_api.client() as client:
        with pytest.raises(api_client.NotAvailableYetError, match="not been played"):
            await client.get_bytes(path)

    assert len(mock_api.requests) == 1


async def test_client_502_match_rule_needs_endpoint_suffix() -> None:
    """
    the 502 rule only applies when the path ends with a match endpoint, so a
    path that merely contains the word is retried like any other 502.
    """
    mock_api = MockApi(httpx.Response(502), httpx.Response(200, content=b"ok"))

    async with mock_api.client() as client:
        body = await client.get_bytes("/boxscore-archive/2025")

    assert body == b"ok"
    assert len(mock_api.requests) == 2


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 405, 410, 422])
async def test_client_no_retry_bad_request(status_code) -> None:
    """
    bad requests fail on the first try since retrying would not help.
    """
    mock_api = MockApi(httpx.Response(status_code))

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError):
            await client.get_bytes("/scoreboard")

    assert len(mock_api.requests) == 1


@pytest.mark.parametrize("status_code", [204, 301, 418])
async def test_client_no_retry_unexpected_status(status_code) -> None:
    """
    codes not in either list also fail with no retry.
    """
    mock_api = MockApi(httpx.Response(status_code))

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError):
            await client.get_bytes("/scoreboard")

    assert len(mock_api.requests) == 1


async def test_client_error_messages_are_readable() -> None:
    """
    joined message strings keep their spaces, ex. "status code 404" and not
    "status code404", since these end up in logs.
    """
    mock_api = MockApi(httpx.Response(404))

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError) as exc_info:
            await client.get_bytes("/scoreboard")

    assert "status code 404. " in str(exc_info.value)


async def test_client_backoff_grows() -> None:
    """
    with no Retry-After, waits grow with exponential backoff.
    """
    mock_api = MockApi(*[httpx.Response(503)] * 4)

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError):
            await client.get_bytes("/scoreboard")

    # 2**n seconds plus up to 1 second of jitter, so check a range
    first_wait, second_wait, third_wait = mock_api.sleeps
    assert 1 <= first_wait <= 2
    assert 2 <= second_wait <= 3
    assert 4 <= third_wait <= 5


async def test_client_honors_retry_after() -> None:
    """
    Retry-After longer than the backoff is used as the wait time.
    """
    mock_api = MockApi(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200),
    )

    async with mock_api.client() as client:
        await client.get_bytes("/scoreboard")

    assert mock_api.sleeps == [7.0]


async def test_client_caps_retry_after() -> None:
    """
    Retry-After is capped at max_wait_secs so the host can't stall the run.
    """
    mock_api = MockApi(
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(200),
    )

    async with mock_api.client(max_wait_secs=30) as client:
        await client.get_bytes("/scoreboard")

    assert mock_api.sleeps == [30]


@pytest.mark.parametrize("retry_after", ["0", "Wed, 21 Oct 2026 07:28:00 GMT", "soon"])
async def test_client_bad_retry_after_uses_backoff(retry_after) -> None:
    """
    Retry-After of 0, an http date, or junk falls back to the backoff.
    """
    mock_api = MockApi(
        httpx.Response(503, headers={"Retry-After": retry_after}),
        httpx.Response(200),
    )

    async with mock_api.client() as client:
        await client.get_bytes("/scoreboard")

    assert len(mock_api.sleeps) == 1
    assert 1 <= mock_api.sleeps[0] <= 2


async def test_client_limits_every_attempt() -> None:
    """
    every attempt goes through the limiter, retries included, since the API
    counts each request against the rate limit.
    """
    mock_api = MockApi(httpx.Response(503), httpx.Response(503), httpx.Response(200))
    limiter = CountingLimiter()

    async with mock_api.client() as client:
        client._limiter = limiter
        await client.get_bytes("/scoreboard")

    assert limiter.entered == 3


async def test_client_shares_limiter_across_requests() -> None:
    """
    concurrent requests on one client all share the same limiter.
    """
    mock_api = MockApi(*[httpx.Response(200)] * 5)
    limiter = CountingLimiter()

    async with mock_api.client() as client:
        client._limiter = limiter
        async with anyio.create_task_group() as task_group:
            for game_id in range(5):
                task_group.start_soon(client.get_bytes, f"/game/{game_id}/boxscore")

    assert limiter.entered == 5


async def test_client_closes_on_exit() -> None:
    """
    connection pool is open inside async with and closed after.
    """
    async with MockApi(httpx.Response(200)).client() as client:
        await client.get_bytes("/scoreboard")
        assert not client._http.is_closed

    assert client._http.is_closed


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
    """
    callers only ever see NotAvailableYetError or FailedFetchError, never the
    internal RetryableFetchError, whatever the failure.
    """
    mock_api = MockApi(*[response] * 4)

    async with mock_api.client() as client:
        with pytest.raises(api_client.FailedFetchError) as exc_info:
            await client.get_bytes("/scoreboard")

    assert not isinstance(exc_info.value, api_client.RetryableFetchError)


async def test_client_closes_on_error() -> None:
    """
    connection pool is still closed when a request raises inside async with.
    """
    with pytest.raises(api_client.FailedFetchError):
        async with MockApi(httpx.Response(404)).client() as client:
            await client.get_bytes("/scoreboard")

    assert client._http.is_closed


async def test_client_from_settings(monkeypatch, isolated_env) -> None:
    """
    from_settings reads the base url and user agent from settings and still
    uses the transport passed in, so no request reaches the real API.
    """
    for name, value in SETTINGS_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("USERAGENT_ID", "from-settings-agent")
    mock_api = MockApi(httpx.Response(200, content=b"ok"))

    async with api_client.Client.from_settings(transport=httpx.MockTransport(mock_api.handle)) as client:
        body = await client.get_bytes("/scoreboard")

    request = mock_api.requests[0]
    assert body == b"ok"
    assert str(request.url) == "https://settings.test/scoreboard"
    assert request.headers["User-Agent"] == "from-settings-agent"


async def test_client_from_settings_default_user_agent(monkeypatch, isolated_env) -> None:
    """
    with no USERAGENT_ID set, the default from settings identifies this repo.
    """
    for name, value in SETTINGS_ENV.items():
        monkeypatch.setenv(name, value)
    mock_api = MockApi(httpx.Response(200))

    async with api_client.Client.from_settings(transport=httpx.MockTransport(mock_api.handle)) as client:
        await client.get_bytes("/scoreboard")

    user_agent = mock_api.requests[0].headers["User-Agent"]
    assert user_agent.startswith("sideout-analytics/")


async def test_client_from_settings_passes_options(monkeypatch, isolated_env) -> None:
    """
    other options passed to from_settings reach the client, ex. max_attempts.
    """
    for name, value in SETTINGS_ENV.items():
        monkeypatch.setenv(name, value)
    mock_api = MockApi(httpx.Response(503), httpx.Response(503))

    async with api_client.Client.from_settings(
        transport=httpx.MockTransport(mock_api.handle),
        rate_limit_per_sec=1000,
        max_attempts=2,
    ) as client:
        client.retry_params["sleep"] = mock_api.sleep
        with pytest.raises(api_client.FailedFetchError):
            await client.get_bytes("/scoreboard")

    assert len(mock_api.requests) == 2
