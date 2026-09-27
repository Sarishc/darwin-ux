"""The versioned telemetry queue message: what the API hands to the worker.

Why a separate, versioned contract when the HTTP schema already exists?
Messages outlive deployments. A message enqueued by today's API may be read by
tomorrow's worker (a queue can hold work for hours during an outage). The HTTP
schema can then evolve for clients without silently changing the meaning of
messages already in the queue. `schema_version` makes the format explicit:
this worker understands exactly version 1, and anything else is rejected
rather than guessed at.

The message contains only client-owned event fields. No database id, no
received_at (the database sets it when the worker stores the event), no
signals, no credentials. It is plain JSON.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue

from darwin.telemetry.schemas import TelemetryEvent

TELEMETRY_EVENT = "telemetry.event"


class TelemetryMessageV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: UUID
    event_type: str
    session_id: UUID
    occurred_at: AwareDatetime
    payload: dict[str, JsonValue]

    @classmethod
    def from_event(cls, event: TelemetryEvent) -> "TelemetryMessageV1":
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            session_id=event.session_id,
            occurred_at=event.occurred_at,
            payload=event.payload,
        )

    def to_body(self) -> dict[str, Any]:
        """JSON-compatible primitives only (UUIDs and datetimes as strings)."""
        return self.model_dump(mode="json")

    def to_event(self) -> TelemetryEvent:
        """Re-apply every API validation rule; the queue is not a trusted source."""
        return TelemetryEvent.model_validate(self.model_dump(exclude={"schema_version"}))


def parse_message(body: dict[str, Any]) -> TelemetryEvent:
    """Body -> validated event. Raises pydantic.ValidationError for anything unusable.

    `schema_version` is required and must be 1: a missing or unknown version is
    rejected, never interpreted as v1.
    """
    if "schema_version" not in body:
        body = {**body, "schema_version": None}  # make "missing" fail, not default
    return TelemetryMessageV1.model_validate(body).to_event()
