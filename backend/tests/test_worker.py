"""Worker decisions (ack / retry / dead) and processing order, without a database.

`InMemoryQueue` implements the same MessageQueue port as PostgresQueue; it
records what the worker asked it to do.
"""

import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from darwin import worker
from darwin.queue.base import OutgoingMessage, PermanentMessageError, ReceivedMessage
from darwin.signals.service import ReconcileResult
from darwin.telemetry import service
from darwin.telemetry.messages import TELEMETRY_EVENT, TelemetryMessageV1
from darwin.telemetry.schemas import IngestionResult, TelemetryEvent
from darwin.worker import Outcome, StopFlag, handle, retry_delay, run, run_once, safe_error


@dataclass
class InMemoryQueue:
    visible: list[ReceivedMessage] = field(default_factory=list)
    acked: list[uuid.UUID] = field(default_factory=list)
    retried: list[tuple[uuid.UUID, timedelta, str]] = field(default_factory=list)
    dead: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    receives: int = 0

    def enqueue(self, message: OutgoingMessage) -> bool:
        self.visible.append(
            ReceivedMessage(message.message_id, message.message_type, message.body, 1, uuid.uuid4())
        )
        return True

    def receive(self) -> ReceivedMessage | None:
        self.receives += 1
        return self.visible.pop(0) if self.visible else None

    def ack(self, message: ReceivedMessage) -> bool:
        self.acked.append(message.message_id)
        return True

    def retry(self, message: ReceivedMessage, *, delay: timedelta, error: str) -> bool:
        self.retried.append((message.message_id, delay, error))
        return True

    def dead_letter(self, message: ReceivedMessage, *, error: str) -> bool:
        self.dead.append((message.message_id, error))
        return True


def _received(attempt: int = 1, message_type: str = TELEMETRY_EVENT) -> ReceivedMessage:
    return ReceivedMessage(uuid.uuid4(), message_type, {"any": "body"}, attempt, uuid.uuid4())


def _ok(_type: str, _body: dict[str, Any]) -> None:
    return None


def _raises(error: BaseException) -> worker.Processor:
    def processor(_type: str, _body: dict[str, Any]) -> None:
        raise error

    return processor


# ---- ack / retry / dead ------------------------------------------------------------


def test_success_acks() -> None:
    queue, message = InMemoryQueue(), _received()

    assert handle(queue, message, _ok, max_attempts=5) is Outcome.ACKED
    assert queue.acked == [message.message_id]
    assert queue.retried == []
    assert queue.dead == []


def test_retryable_failure_is_not_acked_and_is_retried_with_backoff() -> None:
    queue, message = InMemoryQueue(), _received(attempt=2)

    outcome = handle(queue, message, _raises(ConnectionError("db down")), max_attempts=5)

    assert outcome is Outcome.RETRY
    assert queue.acked == []
    assert queue.retried == [(message.message_id, timedelta(seconds=4), "ConnectionError")]


def test_permanent_failure_is_dead_lettered_immediately() -> None:
    queue, message = InMemoryQueue(), _received(attempt=1)

    outcome = handle(queue, message, _raises(PermanentMessageError("bad schema")), max_attempts=5)

    assert outcome is Outcome.DEAD
    assert queue.dead == [(message.message_id, "bad schema")]
    assert queue.acked == queue.retried == []


def test_failure_on_the_last_attempt_is_dead_lettered() -> None:
    queue, message = InMemoryQueue(), _received(attempt=5)

    assert handle(queue, message, _raises(RuntimeError()), max_attempts=5) is Outcome.DEAD
    assert queue.dead == [(message.message_id, "RuntimeError")]
    assert queue.retried == []


def test_message_beyond_max_attempts_is_dead_lettered_without_processing() -> None:
    # e.g. it crashed the worker process on every previous delivery.
    queue, message = InMemoryQueue(), _received(attempt=6)
    calls: list[str] = []

    outcome = handle(queue, message, lambda t, b: calls.append(t), max_attempts=5)

    assert outcome is Outcome.DEAD
    assert calls == []
    assert queue.dead == [(message.message_id, "max attempts exceeded")]


def test_unknown_message_type_is_permanent() -> None:
    queue = InMemoryQueue()
    message = _received(message_type="something.else")
    processor = worker.telemetry_processor(lambda: pytest.fail("no session needed"))

    assert handle(queue, message, processor, max_attempts=5) is Outcome.DEAD
    assert queue.dead[0][1] == "unknown message type: something.else"


def test_lost_lease_is_reported() -> None:
    class LeaseLost(InMemoryQueue):
        def ack(self, message: ReceivedMessage) -> bool:
            return False

    assert handle(LeaseLost(), _received(), _ok, max_attempts=5) is Outcome.LEASE_LOST


# ---- policy helpers ----------------------------------------------------------------


def test_retry_backoff_doubles_and_is_capped() -> None:
    assert [retry_delay(a).total_seconds() for a in (1, 2, 3, 4)] == [2, 4, 8, 16]
    assert retry_delay(30) == worker.RETRY_MAX_DELAY


def test_error_text_never_contains_exception_messages() -> None:
    leaky = RuntimeError("password=hunter2 payload={'email': 'a@b.c'} host=db.internal")

    assert safe_error(leaky) == "RuntimeError"


def test_permanent_error_text_is_kept_but_bounded() -> None:
    assert safe_error(PermanentMessageError("x" * 1000)) == "x" * worker.ERROR_TEXT_MAX_LENGTH


