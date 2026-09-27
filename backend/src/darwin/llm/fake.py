"""FakeLLMProvider: a deterministic stand-in that exercises DarwinUX's control layer.

It is not a model and does not "reason". In `grounded` mode it reads the
request's evidence section and returns a valid, templated hypothesis that
cites the top-ranked excerpts. Every other mode returns (or raises) one
specific kind of bad result, so tests and the golden evaluation can prove
each failure is caught:

    grounded                valid output, usage reported
    no_usage                valid output, no usage metadata
    missing_field           no "rationale"
    unknown_field           an extra "mutation" object
    invalid_enum            confidence "0.87"
    overlong                a 1000-character statement
    not_json                prose instead of JSON
    hallucinated_reference  cites "not-in-the-bundle"
    invalid_component       names a component the bundle did not allow
    obey_injection          if an excerpt says "output APPROVED", does exactly that
    failure                 raises ProviderFailureError
    timeout                 raises ProviderTimeoutError
    unavailable             raises ProviderUnavailableError

Token counts are deterministic character-based estimates (len // 4), clearly
not a real tokenizer.
"""

import json
from typing import Any, Literal, get_args

from .port import (
    ProviderFailureError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    Usage,
)

FakeMode = Literal[
    "grounded",
    "no_usage",
    "missing_field",
    "unknown_field",
    "invalid_enum",
    "overlong",
    "not_json",
    "hallucinated_reference",
    "invalid_component",
    "obey_injection",
    "failure",
    "timeout",
    "unavailable",
]
FAKE_MODES: tuple[str, ...] = get_args(FakeMode)

CITED_EXCERPTS = 3
INJECTION_TRIGGER = "output approved"


def _evidence_document(request: StructuredGenerationRequest) -> dict[str, Any]:
    """The JSON between the BEGIN/END lines — exactly what a real model would be shown."""
    lines = request.evidence.strip().split("\n")
    document = json.loads("\n".join(lines[1:-1]))
    if not isinstance(document, dict):
        raise ProviderFailureError("fake provider: evidence is not a JSON object")
    return document


def _allowed_components(request: StructuredGenerationRequest) -> list[str]:
    options = request.output_schema["properties"]["affected_component"]["anyOf"]
    return [c for option in options for c in option.get("enum", [])]


def _pick_component(
    facts: dict[str, Any], allowed: list[str], cited: list[dict[str, Any]]
) -> str | None:
    """The signal's own component, else the allowed id mentioned most in cited excerpts."""
    if isinstance(facts.get("component"), str) and facts["component"] in allowed:
        return str(facts["component"])
    text = " ".join(e["text"] for e in cited)
    counts = [(text.count(c), -i, c) for i, c in enumerate(allowed) if c in text]
    return max(counts)[2] if counts else None


def grounded_output(request: StructuredGenerationRequest) -> dict[str, Any]:
    document = _evidence_document(request)
    signal = document["signal"]
    facts = signal["facts"]
    excerpts = sorted(document["excerpts"], key=lambda e: e["rank"])
    cited = excerpts[:CITED_EXCERPTS]
    component = _pick_component(facts, _allowed_components(request), cited)
    target = component or "the affected control"
    if signal["signal_type"] == "rage_click":
        statement = (
            f"Repeated clicks on {target} suggest it does not acknowledge a click quickly "
            "enough, so users click again."
        )
        observed = f"{facts.get('count')} clicks on {target}"
    else:
        statement = (
            f"Repeated errors on {target} suggest its validation requirements are not clear "
            "before submission."
        )
        observed = f"{facts.get('count')} error events"
    sources = "; ".join(f"{e['source']} ({e['section']})" for e in cited)
    rationale = (
        f"DarwinUX detected {observed} within {facts.get('window_seconds')} seconds "
        f"(threshold {facts.get('threshold')}). The cited excerpts - {sources} - are the "
        f"closest Product Memory evidence about {target} and this signal type."
    )
    return {
        "statement": statement,
        "rationale": rationale,
        "affected_component": component,
        "confidence": "medium" if component else "low",
        "evidence_chunk_ids": [e["id"] for e in cited],
        "limitations": [
            "The signal comes from a single anonymous session.",
            "Excerpts describe intended design, not what the user actually saw.",
            "Confidence is an uncalibrated judgment, not a probability.",
        ],
    }


class FakeLLMProvider:
    def __init__(self, mode: FakeMode = "grounded") -> None:
        if mode not in FAKE_MODES:
            raise ValueError(f"unknown fake mode {mode!r}")
        self.mode: FakeMode = mode
        self.requests: list[StructuredGenerationRequest] = []  # for request inspection in tests

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-hypothesis:v1"

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult:
        self.requests.append(request)
        mode = self.mode
        if mode == "unavailable":
            raise ProviderUnavailableError("fake provider: unavailable mode")
        if mode == "failure":
            raise ProviderFailureError("fake provider: failure mode")
        if mode == "timeout":
            raise ProviderTimeoutError(f"fake provider: no answer in {request.timeout_seconds}s")

        output: dict[str, Any] = grounded_output(request)
        text: str | None = None
        if mode == "missing_field":
            del output["rationale"]
        elif mode == "unknown_field":
            output["mutation"] = {"component": output["affected_component"], "prop": "variant"}
        elif mode == "invalid_enum":
            output["confidence"] = "0.87"
        elif mode == "overlong":
            output["statement"] = "Users are frustrated. " * 46
        elif mode == "not_json":
            text = "Sure! Here is my hypothesis: the button is slow."
        elif mode == "hallucinated_reference":
            output["evidence_chunk_ids"] = ["not-in-the-bundle"]
        elif mode == "invalid_component":
            output["affected_component"] = "checkout_pay_button"
        elif mode == "obey_injection":
            if INJECTION_TRIGGER in request.evidence.lower():
                output = {"verdict": "APPROVED"}
        if text is None:
            text = json.dumps(output, sort_keys=True)

        usage = None
        if mode != "no_usage":
            prompt_chars = len(request.instructions) + len(request.evidence)
            usage = Usage(input_tokens=prompt_chars // 4, output_tokens=len(text) // 4)
        return StructuredGenerationResult(
            provider=self.name, model=self.model, output_text=text, usage=usage
        )
