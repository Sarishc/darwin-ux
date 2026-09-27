"""POST /api/v1/telemetry/events against the real darwin_test database.

Isolation: every request gets its own Session on one shared connection whose
outer transaction is rolled back after the test. The service's own
`session.begin()` / commit therefore becomes a SAVEPOINT — the real service
code runs unchanged, and nothing is left behind.
"""

import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, func, insert, select
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.models import UserEvent
from darwin.db.session import get_session
from darwin.logging_config import ROOT_LOGGER_NAME
from darwin.main import create_app
from darwin.telemetry.schemas import IngestionResult, TelemetryEvent
from darwin.telemetry.service import ingest_event

pytestmark = pytest.mark.integration

URL = "/api/v1/telemetry/events"
MARKER = "PAYLOAD-MARKER-91c2"


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "button_click",
        "session_id": str(uuid.uuid4()),
        "occurred_at": "2026-09-26T17:00:00Z",
        "payload": {"component": "signup_submit"},
    }
    body.update(overrides)
    return body


def _rows(connection: Connection, event_id: str) -> list[UserEvent]:
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        return list(
            session.scalars(select(UserEvent).where(UserEvent.event_id == uuid.UUID(event_id)))
        )


@pytest.fixture
def api(integration_settings: Settings, connection: Connection) -> Iterator[TestClient]:
    app = create_app(integration_settings)

    def session_on_test_connection() -> Iterator[Session]:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session

    app.dependency_overrides[get_session] = session_on_test_connection
    with TestClient(app) as client:
        yield client


# ---- First delivery -------------------------------------------------------------


def test_valid_event_is_accepted_and_stored_once(api: TestClient, connection: Connection) -> None:
    body = _body(payload={"component": "signup_submit", "x": 10})

    response = api.post(URL, json=body)

    assert response.status_code == 202
    assert response.json() == {"event_id": body["event_id"], "status": "accepted"}
    [row] = _rows(connection, body["event_id"])
    assert str(row.session_id) == body["session_id"]
    assert row.event_type == "button_click"
    assert row.occurred_at == datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
    assert row.payload == {"component": "signup_submit", "x": 10}


def test_response_exposes_no_internal_identifiers(api: TestClient) -> None:
    response = api.post(URL, json=_body())

    assert set(response.json()) == {"event_id", "status"}


def test_received_at_is_set_by_the_database(api: TestClient, connection: Connection) -> None:
    body = _body(occurred_at="2020-01-01T00:00:00Z")  # a stale client clock

    api.post(URL, json=body)

    [row] = _rows(connection, body["event_id"])
    # now() is fixed for the test's transaction, so it equals the stored value exactly.
    assert row.received_at == connection.scalar(select(func.now()))
    assert row.received_at != row.occurred_at


# ---- Idempotency ------------------------------------------------------------------


def test_same_event_twice_is_one_row_and_two_successes(
    api: TestClient, connection: Connection
) -> None:
    body = _body()

    first = api.post(URL, json=body)
    second = api.post(URL, json=body)

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["status"] == "accepted"
    assert second.json() == {"event_id": body["event_id"], "status": "duplicate"}
    assert len(_rows(connection, body["event_id"])) == 1


def test_first_delivery_wins_duplicates_never_overwrite(
    api: TestClient, connection: Connection
) -> None:
    body = _body(payload={"version": "first"})
    api.post(URL, json=body)

    replay = api.post(URL, json={**body, "payload": {"version": "second"}, "event_type": "other"})

    assert replay.json()["status"] == "duplicate"
    [row] = _rows(connection, body["event_id"])
    assert row.payload == {"version": "first"}
    assert row.event_type == "button_click"


def test_one_session_can_record_many_events(api: TestClient, connection: Connection) -> None:
    session_id = str(uuid.uuid4())
    bodies = [_body(session_id=session_id, event_type=t) for t in ("page_view", "button_click")]

    statuses = [api.post(URL, json=body).json()["status"] for body in bodies]

    assert statuses == ["accepted", "accepted"]
    stored = connection.scalar(
        select(func.count())
        .select_from(UserEvent)
        .where(UserEvent.session_id == uuid.UUID(session_id))
    )
    assert stored == 2


