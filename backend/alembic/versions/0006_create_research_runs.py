"""create research runs

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27

Research workflow (Step 10): research_run (one execution of the LangGraph
research graph, with its budget counters and outcome) and research_step
(its step ledger, one row per executed node).

It also widens hypothesis.status from ('proposed') to ('proposed',
'accepted', 'rejected'): a research run ends by accepting or rejecting the
hypothesis it produced. 0005's file is not modified; the constraint is
replaced here.

Budget caps (2 retrieval attempts, 2 LLM calls, 12 graph steps) are repeated
as CHECKs, so a bug cannot record a run beyond them.

Indexes: research_run.signal_id ("runs for this signal"); research_step's
UNIQUE(run_id, sequence) serves "the trajectory of this run".

Downgrade drops both tables and restores the narrower CHECK. Hypotheses
marked accepted/rejected go back to 'proposed' first (the decision itself
lived in the dropped research_run rows); nothing else changes.

Generated with --autogenerate (tables), then reviewed; the hypothesis CHECK
change is hand-written (autogenerate does not compare CHECK constraints).
0001-0005 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("graph_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("current_node", sa.String(length=64), nullable=False),
        sa.Column(
            "queries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("retrieval_attempts", sa.Integer(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("calls_without_usage", sa.Integer(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("hypothesis_run_id", sa.Uuid(), nullable=True),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=True),
        sa.Column(
            "critique", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("review_reason", sa.String(length=64), nullable=True),
        sa.Column("human_decision", sa.String(length=16), nullable=True),
        sa.Column("budget", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("elapsed_ms", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (stop_reason IS NULL)",
            name=op.f("ck_research_run_stop_reason_matches"),
        ),
        sa.CheckConstraint(
            "(status IN ('running', 'waiting_for_human')) = (completed_at IS NULL)",
            name=op.f("ck_research_run_completed_at_matches"),
        ),
        sa.CheckConstraint(
            "critique IS NULL OR jsonb_typeof(critique) = 'object'",
            name=op.f("ck_research_run_critique_is_object"),
        ),
        sa.CheckConstraint(
            "human_decision IS NULL OR human_decision IN ('approve', 'reject')",
            name=op.f("ck_research_run_human_decision_is_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(budget) = 'object'", name=op.f("ck_research_run_budget_is_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(queries) = 'array'", name=op.f("ck_research_run_queries_is_array")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_for_human', 'succeeded', "
            "'insufficient_evidence', 'rejected', 'failed')",
            name=op.f("ck_research_run_status_is_known"),
        ),
        sa.CheckConstraint("elapsed_ms >= 0", name=op.f("ck_research_run_elapsed_not_negative")),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND calls_without_usage >= 0",
            name=op.f("ck_research_run_usage_not_negative"),
        ),
        sa.CheckConstraint(
            "llm_calls BETWEEN 0 AND 2", name=op.f("ck_research_run_llm_calls_within_cap")
        ),
        sa.CheckConstraint(
            "retrieval_attempts BETWEEN 0 AND 2",
            name=op.f("ck_research_run_retrieval_attempts_within_cap"),
        ),
        sa.CheckConstraint("steps BETWEEN 0 AND 12", name=op.f("ck_research_run_steps_within_cap")),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"],
            ["hypothesis.id"],
            name=op.f("fk_research_run_hypothesis_id_hypothesis"),
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_run_id"],
            ["hypothesis_run.id"],
            name=op.f("fk_research_run_hypothesis_run_id_hypothesis_run"),
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["behavior_signal.signal_id"],
            name=op.f("fk_research_run_signal_id_behavior_signal"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_run")),
    )
    op.create_index("ix_research_run_signal_id", "research_run", ["signal_id"], unique=False)
    op.create_table(
        "research_step",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(detail) = 'object'", name=op.f("ck_research_step_detail_is_object")
        ),
        sa.CheckConstraint("node <> ''", name=op.f("ck_research_step_node_not_empty")),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_research_step_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_run.id"],
            name=op.f("fk_research_step_run_id_research_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_step")),
        sa.UniqueConstraint("run_id", "sequence", name="uq_research_step_position"),
    )
    op.drop_constraint(op.f("ck_hypothesis_status_is_known"), "hypothesis", type_="check")
    op.create_check_constraint(
        op.f("ck_hypothesis_status_is_known"),
        "hypothesis",
        "status IN ('proposed', 'accepted', 'rejected')",
    )


def downgrade() -> None:
    op.execute("UPDATE hypothesis SET status = 'proposed' WHERE status <> 'proposed'")
    op.drop_constraint(op.f("ck_hypothesis_status_is_known"), "hypothesis", type_="check")
    op.create_check_constraint(
        op.f("ck_hypothesis_status_is_known"), "hypothesis", "status IN ('proposed')"
    )
    op.drop_table("research_step")
    op.drop_index("ix_research_run_signal_id", table_name="research_run")
    op.drop_table("research_run")
