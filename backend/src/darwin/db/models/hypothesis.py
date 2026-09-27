"""Hypothesis generation: every run is audited; only valid output becomes a Hypothesis.

- HypothesisRun: one explicit generation attempt for one signal — what was
  retrieved (chunk ids, evidence hash), which request/provider/model, the
  outcome, token usage and latency. Failed and "insufficient evidence" runs
  are kept: they are what evaluation and cost tracking need. Every explicit
  call creates a new run; LLM calls are not deterministic and not free, so
  nothing pretends to deduplicate them.
- Hypothesis: the accepted artifact of a successful run (at most one per
  run). Its evidence references carry source and section, so they stay
  meaningful after Product Memory is re-ingested and chunk ids change.

Not stored: prompts (reconstructible from request_version + evidence), chunk
text, model reasoning, raw invalid output, credentials.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

RUN_STATUSES = (
    "succeeded",
    "insufficient_evidence",
    "provider_unavailable",
    "provider_error",
    "invalid_output",
    "grounding_failed",
)
# proposed: generated; accepted / rejected: the outcome of a research run (Step 10).
HYPOTHESIS_STATUSES = ("proposed", "accepted", "rejected")
CONFIDENCE_LEVELS = ("low", "medium", "high")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class HypothesisRun(Base):
    __tablename__ = "hypothesis_run"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status_is_known"),
        # Only success has no error type; every other outcome says why.
        CheckConstraint("(status = 'succeeded') = (error_type IS NULL)", name="error_type_matches"),
        CheckConstraint("jsonb_typeof(evidence_chunk_ids) = 'array'", name="evidence_is_array"),
        CheckConstraint("jsonb_typeof(validation_errors) = 'array'", name="errors_is_array"),
        CheckConstraint(
            "output IS NULL OR jsonb_typeof(output) = 'object'", name="output_is_object"
        ),
        CheckConstraint("evidence_hash ~ '^[0-9a-f]{64}$'", name="evidence_hash_is_sha256"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_positive"),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_positive"
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_not_negative"),
        Index("ix_hypothesis_run_signal_id", "signal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)  # the run id
    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("behavior_signal.signal_id"))
    signal_type: Mapped[str] = mapped_column(String(64))
    request_version: Mapped[str] = mapped_column(String(32))  # e.g. "hypothesis.v1"
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    embedding_model: Mapped[str] = mapped_column(String(64))
    retrieval_query: Mapped[str] = mapped_column(String(1000))
    # Excerpt chunk ids supplied to the model, in rank order ([] if none).
    evidence_chunk_ids: Mapped[list[str]] = mapped_column(JSONB)
    # sha256 of the request's evidence section: what the model saw, without storing it.
    evidence_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    # e.g. "no_context", "timeout", "missing", "unknown_evidence_reference".
    error_type: Mapped[str | None] = mapped_column(String(64))
    # [{loc, type}] from validation — never the offending values.
    validation_errors: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    # The schema-valid draft (also kept when grounding failed); NULL otherwise.
    # none_as_null: Python None must be SQL NULL, not the JSON value `null`.
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[float | None]  # the provider call only; NULL when no call was made
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Hypothesis(Base):
    __tablename__ = "hypothesis"
    __table_args__ = (
        CheckConstraint(_in("status", HYPOTHESIS_STATUSES), name="status_is_known"),
        CheckConstraint(_in("confidence", CONFIDENCE_LEVELS), name="confidence_is_known"),
        CheckConstraint("statement <> ''", name="statement_not_empty"),
        CheckConstraint("rationale <> ''", name="rationale_not_empty"),
        CheckConstraint(
            "jsonb_typeof(evidence_references) = 'array' "
            "AND jsonb_array_length(evidence_references) > 0",
            name="evidence_references_not_empty",
        ),
        CheckConstraint("jsonb_typeof(limitations) = 'array'", name="limitations_is_array"),
        Index("ix_hypothesis_signal_id", "signal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # UNIQUE: one accepted hypothesis per run.
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypothesis_run.id"), unique=True)
    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("behavior_signal.signal_id"))
    statement: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    affected_component: Mapped[str | None] = mapped_column(String(128))
    # Uncalibrated, model-assigned: low | medium | high. Not a probability.
    confidence: Mapped[str] = mapped_column(String(8))
    # [{chunk_id, source_key, section}] — resolved from the bundle at acceptance.
    evidence_references: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    limitations: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
