"""Product Memory against darwin_test: pgvector, ingestion idempotency, retrieval, evaluation.

Fixture documents only (tests/fixtures/memory); changed versions are built in
memory, so committed docs are never modified. Everything runs on the
rolled-back `connection` fixture.
"""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, func, inspect, select, text
from sqlalchemy.orm import Session

from darwin.db.models import KnowledgeChunk, KnowledgeDocument, RetrievalRun
from darwin.memory.chunking import SMALL, STANDARD
from darwin.memory.corpus import CorpusEntry, SourceDocument, load_corpus, load_entry
from darwin.memory.embeddings import HashingEmbeddingProvider
from darwin.memory.evaluation import GOLDEN_PATH, load_golden, run_evaluation
from darwin.memory.ingest import ingest_corpus, ingest_document
from darwin.memory.retrieval import RetrievalFilters, retrieve

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "memory"
FIXTURE_CORPUS = (
    CorpusEntry("repo_document", "alpha.md", "markdown"),
    CorpusEntry("repo_document", "beta.md", "markdown"),
    CorpusEntry("ui_spec", "spec.json", "ui_spec"),
)


class CountingProvider(HashingEmbeddingProvider):
    """The hashing provider, counting how many texts it was asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded = 0

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return super().embed_texts(texts)


def _session(connection: Connection) -> Session:
    return Session(bind=connection, join_transaction_mode="create_savepoint")


def _fixture_docs() -> list[SourceDocument]:
    return load_corpus(FIXTURE_CORPUS, root=FIXTURES, include_generated=False)


def _counts(connection: Connection) -> tuple[int, int]:
    documents = connection.scalar(select(func.count()).select_from(KnowledgeDocument))
    chunks = connection.scalar(select(func.count()).select_from(KnowledgeChunk))
    return int(documents or 0), int(chunks or 0)


@pytest.fixture
def ingested(connection: Connection) -> CountingProvider:
    provider = CountingProvider()
    with _session(connection) as session:
        ingest_corpus(session, provider, _fixture_docs())
    return provider


# ---- pgvector + persistence -----------------------------------------------------------


def test_pgvector_is_enabled(connection: Connection) -> None:
    version = connection.scalar(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )

    assert version is not None


def test_chunks_are_stored_with_384_d_vectors(
    connection: Connection, ingested: CountingProvider
) -> None:
    dims = connection.scalars(
        text("SELECT DISTINCT vector_dims(embedding) FROM knowledge_chunk")
    ).all()
    documents, chunks = _counts(connection)

    assert dims == [384]
    assert documents == 3 and chunks > 3
    doc = connection.execute(
        select(KnowledgeDocument.chunker, KnowledgeDocument.embedding_model).where(
            KnowledgeDocument.source_key == "alpha.md"
        )
    ).one()
    assert tuple(doc) == ("markdown-sections:v1:2000", "hashing-bow:v1:384")


# ---- idempotency and updates ------------------------------------------------------------


def test_unchanged_ingestion_is_a_no_op(connection: Connection, ingested: CountingProvider) -> None:
    before_counts = _counts(connection)
    before_ids = set(connection.scalars(select(KnowledgeChunk.id)).all())
    embedded_before = ingested.embedded

    with _session(connection) as session:
        outcomes = ingest_corpus(session, ingested, _fixture_docs())

    assert {o.status for o in outcomes} == {"unchanged"}
    assert ingested.embedded == embedded_before  # nothing re-embedded
    assert _counts(connection) == before_counts
    assert set(connection.scalars(select(KnowledgeChunk.id)).all()) == before_ids


def test_changed_content_replaces_chunks_and_stale_text_is_never_retrieved(
    connection: Connection, ingested: CountingProvider
) -> None:
    alpha = load_entry(FIXTURE_CORPUS[0], root=FIXTURES)
    old_hash = connection.scalar(
        select(KnowledgeDocument.content_hash).where(KnowledgeDocument.source_key == "alpha.md")
    )
    changed = replace(
        alpha,
        content=alpha.content.replace("tomatoes", "cucumbers").replace("tomato", "cucumber"),
    )

    with _session(connection) as session:
        outcome = ingest_document(session, ingested, changed, STANDARD)

    assert outcome.status == "updated"
    new_hash = connection.scalar(
        select(KnowledgeDocument.content_hash).where(KnowledgeDocument.source_key == "alpha.md")
    )
    assert new_hash != old_hash
    stored = " ".join(connection.scalars(select(KnowledgeChunk.text)).all())
    assert "tomato" not in stored and "cucumber" in stored
    with _session(connection) as session:
        results = retrieve(session, ingested, "planting tomatoes", top_k=10)
    assert all("tomato" not in r.text for r in results)
    documents, _ = _counts(connection)
    assert documents == 3  # still one row per source


def test_changing_the_chunker_rebuilds_even_with_identical_content(
    connection: Connection, ingested: CountingProvider
) -> None:
    with _session(connection) as session:
        outcome = ingest_document(session, ingested, _fixture_docs()[0], SMALL)

    assert outcome.status == "updated"


def test_documents_removed_from_the_corpus_are_pruned(
    connection: Connection, ingested: CountingProvider
) -> None:
    with _session(connection) as session:
        ingest_corpus(session, ingested, _fixture_docs()[:1])  # only alpha remains

    keys = connection.scalars(select(KnowledgeDocument.source_key)).all()
    orphans = connection.scalar(
        text(
            "SELECT count(*) FROM knowledge_chunk c "
            "LEFT JOIN knowledge_document d ON d.id = c.document_id WHERE d.id IS NULL"
        )
    )
    assert keys == ["alpha.md"]
    assert orphans == 0


# ---- retrieval --------------------------------------------------------------------------


def test_vector_search_is_ordered_and_relevant(
    connection: Connection, ingested: CountingProvider
) -> None:
    with _session(connection) as session:
        results = retrieve(session, ingested, "how often should I lubricate my bicycle chain", 3)

    assert results[0].source_key == "beta.md"
    assert results[0].section == "Chain Maintenance"
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert [r.rank for r in results] == [1, 2, 3]


def test_retrieval_is_deterministic(connection: Connection, ingested: CountingProvider) -> None:
    with _session(connection) as session:
        first = retrieve(session, ingested, "compost soil", 5)
        second = retrieve(session, ingested, "compost soil", 5)

    assert [(r.chunk_id, r.score) for r in first] == [(r.chunk_id, r.score) for r in second]


@pytest.mark.parametrize(
    ("filters", "expected_sources"),
    [
        (RetrievalFilters(source_type="ui_spec"), {"spec.json"}),
        (RetrievalFilters(source_key="beta.md"), {"beta.md"}),
        (RetrievalFilters(generation=7), {"spec.json"}),
        (RetrievalFilters(generation=0), set()),
    ],
)
def test_filters_restrict_results(
    connection: Connection,
    ingested: CountingProvider,
    filters: RetrievalFilters,
    expected_sources: set[str],
) -> None:
    with _session(connection) as session:
        results = retrieve(session, ingested, "telescope compost bicycle", 10, filters)

    assert {r.source_key for r in results} == expected_sources


def test_injected_instructions_are_returned_as_plain_data(
    connection: Connection, ingested: CountingProvider
) -> None:
    with _session(connection) as session:
        results = retrieve(session, ingested, "ignore previous instructions system prompt", 1)

    assert results[0].source_key == "beta.md"
    assert "Ignore previous instructions" in results[0].text  # retrieved, not obeyed


def test_retrieval_runs_are_recorded_compactly(
    connection: Connection, ingested: CountingProvider
) -> None:
    with _session(connection) as session:
        results = retrieve(
            session, ingested, "compost", 2, RetrievalFilters(source_key="alpha.md"), record=True
        )

    with _session(connection) as session:
        run = session.scalars(select(RetrievalRun)).one()
    assert (run.query, run.top_k, run.filters) == ("compost", 2, {"source_key": "alpha.md"})
    assert run.embedding_model == "hashing-bow:v1:384"
    assert [r["chunk_id"] for r in run.results] == [str(r.chunk_id) for r in results]
    assert all(
        set(r) == {"rank", "chunk_id", "source_key", "section", "score"} for r in run.results
    )
    assert run.latency_ms >= 0


def test_a_query_without_words_returns_nothing(
    connection: Connection, ingested: CountingProvider
) -> None:
    with _session(connection) as session:
        assert retrieve(session, ingested, "the and of ?", 5) == []


# ---- migration + evaluation ---------------------------------------------------------------


def test_migration_0004_reverses_and_keeps_other_tables_and_the_extension(
    migrated_engine: Engine, alembic_cfg: Config
) -> None:
    command.downgrade(alembic_cfg, "0003")
    try:
        inspector = inspect(migrated_engine)
        assert not inspector.has_table("knowledge_chunk")
        assert not inspector.has_table("knowledge_document")
        assert not inspector.has_table("retrieval_run")
        for table in ("user_event", "behavior_signal", "queue_message"):
            assert inspector.has_table(table)
        with migrated_engine.connect() as conn:
            assert (
                conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 1
            )
    finally:
        command.upgrade(alembic_cfg, "head")

    assert inspect(migrated_engine).has_table("knowledge_chunk")


def test_golden_evaluation_runs_and_is_reproducible(migrated_engine: Engine) -> None:
    documents = load_corpus()
    dataset = load_golden(GOLDEN_PATH, {d.source_key for d in documents})
    provider = HashingEmbeddingProvider()

    first = run_evaluation(migrated_engine, provider, dataset, documents, [SMALL, STANDARD], 5)
    second = run_evaluation(migrated_engine, provider, dataset, documents, [SMALL, STANDARD], 5)

    assert first == second  # deterministic provider + chunking => identical metrics
    for result in first:
        assert 0.0 <= result.precision_at_k <= 1.0
        assert 0.0 <= result.recall_at_k <= 1.0
        assert 0.0 <= result.mrr <= 1.0
        assert len(result.cases) == len(dataset.cases)
    assert first[0].chunks > first[1].chunks  # smaller chunks -> more of them
    with migrated_engine.connect() as conn:  # evaluation left nothing behind
        assert conn.scalar(select(func.count()).select_from(KnowledgeDocument)) == 0
