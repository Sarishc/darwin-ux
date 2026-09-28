"""QueueMessage: one unit of asynchronous work in the local, PostgreSQL-backed queue.

Modelled on SQS so an SQS adapter can replace it later:

- ``visible_at`` is the visibility timeout. A message can be received when
  ``status = 'pending' AND visible_at <= now()``. Receiving pushes
  ``visible_at`` into the future (the lease); a retry pushes it forward by a
  backoff delay. If a worker dies, the lease simply runs out and the message
  becomes visible again — nothing has to "unlock" it.
- ``receipt_handle`` identifies one delivery. Ack/retry/dead-letter must present
  it, so a worker whose lease already expired cannot touch a later delivery.
- ``attempts`` is SQS's receive count.

``body`` carries the message exactly as the producer sent it, including
untrusted telemetry payload data: it is never logged or shown by tooling.

``traceparent`` / ``tracestate`` (Step 16, migration 0012) carry the producer's
W3C trace context as OPERATIONAL metadata, beside the body rather than in it.
Validated on write (format, length); the worker ignores anything malformed and
starts a fresh trace. Never used for authorization.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

PENDING = "pending"
DONE = "done"
DEAD = "dead"
LAST_ERROR_MAX_LENGTH = 500


class QueueMessage(Base):
    __tablename__ = "queue_message"
    __table_args__ = (
        CheckConstraint("message_type <> ''", name="message_type_not_empty"),
        CheckConstraint("status IN ('pending', 'done', 'dead')", name="status_is_known"),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        CheckConstraint("jsonb_typeof(body) = 'object'", name="body_is_object"),
        CheckConstraint(
            "traceparent IS NULL OR traceparent ~ '^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'",
            name="traceparent_is_w3c",
        ),
        CheckConstraint(
            "tracestate IS NULL OR traceparent IS NOT NULL", name="tracestate_needs_traceparent"
        ),
        # Serves the claim query exactly: pending messages ordered by visible_at.
        # Partial, so done/dead history does not bloat it.
        Index(
            "ix_queue_message_pending_visible_at",
            "visible_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Producer-chosen idempotency key (the event_id for telemetry). UNIQUE:
    # resubmitting the same event never creates a second message.
    message_id: Mapped[uuid.UUID] = mapped_column(unique=True)

    # What the body is, e.g. "telemetry.event"; the worker dispatches on it.
    message_type: Mapped[str] = mapped_column(String(64))

    # The serialised, versioned message (see darwin/telemetry/messages.py).
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    receipt_handle: Mapped[uuid.UUID | None]

    # Sanitised, bounded: an exception type and field names, never values.
    last_error: Mapped[str | None] = mapped_column(String(LAST_ERROR_MAX_LENGTH))

    # W3C trace context of the producer (operational metadata; see module docstring).
    traceparent: Mapped[str | None] = mapped_column(String(55))
    tracestate: Mapped[str | None] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the message reached done or dead.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
