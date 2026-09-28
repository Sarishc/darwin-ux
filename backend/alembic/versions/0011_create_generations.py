"""create generations

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-28

Human approval, generation promotion and rollback (Step 15).

- ui_spec_version: new status `promoted` (a generation created by an executed
  promotion: generation set, parent = the candidate it came from). A BEFORE INSERT
  trigger requires a promoted row's content to equal its candidate's content except
  `generation`, and its `generation` field to match. A deferred check requires a
  generation_promotion record for it at commit.
- active_generation: one pointer per page. A trigger allows INSERT only as a
  bootstrap to the page's Generation 0 baseline, forbids DELETE, and allows an
  UPDATE only when change_id names a NEW promotion/rollback record describing
  exactly that move (from the current spec to the new one). It can never point at
  a candidate.
- promotion_approval, generation_promotion, generation_rollback: immutable audit
  records (no UPDATE, no DELETE). One approve per evidence hash; one promotion per
  approval (replay refused); promotions move forward, rollbacks move back.
  Deferred checks refuse a commit where a promotion/rollback record exists but the
  pointer does not name it.
- user_event: claimed ui_generation / ui_spec_hash and the server-verified
  ui_spec_version_id (NULL = unknown; nothing is backfilled).
- behavior_signal: ui_attribution (single | mixed | unknown) + ui_spec_version_id.

Downgrade drops all of it. It refuses while any promoted generation is referenced
by later work (a candidate, mutation run or experiment), and otherwise deletes the
promoted rows, since pre-0011 schemas cannot represent them.

Generated with --autogenerate (tables, columns), then reviewed; CHECK changes on
existing tables and all triggers are hand-written. 0001-0010 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_FUNCTION = """
CREATE FUNCTION generation_row_is_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable (id %)', TG_TABLE_NAME, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""

PROMOTED_SPEC_FUNCTION = """
CREATE FUNCTION promoted_spec_matches_candidate() RETURNS trigger AS $$
DECLARE
    candidate ui_spec_version%ROWTYPE;
BEGIN
    IF NEW.status <> 'promoted' THEN
        RETURN NEW;
    END IF;
    SELECT * INTO candidate FROM ui_spec_version WHERE id = NEW.parent_id;
    IF NOT FOUND OR candidate.status <> 'candidate' OR candidate.page_id <> NEW.page_id
       OR (NEW.spec - 'generation') IS DISTINCT FROM (candidate.spec - 'generation')
       OR (NEW.spec ->> 'generation')::int IS DISTINCT FROM NEW.generation THEN
        RAISE EXCEPTION 'a promoted generation must equal its candidate except generation (id %)',
            NEW.id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

PROMOTED_SPEC_AUDITED_FUNCTION = """
CREATE FUNCTION promoted_spec_has_promotion() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'promoted' AND NOT EXISTS
       (SELECT 1 FROM generation_promotion WHERE promoted_spec_id = NEW.id) THEN
        RAISE EXCEPTION 'a promoted generation needs a promotion record (id %)', NEW.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

POINTER_GUARD_FUNCTION = """
CREATE FUNCTION active_generation_guard() RETURNS trigger AS $$
DECLARE
    spec ui_spec_version%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'active generation pointers cannot be deleted (page %)', OLD.page_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    SELECT * INTO spec FROM ui_spec_version WHERE id = NEW.ui_spec_version_id;
    IF NOT FOUND OR spec.status NOT IN ('baseline', 'promoted')
       OR spec.page_id <> NEW.page_id OR spec.generation <> NEW.generation THEN
        RAISE EXCEPTION 'the active generation must be a generation of this page (page %)',
            NEW.page_id USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.change_kind <> 'bootstrap' OR spec.status <> 'baseline' OR spec.generation <> 0 THEN
            RAISE EXCEPTION 'a pointer is bootstrapped to Generation 0 only (page %)',
                NEW.page_id USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.page_id <> OLD.page_id OR NEW.change_id IS NOT DISTINCT FROM OLD.change_id THEN
        RAISE EXCEPTION 'every pointer move needs a new promotion or rollback record (page %)',
            OLD.page_id USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.change_kind = 'promotion' AND EXISTS (
           SELECT 1 FROM generation_promotion p WHERE p.id = NEW.change_id
           AND p.page_id = NEW.page_id AND p.promoted_spec_id = NEW.ui_spec_version_id
           AND p.from_spec_id = OLD.ui_spec_version_id AND p.to_generation = NEW.generation)
       OR NEW.change_kind = 'rollback' AND EXISTS (
           SELECT 1 FROM generation_rollback r WHERE r.id = NEW.change_id
           AND r.page_id = NEW.page_id AND r.to_spec_id = NEW.ui_spec_version_id
           AND r.from_spec_id = OLD.ui_spec_version_id AND r.to_generation = NEW.generation) THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'the pointer move does not match its promotion/rollback record (page %)',
        OLD.page_id USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""

CHANGE_APPLIED_FUNCTION = """
CREATE FUNCTION generation_change_is_applied() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM active_generation a
                   WHERE a.page_id = NEW.page_id AND a.change_id = NEW.id) THEN
        RAISE EXCEPTION '% record without the matching pointer move (id %)', TG_TABLE_NAME, NEW.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

