"""Signal detection service: session history -> detectors -> reconciled signals.

Invariant
---------
For every session, the canonical signals (behavior_signal rows with
superseded_at IS NULL) equal ``detect_all(<the session's complete event
history>)`` — whatever order the events arrived in, and however many times
detection has run.

How
---
After each accepted event, ``reconcile_session_signals`` runs in its own
transaction:

1. Take a per-session advisory lock, so two reconciliations of one session
   never interleave (different sessions never wait on each other).
2. Load the session's complete history of detector-relevant events. Not a
   time window: a burst can chain for any length of time, so a window can
   see a truncated burst and produce a signal a full replay would not.
3. Run the pure detectors -> the canonical candidate set.
4. Write the difference against what is stored for that session:
   - canonical and missing   -> INSERT (ON CONFLICT on signal_id)
   - canonical but superseded -> un-supersede (a history can grow back into it)
   - stored but not canonical -> set superseded_at (never deleted)
   Nothing outside this one session is touched.

Windowing is by occurred_at (the user's timeline), never arrival order.

There is deliberately no cap on the history loaded: computing on a truncated
history would not be canonical, and skipping would leave stale rows looking
canonical. Step 5 favours canonical correctness over bounded per-event work;
a later queue/session-finalization worker will improve scaling.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, UserEvent
from darwin.signals.detectors import DETECTED_EVENT_TYPES, SignalCandidate, detect_all

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    canonical: int  # signals the detectors produce for the full history
    created: int  # newly inserted
    revived: int  # previously superseded, canonical again
    superseded: int  # previously canonical, no longer produced


def _lock_session(session: Session, session_id: uuid.UUID) -> None:
    """Transaction-scoped advisory lock keyed on the session id.

    Released automatically at COMMIT/ROLLBACK. Two sessions that happen to
    share a 64-bit key only serialise with each other; correctness is unaffected.
    """
    key = int.from_bytes(session_id.bytes[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def session_history(session: Session, session_id: uuid.UUID) -> list[UserEvent]:
    """The session's complete detector-relevant history, in user-time order."""
    statement = (
        select(UserEvent)
        .where(
            UserEvent.session_id == session_id,
            UserEvent.event_type.in_(DETECTED_EVENT_TYPES),
        )
        .order_by(UserEvent.occurred_at, UserEvent.event_id)
    )
    return list(session.scalars(statement))


def _insert_or_revive(session: Session, candidate: SignalCandidate) -> str:
    """Store a canonical candidate. Returns 'created', 'revived', or 'unchanged'."""
    statement = (
        insert(BehaviorSignal)
        .values(
            signal_id=candidate.signal_id,
            signal_type=candidate.signal_type,
            detector_version=candidate.detector_version,
            session_id=candidate.session_id,
            window_start=candidate.window_start,
            window_end=candidate.window_end,
            evidence=candidate.evidence,
        )
        .on_conflict_do_nothing(index_elements=[BehaviorSignal.signal_id])
        .returning(BehaviorSignal.id)
    )
    if session.execute(statement).scalar_one_or_none() is not None:
        return "created"
    # Same signal_id already stored. Its content cannot differ (the id is
    # derived from the evidence); only its canonical status might.
    revived = session.execute(
        update(BehaviorSignal)
        .where(
            BehaviorSignal.signal_id == candidate.signal_id,
            BehaviorSignal.superseded_at.is_not(None),
        )
        .values(superseded_at=None)
        .returning(BehaviorSignal.id)
    ).scalar_one_or_none()
    return "revived" if revived is not None else "unchanged"


def _log(message: str, signal_id: uuid.UUID, signal_type: str, version: str, events: int) -> None:
    logger.info(
        message,
        extra={
            "context": {
                "signal_id": str(signal_id),
                "signal_type": signal_type,
                "detector_version": version,
                "evidence_events": events,
            }
        },
    )


def reconcile_session_signals(session: Session, session_id: uuid.UUID) -> ReconcileResult:
    """Make the session's canonical signals equal detect_all(its full history).

    Owns its transaction. Deterministic, idempotent, scoped to one session.
    """
    with session.begin():
        _lock_session(session, session_id)

        candidates = detect_all(session_history(session, session_id))
        canonical_ids = [c.signal_id for c in candidates]

        outcomes = {"created": 0, "revived": 0, "unchanged": 0}
        for candidate in candidates:
            outcome = _insert_or_revive(session, candidate)
            outcomes[outcome] += 1
            if outcome != "unchanged":
                _log(
                    f"behavior signal {outcome}",
                    candidate.signal_id,
                    candidate.signal_type,
                    candidate.detector_version,
                    len(candidate.evidence.get("event_ids", [])),
                )

        superseded = session.execute(
            update(BehaviorSignal)
            .where(
                BehaviorSignal.session_id == session_id,
                BehaviorSignal.superseded_at.is_(None),
                BehaviorSignal.signal_id.not_in(canonical_ids),
            )
            .values(superseded_at=func.now())
            .returning(
                BehaviorSignal.signal_id,
                BehaviorSignal.signal_type,
                BehaviorSignal.detector_version,
            )
        ).all()
        for signal_id, signal_type, version in superseded:
            _log("behavior signal superseded", signal_id, signal_type, version, 0)

    return ReconcileResult(
        canonical=len(candidates),
        created=outcomes["created"],
        revived=outcomes["revived"],
        superseded=len(superseded),
    )
