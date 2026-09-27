"""Sandbox evaluation without Node or a database: harness output validation, every category,
the aggregation policy, and the service's fail-closed paths (stubbed)."""

import copy
import uuid
from typing import Any

import pytest

from darwin.mutations.specs import load_generation_zero
from darwin.sandbox import service as sandbox_service
from darwin.sandbox.evaluation import GOLDEN_PATH, load_dataset
from darwin.sandbox.harness import HarnessError, NodeHarnessRunner, SpecFacts, parse_output
from darwin.sandbox.policy import (
    CATEGORY_ORDER,
    EVALUATOR_VERSION,
    REASON_CODES,
    aggregate,
    evaluate_facts,
)
from darwin.sandbox.provenance import EvaluationContext, ProvenanceError

GEN0: dict[str, Any] = load_generation_zero()
PRO = ("page", "sections", 1, "components", 1, "plans", 2, "cta")
STARTER = ("page", "sections", 1, "components", 1, "plans", 0, "cta")
FORM = ("page", "sections", 2, "components", 0)


def facts(**changes: Any) -> SpecFacts:
    """Facts shaped like the real harness output for Generation 0."""
    base: dict[str, Any] = {
        "schema": {"ok": True, "issues": []},
        "render": {"ok": True, "error": None, "dom_nodes": 35, "buttons": 3},
        "accessibility": {"initial": [], "revealed": [], "disabled_rules": ["color-contrast"]},
        "semantics": {
            "heading_levels": [1, 2, 3, 3, 3, 2],
            "inputs": 2,
            "labelled_inputs": 2,
            "focusable_buttons": 4,
        },
        "ctas": [
            {
                "component_id": c,
                "telemetry_components": [c],
                "reveal_delay_ms": d,
                "signup_hidden_before_click": True,
            }
            for c, d in (("plan_starter_cta", 0), ("plan_team_cta", 0), ("plan_team_pro_cta", 1500))
        ],
        "form": {
            "present": True,
            "submit_empty_errors": [
                {"field": "email", "reason": "required"},
                {"field": "team_name", "reason": "required"},
            ],
            "summary_alert_shown": True,
            "per_field_errors_shown": 0,
            "inline_error_on_blur": False,
            "completed_with_valid_values": True,
            "telemetry_components": ["signup_form", "signup_form_submit"],
        },
        "telemetry": {
            "payloads": [{"event_type": "page_view"}, {"event_type": "button_click"}],
            "values_leaked": False,
        },
        "render_ms": 1.0,
    }
    for key, value in changes.items():
        base[key] = value
    return SpecFacts.model_validate(base)


def ctas(
    pro: int | None = 0, starter: int | None = 0, pro_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "component_id": "plan_starter_cta",
            "telemetry_components": ["plan_starter_cta"],
            "reveal_delay_ms": starter,
            "signup_hidden_before_click": True,
        },
        {
            "component_id": "plan_team_cta",
            "telemetry_components": ["plan_team_cta"],
            "reveal_delay_ms": 0,
            "signup_hidden_before_click": True,
        },
        {
            "component_id": "plan_team_pro_cta",
            "telemetry_components": pro_ids or ["plan_team_pro_cta"],
            "reveal_delay_ms": pro,
            "signup_hidden_before_click": True,
        },
    ]


