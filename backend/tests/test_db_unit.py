"""Database-layer behaviour that can be verified without PostgreSQL."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from darwin.db.engine import create_db_engine
from darwin.db.models import UserEvent
from darwin.db.safety import UnsafeDatabaseError, require_local_test_database
from darwin.db.session import DbSession

# ---- UserEvent -------------------------------------------------------------


def _event(occurred_at: datetime) -> UserEvent:
    return UserEvent(
        event_id=uuid.uuid4(),
        event_type="click",
        session_id=uuid.uuid4(),
        occurred_at=occurred_at,
    )


def test_user_event_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _event(datetime(2026, 9, 26, 12, 0))


def test_user_event_accepts_timezone_aware_timestamps() -> None:
    occurred_at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    assert _event(occurred_at).occurred_at == occurred_at


# ---- Request-scoped sessions -------------------------------------------------


def test_each_request_gets_its_own_session_which_is_closed_afterwards(app: FastAPI) -> None:
    seen: list[tuple[Session, UserEvent]] = []

    @app.get("/_test/session")
    def use_session(session: DbSession) -> None:
        pending = _event(datetime.now(UTC))
        session.add(pending)  # no flush, so no database connection is needed
        seen.append((session, pending))

    with TestClient(app) as client:
        client.get("/_test/session")
        client.get("/_test/session")

    (first, first_obj), (second, second_obj) = seen
    assert first is not second
    assert first.get_bind() is app.state.engine
    # Closing a session discards its pending objects; nothing leaks between requests.
    assert first_obj not in first
    assert second_obj not in second


# ---- Engine configuration ---------------------------------------------------


def test_engine_keeps_bound_values_out_of_error_messages() -> None:
    # Telemetry payloads are bound parameters; they must never appear in
    # database exception text or tracebacks.
    engine = create_db_engine("postgresql+psycopg://darwin@127.0.0.1:1/darwin_unit_test")

    assert engine.hide_parameters is True


# ---- Destructive-operation guard ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://darwin@localhost:5432/darwin_test",
        "postgresql+psycopg://darwin@127.0.0.1:5432/darwin_test",
    ],
)
def test_guard_allows_local_test_databases(url: str) -> None:
    require_local_test_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://darwin@localhost:5432/darwin_dev",  # not a _test database
        "postgresql+psycopg://darwin@db.example.com:5432/darwin_test",  # not local
        "postgresql+psycopg://darwin@prod.abc123.us-east-1.rds.amazonaws.com/darwin",
    ],
)
def test_guard_refuses_everything_else(url: str) -> None:
    with pytest.raises(UnsafeDatabaseError):
        require_local_test_database(url)
