"""candidate_eval.v1: the category checks and the deterministic aggregation policy.

Each category returns a status (pass | warn | fail | error | skipped), its kind
(deterministic | measured) and named checks. Nothing is blended into a score.

  schema         the app's real Zod schema accepts the candidate                  deterministic
  render         it renders through the real registry / SpecPage                  deterministic
  functional     every CTA still reveals the signup form and emits its own
                 button_click; the form still errors on empty submit, completes
                 with valid values, keeps its telemetry ids; no typed value
                 reaches telemetry                                                deterministic
  accessibility  no NEW axe violation (serious/critical -> fail, others -> warn);
                 every input labelled; no focusable button lost (fail); heading
                 structure unchanged (warn). Pre-existing issues are not held
                 against the candidate.                                           deterministic
  regression     structure, ids, types and actions unchanged; only mutable leaves
                 differ; the candidate is exactly parent + its MutationSpec;
                 something actually changed                                       deterministic
  ux_intent      UX-intent ALIGNMENT with the observed problem — not UX quality:
                 rage_click on button X: X.feedback delayed -> immediate, confirmed
                 by the measured reveal delay (1500 ms -> 0 ms);
                 error_burst: validation on_submit -> inline (confirmed: an error
                 shows on blur) and/or error_display summary -> per_field
                 (confirmed: per-field errors, no summary). Moving ANY property to
                 a known friction value is a regression.                          deterministic
  performance    component count unchanged (fail); JSON size delta <= 2 KiB and
                 rendered DOM-node delta <= 10% (else warn). Local jsdom render
                 time is recorded, never gated — it is not web performance.       measured

Aggregation: any fail -> reject; else any warn or error -> human_review; else
pass only if ux_intent is aligned. Subjective design quality, copy and taste
are NOT evaluated here: they need humans (or a calibrated judge, later).
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from darwin.mutations.apply import Change, apply_mutation, canonical_json, diff_paths
from darwin.mutations.evaluation import mutable_paths, protected_violations
from darwin.mutations.surface import iter_targets

from .harness import AxeViolation, SpecFacts
from .provenance import EvaluationContext

EVALUATOR_VERSION = "candidate_eval.v1"
Recommendation = Literal["pass", "human_review", "reject"]
Status = Literal["pass", "warn", "fail", "error", "skipped"]

DELAYED_MS = 1500
MAX_JSON_DELTA_BYTES = 2048
MAX_DOM_DELTA_RATIO = 0.10
SERIOUS = frozenset({"serious", "critical"})
# Known friction values: moving a property TO one of these is always a regression.
FRICTION = {
    ("button", "feedback"): "delayed",
    ("signup_form", "validation"): "on_submit",
    ("signup_form", "error_display"): "summary",
}

REASON_CODES = (
    "all_gates_passed",
    "provenance_invalid",
    "schema_invalid",
    "render_failed",
    "functional_regression",
    "telemetry_regression",
    "value_leak",
    "accessibility_regression_serious",
    "accessibility_regression_minor",
    "protected_regression",
    "structure_changed",
    "no_change",
    "ux_intent_regression",
    "ux_intent_unrelated",
    "ux_intent_mixed",
    "ux_intent_unconfirmed",
    "ux_intent_unknown_problem",
    "performance_delta",
    "evaluator_error",
)


@dataclass
class Check:
    name: str
    ok: bool
    reason: str | None = None  # a REASON_CODES entry when not ok
    severity: Literal["fail", "warn"] = "fail"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CategoryResult:
    name: str
    kind: Literal["deterministic", "measured"]
    status: Status
    checks: list[Check] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "checks": [
                {"name": c.name, "ok": c.ok, "reason": c.reason, "severity": c.severity, **c.detail}
                for c in self.checks
            ],
            **({"note": self.note} if self.note else {}),
        }


def _category(
    name: str, kind: Literal["deterministic", "measured"], checks: list[Check]
) -> CategoryResult:
    status: Status = "pass"
    if any(not c.ok and c.severity == "fail" for c in checks):
        status = "fail"
    elif any(not c.ok for c in checks):
        status = "warn"
    return CategoryResult(name, kind, status, checks)


# ---- categories ---------------------------------------------------------------------------


def schema_category(candidate: SpecFacts) -> CategoryResult:
    return _category(
        "schema",
        "deterministic",
        [
            Check(
                "frontend_zod_schema",
                candidate.schema_.ok,
                "schema_invalid",
                detail={"issues": candidate.schema_.issues[:5]},
            )
        ],
    )


def render_category(candidate: SpecFacts) -> CategoryResult:
    return _category(
        "render",
        "deterministic",
        [
            Check(
                "renders_through_registry",
                candidate.render.ok,
                "render_failed",
                detail={"error": candidate.render.error},
            )
        ],
    )


def functional_category(source: SpecFacts, candidate: SpecFacts) -> CategoryResult:
    checks = []
    src_ctas = {c.component_id: c for c in source.ctas}
    cand_ctas = {c.component_id: c for c in candidate.ctas}
    checks.append(Check("same_ctas", set(src_ctas) == set(cand_ctas), "functional_regression"))
    for cid, cta in sorted(cand_ctas.items()):
        checks.append(
            Check(f"{cid}.reveals_signup", cta.reveal_delay_ms is not None, "functional_regression")
        )
        before = src_ctas.get(cid)
        hidden_same = (
            before is not None
            and before.signup_hidden_before_click == cta.signup_hidden_before_click
        )
        checks.append(
            Check(f"{cid}.signup_hidden_until_click", hidden_same, "functional_regression")
        )
        checks.append(
            Check(
                f"{cid}.emits_button_click",
                cta.telemetry_components == [cid],
                "telemetry_regression",
            )
        )
    sf, cf = source.form, candidate.form
    if sf is not None:
        present = cf is not None and cf.present
        checks.append(Check("form_present", present, "functional_regression"))
        if cf is not None and present:
            checks.append(
                Check(
                    "form_errors_on_empty_submit",
                    {e.field for e in cf.submit_empty_errors}
                    == {e.field for e in sf.submit_empty_errors}
                    and bool(cf.submit_empty_errors),
                    "functional_regression",
                )
            )
            checks.append(
                Check(
                    "form_completes_with_valid_values",
                    cf.completed_with_valid_values,
                    "functional_regression",
                )
            )
            checks.append(
                Check(
                    "form_telemetry_ids_unchanged",
                    cf.telemetry_components == sf.telemetry_components,
                    "telemetry_regression",
                )
            )
    checks.append(
        Check("no_typed_values_in_telemetry", not candidate.telemetry.values_leaked, "value_leak")
    )
    kinds = sorted({str(p.get("event_type")) for p in candidate.telemetry.payloads})
    src_kinds = sorted({str(p.get("event_type")) for p in source.telemetry.payloads})
    checks.append(
        Check(
            "telemetry_event_types_unchanged",
            kinds == src_kinds,
            "telemetry_regression",
            detail={"event_types": kinds},
        )
    )
    return _category("functional", "deterministic", checks)


def _worst(violations: list[AxeViolation]) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for v in violations:
        impact, nodes = out.get(v.id, (v.impact, 0))
        out[v.id] = (impact, max(nodes, v.nodes))
    return out


def accessibility_category(source: SpecFacts, candidate: SpecFacts) -> CategoryResult:
    src = _worst(source.accessibility.initial + source.accessibility.revealed)
    cand = _worst(candidate.accessibility.initial + candidate.accessibility.revealed)
    introduced = {
        rule: impact
        for rule, (impact, nodes) in cand.items()
        if rule not in src or nodes > src[rule][1]
    }
    serious = sorted(r for r, i in introduced.items() if i in SERIOUS)
    minor = sorted(r for r, i in introduced.items() if i not in SERIOUS)
    s, c = source.semantics, candidate.semantics
    checks = [
        Check(
            "no_new_serious_axe_violations",
            not serious,
            "accessibility_regression_serious",
            detail={"rules": serious},
        ),
        Check(
            "no_new_minor_axe_violations",
            not minor,
            "accessibility_regression_minor",
            severity="warn",
            detail={"rules": minor},
        ),
        Check(
            "all_inputs_labelled", c.labelled_inputs == c.inputs, "accessibility_regression_serious"
        ),
        Check(
            "focusable_buttons_kept",
            c.focusable_buttons >= s.focusable_buttons,
            "accessibility_regression_serious",
        ),
        Check(
            "heading_structure_unchanged",
            c.heading_levels == s.heading_levels,
            "accessibility_regression_minor",
            severity="warn",
        ),
    ]
    result = _category("accessibility", "deterministic", checks)
    result.note = "axe-core in jsdom; disabled (needs a real browser): " + ", ".join(
        candidate.accessibility.disabled_rules
    )
    return result


def changed_leaves(source: dict[str, Any], candidate: dict[str, Any]) -> list[Change]:
    """Mutable-leaf differences as (component, property, new value); generation excluded."""
    out = []
    targets = {}
    for t in iter_targets(source):
        targets[t.component_id] = t
    for path in diff_paths(source, candidate):
        if path == ("generation",) or path not in mutable_paths(source):
            continue
        node = _node_at(source, path[:-1])
        cid = node.get("id") if isinstance(node, dict) else None
        if isinstance(cid, str):
            out.append(Change(cid, path[-1], _node_at(candidate, path)))
    return sorted(out, key=lambda c: (c.component_id, c.property))


def _node_at(spec: Any, path: tuple[str, ...]) -> Any:
    node = spec
    for part in path:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _structure(spec: dict[str, Any]) -> list[tuple[str, str, Any]]:
    return [(t.component_id, t.kind, t.node.get("action")) for t in iter_targets(spec)]


def _component_count(spec: dict[str, Any]) -> int:
    return sum(1 for _ in iter_targets(spec))


def regression_category(context: EvaluationContext) -> CategoryResult:
    source, candidate = context.source, context.candidate
    same_structure = _structure(source) == _structure(candidate)
    checks = [
        Check("ids_types_actions_unchanged", same_structure, "protected_regression"),
        Check(
            "only_mutable_leaves_changed",
            protected_violations(source, candidate) == 0,
            "protected_regression",
        ),
    ]
    changes = [
        Change(str(o.get("component_id")), str(o.get("property")), o.get("value"))
        for o in context.operations
    ]
    try:
        rebuilt = apply_mutation(source, changes, int(candidate.get("generation", -1)))
        matches = canonical_json(rebuilt) == canonical_json(candidate)
    except (KeyError, TypeError, ValueError):
        matches = False
    checks.append(Check("candidate_is_parent_plus_mutation", matches, "protected_regression"))
    checks.append(Check("something_changed", bool(changed_leaves(source, candidate)), "no_change"))
    return _category("regression", "deterministic", checks)


def ux_intent_category(
    context: EvaluationContext, source: SpecFacts, candidate: SpecFacts
) -> CategoryResult:
    kinds = {t.component_id: t.kind for t in iter_targets(context.source)}
    changes = changed_leaves(context.source, context.candidate)
    regressions = [
        c for c in changes if FRICTION.get((kinds.get(c.component_id, ""), c.property)) == c.value
    ]
    if context.signal_type == "rage_click" and context.affected_component:
        targets = {(context.affected_component, "feedback")}
    elif context.signal_type == "error_burst":
        form = next((cid for cid, k in kinds.items() if k == "signup_form"), None)
        targets = {(form, "validation"), (form, "error_display")} if form else set()
    else:
        targets = set()
    aligned = [
        c
        for c in changes
        if (c.component_id, c.property) in targets
        and FRICTION.get((kinds.get(c.component_id, ""), c.property)) not in (None, c.value)
    ]
    unrelated = [c for c in changes if (c.component_id, c.property) not in targets]
    confirmed = all(_confirmed(c, source, candidate) for c in aligned)
    detail = {
        "signal_type": context.signal_type,
        "aligned": [f"{c.component_id}.{c.property}" for c in aligned],
        "regressions": [f"{c.component_id}.{c.property}" for c in regressions],
        "unrelated": [f"{c.component_id}.{c.property}" for c in unrelated],
    }
    if regressions:
        check = Check("moves_toward_known_friction", False, "ux_intent_regression", detail=detail)
    elif not targets:
        check = Check("problem_supported", False, "ux_intent_unknown_problem", "warn", detail)
    elif not aligned:
        check = Check("addresses_observed_problem", False, "ux_intent_unrelated", "warn", detail)
    elif not confirmed:
        check = Check("behaviour_confirms_intent", False, "ux_intent_unconfirmed", "warn", detail)
    elif unrelated:
        check = Check("only_intended_changes", False, "ux_intent_mixed", "warn", detail)
    else:
        check = Check("aligned_with_observed_problem", True, detail=detail)
    result = _category("ux_intent", "deterministic", [check])
    result.note = (
        "UX-intent alignment with the observed problem; not UX quality or user satisfaction"
    )
    return result


def _confirmed(change: Change, source: SpecFacts, candidate: SpecFacts) -> bool:
    if change.property == "feedback":
        before = next((c for c in source.ctas if c.component_id == change.component_id), None)
        after = next((c for c in candidate.ctas if c.component_id == change.component_id), None)
        return bool(
            before and after and before.reveal_delay_ms == DELAYED_MS and after.reveal_delay_ms == 0
        )
    if change.property == "validation":
        return bool(candidate.form and candidate.form.inline_error_on_blur)
    if change.property == "error_display":
        return bool(
            candidate.form
            and candidate.form.per_field_errors_shown > 0
            and not candidate.form.summary_alert_shown
        )
    return False


def performance_category(
    context: EvaluationContext, source: SpecFacts, candidate: SpecFacts
) -> CategoryResult:
    json_delta = abs(len(canonical_json(context.candidate)) - len(canonical_json(context.source)))
    dom_delta = abs(candidate.render.dom_nodes - source.render.dom_nodes)
    checks = [
        Check(
            "component_count_unchanged",
            _component_count(context.source) == _component_count(context.candidate),
            "structure_changed",
            detail={"components": _component_count(context.candidate)},
        ),
        Check(
            "json_size_delta_bounded",
            json_delta <= MAX_JSON_DELTA_BYTES,
            "performance_delta",
            "warn",
            detail={"json_delta_bytes": json_delta},
        ),
        Check(
            "dom_node_delta_bounded",
            dom_delta <= max(5, MAX_DOM_DELTA_RATIO * max(source.render.dom_nodes, 1)),
            "performance_delta",
            "warn",
            detail={"dom_nodes": candidate.render.dom_nodes, "dom_delta": dom_delta},
        ),
    ]
    result = _category("performance", "measured", checks)
    result.note = (
        "structural bounds only; local jsdom render time is informational, not web performance"
    )
    return result


# ---- aggregation --------------------------------------------------------------------------


CATEGORY_ORDER = (
    "schema",
    "render",
    "functional",
    "accessibility",
    "regression",
    "ux_intent",
    "performance",
)


def skipped(name: str, kind: Literal["deterministic", "measured"], why: str) -> CategoryResult:
    return CategoryResult(name, kind, "skipped", note=why)


def aggregate(categories: dict[str, CategoryResult]) -> tuple[Recommendation, list[str]]:
    failed = [
        c.reason
        for cat in categories.values()
        for c in cat.checks
        if not c.ok and c.severity == "fail"
    ]
    warned = [
        c.reason
        for cat in categories.values()
        for c in cat.checks
        if not c.ok and c.severity == "warn"
    ]
    errored = any(cat.status == "error" for cat in categories.values())
    if failed:  # every finding is reported, the recommendation follows the worst
        return "reject", sorted({r for r in failed + warned if r})
    if warned or errored:
        return "human_review", sorted(
            {r for r in warned if r} | ({"evaluator_error"} if errored else set())
        )
    ux = categories.get("ux_intent")
    if ux is None or ux.status != "pass":
        return "human_review", ["ux_intent_unconfirmed"]
    return "pass", ["all_gates_passed"]


def evaluate_facts(
    context: EvaluationContext, source: SpecFacts, candidate: SpecFacts
) -> dict[str, CategoryResult]:
    """Every category for one candidate; later ones are skipped when a hard gate fails."""
    out: dict[str, CategoryResult] = {}
    if not source.schema_.ok or not source.render.ok:
        for name in CATEGORY_ORDER:
            out[name] = CategoryResult(
                name, "deterministic", "error", note="source spec failed in the harness"
            )
        return out
    out["schema"] = schema_category(candidate)
    if out["schema"].status == "fail":
        return out | {n: skipped(n, "deterministic", "schema invalid") for n in CATEGORY_ORDER[1:]}
    out["render"] = render_category(candidate)
    if out["render"].status == "fail":
        return out | {n: skipped(n, "deterministic", "render failed") for n in CATEGORY_ORDER[2:]}
    out["functional"] = functional_category(source, candidate)
    out["accessibility"] = accessibility_category(source, candidate)
    out["regression"] = regression_category(context)
    out["ux_intent"] = ux_intent_category(context, source, candidate)
    out["performance"] = performance_category(context, source, candidate)
    return out
