"""run_research / resume_research: the DarwinUX service around the LangGraph graph.

The CLI and the evaluation call these; nothing outside darwin.research sees
LangGraph. Every explicit execution creates a ResearchRun; a resume continues
the same run. A graph "loop" (one refined retrieval) is research inside a run;
provider retries do not exist in Step 10 (a failed call ends the run); a new
run is always a new, explicit request.

Human review: when the graph reaches human_review, the run is persisted as
waiting_for_human and the invocation ends. There is no LangGraph checkpointer:
Postgres checkpointing is a separate package and couples the schema to
LangGraph internals, while resuming only needs a few ids and counters that
research_run already holds. resume_research atomically moves the run from
waiting_for_human to running (so a second, concurrent or later resume is
refused), rebuilds that small state and invokes the same graph, whose entry
router sends a state carrying a human decision straight to
apply_human_decision. A resume can never call the model or retrieval: its
dependencies are stubs that raise.

LangSmith (pulled in by langchain-core) only traces when its environment
variables enable it. That would ship evidence text to a third party, so
research refuses to run if they are set.
"""

import logging
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update

from darwin.db.models import BehaviorSignal, ResearchRun, ResearchStep
from darwin.db.models.research import HUMAN_DECISIONS
from darwin.hypotheses.evidence import generation_zero_components
from darwin.hypotheses.service import SessionFactory, SignalNotFoundError
from darwin.llm.port import (
    LLMProvider,
    ProviderUnavailableError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from darwin.memory.embeddings import EmbeddingProvider

from .budget import HARD_MAX_GRAPH_STEPS, ResearchBudget
from .graph import GRAPH_VERSION, ResearchDeps, build_graph
from .state import ResearchState

logger = logging.getLogger(__name__)

TRACING_ENV_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)
RECURSION_LIMIT = HARD_MAX_GRAPH_STEPS + 2  # backstop only; the node wrapper stops earlier


class ResearchRunNotFoundError(LookupError):
    pass


class ResumeNotAllowedError(RuntimeError):
    """The run exists but is not waiting for a human (finished, running, or already resumed)."""


class InvalidDecisionError(ValueError):
    pass


class ExternalTracingEnabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchOutcome:
    run_id: uuid.UUID
    status: str
    stop_reason: str | None
    trajectory: tuple[str, ...]  # every node executed in this run, including before a resume
    retrieval_attempts: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    calls_without_usage: int
    steps: int
    queries: tuple[str, ...]
    hypothesis_run_id: uuid.UUID | None
    hypothesis_id: uuid.UUID | None
    review_reason: str | None
    critique: dict[str, Any] | None
    elapsed_ms: float


def refuse_external_tracing(environ: Mapping[str, str] = os.environ) -> None:
    enabled = [
        name
        for name in TRACING_ENV_VARS
        if environ.get(name, "").strip().lower() in {"1", "true", "yes"}
    ]
    if enabled:
        raise ExternalTracingEnabledError(
            f"refusing to run research with external tracing enabled ({', '.join(enabled)})"
        )


def load_outcome(session_factory: SessionFactory, run_id: uuid.UUID) -> ResearchOutcome:
    with session_factory() as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise ResearchRunNotFoundError(f"no research run {run_id}")
        trajectory = session.scalars(
            select(ResearchStep.node)
            .where(ResearchStep.run_id == run_id)
            .order_by(ResearchStep.sequence)
        ).all()
        return ResearchOutcome(
            run_id=run.id,
            status=run.status,
            stop_reason=run.stop_reason,
            trajectory=tuple(trajectory),
            retrieval_attempts=run.retrieval_attempts,
            llm_calls=run.llm_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            calls_without_usage=run.calls_without_usage,
            steps=run.steps,
            queries=tuple(run.queries),
            hypothesis_run_id=run.hypothesis_run_id,
            hypothesis_id=run.hypothesis_id,
            review_reason=run.review_reason,
            critique=run.critique,
            elapsed_ms=run.elapsed_ms,
        )


