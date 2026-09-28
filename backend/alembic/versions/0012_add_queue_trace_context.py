"""add queue trace context

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28

Operational observability (Step 16): the producer's W3C trace context travels
with each durable queue message, beside the body — never inside the telemetry
payload. Two nullable, bounded columns on queue_message:

- traceparent  VARCHAR(55)   CHECK: W3C version-00 format
- tracestate   VARCHAR(512)  CHECK: only with a traceparent

Existing messages keep NULL (their worker spans start fresh traces). Downgrade
drops both columns; no other data changes. 0001-0011 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("queue_message", sa.Column("traceparent", sa.String(length=55), nullable=True))
    op.add_column("queue_message", sa.Column("tracestate", sa.String(length=512), nullable=True))
    op.create_check_constraint(
        "traceparent_is_w3c",
        "queue_message",
        "traceparent IS NULL OR traceparent ~ '^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'",
    )
    op.create_check_constraint(
        "tracestate_needs_traceparent",
        "queue_message",
        "tracestate IS NULL OR traceparent IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_queue_message_tracestate_needs_traceparent"), "queue_message", type_="check"
    )
    op.drop_constraint(op.f("ck_queue_message_traceparent_is_w3c"), "queue_message", type_="check")
    op.drop_column("queue_message", "tracestate")
    op.drop_column("queue_message", "traceparent")
