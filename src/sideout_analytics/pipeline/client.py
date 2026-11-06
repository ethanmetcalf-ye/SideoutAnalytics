from types import TracebackType
from typing import Self

import httpx
import tenacity
from aiolimiter import AsyncLimiter

from ..settings import get_settings

# endpoints that return 502 until the match has been played
MATCH_ENDPOINTS = ("/boxscore", "/play-by-play", "/team-stats")


class RetryableFetchError(Exception):
    """
    Exception class for request calls that fail but should be retried.
    Requires a message and an optional explicit delay in secs.
    retry_delay_secs comes from the Retry-After header when the host sends
    one. Internal to Client, callers never see it.
    """

    def __init__(self, message: str, retry_delay_secs: float | None = None) -> None:
        super().__init__(message)
        self.retry_delay_secs = retry_delay_secs


class FailedFetchError(Exception):
    """
    Exception class for request calls that fail and will not be retried.
    ex. bad requests, or retryable errors once all attempts are used up.
    """


class NotAvailableYetError(Exception):
    """
    Exception class for request calls that fail because the game has not
    been played yet.
    """


class Client:
    """
    Client class that is used to send requests with retries and rate
    limiting. Set up to handle requests asynchronously.
    Meant to be used as one instance inside an async with block, so every
    request shares the same connection pool and rate limit and both are
    closed at the end. Use Client.from_settings() for real use.
    """

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limit_per_sec: float = 3,
        max_attempts: int = 4,
        max_wait_secs: int = 60,
    ) -> None:
        """
        default values for each provided argument. rate limit matches the
        API's public limit of 3 requests per sec. max_attempts counts the
        first try, so 4 attempts is 3 retries. transport is None for the real
        network, tests pass a mock transport instead.
        """

        # sets up async client. 15 seconds max on any read/write/pool wait as a
        # starting point, since not sure initially how long some of the larger
        # reads will take. May revisit to adjust up or down in practice
        # Read timeout is the max gap between chunks, not the whole request
        # Connection given stricter timeout, a slow connect usually means the
        # host is down
        # Passing id in header so host has info if needed
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(15, connect=5),
            headers={"User-Agent": user_agent},
        )

        # sets retry params for only retryable errors. Waits with exponential
        # backoff, or the host's explicit wait time if longer (see wait)
        # Gives up after max_attempts and reraises the last error
        self.max_attempts = max_attempts
        self.retry_params = {
            "stop": tenacity.stop_after_attempt(max_attempts),
            "wait": self.wait,
            "retry": tenacity.retry_if_exception_type(RetryableFetchError),
            "reraise": True,
        }
        self.backoff = tenacity.wait_exponential_jitter(initial=1, max=10)
        self.max_wait_secs = max_wait_secs

        # Leaky-bucket throttling shared by every request on this client so
        # async calls stay under the API rate limit. Allows a short burst up to
        # rate_limit_per_sec, then spaces the rest out evenly
        self._limiter = AsyncLimiter(max_rate=rate_limit_per_sec, time_period=1)

        # Defines expected codes and maps to their behavior (retry vs not)
        # Any code not listed fails with no retry. 502 on match endpoints is
        # checked before this lookup since it means the match is not played yet
        self._codes = {
            "unavailable": [408, 429, 500, 502, 503, 504],
            "bad_requests": [400, 401, 403, 404, 405, 410, 422],
        }

    @classmethod
    def from_settings(cls, transport: httpx.AsyncBaseTransport | None = None, **kwargs: float) -> Self:
        """
        builds a client from settings for real use. reads the base url and
        user agent, any other options are passed through to Client. tests
        call Client directly with their own values instead.
        """
        settings = get_settings()
        return cls(
            base_url=settings.ncaa_base_api_url,
            user_agent=settings.useragent_id,
            transport=transport,
            **kwargs,
        )

    def wait(self, retry_state: tenacity.RetryCallState) -> float:
        """
        wait function that sets the retry wait time to a custom value if it
        is higher than the exponential backoff value while still under the
        max wait time. Custom value is the Retry-After header from the host,
        if given.
        """
        exc = retry_state.outcome.exception()
        wait_val = self.backoff(retry_state)
        if isinstance(exc, RetryableFetchError) and exc.retry_delay_secs is not None:
            wait_val = max(exc.retry_delay_secs, wait_val)
        return min(wait_val, self.max_wait_secs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """
        closes the client and its connection pool. Called on exit of async
        with.
        """
        await self._http.aclose()

    async def _get_once(self, path: str) -> bytes:
        """
        performs one request asynchronously and returns the raw response body
        if successful. otherwise, handles bad responses and raises the
        appropriate error to trigger a retry if needed. Waits on the rate
        limiter first so retries also count against the limit.
        """
        async with self._limiter:
            try:
                response = await self._http.get(path)
            except httpx.TransportError as e:
                raise RetryableFetchError(f"Get request to {path} failed with a network error. Retrying...") from e
        # Retry-After can be secs or an http date, only secs are used for now
        try:
            retry_secs = float(response.headers.get("Retry-After"))
        except (ValueError, TypeError):
            retry_secs = None

        if response.status_code == 200:
            return response.content
        elif response.status_code == 502 and path.endswith(MATCH_ENDPOINTS):
            raise NotAvailableYetError(
                f"Get request to {path} failed with status code "
                f"{response.status_code}. The match has not been played yet..."
            )
        elif response.status_code in self._codes["unavailable"]:
            raise RetryableFetchError(
                f"Get request to {path} failed with status code {response.status_code}. Retrying...",
                retry_delay_secs=retry_secs,
            )
        elif response.status_code in self._codes["bad_requests"]:
            raise FailedFetchError(
                f"Get request to {path} failed with status code {response.status_code}. No retry for bad requests..."
            )
        else:
            raise FailedFetchError(f"Get request to {path} failed with status code {response.status_code}.")

    async def get_bytes(self, path: str) -> bytes:
        """
        wraps the request function for async retries. Returns the raw response
        body so callers can cache it as is before parsing. Raises
        NotAvailableYetError for a match not played yet, or FailedFetchError
        once retries run out, with the last error kept as the cause.
        """
        try:
            async for attempt in tenacity.AsyncRetrying(**self.retry_params):
                with attempt:
                    return await self._get_once(path)
        except RetryableFetchError as e:
            raise FailedFetchError(f"Get request to {path} failed after {self.max_attempts} attempts...") from e
