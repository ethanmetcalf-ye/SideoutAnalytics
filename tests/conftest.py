import pytest

from sideout_analytics import settings


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Run from an empty directory with no settings in the environment."""
    for field in settings.Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    settings.get_settings.cache_clear()
    yield tmp_path
    settings.get_settings.cache_clear()
