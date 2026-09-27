"""Product Memory: documents, their chunks (with embeddings), and retrieval runs.

- KnowledgeDocument: one allowlisted source (a repo doc, a UI Spec, a generated
  summary), identified by (source_type, source_key). Its current content_hash,
  chunker and embedding model say exactly how its active chunks were built.
- KnowledgeChunk: a retrievable piece of a document plus its embedding. Chunks
  are *derived data*: when a document changes they are replaced in the same
  transaction, so stale chunks can never be retrieved.
- RetrievalRun: a compact trace of one retrieval (query, filters, which chunks
  came back with which scores). No chunk text is copied, no model reasoning.

Retrieved text is untrusted data, never instructions (docs/RAG_ARCHITECTURE.md).
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from darwin.db.base import Base

# Locked by migration 0004. A provider with another dimension needs a new
# migration and a full re-embed (every chunk records its embedding_model).
EMBEDDING_DIMENSION = 384

SOURCE_TYPES = ("repo_document", "ui_spec", "system_generated")


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_document"
    __table_args__ = (
        UniqueConstraint("source_type", "source_key", name="uq_knowledge_document_source"),
        CheckConstraint(
            "source_type IN ('repo_document', 'ui_spec', 'system_generated')",
            name="source_type_is_known",
        ),
        CheckConstraint("source_key <> ''", name="source_key_not_empty"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_is_sha256"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_is_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(32))
    # Stable identity within the source type, e.g. "docs/MUTATION_SAFETY.md".
    source_key: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(256))
    # sha256 of the normalised content. Unchanged hash + chunker + model = nothing to do.
    content_hash: Mapped[str] = mapped_column(String(64))
    chunker: Mapped[str] = mapped_column(String(64))  # e.g. "md-sections:v1:2000"
    embedding_model: Mapped[str] = mapped_column(String(64))  # e.g. "hashing-bow:v1:384"
    # `metadata` is reserved by SQLAlchemy's declarative API; the column keeps the name.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        # Also serves "all chunks of a document" (leading column).
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_position"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_not_negative"),
        CheckConstraint("text <> ''", name="text_not_empty"),
        CheckConstraint("char_count = char_length(text)", name="char_count_matches"),
        CheckConstraint("text_hash ~ '^[0-9a-f]{64}$'", name="text_hash_is_sha256"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_is_object"),
    )

    # Deterministic (UUID5 of source + chunk position + text hash): rebuilding
    # the same content gives the same ids.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_document.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int]
    section: Mapped[str] = mapped_column(String(256))  # heading path, e.g. "Validation Pipeline"
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    # Characters, not tokens: no model-specific tokenizer is chosen yet.
    char_count: Mapped[int]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetrievalRun(Base):
    __tablename__ = "retrieval_run"
    __table_args__ = (
        CheckConstraint("top_k BETWEEN 1 AND 50", name="top_k_in_range"),
        CheckConstraint("jsonb_typeof(filters) = 'object'", name="filters_is_object"),
        CheckConstraint("jsonb_typeof(results) = 'array'", name="results_is_array"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    query: Mapped[str] = mapped_column(String(1000))
    top_k: Mapped[int]
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # [{rank, chunk_id, source_key, section, score}] — ids and scores, no chunk text.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    embedding_model: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
