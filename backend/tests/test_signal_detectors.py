"""Detector logic, as pure functions over in-memory UserEvent objects (no database)."""

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from darwin.db.models import UserEvent
from darwin.signals.detectors import (
    ERROR_BURST,
    ERROR_BURST_THRESHOLD,
    ERROR_BURST_VERSION,
    ERROR_BURST_WINDOW,
    RAGE_CLICK,
    RAGE_CLICK_THRESHOLD,
    RAGE_CLICK_VERSION,
    RAGE_CLICK_WINDOW,
    derive_signal_id,
    detect_all,
    detect_error_bursts,
    detect_rage_clicks,
)

T0 = datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)
SESSION = uuid.UUID("00000000-0000-4000-8000-00000000aaaa")
OTHER_SESSION = uuid.UUID("00000000-0000-4000-8000-00000000bbbb")


def _event(
    seconds: float,
    event_type: str = "button_click",
    payload: Any = None,
    session_id: uuid.UUID = SESSION,
    n: int | None = None,
) -> UserEvent:
    return UserEvent(
        event_id=uuid.UUID(int=n) if n is not None else uuid.uuid4(),
        event_type=event_type,
        session_id=session_id,
        occurred_at=T0 + timedelta(seconds=seconds),
        payload={"component": "signup_submit"} if payload is None else payload,
    )


def _clicks(times: list[float], **kwargs: Any) -> list[UserEvent]:
    return [_event(t, **kwargs) for t in times]


def _errors(times: list[float], **kwargs: Any) -> list[UserEvent]:
    return [_event(t, event_type="client_error", payload={}, **kwargs) for t in times]


# Four clicks with the first and last exactly at the window edge.
AT_THRESHOLD = [0.0, 0.5, 1.0, RAGE_CLICK_WINDOW.total_seconds()]


# ---- Rage click ------------------------------------------------------------------


def test_rage_click_triggers_at_threshold_inside_the_window() -> None:
    events = _clicks(AT_THRESHOLD)

    [signal] = detect_rage_clicks(events)

    assert signal.signal_type == RAGE_CLICK
    assert signal.detector_version == RAGE_CLICK_VERSION
    assert signal.session_id == SESSION
    assert signal.window_start == T0
    assert signal.window_end == T0 + RAGE_CLICK_WINDOW
    assert signal.evidence == {
        "component": "signup_submit",
        "event_ids": [str(e.event_id) for e in events],
        "count": RAGE_CLICK_THRESHOLD,
        "threshold": RAGE_CLICK_THRESHOLD,
        "window_seconds": RAGE_CLICK_WINDOW.total_seconds(),
    }


def test_rage_click_needs_the_full_threshold() -> None:
    assert detect_rage_clicks(_clicks([0.0, 0.3, 0.6])) == []


def test_rage_click_window_is_inclusive_and_strict_beyond_it() -> None:
    just_outside = [0.0, 0.5, 1.0, RAGE_CLICK_WINDOW.total_seconds() + 0.001]

    assert len(detect_rage_clicks(_clicks(AT_THRESHOLD))) == 1
    assert detect_rage_clicks(_clicks(just_outside)) == []


def test_rage_click_does_not_combine_components() -> None:
    events = _clicks([0.0, 0.2], payload={"component": "a"}) + _clicks(
        [0.4, 0.6], payload={"component": "b"}
    )

    assert detect_rage_clicks(events) == []


def test_rage_click_does_not_combine_sessions() -> None:
    events = _clicks([0.0, 0.2]) + _clicks([0.4, 0.6], session_id=OTHER_SESSION)

    assert detect_rage_clicks(events) == []


def test_each_component_and_session_gets_its_own_signal() -> None:
    events = (
        _clicks(AT_THRESHOLD, payload={"component": "a"})
        + _clicks(AT_THRESHOLD, payload={"component": "b"})
        + _clicks(AT_THRESHOLD, session_id=OTHER_SESSION)
    )

    signals = detect_rage_clicks(events)

    assert len(signals) == 3
    assert len({s.signal_id for s in signals}) == 3


def test_rage_click_ignores_other_event_types() -> None:
    assert detect_rage_clicks(_clicks(AT_THRESHOLD, event_type="page_view")) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing
        {"component": ""},
        {"component": None},
        {"component": 42},  # wrong type
        {"component": ["signup_submit"]},
        {"component": {"id": "x"}},
        {"component": "user@example.com"},  # free text, not an identifier
        {"component": "x" * 129},
    ],
)
def test_events_without_a_usable_component_are_skipped(payload: dict[str, Any]) -> None:
    assert detect_rage_clicks(_clicks(AT_THRESHOLD, payload=payload)) == []


