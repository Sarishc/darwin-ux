"""Golden research-workflow evaluation: outcomes AND trajectories (make research-eval).

    python -m darwin.research.evaluation

Each case names a signal, a Product Memory corpus, fake-provider modes for
the hypothesis and critique calls, an optional tighter budget and an optional
human decision to resume with. It states expected *properties*: the terminal
status and stop reason, the path (an exact node sequence, or nodes that must
/ must not appear), retrieval attempts, LLM calls and the hypothesis's final
lifecycle state. No case checks prose. Everything runs in one rolled-back
transaction.

As in Step 9, the fake provider means these metrics measure the ORCHESTRATION
— routing, bounds, trajectories, persistence — not model quality.

Metrics (separately, never blended):
- workflow success rate          runs ending "succeeded" / cases (descriptive, not a target)
- terminal-state accuracy        final status (+ stop reason) as expected / cases
- avg retrieval attempts / avg LLM calls
- unnecessary second-retrieval   runs that retrieved twice / cases marked "one retrieval is
  rate                           enough"
- human-review routing accuracy  (reached human_review) == (expected to) / cases
- budget-exhaustion handling     budget cases ending as expected within their budget /
                                 budget cases
- trajectory validity            structurally valid paths (every step an allowed
                                 transition) / cases; and path expectations met / cases
"""

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, Engine, delete
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import Hypothesis, KnowledgeDocument
from darwin.hypotheses.evaluation import (
    GOLDEN_DIR,
    INJECTION_MARKER,
    corpus_documents,
    synthetic_signal,
)
from darwin.llm.fake import CritiqueMode, FakeLLMProvider, FakeMode
from darwin.logging_config import configure_logging
from darwin.memory.corpus import REPO_ROOT, CorpusEntry, SourceDocument, load_corpus
from darwin.memory.embeddings import EmbeddingProvider, HashingEmbeddingProvider
from darwin.memory.ingest import ingest_corpus
from darwin.signals import detectors

from .budget import ResearchBudget
from .graph import GRAPH_VERSION, NODES, trajectory_is_valid
from .service import ResearchOutcome, resume_research, run_research

GOLDEN_PATH = GOLDEN_DIR / "research.json"
RESEARCH_CORPUS_DIR = GOLDEN_DIR / "research_corpus"
REPORT_PATH = REPO_ROOT / "artifacts" / "research-eval.json"

Corpus = Literal["product", "empty", "product_with_injection", "noisy", "distractors_only"]
Status = Literal[
    "running", "waiting_for_human", "succeeded", "insufficient_evidence", "rejected", "failed"
]
HypothesisState = Literal["none", "proposed", "accepted", "rejected"]
NodeName = Literal[
    "load_signal",
    "retrieve",
    "assess_evidence",
    "refine_query",
    "generate_hypothesis",
    "critique_hypothesis",
    "human_review",
    "apply_human_decision",
    "finalize",
]


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Status  # after the resume, if the case resumes
    stop_reason: str | None = None
    status_before_resume: Status | None = None
    review_reason: str | None = None
    trajectory: tuple[NodeName, ...] | None = None  # exact path, when given
    required_nodes: tuple[NodeName, ...] = ()
    forbidden_nodes: tuple[NodeName, ...] = ()
    retrieval_attempts: int | None = None
    llm_calls: int | None = None
    hypothesis: HypothesisState | None = None
    one_retrieval_is_enough: bool = False
    injection_only_in_evidence: bool = False


class ResearchCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    description: str = Field(min_length=10, max_length=300)
    signal_type: Literal["rage_click", "error_burst"]
    component: str | None = Field(default=None, pattern=detectors.COMPONENT_PATTERN.pattern)
    corpus: Corpus = "product"
    fake_mode: FakeMode = "grounded"
    critique_mode: CritiqueMode = "accept"
    budget: dict[str, int] = Field(default_factory=dict)
    resume: Literal["approve", "reject"] | None = None
    expected: Expectation

    @field_validator("budget")
    @classmethod
    def _valid_budget(cls, value: dict[str, int]) -> dict[str, int]:
        ResearchBudget(**value)  # raises for unknown keys or values above the hard caps
        return value

    @property
    def is_budget_case(self) -> bool:
        return bool(self.budget)


class ResearchDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    cases: tuple[ResearchCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, cases: tuple[ResearchCase, ...]) -> tuple[ResearchCase, ...]:
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("golden case ids must be unique")
        return cases


def load_dataset(path: Path = GOLDEN_PATH) -> ResearchDataset:
    dataset = ResearchDataset.model_validate_json(path.read_text(encoding="utf-8"))
    for case in dataset.cases:
        if (case.resume is None) != (case.expected.status_before_resume is None):
            raise ValueError(f"golden case {case.id}: resume and status_before_resume go together")
    return dataset