def edit(spec: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(spec)
    node: Any = out
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return out


def context(
    candidate: dict[str, Any],
    signal: str = "rage_click",
    component: str | None = "plan_team_pro_cta",
) -> EvaluationContext:
    from darwin.sandbox.evaluation import _operations

    candidate = {**candidate, "generation": 1}
    return EvaluationContext(
        candidate_id=uuid.uuid4(),
        parent_id=uuid.uuid4(),
        mutation_run_id=uuid.uuid4(),
        source=GEN0,
        candidate=candidate,
        operations=tuple(_operations(GEN0, candidate)),
        signal_type=signal,
        affected_component=component,
    )


def run(
    ctx: EvaluationContext, candidate_facts: SpecFacts
) -> tuple[str, list[str], dict[str, str]]:
    categories = evaluate_facts(ctx, facts(), candidate_facts)
    recommendation, reasons = aggregate(categories)
    return recommendation, reasons, {n: c.status for n, c in categories.items()}


FIXED = edit(GEN0, (*PRO, "feedback"), "immediate")


# ---- harness output ---------------------------------------------------------------------------


def test_harness_output_is_validated_strictly() -> None:
    good = {
        "harness_version": "sandbox_harness.v1",
        "facts": {"a": facts().model_dump(by_alias=True)},
    }
    assert parse_output(good, {"a"})["a"].render.ok
    for raw, code in (
        ({"harness_version": "other", "facts": good["facts"]}, "harness_version_mismatch"),
        ({"harness_version": "sandbox_harness.v1", "facts": {}}, "harness_output_incomplete"),
        (
            {"harness_version": "sandbox_harness.v1", "facts": {"a": {"schema": {}}}},
            "harness_output_malformed",
        ),
        ({**good, "extra": 1}, "harness_output_malformed"),
        ("not an object", "harness_output_malformed"),
    ):
        with pytest.raises(HarnessError) as error:
            parse_output(raw, {"a"})
        assert error.value.code == code


def test_missing_npm_is_an_evaluator_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("darwin.sandbox.harness.shutil.which", lambda name: None)
    with pytest.raises(HarnessError) as error:
        NodeHarnessRunner().run({"a": GEN0})
    assert error.value.code == "harness_unavailable"


# ---- UX intent: safe != useful -----------------------------------------------------------------


def test_rage_click_fix_passes_every_category() -> None:
    recommendation, reasons, statuses = run(context(FIXED), facts(ctas=ctas(pro=0)))
    assert (recommendation, reasons) == ("pass", ["all_gates_passed"])
    assert set(statuses.values()) == {"pass"}


def test_harmful_but_safe_candidate_is_rejected() -> None:
    harmful = edit(GEN0, (*STARTER, "feedback"), "delayed")
    recommendation, reasons, statuses = run(
        context(harmful, component="plan_starter_cta"), facts(ctas=ctas(pro=1500, starter=1500))
    )
    assert recommendation == "reject" and "ux_intent_regression" in reasons
    assert statuses["schema"] == statuses["regression"] == "pass"  # safe...
    assert statuses["ux_intent"] == "fail"  # ...but not useful


def test_intent_must_be_confirmed_by_behaviour() -> None:
    recommendation, reasons, _ = run(context(FIXED), facts(ctas=ctas(pro=1500)))  # no effect
    assert (recommendation, reasons) == ("human_review", ["ux_intent_unconfirmed"])


def test_unrelated_and_mixed_and_unknown_problems_need_a_human() -> None:
    spacing = edit(GEN0, ("page", "sections", 1, "spacing"), "lg")
    assert run(context(spacing), facts())[1] == ["ux_intent_unrelated"]
    mixed = edit(FIXED, ("page", "sections", 1, "spacing"), "lg")
    assert run(context(mixed), facts(ctas=ctas(pro=0)))[1] == ["ux_intent_mixed"]
    assert run(context(FIXED, signal="scroll_depth"), facts(ctas=ctas(pro=0)))[1] == [
        "ux_intent_unknown_problem"
    ]


def test_error_burst_fixes_are_confirmed_by_behaviour() -> None:
    both = edit(edit(GEN0, (*FORM, "validation"), "inline"), (*FORM, "error_display"), "per_field")
    base_form = facts().form
    assert base_form is not None
    form = {
        **base_form.model_dump(),
        "inline_error_on_blur": True,
        "per_field_errors_shown": 2,
        "summary_alert_shown": False,
    }
    assert run(context(both, "error_burst", "signup_form"), facts(form=form))[0] == "pass"
    inline_only = edit(GEN0, (*FORM, "validation"), "inline")
    # the real renderer shows nothing on blur with summary display: unconfirmed
    assert run(context(inline_only, "error_burst", "signup_form"), facts())[1] == [
        "ux_intent_unconfirmed"
    ]
    back = edit(GEN0, (*FORM, "error_display"), "summary")  # already summary -> no change
    assert "no_change" in run(context(back, "error_burst", "signup_form"), facts())[1]


# ---- functional, accessibility, regression, performance ---------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"ctas": ctas(pro=None)}, "functional_regression"),
        ({"ctas": ctas(pro=0, pro_ids=["renamed"])}, "telemetry_regression"),
        ({"ctas": ctas(pro=0)[:2]}, "functional_regression"),
        ({"telemetry": {"payloads": [], "values_leaked": True}}, "value_leak"),
        ({"form": None}, "functional_regression"),
    ],
)
def test_functional_regressions_reject(changes: dict[str, Any], reason: str) -> None:
    candidate = facts(**({"ctas": ctas(pro=0)} | changes))
    recommendation, reasons, statuses = run(context(FIXED), candidate)
    assert recommendation == "reject" and reason in reasons and statuses["functional"] == "fail"


