"""DecisionRequest (decision_request.v1): bounded structured facts about one finished research run.

Built ONLY from persisted DarwinUX rows — never from caller-supplied JSON —
after checking that the run is eligible:

- the ResearchRun exists and ended "succeeded" or "rejected" (a researched
  outcome); running, waiting, insufficient or failed runs are not decidable;
- it has a Hypothesis, which belongs to this run (same hypothesis run, same
  signal) and whose status matches the outcome (accepted / rejected);
- its critique is structurally valid (the Step 10 CritiqueDraft);
- the underlying signal is still canonical (not superseded).

What goes in: signal type and safe facts (the Step 9 allowlist), the research
outcome and accounting, the hypothesis statement / component / confidence /
cited sources / limitations, the critique findings, and the fixed constraints.
What never goes in: session ids, event ids, payloads, Product Memory text,
prompts, model reasoning, credentials, the hypothesis rationale.

Text fields (statement, limitations, critique findings) are model output and
are UNTRUSTED data for any decider that reads them.
"""

import hashlib
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, Hypothesis, ResearchRun
from darwin.hypotheses.evidence import safe_signal_facts
from darwin.research.critique import CritiqueDraft

from .vocabulary import DECIDER_REASON_CODES, DECISIONS, Confidence, Decision

DECISION_REQUEST_VERSION = "decision_request.v1"
ELIGIBLE_RESEARCH_STATUSES = ("succeeded", "rejected")
_EXPECTED_HYPOTHESIS_STATUS = {"succeeded": "accepted", "rejected": "rejected"}


class DecisionInputError(ValueError):
    """The research artifact cannot be decided. `code` says why; no decider is called."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _strict() -> ConfigDict:
    return ConfigDict(extra="forbid", strict=True, frozen=True)


class SignalFactsV1(BaseModel):
    model_config = _strict()
    signal_type: str = Field(max_length=64)
    detector_version: str = Field(max_length=16)
    facts: dict[str, Any]


class ResearchFactsV1(BaseModel):
    model_config = _strict()
    research_run_id: str = Field(max_length=36)
    graph_version: str = Field(max_length=32)
    status: Literal["succeeded", "rejected"]
    stop_reason: str = Field(max_length=64)
    human_decision: Literal["approve", "reject"] | None
    retrieval_attempts: int = Field(ge=0, le=2)
    refined: bool
    llm_calls: int = Field(ge=0, le=2)


class EvidenceSourceV1(BaseModel):
    model_config = _strict()
    source_key: str = Field(max_length=256)
    section: str = Field(max_length=256)


class HypothesisFactsV1(BaseModel):
    model_config = _strict()
    statement: str = Field(max_length=400)  # untrusted model output
    affected_component: str | None = Field(max_length=128)
    confidence: Confidence
    status: Literal["accepted", "rejected"]
    evidence_sources: list[EvidenceSourceV1] = Field(min_length=1, max_length=5)
    limitations: list[str] = Field(max_length=5)  # untrusted model output


class CritiqueFactsV1(BaseModel):
    model_config = _strict()
    verdict: Literal["accept", "human_review", "reject"]
    issues: list[str] = Field(max_length=5)  # untrusted model output
    unsupported_claims: list[str] = Field(max_length=5)
    missing_evidence: list[str] = Field(max_length=5)


class ConstraintsV1(BaseModel):
    model_config = _strict()
    allowed_decisions: list[Decision]
    allowed_reason_codes: list[str]
    authority: Literal["eligibility_only"]


CONSTRAINTS = ConstraintsV1(
    allowed_decisions=list(DECISIONS),
    allowed_reason_codes=list(DECIDER_REASON_CODES),
    authority="eligibility_only",
)


class DecisionRequest(BaseModel):
    model_config = _strict()

    request_version: Literal["decision_request.v1"]
    signal: SignalFactsV1
    research: ResearchFactsV1
    hypothesis: HypothesisFactsV1
    critique: CritiqueFactsV1
    constraints: ConstraintsV1

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def request_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def build_decision_request(session: Session, research_run_id: uuid.UUID) -> DecisionRequest:
    """Load, check eligibility, project. Raises DecisionInputError for anything undecidable."""
    run = session.get(ResearchRun, research_run_id)
    if run is None:
        raise DecisionInputError("research_run_not_found", f"no research run {research_run_id}")
    if run.status not in ELIGIBLE_RESEARCH_STATUSES:
        raise DecisionInputError(
            "research_not_eligible", f"research run is {run.status}, not a researched outcome"
        )
    if run.hypothesis_id is None or run.hypothesis_run_id is None:
        raise DecisionInputError("hypothesis_missing", "research run has no hypothesis")
    hypothesis = session.get(Hypothesis, run.hypothesis_id)
    if hypothesis is None:
        raise DecisionInputError("hypothesis_missing", "hypothesis row not found")
    if hypothesis.run_id != run.hypothesis_run_id or hypothesis.signal_id != run.signal_id:
        raise DecisionInputError(
            "provenance_mismatch", "hypothesis does not belong to this research run"
        )
    if hypothesis.status != _EXPECTED_HYPOTHESIS_STATUS[run.status]:
        raise DecisionInputError(
            "hypothesis_status_mismatch",
            f"hypothesis is {hypothesis.status} but research {run.status}",
        )
    signal = session.query(BehaviorSignal).filter_by(signal_id=run.signal_id).one_or_none()
    if signal is None:
        raise DecisionInputError("provenance_mismatch", "signal not found")
    if signal.superseded_at is not None:
        raise DecisionInputError(
            "signal_superseded", "the signal was superseded by later events; re-research it"
        )
    if run.critique is None:
        raise DecisionInputError("invalid_critique", "research run has no critique")
    try:
        critique = CritiqueDraft.model_validate(run.critique)
    except ValidationError as error:
        raise DecisionInputError("invalid_critique", "stored critique is not valid") from error

    facts = safe_signal_facts(signal)
    try:
        return DecisionRequest(
            request_version=DECISION_REQUEST_VERSION,
            signal=SignalFactsV1(
                signal_type=facts.signal_type,
                detector_version=facts.detector_version,
                facts=facts.facts,
            ),
            research=ResearchFactsV1(
                research_run_id=str(run.id),
                graph_version=run.graph_version,
                status=run.status,
                stop_reason=run.stop_reason or "",
                human_decision=run.human_decision,
                retrieval_attempts=run.retrieval_attempts,
                refined=run.retrieval_attempts > 1,
                llm_calls=run.llm_calls,
            ),
            hypothesis=HypothesisFactsV1(
                statement=hypothesis.statement,
                affected_component=hypothesis.affected_component,
                confidence=hypothesis.confidence,
                status=hypothesis.status,
                evidence_sources=[
                    EvidenceSourceV1(source_key=r["source_key"], section=r["section"][:256])
                    for r in hypothesis.evidence_references[:5]
                ],
                limitations=list(hypothesis.limitations)[:5],
            ),
            critique=CritiqueFactsV1(
                verdict=critique.verdict,
                issues=list(critique.issues),
                unsupported_claims=list(critique.unsupported_claims),
                missing_evidence=list(critique.missing_evidence),
            ),
            constraints=CONSTRAINTS,
        )
    except ValidationError as error:
        raise DecisionInputError("invalid_input", "research artifact failed validation") from error
