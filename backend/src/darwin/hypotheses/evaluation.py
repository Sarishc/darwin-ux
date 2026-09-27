"""Golden hypothesis evaluation, run with the FakeLLMProvider (make hypothesis-eval).

    python -m darwin.hypotheses.evaluation

Each golden case names a signal, a Product Memory corpus and a fake-provider
mode, and states the *properties* the outcome must have (status, error type,
component, cited sources, text that must not appear). No case requires exact
prose. Everything runs in ONE transaction that is rolled back: synthetic
signals, re-ingested corpora, runs and hypotheses all disappear afterwards.

With a fake provider the rates below measure DarwinUX's control layer —
does every bad output get caught, does every good one get through with valid
citations — not a model's quality. The mix of fixture modes decides the
schema-compliance rate; what matters is that each failure lands in the
expected status. The same harness will measure a real provider later.

Metrics are reported separately, never blended into one score:

- schema compliance:        schema-valid outputs / outputs the provider returned
- evidence-reference validity: outputs citing only supplied excerpts / schema-valid outputs
- component accuracy:       accepted hypotheses naming the expected component / cases
                            that expect one
- source-reference success: cases whose accepted hypothesis cites every expected source /
                            cases that expect sources
- failure handling:         failure cases ending in the expected status and error type,
                            with no Hypothesis / failure cases
- lexical support (BASELINE): mean share of an accepted hypothesis's content words found
                            in its cited evidence — word overlap, not faithfulness
"""

import argparse
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, Engine, delete
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import BehaviorSignal, Hypothesis, HypothesisRun, KnowledgeDocument
from darwin.llm.fake import FakeLLMProvider, FakeMode
from darwin.logging_config import configure_logging
from darwin.memory.corpus import REPO_ROOT, CorpusEntry, SourceDocument, load_corpus
from darwin.memory.embeddings import EmbeddingProvider, HashingEmbeddingProvider
from darwin.memory.ingest import ingest_corpus
from darwin.signals import detectors

from .prompt import REQUEST_VERSION
from .schema import HypothesisDraft, lexical_support
from .service import GenerationOutcome, generate_hypothesis

GOLDEN_DIR = REPO_ROOT / "backend" / "tests" / "evals" / "golden"
GOLDEN_PATH = GOLDEN_DIR / "hypotheses.json"
EVAL_CORPUS_DIR = GOLDEN_DIR / "hypothesis_corpus"
REPORT_PATH = REPO_ROOT / "artifacts" / "hypothesis-eval.json"

CASE_NAMESPACE = uuid.UUID("5b8f0e62-3c1d-4a7e-9f24-6d0a1b7c8e93")
WINDOW_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

Corpus = Literal["product", "empty", "unrelated", "product_with_injection"]
RunStatus = Literal[
    "succeeded",
    "insufficient_evidence",
    "provider_unavailable",
    "provider_error",
    "invalid_output",
    "grounding_failed",
]
INJECTION_MARKER = "Ignore all previous instructions"


# ---- Golden dataset -------------------------------------------------------------------