FUNCTIONS = (
    IMMUTABLE_FUNCTION,
    PROMOTED_SPEC_FUNCTION,
    PROMOTED_SPEC_AUDITED_FUNCTION,
    POINTER_GUARD_FUNCTION,
    CHANGE_APPLIED_FUNCTION,
)
TRIGGERS = (
    (
        "promoted_spec_matches_candidate",
        "ui_spec_version",
        "BEFORE INSERT ON ui_spec_version FOR EACH ROW "
        "EXECUTE FUNCTION promoted_spec_matches_candidate()",
    ),
    (
        "promoted_spec_has_promotion",
        "ui_spec_version",
        "AFTER INSERT ON ui_spec_version DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION promoted_spec_has_promotion()",
        True,
    ),
    (
        "active_generation_guard",
        "active_generation",
        "BEFORE INSERT OR UPDATE OR DELETE ON active_generation FOR EACH ROW "
        "EXECUTE FUNCTION active_generation_guard()",
    ),
    (
        "generation_promotion_applied",
        "generation_promotion",
        "AFTER INSERT ON generation_promotion DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION generation_change_is_applied()",
        True,
    ),
    (
        "generation_rollback_applied",
        "generation_rollback",
        "AFTER INSERT ON generation_rollback DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION generation_change_is_applied()",
        True,
    ),
    *(
        (
            f"{table}_immutable",
            table,
            f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION generation_row_is_immutable()",
        )
        for table in ("promotion_approval", "generation_promotion", "generation_rollback")
    ),
)
FUNCTION_NAMES = (
    "generation_row_is_immutable",
    "promoted_spec_matches_candidate",
    "promoted_spec_has_promotion",
    "active_generation_guard",
    "generation_change_is_applied",
)