@pytest.mark.parametrize(
    ("changes", "recommendation", "reason"),
    [
        (
            {
                "accessibility": {
                    "initial": [{"id": "label", "impact": "critical", "nodes": 1}],
                    "revealed": [],
                    "disabled_rules": [],
                }
            },
            "reject",
            "accessibility_regression_serious",
        ),
        (
            {
                "accessibility": {
                    "initial": [{"id": "heading-order", "impact": "moderate", "nodes": 1}],
                    "revealed": [],
                    "disabled_rules": [],
                }
            },
            "human_review",
            "accessibility_regression_minor",
        ),
        (
            {
                "semantics": {
                    "heading_levels": [1, 2, 3, 3, 3, 2],
                    "inputs": 3,
                    "labelled_inputs": 2,
                    "focusable_buttons": 4,
                }
            },
            "reject",
            "accessibility_regression_serious",
        ),
        (
            {
                "semantics": {
                    "heading_levels": [1, 2, 3, 3, 3, 2],
                    "inputs": 2,
                    "labelled_inputs": 2,
                    "focusable_buttons": 3,
                }
            },
            "reject",
            "accessibility_regression_serious",
        ),
        (
            {
                "semantics": {
                    "heading_levels": [1, 3, 3, 3, 3, 2],
                    "inputs": 2,
                    "labelled_inputs": 2,
                    "focusable_buttons": 4,
                }
            },
            "human_review",
            "accessibility_regression_minor",
        ),
    ],
)
def test_accessibility_gate(changes: dict[str, Any], recommendation: str, reason: str) -> None:
    result = run(context(FIXED), facts(ctas=ctas(pro=0), **changes))
    assert result[0] == recommendation and reason in result[1]


def test_pre_existing_accessibility_issues_are_not_held_against_the_candidate() -> None:
    issue = {
        "initial": [{"id": "heading-order", "impact": "moderate", "nodes": 1}],
        "revealed": [],
        "disabled_rules": [],
    }
    categories = evaluate_facts(
        context(FIXED), facts(accessibility=issue), facts(ctas=ctas(pro=0), accessibility=issue)
    )
    assert categories["accessibility"].status == "pass"
    worse = {**issue, "initial": [{"id": "heading-order", "impact": "moderate", "nodes": 2}]}
    categories = evaluate_facts(
        context(FIXED), facts(accessibility=issue), facts(ctas=ctas(pro=0), accessibility=worse)
    )
    assert categories["accessibility"].status == "warn"  # more nodes = worse


def test_regression_catches_structure_and_provenance_drift() -> None:
    renamed = edit(FIXED, (*PRO, "id"), "renamed_cta")
    assert "protected_regression" in run(context(renamed), facts(ctas=ctas(pro=0)))[1]
    ctx = context(FIXED)
    drifted = EvaluationContext(
        **{**ctx.__dict__, "operations": ()}
    )  # candidate != parent + mutation
    assert (
        "protected_regression"
        in aggregate(evaluate_facts(drifted, facts(), facts(ctas=ctas(pro=0))))[1]
    )
    assert "no_change" in run(context(GEN0), facts())[1]


def test_performance_bounds_are_structural_and_modest() -> None:
    long_text = edit(FIXED, ("page", "sections", 0, "components", 1, "text"), "x" * 400)
    categories = evaluate_facts(
        context(long_text),
        facts(),
        facts(ctas=ctas(pro=0), render={"ok": True, "error": None, "dom_nodes": 80, "buttons": 3}),
    )
    assert categories["performance"].status == "warn"
    assert categories["performance"].kind == "measured"
    assert "not web performance" in (categories["performance"].note or "")


# ---- skipping and aggregation --------------------------------------------------------------


