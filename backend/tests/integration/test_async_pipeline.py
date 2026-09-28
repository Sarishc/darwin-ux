"""HTTP producer -> durable queue -> worker -> UserEvent -> BehaviorSignal (darwin_test).

Uses `producer` (the API only; nothing is processed) and `drain` (the real
worker code) explicitly, so the asynchrony is visible in each test.
"""

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, select, text
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, QueueMessage, UserEvent
from darwin.queue.base import OutgoingMessage
from darwin.queue.postgres import PostgresQueue
from darwin.signals.detectors import detect_all
from darwin.telemetry import service
from darwin.telemetry.messages import TELEMETRY_EVENT, TelemetryMessageV1, parse_message
from darwin.telemetry.schemas import TelemetryEvent
from darwin.worker import Outcome, handle, run_once, telemetry_processor

pytestmark = pytest.mark.integration

URL = "/api/v1/telemetry/events"
T0 = datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)
SessionFactory = Callable[[], Session]


def _event(session_id: str, seconds: float, event_type: str = "button_click") -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "session_id": session_id,
        "occurred_at": (T0 + timedelta(seconds=seconds)).isoformat(),
        "payload": {"component": "signup_submit"} if event_type != "client_error" else {},
    }


def _count(connection: Connection, model: Any, **where: Any) -> int:
    statement = select(func.count()).select_from(model)
    for column, value in where.items():
        statement = statement.where(getattr(model, column) == value)
    return int(connection.scalar(statement) or 0)


def _status(connection: Connection, message_id: str) -> str:
    return str(
        connection.scalar(
            select(QueueMessage.status).where(QueueMessage.message_id == uuid.UUID(message_id))
        )
    )


def _canonical_and_replay(connection: Connection, session_id: str) -> tuple[set[Any], set[Any]]:
    sid = uuid.UUID(session_id)
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        canonical = set(
            session.scalars(
                select(BehaviorSignal.signal_id).where(
                    BehaviorSignal.session_id == sid, BehaviorSignal.superseded_at.is_(None)
                )
            )
        )
        replay = {
            c.signal_id
            for c in detect_all(
                session.scalars(select(UserEvent).where(UserEvent.session_id == sid)).all()
            )
        }
    return canonical, replay


def _expire_leases(connection: Connection) -> None:
    connection.execute(
        text(
            "UPDATE queue_message SET visible_at = now() - interval '1 second' "
            "WHERE status = 'pending'"
        )
    )


RAPID = [0.0, 0.4, 0.8, 1.2]


# ---- The producer only queues -----------------------------------------------------------


def test_post_queues_durably_and_processes_nothing(
    producer: TestClient, connection: Connection
) -> None:
    body = _event(str(uuid.uuid4()), 0.0)

    response = producer.post(URL, json=body)

    assert response.status_code == 202
    assert response.json() == {"event_id": body["event_id"], "status": "accepted"}
    assert _status(connection, body["event_id"]) == "pending"
    assert _count(connection, UserEvent, event_id=uuid.UUID(body["event_id"])) == 0
    assert _count(connection, BehaviorSignal, session_id=uuid.UUID(body["session_id"])) == 0


def test_queued_body_is_the_v2_message_without_server_fields(
    producer: TestClient, connection: Connection
) -> None:
    body = _event(str(uuid.uuid4()), 0.0)
    producer.post(URL, json=body)

    stored = connection.scalar(
        select(QueueMessage.body).where(QueueMessage.message_id == uuid.UUID(body["event_id"]))
    )

    assert stored is not None
    assert stored["schema_version"] == 2  # Step 15: v2 adds the optional UI attribution
    assert {"id", "received_at"}.isdisjoint(stored)
    assert stored["ui_spec_version_id"] is None  # a claim, verified only by the worker


# ---- The worker processes -----------------------------------------------------------


def test_worker_stores_the_event_and_acks(
    producer: TestClient, drain: Callable[[], int], connection: Connection
) -> None:
    body = _event(str(uuid.uuid4()), 0.0)
    producer.post(URL, json=body)

    assert drain() == 1

    assert _count(connection, UserEvent, event_id=uuid.UUID(body["event_id"])) == 1
    assert _status(connection, body["event_id"]) == "done"


