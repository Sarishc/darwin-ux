"""Experiments (Step 14): configuration, exposures, immutable analyses.

- Experiment: one controlled comparison of Generation 0 (control) against one
  sandbox-approved candidate. Its CONFIGURATION (specs, hashes, allocation,
  metrics, sample floor, provenance ids) never changes after insert; only the
  lifecycle columns (status, started_at, paused_at, stopped_at, stop_reason)
  move, and only along the allowed transitions. Both rules are enforced by a
  database trigger (migration 0010), not only by the service.
- ExperimentLifecycleEvent: the immutable, append-only history of every status
  change (sequence 0 = created as draft). Written ONLY by a database trigger on
  experiment INSERT/UPDATE, validated against the experiment row and the
  previous event. Active collection windows (running intervals) are derived
  from it; pause boundaries are therefore auditable, never inferred.
- ExperimentExposure: one row per (experiment, anonymous session) — the first
  successful render of the assigned variant, recorded only while running.
  UNIQUE(experiment_id, session_id) makes repeated exposure events idempotent.
  Immutable.
- ExperimentAnalysis: one immutable aggregate report per analysis run. No raw
  events, no session ids. Re-running analysis adds a row.

Vocabulary: darwin.experiments.vocabulary (the CHECKs below repeat it).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base
from darwin.experiments.vocabulary import (
    ANALYSIS_STATUSES,
    ASSESSMENTS,
    CANDIDATE_ALLOCATIONS_BP,
    EXPERIMENT_KEY_PATTERN,
    EXPERIMENT_STATUSES,
    MAX_GUARDRAILS,
    METRIC_NAMES,
    MIN_SAMPLE_CEILING,
    MIN_SAMPLE_FLOOR,
    SPEC_HASH_PATTERN,
    STOP_REASONS,
    TOTAL_BUCKETS,
    TRAFFIC_SOURCES,
    VARIANTS,
    sql_in,
)

_METRICS_JSON = "[" + ", ".join(f'"{m}"' for m in METRIC_NAMES) + "]"


class Experiment(Base):
    __tablename__ = "experiment"
    __table_args__ = (
        CheckConstraint(f"experiment_key ~ '{EXPERIMENT_KEY_PATTERN}'", name="key_is_valid"),
        CheckConstraint(sql_in("status", EXPERIMENT_STATUSES), name="status_is_known"),
        CheckConstraint(
            sql_in("candidate_allocation_bp", CANDIDATE_ALLOCATIONS_BP),
            name="candidate_allocation_allowlisted",
        ),
        CheckConstraint(
            f"control_allocation_bp + candidate_allocation_bp = {TOTAL_BUCKETS}",
            name="allocation_sums_to_total",
        ),
        CheckConstraint(sql_in("primary_metric", METRIC_NAMES), name="primary_metric_is_known"),
        CheckConstraint(
            "jsonb_typeof(guardrail_metrics) = 'array' AND "
            f"jsonb_array_length(guardrail_metrics) BETWEEN 1 AND {MAX_GUARDRAILS} AND "
            f"guardrail_metrics <@ '{_METRICS_JSON}'::jsonb AND "
            "NOT guardrail_metrics ? primary_metric",
            name="guardrails_are_valid",
        ),
        CheckConstraint(
            f"minimum_sample_per_variant BETWEEN {MIN_SAMPLE_FLOOR} AND {MIN_SAMPLE_CEILING}",
            name="minimum_sample_in_range",
        ),
        CheckConstraint(sql_in("traffic_source", TRAFFIC_SOURCES), name="traffic_source_is_known"),
        CheckConstraint(
            f"control_spec_hash ~ '{SPEC_HASH_PATTERN}' AND "
            f"candidate_spec_hash ~ '{SPEC_HASH_PATTERN}'",
            name="spec_hashes_are_sha256",
        ),
        CheckConstraint("control_spec_hash <> candidate_spec_hash", name="variants_differ"),
        CheckConstraint("control_spec_id <> candidate_spec_id", name="variant_specs_differ"),
        CheckConstraint(
            "(status = 'draft') = (started_at IS NULL) OR status = 'stopped'",
            name="started_at_matches_status",
        ),
        CheckConstraint(
            "(status IN ('stopped', 'completed')) = (stopped_at IS NOT NULL)",
            name="stopped_at_matches_status",
        ),
        CheckConstraint(
            f"stop_reason IS NULL OR {sql_in('stop_reason', STOP_REASONS)}",
            name="stop_reason_is_known",
        ),
        # At most one running or paused experiment per page: no overlapping experiments.
        Index(
            "uq_experiment_one_active_per_page",
            "page_id",
            unique=True,
            postgresql_where=sql_text("status IN ('running', 'paused')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_key: Mapped[str] = mapped_column(String(64), unique=True)
    page_id: Mapped[str] = mapped_column(String(64))
    # Provenance: the evaluation that made the candidate eligible, and its chain.
    candidate_evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_evaluation_run.id")
    )
    mutation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mutation_run.id"))
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypothesis.id"))
    control_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    candidate_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    control_spec_hash: Mapped[str] = mapped_column(String(64))
    candidate_spec_hash: Mapped[str] = mapped_column(String(64))
    # Integer basis points out of 10 000 buckets (exact; no floats).
    control_allocation_bp: Mapped[int]
    candidate_allocation_bp: Mapped[int]
    primary_metric: Mapped[str] = mapped_column(String(64))
    guardrail_metrics: Mapped[list[str]] = mapped_column(JSONB)
    minimum_sample_per_variant: Mapped[int]
    traffic_source: Mapped[str] = mapped_column(String(16))
    # Lifecycle (the only columns an UPDATE may change).
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    # When the current status began (server clock). Strictly increases with every
    # transition; the lifecycle trigger copies it into experiment_lifecycle_event.
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExperimentLifecycleEvent(Base):
    __tablename__ = "experiment_lifecycle_event"
    __table_args__ = (
        CheckConstraint("sequence >= 0", name="sequence_not_negative"),
        CheckConstraint(
            "(sequence = 0) = (from_status IS NULL) AND (sequence > 0 OR to_status = 'draft')",
            name="first_event_is_draft",
        ),
        CheckConstraint(
            f"from_status IS NULL OR {sql_in('from_status', EXPERIMENT_STATUSES)}",
            name="from_status_is_known",
        ),
        CheckConstraint(sql_in("to_status", EXPERIMENT_STATUSES), name="to_status_is_known"),
        UniqueConstraint("experiment_id", "sequence", name="uq_experiment_lifecycle_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment.id"))
    sequence: Mapped[int]
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExperimentExposure(Base):
    __tablename__ = "experiment_exposure"
    __table_args__ = (
        CheckConstraint(sql_in("variant", VARIANTS), name="variant_is_known"),
        CheckConstraint(f"spec_hash ~ '{SPEC_HASH_PATTERN}'", name="spec_hash_is_sha256"),
        # The exposure identity: one per experiment and anonymous session.
        UniqueConstraint("experiment_id", "session_id", name="uq_experiment_exposure_session"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment.id"))
    session_id: Mapped[uuid.UUID]  # anonymous per-visit id; never a user identity
    variant: Mapped[str] = mapped_column(String(16))
    spec_hash: Mapped[str] = mapped_column(String(64))
    # The first exposure event (user_event.event_id) and its client time.
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_event.event_id"))
    exposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExperimentAnalysis(Base):
    __tablename__ = "experiment_analysis"
    __table_args__ = (
        CheckConstraint(sql_in("status", ANALYSIS_STATUSES), name="status_is_known"),
        CheckConstraint(sql_in("assessment", ASSESSMENTS), name="assessment_is_known"),
        CheckConstraint(
            "status = 'completed' OR assessment = 'needs_review'",
            name="analysis_error_needs_review",
        ),
        CheckConstraint("(status = 'completed') = (error_type IS NULL)", name="error_type_matches"),
        CheckConstraint(
            "control_exposures >= 0 AND candidate_exposures >= 0", name="exposures_not_negative"
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0",
            name="reason_codes_not_empty",
        ),
        CheckConstraint("jsonb_typeof(report) = 'object'", name="report_is_object"),
        CheckConstraint("report_hash ~ '^[0-9a-f]{64}$'", name="report_hash_is_sha256"),
        Index("ix_experiment_analysis_experiment_id", "experiment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment.id"))
    analysis_version: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    assessment: Mapped[str] = mapped_column(String(32))
    data_sufficiency: Mapped[str | None] = mapped_column(String(32))
    guardrail_status: Mapped[str | None] = mapped_column(String(16))
    control_exposures: Mapped[int]
    candidate_exposures: Mapped[int]
    reason_codes: Mapped[list[str]] = mapped_column(JSONB)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)  # aggregates only
    report_hash: Mapped[str] = mapped_column(String(64))
    error_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