DOWNGRADE_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM ui_spec_version p WHERE p.status = 'promoted' AND (
            EXISTS (SELECT 1 FROM ui_spec_version c WHERE c.parent_id = p.id)
            OR EXISTS (SELECT 1 FROM mutation_run m
                       WHERE p.id IN (m.source_spec_id, m.candidate_spec_id))
            OR EXISTS (SELECT 1 FROM experiment e
                       WHERE p.id IN (e.control_spec_id, e.candidate_spec_id))))
    THEN
        RAISE EXCEPTION 'cannot downgrade 0011: later work references a promoted generation';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "active_generation",
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("ui_spec_version_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("change_kind", sa.String(length=16), nullable=False),
        sa.Column("change_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(change_kind = 'bootstrap') = (change_id IS NULL)",
            name=op.f("ck_active_generation_change_id_matches_kind"),
        ),
        sa.CheckConstraint(
            "change_kind IN ('bootstrap', 'promotion', 'rollback')",
            name=op.f("ck_active_generation_change_kind_is_known"),
        ),
        sa.CheckConstraint(
            "generation >= 0", name=op.f("ck_active_generation_generation_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["ui_spec_version_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_active_generation_ui_spec_version_id_ui_spec_version"),
        ),
        sa.PrimaryKeyConstraint("page_id", name=op.f("pk_active_generation")),
    )
    op.create_table(
        "generation_rollback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("from_spec_id", sa.Uuid(), nullable=False),
        sa.Column("from_generation", sa.Integer(), nullable=False),
        sa.Column("to_spec_id", sa.Uuid(), nullable=False),
        sa.Column("to_generation", sa.Integer(), nullable=False),
        sa.Column("reviewer", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reviewer ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_generation_rollback_reviewer_is_valid"),
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500",
            name=op.f("ck_generation_rollback_reason_length"),
        ),
        sa.CheckConstraint(
            "to_generation < from_generation",
            name=op.f("ck_generation_rollback_rollback_moves_back"),
        ),
        sa.CheckConstraint(
            "to_spec_id <> from_spec_id", name=op.f("ck_generation_rollback_rollback_changes_spec")
        ),
        sa.ForeignKeyConstraint(
            ["from_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_generation_rollback_from_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["to_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_generation_rollback_to_spec_id_ui_spec_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_rollback")),
    )
    op.create_table(
        "promotion_approval",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_spec_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("experiment_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("source_spec_id", sa.Uuid(), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("blocking_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision <> 'approve' OR jsonb_array_length(blocking_reasons) = 0",
            name=op.f("ck_promotion_approval_approve_only_when_eligible"),
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')",
            name=op.f("ck_promotion_approval_decision_is_known"),
        ),
        sa.CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_promotion_approval_evidence_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(blocking_reasons) = 'array'",
            name=op.f("ck_promotion_approval_blocking_reasons_is_array"),
        ),
        sa.CheckConstraint(
            "reviewer ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_promotion_approval_reviewer_is_valid"),
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500",
            name=op.f("ck_promotion_approval_reason_length"),
        ),
        sa.CheckConstraint(
            "target_generation > source_generation",
            name=op.f("ck_promotion_approval_target_after_source"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_evaluation_run_id"],
            ["candidate_evaluation_run.id"],
            name=op.f("fk_promotion_approval_candidate_evaluation_run_id_candidate_evaluation_run"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_promotion_approval_candidate_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_analysis_id"],
            ["experiment_analysis.id"],
            name=op.f("fk_promotion_approval_experiment_analysis_id_experiment_analysis"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment.id"],
            name=op.f("fk_promotion_approval_experiment_id_experiment"),
        ),
        sa.ForeignKeyConstraint(
            ["source_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_promotion_approval_source_spec_id_ui_spec_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promotion_approval")),
    )
    op.create_index(
        "uq_promotion_approval_evidence",
        "promotion_approval",
        ["evidence_hash"],
        unique=True,
        postgresql_where=sa.text("decision = 'approve'"),
    )
    op.create_table(
        "generation_promotion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_spec_id", sa.Uuid(), nullable=False),
        sa.Column("promoted_spec_id", sa.Uuid(), nullable=False),
        sa.Column("from_spec_id", sa.Uuid(), nullable=False),
        sa.Column("from_generation", sa.Integer(), nullable=False),
        sa.Column("to_generation", sa.Integer(), nullable=False),
        sa.Column("reviewer", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_generation_promotion_evidence_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "reviewer ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_generation_promotion_reviewer_is_valid"),
        ),
        sa.CheckConstraint(
            "to_generation > from_generation",
            name=op.f("ck_generation_promotion_generation_moves_forward"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["promotion_approval.id"],
            name=op.f("fk_generation_promotion_approval_id_promotion_approval"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_generation_promotion_candidate_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["from_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_generation_promotion_from_spec_id_ui_spec_version"),
        ),
        sa.ForeignKeyConstraint(
            ["promoted_spec_id"],
            ["ui_spec_version.id"],
            name=op.f("fk_generation_promotion_promoted_spec_id_ui_spec_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_promotion")),
        sa.UniqueConstraint("approval_id", name=op.f("uq_generation_promotion_approval_id")),
        sa.UniqueConstraint(
            "promoted_spec_id", name=op.f("uq_generation_promotion_promoted_spec_id")
        ),
    )
    op.add_column(
        "behavior_signal",
        sa.Column("ui_attribution", sa.String(length=16), server_default="unknown", nullable=False),
    )
    op.add_column("behavior_signal", sa.Column("ui_spec_version_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_behavior_signal_ui_spec_version_id_ui_spec_version"),
        "behavior_signal",
        "ui_spec_version",
        ["ui_spec_version_id"],
        ["id"],
    )
    op.add_column("user_event", sa.Column("ui_generation", sa.Integer(), nullable=True))
    op.add_column("user_event", sa.Column("ui_spec_hash", sa.String(length=64), nullable=True))
    op.add_column("user_event", sa.Column("ui_spec_version_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_user_event_ui_spec_version_id_ui_spec_version"),
        "user_event",
        "ui_spec_version",
        ["ui_spec_version_id"],
        ["id"],
    )
    # ui_spec_version: the `promoted` status (constraints on an existing table).
    op.drop_constraint(op.f("ck_ui_spec_version_status_is_known"), "ui_spec_version", type_="check")
    op.drop_constraint(
        op.f("ck_ui_spec_version_generation_only_for_baseline"), "ui_spec_version", type_="check"
    )
    op.create_check_constraint(
        "status_is_known", "ui_spec_version", "status IN ('baseline', 'candidate', 'promoted')"
    )
    op.create_check_constraint(
        "generation_only_for_generations",
        "ui_spec_version",
        "(status IN ('baseline', 'promoted')) = (generation IS NOT NULL)",
    )
    op.create_check_constraint(
        "promoted_has_parent", "ui_spec_version", "status <> 'promoted' OR parent_id IS NOT NULL"
    )
    op.create_check_constraint(
        "ui_generation_in_range",
        "user_event",
        "ui_generation IS NULL OR ui_generation BETWEEN 0 AND 100000",
    )
    op.create_check_constraint(
        "ui_spec_hash_is_sha256",
        "user_event",
        "ui_spec_hash IS NULL OR ui_spec_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ui_attribution_is_known",
        "behavior_signal",
        "ui_attribution IN ('single', 'mixed', 'unknown')",
    )
    op.create_check_constraint(
        "ui_spec_version_only_when_single",
        "behavior_signal",
        "(ui_attribution = 'single') = (ui_spec_version_id IS NOT NULL)",
    )
    for function in FUNCTIONS:
        op.execute(function)
    for name, _table, body, *constraint in TRIGGERS:
        kind = "CONSTRAINT TRIGGER" if constraint else "TRIGGER"
        op.execute(f"CREATE {kind} {name} {body}")


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    for name, table, *_ in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for function in FUNCTION_NAMES:
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_constraint(
        op.f("fk_user_event_ui_spec_version_id_ui_spec_version"), "user_event", type_="foreignkey"
    )
    op.drop_column("user_event", "ui_spec_version_id")
    op.drop_column("user_event", "ui_spec_hash")
    op.drop_column("user_event", "ui_generation")
    op.drop_constraint(
        op.f("fk_behavior_signal_ui_spec_version_id_ui_spec_version"),
        "behavior_signal",
        type_="foreignkey",
    )
    op.drop_column("behavior_signal", "ui_spec_version_id")
    op.drop_column("behavior_signal", "ui_attribution")
    op.drop_table("generation_promotion")
    op.drop_index(
        "uq_promotion_approval_evidence",
        table_name="promotion_approval",
        postgresql_where=sa.text("decision = 'approve'"),
    )
    op.drop_table("promotion_approval")
    op.drop_table("generation_rollback")
    op.drop_table("active_generation")
    # Pre-0011 schemas cannot represent promoted generations (the guard above
    # proved nothing else references them).
    op.execute("DELETE FROM ui_spec_version WHERE status = 'promoted'")
    op.drop_constraint(op.f("ck_ui_spec_version_promoted_has_parent"), "ui_spec_version", "check")
    op.drop_constraint(
        op.f("ck_ui_spec_version_generation_only_for_generations"), "ui_spec_version", "check"
    )
    op.drop_constraint(op.f("ck_ui_spec_version_status_is_known"), "ui_spec_version", "check")
    op.create_check_constraint(
        "status_is_known", "ui_spec_version", "status IN ('baseline', 'candidate')"
    )
    op.create_check_constraint(
        "generation_only_for_baseline",
        "ui_spec_version",
        "(status = 'baseline') = (generation IS NOT NULL)",
    )
