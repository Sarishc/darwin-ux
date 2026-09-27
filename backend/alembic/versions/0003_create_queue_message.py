"""create queue_message

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27

The local, PostgreSQL-backed work queue (see darwin/db/models/queue_message.py):
- UNIQUE(message_id): enqueueing the same event twice creates one message.
- ix_queue_message_pending_visible_at (partial, status = 'pending'): serves the
  claim query "oldest pending message with visible_at <= now()".

Generated with --autogenerate, then reviewed. 0001 and 0002 are unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "queue_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "visible_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("receipt_handle", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(body) = 'object'", name=op.f("ck_queue_message_body_is_object")
        ),
        sa.CheckConstraint(
            "message_type <> ''", name=op.f("ck_queue_message_message_type_not_empty")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'dead')", name=op.f("ck_queue_message_status_is_known")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_queue_message_attempts_not_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_message")),
        sa.UniqueConstraint("message_id", name=op.f("uq_queue_message_message_id")),
    )
    op.create_index(
        "ix_queue_message_pending_visible_at",
        "queue_message",
        ["visible_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_queue_message_pending_visible_at",
        table_name="queue_message",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("queue_message")
