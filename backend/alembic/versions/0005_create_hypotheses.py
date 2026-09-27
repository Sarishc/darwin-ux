"""create hypotheses

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27

Hypothesis generation (Step 9): hypothesis_run (every generation attempt,
audited, including failures) and hypothesis (the accepted artifact of a
successful run, at most one per run).

Both reference behavior_signal.signal_id (the signal's stable, UNIQUE
identity), without ON DELETE: signals are never deleted, only superseded.
Neither references knowledge_chunk: chunk ids change on re-ingestion, so
evidence references keep source_key and section alongside the id.

Indexes: the unique run_id, plus signal_id on both tables ("runs /
hypotheses for this signal"). Nothing else until a query needs it.

Downgrade drops both tables; earlier tables are untouched.

Generated with --autogenerate, then reviewed. 0001-0004 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hypothesis_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("request_version", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("retrieval_query", sa.String(length=1000), nullable=False),
        sa.Column("evidence_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "(status = 'succeeded') = (error_type IS NULL)",
            name=op.f("ck_hypothesis_run_error_type_matches"),
        ),
        sa.CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_hypothesis_run_evidence_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_chunk_ids) = 'array'",
            name=op.f("ck_hypothesis_run_evidence_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'",
            name=op.f("ck_hypothesis_run_errors_is_array"),
        ),
        sa.CheckConstraint(
            "output IS NULL OR jsonb_typeof(output) = 'object'",
            name=op.f("ck_hypothesis_run_output_is_object"),
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'insufficient_evidence', 'provider_unavailable', "
            "'provider_error', 'invalid_output', 'grounding_failed')",
            name=op.f("ck_hypothesis_run_status_is_known"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_hypothesis_run_input_tokens_positive"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_hypothesis_run_latency_not_negative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_hypothesis_run_output_tokens_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["behavior_signal.signal_id"],
            name=op.f("fk_hypothesis_run_signal_id_behavior_signal"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hypothesis_run")),
    )
    op.create_index("ix_hypothesis_run_signal_id", "hypothesis_run", ["signal_id"], unique=False)
    op.create_table(
        "hypothesis",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("affected_component", sa.String(length=128), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("evidence_references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name=op.f("ck_hypothesis_confidence_is_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_references) = 'array' "
            "AND jsonb_array_length(evidence_references) > 0",
            name=op.f("ck_hypothesis_evidence_references_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(limitations) = 'array'", name=op.f("ck_hypothesis_limitations_is_array")
        ),
        sa.CheckConstraint("rationale <> ''", name=op.f("ck_hypothesis_rationale_not_empty")),
        sa.CheckConstraint("statement <> ''", name=op.f("ck_hypothesis_statement_not_empty")),
        sa.CheckConstraint("status IN ('proposed')", name=op.f("ck_hypothesis_status_is_known")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["hypothesis_run.id"], name=op.f("fk_hypothesis_run_id_hypothesis_run")
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["behavior_signal.signal_id"],
            name=op.f("fk_hypothesis_signal_id_behavior_signal"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hypothesis")),
        sa.UniqueConstraint("run_id", name=op.f("uq_hypothesis_run_id")),
    )
    op.create_index("ix_hypothesis_signal_id", "hypothesis", ["signal_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_hypothesis_signal_id", table_name="hypothesis")
    op.drop_table("hypothesis")
    op.drop_index("ix_hypothesis_run_signal_id", table_name="hypothesis_run")
    op.drop_table("hypothesis_run")
