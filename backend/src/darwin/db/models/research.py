"""Research workflow runs (Step 10): the run summary and its step ledger.

- ResearchRun: one explicit execution of the research graph for one signal —
  where it is (current node, status), what it cost (retrieval attempts, LLM
  calls, token counts, elapsed time), what it produced (hypothesis run,
  hypothesis, critique findings) and how it ended (stop reason, human
  decision). A resume continues the same run; a new explicit execution is a
  new run. The hard budget caps are repeated as CHECKs, so even a bug cannot
  record a run beyond them.
- ResearchStep: one row per executed graph node, in order — the trajectory.
  Compact metadata only: queries (code-generated), chunk ids, scores,
  statuses, token counts. Never chunk text, prompts, model output or reasoning.

The critique LLM call is audited in its research_step (request version,
provider, model, status, error type, tokens, latency); its validated findings
are on the run. Hypothesis generation keeps its own HypothesisRun (Step 9).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

RESEARCH_STATUSES = (
    "running",
    "waiting_for_human",
    "succeeded",
    "insufficient_evidence",
    "rejected",
    "failed",
)
OPEN_STATUSES = ("running", "waiting_for_human")
HUMAN_DECISIONS = ("approve", "reject")

# Mirrors darwin.research.budget (a unit test keeps them equal).
MAX_RETRIEVAL_ATTEMPTS = 2
MAX_LLM_CALLS = 2
MAX_GRAPH_STEPS = 12


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ResearchRun(Base):
    __tablename__ = "research_run"
    __table_args__ = (
        CheckConstraint(_in("status", RESEARCH_STATUSES), name="status_is_known"),
        # Every status except "running" says why it stopped or paused.
        CheckConstraint("(status = 'running') = (stop_reason IS NULL)", name="stop_reason_matches"),
        CheckConstraint(
            f"({_in('status', OPEN_STATUSES)}) = (completed_at IS NULL)",
            name="completed_at_matches",
        ),
        CheckConstraint(
            f"human_decision IS NULL OR {_in('human_decision', HUMAN_DECISIONS)}",
            name="human_decision_is_known",
        ),
        CheckConstraint(
            f"retrieval_attempts BETWEEN 0 AND {MAX_RETRIEVAL_ATTEMPTS}",
            name="retrieval_attempts_within_cap",
        ),
        CheckConstraint(f"llm_calls BETWEEN 0 AND {MAX_LLM_CALLS}", name="llm_calls_within_cap"),
        CheckConstraint(f"steps BETWEEN 0 AND {MAX_GRAPH_STEPS}", name="steps_within_cap"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND calls_without_usage >= 0",
            name="usage_not_negative",
        ),
        CheckConstraint("elapsed_ms >= 0", name="elapsed_not_negative"),
        CheckConstraint("jsonb_typeof(queries) = 'array'", name="queries_is_array"),
        CheckConstraint("jsonb_typeof(budget) = 'object'", name="budget_is_object"),
        CheckConstraint(
            "critique IS NULL OR jsonb_typeof(critique) = 'object'", name="critique_is_object"
        ),
        Index("ix_research_run_signal_id", "signal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)  # the run id
    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("behavior_signal.signal_id"))
    graph_version: Mapped[str] = mapped_column(String(32))  # e.g. "research_graph.v1"
    status: Mapped[str] = mapped_column(String(32), default="running")
    # e.g. "critique_accept", "insufficient_after_refinement", "llm_budget_exhausted".
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    current_node: Mapped[str] = mapped_column(String(64), default="")
    # Retrieval queries actually run (code-generated text, never model-written).
    queries: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    retrieval_attempts: Mapped[int] = mapped_column(default=0)
    llm_calls: Mapped[int] = mapped_column(default=0)
    # Sums of provider-reported counts (FakeLLMProvider: estimates, not billing data).
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    calls_without_usage: Mapped[int] = mapped_column(default=0)
    steps: Mapped[int] = mapped_column(default=0)
    hypothesis_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypothesis_run.id"))
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypothesis.id"))
    # The critique's validated findings (verdict, summary, issues, ...); NULL if none.
    critique: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    review_reason: Mapped[str | None] = mapped_column(String(64))
    human_decision: Mapped[str | None] = mapped_column(String(16))
    budget: Mapped[dict[str, int]] = mapped_column(JSONB)  # the limits this run ran under
    elapsed_ms: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchStep(Base):
    __tablename__ = "research_step"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_research_step_position"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("node <> ''", name="node_not_empty"),
        CheckConstraint("jsonb_typeof(detail) = 'object'", name="detail_is_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_run.id", ondelete="CASCADE"))
    sequence: Mapped[int]  # 1, 2, 3 ... within the run (also across a resume)
    node: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64))  # e.g. "sufficient", "accept"
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