def _invoke(deps: ResearchDeps, run_id: uuid.UUID, state: ResearchState) -> ResearchOutcome:
    graph = build_graph(deps)
    started = time.perf_counter()
    try:
        graph.invoke(state, config={"recursion_limit": RECURSION_LIMIT})
    except Exception as error:
        _fail(deps.session_factory, run_id, f"internal_error:{type(error).__name__}"[:64])
        raise
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        with deps.session_factory() as session:
            session.execute(
                update(ResearchRun)
                .where(ResearchRun.id == run_id)
                .values(elapsed_ms=ResearchRun.elapsed_ms + elapsed)
            )
            session.commit()
    outcome = load_outcome(deps.session_factory, run_id)
    logger.info(
        "research run finished",
        extra={
            "context": {
                "research_run_id": str(run_id),
                "signal_id": state["signal_id"],
                "graph_version": GRAPH_VERSION,
                "status": outcome.status,
                "stop_reason": outcome.stop_reason,
                "steps": outcome.steps,
                "retrieval_attempts": outcome.retrieval_attempts,
                "llm_calls": outcome.llm_calls,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "elapsed_ms": outcome.elapsed_ms,
            }
        },
    )
    return outcome


def _fail(session_factory: SessionFactory, run_id: uuid.UUID, reason: str) -> None:
    with session_factory() as session:
        session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "running")
            .values(
                status="failed", stop_reason=reason, completed_at=func.now(), updated_at=func.now()
            )
        )
        session.commit()


def run_research(
    session_factory: SessionFactory,
    signal_id: uuid.UUID,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    *,
    budget: ResearchBudget | None = None,
    known_components: Sequence[str] | None = None,
) -> ResearchOutcome:
    refuse_external_tracing()
    budget = budget or ResearchBudget()
    with session_factory() as session:
        exists = session.scalar(
            select(BehaviorSignal.id).where(
                BehaviorSignal.signal_id == signal_id, BehaviorSignal.superseded_at.is_(None)
            )
        )
        if exists is None:
            raise SignalNotFoundError(f"no canonical signal {signal_id}")
        run = ResearchRun(
            id=uuid.uuid4(),
            signal_id=signal_id,
            graph_version=GRAPH_VERSION,
            status="running",
            budget=budget.as_dict(),
        )
        session.add(run)
        session.commit()
        run_id = run.id

    deps = ResearchDeps(
        session_factory=session_factory,
        llm=llm,
        embedder=embedder,
        budget=budget,
        known_components=tuple(
            generation_zero_components() if known_components is None else known_components
        ),
    )
    initial: ResearchState = {
        "research_run_id": str(run_id),
        "signal_id": str(signal_id),
        "steps": 0,
        "trajectory": [],
    }
    return _invoke(deps, run_id, initial)


class _NoModelCalls:
    """The resume path's LLM: resuming applies a human decision and never calls a model."""

    name = "none"
    model = "none"

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult:
        raise ProviderUnavailableError("a resumed research run never calls a model")


class _NoRetrieval:
    name = "none"
    dimension = 0

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("a resumed research run never retrieves")


def resume_research(
    session_factory: SessionFactory, run_id: uuid.UUID, decision: str
) -> ResearchOutcome:
    """Apply an allowlisted human decision to a run that is waiting for one."""
    refuse_external_tracing()
    if decision not in HUMAN_DECISIONS:
        raise InvalidDecisionError(f"decision must be one of {', '.join(HUMAN_DECISIONS)}")
    with session_factory() as session:
        claimed = session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "waiting_for_human")
            .values(
                status="running",
                stop_reason=None,
                human_decision=decision,
                updated_at=func.now(),
            )
            .returning(ResearchRun)
        ).scalar_one_or_none()
        if claimed is None:
            run = session.get(ResearchRun, run_id)
            session.rollback()
            if run is None:
                raise ResearchRunNotFoundError(f"no research run {run_id}")
            raise ResumeNotAllowedError(f"research run {run_id} is {run.status}")
        state: ResearchState = {
            "research_run_id": str(claimed.id),
            "signal_id": str(claimed.signal_id),
            "human_decision": decision,
            "steps": claimed.steps,
            "trajectory": [],
            "queries": list(claimed.queries),
            "retrieval_attempts": claimed.retrieval_attempts,
            "llm_calls": claimed.llm_calls,
            "input_tokens": claimed.input_tokens,
            "output_tokens": claimed.output_tokens,
            "calls_without_usage": claimed.calls_without_usage,
            "hypothesis_run_id": str(claimed.hypothesis_run_id)
            if claimed.hypothesis_run_id
            else None,
            "hypothesis_id": str(claimed.hypothesis_id) if claimed.hypothesis_id else None,
            "critique": claimed.critique,
            "review_reason": claimed.review_reason,
        }
        budget = ResearchBudget(**claimed.budget)
        session.commit()

    deps = ResearchDeps(
        session_factory=session_factory,
        llm=_NoModelCalls(),
        embedder=_NoRetrieval(),
        budget=budget,
        known_components=(),
    )
    return _invoke(deps, run_id, state)
