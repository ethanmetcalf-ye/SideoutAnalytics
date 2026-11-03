import hashlib
import os
import pathlib
import tempfile
import urllib.parse
from typing import Self

from ..settings import get_settings
from .client import Client


class RawStore:
    """
    On disk cache of raw API responses, one file per endpoint.
    Every response gets saved before it is parsed so the db can always be
    rebuilt from these files. A cached endpoint is never requested again
    unless the caller asks for a refresh. Use RawStore.from_settings() for
    real use.
    """

    def __init__(self, cache_directory: pathlib.Path) -> None:
        self.cache_directory = cache_directory

    @classmethod
    def from_settings(cls) -> Self:
        """
        builds the store under data_directory/raw from settings.
        """
        settings = get_settings()
        return cls(cache_directory=settings.data_directory / "raw")

    def map_endpoint_cache_file(self, endpoint: str) -> pathlib.Path:
        """
        maps an endpoint to the file it is cached in, ex.
        /game/123/boxscore -> game/123/boxscore.json and
        /stats/.../45?page=2 -> stats/.../45__page=2.json.
        Raises ValueError if the endpoint is empty or the file would land
        outside the cache directory, since ids parsed from responses end up
        in these paths.
        """
        endpoint = endpoint.strip("/")
        parsed_url = urllib.parse.urlsplit(endpoint)

        if parsed_url.path == "":
            raise ValueError(f"Endpoint {endpoint} cannot be empty...")
        path = pathlib.PurePosixPath(parsed_url.path)

        # query goes between the name and the suffix so the
        # extension stays last, e.g. 45__page=2.json. keys and values are
        # quoted back so a decoded / or .. can't point at another file
        args = [
            f"{urllib.parse.quote(key, safe='')}="
            f"{urllib.parse.quote(value, safe='')}"
            for key, value in sorted(
                urllib.parse.parse_qsl(
                    parsed_url.query, keep_blank_values=True
                )
            )
        ]
        query_part = "__" + "__".join(args) if args else ""
        suffix = path.suffix or ".json"
        normalized_cache_path = path.parent / (path.stem + query_part + suffix)

        root = self.cache_directory.resolve()
        candidate = (root / normalized_cache_path).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(
                f"Endpoint {endpoint} cannot be mapped to a cache file "
                f"at {normalized_cache_path}..."
            )

        return candidate

    def read_cached_file(self, cache_file: pathlib.Path) -> bytes | None:
        """
        returns the cached bytes, or None if nothing is cached yet.
        """
        if cache_file.is_file():
            return cache_file.read_bytes()
        return None

    def write_cache_to_disc(
        self, response_content: bytes, cache_file: pathlib.Path
    ) -> tuple[pathlib.Path, str]:
        """
        writes the response atomically so a crash never leaves a half
        written file, the old file stays until the new one is complete.
        Returns the file and its sha256 for the raw_response index.
        Raises OSError if the write fails, since a missing file means the db
        can't be rebuilt.
        """

        # nested endpoints like game/123/ need their folder first, and
        # the temp file sits next to the target so the replace is atomic
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=cache_file.parent, delete=False
        ) as temp_file:
            temp_path = pathlib.Path(temp_file.name)
            try:
                temp_file.write(response_content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path.replace(cache_file)
            except OSError:
                temp_path.unlink(missing_ok=True)
                raise
        return cache_file, hashlib.sha256(
            response_content, usedforsecurity=False
        ).hexdigest()

    async def get(
        self, client: Client, endpoint: str, refresh: bool = False
    ) -> bytes:
        """
        returns the response for an endpoint, from disk if it is cached and
        from the API otherwise. refresh=True skips the cache and overwrites
        it, used for responses that can still change (see ADR-003).
        NotAvailableYetError and FailedFetchError come straight from the
        client and nothing gets written, so the next run tries again.
        """
        cache_file = self.map_endpoint_cache_file(endpoint)
        if not refresh:
            cached_content = self.read_cached_file(cache_file)
            if cached_content is not None:
                return cached_content

        response_content = await client.get_bytes(endpoint)
        self.write_cache_to_disc(response_content, cache_file)
        return response_content
