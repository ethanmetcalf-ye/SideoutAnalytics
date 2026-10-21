from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Settings for the Sideout Analytics package.
    """

    data_directory: str
    database_url: str
    ncaa_base_api_url: str = "https://api.ncaa.com/v1/sports/volleyball-women"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8"
    )


settings = Settings()
print("Settings loaded successfully")
