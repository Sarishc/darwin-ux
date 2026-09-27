from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from darwin.config import Settings
from darwin.main import create_app

# Nothing listens on port 1: connections are refused immediately. Unit tests use
# this so they never depend on (or touch) a real database.
UNREACHABLE_DATABASE_URL = "postgresql+psycopg://darwin@127.0.0.1:1/darwin_unit_test"


@pytest.fixture
def settings() -> Settings:
    # Explicit values, so tests never depend on the developer's shell or .env.
    return Settings(
        app_name="DarwinUX Test",
        env="test",
        log_level="WARNING",
        database_url=UNREACHABLE_DATABASE_URL,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the lifespan (startup/shutdown),
    # exactly as Uvicorn would.
    with TestClient(app) as test_client:
        yield test_client
