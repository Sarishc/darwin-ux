"""MutationSpec (version 1) and every deterministic check applied to a generator's output.

    {"version": 1, "source_spec_id": "<uuid>", "summary": "<= 200 chars, informational",
     "operations": [{"op": "replace", "component_id": "...", "property": "...", "value": ...}]}

One operation: replace. A target is SEMANTIC (component id + property), never
a JSON Pointer, so there is no path to point anywhere else. Checks, in order
(the first failure decides the status; nothing is applied unless all pass):

  parse (one JSON object) .................................. invalid_output
  strict schema (no unknown fields, 1-5 ops, str|bool values) invalid_output
  source_spec_id == the request's source ................... validation_failed
  no duplicate (component, property) targets ............... validation_failed
  component exists / property mutable for its type / value
  allowed (closed enum, boolean, bounded plain text) / value
  actually changes ......................................... validation_failed
  apply in memory; the generic diff shows exactly the
  targeted leaves (+ generation) changed ................... validation_failed
  candidate shape (unique ids, size bound) ................. validation_failed

The summary is informational only: bounded, plain text, never used for any
decision.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from darwin.hypotheses.schema import error_list, parse_json_object, plain_text

from .apply import (
    MAX_SPEC_BYTES,
    Change,
    apply_mutation,
    canonical_json,
    diff_paths,
    expected_paths,
)
from .request import MAX_OPERATIONS, MutationRequest
from .specs import SpecShapeError, check_shape
from .surface import MUTABLE, SurfaceValueError, check_value, index_targets

PROTECTED_PROPERTIES = frozenset(
    {
        "id",
        "type",
        "action",
        "level",
        "visibility",
        "version",
        "generation",
        "cta",
        "plans",
        "components",
        "sections",
        "fields",
        "features",
        "price_label",
        "name",
        "completion_text",
    }
)


class MutationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    op: Literal["replace"]
    component_id: str = Field(min_length=1, max_length=64)
    property: str = Field(min_length=1, max_length=64)
    value: str | bool


class MutationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)

    version: Literal[1]
    source_spec_id: str = Field(min_length=36, max_length=36)
    summary: str = Field(min_length=10, max_length=200)
    operations: list[MutationOperation] = Field(min_length=1, max_length=MAX_OPERATIONS)

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        if "\n" in value:
            raise PydanticCustomError("multiline", "summary must be a single line")
        return plain_text(value)


Status = Literal["valid", "invalid_output", "validation_failed"]


@dataclass(frozen=True)
class MutationCheck:
    status: Status
    spec: MutationSpec | None = None
    changes: tuple[Change, ...] = ()
    candidate: dict[str, Any] | None = None
    error_type: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def _failed(spec: MutationSpec, code: str, loc: str = "") -> MutationCheck:
    return MutationCheck(
        "validation_failed", spec, error_type=code, errors=[{"loc": loc, "type": code}]
    )


def check_mutation(output: Any, request: MutationRequest, source: dict[str, Any]) -> MutationCheck:
    if isinstance(output, str):
        parsed, unparseable = parse_json_object(output)
        if parsed is None:
            code = unparseable or "not_json"
            return MutationCheck(
                "invalid_output", error_type=code, errors=[{"loc": "", "type": code}]
            )
        output = parsed
    if not isinstance(output, dict):
        return MutationCheck(
            "invalid_output", error_type="not_object", errors=[{"loc": "", "type": "not_object"}]
        )
    try:
        spec = MutationSpec.model_validate(output)
    except ValidationError as error:
        errors = error_list(error)
        return MutationCheck("invalid_output", error_type=errors[0]["type"], errors=errors)

    if spec.source_spec_id != request.source_spec.spec_id:
        return _failed(spec, "source_spec_mismatch", "source_spec_id")
    targets = [(o.component_id, o.property) for o in spec.operations]
    if len(set(targets)) != len(targets):
        return _failed(spec, "duplicate_target", "operations")

    index = index_targets(source)
    changes = []
    for i, op in enumerate(spec.operations):
        loc = f"operations.{i}"
        target = index.get(op.component_id)
        if target is None:
            return _failed(spec, "unknown_component", loc)
        rules = MUTABLE.get(target.kind, {})
        if op.property not in rules:
            code = (
                "protected_property"
                if op.property in PROTECTED_PROPERTIES
                else "property_not_mutable"
            )
            return _failed(spec, code, loc)
        try:
            value = check_value(rules[op.property], op.value)
        except SurfaceValueError as error:
            return _failed(spec, error.code, loc)
        if target.node.get(op.property) == value:
            return _failed(spec, "value_unchanged", loc)
        changes.append(Change(op.component_id, op.property, value))

    candidate = apply_mutation(source, changes, request.source_spec.generation + 1)
    if set(diff_paths(source, candidate)) != expected_paths(source, changes):
        return _failed(spec, "protected_field_changed")
    if len(canonical_json(candidate).encode()) > MAX_SPEC_BYTES:
        return _failed(spec, "spec_too_large")
    try:
        check_shape(candidate)
    except SpecShapeError:
        return _failed(spec, "candidate_shape_invalid")
    return MutationCheck("valid", spec, tuple(changes), candidate)
