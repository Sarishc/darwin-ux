"""Fixtures for integration tests against the local PostgreSQL 17 `darwin_test` database.

Run with `make test-integration` (needs `make db-start` and `make db-setup`).
The URL comes from DARWIN_TEST_DATABASE_URL — never DARWIN_DATABASE_URL — and
must pass the local-`_test` safety guard before anything touches it.
"""

import os
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from darwin.api.telemetry import get_queue
from darwin.config import Settings
from darwin.db.engine import create_db_engine, database_is_available
from darwin.db.safety import require_local_test_database
from darwin.main import create_app
from darwin.queue.postgres import PostgresQueue
from darwin.worker import run_once, telemetry_processor

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


SessionFactory = Callable[[], Session]


@pytest.fixture
def test_session_factory(connection: Connection) -> SessionFactory:
    """Sessions on the rolled-back `connection`; their commits become SAVEPOINTs."""
    return lambda: Session(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def test_queue(test_session_factory: SessionFactory) -> PostgresQueue:
    return PostgresQueue(test_session_factory, visibility_timeout=timedelta(seconds=30))


@pytest.fixture
def drain(test_queue: PostgresQueue, test_session_factory: SessionFactory) -> Callable[[], int]:
    """Run the real worker code until no message is visible. Returns messages handled."""
    processor = telemetry_processor(test_session_factory)

    def run_until_empty() -> int:
        handled = 0
        while run_once(test_queue, processor, max_attempts=5) is not None:
            handled += 1
            assert handled < 100_000, "drain did not terminate"
        return handled

    return run_until_empty


@pytest.fixture
def producer(integration_settings: Settings, test_queue: PostgresQueue) -> Iterator[TestClient]:
    """The real API (the producer) on the rolled-back connection. Nothing is processed."""
    app = create_app(integration_settings)
    app.dependency_overrides[get_queue] = lambda: test_queue
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api(producer: TestClient, drain: Callable[[], int]) -> TestClient:
    """The producer, plus a worker drain after every POST.

    For tests about what happens *once events are processed* (storage,
    idempotency, signals). Tests about asynchrony itself use `producer` and
    call `drain` explicitly.
    """

    def drain_after_post(response: Any) -> None:
        if response.request.method == "POST":
            drain()

    producer.event_hooks = {"request": [], "response": [drain_after_post]}
    return producer
