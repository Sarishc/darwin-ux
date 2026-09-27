"""create decision runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27

Decision layer (Step 11): decision_run, one row per explicit decision about a
finished research run (decider, exact decider version, request version and
hash, the decider's validated answer, and the final decision after
DarwinUX's fail-closed policy).

The fail-closed rules are CHECKs: a failed_closed run always records
human_review, proceed is only ever recorded with status "decided", and
error_type is NULL exactly when status is "decided".

Indexes: research_run_id ("decisions about this research run").

Downgrade drops the table; research runs, hypotheses and everything earlier
are untouched.

Generated with --autogenerate, then reviewed. 0001-0006 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_run_id", sa.Uuid(), nullable=False),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=False),
        sa.Column("request_version", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("decider", sa.String(length=16), nullable=False),
        sa.Column("decider_version", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decider_decision", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=True),
        sa.Column("provider_confidence", sa.Double(), nullable=True),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'decided') = (error_type IS NULL)",
            name=op.f("ck_decision_run_error_type_matches"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence IN ('low', 'medium', 'high')",
            name=op.f("ck_decision_run_confidence_is_known"),
        ),
        sa.CheckConstraint(
            "decider IN ('rules', 'fake', 'llm', 'jev')",
            name=op.f("ck_decision_run_decider_is_known"),
        ),
        sa.CheckConstraint(
            "decider_decision IS NULL OR decider_decision IN ('proceed', 'human_review', 'reject')",
            name=op.f("ck_decision_run_decider_decision_is_known"),
        ),
        sa.CheckConstraint(
            "decision <> 'proceed' OR status = 'decided'",
            name=op.f("ck_decision_run_proceed_only_decided"),
        ),
        sa.CheckConstraint(
            "decision IN ('proceed', 'human_review', 'reject')",
            name=op.f("ck_decision_run_decision_is_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0",
            name=op.f("ck_decision_run_reason_codes_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'",
            name=op.f("ck_decision_run_errors_is_array"),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_decision_run_request_hash_is_sha256")
        ),
        sa.CheckConstraint(
            "status <> 'failed_closed' OR decision = 'human_review'",
            name=op.f("ck_decision_run_failed_closed_reviews"),
        ),
        sa.CheckConstraint(
            "status IN ('decided', 'overridden', 'failed_closed')",
            name=op.f("ck_decision_run_status_is_known"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_decision_run_latency_not_negative"),
        ),
        sa.CheckConstraint(
            "provider_confidence IS NULL OR provider_confidence BETWEEN 0 AND 1",
            name=op.f("ck_decision_run_provider_confidence_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"],
            ["hypothesis.id"],
            name=op.f("fk_decision_run_hypothesis_id_hypothesis"),
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_run.id"],
            name=op.f("fk_decision_run_research_run_id_research_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_run")),
    )
    op.create_index(
        "ix_decision_run_research_run_id", "decision_run", ["research_run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_decision_run_research_run_id", table_name="decision_run")
    op.drop_table("decision_run")