def test_a_different_event_id_is_a_new_row(api: TestClient, connection: Connection) -> None:
    first, second = _body(), _body()

    api.post(URL, json=first)
    response = api.post(URL, json={**first, "event_id": second["event_id"]})

    assert response.json()["status"] == "accepted"
    assert len(_rows(connection, first["event_id"])) == 1
    assert len(_rows(connection, second["event_id"])) == 1


# ---- Invalid input never reaches the database -------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"payload": [1, 2, 3]},
        {"payload": {"k": "x" * 9000}},
        {"occurred_at": "2026-09-26T17:00:00"},  # naive
        {"occurred_at": "yesterday"},
        {"received_at": "2026-09-26T17:00:00Z"},
    ],
)
def test_invalid_event_is_rejected_and_not_stored(
    api: TestClient, connection: Connection, overrides: dict[str, Any]
) -> None:
    body = _body(**overrides)

    response = api.post(URL, json=body)

    assert response.status_code == 422
    assert _rows(connection, body["event_id"]) == []


# ---- Logging ------------------------------------------------------------------------


def test_ingestion_log_has_the_outcome_but_no_payload_or_session(
    api: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    body = _body(payload={"note": MARKER})
    caplog.set_level(logging.INFO, logger=ROOT_LOGGER_NAME)  # restored after the test
    logger = logging.getLogger(ROOT_LOGGER_NAME)  # does not propagate; attach directly
    logger.addHandler(caplog.handler)
    try:
        api.post(URL, json=body)
        api.post(URL, json=body)
    finally:
        logger.removeHandler(caplog.handler)

    records = [r for r in caplog.records if r.getMessage() == "telemetry event ingested"]
    contexts = [getattr(r, "context", {}) for r in records]
    assert [c["status"] for c in contexts] == ["accepted", "duplicate"]
    assert all(c["event_id"] == body["event_id"] for c in contexts)
    logged = repr(contexts) + caplog.text
    assert MARKER not in logged
    assert body["session_id"] not in logged


# ---- Concurrency: the UNIQUE constraint is the final guard ---------------------------


def test_concurrent_deliveries_of_one_event_store_exactly_one_row(
    migrated_engine: Engine,
) -> None:
    """Two transactions race on one event_id; real commits, separate connections.

    Transaction A inserts but has not committed yet. Delivery B (the real
    service) starts meanwhile: PostgreSQL makes B's INSERT ... ON CONFLICT
    wait on A's uncommitted row. When A commits, B's insert becomes a no-op.
    A check-then-insert implementation would have seen "no row" and inserted.
    """
    event = TelemetryEvent.model_validate(_body())
    results: list[IngestionResult] = []

    try:
        with Session(migrated_engine) as session_a:
            session_a.execute(
                insert(UserEvent).values(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    session_id=event.session_id,
                    occurred_at=event.occurred_at,
                )
            )  # A holds the row lock, uncommitted

            def deliver_b() -> None:
                with Session(migrated_engine) as session_b:
                    results.append(ingest_event(session_b, event))

            b = threading.Thread(target=deliver_b)
            b.start()
            b.join(timeout=0.5)
            assert b.is_alive(), "B should be waiting for A's uncommitted insert"

            session_a.commit()
            b.join(timeout=5)
            assert not b.is_alive()

        assert [r.status for r in results] == ["duplicate"]
        with migrated_engine.connect() as check:
            count = check.scalar(
                select(func.count())
                .select_from(UserEvent)
                .where(UserEvent.event_id == event.event_id)
            )
        assert count == 1
    finally:
        # This test really committed; remove exactly its own row from darwin_test.
        with migrated_engine.begin() as cleanup:
            cleanup.execute(delete(UserEvent).where(UserEvent.event_id == event.event_id))
