"""The hypothesis output contract and every deterministic check applied to it.

Three layers, in order — output that fails one never reaches the next, and
only output that passes all three becomes a Hypothesis:

1. parse: the output is one JSON object (not prose, not markdown);
2. schema (`HypothesisDraft`): exact fields, no extras, strict types, bounded
   lengths, a qualitative confidence enum, no code or markup;
3. grounding: every cited excerpt id was in the EvidenceBundle, and the
   affected component is one the bundle allowed.

Errors are recorded as (location, type) pairs only — never the offending
value, which is model output and could be anything.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from darwin.memory.embeddings import features
from darwin.signals.detectors import COMPONENT_PATTERN

from .evidence import EvidenceBundle

Confidence = Literal["low", "medium", "high"]

MAX_EVIDENCE_REFERENCES = 5
_CODE_OR_MARKUP = re.compile(r"```|<\s*/?\s*[A-Za-z][^>]*>")


def _plain_text(value: str) -> str:
    if _CODE_OR_MARKUP.search(value):
        raise PydanticCustomError("code_or_markup", "code or markup is not allowed")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise PydanticCustomError("control_character", "control characters are not allowed")
    return value


class HypothesisDraft(BaseModel):
    """What the model must return. Describes a problem; proposes no change."""

    # Whitespace is stripped before length checks, so "   " is an empty rationale.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)

    # One sentence: the UX problem that may explain the signal.
    statement: str = Field(min_length=20, max_length=400)
    # Why the cited evidence supports that interpretation.
    rationale: str = Field(min_length=40, max_length=1500)
    # A component id from the bundle's allowed list, or null if none is identified.
    affected_component: str | None = Field(default=..., max_length=128)
    # Qualitative and uncalibrated: the model's judgment, not a probability.
    confidence: Confidence
    # Excerpt ids from the EvidenceBundle that support the hypothesis.
    evidence_chunk_ids: list[str] = Field(min_length=1, max_length=MAX_EVIDENCE_REFERENCES)
    # What remains uncertain.
    limitations: list[str] = Field(min_length=1, max_length=5)

    @field_validator("statement")
    @classmethod
    def _one_sentence(cls, value: str) -> str:
        if "\n" in value:
            raise PydanticCustomError("multiline", "statement must be a single line")
        return _plain_text(value)

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        return _plain_text(value)

    @field_validator("affected_component")
    @classmethod
    def _component(cls, value: str | None) -> str | None:
        if value is not None and not COMPONENT_PATTERN.fullmatch(value):
            raise PydanticCustomError("invalid_identifier", "not a component identifier")
        return value

    @field_validator("evidence_chunk_ids")
    @classmethod
    def _references(cls, value: list[str]) -> list[str]:
        if any(not 1 <= len(v) <= 64 for v in value):
            raise PydanticCustomError("invalid_reference", "reference length out of range")
        if len(set(value)) != len(value):
            raise PydanticCustomError("duplicate_reference", "evidence ids must be unique")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: list[str]) -> list[str]:
        for item in value:
            if not 5 <= len(item) <= 300:
                raise PydanticCustomError("limitation_length", "limitation length out of range")
            _plain_text(item)
        return value


def output_schema(allowed_components: tuple[str, ...]) -> dict[str, Any]:
    """JSON Schema for the request. The component enum is the bundle's allowlist."""
    schema = HypothesisDraft.model_json_schema()
    schema["properties"]["affected_component"] = {
        "anyOf": [{"type": "string", "enum": list(allowed_components)}, {"type": "null"}],
        "description": "One allowed component id, or null.",
    }
    return schema


OutputStatus = Literal["valid", "invalid_output", "grounding_failed"]


@dataclass(frozen=True)
class OutputCheck:
    status: OutputStatus
    draft: HypothesisDraft | None
    error_type: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def _unparseable(kind: str) -> OutputCheck:
    return OutputCheck("invalid_output", None, kind, [{"loc": "", "type": kind}])


def check_output(output_text: str, bundle: EvidenceBundle) -> OutputCheck:
    """Parse -> schema -> grounding. Pure and deterministic; no model involved."""
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, RecursionError):
        return _unparseable("not_json")
    if not isinstance(parsed, dict):
        return _unparseable("not_object")

    try:
        draft = HypothesisDraft.model_validate(parsed)
    except ValidationError as error:
        errors = [
            {"loc": ".".join(str(part) for part in e["loc"]), "type": e["type"]}
            for e in error.errors(include_input=False, include_url=False)
        ]
        return OutputCheck("invalid_output", None, errors[0]["type"], errors)

    grounding = grounding_errors(draft, bundle)
    if grounding:
        return OutputCheck("grounding_failed", draft, grounding[0]["type"], grounding)
    return OutputCheck("valid", draft)


def grounding_errors(draft: HypothesisDraft, bundle: EvidenceBundle) -> list[dict[str, str]]:
    """Deterministic citation validity. Says nothing about whether the claims are true."""
    supplied = set(bundle.chunk_ids)
    errors = [
        {"loc": f"evidence_chunk_ids.{i}", "type": "unknown_evidence_reference"}
        for i, ref in enumerate(draft.evidence_chunk_ids)
        if ref not in supplied
    ]
    component = draft.affected_component
    if component is not None and component not in bundle.allowed_components:
        errors.append({"loc": "affected_component", "type": "invalid_component"})
    return errors


def _content_words(text: str) -> set[str]:
    return {f for f in features(text) if "_" not in f}  # unigrams only (bigrams contain "_")


def lexical_support(draft: HypothesisDraft, bundle: EvidenceBundle) -> float:
    """BASELINE ONLY: share of the hypothesis's content words found in its cited evidence.

    Counts shared words between statement+rationale and (cited excerpts + signal
    facts). It is NOT faithfulness: copying words while contradicting them scores
    high, and a correct paraphrase scores low. It exists to catch hypotheses that
    have nothing to do with their citations, and to be replaced by a judged metric.
    """
    claimed = _content_words(f"{draft.statement} {draft.rationale}")
    if not claimed:
        return 0.0
    cited = set(draft.evidence_chunk_ids)
    support_text = " ".join(e.text for e in bundle.excerpts if str(e.chunk_id) in cited)
    facts = bundle.signal
    support_text += f" {facts.signal_type} " + " ".join(f"{k} {v}" for k, v in facts.facts.items())
    supported = claimed & _content_words(support_text)
    return round(len(supported) / len(claimed), 4)
