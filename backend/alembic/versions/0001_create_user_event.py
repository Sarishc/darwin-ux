"""create user_event

Revision ID: 0001
Revises:
Create Date: 2026-09-26 16:18:27.414359

The first table: raw behavioural events. See darwin/db/models/user_event.py
for the meaning of each column. The integration tests compare this migration
against the ORM model, so the two cannot drift apart unnoticed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_event")),
        sa.UniqueConstraint("event_id", name=op.f("uq_user_event_event_id")),
        sa.CheckConstraint("event_type <> ''", name=op.f("ck_user_event_event_type_not_empty")),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_user_event_payload_is_object")
        ),
    )


def downgrade() -> None:
    op.drop_table("user_event")
