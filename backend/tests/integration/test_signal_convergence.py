"""Convergence: whatever order events arrive in, the canonical signals equal a full replay.

Invariant under test (see darwin/signals/service.py):

    canonical signals of a session  ==  detect_all(the session's final event history)

Each case posts the same final history through the real API in a different
arrival order, then compares the stored canonical set with detect_all() over
the events actually stored.
"""

import random
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, func, insert, select, text, update
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, UserEvent
from darwin.signals import service as signal_service
from darwin.signals.detectors import detect_all

pytestmark = pytest.mark.integration

URL = "/api/v1/telemetry/events"
T0 = datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)
EventKind = tuple[str, dict[str, Any]]
CLICK: EventKind = ("button_click", {"component": "signup_submit"})
ERROR: EventKind = ("client_error", {})


def _bodies(
    session_id: str, kind: tuple[str, dict[str, Any]], times: list[float]
) -> dict[float, Any]:
    event_type, payload = kind
    return {
        t: {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "session_id": session_id,
            "occurred_at": (T0 + timedelta(seconds=t)).isoformat(),
            "payload": payload,
        }
        for t in times
    }


def _session_state(
    connection: Connection, session_id: str
) -> tuple[set[uuid.UUID], int, set[uuid.UUID]]:
    """(canonical signal_ids, superseded row count, replay signal_ids) for one session."""
    sid = uuid.UUID(session_id)
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        rows = session.scalars(select(BehaviorSignal).where(BehaviorSignal.session_id == sid)).all()
        events = session.scalars(select(UserEvent).where(UserEvent.session_id == sid)).all()
        replay = {c.signal_id for c in detect_all(events)}
    canonical = {r.signal_id for r in rows if r.superseded_at is None}
    superseded = sum(1 for r in rows if r.superseded_at is not None)
    return canonical, superseded, replay


def _assert_no_overlap(connection: Connection, session_id: str) -> None:
    """No event is evidence for two canonical signals of the same type and scope."""
    rows = connection.execute(
        select(BehaviorSignal.signal_type, BehaviorSignal.evidence).where(
            BehaviorSignal.session_id == uuid.UUID(session_id),
            BehaviorSignal.superseded_at.is_(None),
        )
    ).all()
    seen: set[tuple[str, str | None, str]] = set()
    for signal_type, evidence in rows:
        for event_id in evidence["event_ids"]:
            key = (signal_type, evidence.get("component"), event_id)
            assert key not in seen, "one event backs two canonical signals"
            seen.add(key)


# (label, kind, final history, arrival order) — the late event is last to arrive.
RAGE = [10.0, 10.5, 11.0, 11.5]
ERRS = [100.0, 103.0, 106.0]
CASES = [
    ("rage chronological", CLICK, RAGE, RAGE, 0),
    ("rage out of order", CLICK, RAGE, [11.0, 10.0, 11.5, 10.5], 0),
    ("rage late before", CLICK, [9.6, *RAGE], [*RAGE, 9.6], 1),
    ("rage late inside", CLICK, [*RAGE, 10.2], [*RAGE, 10.2], 1),
    ("rage late after", CLICK, [*RAGE, 11.8], [*RAGE, 11.8], 0),
    (
        "rage late bridge (two bursts become one)",
        CLICK,
        [0.0, 0.3, 0.6, 0.9, 2.4, 4.0, 4.3, 4.6, 4.9],
        [0.0, 0.3, 0.6, 0.9, 4.0, 4.3, 4.6, 4.9, 2.4],
        1,
    ),
    ("errors chronological", ERROR, ERRS, ERRS, 0),
    ("errors out of order", ERROR, ERRS, [106.0, 100.0, 103.0], 0),
    ("errors late before", ERROR, [95.0, *ERRS], [*ERRS, 95.0], 1),
    ("errors late inside", ERROR, [*ERRS, 101.0], [*ERRS, 101.0], 1),
    ("errors late after", ERROR, [*ERRS, 108.0], [*ERRS, 108.0], 0),
    (
        "errors late bridge (two bursts become one)",
        ERROR,
        [0.0, 2.0, 4.0, 12.0, 20.0, 22.0, 24.0],
        [0.0, 2.0, 4.0, 20.0, 22.0, 24.0, 12.0],
        1,
    ),
]


