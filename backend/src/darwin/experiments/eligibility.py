"""Experiment eligibility and the start gate. Fail closed: prove it, or refuse.

Eligibility is re-derived from PERSISTED records every time; a request that
merely claims "this candidate passed" is never trusted:

  the CandidateEvaluationRun exists, completed, recommendation pass,
  evaluator candidate_eval.v1, reason all_gates_passed, every category pass
  (so provenance, schema, render, functional, accessibility, regression,
  ux_intent and performance all passed);
  no NEWER evaluation of the same candidate says anything other than pass;
  Step 13's provenance re-check still holds today (hashes re-computed,
  succeeded MutationRun, decision still proceed, chain intact);
  the parent is the page's ACTIVE generation (the control: Generation 0 until a
  promotion, Step 15) and both specs still contain the components the metrics read.

The start gate repeats all of that and adds: the experiment is draft (or
paused), its stored hashes still equal the evaluated specs, its configuration
is inside the allowlists, and no other experiment is active on the page.
Every failed check is returned as a reason code; nothing is started.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from darwin.db.models import (
    CandidateEvaluationRun,
    DecisionRun,
    Experiment,
    MutationRun,
    UISpecVersion,
)
from darwin.generations.active import active_spec
from darwin.mutations.apply import content_hash
from darwin.mutations.surface import iter_targets
from darwin.sandbox.policy import CATEGORY_ORDER, EVALUATOR_VERSION
from darwin.sandbox.provenance import CandidateNotFoundError, ProvenanceError, load_context

from .assignment import AllocationError, validate_allocation, validate_key
from .vocabulary import (
    MAX_GUARDRAILS,
    METRIC_NAMES,
    MIN_SAMPLE_CEILING,
    MIN_SAMPLE_FLOOR,
    SIGNUP_FORM_ID,
    TRAFFIC_SOURCES,
)


class ExperimentRefused(ValueError):
    def __init__(self, codes: Sequence[str], detail: str | None = None) -> None:
        super().__init__(", ".join(codes))
        self.codes = tuple(codes)
        self.detail = detail


@dataclass(frozen=True)
class EligibleCandidate:
    evaluation_run_id: uuid.UUID
    mutation_run_id: uuid.UUID
    hypothesis_id: uuid.UUID
    page_id: str
    control_spec_id: uuid.UUID
    control_spec_hash: str
    candidate_spec_id: uuid.UUID
    candidate_spec_hash: str


def validate_config(
    *,
    experiment_key: object,
    candidate_allocation_bp: object,
    primary_metric: object,
    guardrail_metrics: object,
    minimum_sample_per_variant: object,
    traffic_source: object,
) -> list[str]:
    """Every configuration problem, as reason codes (empty list = valid)."""
    problems: list[str] = []
    try:
        validate_key(experiment_key)
    except AllocationError as error:
        problems.append(error.code)
    try:
        validate_allocation(candidate_allocation_bp)
    except AllocationError as error:
        problems.append(error.code)
    if primary_metric not in METRIC_NAMES:
        problems.append("primary_metric_unknown")
    if (
        not isinstance(guardrail_metrics, (list, tuple))
        or not 1 <= len(guardrail_metrics) <= MAX_GUARDRAILS
    ):
        problems.append("guardrails_missing_or_too_many")
    else:
        if any(m not in METRIC_NAMES for m in guardrail_metrics):
            problems.append("guardrail_metric_unknown")
        if len(set(guardrail_metrics)) != len(guardrail_metrics):
            problems.append("guardrail_metric_duplicated")
        if primary_metric in guardrail_metrics:
            problems.append("primary_metric_is_guardrail")
    if (
        type(minimum_sample_per_variant) is not int
        or not MIN_SAMPLE_FLOOR <= minimum_sample_per_variant <= MIN_SAMPLE_CEILING
    ):
        problems.append("minimum_sample_invalid")
    if traffic_source not in TRAFFIC_SOURCES:
        problems.append("traffic_source_unknown")
    return problems


def _has_signup_form(spec: dict[str, Any]) -> bool:
    return any(
        t.component_id == SIGNUP_FORM_ID and t.kind == "signup_form" for t in iter_targets(spec)
    )


def check_eligibility(session: Session, evaluation_run_id: uuid.UUID) -> EligibleCandidate:
    """The proven-eligible candidate behind this evaluation, or ExperimentRefused."""
    run = session.get(CandidateEvaluationRun, evaluation_run_id)
    if run is None:
        raise ExperimentRefused(["evaluation_not_found"])
    if run.status != "completed":
        raise ExperimentRefused(["evaluation_not_completed"])
    if run.recommendation != "pass":
        raise ExperimentRefused(["evaluation_not_pass"])
    if run.evaluator_version != EVALUATOR_VERSION:
        raise ExperimentRefused(["evaluator_version_unknown"])
    if list(run.reason_codes) != ["all_gates_passed"]:
        raise ExperimentRefused(["evaluation_reasons_not_clean"])
    categories = run.category_results or {}
    if any(categories.get(n, {}).get("status") != "pass" for n in CATEGORY_ORDER):
        raise ExperimentRefused(["evaluation_category_not_pass"])
    if run.mutation_run_id is None:
        raise ExperimentRefused(["evaluation_without_mutation_run"])
    newer_non_pass = session.scalar(
        select(CandidateEvaluationRun.id)
        .where(
            CandidateEvaluationRun.candidate_spec_id == run.candidate_spec_id,
            CandidateEvaluationRun.created_at > run.created_at,
            CandidateEvaluationRun.recommendation != "pass",
        )
        .limit(1)
    )
    if newer_non_pass is not None:
        raise ExperimentRefused(["superseded_by_newer_evaluation"])

    try:
        context = load_context(session, run.candidate_spec_id, run.mutation_run_id)
    except ProvenanceError as error:
        raise ExperimentRefused(["provenance_invalid"], detail=error.code) from None
    except CandidateNotFoundError:
        raise ExperimentRefused(["candidate_not_found"]) from None

    parent = session.get(UISpecVersion, context.parent_id)
    candidate = session.get(UISpecVersion, context.candidate_id)
    assert parent is not None and candidate is not None  # load_context proved both exist
    current = active_spec(session, parent.page_id)
    if current is None or current.id != parent.id:
        raise ExperimentRefused(["control_not_active_generation"])
    if not (_has_signup_form(parent.spec) and _has_signup_form(candidate.spec)):
        raise ExperimentRefused(["metric_component_missing"])
    mutation = session.get(MutationRun, context.mutation_run_id)
    decision = session.get(DecisionRun, mutation.decision_run_id) if mutation else None
    if decision is None:
        raise ExperimentRefused(["provenance_invalid"], detail="decision_chain_broken")
    return EligibleCandidate(
        evaluation_run_id=run.id,
        mutation_run_id=context.mutation_run_id,
        hypothesis_id=decision.hypothesis_id,
        page_id=parent.page_id,
        control_spec_id=parent.id,
        control_spec_hash=content_hash(parent.spec),
        candidate_spec_id=candidate.id,
        candidate_spec_hash=content_hash(candidate.spec),
    )


def start_gate(session: Session, experiment: Experiment) -> list[str]:
    """Reason codes that forbid starting (or resuming) this experiment; empty = may start."""
    if experiment.status == "running":
        return ["experiment_already_running"]
    if experiment.status not in ("draft", "paused"):
        return ["experiment_not_startable"]
    problems = validate_config(
        experiment_key=experiment.experiment_key,
        candidate_allocation_bp=experiment.candidate_allocation_bp,
        primary_metric=experiment.primary_metric,
        guardrail_metrics=experiment.guardrail_metrics,
        minimum_sample_per_variant=experiment.minimum_sample_per_variant,
        traffic_source=experiment.traffic_source,
    )
    if experiment.control_allocation_bp + experiment.candidate_allocation_bp != 10_000:
        problems.append("allocation_does_not_sum")
    try:
        eligible = check_eligibility(session, experiment.candidate_evaluation_run_id)
    except ExperimentRefused as refused:
        return problems + list(refused.codes)
    if (
        eligible.candidate_spec_id != experiment.candidate_spec_id
        or eligible.candidate_spec_hash != experiment.candidate_spec_hash
    ):
        problems.append("candidate_changed_since_evaluation")
    if (
        eligible.control_spec_id != experiment.control_spec_id
        or eligible.control_spec_hash != experiment.control_spec_hash
    ):
        problems.append("control_not_active_generation")
    if eligible.mutation_run_id != experiment.mutation_run_id:
        problems.append("mutation_run_changed")
    other_active = session.scalar(
        select(Experiment.id).where(
            Experiment.page_id == experiment.page_id,
            Experiment.status.in_(("running", "paused")),
            Experiment.id != experiment.id,
        )
    )
    if other_active is not None:
        problems.append("another_experiment_active")
    return problems