def test_rage_clicks_become_a_signal_only_after_the_worker_runs(
    producer: TestClient, drain: Callable[[], int], connection: Connection
) -> None:
    session_id = str(uuid.uuid4())
    statuses = [producer.post(URL, json=_event(session_id, t)).status_code for t in RAPID]

    assert statuses == [202] * 4
    assert _count(connection, BehaviorSignal, session_id=uuid.UUID(session_id)) == 0  # not yet

    drain()

    canonical, replay = _canonical_and_replay(connection, session_id)
    assert canonical == replay and len(canonical) == 1


def test_duplicate_submissions_create_one_message_and_one_event(
    producer: TestClient, drain: Callable[[], int], connection: Connection
) -> None:
    body = _event(str(uuid.uuid4()), 0.0)

    first = producer.post(URL, json=body).json()["status"]
    second = producer.post(URL, json=body).json()["status"]
    drain()
    after_processing = producer.post(URL, json=body).json()["status"]

    assert (first, second, after_processing) == ("accepted", "duplicate", "duplicate")
    assert _count(connection, QueueMessage, message_id=uuid.UUID(body["event_id"])) == 1
    assert _count(connection, UserEvent, event_id=uuid.UUID(body["event_id"])) == 1
    assert drain() == 0  # nothing new was queued


@pytest.mark.parametrize("seed", range(4))
def test_out_of_order_processing_still_converges_to_replay(
    producer: TestClient, drain: Callable[[], int], connection: Connection, seed: int
) -> None:
    session_id = str(uuid.uuid4())
    times = [9.6, 10.0, 10.2, 10.5, 11.0, 11.5, 30.0, 30.3, 30.6, 30.9]
    bodies = [_event(session_id, t) for t in times] + [
        _event(session_id, 100.0 + i, "client_error") for i in range(3)
    ]
    random.Random(seed).shuffle(bodies)

    for body in bodies:
        producer.post(URL, json=body)
    drain()
    for body in bodies:  # a duplicate wave, delivered again after processing
        producer.post(URL, json=body)
    drain()

    canonical, replay = _canonical_and_replay(connection, session_id)
    assert canonical == replay
    assert _count(connection, UserEvent, session_id=uuid.UUID(session_id)) == len(bodies)


# ---- Redelivery and crash recovery --------------------------------------------------------


def test_redelivered_message_creates_one_event(
    test_queue: PostgresQueue, test_session_factory: SessionFactory, connection: Connection
) -> None:
    body = _event(str(uuid.uuid4()), 0.0)
    producer_body = TelemetryMessageV1.from_event(TelemetryEvent.model_validate(body)).to_body()
    test_queue.enqueue(OutgoingMessage(uuid.UUID(body["event_id"]), TELEMETRY_EVENT, producer_body))
    processor = telemetry_processor(test_session_factory)

    first = test_queue.receive()
    assert first is not None
    processor(first.message_type, first.body)  # processed, but the ACK is lost
    _expire_leases(connection)

    assert run_once(test_queue, processor, max_attempts=5) is Outcome.ACKED

    assert _count(connection, UserEvent, event_id=uuid.UUID(body["event_id"])) == 1
    assert _status(connection, body["event_id"]) == "done"


