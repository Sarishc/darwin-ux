"""The telemetry request contract, validated without HTTP or a database."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from darwin.telemetry.schemas import (
    EVENT_TYPE_MAX_LENGTH,
    PAYLOAD_MAX_BYTES,
    PAYLOAD_MAX_DEPTH,
    TelemetryEvent,
)


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


def _nested(depth: int) -> dict[str, Any]:
    value: Any = 1
    for _ in range(depth):
        value = {"a": value}
    assert isinstance(value, dict)
    return value


def test_valid_event_is_parsed() -> None:
    body = _body()

    event = TelemetryEvent.model_validate(body)

    assert str(event.event_id) == body["event_id"]
    assert event.event_type == "button_click"
    assert event.occurred_at == datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
    assert event.payload == {"component": "signup_submit"}


def test_payload_is_optional_and_defaults_to_empty_object() -> None:
    body = _body()
    del body["payload"]

    assert TelemetryEvent.model_validate(body).payload == {}


@pytest.mark.parametrize("field", ["event_id", "session_id"])
@pytest.mark.parametrize("value", ["not-a-uuid", "", 123, "user@example.com"])
def test_identifiers_must_be_uuids(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(_body(**{field: value}))


def test_naive_occurred_at_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        TelemetryEvent.model_validate(_body(occurred_at="2026-09-26T17:00:00"))


def test_timezone_aware_occurred_at_keeps_the_same_instant() -> None:
    event = TelemetryEvent.model_validate(_body(occurred_at="2026-09-26T19:00:00+02:00"))

    assert event.occurred_at.utcoffset() == timedelta(hours=2)
    assert event.occurred_at == datetime(2026, 9, 26, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "event_type",
    [
        "",  # empty
        "a" * (EVENT_TYPE_MAX_LENGTH + 1),  # longer than the column
        "ButtonClick",  # not snake_case (no silent lower-casing)
        "button click",
        "1st_click",
        "button-click",
    ],
)
def test_invalid_event_types_are_rejected(event_type: str) -> None:
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(_body(event_type=event_type))


def test_event_type_at_max_length_is_accepted() -> None:
    event_type = "a" * EVENT_TYPE_MAX_LENGTH

    assert TelemetryEvent.model_validate(_body(event_type=event_type)).event_type == event_type


@pytest.mark.parametrize("payload", [[1, 2], "text", 42, None, True])
def test_payload_must_be_a_json_object(payload: object) -> None:
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(_body(payload=payload))


def test_payload_over_the_size_limit_is_rejected() -> None:
    too_big = {"k": "x" * PAYLOAD_MAX_BYTES}

    with pytest.raises(ValidationError, match="limit is"):
        TelemetryEvent.model_validate(_body(payload=too_big))


def test_payload_just_under_the_size_limit_is_accepted() -> None:
    # {"k":"..."} adds 8 bytes around the string.
    fits = {"k": "x" * (PAYLOAD_MAX_BYTES - 8)}

    assert TelemetryEvent.model_validate(_body(payload=fits)).payload == fits


def test_payload_nesting_limit() -> None:
    TelemetryEvent.model_validate(_body(payload=_nested(PAYLOAD_MAX_DEPTH)))

    with pytest.raises(ValidationError, match="nesting"):
        TelemetryEvent.model_validate(_body(payload=_nested(PAYLOAD_MAX_DEPTH + 1)))


def test_pathologically_deep_payload_is_a_validation_error_not_a_crash() -> None:
    with pytest.raises(ValidationError, match="nesting"):
        TelemetryEvent.model_validate(_body(payload=_nested(10_000)))


@pytest.mark.parametrize(
    "field",
    ["id", "received_at", "ingested_at", "user_id", "email", "ip_address", "user_agent"],
)
def test_server_owned_and_identifying_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TelemetryEvent.model_validate(_body(**{field: "anything"}))


def test_event_is_immutable_after_validation() -> None:
    event = TelemetryEvent.model_validate(_body())

    with pytest.raises(ValidationError):
        event.event_type = "changed"  # type: ignore[misc]


def test_server_owned_fields_are_not_part_of_the_contract() -> None:
    # received_at comes from PostgreSQL (now()); id from the server.
    assert {"id", "received_at"}.isdisjoint(TelemetryEvent.model_fields)
