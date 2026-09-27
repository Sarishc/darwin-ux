"""Fail-closed policy: what a decider's output is allowed to mean. Enforced here, not by any model.

1. No valid output (exception, timeout, unavailable, non-JSON, unknown field,
   unknown decision, unknown reason code, bad confidence) -> the final decision
   is human_review, status "failed_closed", reason "decider_failure".
2. A valid "proceed" is accepted only if every proceed precondition holds;
   otherwise it becomes human_review, status "overridden", reason
   "policy_override". The preconditions are hard facts about the artifact:
     - research succeeded, and the hypothesis was accepted;
     - the critique accepted it, or a human approved it;
     - the critique lists no unsupported claims;
     - the hypothesis is not low-confidence (unless a human approved it);
     - the decider itself is not low-confidence.
3. Valid human_review / reject are always accepted: a decider may make the
   outcome MORE cautious, never less.

The database repeats the essentials as CHECKs: a failed_closed run is always
human_review, and proceed only ever appears with status "decided".
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from darwin.hypotheses.schema import error_list, parse_json_object

from .request import DecisionRequest
from .vocabulary import Decision, DecisionOutput

DecisionStatus = Literal["decided", "overridden", "failed_closed"]


@dataclass(frozen=True)
class PolicyOutcome:
    decision: Decision  # the FINAL decision DarwinUX records
    status: DecisionStatus
    reason_codes: tuple[str, ...]
    decider_output: DecisionOutput | None  # what the decider validly said, if anything
    error_type: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def validate_output(
    output: Mapping[str, Any] | str,
) -> tuple[DecisionOutput | None, str | None, list[dict[str, str]]]:
    if isinstance(output, str):
        parsed, unparseable = parse_json_object(output)
        if parsed is None:
            assert unparseable is not None
            return None, unparseable, [{"loc": "", "type": unparseable}]
        output = parsed
    try:
        return DecisionOutput.model_validate(dict(output)), None, []
    except ValidationError as error:
        errors = error_list(error)
        return None, errors[0]["type"], errors


def failed_proceed_preconditions(request: DecisionRequest, output: DecisionOutput) -> list[str]:
    research, hypothesis, critique = request.research, request.hypothesis, request.critique
    human_approved = research.human_decision == "approve"
    failing = []
    if research.status != "succeeded":
        failing.append("research_not_succeeded")
    if hypothesis.status != "accepted":
        failing.append("hypothesis_not_accepted")
    if critique.verdict != "accept" and not human_approved:
        failing.append("critique_not_accepted")
    if critique.unsupported_claims:
        failing.append("unsupported_claims")
    if hypothesis.confidence == "low" and not human_approved:
        failing.append("low_confidence_hypothesis")
    if output.confidence == "low":
        failing.append("low_confidence_decider")
    return failing


def fail_closed(error_type: str, errors: list[dict[str, str]] | None = None) -> PolicyOutcome:
    return PolicyOutcome(
        decision="human_review",
        status="failed_closed",
        reason_codes=("decider_failure",),
        decider_output=None,
        error_type=error_type[:64],
        errors=errors or [{"loc": "", "type": error_type[:64]}],
    )


def apply_policy(request: DecisionRequest, output: Mapping[str, Any] | str) -> PolicyOutcome:
    validated, error_type, errors = validate_output(output)
    if validated is None:
        return fail_closed(error_type or "invalid_output", errors)
    if validated.decision == "proceed":
        failing = failed_proceed_preconditions(request, validated)
        if failing:
            return PolicyOutcome(
                decision="human_review",
                status="overridden",
                reason_codes=("policy_override", *validated.reason_codes),
                decider_output=validated,
                error_type=f"precondition:{failing[0]}"[:64],
                errors=[{"loc": "policy", "type": name} for name in failing],
            )
    return PolicyOutcome(
        decision=validated.decision,
        status="decided",
        reason_codes=tuple(validated.reason_codes),
        decider_output=validated,
    )
