"""RulesDecider (rules.v1): the deterministic baseline every other decider must beat.

Derived from the fields Step 10 actually persists. First matching rule wins:

  1. research rejected (critique or human) -> reject        critique_rejected | human_rejected
  2. critique lists unsupported claims     -> reject        unsupported_claim
  3. a human approved the research         -> proceed       human_approved
  4. hypothesis confidence is low          -> human_review  low_confidence
  5. critique lists missing evidence       -> human_review  missing_evidence
  6. critique lists issues                 -> human_review  critique_issues
  7. otherwise (accepted, no findings)     -> proceed       critique_accepted, sufficient_evidence

Rule 3 comes after the rejects but before the soft concerns: those concerns
are exactly what the human reviewed. Confidence is the rule's certainty, not
a probability: "high" for rejects and human decisions, the hypothesis's own
confidence for a clean proceed, "medium" for review rules.
"""

from typing import Any

from .port import DeciderReply
from .request import DecisionRequest

RULES_VERSION = "rules.v1"


def rules_output(request: DecisionRequest) -> dict[str, Any]:
    research, hypothesis, critique = request.research, request.hypothesis, request.critique

    def out(decision: str, confidence: str, *codes: str) -> dict[str, Any]:
        return {"decision": decision, "confidence": confidence, "reason_codes": list(codes)}

    if research.status == "rejected" or critique.verdict == "reject":
        code = "human_rejected" if research.human_decision == "reject" else "critique_rejected"
        return out("reject", "high", code)
    if critique.unsupported_claims:
        return out("reject", "high", "unsupported_claim")
    if research.human_decision == "approve":
        return out("proceed", "high", "human_approved")
    if hypothesis.confidence == "low":
        return out("human_review", "medium", "low_confidence")
    if critique.missing_evidence:
        return out("human_review", "medium", "missing_evidence")
    if critique.issues:
        return out("human_review", "medium", "critique_issues")
    return out("proceed", hypothesis.confidence, "critique_accepted", "sufficient_evidence")


class RulesDecider:
    name = "rules"
    version = RULES_VERSION

    def decide(self, request: DecisionRequest) -> DeciderReply:
        return DeciderReply(output=rules_output(request), decider_version=RULES_VERSION)