def research_corpus(corpus: Corpus) -> list[SourceDocument]:
    if corpus in ("product", "empty", "product_with_injection"):
        return corpus_documents(corpus)
    distractors = load_corpus(
        (CorpusEntry("repo_document", "distractors.md", "markdown"),),
        root=RESEARCH_CORPUS_DIR,
        include_generated=False,
    )
    return corpus_documents("product") + distractors if corpus == "noisy" else distractors


# ---- scoring (pure) ------------------------------------------------------------------------


def segments(trajectory: Sequence[str]) -> list[list[str]]:
    """Split at a resume: each graph invocation is validated from START on its own."""
    parts: list[list[str]] = [[]]
    for node in trajectory:
        parts[-1].append(node)
        if node == "human_review":
            parts.append([])
    return [p for p in parts if p]


def structurally_valid(trajectory: Sequence[str]) -> bool:
    return bool(trajectory) and all(trajectory_is_valid(part) for part in segments(trajectory))


@dataclass(frozen=True)
class CaseResult:
    id: str
    status: str
    stop_reason: str | None
    status_before_resume: str | None
    passed: bool
    failed_checks: list[str]
    trajectory: list[str]
    trajectory_structurally_valid: bool
    trajectory_expectations_met: bool
    reached_human_review: bool
    retrieval_attempts: int
    llm_calls: int
    steps: int
    within_budget: bool
    hypothesis: str
    input_tokens: int
    output_tokens: int


def score_case(
    case: ResearchCase,
    outcome: ResearchOutcome,
    status_before_resume: str | None,
    hypothesis_state: str,
    requests: Sequence[Any],
) -> CaseResult:
    expected = case.expected
    failed: list[str] = []
    trajectory = list(outcome.trajectory)

    if outcome.status != expected.status:
        failed.append("status")
    if expected.stop_reason is not None and outcome.stop_reason != expected.stop_reason:
        failed.append("stop_reason")
    if expected.status_before_resume is not None and (
        status_before_resume != expected.status_before_resume
    ):
        failed.append("status_before_resume")
    if expected.review_reason is not None and outcome.review_reason != expected.review_reason:
        failed.append("review_reason")

    path_ok = True
    if expected.trajectory is not None and tuple(trajectory) != expected.trajectory:
        path_ok = False
    if any(node not in trajectory for node in expected.required_nodes):
        path_ok = False
    if any(node in trajectory for node in expected.forbidden_nodes):
        path_ok = False
    if not path_ok:
        failed.append("trajectory")
    valid = structurally_valid(trajectory)
    if not valid:
        failed.append("trajectory_structure")

    if (
        expected.retrieval_attempts is not None
        and outcome.retrieval_attempts != expected.retrieval_attempts
    ):
        failed.append("retrieval_attempts")
    if expected.llm_calls is not None and outcome.llm_calls != expected.llm_calls:
        failed.append("llm_calls")
    if expected.hypothesis is not None and hypothesis_state != expected.hypothesis:
        failed.append("hypothesis")

    budget = ResearchBudget(**case.budget)
    within = (
        outcome.retrieval_attempts <= budget.max_retrieval_attempts
        and outcome.llm_calls <= budget.max_llm_calls
        and outcome.steps <= budget.max_graph_steps
        and len(requests) == outcome.llm_calls
    )
    if not within:
        failed.append("budget")

    if expected.injection_only_in_evidence:
        in_evidence = any(INJECTION_MARKER in r.evidence for r in requests)
        in_instructions = any(INJECTION_MARKER in r.instructions for r in requests)
        if not in_evidence or in_instructions:
            failed.append("injection_only_in_evidence")

    return CaseResult(
        id=case.id,
        status=outcome.status,
        stop_reason=outcome.stop_reason,
        status_before_resume=status_before_resume,
        passed=not failed,
        failed_checks=failed,
        trajectory=trajectory,
        trajectory_structurally_valid=valid,
        trajectory_expectations_met=path_ok,
        reached_human_review="human_review" in trajectory,
        retrieval_attempts=outcome.retrieval_attempts,
        llm_calls=outcome.llm_calls,
        steps=outcome.steps,
        within_budget=within,
        hypothesis=hypothesis_state,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )


def _rate(values: Sequence[bool]) -> dict[str, Any]:
    passed, of = sum(values), len(values)
    return {"passed": passed, "of": of, "rate": round(passed / of, 4) if of else None}


