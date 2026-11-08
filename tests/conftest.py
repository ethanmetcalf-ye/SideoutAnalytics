import os
import uuid

import pytest
import sqlalchemy

from sideout_analytics import settings
from sideout_analytics.db.loader import connect
from sideout_analytics.db.tables import Base


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Run from an empty directory with no settings in the environment."""
    for field in settings.Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    settings.get_settings.cache_clear()
    yield tmp_path
    settings.get_settings.cache_clear()


@pytest.fixture(scope="session")
def database_schema():
    """
    a throwaway schema built from tables.py, on the Postgres in
    TEST_DATABASE_URL. Tests that need a database skip when it isn't set,
    and the schema is dropped afterwards, so it can point at the dev
    database without touching its tables.
    """
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL isn't set")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    engine = sqlalchemy.create_engine(database_url, connect_args={"options": f"-c search_path={schema}"})
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text(f"CREATE SCHEMA {schema}"))
        Base.metadata.create_all(connection)
    yield database_url, schema
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text(f"DROP SCHEMA {schema} CASCADE"))
    engine.dispose()


@pytest.fixture
def database(database_schema):
    """
    a loader connection to the test schema, with every table emptied first.
    """
    database_url, schema = database_schema
    connection = connect(database_url)
    connection.execute(f"SET search_path TO {schema}")
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    connection.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    yield connection
    connection.close()
