"""Exposure recording: telemetry `experiment_exposure` event -> experiment_exposure row.

Runs in the telemetry worker right after the raw event is stored (same
pipeline, no new queue). ASSIGNED is not EXPOSED: the browser sends this event
only after the assigned variant rendered successfully, and it counts only if:

  the payload is exactly {experiment, variant, spec_hash, generation};
  the experiment exists and is RUNNING when the event is processed (a delayed or
  redelivered exposure processed while paused, stopped or completed is refused);
  the exposure's time lies inside one of the experiment's collection windows;
  the variant equals the deterministic assignment for (experiment, session);
  the spec hash equals the experiment's hash for that variant.

Otherwise the event stays a raw user_event and is NOT an exposure (the
analysis counts such sessions as `rejected_exposures`). Recording is
idempotent: UNIQUE(experiment_id, session_id) + ON CONFLICT DO NOTHING, so a
repeated or redelivered exposure never inflates the sample.

Logs: experiment key, variant, outcome, reason — never the session id.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from darwin.db.models import Experiment, ExperimentExposure
from darwin.telemetry.schemas import TelemetryEvent

from .analysis import lifecycle_steps
from .assignment import AllocationError, assign
from .vocabulary import EXPERIMENT_KEY_PATTERN, SPEC_HASH_PATTERN, VARIANTS
from .windows import LifecycleHistoryError, collection_windows, window_containing

logger = logging.getLogger(__name__)

_KEY = re.compile(EXPERIMENT_KEY_PATTERN)
_HASH = re.compile(SPEC_HASH_PATTERN)
_PAYLOAD_KEYS = {"experiment", "variant", "spec_hash", "generation"}
_OPEN_END = datetime.max.replace(tzinfo=UTC)  # the current window has not closed yet


@dataclass(frozen=True)
class ExposureResult:
    status: Literal["recorded", "duplicate", "rejected"]
    reason: str | None = None


def _reject(reason: str, key: str | None = None) -> ExposureResult:
    logger.info(
        "experiment exposure rejected",
        extra={"context": {"experiment_key": key, "reason": reason}},
    )
    return ExposureResult("rejected", reason)


def record_exposure(session: Session, event: TelemetryEvent) -> ExposureResult:
    """Validate and store one exposure. Owns its transaction. Never raises on bad input."""
    payload = event.payload
    key, variant, spec_hash = (
        payload.get("experiment"),
        payload.get("variant"),
        payload.get("spec_hash"),
    )
    if (
        set(payload) != _PAYLOAD_KEYS
        or not isinstance(key, str)
        or not _KEY.fullmatch(key)
        or variant not in VARIANTS
        or not isinstance(spec_hash, str)
        or not _HASH.fullmatch(spec_hash)
    ):
        return _reject("payload_invalid")

    with session.begin():
        experiment = session.scalar(select(Experiment).where(Experiment.experiment_key == key))
        if experiment is None:
            return _reject("experiment_unknown", key)
        if experiment.status != "running":
            return _reject("experiment_not_running", key)
        try:
            windows = collection_windows(lifecycle_steps(session, experiment.id), _OPEN_END)
        except LifecycleHistoryError:
            return _reject("lifecycle_history_invalid", key)
        if window_containing(windows, event.occurred_at) is None:
            return _reject("exposure_outside_collection_window", key)
        try:
            assigned = assign(key, event.session_id, experiment.candidate_allocation_bp)
        except AllocationError:
            return _reject("assignment_error", key)
        if variant != assigned:
            return _reject("variant_mismatch", key)
        expected_hash = (
            experiment.candidate_spec_hash
            if variant == "candidate"
            else experiment.control_spec_hash
        )
        if spec_hash != expected_hash:
            return _reject("spec_hash_mismatch", key)
        inserted = session.execute(
            insert(ExperimentExposure)
            .values(
                experiment_id=experiment.id,
                session_id=event.session_id,
                variant=variant,
                spec_hash=spec_hash,
                event_id=event.event_id,
                exposed_at=event.occurred_at,
            )
            .on_conflict_do_nothing(constraint="uq_experiment_exposure_session")
            .returning(ExperimentExposure.id)
        ).scalar_one_or_none()

    result = ExposureResult("recorded" if inserted is not None else "duplicate")
    logger.info(
        "experiment exposure",
        extra={"context": {"experiment_key": key, "variant": variant, "status": result.status}},
    )
    return result
