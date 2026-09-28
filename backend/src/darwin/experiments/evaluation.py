"""Golden experiment evaluation (make experiment-eval). Rolled back; nothing persists.

    python -m darwin.experiments.evaluation [--output artifacts/experiment-eval.json]

Every case runs in its own SAVEPOINT on its own page ("exp_<case>"), so it
never collides with real experiments in the database. Candidates are REAL
Step 13 evaluations: the frontend harness runs once over the four distinct
specs (Generation 0 + three candidates), then each case's candidates are
evaluated by candidate_eval.v1 with those cached facts.

Kinds: eligibility, allocation, exposure, analysis, serving, db_constraint.
FAIL-OPEN = an experiment was created/started, a candidate was served, an
exposure was counted, a configuration was stored, or an analysis was treated
as more conclusive than expected, when the required invariant was not proven.
Target: 0.
"""

import argparse
import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, Engine, func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import (
    CandidateEvaluationRun,
    DecisionRun,
    Experiment,
    ExperimentExposure,
    MutationRun,
    UISpecVersion,
    UserEvent,
)
from darwin.decisions.evaluation import Artifact, write_artifact
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.logging_config import configure_logging
from darwin.memory.corpus import REPO_ROOT
from darwin.mutations.apply import content_hash
from darwin.mutations.specs import import_generation_zero, load_generation_zero
from darwin.sandbox.evaluation import CachedRunner
from darwin.sandbox.harness import HarnessError, HarnessRunner, NodeHarnessRunner, SpecFacts
from darwin.sandbox.service import evaluate_candidate
from darwin.signals.service import reconcile_session_signals
from darwin.telemetry.service import process_telemetry_message

from .assignment import AllocationError, assign, bucket, validate_allocation
from .service import (
    analyze_experiment,
    complete_experiment,
    create_experiment,
    pause_experiment,
    start_experiment,
    stop_experiment,
)
from .serving import resolve_variant
from .vocabulary import (
    CANDIDATE_ALLOCATIONS_BP,
    EXPOSURE_EVENT,
    FALLBACK_EVENT,
    SIGNUP_FORM_ID,
    SIGNUP_SUBMIT_COMPONENT,
    TOTAL_BUCKETS,
    MetricName,
    Variant,
)

GOLDEN_PATH = REPO_ROOT / "backend" / "tests" / "evals" / "golden" / "experiments.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "experiment-eval.json"
NAMESPACE = uuid.UUID("6f1d2c4b-8a3e-4b7f-9c15-2e8d0a7b3f61")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
AS_OF = datetime(2100, 1, 1, tzinfo=UTC)  # after every received_at: deterministic reports
CREATED = T0 - timedelta(minutes=2)  # fixture experiments are created ...
STARTED = T0 - timedelta(minutes=1)  # ... and started (window opens) just before T0
SessionFactory = Callable[[], Session]

# ---- the three candidates every world contains (content identical across worlds) ----------


