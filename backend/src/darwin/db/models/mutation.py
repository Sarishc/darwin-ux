"""UI Spec versions and mutation runs (Step 12).

- UISpecVersion: an IMMUTABLE UI Spec document, content-addressed.
  * status "baseline": an imported, committed generation (Generation 0 from
    frontend/src/ui-spec/generation-0.json). `generation` is set.
  * status "candidate": produced by a mutation run. `generation` is NULL — a
    candidate is not a generation until a future promotion step says so —
    and `candidate_for_generation` = parent generation + 1 records what it
    would become. It always has a parent.
  UPDATE is forbidden by a database trigger (migration 0008): versions are
  never edited, only added. Candidates are deduplicated by (parent, content
  hash): generating the same change twice reuses the same candidate row.
- MutationRun: one explicit generation attempt from one proceed decision —
  generator and exact version, request version and hash, outcome, the
  schema-valid MutationSpec (if any), validation errors (types only), and the
  candidate it produced. Only "succeeded" has a candidate.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

SPEC_STATUSES = ("baseline", "candidate")
MUTATION_STATUSES = (
    "succeeded",
    "invalid_output",
    "validation_failed",
    "generator_error",
    "generator_unavailable",
    "stale_provenance",
)
GENERATORS = ("fixture", "llm", "muse")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class UISpecVersion(Base):
    __tablename__ = "ui_spec_version"
    __table_args__ = (
        CheckConstraint(_in("status", SPEC_STATUSES), name="status_is_known"),
        CheckConstraint(
            "(status = 'baseline') = (generation IS NOT NULL)", name="generation_only_for_baseline"
        ),
        CheckConstraint(
            "(status = 'candidate') = "
            "(parent_id IS NOT NULL AND candidate_for_generation IS NOT NULL)",
            name="candidate_has_parent",
        ),
        CheckConstraint("generation IS NULL OR generation >= 0", name="generation_not_negative"),
        CheckConstraint("jsonb_typeof(spec) = 'object'", name="spec_is_object"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_is_sha256"),
        UniqueConstraint("page_id", "generation", name="uq_ui_spec_version_generation"),
        UniqueConstraint("parent_id", "content_hash", name="uq_ui_spec_version_candidate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    page_id: Mapped[str] = mapped_column(String(64))  # e.g. "pricing_signup"
    status: Mapped[str] = mapped_column(String(16))
    generation: Mapped[int | None]  # baselines only
    candidate_for_generation: Mapped[int | None]  # candidates only
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ui_spec_version.id"))
    schema_version: Mapped[int]  # the UI Spec document's own "version" field
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of canonical JSON
    source: Mapped[str] = mapped_column(String(160))  # "repo:<path>" | "mutation_run"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MutationRun(Base):
    __tablename__ = "mutation_run"
    __table_args__ = (
        CheckConstraint(_in("status", MUTATION_STATUSES), name="status_is_known"),
        CheckConstraint(_in("generator", GENERATORS), name="generator_is_known"),
        CheckConstraint(
            "(status = 'succeeded') = (candidate_spec_id IS NOT NULL)",
            name="candidate_only_on_success",
        ),
        CheckConstraint("(status = 'succeeded') = (error_type IS NULL)", name="error_type_matches"),
        CheckConstraint(
            "mutation_spec IS NULL OR jsonb_typeof(mutation_spec) = 'object'",
            name="mutation_spec_is_object",
        ),
        CheckConstraint("jsonb_typeof(validation_errors) = 'array'", name="errors_is_array"),
        CheckConstraint(
            "request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'", name="request_hash_is_sha256"
        ),
        # Only a stale-provenance refusal has no request (it is refused before one is built).
        CheckConstraint(
            "(status = 'stale_provenance') = (request_hash IS NULL)", name="request_hash_matches"
        ),
        CheckConstraint(
            "operation_count IS NULL OR operation_count BETWEEN 1 AND 5",
            name="operation_count_in_range",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_not_negative"),
        Index("ix_mutation_run_decision_run_id", "decision_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    decision_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decision_run.id"))
    source_spec_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ui_spec_version.id"))
    candidate_spec_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ui_spec_version.id"))
    generator: Mapped[str] = mapped_column(String(16))  # fixture | llm | muse
    generator_version: Mapped[str] = mapped_column(String(128))
    request_version: Mapped[str] = mapped_column(String(32))  # "mutation_request.v1"
    request_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    error_type: Mapped[str | None] = mapped_column(String(64))
    # [{loc, type}] — never the offending values.
    validation_errors: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    # The schema-valid MutationSpec, kept even when later validation failed; NULL otherwise.
    mutation_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    operation_count: Mapped[int | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[float | None]  # the generator call; NULL when none was made
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
