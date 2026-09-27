"""Behaviour signals end to end: HTTP events -> UserEvent -> detectors -> BehaviorSignal.

Uses the rolled-back `api` / `connection` fixtures (conftest.py): the real
ingestion and detection code runs, and nothing remains in darwin_test.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, inspect, select
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, UserEvent
from darwin.signals import service as signal_service
from darwin.signals.detectors import (
    ERROR_BURST_THRESHOLD,
    RAGE_CLICK_THRESHOLD,
    RAGE_CLICK_VERSION,
)

pytestmark = pytest.mark.integration

URL = "/api/v1/telemetry/events"
T0 = datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)


def _event(
    session_id: str,
    seconds: float,
    event_type: str = "button_click",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "session_id": session_id,
        "occurred_at": (T0 + timedelta(seconds=seconds)).isoformat(),
        "payload": {"component": "signup_submit"} if payload is None else payload,
    }


def _clicks(session_id: str, times: list[float], component: str = "signup_submit") -> list[Any]:
    return [_event(session_id, t, payload={"component": component}) for t in times]


def _post_all(api: TestClient, bodies: list[dict[str, Any]]) -> list[int]:
    return [api.post(URL, json=body).status_code for body in bodies]


def _signals(connection: Connection, session_id: str | None = None) -> list[BehaviorSignal]:
    """Canonical (not superseded) signals."""
    statement = (
        select(BehaviorSignal)
        .where(BehaviorSignal.superseded_at.is_(None))
        .order_by(BehaviorSignal.window_start)
    )
    if session_id is not None:
        statement = statement.where(BehaviorSignal.session_id == uuid.UUID(session_id))
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        return list(session.scalars(statement))


def _new_session() -> str:
    return str(uuid.uuid4())


RAPID = [0.0, 0.4, 0.8, 1.2]  # RAGE_CLICK_THRESHOLD clicks inside 2 s


# ---- Rage click ---------------------------------------------------------------------


def test_rage_clicks_create_exactly_one_signal(api: TestClient, connection: Connection) -> None:
    session_id = _new_session()
    bodies = _clicks(session_id, RAPID)

    statuses = _post_all(api, bodies)

    assert statuses == [202] * len(bodies)  # detection never changes the API response
    [signal] = _signals(connection, session_id)
    assert signal.signal_type == "rage_click"
    assert signal.detector_version == RAGE_CLICK_VERSION
    assert signal.evidence["component"] == "signup_submit"
    assert signal.evidence["count"] == RAGE_CLICK_THRESHOLD
    assert signal.evidence["event_ids"] == [b["event_id"] for b in bodies]
    assert signal.window_start == T0
    assert signal.window_end == T0 + timedelta(seconds=RAPID[-1])
    assert signal.detected_at is not None


def test_ingestion_response_does_not_expose_signals(api: TestClient) -> None:
    session_id = _new_session()
    responses = [api.post(URL, json=body).json() for body in _clicks(session_id, RAPID)]

    assert all(set(r) == {"event_id", "status"} for r in responses)


def test_evidence_references_stored_events(api: TestClient, connection: Connection) -> None:
    session_id = _new_session()
    _post_all(api, _clicks(session_id, RAPID))

    [signal] = _signals(connection, session_id)
    stored = connection.scalars(
        select(UserEvent.event_id).where(UserEvent.session_id == uuid.UUID(session_id))
    ).all()
    assert {uuid.UUID(e) for e in signal.evidence["event_ids"]} <= set(stored)


def test_resending_the_same_events_creates_no_new_signal(
    api: TestClient, connection: Connection
) -> None:
    session_id = _new_session()
    bodies = _clicks(session_id, RAPID)
    _post_all(api, bodies)

    replies = [api.post(URL, json=body).json()["status"] for body in bodies]

    assert replies == ["duplicate"] * len(bodies)
    assert len(_signals(connection, session_id)) == 1


def test_more_clicks_in_the_same_burst_do_not_create_more_signals(
    api: TestClient, connection: Connection
) -> None:
    session_id = _new_session()

    _post_all(api, _clicks(session_id, [i * 0.3 for i in range(10)]))

    assert len(_signals(connection, session_id)) == 1


def test_different_components_get_separate_signals(api: TestClient, connection: Connection) -> None:
    session_id = _new_session()

    _post_all(api, _clicks(session_id, RAPID, "a") + _clicks(session_id, RAPID, "b"))

    signals = _signals(connection, session_id)
    assert sorted(s.evidence["component"] for s in signals) == ["a", "b"]


def test_sessions_are_never_merged(api: TestClient, connection: Connection) -> None:
    first, second = _new_session(), _new_session()

    # Two clicks each: together they would reach the threshold; apart they don't.
    _post_all(api, _clicks(first, [0.0, 0.2]) + _clicks(second, [0.4, 0.6]))

    assert _signals(connection, first) == []
    assert _signals(connection, second) == []


def test_below_threshold_creates_no_signal(api: TestClient, connection: Connection) -> None:
    session_id = _new_session()

    _post_all(api, _clicks(session_id, RAPID[: RAGE_CLICK_THRESHOLD - 1]))
    _post_all(api, _clicks(session_id, [30.0, 40.0, 50.0, 60.0]))  # enough clicks, too slow

    assert _signals(connection, session_id) == []


def test_out_of_order_arrival_is_detected_by_occurred_at(
    api: TestClient, connection: Connection
) -> None:
    session_id = _new_session()
    bodies = _clicks(session_id, RAPID)
    arrival_order = [bodies[2], bodies[0], bodies[3], bodies[1]]

    _post_all(api, arrival_order)

    [signal] = _signals(connection, session_id)
    # Evidence is in user-time order, not arrival order.
    assert signal.evidence["event_ids"] == [b["event_id"] for b in bodies]


# ---- Error burst --------------------------------------------------------------------


def test_error_burst_is_persisted(api: TestClient, connection: Connection) -> None:
    session_id = _new_session()
    bodies = [
        _event(session_id, 0.0, "client_error", {}),
        _event(session_id, 3.0, "form_error", {"field": "email"}),
        _event(session_id, 6.0, "client_error", {}),
    ]

    _post_all(api, bodies)

    [signal] = _signals(connection, session_id)
    assert signal.signal_type == "error_burst"
    assert signal.evidence["count"] == ERROR_BURST_THRESHOLD
    assert signal.evidence["event_types"] == ["client_error", "form_error"]
    assert signal.evidence["event_ids"] == [b["event_id"] for b in bodies]
    # Evidence is compact: no payload content is copied into it.
    assert "email" not in repr(signal.evidence)


# ---- Robustness -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [{}, {"component": 42}, {"component": ["x"]}, {"component": "has spaces and @"}],
)
def test_clicks_without_usable_component_are_stored_but_not_detected(
    api: TestClient, connection: Connection, payload: dict[str, Any]
) -> None:
    session_id = _new_session()
    bodies = [_event(session_id, t, payload=payload) for t in RAPID]

    assert _post_all(api, bodies) == [202] * len(bodies)

    stored = connection.scalar(
        select(func.count())
        .select_from(UserEvent)
        .where(UserEvent.session_id == uuid.UUID(session_id))
    )
    assert stored == len(bodies)
    assert _signals(connection, session_id) == []


def test_detector_crash_after_storage_does_not_lose_the_event(
    api: TestClient, connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_detection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("detector bug")

    monkeypatch.setattr("darwin.telemetry.service.reconcile_session_signals", broken_detection)
    body = _event(_new_session(), 0.0)

    response = api.post(URL, json=body)

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    stored = connection.scalar(
        select(func.count())
        .select_from(UserEvent)
        .where(UserEvent.event_id == uuid.UUID(body["event_id"]))
    )
    assert stored == 1


def test_signal_insert_failure_does_not_lose_the_event(
    api: TestClient, connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failure *inside* the detection transaction (after it wrote nothing yet).
    def failing_store(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("signal insert failed")

    monkeypatch.setattr(signal_service, "_insert_or_revive", failing_store)
    session_id = _new_session()
    bodies = _clicks(session_id, RAPID)

    assert _post_all(api, bodies) == [202] * len(bodies)
    stored = connection.scalar(
        select(func.count())
        .select_from(UserEvent)
        .where(UserEvent.session_id == uuid.UUID(session_id))
    )
    assert stored == len(bodies)
    assert _signals(connection, session_id) == []


# ---- Replay ----------------------------------------------------------------------------


def test_replaying_detection_over_stored_history_is_idempotent(
    api: TestClient, connection: Connection
) -> None:
    session_id = _new_session()
    _post_all(
        api,
        _clicks(session_id, RAPID)
        + _clicks(session_id, [100.0, 100.5, 101.0, 101.5], "other")
        + [_event(session_id, 200.0 + i, "client_error", {}) for i in range(3)],
    )
    before = _signals(connection, session_id)
    before_total = connection.scalar(select(func.count()).select_from(BehaviorSignal))

    # Re-run reconciliation twice over the whole stored history for this session.
    results = []
    for _ in range(2):
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            results.append(signal_service.reconcile_session_signals(session, uuid.UUID(session_id)))

    after = _signals(connection, session_id)
    assert [(r.canonical, r.created, r.revived, r.superseded) for r in results] == [
        (3, 0, 0, 0),
        (3, 0, 0, 0),
    ]
    assert [s.signal_id for s in after] == [s.signal_id for s in before]
    assert [s.evidence for s in after] == [s.evidence for s in before]
    assert connection.scalar(select(func.count()).select_from(BehaviorSignal)) == before_total


def test_duplicate_signal_ids_are_rejected_by_the_database(connection: Connection) -> None:
    constraints = inspect(connection).get_unique_constraints("behavior_signal")

    assert [c["column_names"] for c in constraints] == [["signal_id"]]
