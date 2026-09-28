"""Generations (Step 15): the active-generation pointer and its immutable audit trail.

- ActiveGeneration: ONE row per page — which generation (a baseline or promoted
  UISpecVersion) the page serves. It is a database-backed feature flag: promotion
  moves it forward, rollback moves it back. A trigger (migration 0011) allows a move
  only when it names a promotion or rollback record describing exactly that move;
  it can never point at a candidate.
- PromotionApproval: an immutable human decision (approve | reject) bound to an
  evidence hash over the exact candidate, evaluation, experiment, analysis, source
  and target generation, and policy version. Approval grants nothing by itself.
- GenerationPromotion: the immutable record of an executed promotion (one per
  approval). Created in the same transaction as the promoted UISpecVersion and the
  pointer move; deferred checks refuse a commit where these disagree.
- GenerationRollback: the immutable record of a pointer reversal. Nothing is deleted.

Reviewer strings are self-asserted operator labels, not authenticated identities.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base
from darwin.generations.vocabulary import (
    CHANGE_KINDS,
    DECISIONS,
    REASON_MAX,
    REVIEWER_PATTERN,
    SPEC_HASH_PATTERN,
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


_REVIEWER = f"reviewer ~ '{REVIEWER_PATTERN}'"
_REASON = f"char_length(reason) BETWEEN 1 AND {REASON_MAX}"


class ActiveGeneration(Base):
    __tablename__ = "active_generation"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="generation_not_negative"),
        CheckConstraint(_in("change_kind", CHANGE_KINDS), name="change_kind_is_known"),
        CheckConstraint(
            "(change_kind = 'bootstrap') = (change_id IS NULL)", name="change_id_matches_kind"
        ),
    )

    page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ui_spec_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    generation: Mapped[int]
    change_kind: Mapped[str] = mapped_column(String(16))  # what last moved the pointer
    change_id: Mapped[uuid.UUID | None]  # the promotion or rollback record (NULL: bootstrap)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromotionApproval(Base):
    __tablename__ = "promotion_approval"
    __table_args__ = (
        CheckConstraint(_in("decision", DECISIONS), name="decision_is_known"),
        CheckConstraint(_REVIEWER, name="reviewer_is_valid"),
        CheckConstraint(_REASON, name="reason_length"),
        CheckConstraint(f"evidence_hash ~ '{SPEC_HASH_PATTERN}'", name="evidence_hash_is_sha256"),
        CheckConstraint("target_generation > source_generation", name="target_after_source"),
        CheckConstraint(
            "jsonb_typeof(blocking_reasons) = 'array'", name="blocking_reasons_is_array"
        ),
        CheckConstraint(
            "decision <> 'approve' OR jsonb_array_length(blocking_reasons) = 0",
            name="approve_only_when_eligible",
        ),
        # At most one approval per exact evidence: a duplicate approval is refused.
        Index(
            "uq_promotion_approval_evidence",
            "evidence_hash",
            unique=True,
            postgresql_where=text("decision = 'approve'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    page_id: Mapped[str] = mapped_column(String(64))
    candidate_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    candidate_evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_evaluation_run.id")
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment.id"))
    experiment_analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment_analysis.id"))
    source_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    source_generation: Mapped[int]
    target_generation: Mapped[int]
    decision: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(64))  # self-asserted, not authenticated
    reason: Mapped[str] = mapped_column(String(REASON_MAX))
    policy_version: Mapped[str] = mapped_column(String(32))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    # The gate's reason codes at decision time (empty for approve; a reject may be of
    # an ineligible candidate).
    blocking_reasons: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GenerationPromotion(Base):
    __tablename__ = "generation_promotion"
    __table_args__ = (
        CheckConstraint("to_generation > from_generation", name="generation_moves_forward"),
        CheckConstraint(_REVIEWER, name="reviewer_is_valid"),
        CheckConstraint(f"evidence_hash ~ '{SPEC_HASH_PATTERN}'", name="evidence_hash_is_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # UNIQUE: an approval can be executed once — a replayed promotion is refused.
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("promotion_approval.id"), unique=True)
    page_id: Mapped[str] = mapped_column(String(64))
    candidate_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    promoted_spec_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ui_spec_version.id"), unique=True
    )
    from_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    from_generation: Mapped[int]
    to_generation: Mapped[int]
    reviewer: Mapped[str] = mapped_column(String(64))  # who executed it (self-asserted)
    policy_version: Mapped[str] = mapped_column(String(32))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GenerationRollback(Base):
    __tablename__ = "generation_rollback"
    __table_args__ = (
        CheckConstraint("to_generation < from_generation", name="rollback_moves_back"),
        CheckConstraint("to_spec_id <> from_spec_id", name="rollback_changes_spec"),
        CheckConstraint(_REVIEWER, name="reviewer_is_valid"),
        CheckConstraint(_REASON, name="reason_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    page_id: Mapped[str] = mapped_column(String(64))
    from_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    from_generation: Mapped[int]
    to_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    to_generation: Mapped[int]
    reviewer: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(REASON_MAX))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
