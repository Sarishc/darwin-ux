"""create ui spec versions and mutation runs

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27

Candidate mutation generation (Step 12):

- ui_spec_version: immutable, content-addressed UI Spec documents. A
  "baseline" is an imported committed generation (generation set); a
  "candidate" comes from a mutation run (generation NULL until a future
  promotion step, candidate_for_generation = parent + 1, parent required).
  UNIQUE(page_id, generation) keeps generations monotonic per page;
  UNIQUE(parent_id, content_hash) deduplicates identical candidates.
  A BEFORE UPDATE trigger rejects every UPDATE: versions are only ever added.
- mutation_run: one explicit generation attempt from one proceed decision;
  a candidate exists exactly when status = 'succeeded'.

Indexes: the unique constraints above and mutation_run.decision_run_id.

Downgrade drops the trigger, its function and both tables; everything
earlier is untouched.

Generated with --autogenerate (tables), then reviewed; the immutability
trigger is hand-written. 0001-0007 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_FUNCTION = """
CREATE FUNCTION ui_spec_version_is_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ui_spec_version rows are immutable (id %)', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""
IMMUTABLE_TRIGGER = """
CREATE TRIGGER ui_spec_version_no_update BEFORE UPDATE ON ui_spec_version
FOR EACH ROW EXECUTE FUNCTION ui_spec_version_is_immutable()
"""


def upgrade() -> None:
    op.create_table(
        "ui_spec_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("candidate_for_generation", sa.Integer(), nullable=True),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'baseline') = (generation IS NOT NULL)",
            name=op.f("ck_ui_spec_version_generation_only_for_baseline"),
        ),
        sa.CheckConstraint(
            "(status = 'candidate') = "
            "(parent_id IS NOT NULL AND candidate_for_generation IS NOT NULL)",
            name=op.f("ck_ui_spec_version_candidate_has_parent"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ui_spec_version_content_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(spec) = 'object'", name=op.f("ck_ui_spec_version_spec_is_object")
        ),
        sa.CheckConstraint(
            "status IN ('baseline', 'candidate')", name=op.f("ck_ui_spec_version_status_is_known")
        ),
        sa.CheckConstraint(
            "generation IS NULL OR generation >= 0",
            name=op.f("ck_ui_spec_version_generation_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_ui_spec_version_parent_id_ui_spec_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ui_spec_version")),
        sa.UniqueConstraint("page_id", "generation", name="uq_ui_spec_version_generation"),
        sa.UniqueConstraint("parent_id", "content_hash", name="uq_ui_spec_version_candidate"),
    )
    op.create_table(
        "mutation_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("decision_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_spec_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_spec_id", sa.Uuid(), nullable=True),
        sa.Column("generator", sa.String(length=16), nullable=False),
        sa.Column("generator_version", sa.String(length=128), nullable=False),
        sa.Column("request_version", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "mutation_spec",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("operation_count", sa.Integer(), nullable=True),
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
            "(status = 'succeeded') = (candidate_spec_id IS NOT NULL)",
            name=op.f("ck_mutation_run_candidate_only_on_success"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (error_type IS NULL)",
            name=op.f("ck_mutation_run_error_type_matches"),
        ),
        sa.CheckConstraint(
            "generator IN ('fixture', 'llm', 'muse')",
            name=op.f("ck_mutation_run_generator_is_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'",
            name=op.f("ck_mutation_run_errors_is_array"),
        ),
        sa.CheckConstraint(
            "mutation_spec IS NULL OR jsonb_typeof(mutation_spec) = 'object'",
            name=op.f("ck_mutation_run_mutation_spec_is_object"),
        ),
        sa.CheckConstraint(
            "request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_mutation_run_request_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "(status = 'stale_provenance') = (request_hash IS NULL)",
            name=op.f("ck_mutation_run_request_hash_matches"),
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'invalid_output', 'validation_failed', "
            "'generator_error', 'generator_unavailable', 'stale_provenance')",
            name=op.f("ck_mutation_run_status_is_known"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_mutation_run_latency_not_negative"),
        ),
        sa.CheckConstraint(
            "operation_count IS NULL OR operation_count BETWEEN 1 AND 5",
            name=op.f("ck_mutation_run_operation_count_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_mutation_run_candidate_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_run_id"],
            ["decision_run.id"],
            name=op.f("fk_mutation_run_decision_run_id_decision_run"),
        ),
        sa.ForeignKeyConstraint(
            ["source_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_mutation_run_source_spec_id_ui_spec_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mutation_run")),
    )
    op.create_index(
        "ix_mutation_run_decision_run_id", "mutation_run", ["decision_run_id"], unique=False
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(IMMUTABLE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ui_spec_version_no_update ON ui_spec_version")
    op.execute("DROP FUNCTION IF EXISTS ui_spec_version_is_immutable()")
    op.drop_index("ix_mutation_run_decision_run_id", table_name="mutation_run")
    op.drop_table("mutation_run")
    op.drop_table("ui_spec_version")
