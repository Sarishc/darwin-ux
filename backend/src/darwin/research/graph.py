"""research_graph.v1: the LangGraph StateGraph and its nodes.

    START -> load_signal -> retrieve -> assess_evidence
    assess_evidence -> generate_hypothesis | refine_query | finalize
    refine_query -> retrieve                          (the only cycle)
    generate_hypothesis -> critique_hypothesis | finalize
    critique_hypothesis -> finalize | human_review
    human_review -> END                               (run waits; resume = new invocation)
    START -> apply_human_decision -> finalize -> END  (only when resuming with a decision)

Every node returns the next node in `route`; the conditional edges only
accept targets listed in TRANSITIONS (the same table the evaluation uses to
check trajectories). The one cycle, retrieve -> assess_evidence ->
refine_query -> retrieve, is bounded twice: assess_evidence routes to
refine_query only while retrieval_attempts < max_retrieval_attempts and
refinements < max_refinements, and refine_query increments refinements.
Independently, the node wrapper counts steps and forces `finalize` one step
before max_graph_steps; LangGraph's recursion limit is only a backstop.

Nodes call existing services; none reimplements them:
  retrieval ............ darwin.memory.retrieval.retrieve (Step 8)
  evidence ............. darwin.hypotheses.evidence.build_evidence_bundle (Step 9)
  hypothesis ........... darwin.hypotheses.service.generate_from_bundle (Step 9)
  critique call ........ darwin.hypotheses.service.call_provider + research.critique
The graph's only capabilities are those in ResearchDeps. No tool registry,
no shell, files, SQL, HTTP, git, deployment or mutation.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from darwin.db.models import BehaviorSignal, Hypothesis
from darwin.hypotheses.evidence import build_evidence_bundle
from darwin.hypotheses.queries import (
    DEFAULT_TOP_K,
    RetrievalPlan,
    build_retrieval_plan,
    signal_component,
)
from darwin.hypotheses.service import SessionFactory, call_provider, generate_from_bundle
from darwin.llm.port import LLMProvider
from darwin.memory.embeddings import EmbeddingProvider
from darwin.memory.retrieval import RetrievalFilters, retrieve

from .budget import ResearchBudget
from .critique import CRITIQUE_REQUEST_VERSION, build_critique_request, check_critique
from .ledger import record_step
from .planning import REFINED_TOP_K, assess_sufficiency, merge_chunks, refine_plan
from .state import ResearchState

logger = logging.getLogger(__name__)

GRAPH_VERSION = "research_graph.v1"

# The only transitions a node may choose. The graph is built from this table.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    START: ("load_signal", "apply_human_decision"),
    "load_signal": ("retrieve", "finalize"),
    "retrieve": ("assess_evidence", "finalize"),
    "assess_evidence": ("generate_hypothesis", "refine_query", "finalize"),
    "refine_query": ("retrieve", "finalize"),
    "generate_hypothesis": ("critique_hypothesis", "finalize"),
    "critique_hypothesis": ("finalize", "human_review"),
    "human_review": (END,),
    "apply_human_decision": ("finalize",),
    "finalize": (END,),
}
NODES: tuple[str, ...] = tuple(n for n in TRANSITIONS if n != START)
TERMINAL_NODES = ("human_review", "finalize")


@dataclass(frozen=True)
class ResearchDeps:
    """Everything a node may use. This is the complete capability set of the graph."""

    session_factory: SessionFactory
    llm: LLMProvider
    embedder: EmbeddingProvider
    budget: ResearchBudget
    known_components: tuple[str, ...]


@dataclass(frozen=True)
class NodeResult:
    updates: dict[str, Any]
    outcome: str  # short, code-defined label for the step ledger
    detail: dict[str, Any] = field(default_factory=dict)  # compact ledger metadata


NodeFn = Callable[[ResearchState, ResearchDeps], NodeResult]


class GraphContractError(RuntimeError):
    """A node chose a transition that is not in TRANSITIONS (a bug, never data-driven)."""


def _stop(status: str, reason: str, **updates: Any) -> dict[str, Any]:
    return {**updates, "route": "finalize", "final_status": status, "stop_reason": reason}


def _account(
    state: ResearchState, called: bool, input_tokens: int | None, output_tokens: int | None
) -> dict[str, int]:
    if not called:
        return {}
    unknown = input_tokens is None and output_tokens is None
    return {
        "llm_calls": state.get("llm_calls", 0) + 1,
        "input_tokens": state.get("input_tokens", 0) + (input_tokens or 0),
        "output_tokens": state.get("output_tokens", 0) + (output_tokens or 0),
        "calls_without_usage": state.get("calls_without_usage", 0) + int(unknown),
    }


def _transient_signal(signal: BehaviorSignal) -> BehaviorSignal:
    """A detached copy with every column loaded, safe to keep in graph state."""
    columns = [c.key for c in sa_inspect(BehaviorSignal).mapper.column_attrs]
    return BehaviorSignal(**{name: getattr(signal, name) for name in columns})


# ---- nodes ---------------------------------------------------------------------------------


def load_signal(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    with deps.session_factory() as session:
        signal = session.scalar(
            select(BehaviorSignal).where(
                BehaviorSignal.signal_id == uuid.UUID(state["signal_id"]),
                BehaviorSignal.superseded_at.is_(None),
            )
        )
        snapshot = _transient_signal(signal) if signal is not None else None
        session.rollback()
    if snapshot is None:
        return NodeResult(_stop("failed", "signal_not_found"), "signal_not_found")
    plan = build_retrieval_plan(snapshot)  # the deterministic Step 9 query
    updates = {
        "signal": snapshot,
        "signal_type": snapshot.signal_type,
        "component": signal_component(snapshot),
        "research_query": plan.query,
        "refined_filters": {},
        "route": "retrieve",
    }
    detail = {"signal_type": snapshot.signal_type, "detector_version": snapshot.detector_version}
    return NodeResult(updates, "loaded", detail)


def retrieve_context(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    attempt = state.get("retrieval_attempts", 0) + 1
    if attempt > deps.budget.max_retrieval_attempts:  # unreachable by routing; defensive
        return NodeResult(
            _stop("insufficient_evidence", "retrieval_budget_exhausted"), "over_budget"
        )
    signal = state["signal"]
    query = state["research_query"]
    filters = RetrievalFilters(**state.get("refined_filters", {}))
    with deps.session_factory() as session:
        if attempt == 1:
            chunks = retrieve(session, deps.embedder, query, DEFAULT_TOP_K, filters)
        else:
            targeted = retrieve(session, deps.embedder, query, REFINED_TOP_K, filters)
            chunks = merge_chunks(targeted, state.get("previous_chunks", []), DEFAULT_TOP_K)
        session.rollback()  # read-only
    bundle = build_evidence_bundle(
        signal,
        RetrievalPlan(query=query, top_k=DEFAULT_TOP_K),
        chunks,
        deps.embedder.name,
        deps.known_components,
    )
    updates: dict[str, Any] = {
        "retrieval_attempts": attempt,
        "queries": [*state.get("queries", []), query],
        "evidence": bundle,
        "retrieved_chunk_ids": list(bundle.chunk_ids),
        "route": "assess_evidence",
    }
    if attempt == 1:
        updates["previous_chunks"] = chunks
    detail = {
        "attempt": attempt,
        "query": query,
        "filters": filters.as_dict(),
        "retrieved": len(chunks),
        "excerpts": [
            {"chunk_id": str(e.chunk_id), "source_key": e.source_key, "score": e.score}
            for e in bundle.excerpts
        ],
    }
    return NodeResult(updates, f"{len(bundle.excerpts)}_excerpts", detail)


def assess_evidence(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    bundle = state["evidence"]
    sufficiency = assess_sufficiency(bundle)
    attempts = state.get("retrieval_attempts", 0)
    refinements = state.get("refinements", 0)
    updates: dict[str, Any] = {"sufficiency": sufficiency.as_dict()}
    if sufficiency.sufficient:
        if state.get("llm_calls", 0) >= deps.budget.max_llm_calls:
            updates |= _stop("failed", "llm_budget_exhausted")
        else:
            updates["route"] = "generate_hypothesis"
    elif bundle.retrieved == 0 and refinements == 0:
        updates |= _stop("insufficient_evidence", "no_context")  # nothing to refine against
    elif (
        attempts < deps.budget.max_retrieval_attempts and refinements < deps.budget.max_refinements
    ):
        updates["route"] = "refine_query"
    else:
        reason = "insufficient_after_refinement" if refinements else "retrieval_budget_exhausted"
        updates |= _stop("insufficient_evidence", reason)
    outcome = "sufficient" if sufficiency.sufficient else "insufficient"
    return NodeResult(updates, outcome, sufficiency.as_dict())


def refine_query(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    refinements = state.get("refinements", 0)
    if refinements >= deps.budget.max_refinements:  # unreachable by routing; defensive
        return NodeResult(
            _stop("insufficient_evidence", "refinement_budget_exhausted"), "over_budget"
        )
    sufficiency = assess_sufficiency(state["evidence"])
    plan = refine_plan(state["signal"], sufficiency)
    updates = {
        "research_query": plan.query,
        "refined_filters": plan.filters.as_dict(),
        "refinements": refinements + 1,
        "route": "retrieve",
    }
    detail = {
        "query": plan.query,
        "filters": plan.filters.as_dict(),
        "addressing": list(sufficiency.reasons),
    }
    return NodeResult(updates, "refined", detail)


def generate_hypothesis(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    generations = state.get("hypothesis_generations", 0)
    if generations >= deps.budget.max_hypothesis_generations:  # defensive
        return NodeResult(_stop("failed", "hypothesis_budget_exhausted"), "over_budget")
    if state.get("llm_calls", 0) >= deps.budget.max_llm_calls:  # defensive
        return NodeResult(_stop("failed", "llm_budget_exhausted"), "over_budget")

    outcome = generate_from_bundle(
        deps.session_factory, uuid.UUID(state["signal_id"]), state["evidence"], deps.llm
    )
    accounted = _account(
        state, outcome.provider_called, outcome.input_tokens, outcome.output_tokens
    )
    updates: dict[str, Any] = {
        **accounted,
        "hypothesis_generations": generations + 1,
        "hypothesis_run_id": str(outcome.run_id),
        "hypothesis_id": str(outcome.hypothesis_id) if outcome.hypothesis_id else None,
        "hypothesis_status": outcome.status,
    }
    llm_calls = accounted.get("llm_calls", state.get("llm_calls", 0))
    if outcome.status == "succeeded":
        with deps.session_factory() as session:
            hypothesis = session.get(Hypothesis, outcome.hypothesis_id)
            updates["hypothesis_confidence"] = hypothesis.confidence if hypothesis else None
            session.rollback()
        over_llm = llm_calls >= deps.budget.max_llm_calls
        over_critique = state.get("critique_calls", 0) >= deps.budget.max_critique_calls
        if over_llm or over_critique:
            updates |= _stop("failed", "llm_budget_exhausted")
        else:
            updates["route"] = "critique_hypothesis"
    elif outcome.status == "insufficient_evidence":
        updates |= _stop("insufficient_evidence", "hypothesis_insufficient_evidence")
    else:
        updates |= _stop("failed", f"hypothesis_{outcome.status}")
    detail = {
        "hypothesis_run_id": str(outcome.run_id),
        "status": outcome.status,
        "error_type": outcome.error_type,
        "llm_call": outcome.provider_called,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "latency_ms": outcome.latency_ms,
    }
    return NodeResult(updates, outcome.status, detail)


def critique_hypothesis(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    calls = state.get("critique_calls", 0)
    if calls >= deps.budget.max_critique_calls or state.get("llm_calls", 0) >= (
        deps.budget.max_llm_calls
    ):  # unreachable by routing; defensive
        return NodeResult(_stop("failed", "llm_budget_exhausted"), "over_budget")
    with deps.session_factory() as session:
        hypothesis = session.get(Hypothesis, uuid.UUID(state["hypothesis_id"] or ""))
        if hypothesis is None:
            session.rollback()
            return NodeResult(_stop("failed", "hypothesis_missing"), "hypothesis_missing")
        request = build_critique_request(hypothesis, state["evidence"])
        session.rollback()

    result, failed_status, error_type, latency_ms = call_provider(deps.llm, request)
    usage = result.usage if result else None
    updates: dict[str, Any] = {
        **_account(
            state,
            True,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
        ),
        "critique_calls": calls + 1,
    }
    detail: dict[str, Any] = {
        "request_version": CRITIQUE_REQUEST_VERSION,
        "provider": result.provider if result else deps.llm.name,
        "model": result.model if result else deps.llm.model,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "latency_ms": latency_ms,
    }
    if result is None:
        assert failed_status is not None
        updates |= _stop("failed", f"critique_{failed_status}", critique_status=failed_status)
        return NodeResult(updates, failed_status, {**detail, "error_type": error_type})

    check = check_critique(result.output_text)
    detail |= {"status": check.status, "error_type": check.error_type, "errors": check.errors}
    if check.draft is None:
        updates |= _stop("failed", "critique_invalid_output", critique_status="invalid_output")
        return NodeResult(updates, "invalid_output", detail)

    verdict = check.draft.verdict
    updates |= {"critique": check.draft.model_dump(), "critique_status": "valid"}
    detail["verdict"] = verdict
    if verdict == "reject":
        updates |= _stop("rejected", "critique_reject")
    elif verdict == "human_review":
        updates |= {"route": "human_review", "review_reason": "critique_human_review"}
    elif state.get("hypothesis_confidence") == "low":
        # A deterministic rule on top of the critic: low confidence always gets a human.
        updates |= {"route": "human_review", "review_reason": "low_confidence"}
    else:
        updates |= _stop("succeeded", "critique_accept")
    return NodeResult(updates, verdict, detail)


def human_review(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    reason = state.get("review_reason") or "human_review"
    return NodeResult({"route": END}, "waiting_for_human", {"review_reason": reason})


def apply_human_decision(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    decision = state.get("human_decision")
    if decision == "approve":
        updates = _stop("succeeded", "human_approved")
    elif decision == "reject":
        updates = _stop("rejected", "human_rejected")
    else:  # the service validates decisions; defensive
        updates = _stop("failed", "invalid_human_decision")
    return NodeResult(updates, f"human_{decision}", {"decision": decision})


def finalize(state: ResearchState, deps: ResearchDeps) -> NodeResult:
    status = state.get("final_status") or "failed"
    reason = state.get("stop_reason") or "internal_no_outcome"
    updates = {"route": END, "final_status": status, "stop_reason": reason}
    return NodeResult(updates, status, {"stop_reason": reason})


NODE_FUNCTIONS: dict[str, NodeFn] = {
    "load_signal": load_signal,
    "retrieve": retrieve_context,
    "assess_evidence": assess_evidence,
    "refine_query": refine_query,
    "generate_hypothesis": generate_hypothesis,
    "critique_hypothesis": critique_hypothesis,
    "human_review": human_review,
    "apply_human_decision": apply_human_decision,
    "finalize": finalize,
}


# ---- wiring ------------------------------------------------------------------------------


def _ledgered(name: str, fn: NodeFn, deps: ResearchDeps) -> Any:  # a LangGraph node
    allowed = TRANSITIONS[name]

    def node(state: ResearchState) -> dict[str, Any]:
        result = fn(state, deps)
        updates = dict(result.updates)
        steps = state.get("steps", 0) + 1
        if updates.get("route") not in allowed:
            raise GraphContractError(f"{name} chose {updates.get('route')!r}")
        # Defensive step cap: always leave room for finalize.
        if (
            name not in TERMINAL_NODES
            and updates["route"] != "finalize"
            and steps >= deps.budget.max_graph_steps - 1
        ):
            updates |= _stop("failed", "step_budget_exhausted")
        merged: ResearchState = {**state, **updates, "steps": steps}  # type: ignore[typeddict-item]
        record_step(deps.session_factory, merged, name, result.outcome, result.detail)
        logger.info(
            "research step",
            extra={
                "context": {
                    "research_run_id": state["research_run_id"],
                    "signal_id": state["signal_id"],
                    "graph_version": GRAPH_VERSION,
                    "node": name,
                    "sequence": steps,
                    "outcome": result.outcome,
                    "retrieval_attempts": merged.get("retrieval_attempts", 0),
                    "llm_calls": merged.get("llm_calls", 0),
                }
            },
        )
        return {**updates, "steps": steps, "trajectory": [name]}

    return node


def _router(name: str) -> Callable[[ResearchState], str]:
    def route(state: ResearchState) -> str:
        return state["route"]

    route.__name__ = f"route_after_{name}"
    return route


def _entry(state: ResearchState) -> str:
    return "apply_human_decision" if state.get("human_decision") else "load_signal"


def build_graph(deps: ResearchDeps) -> CompiledStateGraph[Any, Any, Any, Any]:
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ResearchState)
    for name in NODES:
        builder.add_node(name, _ledgered(name, NODE_FUNCTIONS[name], deps))
    builder.add_conditional_edges(START, _entry, {t: t for t in TRANSITIONS[START]})
    for name in NODES:
        targets = TRANSITIONS[name]
        if targets == (END,):
            builder.add_edge(name, END)
        else:
            builder.add_conditional_edges(name, _router(name), {t: t for t in targets})
    return builder.compile()


def trajectory_is_valid(trajectory: Sequence[str]) -> bool:
    """Every consecutive pair of executed nodes is an allowed transition."""
    if not trajectory:
        return False
    previous = START
    for node in trajectory:
        if node not in TRANSITIONS.get(previous, ()):
            return False
        previous = node
    return END in TRANSITIONS.get(previous, ())