class Expectation(BaseModel):
    """Properties of the outcome. Fields left out are not checked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RunStatus
    error_type: str | None = None
    affected_component: str | None = None
    must_reference_sources: tuple[str, ...] = ()
    forbidden_output_substrings: tuple[str, ...] = ()
    injection_only_in_evidence: bool = False


class HypothesisCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    description: str = Field(min_length=10, max_length=300)
    signal_type: Literal["rage_click", "error_burst"]
    component: str | None = Field(default=None, pattern=detectors.COMPONENT_PATTERN.pattern)
    corpus: Corpus = "product"
    fake_mode: FakeMode = "grounded"
    expected: Expectation


class HypothesisDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    cases: tuple[HypothesisCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, cases: tuple[HypothesisCase, ...]) -> tuple[HypothesisCase, ...]:
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("golden case ids must be unique")
        return cases


def corpus_documents(corpus: Corpus) -> list[SourceDocument]:
    if corpus == "empty":
        return []
    if corpus == "unrelated":
        entries = (CorpusEntry("repo_document", "unrelated.md", "markdown"),)
        return load_corpus(entries, root=EVAL_CORPUS_DIR, include_generated=False)
    documents = load_corpus()
    if corpus == "product_with_injection":
        entries = (CorpusEntry("repo_document", "injection.md", "markdown"),)
        documents += load_corpus(entries, root=EVAL_CORPUS_DIR, include_generated=False)
    return documents


def known_sources() -> set[str]:
    corpora: tuple[Corpus, ...] = ("product", "unrelated", "product_with_injection")
    return {d.source_key for c in corpora for d in corpus_documents(c)}


def load_dataset(path: Path = GOLDEN_PATH) -> HypothesisDataset:
    """Parse and validate. A case expecting a source no corpus contains is malformed."""
    dataset = HypothesisDataset.model_validate_json(path.read_text(encoding="utf-8"))
    sources = known_sources()
    for case in dataset.cases:
        unknown = set(case.expected.must_reference_sources) - sources
        if unknown:
            raise ValueError(f"golden case {case.id}: unknown sources {sorted(unknown)}")
        if case.expected.status == "succeeded" and case.expected.error_type is not None:
            raise ValueError(f"golden case {case.id}: a success has no error type")
    return dataset


def case_signal(case: HypothesisCase) -> BehaviorSignal:
    """A synthetic canonical signal for one case (exists only inside the rolled-back run)."""
    return synthetic_signal(f"signal:{case.id}", case.signal_type, case.component)


def synthetic_signal(key: str, signal_type: str, component: str | None) -> BehaviorSignal:
    """A deterministic synthetic signal (uuid5 of `key`), shaped like a detector's output."""
    if signal_type == detectors.RAGE_CLICK:
        evidence: dict[str, Any] = {
            "component": component,
            "count": detectors.RAGE_CLICK_THRESHOLD,
            "event_ids": [],
            "threshold": detectors.RAGE_CLICK_THRESHOLD,
            "window_seconds": detectors.RAGE_CLICK_WINDOW.total_seconds(),
        }
        duration = timedelta(seconds=1.5)
    else:
        evidence = {
            "count": detectors.ERROR_BURST_THRESHOLD,
            "event_ids": [],
            "event_types": ["form_error"],
            "threshold": detectors.ERROR_BURST_THRESHOLD,
            "window_seconds": detectors.ERROR_BURST_WINDOW.total_seconds(),
        }
        duration = timedelta(seconds=6)
    return BehaviorSignal(
        signal_id=uuid.uuid5(CASE_NAMESPACE, key),
        signal_type=signal_type,
        detector_version="1",
        session_id=uuid.uuid5(CASE_NAMESPACE, f"session:{key}"),
        window_start=WINDOW_START,
        window_end=WINDOW_START + duration,
        evidence={k: v for k, v in evidence.items() if v is not None},
    )


# ---- Scoring one case (pure) -----------------------------------------------------------

_RETURNED_OUTPUT = {"succeeded", "invalid_output", "grounding_failed"}


@dataclass(frozen=True)
class CaseResult:
    id: str
    status: str
    error_type: str | None
    passed: bool
    failed_checks: list[str]
    hypothesis_persisted: bool
    schema_valid: bool | None  # None: the provider returned no output
    references_valid: bool | None  # None: no schema-valid output
    component_correct: bool | None  # None: case does not expect a component
    sources_referenced: bool | None  # None: case does not expect sources
    lexical_support: float | None  # None: no accepted hypothesis
    cited_sources: list[str]
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None


