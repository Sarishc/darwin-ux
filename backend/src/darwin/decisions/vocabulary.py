"""The closed decision vocabulary: three decisions, twelve reason codes, three confidence levels.

Everything a decider returns must come from these sets; anything else fails
closed (see policy.py). Adding a word here is a reviewed code change and a
new decision_request / decider version.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

Decision = Literal["proceed", "human_review", "reject"]
DECISIONS: tuple[Decision, ...] = ("proceed", "human_review", "reject")

DECISION_MEANINGS: dict[str, str] = {
    "proceed": (
        "The research artifact is eligible to enter a future mutation-generation stage. "
        "It creates, approves and deploys nothing."
    ),
    "human_review": "A person must look at the research artifact before anything else happens.",
    "reject": "The research artifact should not be used further.",
}

Confidence = Literal["low", "medium", "high"]
CONFIDENCE_LEVELS: tuple[Confidence, ...] = ("low", "medium", "high")

# code -> meaning. Deciders may use the first ten; the last two are added only by DarwinUX's
# fail-closed policy, never accepted from a decider.
REASON_CODES: dict[str, str] = {
    "critique_accepted": "The critique accepted the hypothesis.",
    "sufficient_evidence": "The research found enough evidence and reported no gaps.",
    "human_approved": "A person approved the research outcome.",
    "human_rejected": "A person rejected the research outcome.",
    "critique_rejected": "The critique rejected the hypothesis.",
    "unsupported_claim": "The critique lists at least one unsupported claim.",
    "missing_evidence": "The critique lists evidence that is missing.",
    "critique_issues": "The critique lists issues with the hypothesis.",
    "low_confidence": "The hypothesis (or the decider) has low confidence.",
    "human_judgment_required": "The decider wants a person to decide.",
    "decider_failure": "The decider failed or returned invalid output (added by policy).",
    "policy_override": "A proceed was downgraded because a precondition failed (added by policy).",
}
POLICY_ONLY_REASON_CODES = frozenset({"decider_failure", "policy_override"})
DECIDER_REASON_CODES: tuple[str, ...] = tuple(
    code for code in REASON_CODES if code not in POLICY_ONLY_REASON_CODES
)
MAX_REASON_CODES = 4


class DecisionOutput(BaseModel):
    """What every decider must produce. Validated by DarwinUX, never trusted as-is."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    decision: Decision
    # Qualitative and uncalibrated for every decider (see docs/AGENT_ARCHITECTURE.md).
    confidence: Confidence
    reason_codes: list[str] = Field(min_length=1, max_length=MAX_REASON_CODES)
    # A provider's own 0-1 number, if it has one (Jev: derived from its distribution).
    # Recorded, never treated as a calibrated probability.
    provider_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("reason_codes")
    @classmethod
    def _allowlisted(cls, codes: list[str]) -> list[str]:
        if len(set(codes)) != len(codes):
            raise PydanticCustomError("duplicate_reason_code", "reason codes must be unique")
        if any(code not in DECIDER_REASON_CODES for code in codes):
            raise PydanticCustomError("unknown_reason_code", "reason code is not allowlisted")
        return codes