def test_crash_after_storing_before_reconcile_and_ack_recovers(
    producer: TestClient,
    test_queue: PostgresQueue,
    test_session_factory: SessionFactory,
    drain: Callable[[], int],
    connection: Connection,
) -> None:
    """The at-least-once lesson, end to end.

    1-3. Three rapid clicks are fully processed. 4. The fourth click's worker
    stores the UserEvent, then crashes before reconciliation and before ACK:
    the event exists but no signal does. 5. The lease expires. 6-9. A second
    worker receives the same message; the insert is a duplicate (no-op), the
    session is reconciled anyway, and the message is acked.
    """
    session_id = str(uuid.uuid4())
    bodies = [_event(session_id, t) for t in RAPID]
    for body in bodies[:3]:
        producer.post(URL, json=body)
    drain()

    producer.post(URL, json=bodies[3])
    first_delivery = test_queue.receive()
    assert first_delivery is not None and first_delivery.attempt == 1
    with test_session_factory() as session:  # the part that ran before the crash
        stored = service.ingest_event(session, parse_message(first_delivery.body))
    assert stored.status == "accepted"
    assert _count(connection, BehaviorSignal, session_id=uuid.UUID(session_id)) == 0
    # ...crash: no reconcile, no ACK.

    _expire_leases(connection)
    second_delivery = test_queue.receive()
    assert second_delivery is not None
    assert second_delivery.message_id == first_delivery.message_id
    assert second_delivery.attempt == 2
    outcome = handle(
        test_queue, second_delivery, telemetry_processor(test_session_factory), max_attempts=5
    )

    assert outcome is Outcome.ACKED
    assert _count(connection, UserEvent, event_id=uuid.UUID(bodies[3]["event_id"])) == 1
    canonical, replay = _canonical_and_replay(connection, session_id)
    assert canonical == replay and len(canonical) == 1  # the signal was created on redelivery
    assert _status(connection, bodies[3]["event_id"]) == "done"
    assert _count(connection, QueueMessage, message_id=uuid.UUID(bodies[3]["event_id"])) == 1


# ---- Failures ------------------------------------------------------------------------------


def _inject(
    test_queue: PostgresQueue, body: dict[str, Any], message_type: str = TELEMETRY_EVENT
) -> uuid.UUID:
    message_id = uuid.uuid4()
    test_queue.enqueue(OutgoingMessage(message_id, message_type, body))
    return message_id


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("malformed", {"schema_version": 1, "event_id": "nope", "secret": "hunter2"}),
        ("unsupported version", {"schema_version": 2, "event_id": str(uuid.uuid4())}),
        ("missing version", {"event_id": str(uuid.uuid4())}),
    ],
)
def test_unprocessable_messages_are_dead_lettered_at_once(
    test_queue: PostgresQueue,
    drain: Callable[[], int],
    connection: Connection,
    label: str,
    body: dict[str, Any],
) -> None:
    message_id = _inject(test_queue, body)

    drain()

    row = connection.execute(
        select(QueueMessage.status, QueueMessage.attempts, QueueMessage.last_error).where(
            QueueMessage.message_id == message_id
        )
    ).one()
    assert (row.status, row.attempts) == ("dead", 1), label
    assert row.last_error.startswith("invalid telemetry message")
    assert "hunter2" not in row.last_error and "nope" not in row.last_error


def test_unknown_message_type_is_dead_lettered(
    test_queue: PostgresQueue, drain: Callable[[], int], connection: Connection
) -> None:
    message_id = _inject(test_queue, {"a": 1}, message_type="unknown.type")

    drain()

    assert (
        connection.scalar(select(QueueMessage.status).where(QueueMessage.message_id == message_id))
        == "dead"
    )


def test_retryable_failures_back_off_then_dead_letter_at_max_attempts(
    producer: TestClient,
    drain: Callable[[], int],
    connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def transient(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("password=hunter2 host=db.internal")

    monkeypatch.setattr(service, "reconcile_session_signals", transient)
    body = _event(str(uuid.uuid4()), 0.0)
    producer.post(URL, json=body)
    message_id = uuid.UUID(body["event_id"])

    def state() -> Any:
        return connection.execute(
            select(QueueMessage.status, QueueMessage.attempts, QueueMessage.last_error).where(
                QueueMessage.message_id == message_id
            )
        ).one()

    drain()
    assert tuple(state()) == ("pending", 1, "ConnectionError")  # rescheduled, not acked
    assert drain() == 0  # hidden during its backoff delay

    for expected_attempt in (2, 3, 4):
        _expire_leases(connection)
        drain()
        assert tuple(state()) == ("pending", expected_attempt, "ConnectionError")

    _expire_leases(connection)
    drain()
    assert tuple(state()) == ("dead", 5, "ConnectionError")
    assert "hunter2" not in str(state())
    # The event itself was stored on the first attempt and never duplicated.
    assert _count(connection, UserEvent, event_id=message_id) == 1
