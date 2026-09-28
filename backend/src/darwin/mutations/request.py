"""Provenance re-check and the MutationRequest (mutation_request.v1).

A stored `proceed` is not permanent authority. Before any generator is
called, everything it rested on is re-derived and compared:

- the DecisionRun exists and its final decision is proceed;           (else refused)
- Step 11's DecisionRequest can still be built for its research run  (research
  still eligible, hypothesis still accepted and still this run's, signal still
  canonical, critique still valid) AND hashes exactly as it did when the
  decision was made — any change to those facts makes the decision stale;
- the source UI Spec is the page's CURRENT baseline (a newer baseline, or a
  candidate, is refused); the candidate is for generation current + 1.

Stale provenance is recorded as a MutationRun (status stale_provenance) with
no request and no generator call; a missing decision, a non-proceed decision
or a missing baseline is refused outright (MutationInputError).

The request holds bounded data only: decision summary, hypothesis
statement/component/confidence/limitations, critique findings (all
untrusted model output), the source spec's id/generation/hash, and for each
mutable node in the affected area: its id, type, current values and allowed
values. Never the full spec, Product Memory text, telemetry, session/event
ids, prompts or paths.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, DecisionRun, Hypothesis, ResearchRun, UISpecVersion
from darwin.decisions.request import DecisionInputError, build_decision_request

from .specs import current_baseline
from .surface import MUTABLE, Target, describe_target, iter_targets

MUTATION_REQUEST_VERSION = "mutation_request.v1"
MAX_OPERATIONS = 5
DEMO_PAGE_ID = "pricing_signup"  # the only page Generation 0 defines


class MutationInputError(ValueError):
    """Not eligible for mutation at all; nothing is recorded."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StaleProvenanceError(ValueError):
    """The proceed decision no longer holds; recorded as stale_provenance."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _strict() -> ConfigDict:
    return ConfigDict(extra="forbid", strict=True, frozen=True)


class DecisionFactsV1(BaseModel):
    model_config = _strict()
    decision_run_id: str
    decision: Literal["proceed"]
    decider_version: str = Field(max_length=128)
    reason_codes: list[str] = Field(max_length=5)


class HypothesisFactsV1(BaseModel):
    model_config = _strict()
    statement: str = Field(max_length=400)  # untrusted
    affected_component: str | None = Field(max_length=128)
    confidence: Literal["low", "medium", "high"]
    limitations: list[str] = Field(max_length=5)  # untrusted


class CritiqueFactsV1(BaseModel):
    model_config = _strict()
    issues: list[str] = Field(max_length=5)  # untrusted
    missing_evidence: list[str] = Field(max_length=5)


class SourceSpecV1(BaseModel):
    model_config = _strict()
    spec_id: str
    page_id: str
    generation: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TargetViewV1(BaseModel):
    model_config = _strict()
    component_id: str
    type: str
    properties: dict[str, dict[str, Any]]


class ConstraintsV1(BaseModel):
    model_config = _strict()
    operation: Literal["replace"]
    max_operations: int
    authority: Literal["candidate_data_only"]


class MutationRequest(BaseModel):
    model_config = _strict()

    request_version: Literal["mutation_request.v1"]
    signal_type: str
    decision: DecisionFactsV1
    hypothesis: HypothesisFactsV1
    critique: CritiqueFactsV1
    source_spec: SourceSpecV1
    targets: list[TargetViewV1] = Field(min_length=1, max_length=40)
    constraints: ConstraintsV1

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def request_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProceedContext:
    decision_run: DecisionRun
    source: UISpecVersion


def load_proceed_context(
    session: Session, decision_run_id: uuid.UUID, source_spec_id: uuid.UUID | None
) -> ProceedContext:
    """Refuse (MutationInputError) or declare stale (StaleProvenanceError), else return context."""
    decision = session.get(DecisionRun, decision_run_id)
    if decision is None:
        raise MutationInputError("decision_run_not_found", f"no decision run {decision_run_id}")
    if decision.decision != "proceed" or decision.status != "decided":
        raise MutationInputError(
            "decision_not_proceed", f"decision is {decision.decision} ({decision.status})"
        )
    baseline = current_baseline(session, DEMO_PAGE_ID)
    if baseline is None:
        raise MutationInputError("baseline_missing", "no baseline UI Spec; run make ui-spec-import")
    if source_spec_id is not None and source_spec_id != baseline.id:
        if session.get(UISpecVersion, source_spec_id) is None:
            raise MutationInputError("source_spec_not_found", f"no UI Spec {source_spec_id}")
        raise StaleProvenanceError(
            "source_spec_not_current", "the source spec is not the page's current baseline"
        )
    context = ProceedContext(decision_run=decision, source=baseline)
    try:
        rebuilt = build_decision_request(session, decision.research_run_id)
    except DecisionInputError as error:
        raise StaleProvenanceError(error.code, str(error)) from error
    if rebuilt.request_hash() != decision.request_hash:
        raise StaleProvenanceError(
            "decision_inputs_changed", "the facts the decision was made on have changed"
        )
    run = session.get(ResearchRun, decision.research_run_id)
    if run is None or run.hypothesis_id != decision.hypothesis_id:
        raise StaleProvenanceError("provenance_mismatch", "decision and research disagree")
    _require_signal_from_source(session, run.signal_id, baseline)
    return context


def _require_signal_from_source(
    session: Session, signal_id: uuid.UUID, source: UISpecVersion
) -> None:
    """Fail closed when the signal's evidence is not proven to come from the source spec.

    Step 15: a signal observed on another generation (or on an experiment candidate)
    must not drive a mutation of the current one. Unknown attribution (every event
    before Step 15) is accepted only while the page has never had a promoted
    generation — then there is only one generation it could have come from.
    """
    signal = session.scalar(select(BehaviorSignal).where(BehaviorSignal.signal_id == signal_id))
    if signal is None:
        raise StaleProvenanceError("signal_missing", "the research signal no longer exists")
    if signal.ui_attribution == "mixed":
        raise StaleProvenanceError(
            "signal_generation_mixed", "the signal's evidence spans several UI versions"
        )
    if signal.ui_attribution == "single" and signal.ui_spec_version_id != source.id:
        raise StaleProvenanceError(
            "signal_generation_mismatch", "the signal was observed on a different UI version"
        )
    if signal.ui_attribution == "unknown":
        promoted = session.scalar(
            select(UISpecVersion.id)
            .where(UISpecVersion.page_id == source.page_id, UISpecVersion.status == "promoted")
            .limit(1)
        )
        if promoted is not None:
            raise StaleProvenanceError(
                "signal_generation_unknown",
                "the page has several generations and the signal's is unknown",
            )


def affected_targets(spec: dict[str, Any], component: str | None) -> list[Target]:
    """Mutable nodes in the affected component's section (the whole page if unknown)."""
    targets = [t for t in iter_targets(spec) if t.kind in MUTABLE]
    if component is None:
        return targets
    for section in spec["page"]["sections"]:
        section_ids = {t.component_id for t in iter_targets({"page": {"sections": [section]}})}
        if component in section_ids:
            return [t for t in targets if t.component_id in section_ids]
    return targets


