"""Fixtures for integration tests against the local PostgreSQL 17 `darwin_test` database.

Run with `make test-integration` (needs `make db-start` and `make db-setup`).
The URL comes from DARWIN_TEST_DATABASE_URL — never DARWIN_DATABASE_URL — and
must pass the local-`_test` safety guard before anything touches it.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine, database_is_available
from darwin.db.safety import require_local_test_database
from darwin.db.session import get_session
from darwin.main import create_app

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://darwin@localhost:5432/darwin_test"
BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logging"] = False
    return config


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get("DARWIN_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    require_local_test_database(url)  # raises before any connection is made
    return url


@pytest.fixture(scope="session")
def alembic_cfg(test_database_url: str) -> Config:
    return alembic_config(test_database_url)


@pytest.fixture(scope="session")
def migrated_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_db_engine(test_database_url)
    if not database_is_available(engine):
        pytest.fail(
            "darwin_test is not reachable. Run `make db-start` and `make db-setup`.",
            pytrace=False,
        )
    command.upgrade(alembic_config(test_database_url), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """A Session whose work is always rolled back, so tests never see each other's rows.

    The outer transaction is never committed; `session.commit()` inside a test
    only releases a SAVEPOINT.
    """
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def integration_settings(test_database_url: str) -> Settings:
    return Settings(env="test", log_level="WARNING", database_url=test_database_url)


@pytest.fixture
def connection(migrated_engine: Engine) -> Iterator[Connection]:
    """One connection inside a transaction that is always rolled back."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def api(integration_settings: Settings, connection: Connection) -> Iterator[TestClient]:
    """The real app, with every request's Session bound to the rolled-back `connection`.

    The services' own `session.begin()` / commit become SAVEPOINTs, so the
    production code runs unchanged and nothing is left in darwin_test.
    """
    app = create_app(integration_settings)

    def session_on_test_connection() -> Iterator[Session]:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session

    app.dependency_overrides[get_session] = session_on_test_connection
    with TestClient(app) as client:
        yield client
