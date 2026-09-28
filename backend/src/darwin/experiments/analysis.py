"""Aggregation from the database: exposures and session-level outcomes per variant.

assignment != exposure != outcome attribution. Only evidence generated INSIDE an
active collection window (a `running` interval from the immutable lifecycle
history, see windows.py) contributes. Counting rules (experiment_analysis.v1):

- Exposure: an experiment_exposure row (recorded only while running) counts if
  it reached DarwinUX by `as_of` (recorded_at) and its exposed_at lies inside
  a collection window. A row outside every window is excluded and reported
  (`exposures_outside_windows`, needs review).
- Event outcome (user_event, by event_type and payload.component) counts for
  an exposed session if occurred_at >= exposed_at, occurred_at lies inside a
  collection window W, the event arrived no earlier than W opened
  (received_at >= W.opened_at, server clock) and by as_of.
- Signal outcome (canonical behavior_signal, by signal_type) counts if its
  WHOLE evidence interval [window_start, window_end] lies inside ONE
  collection window W, window_start >= exposed_at, and it was detected no
  earlier than W opened and by as_of. A signal whose evidence straddles a
  pause/resume/terminal boundary cannot be attributed and is NOT counted
  (fail closed); it is reported as `boundary_signals_excluded`.
- Integrity counts: render fallbacks and refused exposure events that happened
  inside a collection window, stored exposures whose variant/hash no longer
  match a fresh assignment.

Outcomes during a pause, or after stop/complete, lie outside every window and
never count. With the same as_of, a rerun reads exactly the same rows (the
history is immutable and everything is filtered by arrival <= as_of).

Only aggregate counts leave this module — never session ids or payloads.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, false, func, or_, select
from sqlalchemy.orm import Session

from darwin.db.models import (
    BehaviorSignal,
    Experiment,
    ExperimentExposure,
    ExperimentLifecycleEvent,
    UserEvent,
)

from .assignment import assign
from .policy import ArmCounts, Integrity
from .vocabulary import (
    EXPOSURE_EVENT,
    FALLBACK_EVENT,
    METRICS,
    MetricDefinition,
    MetricName,
    Variant,
)
from .windows import LifecycleStep, Window, collection_windows


def lifecycle_steps(session: Session, experiment_id: uuid.UUID) -> list[LifecycleStep]:
    rows = session.scalars(
        select(ExperimentLifecycleEvent)
        .where(ExperimentLifecycleEvent.experiment_id == experiment_id)
        .order_by(ExperimentLifecycleEvent.sequence)
    ).all()
    # UTC, so reports (and their hashes) never depend on the connection's time zone.
    return [
        LifecycleStep(r.sequence, r.from_status, r.to_status, r.occurred_at.astimezone(UTC))
        for r in rows
    ]


def _any(conditions: Sequence[ColumnElement[bool]]) -> ColumnElement[bool]:
    return or_(*conditions) if conditions else false()


def _inside(column: Any, windows: Sequence[Window]) -> ColumnElement[bool]:
    return _any([and_(column >= w.opened_at, column < w.closed_at) for w in windows])


def _signal_parts(
    definition: MetricDefinition, windows: Sequence[Window], as_of: datetime
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    s, e = BehaviorSignal, ExperimentExposure
    base = and_(
        s.session_id == e.session_id,
        s.signal_type == definition.signal_type,
        s.superseded_at.is_(None),
        s.window_start >= e.exposed_at,
        s.detected_at <= as_of,
    )
    inside_one = _any(
        [
            and_(
                s.window_start >= w.opened_at,
                s.window_end < w.closed_at,
                s.detected_at >= w.opened_at,
            )
            for w in windows
        ]
    )
    return base, inside_one


def _outcome(
    definition: MetricDefinition, windows: Sequence[Window], as_of: datetime
) -> ColumnElement[bool]:
    if definition.source == "signal":
        base, inside_one = _signal_parts(definition, windows, as_of)
        return exists().where(base, inside_one)
    u, e = UserEvent, ExperimentExposure
    in_window = _any(
        [
            and_(
                u.occurred_at >= w.opened_at,
                u.occurred_at < w.closed_at,
                u.received_at >= w.opened_at,
            )
            for w in windows
        ]
    )
    return exists().where(
        u.session_id == e.session_id,
        u.event_type == definition.event_type,
        u.payload["component"].astext == definition.component,
        u.occurred_at >= e.exposed_at,
        u.received_at <= as_of,
        in_window,
    )


def _boundary_signals(
    session: Session,
    counted: ColumnElement[bool],
    definition: MetricDefinition,
    windows: Sequence[Window],
    as_of: datetime,
) -> int:
    """Signals after exposure whose evidence touches a window but is not inside one."""
    s = BehaviorSignal
    base, inside_one = _signal_parts(definition, windows, as_of)
    touches = _any(
        [
            or_(
                and_(s.window_start >= w.opened_at, s.window_start < w.closed_at),
                and_(s.window_end >= w.opened_at, s.window_end < w.closed_at),
            )
            for w in windows
        ]
    )
    return int(
        session.scalar(
            select(func.count(func.distinct(s.id))).where(counted, base, touches, ~inside_one)
        )
        or 0
    )


def collect(
    session: Session, experiment: Experiment, metrics: tuple[MetricName, ...], as_of: datetime
) -> tuple[ArmCounts, ArmCounts, Integrity, list[Window], int]:
    """Per-variant counts, integrity counts, the windows used, and excluded boundary signals."""
    windows = collection_windows(lifecycle_steps(session, experiment.id), as_of)
    e = ExperimentExposure
    counted = and_(
        e.experiment_id == experiment.id, e.recorded_at <= as_of, _inside(e.exposed_at, windows)
    )
    exposed: dict[str, int] = {"control": 0, "candidate": 0}
    mismatches = 0
    for row in session.scalars(select(e).where(counted)):
        exposed[row.variant] += 1
        expected_hash = (
            experiment.candidate_spec_hash
            if row.variant == "candidate"
            else experiment.control_spec_hash
        )
        if (
            assign(experiment.experiment_key, row.session_id, experiment.candidate_allocation_bp)
            != row.variant
            or row.spec_hash != expected_hash
        ):
            mismatches += 1
    outside = session.scalar(
        select(func.count()).where(
            e.experiment_id == experiment.id,
            e.recorded_at <= as_of,
            ~_inside(e.exposed_at, windows),
        )
    )

    successes: dict[str, dict[MetricName, int]] = {"control": {}, "candidate": {}}
    boundary_signals = 0
    for metric in metrics:
        definition = METRICS[metric]
        rows = session.execute(
            select(e.variant, func.count(func.distinct(e.session_id)))
            .where(counted, _outcome(definition, windows, as_of))
            .group_by(e.variant)
        ).all()
        counts = {variant: n for variant, n in rows}
        for variant in ("control", "candidate"):
            successes[variant][metric] = counts.get(variant, 0)
        if definition.source == "signal":
            boundary_signals += _boundary_signals(session, counted, definition, windows, as_of)

    # Integrity events count only if they happened inside a collection window: anything
    # outside (e.g. an exposure refused because it happened while paused) is expected.
    key_matches = and_(
        UserEvent.payload["experiment"].astext == experiment.experiment_key,
        _inside(UserEvent.occurred_at, windows),
    )
    fallback_sessions: Sequence[uuid.UUID] = session.scalars(
        select(func.distinct(UserEvent.session_id)).where(
            UserEvent.event_type == FALLBACK_EVENT, key_matches, UserEvent.received_at <= as_of
        )
    ).all()
    fallbacks: dict[Variant, int] = {"control": 0, "candidate": 0}
    for session_id in fallback_sessions:
        sid = uuid.UUID(str(session_id))
        fallbacks[assign(experiment.experiment_key, sid, experiment.candidate_allocation_bp)] += 1

    recorded = exists().where(
        and_(
            e.experiment_id == experiment.id,
            e.session_id == UserEvent.session_id,
            e.variant == UserEvent.payload["variant"].astext,
            e.spec_hash == UserEvent.payload["spec_hash"].astext,
        )
    )
    rejected = session.scalar(
        select(func.count(func.distinct(UserEvent.session_id))).where(
            UserEvent.event_type == EXPOSURE_EVENT,
            key_matches,
            UserEvent.received_at <= as_of,
            ~recorded,
        )
    )
    return (
        ArmCounts(exposed["control"], successes["control"]),
        ArmCounts(exposed["candidate"], successes["candidate"]),
        Integrity(fallbacks, int(rejected or 0), mismatches, int(outside or 0)),
        windows,
        boundary_signals,
    )
