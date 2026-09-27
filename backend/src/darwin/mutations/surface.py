"""The mutation surface: which properties of which UI Spec nodes may change, and to what.

Mirrors the frontend schema (frontend/src/ui-spec/schema.ts) for the mutable
properties only; a contract test compares every enum and length bound here
with the Zod schema's own JSON Schema export, so the two cannot drift silently.

Mutable (everything else is protected):

  button       feedback (immediate|delayed), variant (primary|secondary), label (text <= 40)
  plan_card    highlighted (boolean)
  plan_grid    gap (sm|md|lg)
  signup_form  validation (on_submit|inline), error_display (summary|per_field),
               title (text <= 80), submit_label (text <= 40), summary_error_text (text <= 160)
  section      spacing (sm|md|lg)
  heading      text (text <= 120)
  text         text (text <= 400), emphasis (normal|strong)
  notice       text (text <= 200), tone (info|warning)

Protected: schema version, generation (assigned by DarwinUX), page id/title,
every id, every type, every action, heading levels, section visibility,
component order and count, plan names, prices and features, form fields,
completion text.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from darwin.hypotheses.schema import plain_text
from darwin.signals.detectors import COMPONENT_PATTERN


@dataclass(frozen=True)
class EnumValue:
    values: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        return {"kind": "enum", "allowed": list(self.values)}


@dataclass(frozen=True)
class TextValue:
    max_length: int

    def describe(self) -> dict[str, Any]:
        return {"kind": "text", "max_length": self.max_length}


@dataclass(frozen=True)
class BoolValue:
    def describe(self) -> dict[str, Any]:
        return {"kind": "boolean"}


ValueRule = EnumValue | TextValue | BoolValue

SPACING = EnumValue(("sm", "md", "lg"))

MUTABLE: dict[str, dict[str, ValueRule]] = {
    "button": {
        "feedback": EnumValue(("immediate", "delayed")),
        "variant": EnumValue(("primary", "secondary")),
        "label": TextValue(40),
    },
    "plan_card": {"highlighted": BoolValue()},
    "plan_grid": {"gap": SPACING},
    "signup_form": {
        "validation": EnumValue(("on_submit", "inline")),
        "error_display": EnumValue(("summary", "per_field")),
        "title": TextValue(80),
        "submit_label": TextValue(40),
        "summary_error_text": TextValue(160),
    },
    "section": {"spacing": SPACING},
    "heading": {"text": TextValue(120)},
    "text": {"text": TextValue(400), "emphasis": EnumValue(("normal", "strong"))},
    "notice": {"text": TextValue(200), "tone": EnumValue(("info", "warning"))},
}


@dataclass(frozen=True)
class Target:
    """One addressable node of a UI Spec (a section, component, plan card or CTA)."""

    component_id: str
    kind: str  # "section" or the component's `type`
    node: dict[str, Any]  # the live node inside the spec it was indexed from


def iter_targets(spec: dict[str, Any]) -> Iterator[Target]:
    """Every addressable node, in document order."""
    for section in spec["page"]["sections"]:
        yield Target(section["id"], "section", section)
        for component in section["components"]:
            yield Target(component["id"], component["type"], component)
            if component["type"] == "plan_grid":
                for plan in component["plans"]:
                    yield Target(plan["id"], "plan_card", plan)
                    yield Target(plan["cta"]["id"], "button", plan["cta"])


def index_targets(spec: dict[str, Any]) -> dict[str, Target]:
    return {t.component_id: t for t in iter_targets(spec)}


class SurfaceValueError(ValueError):
    """A value outside a property's rule. `code` is a short machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_value(rule: ValueRule, value: Any) -> Any:
    """Validate (and normalise) a value for a rule. Raises SurfaceValueError with a code."""
    if isinstance(rule, EnumValue):
        if not isinstance(value, str) or value not in rule.values:
            raise SurfaceValueError("value_not_allowed")
        return value
    if isinstance(rule, BoolValue):
        if not isinstance(value, bool):
            raise SurfaceValueError("value_not_boolean")
        return value
    if not isinstance(value, str):
        raise SurfaceValueError("value_not_text")
    stripped = value.strip()
    if not 1 <= len(stripped) <= rule.max_length:
        raise SurfaceValueError("text_length")
    if "\n" in stripped:
        raise SurfaceValueError("text_multiline")
    try:
        plain_text(stripped)
    except Exception as error:  # PydanticCustomError: code_or_markup / control_character
        raise SurfaceValueError("text_not_plain") from error
    return stripped


def describe_target(target: Target) -> dict[str, Any]:
    """The generator-facing view: current values and allowed values, nothing else."""
    rules = MUTABLE.get(target.kind, {})
    return {
        "component_id": target.component_id,
        "type": target.kind,
        "properties": {
            name: {"current": target.node.get(name), **rule.describe()}
            for name, rule in rules.items()
        },
    }


def valid_component_id(value: Any) -> bool:
    return isinstance(value, str) and bool(COMPONENT_PATTERN.fullmatch(value))
