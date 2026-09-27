"""DecisionRun (Step 11): one explicit decision about one finished research run.

Records which implementation decided (decider + exact version), on which
request (version + sha256 of its canonical JSON), what the decider validly
said (if anything), and what DarwinUX finally recorded after its fail-closed
policy. Every explicit invocation is a new row — decisions are not
deduplicated, because deciders may be non-deterministic and each call is
audited.

The fail-closed rules are repeated as CHECKs:
- status "failed_closed" always records decision "human_review";
- "proceed" can only be recorded with status "decided" (never after a
  failure or a policy override);
- error_type is NULL exactly when status is "decided".

Not stored: the request itself (reconstructible from the research rows it
hashes), prompts, model reasoning, raw invalid decider output, credentials.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

DECISIONS = ("proceed", "human_review", "reject")
DECISION_STATUSES = ("decided", "overridden", "failed_closed")
DECIDERS = ("rules", "fake", "llm", "jev")
CONFIDENCE_LEVELS = ("low", "medium", "high")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class DecisionRun(Base):
    __tablename__ = "decision_run"
    __table_args__ = (
        CheckConstraint(_in("decision", DECISIONS), name="decision_is_known"),
        CheckConstraint(_in("status", DECISION_STATUSES), name="status_is_known"),
        CheckConstraint(_in("decider", DECIDERS), name="decider_is_known"),
        CheckConstraint(
            f"decider_decision IS NULL OR {_in('decider_decision', DECISIONS)}",
            name="decider_decision_is_known",
        ),
        CheckConstraint(
            f"confidence IS NULL OR {_in('confidence', CONFIDENCE_LEVELS)}",
            name="confidence_is_known",
        ),
        CheckConstraint(
            "status <> 'failed_closed' OR decision = 'human_review'", name="failed_closed_reviews"
        ),
        CheckConstraint("decision <> 'proceed' OR status = 'decided'", name="proceed_only_decided"),
        CheckConstraint("(status = 'decided') = (error_type IS NULL)", name="error_type_matches"),
        CheckConstraint(
            "provider_confidence IS NULL OR provider_confidence BETWEEN 0 AND 1",
            name="provider_confidence_in_range",
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0",
            name="reason_codes_not_empty",
        ),
        CheckConstraint("jsonb_typeof(validation_errors) = 'array'", name="errors_is_array"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash_is_sha256"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_not_negative"),
        Index("ix_decision_run_research_run_id", "research_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_run.id"))
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypothesis.id"))
    request_version: Mapped[str] = mapped_column(String(32))  # "decision_request.v1"
    request_hash: Mapped[str] = mapped_column(String(64))
    decider: Mapped[str] = mapped_column(String(16))  # rules | fake | llm | jev
    decider_version: Mapped[str] = mapped_column(String(128))  # e.g. "rules.v1", "jev:jev-1.13.0"
    # The FINAL decision after policy. "proceed" = eligible for a future mutation stage only.
    decision: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # decided | overridden | failed_closed
    decider_decision: Mapped[str | None] = mapped_column(String(16))  # what it validly said
    confidence: Mapped[str | None] = mapped_column(String(8))  # decider's, qualitative
    provider_confidence: Mapped[float | None]  # a provider's own 0-1 statistic (uncalibrated)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB)  # final, allowlisted
    error_type: Mapped[str | None] = mapped_column(String(64))
    # [{loc, type}] from validation or failed policy preconditions — never offending values.
    validation_errors: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
