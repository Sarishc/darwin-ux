"""CandidateEvaluationRun (Step 13): one immutable sandbox evaluation of one candidate.

Normalised summary (status, recommendation, reason codes, versions, duration)
plus compact per-category JSON (`category_results`: status, kind, named
checks). Repeated evaluation of the same candidate adds a new row, so
evaluator versions can be compared later. UPDATE is refused by a trigger.

CHECKs encode fail-closed: `pass` only with status completed; provenance
failures always reject; an evaluator error can never pass.
No screenshots, prompts or model reasoning exist in this step.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

EVALUATION_STATUSES = ("completed", "provenance_failed", "evaluator_error")
RECOMMENDATIONS = ("pass", "human_review", "reject")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CandidateEvaluationRun(Base):
    __tablename__ = "candidate_evaluation_run"
    __table_args__ = (
        CheckConstraint(_in("status", EVALUATION_STATUSES), name="status_is_known"),
        CheckConstraint(_in("recommendation", RECOMMENDATIONS), name="recommendation_is_known"),
        CheckConstraint(
            "recommendation <> 'pass' OR status = 'completed'", name="pass_only_when_completed"
        ),
        CheckConstraint(
            "status <> 'provenance_failed' OR recommendation = 'reject'",
            name="provenance_failure_rejects",
        ),
        CheckConstraint("(status = 'completed') = (error_type IS NULL)", name="error_type_matches"),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0",
            name="reason_codes_not_empty",
        ),
        CheckConstraint(
            "jsonb_typeof(category_results) = 'object'", name="category_results_is_object"
        ),
        CheckConstraint("duration_ms >= 0", name="duration_not_negative"),
        Index("ix_candidate_evaluation_run_candidate_spec_id", "candidate_spec_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    candidate_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    # NULL only when provenance failed before a mutation run could be identified.
    mutation_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mutation_run.id"))
    evaluator_version: Mapped[str] = mapped_column(String(32))  # "candidate_eval.v1"
    harness_version: Mapped[str | None] = mapped_column(String(32))  # "sandbox_harness.v1"
    status: Mapped[str] = mapped_column(String(32))
    # pass = eligible for FUTURE human approval / experiment setup; never deployment.
    recommendation: Mapped[str] = mapped_column(String(16))
    reason_codes: Mapped[list[str]] = mapped_column(JSONB)
    category_results: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