@pytest.mark.parametrize(
    ("label", "kind", "history", "arrival", "expected_superseded"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_arrival_order_never_changes_the_canonical_signals(
    api: TestClient,
    connection: Connection,
    label: str,
    kind: tuple[str, dict[str, Any]],
    history: list[float],
    arrival: list[float],
    expected_superseded: int,
) -> None:
    assert sorted(arrival) == sorted(history), "same final history"
    session_id = str(uuid.uuid4())
    bodies = _bodies(session_id, kind, history)

    statuses = [api.post(URL, json=bodies[t]).status_code for t in arrival]

    assert statuses == [202] * len(arrival)
    canonical, superseded, replay = _session_state(connection, session_id)
    assert canonical == replay, label
    assert len(canonical) == 1, "one behavioural episode, one canonical signal"
    assert superseded == expected_superseded  # replaced signals are kept, not deleted
    _assert_no_overlap(connection, session_id)


@pytest.mark.parametrize("seed", range(8))
def test_any_permutation_of_a_mixed_history_converges(
    api: TestClient, connection: Connection, seed: int
) -> None:
    session_id = str(uuid.uuid4())
    bodies = {
        **_bodies(session_id, CLICK, [0.0, 0.3, 0.6, 0.9, 2.4, 4.0, 4.3, 4.6, 4.9, 30.0, 30.5]),
        **_bodies(session_id, ("click", {"component": "nav.menu"}), [50.0, 50.4, 50.8, 51.2]),
        **_bodies(session_id, ERROR, [95.0, 100.0, 101.0, 103.0, 106.0, 200.0]),
    }
    arrival = list(bodies)
    random.Random(seed).shuffle(arrival)

    for t in arrival:
        assert api.post(URL, json=bodies[t]).status_code == 202

    canonical, _, replay = _session_state(connection, session_id)
    assert canonical == replay
    _assert_no_overlap(connection, session_id)


def test_a_chain_longer_than_any_window_is_still_one_burst(
    api: TestClient, connection: Connection
) -> None:
    """A burst can chain for any length of time (every gap <= 2 s).

    The earlier ±5-minute detection window saw only the tail of this chain and
    produced a second signal for the fast run at its end. Detection over the
    full session history does not.
    """
    session_id = str(uuid.uuid4())
    fast_start = [0.0, 0.5, 1.0, 1.5]
    chain = [1.5 + 1.9 * k for k in range(1, 380)]  # ~12 minutes of 1.9 s gaps
    fast_end = [chain[-1] + 0.5 * k for k in range(1, 5)]
    bodies = _bodies(session_id, CLICK, fast_start + chain + fast_end)

    # Store the long middle directly (fast), then send the ends through the API.
    connection.execute(
        insert(UserEvent),
        [
            {
                "event_id": uuid.UUID(bodies[t]["event_id"]),
                "event_type": "button_click",
                "session_id": uuid.UUID(session_id),
                "occurred_at": T0 + timedelta(seconds=t),
                "payload": {"component": "signup_submit"},
            }
            for t in chain
        ],
    )
    for t in fast_start + fast_end:
        assert api.post(URL, json=bodies[t]).status_code == 202

    canonical, _, replay = _session_state(connection, session_id)
    assert canonical == replay
    assert len(canonical) == 1


def test_a_superseded_signal_that_is_canonical_again_is_revived(
    api: TestClient, connection: Connection
) -> None:
    # E.g. a detector version rolled back, or a manual correction: reconciliation
    # restores the stored state to exactly the canonical set.
    session_id = str(uuid.uuid4())
    bodies = _bodies(session_id, CLICK, RAGE)
    for t in RAGE:
        api.post(URL, json=bodies[t])
    connection.execute(
        update(BehaviorSignal)
        .where(BehaviorSignal.session_id == uuid.UUID(session_id))
        .values(superseded_at=func.now())
    )

    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        result = signal_service.reconcile_session_signals(session, uuid.UUID(session_id))

    assert (result.canonical, result.created, result.revived, result.superseded) == (1, 0, 1, 0)
    canonical, superseded, replay = _session_state(connection, session_id)
    assert canonical == replay
    assert superseded == 0


def test_reconciliation_only_touches_its_own_session(
    api: TestClient, connection: Connection
) -> None:
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    for session_id in (mine, other):
        bodies = _bodies(session_id, CLICK, RAGE)
        for t in RAGE:
            api.post(URL, json=bodies[t])
    other_before = _session_state(connection, other)

    late = _bodies(mine, CLICK, [9.6])[9.6]  # supersedes a signal in `mine` only
    api.post(URL, json=late)

    assert _session_state(connection, mine)[1] == 1
    assert _session_state(connection, other) == other_before


def test_a_very_long_session_is_fully_reconciled(connection: Connection) -> None:
    """More than 10,000 relevant events: reconciliation still runs on the full history.

    A signal that was canonical for the early history is invalidated by a late
    event; it must not remain canonical just because the session is long.
    """
    session_id = uuid.uuid4()

    def rows(times: list[float], event_type: str = "button_click") -> list[dict[str, Any]]:
        return [
            {
                "event_id": uuid.uuid4(),
                "event_type": event_type,
                "session_id": session_id,
                "occurred_at": T0 + timedelta(seconds=t),
                "payload": {"component": "signup_submit"} if event_type != "client_error" else {},
            }
            for t in times
        ]

    def reconcile() -> signal_service.ReconcileResult:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            return signal_service.reconcile_session_signals(session, session_id)

    # 1. A short history with one canonical rage click.
    connection.execute(insert(UserEvent), rows(RAGE))
    assert reconcile().created == 1
    stale = connection.scalar(
        select(BehaviorSignal.signal_id).where(BehaviorSignal.session_id == session_id)
    )

    # 2. The session grows past 10,000 relevant events, including a late click
    #    before the burst (invalidates the stored signal), 10,000 isolated clicks
    #    (3 s apart: never a burst), one more burst, and an error burst.
    isolated = [100.0 + 3.0 * k for k in range(10_000)]
    last_burst = [isolated[-1] + 5.0 + 0.3 * k for k in range(4)]
    connection.execute(insert(UserEvent), rows([9.6, *isolated, *last_burst]))
    connection.execute(insert(UserEvent), rows([50.0, 52.0, 54.0], "client_error"))
    relevant = connection.scalar(
        select(func.count()).select_from(UserEvent).where(UserEvent.session_id == session_id)
    )
    assert relevant is not None and relevant > 10_000

    result = reconcile()

    canonical, superseded, replay = _session_state(connection, str(session_id))
    assert canonical == replay  # computed over the complete history
    assert len(canonical) == 3  # corrected first burst, last burst, error burst
    assert stale not in canonical  # the invalidated signal is no longer canonical
    assert superseded == 1
    assert (result.canonical, result.created, result.superseded) == (3, 3, 1)
    assert reconcile() == signal_service.ReconcileResult(3, 0, 0, 0)  # and idempotent


# ---- Concurrency (real commits on separate connections; cleaned up by session_id) -----


def _commit_events(
    engine: Engine, session_id: uuid.UUID, kind: tuple[str, Any], times: list[float]
) -> None:
    event_type, payload = kind
    with engine.begin() as conn:
        conn.execute(
            insert(UserEvent),
            [
                {
                    "event_id": uuid.uuid4(),
                    "event_type": event_type,
                    "session_id": session_id,
                    "occurred_at": T0 + timedelta(seconds=t),
                    "payload": payload,
                }
                for t in times
            ],
        )


def _cleanup(engine: Engine, session_id: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(delete(BehaviorSignal).where(BehaviorSignal.session_id == session_id))
        conn.execute(delete(UserEvent).where(UserEvent.session_id == session_id))


def test_concurrent_reconciliations_of_one_session_converge(migrated_engine: Engine) -> None:
    session_id = uuid.uuid4()
    try:
        _commit_events(migrated_engine, session_id, CLICK, RAGE)
        with Session(migrated_engine) as session:
            signal_service.reconcile_session_signals(session, session_id)
        # A late event arrives; many reconcilers race on the same session.
        _commit_events(migrated_engine, session_id, CLICK, [9.6])
        errors: list[BaseException] = []

        def reconcile() -> None:
            try:
                with Session(migrated_engine) as session:
                    signal_service.reconcile_session_signals(session, session_id)
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=reconcile) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        with Session(migrated_engine) as session:
            rows = session.scalars(
                select(BehaviorSignal).where(BehaviorSignal.session_id == session_id)
            ).all()
            events = session.scalars(
                select(UserEvent).where(UserEvent.session_id == session_id)
            ).all()
            replay = {c.signal_id for c in detect_all(events)}
        assert {r.signal_id for r in rows if r.superseded_at is None} == replay
        assert len(rows) == 2  # one canonical, one superseded: no duplicates from the race
    finally:
        _cleanup(migrated_engine, session_id)


def test_reconciliation_waits_for_the_session_lock(migrated_engine: Engine) -> None:
    """While one transaction holds the session's advisory lock, another reconciler waits."""
    session_id = uuid.uuid4()
    key = int.from_bytes(session_id.bytes[:8], "big", signed=True)
    finished = threading.Event()
    try:
        _commit_events(migrated_engine, session_id, CLICK, RAGE)
        with migrated_engine.connect() as holder:
            holder.begin()
            holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

            def reconcile() -> None:
                with Session(migrated_engine) as session:
                    signal_service.reconcile_session_signals(session, session_id)
                finished.set()

            thread = threading.Thread(target=reconcile)
            thread.start()
            assert not finished.wait(timeout=0.5), "reconciler must wait for the lock"
            holder.rollback()  # releases the lock
            thread.join(timeout=10)
        assert finished.is_set()
    finally:
        _cleanup(migrated_engine, session_id)
