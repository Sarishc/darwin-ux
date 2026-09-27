"""ResearchState: the graph's typed state. Facts, ids, counters, decisions — no reasoning.

It lives in memory for one graph invocation (there is no LangGraph
checkpointer). What must survive — status, counters, ids, the critique's
findings — is persisted by DarwinUX in research_run / research_step after
every node, and a resume rebuilds the small state it needs from there.

The EvidenceBundle (bounded excerpts) is held in memory only: it is never
logged and never persisted; runs record chunk ids and an evidence hash.
"""

import operator
from typing import Annotated, Any, TypedDict

from darwin.db.models import BehaviorSignal
from darwin.hypotheses.evidence import EvidenceBundle
from darwin.memory.retrieval import RetrievedChunk


class ResearchState(TypedDict, total=False):
    # identity
    research_run_id: str
    signal_id: str
    signal_type: str
    component: str | None
    signal: BehaviorSignal  # a transient copy, never attached to a session

    # research
    research_query: str  # the query of the latest (or next) retrieval
    refined_filters: dict[str, Any]  # SQL filters of the refined retrieval ({} if none)
    queries: list[str]  # every query actually run, in order
    retrieval_attempts: int
    refinements: int
    previous_chunks: list[RetrievedChunk]  # attempt 1, merged into attempt 2
    evidence: EvidenceBundle
    retrieved_chunk_ids: list[str]
    sufficiency: dict[str, Any]  # {"sufficient": bool, "reasons": [...], ...}

    # generation (Step 9)
    hypothesis_generations: int
    hypothesis_run_id: str | None
    hypothesis_id: str | None
    hypothesis_status: str | None
    hypothesis_confidence: str | None

    # critique
    critique_calls: int
    critique: dict[str, Any] | None  # validated findings only
    critique_status: str | None

    # human review
    review_reason: str | None
    human_decision: str | None  # "approve" | "reject", only when resuming

    # accounting
    llm_calls: int
    input_tokens: int  # sum of reported counts (fake: estimates, not billing data)
    output_tokens: int
    calls_without_usage: int
    steps: int
    trajectory: Annotated[list[str], operator.add]

    # routing and outcome
    route: str  # the next node, chosen by the node that just ran
    final_status: str | None  # set when a node decides the run must end
    stop_reason: str | None
