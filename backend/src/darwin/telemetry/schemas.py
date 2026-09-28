"""The public telemetry contract: what a client may send, and what it gets back.

These models are the API boundary. They stay the same when ingestion later
moves behind a queue, so clients never have to change.

Privacy: the contract has no field for a user identity, IP address, user
agent, or device identifier. ``session_id`` is a random per-visit UUID.
``payload`` is still untrusted input that *may* accidentally contain
personal data, so it is size-limited here and never logged.
"""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator

# Matches the user_event.event_type column (VARCHAR(64)).
EVENT_TYPE_MAX_LENGTH = 64
# snake_case: starts with a letter; lowercase letters, digits, underscores.
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*$"

# Step 4 safeguards against oversized or pathological payloads.
PAYLOAD_MAX_BYTES = 8 * 1024  # serialized, compact JSON
PAYLOAD_MAX_DEPTH = 5  # {"a": {"b": ...}} nesting levels

EventType = Annotated[
    str,
    Field(
        min_length=1,
        max_length=EVENT_TYPE_MAX_LENGTH,
        pattern=EVENT_TYPE_PATTERN,
        examples=["button_click"],
        description="snake_case event name, e.g. `button_click`, `form_submit`.",
    ),
]


def _depth(value: object) -> int:
    """Nesting depth of a JSON value, computed without recursion."""
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            depth += 1
            stack.extend((child, depth) for child in current.values())
        elif isinstance(current, list):
            depth += 1
            stack.extend((child, depth) for child in current)
        deepest = max(deepest, depth)
    return deepest


class TelemetryEvent(BaseModel):
    """One behavioural event, as sent by the client.

    Only client-owned fields are allowed. The server owns the row id and
    ``received_at``; sending them (or any other unknown field) is a 422.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "event_id": "5b2f0c8e-4c1a-4a5e-9d6f-0a1b2c3d4e5f",
                    "event_type": "button_click",
                    "session_id": "9c8b7a6f-5e4d-4c3b-8a29-1f0e9d8c7b6a",
                    "occurred_at": "2026-09-26T17:00:00Z",
                    "payload": {"component": "signup_submit"},
                }
            ]
        },
    )

    event_id: UUID = Field(
        description="Client-generated idempotency key. Resending the same event_id is safe."
    )
    event_type: EventType
    session_id: UUID = Field(description="Random, anonymous per-visit identifier.")
    occurred_at: AwareDatetime = Field(
        description="When the event happened on the client. Must include a timezone."
    )
    # UI attribution (Step 15). Optional claims about what the page rendered. The
    # worker verifies ui_spec_version_id against ui_spec_hash; unverified claims are
    # stored but never trusted. Omitted = unknown (all pre-Step-15 clients).
    ui_generation: int | None = Field(
        default=None, ge=0, le=100_000, description="Generation number the page rendered."
    )
    ui_spec_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the rendered UI Spec (as served by the backend).",
    )
    ui_spec_version_id: UUID | None = Field(
        default=None, description="The served UI Spec version id (as served by the backend)."
    )
    payload: dict[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            f"Event-specific details as a JSON object "
            f"(max {PAYLOAD_MAX_BYTES} bytes, max depth {PAYLOAD_MAX_DEPTH}). "
            "Never include personal data."
        ),
    )

    # mode="before": runs on the raw parsed JSON, *before* pydantic walks the
    # structure, so a pathologically deep payload is rejected with a clear 422
    # instead of exhausting recursion inside validation.
    @field_validator("payload", mode="before")
    @classmethod
    def _limit_payload(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value  # let normal validation report "must be an object"
        if _depth(value) > PAYLOAD_MAX_DEPTH:
            raise ValueError(f"payload nesting deeper than {PAYLOAD_MAX_DEPTH} levels")
        size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())
        if size > PAYLOAD_MAX_BYTES:
            raise ValueError(f"payload is {size} bytes; the limit is {PAYLOAD_MAX_BYTES}")
        return value


IngestionStatus = Literal["accepted", "duplicate"]


class IngestionResult(BaseModel):
    """What happened to a submitted event. Both outcomes are successes.

    ``accepted``: newly queued for asynchronous processing (not yet stored).
    ``duplicate``: an event with this event_id was already accepted earlier —
    it is queued, being processed, or processed — and nothing new was queued.
    Clients must treat both values the same way (no retry needed).
    """

    event_id: UUID
    status: IngestionStatus
