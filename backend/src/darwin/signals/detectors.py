"""Detectors: pure, deterministic functions from events to signal candidates.

Every detector here answers a counting/timing question with fixed rules. No
model is involved, no clock is read, nothing is random: the same events always
produce the same candidates, with the same signal_ids.

Burst rule shared by the detectors
----------------------------------
Relevant events are sorted by (occurred_at, event_id). The detector looks for
the *earliest* run of `threshold` consecutive events whose first and last
occurred_at are at most `window` apart. That run is the evidence, and its
event ids define the signal's identity. After a hit, the rest of the burst —
every following event within `window` of the previous one — is consumed
without producing another signal, so one frustrated burst is one signal.

Consequences worth knowing:

- A burst has no fixed length: a chain of events each within `window` of the
  previous one is one burst, however long. So the result for any part of a
  session can depend on events far earlier in that session.
- A late-arriving event can change which run is earliest, or merge two
  bursts into one. The *function* stays deterministic — same history, same
  output — but its output for the grown history can differ from the output
  for the smaller one.

Both are why the service always detects over a session's complete history
and reconciles stored signals against the result (signals/service.py).
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from darwin.db.models import UserEvent

# Fixed namespace for UUID5 signal ids. Never change it: doing so would give
# every existing signal a new identity and break replay idempotency.
SIGNAL_ID_NAMESPACE = uuid.UUID("9319399b-1d65-42ab-a519-78f9d0cb0b51")

# ---- Rage click ----------------------------------------------------------------
RAGE_CLICK = "rage_click"
RAGE_CLICK_VERSION = "1"
RAGE_CLICK_EVENT_TYPES = frozenset({"button_click", "click"})
# 4, not 3: a double-click followed by one retry is normal; four clicks on the
# same control inside two seconds is not.
RAGE_CLICK_THRESHOLD = 4
RAGE_CLICK_WINDOW = timedelta(seconds=2)

# ---- Error burst -----------------------------------------------------------------
ERROR_BURST = "error_burst"
ERROR_BURST_VERSION = "1"
ERROR_BURST_EVENT_TYPES = frozenset({"client_error", "form_error"})
ERROR_BURST_THRESHOLD = 3
ERROR_BURST_WINDOW = timedelta(seconds=10)

# Every event type any detector reads. The service loads only these.
DETECTED_EVENT_TYPES = RAGE_CLICK_EVENT_TYPES | ERROR_BURST_EVENT_TYPES

# A component identifier must look like an identifier (e.g. "signup_submit",
# "checkout.pay-button"). Free text — which could carry personal data — is
# ignored and never copied into evidence.
COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class SignalCandidate:
    """A detected pattern, ready to be stored. Not yet persisted."""

    signal_id: uuid.UUID
    signal_type: str
    detector_version: str
    session_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    evidence: dict[str, Any]


def derive_signal_id(
    signal_type: str,
    detector_version: str,
    session_id: uuid.UUID,
    scope: str,
    event_ids: Iterable[uuid.UUID],
) -> uuid.UUID:
    """UUID5 over a canonical text form of everything that defines the signal.

    `scope` separates otherwise-identical signals (the component for rage
    clicks; empty for error bursts). Event ids are sorted, so their order in
    memory never matters. The time window is not included separately: it is
    fully determined by the events.
    """
    canonical = "|".join(
        [
            signal_type,
            f"v{detector_version}",
            str(session_id),
            scope,
            ",".join(sorted(str(event_id) for event_id in event_ids)),
        ]
    )
    return uuid.uuid5(SIGNAL_ID_NAMESPACE, canonical)


def component_of(event: UserEvent) -> str | None:
    """The event's payload["component"] if it is a valid identifier, else None."""
    value = event.payload.get("component") if isinstance(event.payload, dict) else None
    if isinstance(value, str) and COMPONENT_PATTERN.fullmatch(value):
        return value
    return None


def _chronological(events: Iterable[UserEvent]) -> list[UserEvent]:
    # Arrival order is irrelevant; event_id breaks ties deterministically.
    return sorted(events, key=lambda e: (e.occurred_at, str(e.event_id)))


