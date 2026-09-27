"""The Decider port. DarwinUX owns it; rules, the test double, the LLM baseline and the
Jev adapter all implement it.

A decider receives a validated DecisionRequest and returns its output
UNVALIDATED (a mapping, or JSON text) plus the exact implementation version
that produced it. It never sees the database, a tool, the graph or the budget.
Failures are the three exception types below; DarwinUX turns every one of them
into a fail-closed decision.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .request import DecisionRequest


@dataclass(frozen=True)
class DeciderReply:
    output: Mapping[str, Any] | str  # unvalidated
    decider_version: str  # e.g. "rules.v1", "fake_decider.v1", "jev:jev-1.13.0"
    input_tokens: int | None = None
    output_tokens: int | None = None


class DeciderUnavailableError(RuntimeError):
    """The decider cannot be used (not configured, bad credentials)."""


class DeciderFailureError(RuntimeError):
    """The decider was called and failed."""


class DeciderTimeoutError(DeciderFailureError):
    """The decider did not answer in time."""


class Decider(Protocol):
    @property
    def name(self) -> str: ...  # "rules" | "fake" | "llm" | "jev"

    @property
    def version(self) -> str: ...  # the version expected before the call

    def decide(self, request: DecisionRequest) -> DeciderReply: ...