def test_bad_events_do_not_hide_good_ones() -> None:
    events = _clicks(AT_THRESHOLD) + _clicks([0.1, 0.2, 0.3], payload={"component": 7})

    assert len(detect_rage_clicks(events)) == 1


def test_one_long_burst_is_one_signal() -> None:
    # 12 clicks, 0.25 s apart: one frustrated burst, not three signals.
    events = _clicks([i * 0.25 for i in range(12)])

    [signal] = detect_rage_clicks(events)

    assert signal.evidence["event_ids"] == [str(e.event_id) for e in events[:RAGE_CLICK_THRESHOLD]]


def test_separate_bursts_are_separate_signals() -> None:
    events = _clicks(AT_THRESHOLD) + _clicks([60.0, 60.5, 61.0, 61.5])

    assert len(detect_rage_clicks(events)) == 2


def test_the_earliest_qualifying_run_is_the_evidence() -> None:
    # 0 s is too far from the next three; the run 10..11.5 qualifies.
    events = _clicks([0.0, 10.0, 10.5, 11.0, 11.5])

    [signal] = detect_rage_clicks(events)

    assert signal.evidence["event_ids"] == [str(e.event_id) for e in events[1:]]


def test_rage_click_signal_id_is_stable_for_the_same_evidence() -> None:
    events = _clicks(AT_THRESHOLD)

    first = detect_rage_clicks(events)
    second = detect_rage_clicks(list(reversed(events)))

    assert first == second
    assert first[0].signal_id == derive_signal_id(
        RAGE_CLICK, RAGE_CLICK_VERSION, SESSION, "signup_submit", [e.event_id for e in events]
    )


# ---- Error burst -----------------------------------------------------------------


def test_error_burst_triggers_at_threshold() -> None:
    times = [0.0, 4.0, ERROR_BURST_WINDOW.total_seconds()]
    events = _errors(times[:1]) + [
        _event(t, event_type="form_error", payload={}) for t in times[1:]
    ]

    [signal] = detect_error_bursts(events)

    assert signal.signal_type == ERROR_BURST
    assert signal.detector_version == ERROR_BURST_VERSION
    assert signal.evidence == {
        "event_ids": [str(e.event_id) for e in events],
        "count": ERROR_BURST_THRESHOLD,
        "event_types": ["client_error", "form_error"],
        "threshold": ERROR_BURST_THRESHOLD,
        "window_seconds": ERROR_BURST_WINDOW.total_seconds(),
    }


def test_error_burst_needs_the_full_threshold() -> None:
    assert detect_error_bursts(_errors([0.0, 1.0])) == []


def test_error_burst_keeps_sessions_apart() -> None:
    events = _errors([0.0, 1.0]) + _errors([2.0], session_id=OTHER_SESSION)

    assert detect_error_bursts(events) == []


def test_error_burst_outside_the_window_is_not_a_burst() -> None:
    events = _errors([0.0, 5.0, ERROR_BURST_WINDOW.total_seconds() + 0.001])

    assert detect_error_bursts(events) == []


def test_error_burst_ignores_non_error_events_and_needs_no_payload() -> None:
    events = _errors([0.0, 1.0]) + [_event(1.5, event_type="page_view"), *_errors([2.0])]

    [signal] = detect_error_bursts(events)

    assert signal.evidence["count"] == 3


def test_error_burst_signal_id_is_stable() -> None:
    events = _errors([0.0, 1.0, 2.0])

    assert detect_error_bursts(events) == detect_error_bursts(list(reversed(events)))
    assert detect_error_bursts(events)[0].signal_id == derive_signal_id(
        ERROR_BURST, ERROR_BURST_VERSION, SESSION, "", [e.event_id for e in events]
    )


# ---- Determinism and identity -----------------------------------------------------


def test_same_events_in_any_arrival_order_give_identical_candidates() -> None:
    events = (
        _clicks(AT_THRESHOLD)
        + _clicks([30.0, 30.2, 30.4, 30.6], payload={"component": "b"})
        + _errors([5.0, 6.0, 7.0])
        + _clicks([0.1, 0.2], session_id=OTHER_SESSION)
    )
    shuffled = list(events)
    random.Random(1234).shuffle(shuffled)

    assert detect_all(events) == detect_all(shuffled)
    assert len(detect_all(events)) == 3


def test_ties_in_occurred_at_are_broken_deterministically() -> None:
    same_time = [_event(0.0, n=i) for i in (4, 2, 3, 1)]

    [signal] = detect_rage_clicks(same_time)

    assert signal.evidence["event_ids"] == [str(uuid.UUID(int=i)) for i in (1, 2, 3, 4)]


