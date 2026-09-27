"""Ingestion: load -> normalise -> hash -> chunk -> embed -> persist.

    python -m darwin.memory.ingest [--config standard]     (make memory-ingest)

Replay-safe. A document is identified by (source_type, source_key). If its
content hash, chunker and embedding model are all unchanged, nothing happens —
no re-chunking, no re-embedding. Otherwise its chunks are rebuilt and the old
ones deleted *in the same transaction*, so a stale chunk is never retrievable.
Documents that leave the allowlist are pruned (with their chunks).

Logs name documents and counts only — never chunk text.
"""

import argparse
import hashlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from darwin.logging_config import configure_logging
from darwin.memory.chunking import CONFIGS, STANDARD, ChunkerConfig, chunk_document
from darwin.memory.corpus import SourceDocument, load_corpus
from darwin.memory.embeddings import EmbeddingProvider, HashingEmbeddingProvider, require_dimension

logger = logging.getLogger(__name__)

CHUNK_ID_NAMESPACE = uuid.UUID("a0f4c1b2-7d35-4e8a-9b61-2c5d8e3f7a10")


class IngestError(RuntimeError):
    """Raised with a message that never contains document content."""


@dataclass(frozen=True)
class IngestOutcome:
    source_key: str
    status: Literal["created", "updated", "unchanged"]
    chunks: int


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_id(document: SourceDocument, index: int, text_hash: str) -> uuid.UUID:
    return uuid.uuid5(
        CHUNK_ID_NAMESPACE, f"{document.source_type}|{document.source_key}|{index}|{text_hash}"
    )


def ingest_document(
    session: Session,
    provider: EmbeddingProvider,
    document: SourceDocument,
    config: ChunkerConfig = STANDARD,
) -> IngestOutcome:
    """Bring one document's stored chunks in line with its current content. Owns its transaction."""
    require_dimension(provider)
    content_hash = sha256(document.content)
    chunker = config.name(document.kind)

    with session.begin():
        existing = session.scalars(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.source_type == document.source_type,
                KnowledgeDocument.source_key == document.source_key,
            )
            .with_for_update()
        ).one_or_none()
        if (
            existing is not None
            and existing.content_hash == content_hash
            and existing.chunker == chunker
            and existing.embedding_model == provider.name
        ):
            count = session.scalar(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == existing.id)
            )
            return IngestOutcome(document.source_key, "unchanged", int(count or 0))

        chunks = chunk_document(document, config)
        try:
            vectors = provider.embed_texts([c.text for c in chunks])
        except Exception as error:
            raise IngestError(
                f"{document.source_key}: embedding failed ({type(error).__name__})"
            ) from None
        if len(vectors) != len(chunks) or any(len(v) != provider.dimension for v in vectors):
            raise IngestError(f"{document.source_key}: provider returned malformed embeddings")

        if existing is None:
            existing = KnowledgeDocument(
                source_type=document.source_type, source_key=document.source_key
            )
            session.add(existing)
            status: Literal["created", "updated"] = "created"
        else:
            session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == existing.id))
            status = "updated"
        existing.title = document.title[:256]
        existing.content_hash = content_hash
        existing.chunker = chunker
        existing.embedding_model = provider.name
        existing.meta = {"kind": document.kind, **document.metadata}
        existing.updated_at = func.now()
        session.flush()

        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            text_hash = sha256(chunk.text)
            session.add(
                KnowledgeChunk(
                    id=chunk_id(document, index, text_hash),
                    document_id=existing.id,
                    chunk_index=index,
                    section=chunk.section[:256],
                    text=chunk.text,
                    text_hash=text_hash,
                    char_count=len(chunk.text),
                    embedding=vector,
                    meta={
                        **chunk.metadata,
                        "source_type": document.source_type,
                        "source_key": document.source_key,
                        "chunk_index": index,
                    },
                )
            )
    logger.info(
        "knowledge document ingested",
        extra={
            "context": {"source_key": document.source_key, "status": status, "chunks": len(chunks)}
        },
    )
    return IngestOutcome(document.source_key, status, len(chunks))


def prune_documents(session: Session, keep: Sequence[SourceDocument]) -> int:
    """Delete documents (and, by cascade, chunks) that are no longer in the corpus."""
    keys = [(d.source_type, d.source_key) for d in keep]
    with session.begin():
        statement = delete(KnowledgeDocument)
        if keys:
            statement = statement.where(
                tuple_(KnowledgeDocument.source_type, KnowledgeDocument.source_key).not_in(keys)
            )
        removed = session.execute(statement.returning(KnowledgeDocument.source_key)).all()
    for (source_key,) in removed:
        logger.info("knowledge document pruned", extra={"context": {"source_key": source_key}})
    return len(removed)


def ingest_corpus(
    session: Session,
    provider: EmbeddingProvider,
    documents: Sequence[SourceDocument],
    config: ChunkerConfig = STANDARD,
    prune: bool = True,
) -> list[IngestOutcome]:
    outcomes = [ingest_document(session, provider, d, config) for d in documents]
    if prune:
        prune_documents(session, documents)
    return outcomes


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ingest the allowlisted Product Memory corpus.")
    parser.add_argument("--config", choices=sorted(CONFIGS), default=STANDARD.label)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(str(settings.database_url))
    provider = HashingEmbeddingProvider()
    try:
        with Session(engine) as session:
            outcomes = ingest_corpus(session, provider, load_corpus(), CONFIGS[args.config])
    finally:
        engine.dispose()
    for o in outcomes:
        print(f"{o.status:<9} {o.chunks:>3} chunks  {o.source_key}")
    print(f"embedding model: {provider.name}   chunker config: {args.config}")


if __name__ == "__main__":
    main()
