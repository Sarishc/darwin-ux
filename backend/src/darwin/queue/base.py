"""The queue port: the only queue API producers and the worker may use.

Deliberately the shape of SQS, so an SQS adapter can implement it later:

    enqueue      ~ SendMessage       (idempotent on message_id)
    receive      ~ ReceiveMessage    (starts a visibility timeout / lease)
    ack          ~ DeleteMessage
    retry        ~ ChangeMessageVisibility (make visible again after a delay)
    dead_letter  ~ redrive to a dead-letter queue

Delivery is AT LEAST ONCE: a message can be received again after a crash or an
expired lease. Consumers must be idempotent.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol


class PermanentMessageError(Exception):
    """The message can never be processed (malformed, unknown type/version).

    The worker dead-letters it immediately instead of retrying. The message
    text must be safe to store: no values from the message body.
    """


@dataclass(frozen=True)
class OutgoingMessage:
    message_id: uuid.UUID
    message_type: str
    body: dict[str, Any]  # JSON-compatible
    # W3C trace context of the producer: operational metadata, never inside `body`.
    traceparent: str | None = None
    tracestate: str | None = None


@dataclass(frozen=True)
class ReceivedMessage:
    message_id: uuid.UUID
    message_type: str
    body: dict[str, Any]
    attempt: int  # 1 on first delivery
    receipt_handle: uuid.UUID  # identifies this delivery (its lease)
    traceparent: str | None = None  # as stored; the worker re-validates before use
    tracestate: str | None = None


class MessageQueue(Protocol):
    def enqueue(self, message: OutgoingMessage) -> bool:
        """Durably store the message. True if newly accepted, False if already known."""
        ...

    def receive(self) -> ReceivedMessage | None:
        """Lease one visible message, or None if nothing is visible."""
        ...

    def ack(self, message: ReceivedMessage) -> bool:
        """Mark processed. False if this delivery's lease was already lost."""
        ...

    def retry(self, message: ReceivedMessage, *, delay: timedelta, error: str) -> bool:
        """Release the lease; the message becomes visible again after `delay`."""
        ...

    def dead_letter(self, message: ReceivedMessage, *, error: str) -> bool:
        """Stop delivering the message; keep it for inspection."""
        ...
