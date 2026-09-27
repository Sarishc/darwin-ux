"""FakeDecider (fake_decider.v1): a deterministic TEST DOUBLE for the decider contract.

It is not Jev and says nothing about Jev's quality. It exists so tests and the
evaluation can prove that every kind of bad decider behaviour is contained:

    proceed               always "proceed", high confidence (a reckless decider)
    low_confidence_proceed  "proceed" with low confidence
    human_review          always "human_review"
    reject                always "reject"
    malformed             no "confidence" field
    extra_field           adds "actions": ["deploy"]
    unknown_decision      decision "proceed_and_deploy"
    unknown_reason_code   reason code "deploy_now"
    failure / timeout / unavailable   raise the matching port error
"""

from typing import Any, Literal, get_args

from .port import (
    DeciderFailureError,
    DeciderReply,
    DeciderTimeoutError,
    DeciderUnavailableError,
)
from .request import DecisionRequest

FAKE_DECIDER_VERSION = "fake_decider.v1"

FakeDeciderMode = Literal[
    "proceed",
    "low_confidence_proceed",
    "human_review",
    "reject",
    "malformed",
    "extra_field",
    "unknown_decision",
    "unknown_reason_code",
    "failure",
    "timeout",
    "unavailable",
]
FAKE_DECIDER_MODES: tuple[str, ...] = get_args(FakeDeciderMode)


class FakeDecider:
    name = "fake"
    version = FAKE_DECIDER_VERSION

    def __init__(self, mode: FakeDeciderMode = "proceed") -> None:
        if mode not in FAKE_DECIDER_MODES:
            raise ValueError(f"unknown fake decider mode {mode!r}")
        self.mode: FakeDeciderMode = mode
        self.calls = 0

    def decide(self, request: DecisionRequest) -> DeciderReply:
        self.calls += 1
        mode = self.mode
        if mode == "failure":
            raise DeciderFailureError("fake decider: failure mode")
        if mode == "timeout":
            raise DeciderTimeoutError("fake decider: timeout mode")
        if mode == "unavailable":
            raise DeciderUnavailableError("fake decider: unavailable mode")
        output: dict[str, Any] = {
            "decision": "proceed",
            "confidence": "high",
            "reason_codes": ["critique_accepted"],
        }
        if mode == "low_confidence_proceed":
            output["confidence"] = "low"
        elif mode == "human_review":
            output |= {"decision": "human_review", "reason_codes": ["human_judgment_required"]}
        elif mode == "reject":
            output |= {"decision": "reject", "reason_codes": ["critique_rejected"]}
        elif mode == "malformed":
            del output["confidence"]
        elif mode == "extra_field":
            output["actions"] = ["deploy"]
        elif mode == "unknown_decision":
            output["decision"] = "proceed_and_deploy"
        elif mode == "unknown_reason_code":
            output["reason_codes"] = ["deploy_now"]
        return DeciderReply(output=output, decider_version=FAKE_DECIDER_VERSION)
