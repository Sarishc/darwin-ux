"""Local queue implementation on PostgreSQL (the database DarwinUX already uses).

Every operation is one short transaction. No transaction is held open while a
message is being processed: `receive` commits the lease immediately, and the
worker processes afterwards in its own transactions.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from darwin.db.models.queue_message import (
    DEAD,
    DONE,
    LAST_ERROR_MAX_LENGTH,
    PENDING,
    QueueMessage,
)
from darwin.observability.propagation import valid_traceparent, valid_tracestate
from darwin.queue.base import OutgoingMessage, ReceivedMessage


class PostgresQueue:
    def __init__(
        self, session_factory: Callable[[], Session], *, visibility_timeout: timedelta
    ) -> None:
        self._session_factory = session_factory
        self._visibility_timeout = visibility_timeout

    def enqueue(self, message: OutgoingMessage) -> bool:
        """INSERT ... ON CONFLICT (message_id): one statement, no check-then-insert race.

        A new message_id is inserted. A known one is left alone (`False`) —
        unless it was dead-lettered, in which case resubmitting it re-queues it
        with a fresh attempt budget (`True`). The original body is kept: the
        first submission wins.
        """
        traceparent = valid_traceparent(message.traceparent)
        tracestate = valid_tracestate(message.tracestate) if traceparent else None
        statement = (
            insert(QueueMessage)
            .values(
                message_id=message.message_id,
                message_type=message.message_type,
                body=message.body,
                traceparent=traceparent,
                tracestate=tracestate,
            )
            .on_conflict_do_update(
                index_elements=[QueueMessage.message_id],
                set_={
                    "status": PENDING,
                    "attempts": 0,
                    "visible_at": func.now(),
                    "receipt_handle": None,
                    "finished_at": None,
                    # A resubmission is a new operation: it carries its own trace context.
                    "traceparent": traceparent,
                    "tracestate": tracestate,
                },
                where=QueueMessage.status == DEAD,
            )
            .returning(QueueMessage.id)
        )
        with self._session_factory() as session, session.begin():
            return session.execute(statement).scalar_one_or_none() is not None

    def receive(self) -> ReceivedMessage | None:
        """Claim the oldest visible message in one atomic statement.

            UPDATE queue_message SET attempts = attempts + 1, receipt_handle = :new,
                                     visible_at = now() + :visibility_timeout
            WHERE id = (SELECT id FROM queue_message
                        WHERE status = 'pending' AND visible_at <= now()
                        ORDER BY visible_at LIMIT 1
                        FOR UPDATE SKIP LOCKED)
            RETURNING ...

        SKIP LOCKED: a row another worker is claiming right now is skipped, not
        waited for, so concurrent workers never receive the same delivery.
        """
        candidate = (
            select(QueueMessage.id)
            .where(QueueMessage.status == PENDING, QueueMessage.visible_at <= func.now())
            .order_by(QueueMessage.visible_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        receipt = uuid.uuid4()
        statement = (
            update(QueueMessage)
            .where(QueueMessage.id == candidate)
            .values(
                attempts=QueueMessage.attempts + 1,
                receipt_handle=receipt,
                visible_at=func.now() + self._visibility_timeout,
            )
            .returning(
                QueueMessage.message_id,
                QueueMessage.message_type,
                QueueMessage.body,
                QueueMessage.attempts,
                QueueMessage.traceparent,
                QueueMessage.tracestate,
            )
        )
        with self._session_factory() as session, session.begin():
            row = session.execute(statement).one_or_none()
        if row is None:
            return None
        message_id, message_type, body, attempts, traceparent, tracestate = row
        return ReceivedMessage(
            message_id, message_type, body, attempts, receipt, traceparent, tracestate
        )

    def _finish(self, message: ReceivedMessage, **values: object) -> bool:
        """Apply a state change only if `message` still holds the current lease."""
        statement = (
            update(QueueMessage)
            .where(
                QueueMessage.message_id == message.message_id,
                QueueMessage.receipt_handle == message.receipt_handle,
                QueueMessage.status == PENDING,
            )
            .values(receipt_handle=None, **values)
            .returning(QueueMessage.id)
        )
        with self._session_factory() as session, session.begin():
            return session.execute(statement).scalar_one_or_none() is not None

    def ack(self, message: ReceivedMessage) -> bool:
        return self._finish(message, status=DONE, finished_at=func.now())

    def retry(self, message: ReceivedMessage, *, delay: timedelta, error: str) -> bool:
        return self._finish(
            message,
            visible_at=func.now() + delay,
            last_error=error[:LAST_ERROR_MAX_LENGTH],
        )

    def dead_letter(self, message: ReceivedMessage, *, error: str) -> bool:
        return self._finish(
            message,
            status=DEAD,
            finished_at=func.now(),
            last_error=error[:LAST_ERROR_MAX_LENGTH],
        )
