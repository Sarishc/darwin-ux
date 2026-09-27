"""Retrieval evaluation on a golden dataset: Precision@K, Recall@K, MRR.

    python -m darwin.memory.evaluation [--k 5] [--configs small,standard,large]   (make memory-eval)

For each chunking config the corpus is ingested and queried inside ONE
transaction that is then rolled back: evaluation never changes stored memory,
and configs never see each other's chunks.

Definitions (per query, then averaged over queries — reported separately,
never combined into one score):

- a retrieved chunk is *relevant* if its source is one of the case's
  expected sources (and, when the case names sections, its section matches one);
- Precision@K = relevant chunks among the top K / K;
- Recall@K    = expected sources found among the top K / expected sources;
- reciprocal rank = 1 / rank of the first relevant chunk (0 if none in the top K);
  MRR = mean reciprocal rank.
"""

import argparse
import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models.knowledge import KnowledgeDocument
from darwin.memory.chunking import CONFIGS, ChunkerConfig
from darwin.memory.corpus import REPO_ROOT, SourceDocument, load_corpus
from darwin.memory.embeddings import EmbeddingProvider, HashingEmbeddingProvider
from darwin.memory.ingest import ingest_corpus
from darwin.memory.retrieval import RetrievedChunk, retrieve

GOLDEN_PATH = REPO_ROOT / "backend" / "tests" / "evals" / "golden" / "retrieval.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "retrieval-eval.json"


# ---- Golden dataset -------------------------------------------------------------------


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    query: str = Field(min_length=8, max_length=300)
    relevant_sources: tuple[str, ...] = Field(min_length=1, max_length=5)
    relevant_sections: tuple[str, ...] = ()
    note: str = ""


class GoldenDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    cases: tuple[GoldenCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, cases: tuple[GoldenCase, ...]) -> tuple[GoldenCase, ...]:
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("golden case ids must be unique")
        return cases


def load_golden(path: Path, known_sources: set[str]) -> GoldenDataset:
    """Parse and validate. A case that names a source outside the corpus is malformed."""
    dataset = GoldenDataset.model_validate_json(path.read_text(encoding="utf-8"))
    for case in dataset.cases:
        unknown = set(case.relevant_sources) - known_sources
        if unknown:
            raise ValueError(f"golden case {case.id}: unknown sources {sorted(unknown)}")
    return dataset


# ---- Metrics (pure) -------------------------------------------------------------------


def is_relevant(chunk: RetrievedChunk, case: GoldenCase) -> bool:
    if chunk.source_key not in case.relevant_sources:
        return False
    if not case.relevant_sections:
        return True
    section = chunk.section.lower()
    return any(wanted.lower() in section for wanted in case.relevant_sections)


def precision_at_k(results: Sequence[RetrievedChunk], case: GoldenCase, k: int) -> float:
    return sum(is_relevant(r, case) for r in results[:k]) / k


def recall_at_k(results: Sequence[RetrievedChunk], case: GoldenCase, k: int) -> float:
    found = {r.source_key for r in results[:k] if is_relevant(r, case)}
    return len(found) / len(set(case.relevant_sources))


def reciprocal_rank(results: Sequence[RetrievedChunk], case: GoldenCase, k: int) -> float:
    for rank, result in enumerate(results[:k], start=1):
        if is_relevant(result, case):
            return 1.0 / rank
    return 0.0


# ---- Running ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    id: str
    precision: float
    recall: float
    reciprocal_rank: float
    top_sources: list[str]


@dataclass(frozen=True)
class ConfigResult:
    config: str
    max_chars: int
    chunks: int
    precision_at_k: float
    recall_at_k: float
    mrr: float
    cases: list[CaseResult]


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


@contextmanager
def isolated_session(engine: Engine) -> Iterator[Session]:
    """A session whose every change is rolled back at the end (commits become savepoints)."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                yield session
        finally:
            transaction.rollback()


def evaluate_config(
    session: Session,
    provider: EmbeddingProvider,
    documents: Sequence[SourceDocument],
    dataset: GoldenDataset,
    config: ChunkerConfig,
    k: int,
) -> ConfigResult:
    """Must run inside an isolated (rolled-back) session."""
    with session.begin():
        session.execute(delete(KnowledgeDocument))  # a clean index for this config only
    outcomes = ingest_corpus(session, provider, documents, config, prune=False)
    cases = []
    for case in dataset.cases:
        results = retrieve(session, provider, case.query, top_k=k)
        cases.append(
            CaseResult(
                id=case.id,
                precision=round(precision_at_k(results, case, k), 4),
                recall=round(recall_at_k(results, case, k), 4),
                reciprocal_rank=round(reciprocal_rank(results, case, k), 4),
                top_sources=[r.source_key for r in results],
            )
        )
    return ConfigResult(
        config=config.label,
        max_chars=config.max_chars,
        chunks=sum(o.chunks for o in outcomes),
        precision_at_k=_mean([c.precision for c in cases]),
        recall_at_k=_mean([c.recall for c in cases]),
        mrr=_mean([c.reciprocal_rank for c in cases]),
        cases=cases,
    )


def run_evaluation(
    engine: Engine,
    provider: EmbeddingProvider,
    dataset: GoldenDataset,
    documents: Sequence[SourceDocument],
    configs: Sequence[ChunkerConfig],
    k: int,
) -> list[ConfigResult]:
    results = []
    for config in configs:
        with isolated_session(engine) as session:
            results.append(evaluate_config(session, provider, documents, dataset, config, k))
    return results


def report(
    results: Sequence[ConfigResult], provider: EmbeddingProvider, golden: Path, k: int
) -> dict[str, object]:
    return {
        "schema": "darwinux.retrieval-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "embedding_model": provider.name,
        "k": k,
        "dataset": {
            "path": str(golden.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(golden.read_bytes()).hexdigest(),
        },
        "configs": [asdict(r) for r in results],
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval on the golden dataset.")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--configs", default="small,standard,large")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)

    configs = [CONFIGS[name] for name in args.configs.split(",")]
    documents = load_corpus()
    dataset = load_golden(args.golden, {d.source_key for d in documents})
    provider = HashingEmbeddingProvider()
    engine = create_db_engine(str(Settings().database_url))
    try:
        results = run_evaluation(engine, provider, dataset, documents, configs, args.k)
    finally:
        engine.dispose()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report(results, provider, args.golden, args.k), indent=2) + "\n"
    )
    print(f"golden: {len(dataset.cases)} queries   embedding: {provider.name}   k={args.k}")
    print(f"{'config':<9} {'max_chars':>9} {'chunks':>6}  {'P@k':>6}  {'R@k':>6}  {'MRR':>6}")
    for r in results:
        print(
            f"{r.config:<9} {r.max_chars:>9} {r.chunks:>6}  "
            f"{r.precision_at_k:>6.3f}  {r.recall_at_k:>6.3f}  {r.mrr:>6.3f}"
        )
    print(f"report: {args.output.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
