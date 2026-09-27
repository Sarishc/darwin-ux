"""The telemetry endpoint's HTTP behaviour, without a database.

The unit-test settings point at a closed port, so anything that reached the
database would fail with 500. A 422 therefore proves the request was rejected
before persistence was attempted.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from darwin.logging_config import ROOT_LOGGER_NAME

URL = "/api/v1/telemetry/events"
MARKER = "PAYLOAD-MARKER-7f3a"


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


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record) + repr(getattr(record, "context", "")))


@pytest.fixture
def darwin_logs() -> Iterator[_Collect]:
    handler = _Collect()
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "not-a-uuid"},
        {"session_id": "not-a-uuid"},
        {"occurred_at": "2026-09-26T17:00:00"},  # naive
        {"event_type": ""},
        {"event_type": "Button Click"},
        {"payload": [1, 2, 3]},
        {"payload": {"k": "x" * 9000}},
        {"received_at": "2026-09-26T17:00:00Z"},  # server-owned
        {"id": str(uuid.uuid4())},  # server-owned
    ],
)
def test_invalid_events_are_rejected_before_persistence(
    client: TestClient, overrides: dict[str, Any]
) -> None:
    response = client.post(URL, json=_body(**overrides))

    assert response.status_code == 422


def test_missing_required_fields_are_rejected(client: TestClient) -> None:
    body = _body()
    del body["event_id"]

    response = client.post(URL, json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "event_id"]


def test_validation_errors_do_not_echo_the_rejected_values(client: TestClient) -> None:
    response = client.post(URL, json=_body(payload=[MARKER]))

    assert response.status_code == 422
    assert MARKER not in response.text
    assert "input" not in response.json()["detail"][0]


def test_deeply_nested_payload_is_a_small_422_not_a_500(client: TestClient) -> None:
    nested = "1"
    for _ in range(5_000):
        nested = '{"a":' + nested + "}"
    body = _body()
    del body["payload"]
    # Built as text: the nesting is too deep for json.dumps to serialise.
    raw = json.dumps(body)[:-1] + f', "payload": {nested}}}'

    response = client.post(URL, content=raw, headers={"content-type": "application/json"})

    assert response.status_code == 422
    assert len(response.content) < 500


def test_database_failure_is_a_500_without_leaking_payload(
    app: FastAPI, darwin_logs: _Collect
) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(URL, json=_body(payload={"note": MARKER}))

    assert response.status_code == 500
    assert MARKER not in response.text
    assert all(MARKER not in line for line in darwin_logs.lines)


def test_endpoint_is_documented_in_openapi(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"][URL]["post"]

    assert operation["summary"] == "Submit one behavioural event"
    assert "202" in operation["responses"]
