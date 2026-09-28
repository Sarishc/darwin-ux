"""decide_research_run: the Step 11 gate, one explicit call, no loop.

    load ResearchRun + Hypothesis + Signal -> eligibility checks -> DecisionRequest
      -> Decider.decide (exactly once) -> strict validation -> fail-closed policy
      -> DecisionRun (always, once the request was built) -> DecisionOutcome

An ineligible artifact raises DecisionInputError before any decider is called
and records nothing. A decider problem of any kind is recorded as a
failed_closed human_review — never as proceed. The research, hypothesis and
signal rows are only read.

This is a separate service, not a graph node: research_graph.v1 is unchanged,
and a decider has no path to the graph's transitions or budgets.

Logs: ids, decider and versions, decision, status, confidence, reason codes,
latency. Never hypothesis text, critique text, prompts or evidence.
"""

import logging
import time
import uuid
from dataclasses import dataclass

from darwin.db.models import DecisionRun, ResearchRun
from darwin.hypotheses.service import SessionFactory
from darwin.observability import stage

from .policy import PolicyOutcome, apply_policy, fail_closed
from .port import Decider, DeciderReply, DeciderTimeoutError, DeciderUnavailableError
from .request import DecisionRequest, build_decision_request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionOutcome:
    decision_run_id: uuid.UUID
    research_run_id: uuid.UUID
    hypothesis_id: uuid.UUID
    decider: str
    decider_version: str
    request_version: str
    request_hash: str
    decision: str
    status: str
    decider_decision: str | None
    confidence: str | None
    reason_codes: tuple[str, ...]
    error_type: str | None
    latency_ms: float
    request: DecisionRequest


def _call(decider: Decider, request: DecisionRequest) -> tuple[DeciderReply | None, str | None]:
    try:
        return decider.decide(request), None
    except DeciderUnavailableError:
        return None, "decider_unavailable"
    except DeciderTimeoutError:
        return None, "decider_timeout"
    except Exception as error:  # DeciderFailureError, or a decider bug: both fail closed
        return None, f"decider_error:{type(error).__name__}"[:64]


def decide_research_run(
    session_factory: SessionFactory, research_run_id: uuid.UUID, decider: Decider
) -> DecisionOutcome:
    """Traced as `decision.run`: decider, version, decision, status, fail-closed flag —
    never the DecisionRequest, the hypothesis or the decider's reasoning."""
    with stage("decision.run", "decision", {"darwin.research_run.id": research_run_id}) as s:
        outcome = _decide_research_run(session_factory, research_run_id, decider)
        s.set(
            **{
                "darwin.decision_run.id": outcome.decision_run_id,
                "darwin.decider.type": outcome.decider,
                "darwin.decider.version": outcome.decider_version,
                "darwin.decision": outcome.decision,
                "darwin.status": outcome.status,
                "darwin.error.type": outcome.error_type,
                "darwin.decision.fail_closed": outcome.decision != outcome.decider_decision,
            }
        )
        s.outcome = outcome.decision
        return outcome


def _decide_research_run(
    session_factory: SessionFactory, research_run_id: uuid.UUID, decider: Decider
) -> DecisionOutcome:
    with session_factory() as session:
        request = build_decision_request(session, research_run_id)  # raises DecisionInputError
        research_run = session.get(ResearchRun, research_run_id)
        assert research_run is not None and research_run.hypothesis_id is not None  # validated
        hypothesis_id = research_run.hypothesis_id
        session.rollback()

    started = time.perf_counter()
    reply, failure = _call(decider, request)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    policy: PolicyOutcome = (
        apply_policy(request, reply.output) if reply is not None else fail_closed(failure or "")
    )
    decider_version = reply.decider_version if reply is not None else decider.version
    validated = policy.decider_output

    run = DecisionRun(
        id=uuid.uuid4(),
        research_run_id=research_run_id,
        hypothesis_id=hypothesis_id,
        request_version=request.request_version,
        request_hash=request.request_hash(),
        decider=decider.name,
        decider_version=decider_version[:128],
        decision=policy.decision,
        status=policy.status,
        decider_decision=validated.decision if validated else None,
        confidence=validated.confidence if validated else None,
        provider_confidence=validated.provider_confidence if validated else None,
        reason_codes=list(policy.reason_codes),
        error_type=policy.error_type,
        validation_errors=policy.errors,
        input_tokens=reply.input_tokens if reply else None,
        output_tokens=reply.output_tokens if reply else None,
        latency_ms=latency_ms,
    )
    run_id = run.id
    with session_factory() as session:
        session.add(run)
        session.commit()

    outcome = DecisionOutcome(
        decision_run_id=run_id,
        research_run_id=research_run_id,
        hypothesis_id=hypothesis_id,
        decider=decider.name,
        decider_version=decider_version,
        request_version=request.request_version,
        request_hash=request.request_hash(),
        decision=policy.decision,
        status=policy.status,
        decider_decision=validated.decision if validated else None,
        confidence=validated.confidence if validated else None,
        reason_codes=policy.reason_codes,
        error_type=policy.error_type,
        latency_ms=latency_ms,
        request=request,
    )
    logger.info(
        "decision recorded",
        extra={
            "context": {
                "decision_run_id": str(run_id),
                "research_run_id": str(research_run_id),
                "hypothesis_id": str(hypothesis_id),
                "decider": decider.name,
                "decider_version": decider_version,
                "request_version": request.request_version,
                "decision": policy.decision,
                "status": policy.status,
                "decider_decision": outcome.decider_decision,
                "confidence": outcome.confidence,
                "reason_codes": list(policy.reason_codes),
                "error_type": policy.error_type,
                "latency_ms": latency_ms,
            }
        },
    )
    return outcome
