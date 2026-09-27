"""create product memory

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27

Product Memory (Step 8): knowledge_document, knowledge_chunk (with a
vector(384) embedding), retrieval_run.

pgvector: `vector` is not a *trusted* extension, so the application role
cannot create it. The one-time superuser setup (`make db-setup`; on RDS the
rds_superuser role) enables it; this migration only asserts it with
CREATE EXTENSION IF NOT EXISTS, which is a no-op once it exists and fails
with a clear message otherwise.

Downgrade drops the three tables but deliberately leaves the extension
installed: other objects may depend on it, dropping it needs superuser
rights, and leaving it is harmless.

Indexes: only the unique constraints. Search is exact (sequential scan over a
small corpus); an HNSW index is deferred until the corpus is large enough for
latency to matter and recall can be measured against exact search.

Generated with --autogenerate, then reviewed and edited. 0001-0003 unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSION = 384  # must match darwin.db.models.knowledge.EMBEDDING_DIMENSION


def upgrade() -> None:
    bind = op.get_bind()
    installed = bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
    if installed is None:
        try:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except sa.exc.DBAPIError as error:
            raise RuntimeError(
                "pgvector is not enabled in this database and the migration role may not "
                "enable it. Run `make db-setup` (local) or enable it as rds_superuser (AWS)."
            ) from error

    op.create_table(
        "knowledge_document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("chunker", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_knowledge_document_content_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name=op.f("ck_knowledge_document_metadata_is_object"),
        ),
        sa.CheckConstraint(
            "source_key <> ''", name=op.f("ck_knowledge_document_source_key_not_empty")
        ),
        sa.CheckConstraint(
            "source_type IN ('repo_document', 'ui_spec', 'system_generated')",
            name=op.f("ck_knowledge_document_source_type_is_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_document")),
        sa.UniqueConstraint("source_type", "source_key", name="uq_knowledge_document_source"),
    )
    op.create_table(
        "retrieval_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("query", sa.String(length=1000), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(filters) = 'object'", name=op.f("ck_retrieval_run_filters_is_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(results) = 'array'", name=op.f("ck_retrieval_run_results_is_array")
        ),
        sa.CheckConstraint("top_k BETWEEN 1 AND 50", name=op.f("ck_retrieval_run_top_k_in_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_run")),
    )
    op.create_table(
        "knowledge_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=256), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=False),
        sa.Column(
            "metadata",
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
            "jsonb_typeof(metadata) = 'object'", name=op.f("ck_knowledge_chunk_metadata_is_object")
        ),
        sa.CheckConstraint("text <> ''", name=op.f("ck_knowledge_chunk_text_not_empty")),
        sa.CheckConstraint(
            "text_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_knowledge_chunk_text_hash_is_sha256")
        ),
        sa.CheckConstraint(
            "char_count = char_length(text)", name=op.f("ck_knowledge_chunk_char_count_matches")
        ),
        sa.CheckConstraint(
            "chunk_index >= 0", name=op.f("ck_knowledge_chunk_chunk_index_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.id"],
            name=op.f("fk_knowledge_chunk_document_id_knowledge_document"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunk")),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_position"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_chunk")
    op.drop_table("retrieval_run")
    op.drop_table("knowledge_document")
    # The `vector` extension is intentionally left installed (see module docstring).