# ---- loop -------------------------------------------------------------------------


def test_run_once_returns_none_when_idle() -> None:
    assert run_once(InMemoryQueue(), _ok, max_attempts=5) is None


class CountingStop(threading.Event):
    """Stops after N idle waits; records the requested sleep."""

    def __init__(self, waits_before_stop: int) -> None:
        super().__init__()
        self.remaining = waits_before_stop
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        self.remaining -= 1
        if self.remaining <= 0:
            self.set()
        return self.is_set()


def test_idle_worker_sleeps_between_polls_instead_of_spinning() -> None:
    queue, stop = InMemoryQueue(), CountingStop(waits_before_stop=3)

    run(queue, _ok, stop=stop, poll_interval=0.25, max_attempts=5)

    # One receive per sleep: the loop never polls again without waiting.
    assert queue.receives == 3
    assert stop.timeouts == [0.25, 0.25, 0.25]


def test_busy_worker_does_not_sleep_between_messages() -> None:
    queue, stop = InMemoryQueue(), CountingStop(waits_before_stop=1)
    for _ in range(3):
        queue.enqueue(OutgoingMessage(uuid.uuid4(), TELEMETRY_EVENT, {}))

    run(queue, _ok, stop=stop, poll_interval=0.25, max_attempts=5)

    assert len(queue.acked) == 3
    assert stop.timeouts == [0.25]  # slept only once the queue was empty


def test_stop_requested_before_start_exits_immediately() -> None:
    queue, stop = InMemoryQueue(), threading.Event()
    stop.set()

    run(queue, _ok, stop=stop, poll_interval=10, max_attempts=5)

    assert queue.receives == 0


# ---- processing order: store, then ALWAYS reconcile ---------------------------------


def _message_body() -> dict[str, Any]:
    event = TelemetryEvent.model_validate(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "button_click",
            "session_id": str(uuid.uuid4()),
            "occurred_at": "2026-09-26T17:00:00Z",
            "payload": {"component": "signup_submit"},
        }
    )
    return TelemetryMessageV1.from_event(event).to_body()


@pytest.mark.parametrize("stored_status", ["accepted", "duplicate"])
def test_processing_stores_then_reconciles_even_for_a_duplicate(
    monkeypatch: pytest.MonkeyPatch, stored_status: str
) -> None:
    calls: list[str] = []

    def fake_ingest(_session: Session, event: TelemetryEvent) -> IngestionResult:
        calls.append("ingest")
        return IngestionResult(event_id=event.event_id, status=stored_status)

    def fake_reconcile(_session: Session, _session_id: uuid.UUID) -> ReconcileResult:
        calls.append("reconcile")
        return ReconcileResult(0, 0, 0, 0)

    monkeypatch.setattr(service, "ingest_event", fake_ingest)
    monkeypatch.setattr(service, "reconcile_session_signals", fake_reconcile)

    outcome = service.process_telemetry_message(Session(), _message_body())

    assert calls == ["ingest", "reconcile"]  # a duplicate is NOT a reason to skip
    assert outcome.stored.status == stored_status


def test_invalid_message_is_permanent_and_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "ingest_event", lambda *a: pytest.fail("must not store"))
    body = {**_message_body(), "schema_version": 2, "payload": {"secret": "hunter2"}}

    with pytest.raises(PermanentMessageError) as error:
        service.process_telemetry_message(Session(), body)

    assert "schema_version" in str(error.value)
    assert "hunter2" not in str(error.value)  # field names, never values


def test_reconciliation_failure_propagates_so_the_message_is_not_acked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "ingest_event",
        lambda _s, e: IngestionResult(event_id=e.event_id, status="accepted"),
    )

    def broken(*_args: object) -> ReconcileResult:
        raise RuntimeError("detector bug")

    monkeypatch.setattr(service, "reconcile_session_signals", broken)
    queue = InMemoryQueue()
    message = _received()
    message = ReceivedMessage(message.message_id, TELEMETRY_EVENT, _message_body(), 1, uuid.uuid4())

    def processor(_type: str, body: dict[str, Any]) -> None:
        service.process_telemetry_message(Session(), body)

    assert handle(queue, message, processor, max_attempts=5) is Outcome.RETRY
    assert queue.acked == []


# ---- shutdown flag -----------------------------------------------------------------


def test_stop_flag_wait_times_out_when_not_set() -> None:
    import time

    started = time.monotonic()
    assert StopFlag().wait(0.15) is False
    assert time.monotonic() - started >= 0.15


def test_stop_flag_set_from_a_real_signal_handler_ends_the_wait_promptly() -> None:
    import os
    import signal
    import time

    flag = StopFlag()
    previous = signal.signal(signal.SIGUSR1, lambda *_: flag.set("SIGUSR1"))
    try:
        timer = threading.Timer(0.05, os.kill, args=(os.getpid(), signal.SIGUSR1))
        timer.start()
        started = time.monotonic()
        assert flag.wait(10.0) is True
        assert time.monotonic() - started < 1.0
        assert flag.reason == "SIGUSR1"
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_run_exits_when_the_stop_flag_is_set_during_an_idle_wait() -> None:
    flag = StopFlag()
    threading.Timer(0.05, flag.set).start()

    run(InMemoryQueue(), _ok, stop=flag, poll_interval=10.0, max_attempts=5)  # returns promptly
