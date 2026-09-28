"""BehaviorSignal: a deterministic, explainable pattern found in UserEvent history.

A signal says "this happened, in this anonymous session, between these times,
and here are the events that prove it". It is produced by rules, never by a
model, and is the evidence later stages (hypotheses, experiments) build on.

The *canonical* signals of a session are its rows with superseded_at IS NULL.
They always equal what the detectors produce over the session's full event
history. When a late event changes that result, rows that are no longer
canonical get superseded_at set; they are kept, never deleted, as an audit
trail of what was believed earlier.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base


class BehaviorSignal(Base):
    __tablename__ = "behavior_signal"
    __table_args__ = (
        CheckConstraint("signal_type <> ''", name="signal_type_not_empty"),
        CheckConstraint("detector_version <> ''", name="detector_version_not_empty"),
        CheckConstraint("window_end >= window_start", name="window_is_ordered"),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_is_object"),
        # Reconciliation reads and updates one session's signals after every
        # accepted event; without this it would scan the whole table.
        Index("ix_behavior_signal_session_id", "session_id"),
        CheckConstraint(
            "ui_attribution IN ('single', 'mixed', 'unknown')", name="ui_attribution_is_known"
        ),
        CheckConstraint(
            "(ui_attribution = 'single') = (ui_spec_version_id IS NOT NULL)",
            name="ui_spec_version_only_when_single",
        ),
    )

    # Server-owned row identity (same convention as user_event).
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Deterministic idempotency key (UUID5 of detector + version + session +
    # scope + triggering event ids). UNIQUE: replaying detection is a no-op.
    signal_id: Mapped[uuid.UUID] = mapped_column(unique=True)

    # e.g. "rage_click". Also names the detector that produced it.
    signal_type: Mapped[str] = mapped_column(String(64))

    # Changing a detector's rules bumps this, so old and new signals are
    # distinguishable and reproducible.
    detector_version: Mapped[str] = mapped_column(String(16))

    # The anonymous session the pattern occurred in (never a user identity).
    session_id: Mapped[uuid.UUID]

    # occurred_at of the first and last evidence event.
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Compact explanation: event ids, counts, thresholds. Never raw payloads.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)

    # When DarwinUX first recorded the signal (server clock).
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # UI attribution (Step 15): derived ONLY from the evidence events' verified
    # ui_spec_version_id. "single" = every evidence event verified on the same spec
    # version; "mixed" = verified on different versions; "unknown" = at least one
    # evidence event has no verified attribution (all pre-Step-15 events).
    ui_attribution: Mapped[str] = mapped_column(String(16), server_default="unknown")
    ui_spec_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ui_spec_version.id"))

    # NULL = canonical. Set when later (late-arriving) events mean the
    # detectors no longer produce this signal for the session's history.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
