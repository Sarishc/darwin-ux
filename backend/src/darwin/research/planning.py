"""Deterministic research planning: first query, sufficiency heuristic, one refinement.

No model is involved in any of this.

First query: exactly the Step 9 query builder (darwin.hypotheses.queries).

Sufficiency is a DETERMINISTIC HEURISTIC, not a judgment of truth. After the
Step 9 relevance floor has dropped weak excerpts, evidence counts as
"sufficient" when there are
  - at least MIN_EXCERPTS excerpts,
  - from at least MIN_DISTINCT_SOURCES different sources,
  - at least one of them an *anchor* source — the UI Spec (what the component
    is) or the system-generated detector definitions (why the signal fired).
It says the bundle has the right *shape* to support a hypothesis, nothing
about whether the text is correct.

Refinement (at most one): a signal-specific query aimed at the UI Spec, and
— when no anchor source was found — the SQL filters source_type = ui_spec,
generation = 0 (Step 8 filters; chosen by code, never by a model). The
targeted results are merged in front of the first attempt's.
"""

from dataclasses import dataclass, replace
from typing import Any

from darwin.db.models import BehaviorSignal
from darwin.hypotheses.evidence import EvidenceBundle
from darwin.hypotheses.queries import signal_component
from darwin.memory.retrieval import RetrievalFilters, RetrievedChunk
from darwin.signals import detectors

MIN_EXCERPTS = 2
MIN_DISTINCT_SOURCES = 2
ANCHOR_SOURCE_TYPES = ("ui_spec", "system_generated")
REFINED_TOP_K = 3


@dataclass(frozen=True)
class Sufficiency:
    sufficient: bool
    reasons: tuple[str, ...]  # why not sufficient; empty when sufficient
    excerpts: int
    distinct_sources: int
    anchor_source_types: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "reasons": list(self.reasons),
            "excerpts": self.excerpts,
            "distinct_sources": self.distinct_sources,
            "anchor_source_types": list(self.anchor_source_types),
        }


def assess_sufficiency(bundle: EvidenceBundle) -> Sufficiency:
    excerpts = bundle.excerpts
    sources = {e.source_key for e in excerpts}
    anchors = tuple(t for t in ANCHOR_SOURCE_TYPES if any(e.source_type == t for e in excerpts))
    reasons = []
    if len(excerpts) < MIN_EXCERPTS:
        reasons.append("too_few_excerpts")
    if len(sources) < MIN_DISTINCT_SOURCES:
        reasons.append("too_few_sources")
    if not anchors:
        reasons.append("no_anchor_source")
    return Sufficiency(not reasons, tuple(reasons), len(excerpts), len(sources), anchors)


@dataclass(frozen=True)
class RefinedPlan:
    query: str
    filters: RetrievalFilters
    top_k: int = REFINED_TOP_K


def refine_plan(signal: BehaviorSignal, sufficiency: Sufficiency) -> RefinedPlan:
    """The one refinement. Deterministic; plain text; filters chosen by code only."""
    if signal.signal_type == detectors.RAGE_CLICK:
        component = signal_component(signal) or "button"
        query = (
            f"UI Spec component {component}: button feedback, action and plan card "
            "in a Generation 0 page section"
        )
    else:
        query = (
            "UI Spec signup form: fields, validation on submit, summary error display "
            "in a Generation 0 page section"
        )
    filters = (
        RetrievalFilters(source_type="ui_spec", generation=0)
        if "no_anchor_source" in sufficiency.reasons
        else RetrievalFilters()
    )
    return RefinedPlan(query=query, filters=filters)


def merge_chunks(
    targeted: list[RetrievedChunk], previous: list[RetrievedChunk], top_k: int
) -> list[RetrievedChunk]:
    """Targeted results first (they are why we refined), then the earlier ones; re-ranked."""
    merged: list[RetrievedChunk] = []
    seen: set[str] = set()
    for chunk in [*targeted[:REFINED_TOP_K], *previous]:
        if str(chunk.chunk_id) not in seen:
            seen.add(str(chunk.chunk_id))
            merged.append(chunk)
    return [replace(c, rank=i) for i, c in enumerate(merged[:top_k], start=1)]
