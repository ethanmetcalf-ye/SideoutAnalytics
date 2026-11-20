import typing
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Settings for the Sideout Analytics package.
    """

    data_directory: Path
    database_url: str
    ncaa_base_api_url: str = "https://ncaa-api.henrygd.me"
    log_level: typing.Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    useragent_id: str = "sideout-analytics/0.1 (+https://github.com/ethanmetcalf-ye/SideoutAnalytics)"
    # Sentry stays off when a DSN is unset, so local runs and tests send nothing.
    # The api and the pipeline jobs are separate Sentry projects, so each has its own DSN.
    sentry_api_dsn: str | None = None
    sentry_pipeline_dsn: str | None = None
    # "production" on the server; names the environment on Sentry errors and switches logs to JSON.
    environment: str = "development"
    # /health/freshness reports the data as stale when no poll has succeeded for this long.
    # The poll runs hourly, so the default allows two missed runs.
    freshness_stale_after_minutes: int = 120

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # .env is shared with docker compose (DB_USER, etc.)
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