def test_hard_gates_skip_later_categories_and_source_failure_errors() -> None:
    invalid = facts(schema={"ok": False, "issues": ["x"]})
    categories = evaluate_facts(context(FIXED), facts(), invalid)
    assert categories["schema"].status == "fail" and categories["render"].status == "skipped"
    assert aggregate(categories)[0] == "reject"
    crashed = facts(render={"ok": False, "error": "TypeError", "dom_nodes": 0, "buttons": 0})
    assert aggregate(evaluate_facts(context(FIXED), facts(), crashed))[1] == ["render_failed"]
    broken_source = evaluate_facts(
        context(FIXED),
        facts(render={"ok": False, "error": "X", "dom_nodes": 0, "buttons": 0}),
        facts(),
    )
    assert {c.status for c in broken_source.values()} == {"error"}
    assert aggregate(broken_source) == ("human_review", ["evaluator_error"])


def test_reason_codes_are_closed_and_versioned() -> None:
    assert EVALUATOR_VERSION == "candidate_eval.v1"
    assert set(CATEGORY_ORDER) == {
        "schema",
        "render",
        "functional",
        "accessibility",
        "regression",
        "ux_intent",
        "performance",
    }
    for ctx, candidate in ((context(FIXED), facts(ctas=ctas(pro=0))), (context(GEN0), facts())):
        assert set(aggregate(evaluate_facts(ctx, facts(), candidate))[1]) <= set(REASON_CODES)


# ---- the service never fails open (stubbed database) -------------------------------------------


class StubSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def __enter__(self) -> "StubSession":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def add(self, row: Any) -> None:
        self.added.append(row)

    def get(self, *_: Any) -> Any:
        return None


class Runner:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.calls = error, 0

    def run(self, specs: Any) -> Any:
        self.calls += 1
        if self.error:
            raise self.error
        return {
            key: facts(ctas=ctas(pro=0)) if spec.get("generation") == 1 else facts()
            for key, spec in specs.items()
        }


def test_provenance_failure_rejects_without_the_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: Any) -> Any:
        raise ProvenanceError("candidate_hash_mismatch")

    monkeypatch.setattr(sandbox_service, "load_context", fail)
    session, runner = StubSession(), Runner()
    outcome = sandbox_service.evaluate_candidate(lambda: session, uuid.uuid4(), runner)  # type: ignore[arg-type,return-value]
    assert (outcome.status, outcome.recommendation, outcome.error_type) == (
        "provenance_failed",
        "reject",
        "candidate_hash_mismatch",
    )
    assert runner.calls == 0 and not outcome.harness_called
    [row] = session.added
    assert row.recommendation == "reject" and row.mutation_run_id is None


@pytest.mark.parametrize("error", [HarnessError("harness_timeout"), RuntimeError("bug")])
def test_evaluator_failures_fail_closed(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(sandbox_service, "load_context", lambda *_: context(FIXED))
    session = StubSession()
    outcome = sandbox_service.evaluate_candidate(lambda: session, uuid.uuid4(), Runner(error))  # type: ignore[arg-type,return-value]
    assert (outcome.status, outcome.recommendation) == ("evaluator_error", "human_review")
    assert outcome.reason_codes == ("evaluator_error",)


def test_service_passes_only_a_fully_aligned_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_service, "load_context", lambda *_: context(FIXED))
    session = StubSession()
    outcome = sandbox_service.evaluate_candidate(lambda: session, uuid.uuid4(), Runner())  # type: ignore[arg-type,return-value]
    assert (outcome.status, outcome.recommendation) == ("completed", "pass")
    [row] = session.added
    assert set(row.category_results) == set(CATEGORY_ORDER)
    assert "Notewise" not in str(row.category_results)  # no spec text in the stored report


def test_golden_sandbox_dataset_is_valid() -> None:
    dataset = load_dataset(GOLDEN_PATH)
    assert len(dataset.cases) >= 22
    tags = {t for c in dataset.cases for t in c.tags}
    assert {
        "harmful_safe",
        "irrelevant_safe",
        "provenance",
        "evaluator_failure",
        "accessibility_regression",
        "functional_regression",
    } <= tags
    assert {c.expected.recommendation for c in dataset.cases} == {"pass", "human_review", "reject"}
