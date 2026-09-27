"""create behavior_signal

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 23:20:18.785639

- behavior_signal: deterministic signals detected from user_event history.
  UNIQUE(signal_id) makes re-running detection idempotent. superseded_at marks
  rows that are no longer canonical after late-arriving events (never deleted).
  ix_behavior_signal_session_id serves per-session reconciliation.
- ix_user_event_session_id_occurred_at: detection reads one session's events,
  ordered by occurred_at, after every accepted event.

Generated with --autogenerate, then reviewed. No foreign keys: a signal's
evidence events are listed in its `evidence` JSON (see behavior_signal model).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "behavior_signal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("detector_version", sa.String(length=16), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "detector_version <> ''",
            name=op.f("ck_behavior_signal_detector_version_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name=op.f("ck_behavior_signal_evidence_is_object"),
        ),
        sa.CheckConstraint(
            "signal_type <> ''", name=op.f("ck_behavior_signal_signal_type_not_empty")
        ),
        sa.CheckConstraint(
            "window_end >= window_start", name=op.f("ck_behavior_signal_window_is_ordered")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_behavior_signal")),
        sa.UniqueConstraint("signal_id", name=op.f("uq_behavior_signal_signal_id")),
    )
    op.create_index(
        "ix_behavior_signal_session_id", "behavior_signal", ["session_id"], unique=False
    )
    op.create_index(
        "ix_user_event_session_id_occurred_at",
        "user_event",
        ["session_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_event_session_id_occurred_at", table_name="user_event")
    op.drop_index("ix_behavior_signal_session_id", table_name="behavior_signal")
    op.drop_table("behavior_signal")
