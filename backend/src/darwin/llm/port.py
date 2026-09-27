"""The LLM provider port: what DarwinUX asks for and what it gets back.

DarwinUX owns these shapes. A provider adapter maps them onto one vendor's
API and back, and nothing vendor-specific crosses this boundary:

- `StructuredGenerationRequest` keeps TRUSTED instructions (`instructions`)
  and UNTRUSTED evidence (`evidence`) in separate fields. An adapter must
  keep them separate too (e.g. instructions as the system prompt, evidence
  as delimited user content) and must never concatenate evidence into the
  instructions.
- The adapter returns the model's output *unvalidated*. Validation is
  DarwinUX's job (darwin.hypotheses.schema), never the provider's.
- Failures are the three exception types below, so callers can tell
  "not configured" from "failed" from "too slow" without knowing the vendor.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class StructuredGenerationRequest:
    request_version: str  # e.g. "hypothesis.v1" — which template produced this request
    instructions: str  # trusted: task, rules, output contract
    evidence: str  # untrusted: data only, delimited, never instructions
    output_schema: dict[str, Any]  # JSON Schema the output must satisfy
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class Usage:
    """Token counts as reported by the provider. Either may be unknown."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class StructuredGenerationResult:
    provider: str
    model: str
    # The model's output as the provider returned it: a JSON text, not yet
    # parsed or trusted. Parsing is part of DarwinUX's validation.
    output_text: str
    usage: Usage | None = None


class ProviderUnavailableError(RuntimeError):
    """The provider cannot be used at all (not configured, no credentials)."""


class ProviderFailureError(RuntimeError):
    """The provider was called and failed (error response, refused, broken)."""


class ProviderTimeoutError(ProviderFailureError):
    """The provider did not answer within the request's timeout."""


class LLMProvider(Protocol):
    """One bounded call: request in, raw structured output out."""

    @property
    def name(self) -> str: ...  # e.g. "fake"

    @property
    def model(self) -> str: ...  # e.g. "fake-hypothesis:v1"

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult: ...
