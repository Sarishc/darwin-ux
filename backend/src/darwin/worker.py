"""The telemetry worker: a separate process that consumes the queue.

    python -m darwin.worker        (or: make worker)

Loop:
    receive (lease) -> process -> ack            on success
                              -> dead_letter      on a permanent error, or out of attempts
                              -> retry (backoff)  on any other error

Delivery is at least once. Processing is idempotent (UNIQUE(event_id) plus
deterministic signal reconciliation), so a redelivered message is harmless.
The worker never holds a database transaction open across processing: the
lease is committed on receive, processing uses its own transactions, and the
ack is a separate short transaction afterwards.
"""

import logging
import signal
import time
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.session import create_session_factory
from darwin.logging_config import configure_logging
from darwin.queue.base import MessageQueue, PermanentMessageError, ReceivedMessage
from darwin.queue.postgres import PostgresQueue
from darwin.telemetry.messages import TELEMETRY_EVENT
from darwin.telemetry.service import process_telemetry_message

# A fixed name, not __name__: run as `python -m darwin.worker`, __name__ is
# "__main__", which is outside the configured "darwin" logger tree.
logger = logging.getLogger("darwin.worker")

# Retry backoff: 2 s, 4 s, 8 s, ... capped. Small local defaults, not production tuning.
RETRY_BASE_DELAY = timedelta(seconds=2)
RETRY_MAX_DELAY = timedelta(minutes=5)
ERROR_TEXT_MAX_LENGTH = 300

# A processor handles one message body of one type; it raises to signal failure.
Processor = Callable[[str, dict[str, Any]], None]


class StopSignal(Protocol):
    def is_set(self) -> bool: ...
    def wait(self, timeout: float) -> bool: ...


class StopFlag:
    """A stop request that is safe to set from a signal handler.

    threading.Event is NOT: its set() takes a non-reentrant lock that the main
    thread briefly holds inside Event.wait(). A SIGTERM arriving at that moment
    runs the handler on the main thread, which then waits forever for a lock it
    already holds. This flag's set() is a plain assignment, and wait() sleeps
    in short slices, so shutdown is prompt and cannot deadlock.
    """

    SLICE_SECONDS = 0.1

    def __init__(self) -> None:
        self._stopped = False
        self.reason: str | None = None

    def set(self, reason: str | None = None) -> None:
        self.reason = reason
        self._stopped = True

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._stopped:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.SLICE_SECONDS, remaining))
        return self._stopped


class Outcome(StrEnum):
    ACKED = "acked"
    RETRY = "retry"
    DEAD = "dead"
    LEASE_LOST = "lease_lost"  # our lease expired and someone else owns the message now


def retry_delay(attempt: int) -> timedelta:
    """Exponential backoff for the delivery that just failed (attempt >= 1)."""
    return min(RETRY_BASE_DELAY * (1 << (attempt - 1)), RETRY_MAX_DELAY)


def safe_error(error: BaseException) -> str:
    """Error text that is safe to store and log.

    PermanentMessageError text is written to be safe (field names, no values).
    For anything else only the exception *type* is kept: arbitrary messages can
    contain payload values, SQL, hostnames, or credentials.
    """
    if isinstance(error, PermanentMessageError):
        text = str(error)
    else:
        text = type(error).__name__
    return text[:ERROR_TEXT_MAX_LENGTH]


def _log(level: int, message: str, received: ReceivedMessage, **context: object) -> None:
    logger.log(
        level,
        message,
        extra={
            "context": {
                "message_id": str(received.message_id),
                "message_type": received.message_type,
                "attempt": received.attempt,
                **context,
            }
        },
    )


def handle(
    queue: MessageQueue, received: ReceivedMessage, processor: Processor, *, max_attempts: int
) -> Outcome:
    """Process one received message and settle it with the queue."""
    if received.attempt > max_attempts:
        # A message that crashed the worker on every delivery never reached the
        # failure branch below; its receive count still stops it here.
        queue.dead_letter(received, error="max attempts exceeded")
        _log(logging.WARNING, "message dead-lettered", received, reason="max attempts exceeded")
        return Outcome.DEAD

    _log(logging.INFO, "message received", received)
    try:
        processor(received.message_type, received.body)
    except PermanentMessageError as error:
        queue.dead_letter(received, error=safe_error(error))
        _log(logging.WARNING, "message dead-lettered", received, reason=safe_error(error))
        return Outcome.DEAD
    except Exception as error:  # retry boundary: any other failure may be transient
        if received.attempt >= max_attempts:
            queue.dead_letter(received, error=safe_error(error))
            _log(logging.WARNING, "message dead-lettered", received, reason=safe_error(error))
            return Outcome.DEAD
        delay = retry_delay(received.attempt)
        queue.retry(received, delay=delay, error=safe_error(error))
        _log(
            logging.WARNING,
            "message retry scheduled",
            received,
            reason=safe_error(error),
            delay_seconds=delay.total_seconds(),
        )
        return Outcome.RETRY

    if not queue.ack(received):
        # Processing took longer than the lease; another delivery is in flight.
        # Harmless (processing is idempotent) but worth seeing.
        _log(logging.WARNING, "ack rejected: lease expired before processing finished", received)
        return Outcome.LEASE_LOST
    _log(logging.INFO, "message processed", received)
    return Outcome.ACKED


def run_once(queue: MessageQueue, processor: Processor, *, max_attempts: int) -> Outcome | None:
    """Receive and handle at most one message. None if the queue had nothing visible."""
    received = queue.receive()
    if received is None:
        return None
    return handle(queue, received, processor, max_attempts=max_attempts)


def run(
    queue: MessageQueue,
    processor: Processor,
    *,
    stop: StopSignal,
    poll_interval: float,
    max_attempts: int,
) -> None:
    """Work until `stop` is set. Sleeps (without spinning) whenever the queue is empty."""
    while not stop.is_set():
        if run_once(queue, processor, max_attempts=max_attempts) is None:
            stop.wait(poll_interval)  # returns early on shutdown


def telemetry_processor(session_factory: Callable[[], Session]) -> Processor:
    """Dispatch by message type; one fresh Session per message."""

    def process(message_type: str, body: dict[str, Any]) -> None:
        if message_type != TELEMETRY_EVENT:
            raise PermanentMessageError(f"unknown message type: {message_type[:64]}")
        with session_factory() as session:
            process_telemetry_message(session, body)

    return process


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(str(settings.database_url))
    session_factory = create_session_factory(engine)
    queue = PostgresQueue(
        session_factory,
        visibility_timeout=timedelta(seconds=settings.queue_visibility_timeout_seconds),
    )

    stop = StopFlag()

    def request_stop(signum: int, _frame: object) -> None:
        # Only a lock-free assignment here: no logging, no threading primitives.
        # The current message finishes, then the loop exits.
        stop.set(signal.Signals(signum).name)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    logger.info(
        "worker started",
        extra={
            "context": {
                "visibility_timeout_seconds": settings.queue_visibility_timeout_seconds,
                "max_attempts": settings.queue_max_attempts,
                "poll_interval_seconds": settings.worker_poll_interval_seconds,
            }
        },
    )
    try:
        run(
            queue,
            telemetry_processor(session_factory),
            stop=stop,
            poll_interval=settings.worker_poll_interval_seconds,
            max_attempts=settings.queue_max_attempts,
        )
    finally:
        logger.info("worker stopping", extra={"context": {"signal": stop.reason}})
        engine.dispose()
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
