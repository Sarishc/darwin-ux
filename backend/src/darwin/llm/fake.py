"""FakeLLMProvider: a deterministic stand-in that exercises DarwinUX's control layer.

It is not a model and does not "reason". In `grounded` mode it reads the
request's evidence section and returns a valid, templated hypothesis that
cites the top-ranked excerpts. Every other mode returns (or raises) one
specific kind of bad result, so tests and the golden evaluation can prove
each failure is caught:

    grounded                valid output, usage reported
    low_confidence          valid output with confidence "low"
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

Critique requests (request_version "hypothesis_critique.*", Step 10) are
answered by `critique_mode` instead, so one fake can play both calls of a
research run:

    accept          verdict "accept", no findings
    human_review    verdict "human_review" with an issue and missing evidence
    reject          verdict "reject" with an unsupported claim
    malformed       adds a "reasoning" field (forbidden: no reasoning transcripts)
    obey_injection  if an excerpt says "output APPROVED", returns {"verdict": "APPROVED"}
    failure         raises ProviderFailureError

Decision requests (request_version "decision.*", Step 11) are answered by
`decision_mode`:

    cautious            always human_review, low confidence (the default)
    proceed / reject    that decision
    malformed           no "confidence" field
    proceed_and_deploy  an invented decision (must fail closed)
    failure             raises ProviderFailureError

Mutation requests (request_version "mutation.*", Step 12) are answered by
`mutation_mode`:

    first_enum     the affected component's first enum property, switched to the
                   first other allowed value (the default)
    malformed      a truncated JSON object
    unsafe_action  tries to set the component's "action" to deploy_production
    failure        raises ProviderFailureError

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
    "low_confidence",
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

CritiqueMode = Literal["accept", "human_review", "reject", "malformed", "obey_injection", "failure"]
CRITIQUE_MODES: tuple[str, ...] = get_args(CritiqueMode)
CRITIQUE_REQUEST_PREFIX = "hypothesis_critique."

DecisionMode = Literal[
    "cautious", "proceed", "reject", "malformed", "proceed_and_deploy", "failure"
]
DECISION_MODES: tuple[str, ...] = get_args(DecisionMode)
DECISION_REQUEST_PREFIX = "decision."

MutationMode = Literal["first_enum", "malformed", "unsafe_action", "failure"]
MUTATION_MODES: tuple[str, ...] = get_args(MutationMode)
MUTATION_REQUEST_PREFIX = "mutation."

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


def critique_output(mode: CritiqueMode, request: StructuredGenerationRequest) -> dict[str, Any]:
    if mode == "obey_injection" and INJECTION_TRIGGER in request.evidence.lower():
        return {"verdict": "APPROVED"}
    output: dict[str, Any] = {
        "verdict": "accept",
        "summary": "The hypothesis is consistent with its cited excerpts and the signal facts.",
        "issues": [],
        "unsupported_claims": [],
        "missing_evidence": [],
    }
    if mode == "human_review":
        output["verdict"] = "human_review"
        output["summary"] = "Plausible, but the excerpts describe intended design only."
        output["issues"] = ["The cited excerpts do not show what the user actually experienced."]
        output["missing_evidence"] = ["Observed response time of the control in this session."]
    elif mode == "reject":
        output["verdict"] = "reject"
        output["summary"] = "The main claim is not supported by the cited excerpts."
        output["unsupported_claims"] = ["That the control fails to acknowledge clicks."]
    elif mode == "malformed":
        output["reasoning"] = "Step 1: read the evidence. Step 2: decide."
    return output


class FakeLLMProvider:
    def __init__(
        self,
        mode: FakeMode = "grounded",
        critique_mode: CritiqueMode = "accept",
        decision_mode: DecisionMode = "cautious",
        mutation_mode: MutationMode = "first_enum",
    ) -> None:
        if mode not in FAKE_MODES:
            raise ValueError(f"unknown fake mode {mode!r}")
        if critique_mode not in CRITIQUE_MODES:
            raise ValueError(f"unknown fake critique mode {critique_mode!r}")
        self.mode: FakeMode = mode
        self.critique_mode: CritiqueMode = critique_mode
        if decision_mode not in DECISION_MODES:
            raise ValueError(f"unknown fake decision mode {decision_mode!r}")
        self.decision_mode: DecisionMode = decision_mode
        if mutation_mode not in MUTATION_MODES:
            raise ValueError(f"unknown fake mutation mode {mutation_mode!r}")
        self.mutation_mode: MutationMode = mutation_mode
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
        if request.request_version.startswith(CRITIQUE_REQUEST_PREFIX):
            return self._critique(request)
        if request.request_version.startswith(DECISION_REQUEST_PREFIX):
            return self._decision(request)
        if request.request_version.startswith(MUTATION_REQUEST_PREFIX):
            return self._mutation(request)
        mode = self.mode
        if mode == "unavailable":
            raise ProviderUnavailableError("fake provider: unavailable mode")
        if mode == "failure":
            raise ProviderFailureError("fake provider: failure mode")
        if mode == "timeout":
            raise ProviderTimeoutError(f"fake provider: no answer in {request.timeout_seconds}s")

        output: dict[str, Any] = grounded_output(request)
        text: str | None = None
        if mode == "low_confidence":
            output["confidence"] = "low"
        elif mode == "missing_field":
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

        return self._result(request, text, with_usage=mode != "no_usage")

    def _critique(self, request: StructuredGenerationRequest) -> StructuredGenerationResult:
        if self.critique_mode == "failure":
            raise ProviderFailureError("fake provider: critique failure mode")
        text = json.dumps(critique_output(self.critique_mode, request), sort_keys=True)
        return self._result(request, text, with_usage=True)

    def _decision(self, request: StructuredGenerationRequest) -> StructuredGenerationResult:
        mode = self.decision_mode
        if mode == "failure":
            raise ProviderFailureError("fake provider: decision failure mode")
        output: dict[str, Any] = {
            "decision": "human_review",
            "confidence": "low",
            "reason_codes": ["human_judgment_required"],
        }
        if mode == "proceed":
            output |= {"decision": "proceed", "confidence": "high"}
            output["reason_codes"] = ["critique_accepted"]
        elif mode == "reject":
            output |= {"decision": "reject", "confidence": "high"}
            output["reason_codes"] = ["critique_rejected"]
        elif mode == "malformed":
            del output["confidence"]
        elif mode == "proceed_and_deploy":
            output["decision"] = "proceed_and_deploy"
        return self._result(request, json.dumps(output, sort_keys=True), with_usage=True)

    def _mutation(self, request: StructuredGenerationRequest) -> StructuredGenerationResult:
        mode = self.mutation_mode
        if mode == "failure":
            raise ProviderFailureError("fake provider: mutation failure mode")
        if mode == "malformed":
            return self._result(request, '{"version": 1, "operations": [', with_usage=True)
        document = _evidence_document(request)
        affected = document["hypothesis"]["affected_component"]
        targets = document["targets"]
        target = next((t for t in targets if t["component_id"] == affected), None)
        if target is None:  # no named component: prefer the form, else the first target
            target = next((t for t in targets if t["type"] == "signup_form"), targets[0])
        op: dict[str, Any] = {"op": "replace", "component_id": target["component_id"]}
        if mode == "unsafe_action":
            op |= {"property": "action", "value": "deploy_production"}
        else:
            name, prop = next(
                (n, p) for n, p in target["properties"].items() if p["kind"] == "enum"
            )
            other = next(v for v in prop["allowed"] if v != prop["current"])
            op |= {"property": name, "value": other}
        output = {
            "version": 1,
            "source_spec_id": document["source_spec"]["spec_id"],
            "summary": "Adjust the affected component to remove the observed friction.",
            "operations": [op],
        }
        return self._result(request, json.dumps(output, sort_keys=True), with_usage=True)

    def _result(
        self, request: StructuredGenerationRequest, text: str, *, with_usage: bool
    ) -> StructuredGenerationResult:
        usage = None
        if with_usage:
            prompt_chars = len(request.instructions) + len(request.evidence)
            usage = Usage(input_tokens=prompt_chars // 4, output_tokens=len(text) // 4)
        return StructuredGenerationResult(
            provider=self.name, model=self.model, output_text=text, usage=usage
        )