def test_signal_id_depends_on_every_identity_part() -> None:
    ids = [uuid.UUID(int=i) for i in range(4)]
    base = derive_signal_id(RAGE_CLICK, "1", SESSION, "a", ids)

    assert derive_signal_id(RAGE_CLICK, "2", SESSION, "a", ids) != base  # detector version
    assert derive_signal_id(ERROR_BURST, "1", SESSION, "a", ids) != base  # signal type
    assert derive_signal_id(RAGE_CLICK, "1", OTHER_SESSION, "a", ids) != base  # session
    assert derive_signal_id(RAGE_CLICK, "1", SESSION, "b", ids) != base  # component
    assert derive_signal_id(RAGE_CLICK, "1", SESSION, "a", ids[:3]) != base  # evidence
    assert derive_signal_id(RAGE_CLICK, "1", SESSION, "a", list(reversed(ids))) == base


def test_signal_ids_are_stable_across_processes() -> None:
    # A hard-coded expectation: if this changes, every stored signal would get
    # a new identity and replay idempotency would break.
    ids = [uuid.UUID(int=i) for i in range(4)]

    assert derive_signal_id(RAGE_CLICK, "1", SESSION, "a", ids) == uuid.uuid5(
        uuid.UUID("9319399b-1d65-42ab-a519-78f9d0cb0b51"),
        f"rage_click|v1|{SESSION}|a|" + ",".join(str(i) for i in ids),
    )


def test_detectors_do_not_modify_events() -> None:
    events = _clicks(AT_THRESHOLD)
    before = [(e.event_id, e.occurred_at, dict(e.payload)) for e in events]

    detect_all(events)

    assert [(e.event_id, e.occurred_at, dict(e.payload)) for e in events] == before


def test_in_chronological_arrival_every_prefix_result_is_part_of_the_final_result() -> None:
    # When events arrive in user-time order, no signal detected along the way
    # ever stops being valid: the union of per-prefix results equals the final one.
    events = sorted(
        _clicks([i * 0.25 for i in range(9)])  # one long burst
        + _clicks([20.0, 20.5, 21.0, 21.5])  # a second burst
        + _errors([3.0, 4.0, 5.0, 6.0, 7.0])
        + _clicks([40.0, 45.0, 50.0, 55.0]),  # too slow: never a signal
        key=lambda e: e.occurred_at,
    )

    incremental = {c.signal_id: c for i in range(len(events)) for c in detect_all(events[: i + 1])}
    replay = {c.signal_id: c for c in detect_all(events)}

    assert incremental == replay
    assert len(replay) == 3


@pytest.mark.parametrize(
    ("label", "detector", "make", "times", "late"),
    [
        ("rage before", detect_rage_clicks, _clicks, [10.0, 10.5, 11.0, 11.5], 9.6),
        ("rage inside", detect_rage_clicks, _clicks, [10.0, 10.5, 11.0, 11.5], 10.2),
        ("rage bridge", detect_rage_clicks, _clicks, [0, 0.3, 0.6, 0.9, 4.0, 4.3, 4.6, 4.9], 2.4),
        ("errors before", detect_error_bursts, _errors, [100.0, 103.0, 106.0], 95.0),
        ("errors inside", detect_error_bursts, _errors, [100.0, 103.0, 106.0], 101.0),
        ("errors bridge", detect_error_bursts, _errors, [0.0, 2.0, 4.0, 20.0, 22.0, 24.0], 12.0),
    ],
)
def test_a_late_event_can_invalidate_an_earlier_result(
    label: str, detector: Any, make: Any, times: list[float], late: float
) -> None:
    # Why the service must *reconcile*, not just insert: a signal that was
    # correct for the history-so-far is not produced for the grown history.
    # Append-only persistence would therefore keep an extra, overlapping signal.
    before_late = make(times)
    final = before_late + make([late])

    earlier = {c.signal_id for c in detector(before_late)}
    canonical = {c.signal_id for c in detector(final)}

    assert earlier - canonical, f"{label}: expected a stale signal"
    assert len(canonical) == 1, f"{label}: one episode, one canonical signal"


def test_a_late_event_after_the_run_changes_nothing() -> None:
    before = _clicks([10.0, 10.5, 11.0, 11.5])
    after = [*before, _event(11.8)]  # lands after the detected run, inside the burst

    assert len(detect_rage_clicks(before)) == 1
    assert detect_rage_clicks(after) == detect_rage_clicks(before)