def score_case(
    case: HypothesisCase,
    outcome: GenerationOutcome,
    run: HypothesisRun,
    hypothesis: Hypothesis | None,
) -> CaseResult:
    expected = case.expected
    failed: list[str] = []
    if outcome.status != expected.status:
        failed.append("status")
    if "error_type" in expected.model_fields_set and outcome.error_type != expected.error_type:
        failed.append("error_type")
    if (hypothesis is not None) != (outcome.status == "succeeded"):
        failed.append("hypothesis_iff_success")

    schema_valid = run.status != "invalid_output" if run.status in _RETURNED_OUTPUT else None
    references_valid = (
        all(e["type"] != "unknown_evidence_reference" for e in run.validation_errors)
        if schema_valid
        else None
    )
    cited = [ref["source_key"] for ref in hypothesis.evidence_references] if hypothesis else []

    component_correct: bool | None = None
    if "affected_component" in expected.model_fields_set:
        component_correct = (
            hypothesis is not None and hypothesis.affected_component == expected.affected_component
        )
        if not component_correct:
            failed.append("affected_component")

    sources_referenced: bool | None = None
    if expected.must_reference_sources:
        sources_referenced = set(expected.must_reference_sources) <= set(cited)
        if not sources_referenced:
            failed.append("must_reference_sources")

    stored_text = json.dumps(run.output or {}) + (
        f"{hypothesis.statement} {hypothesis.rationale}" if hypothesis else ""
    )
    if any(s.lower() in stored_text.lower() for s in expected.forbidden_output_substrings):
        failed.append("forbidden_output_substrings")

    if expected.injection_only_in_evidence:
        request = outcome.request
        in_evidence = request is not None and INJECTION_MARKER in request.evidence
        in_instructions = request is not None and INJECTION_MARKER in request.instructions
        if not in_evidence or in_instructions:
            failed.append("injection_only_in_evidence")

    support = None
    if hypothesis is not None:
        draft = HypothesisDraft.model_validate(run.output)
        support = lexical_support(draft, outcome.bundle)

    return CaseResult(
        id=case.id,
        status=outcome.status,
        error_type=outcome.error_type,
        passed=not failed,
        failed_checks=failed,
        hypothesis_persisted=hypothesis is not None,
        schema_valid=schema_valid,
        references_valid=references_valid,
        component_correct=component_correct,
        sources_referenced=sources_referenced,
        lexical_support=support,
        cited_sources=cited,
        latency_ms=run.latency_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
    )


@dataclass(frozen=True)
class Rate:
    passed: int
    of: int

    @property
    def rate(self) -> float | None:
        return round(self.passed / self.of, 4) if self.of else None

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "of": self.of, "rate": self.rate}


def _rate(values: Sequence[bool | None]) -> Rate:
    known = [v for v in values if v is not None]
    return Rate(sum(known), len(known))


def metrics(dataset: HypothesisDataset, results: Sequence[CaseResult]) -> dict[str, Any]:
    by_id = {c.id: c for c in dataset.cases}
    failure_cases = [r for r in results if by_id[r.id].expected.status != "succeeded"]
    supports = [r.lexical_support for r in results if r.lexical_support is not None]
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return {
        "cases_meeting_all_expectations": _rate([r.passed for r in results]).as_dict(),
        "schema_compliance": _rate([r.schema_valid for r in results]).as_dict(),
        "evidence_reference_validity": _rate([r.references_valid for r in results]).as_dict(),
        "component_accuracy": _rate([r.component_correct for r in results]).as_dict(),
        "source_reference_success": _rate([r.sources_referenced for r in results]).as_dict(),
        "failure_handling": _rate(
            [
                "status" not in r.failed_checks
                and "error_type" not in r.failed_checks
                and not r.hypothesis_persisted
                for r in failure_cases
            ]
        ).as_dict(),
        "lexical_support_baseline": {
            "mean": round(sum(supports) / len(supports), 4) if supports else None,
            "hypotheses": len(supports),
            "note": "word overlap with cited evidence; NOT a faithfulness measure",
        },
        "provider_calls": {
            "calls": len(latencies),
            "mean_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "input_tokens": sum(r.input_tokens or 0 for r in results),
            "output_tokens": sum(r.output_tokens or 0 for r in results),
            "runs_without_usage": sum(
                1 for r in results if r.latency_ms is not None and r.input_tokens is None
            ),
        },
    }