def build_mutation_request(session: Session, context: ProceedContext) -> MutationRequest:
    decision, source = context.decision_run, context.source
    hypothesis = session.get(Hypothesis, decision.hypothesis_id)
    run = session.get(ResearchRun, decision.research_run_id)
    assert hypothesis is not None and run is not None and run.critique is not None
    rebuilt = build_decision_request(session, decision.research_run_id)
    return MutationRequest(
        request_version=MUTATION_REQUEST_VERSION,
        signal_type=rebuilt.signal.signal_type,
        decision=DecisionFactsV1(
            decision_run_id=str(decision.id),
            decision="proceed",
            decider_version=decision.decider_version,
            reason_codes=list(decision.reason_codes),
        ),
        hypothesis=HypothesisFactsV1(
            statement=hypothesis.statement,
            affected_component=hypothesis.affected_component,
            confidence=hypothesis.confidence,
            limitations=list(hypothesis.limitations)[:5],
        ),
        critique=CritiqueFactsV1(
            issues=list(run.critique.get("issues", []))[:5],
            missing_evidence=list(run.critique.get("missing_evidence", []))[:5],
        ),
        source_spec=SourceSpecV1(
            spec_id=str(source.id),
            page_id=source.page_id,
            generation=source.generation or 0,
            content_hash=source.content_hash,
        ),
        targets=[
            TargetViewV1(**describe_target(t))
            for t in affected_targets(source.spec, hypothesis.affected_component)
        ],
        constraints=ConstraintsV1(
            operation="replace", max_operations=MAX_OPERATIONS, authority="candidate_data_only"
        ),
    )