def _bursts(
    events: Sequence[UserEvent], threshold: int, window: timedelta
) -> list[list[UserEvent]]:
    """Earliest qualifying run per burst (see module docstring). `events` must be sorted."""
    hits: list[list[UserEvent]] = []
    i = 0
    while i + threshold <= len(events):
        last = i + threshold - 1
        if events[last].occurred_at - events[i].occurred_at <= window:
            hits.append(list(events[i : last + 1]))
            # Consume the rest of this burst so it cannot trigger again.
            i = last + 1
            while i < len(events) and events[i].occurred_at - events[i - 1].occurred_at <= window:
                i += 1
        else:
            i += 1
    return hits


def detect_rage_clicks(events: Iterable[UserEvent]) -> list[SignalCandidate]:
    """≥ RAGE_CLICK_THRESHOLD clicks on one component, one session, within RAGE_CLICK_WINDOW."""
    groups: defaultdict[tuple[uuid.UUID, str], list[UserEvent]] = defaultdict(list)
    for event in events:
        if event.event_type not in RAGE_CLICK_EVENT_TYPES:
            continue
        component = component_of(event)
        if component is None:
            continue  # no usable component: skip the event, never guess
        groups[(event.session_id, component)].append(event)

    candidates = []
    for (session_id, component), group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        for run in _bursts(_chronological(group), RAGE_CLICK_THRESHOLD, RAGE_CLICK_WINDOW):
            event_ids = [e.event_id for e in run]
            candidates.append(
                SignalCandidate(
                    signal_id=derive_signal_id(
                        RAGE_CLICK, RAGE_CLICK_VERSION, session_id, component, event_ids
                    ),
                    signal_type=RAGE_CLICK,
                    detector_version=RAGE_CLICK_VERSION,
                    session_id=session_id,
                    window_start=run[0].occurred_at,
                    window_end=run[-1].occurred_at,
                    evidence={
                        "component": component,
                        "event_ids": [str(event_id) for event_id in event_ids],
                        "count": len(run),
                        "threshold": RAGE_CLICK_THRESHOLD,
                        "window_seconds": RAGE_CLICK_WINDOW.total_seconds(),
                    },
                )
            )
    return candidates


def detect_error_bursts(events: Iterable[UserEvent]) -> list[SignalCandidate]:
    """≥ ERROR_BURST_THRESHOLD error events in one session within ERROR_BURST_WINDOW."""
    groups: defaultdict[uuid.UUID, list[UserEvent]] = defaultdict(list)
    for event in events:
        if event.event_type in ERROR_BURST_EVENT_TYPES:
            groups[event.session_id].append(event)

    candidates = []
    for session_id, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        for run in _bursts(_chronological(group), ERROR_BURST_THRESHOLD, ERROR_BURST_WINDOW):
            event_ids = [e.event_id for e in run]
            candidates.append(
                SignalCandidate(
                    signal_id=derive_signal_id(
                        ERROR_BURST, ERROR_BURST_VERSION, session_id, "", event_ids
                    ),
                    signal_type=ERROR_BURST,
                    detector_version=ERROR_BURST_VERSION,
                    session_id=session_id,
                    window_start=run[0].occurred_at,
                    window_end=run[-1].occurred_at,
                    evidence={
                        "event_ids": [str(event_id) for event_id in event_ids],
                        "count": len(run),
                        "event_types": sorted({e.event_type for e in run}),
                        "threshold": ERROR_BURST_THRESHOLD,
                        "window_seconds": ERROR_BURST_WINDOW.total_seconds(),
                    },
                )
            )
    return candidates


Detector = Callable[[Iterable[UserEvent]], list[SignalCandidate]]

# Explicit list; adding a detector is a code change reviewed like any other.
DETECTORS: tuple[Detector, ...] = (detect_rage_clicks, detect_error_bursts)


def detect_all(events: Iterable[UserEvent]) -> list[SignalCandidate]:
    materialised = list(events)
    return [candidate for detector in DETECTORS for candidate in detector(materialised)]
