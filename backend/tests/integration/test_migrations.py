import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, text

from darwin.config import Settings
from darwin.db.base import Base
from darwin.main import create_app

pytestmark = pytest.mark.integration


def test_server_is_postgresql_17(migrated_engine: Engine) -> None:
    # Catches PostgreSQL 14/16 answering on port 5432 instead of 17.
    with migrated_engine.connect() as connection:
        version = int(connection.execute(text("SHOW server_version_num")).scalar_one())

    assert version // 10000 == 17


def test_database_is_at_the_latest_migration(migrated_engine: Engine, alembic_cfg: Config) -> None:
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    with migrated_engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    assert current == head == "0011"


def test_user_event_table_has_the_expected_shape(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    columns = {column["name"]: column for column in inspector.get_columns("user_event")}
    assert set(columns) == {
        "id",
        "event_id",
        "event_type",
        "session_id",
        "occurred_at",
        "received_at",
        "payload",
        # Step 15 UI attribution: nullable by design (NULL = unknown, never backfilled).
        "ui_generation",
        "ui_spec_hash",
        "ui_spec_version_id",
    }
    attribution = {"ui_generation", "ui_spec_hash", "ui_spec_version_id"}
    assert all(column["nullable"] is (name in attribution) for name, column in columns.items())
    assert inspector.get_pk_constraint("user_event")["constrained_columns"] == ["id"]
    assert [c["column_names"] for c in inspector.get_unique_constraints("user_event")] == [
        ["event_id"]
    ]
    assert {c["name"] for c in inspector.get_check_constraints("user_event")} == {
        "ck_user_event_event_type_not_empty",
        "ck_user_event_payload_is_object",
        "ck_user_event_ui_generation_in_range",
        "ck_user_event_ui_spec_hash_is_sha256",
    }


def test_migration_matches_the_orm_models(migrated_engine: Engine) -> None:
    # The same comparison `alembic revision --autogenerate` does. An empty diff
    # means the hand-written migration and the Python model describe one schema.
    with migrated_engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert differences == []


def test_downgrade_and_upgrade_and_startup_never_creates_tables(
    migrated_engine: Engine, alembic_cfg: Config, integration_settings: Settings
) -> None:
    command.downgrade(alembic_cfg, "base")
    try:
        assert not inspect(migrated_engine).has_table("user_event")

        # Starting the app must not create the schema behind Alembic's back.
        with TestClient(create_app(integration_settings)) as client:
            assert client.get("/api/v1/health/live").status_code == 200
        assert not inspect(migrated_engine).has_table("user_event")
    finally:
        command.upgrade(alembic_cfg, "head")

    assert inspect(migrated_engine).has_table("user_event")


def test_queue_migration_downgrades_and_upgrades(
    migrated_engine: Engine, alembic_cfg: Config
) -> None:
    command.downgrade(alembic_cfg, "0002")
    try:
        assert not inspect(migrated_engine).has_table("queue_message")
        assert inspect(migrated_engine).has_table("behavior_signal")  # 0002 untouched
    finally:
        command.upgrade(alembic_cfg, "head")

    inspector = inspect(migrated_engine)
    assert inspector.has_table("queue_message")
    assert [c["column_names"] for c in inspector.get_unique_constraints("queue_message")] == [
        ["message_id"]
    ]
    indexes = {i["name"]: i for i in inspector.get_indexes("queue_message")}
    claim_index = indexes["ix_queue_message_pending_visible_at"]
    assert claim_index["column_names"] == ["visible_at"]
    assert "status" in str(claim_index["dialect_options"]["postgresql_where"])  # partial
