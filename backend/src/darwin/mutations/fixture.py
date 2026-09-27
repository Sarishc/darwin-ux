"""FixtureMutationGenerator (fixture_mutation.v1): deterministic, realistic, and NOT Muse.

`auto` maps the Generation 0 friction to its designed fix:
  rage_click on a button whose feedback is "delayed" -> feedback: immediate
  error_burst (signup_form)  -> validation: inline + error_display: per_field
Other modes produce one specific safe or unsafe output, so tests and the
evaluation can prove what the validation layers accept and reject:

  inline_only / per_field_only / text_only / multi       safe variants
  unknown_component / immutable_property / invalid_value / duplicate_target /
  too_many_operations / change_action / change_type / executable / html_text /
  arbitrary_path / malformed / wrong_source / no_op / echo_injection   unsafe
  failure / timeout / unavailable                        raise port errors

It exists for offline tests and deterministic evaluation; it says nothing
about Muse's quality.
"""

from typing import Any, Literal, get_args

from .port import (
    GeneratorFailureError,
    GeneratorReply,
    GeneratorTimeoutError,
    GeneratorUnavailableError,
)
from .request import MutationRequest

FIXTURE_VERSION = "fixture_mutation.v1"

FixtureMode = Literal[
    "auto",
    "inline_only",
    "per_field_only",
    "text_only",
    "multi",
    "unknown_component",
    "immutable_property",
    "invalid_value",
    "duplicate_target",
    "too_many_operations",
    "change_action",
    "change_type",
    "executable",
    "html_text",
    "arbitrary_path",
    "malformed",
    "wrong_source",
    "no_op",
    "echo_injection",
    "failure",
    "timeout",
    "unavailable",
]
FIXTURE_MODES: tuple[str, ...] = get_args(FixtureMode)


def _op(component_id: str, prop: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "component_id": component_id, "property": prop, "value": value}


def _current(request: MutationRequest, component_id: str, prop: str) -> Any:
    for target in request.targets:
        if target.component_id == component_id and prop in target.properties:
            return target.properties[prop]["current"]
    return None


def auto_operations(request: MutationRequest) -> list[dict[str, Any]]:
    component = request.hypothesis.affected_component
    if request.signal_type == "rage_click" and component:
        if _current(request, component, "feedback") == "delayed":
            return [_op(component, "feedback", "immediate")]
    if request.signal_type == "error_burst":
        form = next((t for t in request.targets if t.type == "signup_form"), None)
        if form is not None:
            ops = []
            if form.properties["validation"]["current"] == "on_submit":
                ops.append(_op(form.component_id, "validation", "inline"))
            if form.properties["error_display"]["current"] == "summary":
                ops.append(_op(form.component_id, "error_display", "per_field"))
            return ops
    return []


class FixtureMutationGenerator:
    name = "fixture"
    version = FIXTURE_VERSION

    def __init__(self, mode: FixtureMode = "auto") -> None:
        if mode not in FIXTURE_MODES:
            raise ValueError(f"unknown fixture mode {mode!r}")
        self.mode: FixtureMode = mode
        self.calls = 0

    def generate(self, request: MutationRequest) -> GeneratorReply:
        self.calls += 1
        mode = self.mode
        if mode == "failure":
            raise GeneratorFailureError("fixture: failure mode")
        if mode == "timeout":
            raise GeneratorTimeoutError("fixture: timeout mode")
        if mode == "unavailable":
            raise GeneratorUnavailableError("fixture: unavailable mode")
        if mode == "malformed":
            return GeneratorReply('{"version": 1, "operations": [', FIXTURE_VERSION)

        cta = request.hypothesis.affected_component or "plan_team_pro_cta"
        ops = auto_operations(request)
        if mode == "inline_only":
            ops = [o for o in ops if o["property"] == "validation"]
        elif mode == "per_field_only":
            ops = [o for o in ops if o["property"] == "error_display"]
        elif mode == "text_only":
            ops = [_op(cta, "label", "Start with Team Pro")]
        elif mode == "multi":
            ops = [*ops, _op("plans", "spacing", "lg")]
        elif mode == "unknown_component":
            ops = [_op("checkout_pay_button", "feedback", "immediate")]
        elif mode == "immutable_property":
            ops = [_op("plan_team_pro", "price_label", "Free")]
        elif mode == "invalid_value":
            ops = [_op(cta, "feedback", "instant")]
        elif mode == "duplicate_target":
            ops = [_op(cta, "feedback", "immediate"), _op(cta, "feedback", "immediate")]
        elif mode == "too_many_operations":
            ops = [_op(f"x{i}", "text", "x" * 10) for i in range(6)]
        elif mode == "change_action":
            ops = [_op(cta, "action", "deploy_production")]
        elif mode == "change_type":
            ops = [_op(cta, "type", "script")]
        elif mode == "executable":
            ops = [_op(cta, "onClick", "<script>alert(1)</script>")]
        elif mode == "html_text":
            ops = [_op(cta, "label", "<b>Get started</b>")]
        elif mode == "arbitrary_path":
            ops = [{"op": "replace", "path": "/page/sections/1/components/1", "value": "x"}]
        elif mode == "no_op":
            ops = [_op(cta, "variant", _current(request, cta, "variant") or "primary")]
        elif mode == "echo_injection":
            ops = [
                _op(cta, "action", "deploy_production"),
                _op(cta, "label", "<script>deploy()</script>"),
            ]
        if not ops:
            raise GeneratorFailureError("fixture: no applicable mutation for this hypothesis")
        output: dict[str, Any] = {
            "version": 1,
            "source_spec_id": request.source_spec.spec_id,
            "summary": "Remove the Generation 0 friction the hypothesis points at.",
            "operations": ops,
        }
        if mode == "wrong_source":
            output["source_spec_id"] = "00000000-0000-0000-0000-000000000000"
        return GeneratorReply(output, FIXTURE_VERSION)
