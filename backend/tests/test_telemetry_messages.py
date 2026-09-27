"""The versioned telemetry queue message (no database)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from darwin.telemetry.messages import TelemetryMessageV1, parse_message
from darwin.telemetry.schemas import TelemetryEvent


def _event(**overrides: Any) -> TelemetryEvent:
    body: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "button_click",
        "session_id": str(uuid.uuid4()),
        "occurred_at": "2026-09-26T17:00:00+02:00",
        "payload": {"component": "signup_submit", "x": 3},
    }
    body.update(overrides)
    return TelemetryEvent.model_validate(body)


def _body(**overrides: Any) -> dict[str, Any]:
    body = TelemetryMessageV1.from_event(_event()).to_body()
    body.update(overrides)
    return body


def test_v1_body_is_plain_json_with_exactly_the_client_fields() -> None:
    event = _event()

    body = TelemetryMessageV1.from_event(event).to_body()

    assert json.loads(json.dumps(body)) == body  # JSON primitives only
    assert body == {
        "schema_version": 1,
        "event_id": str(event.event_id),
        "event_type": "button_click",
        "session_id": str(event.session_id),
        "occurred_at": "2026-09-26T17:00:00+02:00",
        "payload": {"component": "signup_submit", "x": 3},
    }


def test_body_contains_no_server_owned_fields() -> None:
    body = _body()

    assert {"id", "received_at", "signals", "status"}.isdisjoint(body)


def test_round_trip_preserves_the_event() -> None:
    event = _event()

    assert parse_message(TelemetryMessageV1.from_event(event).to_body()) == event


def test_round_trip_preserves_the_instant() -> None:
    parsed = parse_message(_body(occurred_at="2026-09-26T19:00:00+02:00"))

    assert parsed.occurred_at == datetime(2026, 9, 26, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize("version", [0, 2, "1", None, 1.5])
def test_unknown_schema_versions_are_rejected(version: object) -> None:
    with pytest.raises(ValidationError):
        parse_message(_body(schema_version=version))


def test_missing_schema_version_is_rejected_not_assumed_v1() -> None:
    body = _body()
    del body["schema_version"]

    with pytest.raises(ValidationError):
        parse_message(body)


@pytest.mark.parametrize("field", ["event_id", "session_id"])
def test_malformed_uuids_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        parse_message(_body(**{field: "not-a-uuid"}))


def test_naive_occurred_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_message(_body(occurred_at="2026-09-26T17:00:00"))


@pytest.mark.parametrize("field", ["id", "received_at", "user_id"])
def test_unexpected_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        parse_message(_body(**{field: "x"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "Not Snake"},  # the API's event_type rule
        {"payload": {"k": "x" * 9000}},  # the API's payload size limit
        {"payload": [1, 2]},
    ],
)
def test_worker_reapplies_the_api_validation_rules(overrides: dict[str, Any]) -> None:
    # A queue is not a trusted source: a message that bypassed the API is
    # held to the same rules.
    with pytest.raises(ValidationError):
        parse_message(_body(**overrides))
