"""create experiments

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27

Controlled experiments (Step 14):

- experiment: Generation 0 (control) vs one sandbox-approved candidate. CHECKs
  pin the closed vocabulary: allowlisted candidate allocation in integer basis
  points (100/500/1000/2500/5000 of 10 000) summing to 10 000 with control;
  a known primary metric; 1-3 known guardrails that exclude the primary; a
  sample floor of 100-100 000 per variant; a known traffic source; two
  different sha256 spec hashes; lifecycle timestamps consistent with status.
  A partial unique index allows at most one running/paused experiment per page.
  A BEFORE INSERT/UPDATE trigger: experiments are born `draft`; the
  configuration is immutable; only the listed status transitions are allowed,
  each with a strictly later status_changed_at; started_at is set once.
- experiment_lifecycle_event: the append-only history of every status change,
  written ONLY by an AFTER INSERT/UPDATE trigger on experiment and validated on
  insert (it must equal the experiment's current status and timestamp and
  continue the previous event's chain). Immutable: no UPDATE, no DELETE.
  Active collection windows (the running intervals) are derived from it.
- experiment_exposure: one row per (experiment, session) — UNIQUE, so repeated
  exposure events never inflate counts. Immutable (trigger).
- experiment_analysis: one immutable aggregate report per analysis run; an
  analysis error can only be `needs_review`. Immutable (trigger).

Downgrade drops the triggers, their functions and the three tables; everything
earlier is untouched.

Generated with --autogenerate (tables), then reviewed; triggers hand-written.
0001-0009 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONFIG_COLUMNS = (
    "id, experiment_key, page_id, candidate_evaluation_run_id, mutation_run_id, hypothesis_id, "
    "control_spec_id, candidate_spec_id, control_spec_hash, candidate_spec_hash, "
    "control_allocation_bp, candidate_allocation_bp, primary_metric, guardrail_metrics, "
    "minimum_sample_per_variant, traffic_source, created_at"
)
TRANSITIONS = (
    "('draft','running'), ('running','paused'), ('paused','running'), ('draft','stopped'), "
    "('running','stopped'), ('paused','stopped'), ('running','completed'), ('paused','completed')"
)
_OLD = ", ".join(f"OLD.{c.strip()}" for c in CONFIG_COLUMNS.split(","))
_NEW = ", ".join(f"NEW.{c.strip()}" for c in CONFIG_COLUMNS.split(","))

EXPERIMENT_FUNCTION = f"""
CREATE FUNCTION experiment_guard_update() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.started_at IS NOT NULL THEN
            RAISE EXCEPTION 'experiments are created as draft (id %)', NEW.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF ROW({_NEW}) IS DISTINCT FROM ROW({_OLD}) THEN
        RAISE EXCEPTION 'experiment configuration is immutable (id %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (OLD.status, NEW.status) NOT IN ({TRANSITIONS}) THEN
        RAISE EXCEPTION 'experiment transition % -> % is not allowed (id %)',
            OLD.status, NEW.status, OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status_changed_at <= OLD.status_changed_at THEN
        RAISE EXCEPTION 'experiment status_changed_at must increase (id %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'experiment started_at is set once (id %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
LIFECYCLE_RECORD_FUNCTION = """
CREATE FUNCTION experiment_record_lifecycle() RETURNS trigger AS $$
BEGIN
    INSERT INTO experiment_lifecycle_event
        (id, experiment_id, sequence, from_status, to_status, occurred_at)
    SELECT gen_random_uuid(), NEW.id,
           COALESCE((SELECT max(sequence) + 1 FROM experiment_lifecycle_event
                     WHERE experiment_id = NEW.id), 0),
           CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END,
           NEW.status, NEW.status_changed_at;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""
LIFECYCLE_GUARD_FUNCTION = f"""
CREATE FUNCTION experiment_lifecycle_guard_insert() RETURNS trigger AS $$
DECLARE
    current experiment%ROWTYPE;
    previous experiment_lifecycle_event%ROWTYPE;
BEGIN
    SELECT * INTO current FROM experiment WHERE id = NEW.experiment_id;
    IF NOT FOUND OR NEW.to_status <> current.status
       OR NEW.occurred_at <> current.status_changed_at THEN
        RAISE EXCEPTION 'lifecycle event does not match the experiment state (id %)',
            NEW.experiment_id USING ERRCODE = 'restrict_violation';
    END IF;
    SELECT * INTO previous FROM experiment_lifecycle_event
        WHERE experiment_id = NEW.experiment_id ORDER BY sequence DESC LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.sequence <> 0 OR NEW.from_status IS NOT NULL OR NEW.to_status <> 'draft' THEN
            RAISE EXCEPTION 'lifecycle history must start with draft (id %)',
                NEW.experiment_id USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF NEW.sequence <> previous.sequence + 1
          OR NEW.from_status IS DISTINCT FROM previous.to_status
          OR NEW.occurred_at <= previous.occurred_at
          OR (NEW.from_status, NEW.to_status) NOT IN ({TRANSITIONS}) THEN
        RAISE EXCEPTION 'lifecycle event breaks the experiment history (id %)', NEW.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
IMMUTABLE_FUNCTION = """
CREATE FUNCTION experiment_row_is_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable (id %)', TG_TABLE_NAME, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""
TRIGGERS = (
    "CREATE TRIGGER experiment_guard BEFORE INSERT OR UPDATE ON experiment "
    "FOR EACH ROW EXECUTE FUNCTION experiment_guard_update()",
    "CREATE TRIGGER experiment_lifecycle_record AFTER INSERT OR UPDATE OF status ON experiment "
    "FOR EACH ROW EXECUTE FUNCTION experiment_record_lifecycle()",
    "CREATE TRIGGER experiment_lifecycle_event_guard BEFORE INSERT ON experiment_lifecycle_event "
    "FOR EACH ROW EXECUTE FUNCTION experiment_lifecycle_guard_insert()",
    "CREATE TRIGGER experiment_lifecycle_event_immutable BEFORE UPDATE OR DELETE "
    "ON experiment_lifecycle_event FOR EACH ROW EXECUTE FUNCTION experiment_row_is_immutable()",
    "CREATE TRIGGER experiment_exposure_no_update BEFORE UPDATE ON experiment_exposure "
    "FOR EACH ROW EXECUTE FUNCTION experiment_row_is_immutable()",
    "CREATE TRIGGER experiment_analysis_no_update BEFORE UPDATE ON experiment_analysis "
    "FOR EACH ROW EXECUTE FUNCTION experiment_row_is_immutable()",
)


def upgrade() -> None:
    op.create_table(
        "experiment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_key", sa.String(length=64), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("mutation_run_id", sa.Uuid(), nullable=False),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=False),
        sa.Column("control_spec_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_spec_id", sa.Uuid(), nullable=False),
        sa.Column("control_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("candidate_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("control_allocation_bp", sa.Integer(), nullable=False),
        sa.Column("candidate_allocation_bp", sa.Integer(), nullable=False),
        sa.Column("primary_metric", sa.String(length=64), nullable=False),
        sa.Column("guardrail_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("minimum_sample_per_variant", sa.Integer(), nullable=False),
        sa.Column("traffic_source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (started_at IS NULL) OR status = 'stopped'",
            name=op.f("ck_experiment_started_at_matches_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('stopped', 'completed')) = (stopped_at IS NOT NULL)",
            name=op.f("ck_experiment_stopped_at_matches_status"),
        ),
        sa.CheckConstraint(
            "control_spec_hash ~ '^[0-9a-f]{64}$' AND candidate_spec_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_experiment_spec_hashes_are_sha256"),
        ),
        sa.CheckConstraint(
            "experiment_key ~ '^[a-z][a-z0-9_]{2,63}$'", name=op.f("ck_experiment_key_is_valid")
        ),
        sa.CheckConstraint(
            "primary_metric IN ('rage_click_session_rate', 'error_burst_session_rate', "
            "'form_error_session_rate', 'signup_submit_session_rate')",
            name=op.f("ck_experiment_primary_metric_is_known"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'stopped', 'completed')",
            name=op.f("ck_experiment_status_is_known"),
        ),
        sa.CheckConstraint(
            "stop_reason IS NULL OR stop_reason IN "
            "('human_decision', 'guardrail_concern', 'candidate_issue', 'planned_end')",
            name=op.f("ck_experiment_stop_reason_is_known"),
        ),
        sa.CheckConstraint(
            "traffic_source IN ('simulated', 'real')",
            name=op.f("ck_experiment_traffic_source_is_known"),
        ),
        sa.CheckConstraint(
            "candidate_allocation_bp IN (100, 500, 1000, 2500, 5000)",
            name=op.f("ck_experiment_candidate_allocation_allowlisted"),
        ),
        sa.CheckConstraint(
            "control_allocation_bp + candidate_allocation_bp = 10000",
            name=op.f("ck_experiment_allocation_sums_to_total"),
        ),
        sa.CheckConstraint(
            "control_spec_hash <> candidate_spec_hash", name=op.f("ck_experiment_variants_differ")
        ),
        sa.CheckConstraint(
            "control_spec_id <> candidate_spec_id", name=op.f("ck_experiment_variant_specs_differ")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(guardrail_metrics) = 'array' AND "
            "jsonb_array_length(guardrail_metrics) BETWEEN 1 AND 3 AND "
            'guardrail_metrics <@ \'["rage_click_session_rate", "error_burst_session_rate", '
            '"form_error_session_rate", "signup_submit_session_rate"]\'::jsonb AND '
            "NOT guardrail_metrics ? primary_metric",
            name=op.f("ck_experiment_guardrails_are_valid"),
        ),
        sa.CheckConstraint(
            "minimum_sample_per_variant BETWEEN 100 AND 100000",
            name=op.f("ck_experiment_minimum_sample_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_evaluation_run_id"],
            ["candidate_evaluation_run.id"],
            name=op.f("fk_experiment_candidate_evaluation_run_id_candidate_evaluation_run"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_experiment_candidate_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["control_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_experiment_control_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"],
            ["hypothesis.id"],
            name=op.f("fk_experiment_hypothesis_id_hypothesis"),
        ),
        sa.ForeignKeyConstraint(
            ["mutation_run_id"],
            ["mutation_run.id"],
            name=op.f("fk_experiment_mutation_run_id_mutation_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment")),
        sa.UniqueConstraint("experiment_key", name=op.f("uq_experiment_experiment_key")),
    )
    op.create_index(
        "uq_experiment_one_active_per_page",
        "experiment",
        ["page_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'paused')"),
    )
    op.create_table(
        "experiment_analysis",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_version", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("assessment", sa.String(length=32), nullable=False),
        sa.Column("data_sufficiency", sa.String(length=32), nullable=True),
        sa.Column("guardrail_status", sa.String(length=16), nullable=True),
        sa.Column("control_exposures", sa.Integer(), nullable=False),
        sa.Column("candidate_exposures", sa.Integer(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (error_type IS NULL)",
            name=op.f("ck_experiment_analysis_error_type_matches"),
        ),
        sa.CheckConstraint(
            "assessment IN "
            "('insufficient_data', 'evidence_ready', 'needs_review', 'stop_recommended')",
            name=op.f("ck_experiment_analysis_assessment_is_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0",
            name=op.f("ck_experiment_analysis_reason_codes_not_empty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object'", name=op.f("ck_experiment_analysis_report_is_object")
        ),
        sa.CheckConstraint(
            "report_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_experiment_analysis_report_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "status = 'completed' OR assessment = 'needs_review'",
            name=op.f("ck_experiment_analysis_analysis_error_needs_review"),
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'analysis_error')",
            name=op.f("ck_experiment_analysis_status_is_known"),
        ),
        sa.CheckConstraint(
            "control_exposures >= 0 AND candidate_exposures >= 0",
            name=op.f("ck_experiment_analysis_exposures_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment.id"],
            name=op.f("fk_experiment_analysis_experiment_id_experiment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_analysis")),
    )
    op.create_index(
        "ix_experiment_analysis_experiment_id",
        "experiment_analysis",
        ["experiment_id"],
        unique=False,
    )
    op.create_table(
        "experiment_lifecycle_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_experiment_lifecycle_event_sequence_not_negative")
        ),
        sa.CheckConstraint(
            "(sequence = 0) = (from_status IS NULL) AND (sequence > 0 OR to_status = 'draft')",
            name=op.f("ck_experiment_lifecycle_event_first_event_is_draft"),
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('draft', 'running', 'paused', 'stopped', 'completed')",
            name=op.f("ck_experiment_lifecycle_event_from_status_is_known"),
        ),
        sa.CheckConstraint(
            "to_status IN ('draft', 'running', 'paused', 'stopped', 'completed')",
            name=op.f("ck_experiment_lifecycle_event_to_status_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment.id"],
            name=op.f("fk_experiment_lifecycle_event_experiment_id_experiment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_lifecycle_event")),
        sa.UniqueConstraint("experiment_id", "sequence", name="uq_experiment_lifecycle_sequence"),
    )
    op.create_table(
        "experiment_exposure",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("exposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "spec_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_experiment_exposure_spec_hash_is_sha256")
        ),
        sa.CheckConstraint(
            "variant IN ('control', 'candidate')",
            name=op.f("ck_experiment_exposure_variant_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["user_event.event_id"],
            name=op.f("fk_experiment_exposure_event_id_user_event"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment.id"],
            name=op.f("fk_experiment_exposure_experiment_id_experiment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_exposure")),
        sa.UniqueConstraint("experiment_id", "session_id", name="uq_experiment_exposure_session"),
    )
    op.execute(EXPERIMENT_FUNCTION)
    op.execute(LIFECYCLE_RECORD_FUNCTION)
    op.execute(LIFECYCLE_GUARD_FUNCTION)
    op.execute(IMMUTABLE_FUNCTION)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    for trigger, table in (
        ("experiment_lifecycle_event_immutable", "experiment_lifecycle_event"),
        ("experiment_lifecycle_event_guard", "experiment_lifecycle_event"),
        ("experiment_lifecycle_record", "experiment"),
        ("experiment_analysis_no_update", "experiment_analysis"),
        ("experiment_exposure_no_update", "experiment_exposure"),
        ("experiment_guard", "experiment"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS experiment_row_is_immutable()")
    op.execute("DROP FUNCTION IF EXISTS experiment_guard_update()")
    op.execute("DROP FUNCTION IF EXISTS experiment_record_lifecycle()")
    op.execute("DROP FUNCTION IF EXISTS experiment_lifecycle_guard_insert()")
    op.drop_table("experiment_lifecycle_event")
    op.drop_table("experiment_exposure")
    op.drop_index("ix_experiment_analysis_experiment_id", table_name="experiment_analysis")
    op.drop_table("experiment_analysis")
    op.drop_index(
        "uq_experiment_one_active_per_page",
        table_name="experiment",
        postgresql_where=sa.text("status IN ('running', 'paused')"),
    )
    op.drop_table("experiment")