def _mean(values: Sequence[int]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def metrics(dataset: ResearchDataset, results: Sequence[CaseResult]) -> dict[str, Any]:
    by_id = {c.id: c for c in dataset.cases}

    def expects_review(r: CaseResult) -> bool:
        e = by_id[r.id].expected
        return "waiting_for_human" in (e.status, e.status_before_resume)

    terminal = [
        r.status == by_id[r.id].expected.status
        and (
            by_id[r.id].expected.stop_reason is None
            or r.stop_reason == by_id[r.id].expected.stop_reason
        )
        for r in results
    ]
    one_enough = [r for r in results if by_id[r.id].expected.one_retrieval_is_enough]
    budget_cases = [r for r in results if by_id[r.id].is_budget_case]
    return {
        "cases_meeting_all_expectations": _rate([r.passed for r in results]),
        "workflow_success_rate": _rate([r.status == "succeeded" for r in results]),
        "terminal_state_accuracy": _rate(terminal),
        "avg_retrieval_attempts": _mean([r.retrieval_attempts for r in results]),
        "avg_llm_calls": _mean([r.llm_calls for r in results]),
        "unnecessary_second_retrieval_rate": _rate([r.retrieval_attempts > 1 for r in one_enough]),
        "human_review_routing_accuracy": _rate(
            [r.reached_human_review == expects_review(r) for r in results]
        ),
        "budget_exhaustion_handling": _rate(
            [
                r.within_budget
                and "status" not in r.failed_checks
                and "stop_reason" not in r.failed_checks
                for r in budget_cases
            ]
        ),
        "trajectory_structural_validity": _rate([r.trajectory_structurally_valid for r in results]),
        "trajectory_expectations_met": _rate([r.trajectory_expectations_met for r in results]),
        "all_runs_within_budget": _rate([r.within_budget for r in results]),
        "tokens": {
            "input": sum(r.input_tokens for r in results),
            "output": sum(r.output_tokens for r in results),
            "note": "FakeLLMProvider character-based estimates, not billing data",
        },
    }


# ---- running -------------------------------------------------------------------------------


def evaluate(
    connection: Connection, dataset: ResearchDataset, embedder: EmbeddingProvider
) -> list[CaseResult]:
    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    signals = {
        c.id: synthetic_signal(f"research:{c.id}", c.signal_type, c.component)
        for c in dataset.cases
    }
    signal_ids = {case_id: signal.signal_id for case_id, signal in signals.items()}
    with factory() as session:
        session.add_all(signals.values())
        session.commit()

    results: dict[str, CaseResult] = {}
    for corpus in dict.fromkeys(c.corpus for c in dataset.cases):
        with factory() as session:
            session.execute(delete(KnowledgeDocument))
            session.commit()
            ingest_corpus(session, embedder, research_corpus(corpus), prune=False)
        for case in (c for c in dataset.cases if c.corpus == corpus):
            llm = FakeLLMProvider(case.fake_mode, case.critique_mode)
            outcome = run_research(
                factory,
                signal_ids[case.id],
                llm,
                embedder,
                budget=ResearchBudget(**case.budget),
            )
            before = None
            if case.resume is not None:
                before = outcome.status
                if outcome.status == "waiting_for_human":
                    outcome = resume_research(factory, outcome.run_id, case.resume)
            hypothesis_state = "none"
            if outcome.hypothesis_id is not None:
                with factory() as session:
                    hypothesis = session.get(Hypothesis, outcome.hypothesis_id)
                    hypothesis_state = hypothesis.status if hypothesis else "none"
            results[case.id] = score_case(case, outcome, before, hypothesis_state, llm.requests)
    return [results[c.id] for c in dataset.cases]


def run_evaluation(
    engine: Engine, dataset: ResearchDataset, embedder: EmbeddingProvider
) -> list[CaseResult]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            return evaluate(connection, dataset, embedder)
        finally:
            transaction.rollback()


def report(
    dataset: ResearchDataset, results: Sequence[CaseResult], embedder: EmbeddingProvider
) -> dict[str, Any]:
    return {
        "schema": "darwinux.research-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "graph_version": GRAPH_VERSION,
        "nodes": list(NODES),
        "provider": "fake",
        "embedding_model": embedder.name,
        "metrics": metrics(dataset, results),
        "cases": [asdict(r) for r in results],
    }


def _fmt(metric: dict[str, Any]) -> str:
    rate = metric["rate"]
    return f"{metric['passed']}/{metric['of']}" + (f"  ({rate:.3f})" if rate is not None else "")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Golden research-workflow evaluation.")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    engine = create_db_engine(str(Settings().database_url))
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
    print(f"golden: {len(dataset.cases)} cases   {GRAPH_VERSION}   provider: fake")
    for r in results:
        mark = "ok  " if r.passed else "FAIL"
        detail = f"  failed: {','.join(r.failed_checks)}" if r.failed_checks else ""
        print(f"  {mark} {r.id:<38} {r.status}/{r.stop_reason}{detail}")
    for name in (
        "cases_meeting_all_expectations",
        "workflow_success_rate",
        "terminal_state_accuracy",
        "unnecessary_second_retrieval_rate",
        "human_review_routing_accuracy",
        "budget_exhaustion_handling",
        "trajectory_structural_validity",
        "trajectory_expectations_met",
        "all_runs_within_budget",
    ):
        print(f"{name:<36} {_fmt(m[name])}")
    print(f"{'avg_retrieval_attempts':<36} {m['avg_retrieval_attempts']}")
    print(f"{'avg_llm_calls':<36} {m['avg_llm_calls']}")
    print(f"{'tokens (fake estimates)':<36} in={m['tokens']['input']} out={m['tokens']['output']}")
    print(f"report: {args.output.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
