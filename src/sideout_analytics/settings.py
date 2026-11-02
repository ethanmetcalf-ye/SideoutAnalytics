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
    useragent_id: str = (
        "sideout-analytics/0.1 "
        "(+https://github.com/ethanmetcalf-ye/SideoutAnalytics)"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # .env is shared with docker compose (DB_USER, etc.)
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
