"""Variant serving: which UI Spec should this anonymous session render?

The backend is the single place assignment happens (assignment.assign); the
browser only renders what it is told. The response carries exactly what
rendering needs — experiment key, variant, spec hash, the spec data — and no
evaluation reports, provenance ids, allocation or metrics.

  no running experiment for the page  -> {"status": "none"}      (bundled Generation 0)
  assigned                            -> {"status": "assigned", ...spec}
  anything wrong while serving        -> {"status": "fallback", reason}
                                         (bundled Generation 0, NO exposure)

Before serving, the stored spec is re-hashed and must equal the experiment's
hash for that variant: a candidate that is not exactly what Step 13 evaluated
is never served. Both variants travel the same path (fetch -> validate ->
render), so neither arm gets a faster or different loading experience.
A paused experiment serves nothing: new and returning sessions see the active
generation. Neither does an experiment whose control is no longer the page's active
generation (Step 15: e.g. after a rollback) — it is stale.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from darwin.db.models import Experiment, UISpecVersion
from darwin.generations.active import active_spec
from darwin.mutations.apply import content_hash

from .assignment import assign
from .vocabulary import FallbackReason, Variant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Served:
    status: str  # none | assigned | fallback
    experiment_key: str | None = None
    variant: Variant | None = None
    spec_hash: str | None = None
    spec_version_id: uuid.UUID | None = None
    spec: dict[str, Any] | None = None
    reason: FallbackReason | None = None


def _fallback(key: str, reason: FallbackReason) -> Served:
    logger.warning(
        "experiment serving fallback", extra={"context": {"experiment_key": key, "reason": reason}}
    )
    return Served("fallback", experiment_key=key, reason=reason)


def resolve_variant(session: Session, session_id: uuid.UUID, page_id: str) -> Served:
    experiment = session.scalar(
        select(Experiment).where(Experiment.page_id == page_id, Experiment.status == "running")
    )
    if experiment is None:
        return Served("none")
    key = experiment.experiment_key
    current = active_spec(session, page_id)
    if current is not None and current.id != experiment.control_spec_id:
        # The control is no longer the page's active generation (e.g. after a rollback):
        # the experiment is stale. Serve the active generation; record no exposure.
        logger.warning(
            "experiment control is not the active generation",
            extra={"context": {"experiment_key": key}},
        )
        return Served("none")
    try:
        variant = assign(key, session_id, experiment.candidate_allocation_bp)
    except Exception:  # noqa: BLE001 — any assignment problem serves control, never candidate
        return _fallback(key, "assignment_error")
    spec_id, expected = (
        (experiment.candidate_spec_id, experiment.candidate_spec_hash)
        if variant == "candidate"
        else (experiment.control_spec_id, experiment.control_spec_hash)
    )
    row = session.get(UISpecVersion, spec_id)
    if row is None:
        return _fallback(key, "spec_unavailable")
    if row.content_hash != expected or content_hash(row.spec) != expected:
        return _fallback(key, "spec_hash_mismatch")
    return Served("assigned", key, variant, expected, row.id, dict(row.spec))