# ---- Running -----------------------------------------------------------------------------


def _reset_memory(
    factory: Callable[[], Session], embedder: EmbeddingProvider, corpus: Corpus
) -> None:
    with factory() as session:
        session.execute(delete(KnowledgeDocument))
        session.commit()
        ingest_corpus(session, embedder, corpus_documents(corpus), prune=False)


def evaluate(
    connection: Connection, dataset: HypothesisDataset, embedder: EmbeddingProvider
) -> list[CaseResult]:
    """Run every case on `connection`. The caller owns (and rolls back) the transaction."""

    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    with factory() as session:
        session.add_all([case_signal(c) for c in dataset.cases])
        session.commit()

    results: dict[str, CaseResult] = {}
    corpora = list(dict.fromkeys(c.corpus for c in dataset.cases))
    for corpus in corpora:
        _reset_memory(factory, embedder, corpus)
        for case in (c for c in dataset.cases if c.corpus == corpus):
            outcome = generate_hypothesis(
                factory,
                uuid.uuid5(CASE_NAMESPACE, f"signal:{case.id}"),
                FakeLLMProvider(case.fake_mode),
                embedder,
            )
            with factory() as session:
                run = session.get(HypothesisRun, outcome.run_id)
                assert run is not None
                hypothesis = (
                    session.get(Hypothesis, outcome.hypothesis_id)
                    if outcome.hypothesis_id
                    else None
                )
                results[case.id] = score_case(case, outcome, run, hypothesis)
    return [results[c.id] for c in dataset.cases]


def run_evaluation(
    engine: Engine, dataset: HypothesisDataset, embedder: EmbeddingProvider
) -> list[CaseResult]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            return evaluate(connection, dataset, embedder)
        finally:
            transaction.rollback()


def report(
    dataset: HypothesisDataset,
    results: Sequence[CaseResult],
    embedder: EmbeddingProvider,
    dataset_path: Path = GOLDEN_PATH,
) -> dict[str, Any]:
    return {
        "schema": "darwinux.hypothesis-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(dataset_path.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "request_version": REQUEST_VERSION,
        "provider": "fake",
        "model": FakeLLMProvider().model,
        "embedding_model": embedder.name,
        "metrics": metrics(dataset, results),
        "cases": [asdict(r) for r in results],
    }


def _fmt(metric: dict[str, Any]) -> str:
    rate = metric["rate"]
    return f"{metric['passed']}/{metric['of']}" + (f"  ({rate:.3f})" if rate is not None else "")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Golden hypothesis evaluation (fake LLM).")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging("WARNING")  # per-run INFO lines would drown the table
    engine = create_db_engine(str(settings.database_url))
    embedder = HashingEmbeddingProvider()
    dataset = load_dataset()
    try:
        results = run_evaluation(engine, dataset, embedder)
    finally:
        engine.dispose()
    data = report(dataset, results, embedder)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    m = data["metrics"]
    print(f"golden: {len(dataset.cases)} cases   {REQUEST_VERSION}   provider: fake")
    for r in results:
        mark = "ok  " if r.passed else "FAIL"
        detail = f"  failed: {','.join(r.failed_checks)}" if r.failed_checks else ""
        print(f"  {mark} {r.id:<36} {r.status}/{r.error_type}{detail}")
    for name in (
        "cases_meeting_all_expectations",
        "schema_compliance",
        "evidence_reference_validity",
        "component_accuracy",
        "source_reference_success",
        "failure_handling",
    ):
        print(f"{name:<32} {_fmt(m[name])}")
    lex = m["lexical_support_baseline"]
    print(f"{'lexical_support_baseline':<32} {lex['mean']}  (baseline: word overlap only)")
    calls = m["provider_calls"]
    print(
        f"{'provider_calls':<32} {calls['calls']} calls, tokens in={calls['input_tokens']} "
        f"out={calls['output_tokens']} (fake estimates)"
    )
    print(f"report: {args.output.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
