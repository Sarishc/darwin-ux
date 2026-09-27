"""create candidate evaluation runs

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27

Candidate sandbox evaluation (Step 13): candidate_evaluation_run, one
immutable record per explicit evaluation of a candidate UI Spec — status,
recommendation (pass | human_review | reject), reason codes, per-category
results, evaluator and harness versions, duration.

Fail-closed rules are CHECKs: pass only when completed; provenance failures
always reject; error_type is NULL exactly when completed. A BEFORE UPDATE
trigger rejects every update: evaluations are only ever added.

Index: candidate_spec_id ("evaluations of this candidate").

Downgrade drops the trigger, its function and the table; everything earlier
is untouched.

Generated with --autogenerate (table), then reviewed; the trigger is
hand-written. 0001-0008 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_FUNCTION = """
CREATE FUNCTION candidate_evaluation_run_is_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'candidate_evaluation_run rows are immutable (id %)', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""
IMMUTABLE_TRIGGER = """
CREATE TRIGGER candidate_evaluation_run_no_update BEFORE UPDATE ON candidate_evaluation_run
FOR EACH ROW EXECUTE FUNCTION candidate_evaluation_run_is_immutable()
"""


def upgrade() -> None:
    op.create_table(
        "candidate_evaluation_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_spec_id", sa.Uuid(), nullable=False),
        sa.Column("mutation_run_id", sa.Uuid(), nullable=True),
        sa.Column("evaluator_version", sa.String(length=32), nullable=False),
        sa.Column("harness_version", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("recommendation", sa.String(length=16), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("category_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (error_type IS NULL)",
            name=op.f("ck_candidate_evaluation_run_error_type_matches"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(category_results) = 'object'",
            name=op.f("ck_candidate_evaluation_run_category_results_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0",
            name=op.f("ck_candidate_evaluation_run_reason_codes_not_empty"),
        ),
        sa.CheckConstraint(
            "recommendation <> 'pass' OR status = 'completed'",
            name=op.f("ck_candidate_evaluation_run_pass_only_when_completed"),
        ),
        sa.CheckConstraint(
            "recommendation IN ('pass', 'human_review', 'reject')",
            name=op.f("ck_candidate_evaluation_run_recommendation_is_known"),
        ),
        sa.CheckConstraint(
            "status <> 'provenance_failed' OR recommendation = 'reject'",
            name=op.f("ck_candidate_evaluation_run_provenance_failure_rejects"),
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'provenance_failed', 'evaluator_error')",
            name=op.f("ck_candidate_evaluation_run_status_is_known"),
        ),
        sa.CheckConstraint(
            "duration_ms >= 0", name=op.f("ck_candidate_evaluation_run_duration_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_candidate_evaluation_run_candidate_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["mutation_run_id"],
            ["mutation_run.id"],
            name=op.f("fk_candidate_evaluation_run_mutation_run_id_mutation_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_evaluation_run")),
    )
    op.create_index(
        "ix_candidate_evaluation_run_candidate_spec_id",
        "candidate_evaluation_run",
        ["candidate_spec_id"],
        unique=False,
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(IMMUTABLE_TRIGGER)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS candidate_evaluation_run_no_update ON candidate_evaluation_run"
    )
    op.execute("DROP FUNCTION IF EXISTS candidate_evaluation_run_is_immutable()")
    op.drop_index(
        "ix_candidate_evaluation_run_candidate_spec_id", table_name="candidate_evaluation_run"
    )
    op.drop_table("candidate_evaluation_run")
