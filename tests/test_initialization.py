from pathlib import Path

import pytest
from pydantic import ValidationError

from sideout_analytics.settings import settings

REQUIRED_ENV = {
    "DATA_DIRECTORY": "./data",
    "DATABASE_URL": "postgresql://user:password@localhost:5432/testdb",
}


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Run from an empty directory with no required settings set."""
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    settings.get_settings.cache_clear()
    yield tmp_path
    settings.get_settings.cache_clear()


def test_package_imports_and_exposes_entry_point() -> None:
    from sideout_analytics import main

    assert callable(main)


def test_settings_defaults(monkeypatch, isolated_env) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)

    loaded_settings = settings.get_settings()
    assert loaded_settings.ncaa_base_api_url == "https://ncaa-api.henrygd.me"
    assert loaded_settings.log_level == "INFO"


@pytest.mark.parametrize("missing", list(REQUIRED_ENV))
def test_settings_missing_required_field(
    monkeypatch, isolated_env, missing
) -> None:
    for name, value in REQUIRED_ENV.items():
        if name != missing:
            monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError) as exc_info:
        settings.get_settings()

    errors = exc_info.value.errors()
    assert [error["loc"] for error in errors] == [(missing.lower(),)]
    assert errors[0]["type"] == "missing"


def test_settings_loaded_from_env_file(isolated_env) -> None:
    (isolated_env / ".env").write_text(
        "DATA_DIRECTORY=./from-file\n"
        "DATABASE_URL=postgresql://file:file@db:5432/filedb\n"
        "LOG_LEVEL=DEBUG\n"
    )

    loaded_settings = settings.get_settings()
    assert loaded_settings.data_directory == Path("./from-file")
    assert (
        loaded_settings.database_url == "postgresql://file:file@db:5432/filedb"
    )
    assert loaded_settings.log_level == "DEBUG"


def test_settings_data_directory_is_path(monkeypatch, isolated_env) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)

    loaded_settings = settings.get_settings()
    assert isinstance(loaded_settings.data_directory, Path)
    assert loaded_settings.data_directory == Path("./data")


def test_settings_env_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path / "data"))
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:password@localhost:5432/testdb"
    )
    settings.get_settings.cache_clear()

    loaded_settings = settings.get_settings()
    assert loaded_settings.data_directory == tmp_path / "data"
    assert (
        loaded_settings.database_url
        == "postgresql://user:password@localhost:5432/testdb"
    )