def _set(spec: dict[str, Any], component: str, prop: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(spec)
    for section in out["page"]["sections"]:
        for c in section["components"]:
            nodes = [c] + [n for p in c.get("plans", []) for n in (p, p["cta"])]
            for node in nodes:
                if node.get("id") == component:
                    node[prop] = value
    out["generation"] = 1
    return out


def candidate_specs(gen0: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "pass": _set(gen0, "plan_team_pro_cta", "feedback", "immediate"),  # the rage fix
        "reject": _set(gen0, "plan_starter_cta", "feedback", "delayed"),  # harmful but safe
        "human_review": _set(gen0, "plan_team_pro_cta", "label", "Start now"),  # unrelated
    }


def harness_facts(runner: HarnessRunner | None = None) -> dict[str, SpecFacts]:
    """One real harness run over every distinct spec a world needs."""
    gen0 = load_generation_zero()
    specs = {content_hash(gen0): gen0}
    specs.update({content_hash(s): s for s in candidate_specs(gen0).values()})
    return (runner or NodeHarnessRunner()).run(specs)


@dataclass
class World:
    tag: str
    page_id: str
    control_id: uuid.UUID
    candidate_ids: dict[str, uuid.UUID]
    evaluation_ids: dict[str, uuid.UUID]
    decision_run_id: uuid.UUID


def _uid(tag: str, part: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{tag}:{part}")


def build_world(factory: SessionFactory, tag: str, facts: Mapping[str, SpecFacts]) -> World:
    """A private Generation 0 baseline + a proceed decision + three evaluated candidates."""
    with factory() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(session, f"experiment:{tag}", Artifact())
    decision = decide_research_run(factory, research_run_id, RulesDecider())
    gen0 = dict(load_generation_zero())
    page_id = f"exp_{tag}"[:64]
    with factory() as session:
        control = UISpecVersion(
            id=_uid(tag, "control"),
            page_id=page_id,
            status="baseline",
            generation=0,
            schema_version=1,
            spec=gen0,
            content_hash=content_hash(gen0),
            source="experiment-eval-fixture",
        )
        session.add(control)
        session.flush()
        candidate_ids, runs = {}, {}
        for label, spec in candidate_specs(gen0).items():
            row = UISpecVersion(
                id=_uid(tag, f"candidate:{label}"),
                page_id=page_id,
                status="candidate",
                candidate_for_generation=1,
                parent_id=control.id,
                schema_version=1,
                spec=spec,
                content_hash=content_hash(spec),
                source="experiment-eval-fixture",
            )
            session.add(row)
            session.flush()
            ops = [
                {"op": "replace", "component_id": c, "property": p, "value": v}
                for c, p, v in {
                    "pass": [("plan_team_pro_cta", "feedback", "immediate")],
                    "reject": [("plan_starter_cta", "feedback", "delayed")],
                    "human_review": [("plan_team_pro_cta", "label", "Start now")],
                }[label]
            ]
            run = MutationRun(
                id=_uid(tag, f"run:{label}"),
                decision_run_id=decision.decision_run_id,
                source_spec_id=control.id,
                candidate_spec_id=row.id,
                generator="fixture",
                generator_version="experiment-eval-fixture",
                request_version="mutation_request.v1",
                request_hash=hashlib.sha256(f"{tag}:{label}".encode()).hexdigest(),
                status="succeeded",
                error_type=None,
                validation_errors=[],
                mutation_spec={"version": 1, "operations": ops},
                operation_count=len(ops),
            )
            session.add(run)
            candidate_ids[label], runs[label] = row.id, run.id
        session.commit()
    evaluations = {}
    for label, candidate_id in candidate_ids.items():
        outcome = evaluate_candidate(factory, candidate_id, CachedRunner(facts), runs[label])
        assert outcome.recommendation == label, f"{tag}: {label} evaluated {outcome.recommendation}"
        evaluations[label] = outcome.evaluation_run_id
    control_id = _uid(tag, "control")
    return World(tag, page_id, control_id, candidate_ids, evaluations, decision.decision_run_id)


# ---- synthetic traffic through the real worker path ----------------------------------------


def sessions_for(
    key: str, allocation_bp: int, variant: Variant, count: int, tag: str
) -> list[uuid.UUID]:
    """Deterministic session ids that the canonical assignment puts in `variant`."""
    out: list[uuid.UUID] = []
    i = 0
    while len(out) < count:
        sid = uuid.uuid5(NAMESPACE, f"{tag}:session:{i}")
        if assign(key, sid, allocation_bp) == variant:
            out.append(sid)
        i += 1
    return out


def telemetry(
    factory: SessionFactory,
    session_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    at: datetime,
    event_id: uuid.UUID | None = None,
) -> Any:
    """One event through the REAL worker code (store -> signals -> exposure)."""
    body = {
        "schema_version": 1,
        "event_id": str(event_id or uuid.uuid4()),
        "event_type": event_type,
        "session_id": str(session_id),
        "occurred_at": at.isoformat(),
        "payload": payload,
    }
    with factory() as session:  # one fresh Session per message, like the worker
        return process_telemetry_message(session, body)


def exposure_payload(experiment: Experiment, variant: Variant) -> dict[str, Any]:
    spec_hash = (
        experiment.candidate_spec_hash if variant == "candidate" else experiment.control_spec_hash
    )
    return {
        "generation": 1 if variant == "candidate" else 0,
        "experiment": experiment.experiment_key,
        "variant": variant,
        "spec_hash": spec_hash,
    }


OUTCOME_ORDER: tuple[MetricName, ...] = (
    "rage_click_session_rate",
    "error_burst_session_rate",
    "form_error_session_rate",
    "signup_submit_session_rate",
)


def seed_outcomes(
    factory: SessionFactory,
    session_ids: Sequence[uuid.UUID],
    outcomes: Mapping[MetricName, int],
    exposed_at: datetime,
) -> None:
    """Outcome events for the first k sessions of each metric (direct inserts + real signals).

    error_burst sessions get 3 form errors, so they also count for form_error:
    datasets must keep form_error >= error_burst.
    """
    rows: list[dict[str, Any]] = []
    needs_signals: set[uuid.UUID] = set()
    burst = outcomes.get("error_burst_session_rate", 0)
    for metric in OUTCOME_ORDER:
        k = outcomes.get(metric, 0)
        for index, sid in enumerate(session_ids[:k]):
            t = exposed_at + timedelta(seconds=5)
            if metric == "rage_click_session_rate":
                needs_signals.add(sid)
                rows += [
                    _row(
                        sid,
                        "button_click",
                        {"component": "plan_team_pro_cta"},
                        t + timedelta(milliseconds=300 * i),
                    )
                    for i in range(4)
                ]
            elif metric == "error_burst_session_rate":
                needs_signals.add(sid)
                rows += [
                    _row(sid, "form_error", _form_error(), t + timedelta(seconds=20 + i))
                    for i in range(3)
                ]
            elif metric == "form_error_session_rate":
                if index >= burst:  # burst sessions already have form errors
                    rows.append(_row(sid, "form_error", _form_error(), t + timedelta(seconds=40)))
            else:
                rows.append(
                    _row(
                        sid,
                        "button_click",
                        {"component": SIGNUP_SUBMIT_COMPONENT},
                        t + timedelta(seconds=60),
                    )
                )
    with factory() as session:
        if rows:
            session.execute(insert(UserEvent), rows)
        session.commit()
    with factory() as session:
        for sid in sorted(needs_signals):
            reconcile_session_signals(session, sid)


def _form_error() -> dict[str, Any]:
    return {"component": SIGNUP_FORM_ID, "field": "email", "reason": "required", "generation": 0}


def _row(sid: uuid.UUID, event_type: str, payload: dict[str, Any], at: datetime) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "event_type": event_type,
        "session_id": sid,
        "occurred_at": at,
        "payload": payload,
    }


# ---- the dataset ------------------------------------------------------------------------------

Kind = Literal[
    "eligibility", "allocation", "exposure", "analysis", "window", "serving", "db_constraint"
]


class Step(BaseModel):
    """One action on a window-case timeline, processed in list order (= arrival order).

    `minute` is the time the thing HAPPENED (T0 + minutes): for lifecycle actions the
    server transition time, for expose/outcome the client occurred_at. A step listed
    after a pause but timed before it models a delayed/redelivered event.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    do: Literal[
        "expose", "rage", "straddle_rage", "form_error", "pause", "resume", "complete", "stop"
    ]
    minute: float
    session: int = Field(default=0, ge=0, le=20)


class ArmSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    exposed: int = Field(ge=0, le=1000)
    outcomes: dict[MetricName, int] = Field(default_factory=dict)
    fallback_sessions: int = Field(default=0, ge=0)


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    created: bool | None = None
    started: bool | None = None
    reasons_include: tuple[str, ...] = ()
    exposures: dict[Variant, int] | None = None
    assessment: str | None = None
    difference_sign: Literal["positive", "negative", "zero"] | None = None
    served: Literal["none", "assigned", "fallback"] | None = None
    accepted: bool | None = None  # allocation / db_constraint
    constraint: str | None = None  # db_constraint: the constraint/trigger that refused it
    successes: dict[MetricName, list[int]] | None = None  # window: [control, candidate]
    boundary_signals_excluded: int | None = None
    windows: int | None = None  # window: number of collection windows in the report


class ExperimentCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9_]{3,48}$")
    kind: Kind
    description: str = Field(min_length=10, max_length=300)
    evaluation: Literal["pass", "reject", "human_review", "missing"] = "pass"
    corrupt: str = "none"
    config: dict[str, Any] = Field(default_factory=dict)
    scenario: str = "default"
    control: ArmSpec | None = None
    candidate: ArmSpec | None = None
    values: tuple[Any, ...] = ()
    fault: bool = False
    timeline: tuple[Step, ...] = ()
    rerun: bool = False
    expected: Expected


class ExperimentDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=1)
    cases: tuple[ExperimentCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique(cls, cases: tuple[ExperimentCase, ...]) -> tuple[ExperimentCase, ...]:
        if len({c.id for c in cases}) != len(cases):
            raise ValueError("golden case ids must be unique")
        return cases


def load_dataset(path: Path = GOLDEN_PATH) -> ExperimentDataset:
    return ExperimentDataset.model_validate_json(path.read_text(encoding="utf-8"))


DEFAULT_CONFIG: dict[str, Any] = {
    "candidate_allocation_bp": 5000,
    "primary_metric": "rage_click_session_rate",
    "guardrail_metrics": ["form_error_session_rate", "signup_submit_session_rate"],
    "minimum_sample_per_variant": 100,
    "traffic_source": "simulated",
}


# ---- running one case -------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    kind: str
    observed: dict[str, Any]
    correct: bool
    fail_open: bool
    failed_checks: list[str] = field(default_factory=list)


def _key(case: ExperimentCase, suffix: str = "") -> str:
    return f"golden_{case.id}{suffix}"[:64]


def _create(
    factory: SessionFactory, case: ExperimentCase, world: World, evaluation_id: uuid.UUID
) -> Any:
    config = {**DEFAULT_CONFIG, **case.config}
    return create_experiment(
        factory,
        experiment_key=_key(case),
        candidate_evaluation_run_id=evaluation_id,
        at=CREATED,
        **config,
    )


def _running(factory: SessionFactory, case: ExperimentCase, world: World) -> Experiment:
    outcome = _create(factory, case, world, world.evaluation_ids["pass"])
    assert outcome.created, f"{case.id}: fixture experiment refused {outcome.reasons}"
    started = start_experiment(factory, outcome.experiment_id, at=STARTED)
    assert started.changed, f"{case.id}: fixture experiment did not start {started.reasons}"
    with factory() as session:
        experiment = session.get(Experiment, outcome.experiment_id)
        assert experiment is not None
        session.expunge(experiment)
        return experiment


def _direct_draft(
    factory: SessionFactory, case: ExperimentCase, world: World, **overrides: Any
) -> uuid.UUID:
    """A draft row inserted WITHOUT the service: a request that merely claims eligibility."""
    with factory() as session:
        control = session.get(UISpecVersion, world.control_id)
        candidate = session.get(UISpecVersion, world.candidate_ids["pass"])
        run = session.get(CandidateEvaluationRun, world.evaluation_ids["pass"])
        mutation = session.get(MutationRun, run.mutation_run_id if run else None)
        assert control and candidate and run and mutation
        decision = session.get(DecisionRun, mutation.decision_run_id)
        assert decision is not None
        values: dict[str, Any] = {
            "id": _uid(case.id, "direct-draft"),
            "experiment_key": _key(case),
            "page_id": world.page_id,
            "candidate_evaluation_run_id": run.id,
            "mutation_run_id": mutation.id,
            "hypothesis_id": decision.hypothesis_id,
            "control_spec_id": control.id,
            "candidate_spec_id": candidate.id,
            "control_spec_hash": control.content_hash,
            "candidate_spec_hash": candidate.content_hash,
            "control_allocation_bp": 5000,
            "candidate_allocation_bp": 5000,
            "primary_metric": "rage_click_session_rate",
            "guardrail_metrics": ["form_error_session_rate"],
            "minimum_sample_per_variant": 100,
            "traffic_source": "simulated",
            "status": "draft",
            "status_changed_at": CREATED,
        } | overrides
        session.execute(insert(Experiment).values(**values))
        session.commit()
        return uuid.UUID(str(values["id"]))


def _forge_evaluation(
    factory: SessionFactory, world: World, candidate_id: uuid.UUID, tag: str, **overrides: Any
) -> uuid.UUID:
    """A CandidateEvaluationRun row that CLAIMS pass (never produced by the evaluator)."""
    with factory() as session:
        run_id = session.scalar(
            select(MutationRun.id).where(MutationRun.candidate_spec_id == candidate_id)
        )
        row_id = _uid(tag, "forged-evaluation")
        values: dict[str, Any] = {
            "id": row_id,
            "candidate_spec_id": candidate_id,
            "mutation_run_id": run_id,
            "evaluator_version": "candidate_eval.v1",
            "harness_version": "sandbox_harness.v1",
            "status": "completed",
            "recommendation": "pass",
            "reason_codes": ["all_gates_passed"],
            "category_results": {
                n: {"status": "pass"}
                for n in (
                    "schema",
                    "render",
                    "functional",
                    "accessibility",
                    "regression",
                    "ux_intent",
                    "performance",
                )
            },
            "error_type": None,
            "duration_ms": 1.0,
        } | overrides
        session.execute(insert(CandidateEvaluationRun).values(**values))
        session.commit()
        return row_id


def _tampered_candidate(factory: SessionFactory, world: World, tag: str) -> uuid.UUID:
    """A candidate row whose stored hash does not match its content."""
    with factory() as session:
        gen0 = dict(load_generation_zero())
        spec = _set(gen0, "plan_team_pro_cta", "feedback", "immediate")
        spec["page"]["title"] = "Tampered after evaluation"
        row = UISpecVersion(
            id=_uid(tag, "tampered"),
            page_id=world.page_id,
            status="candidate",
            candidate_for_generation=1,
            parent_id=world.control_id,
            schema_version=1,
            spec=spec,
            content_hash=hashlib.sha256(b"claimed, not computed").hexdigest(),
            source="experiment-eval-fixture",
        )
        session.add(row)
        session.flush()
        session.add(
            MutationRun(
                id=_uid(tag, "tampered-run"),
                decision_run_id=world.decision_run_id,
                source_spec_id=world.control_id,
                candidate_spec_id=row.id,
                generator="fixture",
                generator_version="experiment-eval-fixture",
                request_version="mutation_request.v1",
                request_hash=hashlib.sha256(b"tampered").hexdigest(),
                status="succeeded",
                validation_errors=[],
                mutation_spec={"version": 1, "operations": []},
            )
        )
        session.commit()
        return row.id


def run_eligibility(factory: SessionFactory, case: ExperimentCase, world: World) -> dict[str, Any]:
    reasons: list[str] = []
    created = started = False
    experiment_id: uuid.UUID | None = None
    c = case.corrupt
    if c in ("draft_claims_other_candidate", "draft_control_not_gen0"):
        with factory() as session:
            other = session.get(UISpecVersion, world.candidate_ids["human_review"])
            assert other is not None
            override: dict[str, Any] = (
                {"candidate_spec_id": other.id, "candidate_spec_hash": other.content_hash}
                if c == "draft_claims_other_candidate"
                else {"control_spec_hash": hashlib.sha256(b"x").hexdigest()}
            )
        experiment_id = _direct_draft(factory, case, world, **override)
        created = True
    else:
        if case.evaluation == "missing":
            evaluation_id = uuid.uuid5(NAMESPACE, f"{case.id}:missing")
        elif c == "forged_pass_on_tampered_candidate":
            evaluation_id = _forge_evaluation(
                factory, world, _tampered_candidate(factory, world, case.id), case.id
            )
        elif c == "forged_pass_category_fail":
            evaluation_id = _forge_evaluation(
                factory,
                world,
                world.candidate_ids["reject"],
                case.id,
                category_results={"ux_intent": {"status": "fail"}},
            )
        else:
            evaluation_id = world.evaluation_ids[case.evaluation]
        if c == "newer_non_pass":
            _forge_evaluation(
                factory,
                world,
                world.candidate_ids["pass"],
                case.id + "_newer",
                status="completed",
                recommendation="reject",
                reason_codes=["ux_intent_regression"],
                created_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        outcome = _create(factory, case, world, evaluation_id)
        created, reasons, experiment_id = (
            outcome.created,
            list(outcome.reasons),
            outcome.experiment_id,
        )
        if outcome.detail:
            reasons.append(outcome.detail)
    if created and experiment_id is not None:
        if c == "another_active":
            other = _create(
                factory,
                case.model_copy(update={"id": case.id + "_first"}),
                world,
                world.evaluation_ids["pass"],
            )
            assert other.created and start_experiment(factory, other.experiment_id).changed
        result = start_experiment(factory, experiment_id)
        started = result.changed
        reasons += list(result.reasons)
        if c == "start_twice" and started:
            again = start_experiment(factory, experiment_id)
            started = again.changed
            reasons += list(again.reasons)
    return {"created": created, "started": started, "reasons": reasons}


def run_allocation(case: ExperimentCase) -> dict[str, Any]:
    key = "golden_allocation"
    sessions = [uuid.uuid5(NAMESPACE, f"alloc:{i}") for i in range(20_000)]
    if case.scenario == "invalid_values":
        accepted = []
        for value in case.values:
            try:
                validate_allocation(value)
                accepted.append(value)
            except AllocationError:
                pass
        return {"accepted_invalid": accepted, "accepted": bool(accepted)}
    if case.scenario == "allowed_values":
        accepted = [v for v in CANDIDATE_ALLOCATIONS_BP if validate_allocation(v) == v]
        shares = {}
        for bp in CANDIDATE_ALLOCATIONS_BP:
            share = sum(assign(key, s, bp) == "candidate" for s in sessions) / len(sessions)
            shares[bp] = round(share, 4)
        within = all(abs(shares[bp] - bp / TOTAL_BUCKETS) < 0.01 for bp in shares)
        return {
            "accepted": len(accepted) == len(CANDIDATE_ALLOCATIONS_BP) and within,
            "shares": shares,
        }
    if case.scenario == "deterministic":
        first = [assign(key, s, 1000) for s in sessions[:2000]]
        second = [assign(key, s, 1000) for s in sessions[:2000]]
        return {"accepted": first == second}
    if case.scenario == "same_session":
        sid = sessions[0]
        return {"accepted": len({assign(key, sid, 2500) for _ in range(100)}) == 1}
    if case.scenario == "independent_experiments":
        a = [assign("golden_alpha", s, 5000) for s in sessions[:5000]]
        b = [assign("golden_beta", s, 5000) for s in sessions[:5000]]
        agreement = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
        return {"accepted": 0.45 < agreement < 0.55, "agreement": round(agreement, 4)}
    if case.scenario == "boundary":
        violations = sum(
            (assign(key, s, 1000) == "candidate") != (bucket(key, s) < 1000) for s in sessions
        )
        over = sum(assign(key, s, 1000) == "candidate" and bucket(key, s) >= 1000 for s in sessions)
        return {"accepted": violations == 0, "candidate_outside_allocation": over}
    raise ValueError(f"unknown allocation scenario {case.scenario}")


def _exposure_counts(factory: SessionFactory, experiment_id: uuid.UUID) -> dict[str, int]:
    with factory() as session:
        rows = session.execute(
            select(ExperimentExposure.variant, func.count())
            .where(ExperimentExposure.experiment_id == experiment_id)
            .group_by(ExperimentExposure.variant)
        ).all()
    counts = {"control": 0, "candidate": 0}
    counts.update({v: n for v, n in rows})
    return counts


def run_exposure(factory: SessionFactory, case: ExperimentCase, world: World) -> dict[str, Any]:
    if case.scenario == "not_running":
        outcome = _create(factory, case, world, world.evaluation_ids["pass"])
        with factory() as session:
            experiment = session.get(Experiment, outcome.experiment_id)
            assert experiment is not None
            session.expunge(experiment)
    else:
        experiment = _running(factory, case, world)
    key, bp = experiment.experiment_key, experiment.candidate_allocation_bp
    control = sessions_for(key, bp, "control", 5, case.id)
    candidate = sessions_for(key, bp, "candidate", 5, case.id)
    with factory() as session:  # every session asks to be assigned ...
        for sid in control + candidate:
            resolve_variant(session, sid, world.page_id)
    # ... but only these report a successful render (each event through the real worker).
    if case.scenario in ("exposed_after_render", "duplicate", "not_running"):
        repeats = 3 if case.scenario == "duplicate" else 1
        pairs: list[tuple[uuid.UUID, Variant]] = [(s, "control") for s in control]
        pairs += [(s, "candidate") for s in candidate]
        for sid, variant in pairs:
            for r in range(repeats):
                at = T0 + timedelta(seconds=r)
                telemetry(factory, sid, EXPOSURE_EVENT, exposure_payload(experiment, variant), at)
    if case.scenario == "wrong_variant":
        payload = exposure_payload(experiment, "candidate")
        telemetry(factory, control[0], EXPOSURE_EVENT, payload, T0)
    if case.scenario == "wrong_hash":
        payload = exposure_payload(experiment, "control") | {"spec_hash": "0" * 64}
        telemetry(factory, control[0], EXPOSURE_EVENT, payload, T0)
    return {"exposures": _exposure_counts(factory, experiment.id)}


def run_analysis(factory: SessionFactory, case: ExperimentCase, world: World) -> dict[str, Any]:
    experiment = _running(factory, case, world)
    key, bp = experiment.experiment_key, experiment.candidate_allocation_bp
    assert case.control is not None and case.candidate is not None
    arms: tuple[tuple[Variant, ArmSpec], ...] = (
        ("control", case.control),
        ("candidate", case.candidate),
    )
    for variant, arm in arms:
        total = arm.exposed + arm.fallback_sessions
        sids = sessions_for(key, bp, variant, total, f"{case.id}:{variant}")
        exposed, fallback = sids[: arm.exposed], sids[arm.exposed :]
        for sid in exposed:
            telemetry(factory, sid, EXPOSURE_EVENT, exposure_payload(experiment, variant), T0)
        for sid in fallback:
            payload = {"generation": 0, "experiment": key, "reason": "render_error"}
            telemetry(factory, sid, FALLBACK_EVENT, payload, T0)
        seed_outcomes(factory, exposed, arm.outcomes, T0)

    def boom() -> None:
        raise RuntimeError("simulated analysis failure")

    result = analyze_experiment(factory, experiment.id, AS_OF, fault=boom if case.fault else None)
    with factory() as session:
        status_after = session.scalar(
            select(Experiment.status).where(Experiment.id == experiment.id)
        )
        baselines = session.scalar(
            select(func.count())
            .select_from(UISpecVersion)
            .where(UISpecVersion.page_id == world.page_id, UISpecVersion.status == "baseline")
        )
    counts: dict[str, list[int]] = {}
    for block in [result.report.get("primary")] + list(result.report.get("guardrails", [])):
        if block:
            counts[block["metric"]] = [
                block["control"]["successes"],
                block["candidate"]["successes"],
            ]
    primary = result.report.get("primary", {})
    d = primary.get("absolute_difference")
    sign = None if d is None else "positive" if d > 0 else "negative" if d < 0 else "zero"
    return {
        "assessment": result.assessment,
        "reasons": list(result.reason_codes),
        "difference_sign": sign,
        "exposures": result.report.get("exposed_sessions"),
        "successes": counts,
        "report_hash": result.report_hash,
        "status_after": status_after,
        "baselines_after": baselines,
    }


def run_window(factory: SessionFactory, case: ExperimentCase, world: World) -> dict[str, Any]:
    """A pause/resume/terminal timeline through the real service, worker and analysis."""
    experiment = _running(factory, case, world)
    sids = sessions_for(experiment.experiment_key, 5000, "candidate", 21, case.id)
    for step in case.timeline:
        at = T0 + timedelta(minutes=step.minute)
        sid = sids[step.session]
        if step.do in ("pause", "resume", "complete", "stop"):
            if step.do == "pause":
                result = pause_experiment(factory, experiment.id, at=at)
            elif step.do == "resume":
                result = start_experiment(factory, experiment.id, at=at)
            elif step.do == "complete":
                result = complete_experiment(factory, experiment.id, at=at)
            else:
                result = stop_experiment(factory, experiment.id, "human_decision", at=at)
            assert result.changed, f"{case.id}: {step.do} refused {result.reasons}"
        elif step.do == "expose":
            payload = exposure_payload(experiment, "candidate")
            telemetry(factory, sid, EXPOSURE_EVENT, payload, at)
        elif step.do in ("rage", "straddle_rage"):
            start = at - timedelta(milliseconds=600) if step.do == "straddle_rage" else at
            for i in range(4):  # 4 clicks within 1.2 s -> one canonical rage_click signal
                click = {"generation": 1, "component": "plan_team_pro_cta"}
                telemetry(
                    factory, sid, "button_click", click, start + timedelta(milliseconds=400 * i)
                )
        else:
            telemetry(factory, sid, "form_error", _form_error() | {"generation": 1}, at)
    first = analyze_experiment(factory, experiment.id, AS_OF)
    observed: dict[str, Any] = {
        "exposures": first.report["exposed_sessions"],
        "successes": {
            block["metric"]: [block["control"]["successes"], block["candidate"]["successes"]]
            for block in [first.report["primary"], *first.report["guardrails"]]
        },
        "boundary_signals_excluded": first.report["integrity"]["boundary_signals_excluded"],
        "windows": len(first.report["collection_windows"]),
        "report_hash": first.report_hash,
    }
    if case.rerun:
        second = analyze_experiment(factory, experiment.id, AS_OF)
        observed["rerun_identical"] = second.report_hash == first.report_hash
    return observed


def run_serving(factory: SessionFactory, case: ExperimentCase, world: World) -> dict[str, Any]:
    key = _key(case)
    if case.scenario == "none_running":
        with factory() as session:
            served = resolve_variant(session, uuid.uuid4(), world.page_id)
        return {"served": served.status, "variant": served.variant}
    if case.scenario == "assigned":
        experiment = _running(factory, case, world)
        sid = sessions_for(key, experiment.candidate_allocation_bp, "candidate", 1, case.id)[0]
        with factory() as session:
            served = resolve_variant(session, sid, world.page_id)
        ok = served.spec_hash == experiment.candidate_spec_hash and served.spec is not None
        return {"served": served.status if ok else "wrong_spec", "variant": served.variant}
    # running, but the stored candidate hash does not match the spec row (inserted directly)
    with factory() as session:
        candidate = session.get(UISpecVersion, world.candidate_ids["pass"])
        assert candidate is not None
    overrides: dict[str, Any] = {}
    if case.scenario == "candidate_hash_mismatch":
        overrides["candidate_spec_hash"] = hashlib.sha256(b"not the evaluated spec").hexdigest()
    experiment_id = _direct_draft(factory, case, world, **overrides)
    with factory() as session:  # started WITHOUT the start gate (which would refuse it)
        session.execute(
            text(
                "UPDATE experiment SET status = 'running', started_at = :t, "
                "status_changed_at = :t WHERE id = :id"
            ),
            {"t": STARTED, "id": experiment_id},
        )
        session.commit()
    sid = sessions_for(key, 5000, "candidate", 1, case.id)[0]
    with factory() as session:
        served = resolve_variant(session, sid, world.page_id)
    return {"served": served.status, "variant": served.variant, "reason": served.reason}


def run_db_constraint(
    factory: SessionFactory, connection: Connection, case: ExperimentCase, world: World
) -> dict[str, Any]:
    accepted = True
    savepoint = connection.begin_nested()
    try:
        if case.scenario == "update_running_allocation":
            experiment = _running(factory, case, world)
            connection.execute(
                text(
                    "UPDATE experiment SET candidate_allocation_bp = 100, "
                    "control_allocation_bp = 9900 WHERE id = :id"
                ),
                {"id": experiment.id},
            )
        elif case.scenario == "update_analysis":
            experiment = _running(factory, case, world)
            analysis = analyze_experiment(factory, experiment.id, AS_OF)
            connection.execute(
                text("UPDATE experiment_analysis SET assessment = 'evidence_ready' WHERE id = :id"),
                {"id": analysis.analysis_id},
            )
        elif case.scenario == "completed_to_running":
            experiment = _running(factory, case, world)
            connection.execute(
                text(
                    "UPDATE experiment SET status='completed', stopped_at=:t, "
                    "status_changed_at=:t WHERE id=:id"
                ),
                {"id": experiment.id, "t": T0 + timedelta(minutes=5)},
            )
            connection.execute(
                text(
                    "UPDATE experiment SET status='running', stopped_at=NULL, "
                    "status_changed_at=:t WHERE id=:id"
                ),
                {"id": experiment.id, "t": T0 + timedelta(minutes=6)},
            )
        elif case.scenario == "duplicate_transition":
            experiment = _running(factory, case, world)
            connection.execute(
                text("UPDATE experiment SET status='running', status_changed_at=:t WHERE id=:id"),
                {"id": experiment.id, "t": T0 + timedelta(minutes=5)},
            )
        elif case.scenario == "time_not_increasing":
            experiment = _running(factory, case, world)
            connection.execute(
                text("UPDATE experiment SET status='paused', status_changed_at=:t WHERE id=:id"),
                {"id": experiment.id, "t": STARTED - timedelta(seconds=1)},
            )
        elif case.scenario == "insert_running":
            _direct_draft(factory, case, world, status="running", started_at=STARTED)
        elif case.scenario == "forge_lifecycle_event":
            experiment = _running(factory, case, world)  # history: draft, running
            connection.execute(  # a fake pause that never happened
                text(
                    "INSERT INTO experiment_lifecycle_event "
                    "(id, experiment_id, sequence, from_status, to_status, occurred_at) "
                    "VALUES (gen_random_uuid(), :id, 2, 'running', 'paused', :t)"
                ),
                {"id": experiment.id, "t": T0 + timedelta(minutes=5)},
            )
        elif case.scenario == "update_lifecycle_event":
            experiment = _running(factory, case, world)
            connection.execute(
                text(
                    "UPDATE experiment_lifecycle_event SET occurred_at = :t "
                    "WHERE experiment_id = :id AND sequence = 1"
                ),
                {"id": experiment.id, "t": T0 - timedelta(days=30)},
            )
        elif case.scenario == "delete_lifecycle_event":
            experiment = _running(factory, case, world)
            connection.execute(
                text("DELETE FROM experiment_lifecycle_event WHERE experiment_id = :id"),
                {"id": experiment.id},
            )
        else:
            _direct_draft(factory, case, world, **case.config)
    except DBAPIError as error:
        accepted = False
        diag = getattr(error.orig, "diag", None)
        name = getattr(diag, "constraint_name", None)
        message = str(getattr(diag, "message_primary", "") or "")
        constraint = name or ("trigger:" + message.split(" (id")[0])
    else:
        constraint = None
    finally:
        savepoint.rollback()
    return {"accepted": accepted, "constraint": constraint}


PERMISSIVENESS = {
    "stop_recommended": 0,
    "needs_review": 1,
    "insufficient_data": 2,
    "evidence_ready": 3,
}


def score(case: ExperimentCase, observed: dict[str, Any]) -> CaseResult:
    e = case.expected
    failed: list[str] = []
    fail_open = False
    if e.created is not None and observed.get("created") != e.created:
        failed.append("created")
        fail_open |= bool(observed.get("created")) and not e.created
    if e.started is not None and observed.get("started") != e.started:
        failed.append("started")
        fail_open |= bool(observed.get("started")) and not e.started
    for reason in e.reasons_include:
        if reason not in observed.get("reasons", []):
            failed.append(f"reason:{reason}")
    if e.exposures is not None:
        got = observed.get("exposures") or {}
        if dict(got) != dict(e.exposures):
            failed.append("exposures")
            fail_open |= any(got.get(v, 0) > n for v, n in e.exposures.items())
    if e.assessment is not None and observed.get("assessment") != e.assessment:
        failed.append("assessment")
        fail_open |= (
            PERMISSIVENESS.get(observed.get("assessment", ""), 9) > PERMISSIVENESS[e.assessment]
        )
    if e.difference_sign is not None and observed.get("difference_sign") != e.difference_sign:
        failed.append("difference_sign")
    if e.served is not None and observed.get("served") != e.served:
        failed.append("served")
        fail_open |= observed.get("served") == "assigned"
    if e.accepted is not None and observed.get("accepted") != e.accepted:
        failed.append("accepted")
        fail_open |= bool(observed.get("accepted")) and not e.accepted
    if case.kind == "analysis" and not case.fault and case.control and case.candidate:
        # The counts must be exactly what was seeded: proves the event AND signal paths.
        for metric, (got_control, got_candidate) in observed.get("successes", {}).items():
            want = [case.control.outcomes.get(metric, 0), case.candidate.outcomes.get(metric, 0)]
            if [got_control, got_candidate] != want:
                failed.append(f"successes:{metric}")
    if e.successes is not None:
        got = observed.get("successes", {})
        for metric, want in e.successes.items():
            if got.get(metric) != want:
                failed.append(f"successes:{metric}")
                fail_open |= any(g > w for g, w in zip(got.get(metric, want), want, strict=True))
    if (
        e.boundary_signals_excluded is not None
        and observed.get("boundary_signals_excluded") != e.boundary_signals_excluded
    ):
        failed.append("boundary_signals_excluded")
    if e.windows is not None and observed.get("windows") != e.windows:
        failed.append("windows")
    if case.rerun and observed.get("rerun_identical") is not True:
        failed.append("rerun_identical")
    if e.constraint is not None and observed.get("constraint") != e.constraint:
        failed.append("constraint")
    if case.kind == "analysis":  # never an automatic promotion, whatever the numbers
        if observed.get("status_after") != "running" or observed.get("baselines_after") != 1:
            failed.append("promotion_side_effect")
            fail_open = True
    return CaseResult(case.id, case.kind, observed, not failed, fail_open, failed)


def _savepointed(connection: Connection, work: Callable[[], Any]) -> Any:
    savepoint = connection.begin_nested()
    try:
        return work()
    finally:
        savepoint.rollback()


def run_evaluation(
    engine: Engine, dataset: ExperimentDataset, facts: Mapping[str, SpecFacts] | None = None
) -> list[CaseResult]:
    facts = facts if facts is not None else harness_facts()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

            results = []
            for case in dataset.cases:

                def work(case: ExperimentCase = case) -> CaseResult:
                    if case.kind == "allocation":
                        return score(case, run_allocation(case))
                    world = build_world(factory, case.id, facts)
                    runner = {
                        "eligibility": run_eligibility,
                        "exposure": run_exposure,
                        "analysis": run_analysis,
                        "window": run_window,
                        "serving": run_serving,
                    }.get(case.kind)
                    if runner is None:
                        observed = run_db_constraint(factory, connection, case, world)
                    else:
                        observed = runner(factory, case, world)
                    return score(case, observed)

                results.append(_savepointed(connection, work))
            return results
        finally:
            transaction.rollback()


def metrics(results: Sequence[CaseResult]) -> dict[str, Any]:
    def rate(kind: str) -> dict[str, Any]:
        rows = [r for r in results if r.kind == kind]
        passed = sum(r.correct for r in rows)
        return {
            "passed": passed,
            "of": len(rows),
            "rate": round(passed / len(rows), 4) if rows else None,
        }

    correct = sum(r.correct for r in results)
    return {
        "cases_meeting_all_expectations": {
            "passed": correct,
            "of": len(results),
            "rate": round(correct / len(results), 4),
        },
        **{
            f"{kind}_accuracy": rate(kind)
            for kind in (
                "eligibility",
                "allocation",
                "exposure",
                "analysis",
                "window",
                "serving",
                "db_constraint",
            )
        },
        "automatic_promotions": sum(
            1 for r in results if "promotion_side_effect" in r.failed_checks
        ),
        "fail_open_count": sum(r.fail_open for r in results),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden experiment evaluation.")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    dataset = load_dataset()
    engine = create_db_engine(str(Settings().database_url))
    try:
        results = run_evaluation(engine, dataset)
    except HarnessError as error:
        print(f"Cannot run the sandbox harness: {error.code}")
        return 1
    finally:
        engine.dispose()
    m = metrics(results)
    data = {
        "schema": "darwinux.experiment-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "analysis_version": "experiment_analysis.v1",
        "metrics": m,
        "cases": [asdict(r) for r in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"golden: {len(dataset.cases)} cases   experiment_analysis.v1")
    for r in results:
        mark = "ok  " if r.correct else "FAIL"
        extra = f"  failed: {','.join(r.failed_checks)}" if r.failed_checks else ""
        summary = r.observed.get("assessment") or r.observed.get("served") or ""
        print(f"  {mark} {r.kind:<13} {r.id:<40} {summary}{extra}")
    for name, v in m.items():
        if isinstance(v, dict):
            print(f"{name:<34} {v['passed']}/{v['of']}  ({v['rate']})")
    print(f"{'automatic_promotions':<34} {m['automatic_promotions']}")
    print(f"{'FAIL_OPEN_COUNT':<34} {m['fail_open_count']}")
    print(f"report: {args.output.relative_to(REPO_ROOT)}")
    return 0 if m["fail_open_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
