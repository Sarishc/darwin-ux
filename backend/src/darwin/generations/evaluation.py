"""Golden promotion / rollback evaluation (make promotion-eval). Rolled back; nothing persists.

    python -m darwin.generations.evaluation [--output artifacts/promotion-eval.json]

Every case runs in its own SAVEPOINT on its own page ("exp_<case>"): a private
Generation 0 baseline (pointer bootstrapped), three REAL Step 13 evaluated candidates
(pass / reject / human_review; the harness runs once) and, where the scenario needs
one, a completed Step 14 experiment with an immutable analysis. Scenarios then try to
approve, promote, roll back, or forge the pointer, and record what happened.

Safety metrics:
  UNAUTHORIZED_PROMOTION_COUNT  a generation became active (or a promoted row / promotion
                                record appeared) when the case required a refusal
  INVALID_ROLLBACK_COUNT        the pointer moved backwards when the case required a
                                refusal, or to something that is not an earlier generation
Both must be 0.
"""

import argparse
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, Engine, func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from darwin.api.generations import active_generation
from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import (
    ActiveGeneration,
    BehaviorSignal,
    CandidateEvaluationRun,
    Experiment,
    ExperimentExposure,
    GenerationPromotion,
    UISpecVersion,
    UserEvent,
)
from darwin.experiments.evaluation import (
    AS_OF,
    CREATED,
    STARTED,
    T0,
    World,
    build_world,
    exposure_payload,
    harness_facts,
    seed_outcomes,
    sessions_for,
    telemetry,
)
from darwin.experiments.service import (
    analyze_experiment,
    complete_experiment,
    create_experiment,
    pause_experiment,
    start_experiment,
)
from darwin.logging_config import configure_logging
from darwin.memory.corpus import REPO_ROOT
from darwin.mutations.apply import content_hash
from darwin.sandbox.harness import HarnessError, SpecFacts

from .service import bootstrap_active, decide, promote, rollback

GOLDEN_PATH = REPO_ROOT / "backend" / "tests" / "evals" / "golden" / "promotions.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "promotion-eval.json"
REVIEWER = "golden-reviewer"
REASON = "Golden case."
COMPLETED = T0 + timedelta(minutes=90)
SessionFactory = Callable[[], Session]

CLEAN = {
    "control": {
        "rage_click_session_rate": 48,
        "form_error_session_rate": 12,
        "signup_submit_session_rate": 40,
    },
    "candidate": {
        "rage_click_session_rate": 12,
        "form_error_session_rate": 12,
        "signup_submit_session_rate": 40,
    },
}


# ---- building blocks -------------------------------------------------------------------------


def _seed_arm(
    factory: SessionFactory,
    experiment: Experiment,
    variant: str,
    count: int,
    outcomes: Mapping[str, int],
    tag: str,
) -> None:
    """Exposed sessions inserted directly (valid by construction), outcomes via seed_outcomes."""
    sids = sessions_for(experiment.experiment_key, 5000, variant, count, tag)  # type: ignore[arg-type]
    payload = exposure_payload(experiment, variant)  # type: ignore[arg-type]
    events, exposures = [], []
    for sid in sids:
        event_id = uuid.uuid4()
        events.append(
            {
                "id": uuid.uuid4(),
                "event_id": event_id,
                "event_type": "experiment_exposure",
                "session_id": sid,
                "occurred_at": T0,
                "payload": payload,
            }
        )
        exposures.append(
            {
                "id": uuid.uuid4(),
                "experiment_id": experiment.id,
                "session_id": sid,
                "variant": variant,
                "spec_hash": payload["spec_hash"],
                "event_id": event_id,
                "exposed_at": T0,
            }
        )
    with factory() as session:
        session.execute(insert(UserEvent), events)
        session.execute(insert(ExperimentExposure), exposures)
        session.commit()
    seed_outcomes(factory, sids, outcomes, T0)  # type: ignore[arg-type]


def _experiment(
    factory: SessionFactory,
    world: World,
    key: str,
    label: str = "pass",
    arms: Mapping[str, Mapping[str, int]] | None = None,
    per_arm: int = 120,
    finish: str = "complete",  # complete | running | paused
    fallbacks: int = 0,
    analyze: bool = True,
    fault: bool = False,
) -> tuple[Experiment, uuid.UUID | None]:
    created = create_experiment(
        factory,
        experiment_key=key,
        candidate_evaluation_run_id=world.evaluation_ids[label],
        candidate_allocation_bp=5000,
        primary_metric="rage_click_session_rate",
        guardrail_metrics=["form_error_session_rate", "signup_submit_session_rate"],
        minimum_sample_per_variant=100,
        traffic_source="simulated",
        at=CREATED,
    )
    assert created.created, f"{key}: {created.reasons}"
    assert start_experiment(factory, created.experiment_id, at=STARTED).changed  # type: ignore[arg-type]
    with factory() as session:
        experiment = session.get(Experiment, created.experiment_id)
        assert experiment is not None
        session.expunge(experiment)
    for variant, outcomes in (arms or CLEAN).items():
        _seed_arm(factory, experiment, variant, per_arm, outcomes, f"{key}:{variant}")
    if fallbacks:
        for sid in sessions_for(key, 5000, "candidate", fallbacks, f"{key}:fallback"):
            payload = {"generation": 0, "experiment": key, "reason": "render_error"}
            telemetry(factory, sid, "experiment_fallback", payload, T0)
    if finish == "complete":
        assert complete_experiment(factory, experiment.id, at=COMPLETED).changed
    elif finish == "paused":
        assert pause_experiment(factory, experiment.id, at=COMPLETED).changed
    if not analyze:
        return experiment, None

    def boom() -> None:
        raise RuntimeError("simulated analysis failure")

    analysis = analyze_experiment(factory, experiment.id, AS_OF, fault=boom if fault else None)
    return experiment, analysis.analysis_id


def _forged_experiment(
    factory: SessionFactory, world: World, key: str, **overrides: Any
) -> tuple[Experiment, uuid.UUID]:
    """An experiment row inserted WITHOUT create/start gates (a request that merely claims
    eligibility), then moved through the lifecycle directly and analysed for real."""
    with factory() as session:
        control = session.get(UISpecVersion, world.control_id)
        candidate = session.get(UISpecVersion, world.candidate_ids["pass"])
        run = session.get(CandidateEvaluationRun, world.evaluation_ids["pass"])
        assert control and candidate and run and run.mutation_run_id
        from darwin.db.models import DecisionRun, MutationRun

        mutation = session.get(MutationRun, run.mutation_run_id)
        decision = session.get(DecisionRun, mutation.decision_run_id if mutation else None)
        assert decision is not None
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "experiment_key": key,
            "page_id": world.page_id,
            "candidate_evaluation_run_id": run.id,
            "mutation_run_id": run.mutation_run_id,
            "hypothesis_id": decision.hypothesis_id,
            "control_spec_id": control.id,
            "candidate_spec_id": candidate.id,
            "control_spec_hash": control.content_hash,
            "candidate_spec_hash": candidate.content_hash,
            "control_allocation_bp": 5000,
            "candidate_allocation_bp": 5000,
            "primary_metric": "rage_click_session_rate",
            "guardrail_metrics": ["form_error_session_rate", "signup_submit_session_rate"],
            "minimum_sample_per_variant": 100,
            "traffic_source": "simulated",
            "status": "draft",
            "status_changed_at": CREATED,
        } | overrides
        session.execute(insert(Experiment).values(**values))
        session.execute(
            text(
                "UPDATE experiment SET status='running', started_at=:t, status_changed_at=:t "
                "WHERE id=:id"
            ),
            {"t": STARTED, "id": values["id"]},
        )
        session.commit()
        experiment = session.get(Experiment, values["id"])
        assert experiment is not None
        session.expunge(experiment)
    for variant, outcomes in CLEAN.items():
        _seed_arm(factory, experiment, variant, 120, outcomes, f"{key}:{variant}")
    with factory() as session:
        session.execute(
            text(
                "UPDATE experiment SET status='completed', stopped_at=:t, status_changed_at=:t, "
                "stop_reason='planned_end' WHERE id=:id"
            ),
            {"t": COMPLETED, "id": experiment.id},
        )
        session.commit()
    return experiment, analyze_experiment(factory, experiment.id, AS_OF).analysis_id


def _state(factory: SessionFactory, page: str) -> dict[str, Any]:
    with factory() as session:
        pointer = session.get(ActiveGeneration, page)
        promoted = session.scalar(
            select(func.count())
            .select_from(UISpecVersion)
            .where(UISpecVersion.page_id == page, UISpecVersion.status == "promoted")
        )
        promotions = session.scalar(
            select(func.count())
            .select_from(GenerationPromotion)
            .where(GenerationPromotion.page_id == page)
        )
        return {
            "active_generation": pointer.generation if pointer else None,
            "active_spec_id": str(pointer.ui_spec_version_id) if pointer else None,
            "promoted_rows": promoted,
            "promotion_records": promotions,
        }


def _approve(factory: SessionFactory, analysis_id: uuid.UUID) -> Any:
    return decide(factory, analysis_id, "approve", REVIEWER, REASON)


def _promote(factory: SessionFactory, approval_id: uuid.UUID, page: str, target: int = 1) -> Any:
    return promote(factory, approval_id, REVIEWER, f"{page}:{target}")


def _forge_evaluation(
    factory: SessionFactory, candidate_id: uuid.UUID, mutation_run_id: uuid.UUID | None, **kw: Any
) -> None:
    with factory() as session:
        session.execute(
            insert(CandidateEvaluationRun).values(
                id=uuid.uuid4(),
                candidate_spec_id=candidate_id,
                mutation_run_id=mutation_run_id,
                evaluator_version="candidate_eval.v1",
                harness_version="sandbox_harness.v1",
                status="completed",
                recommendation="reject",
                reason_codes=["ux_intent_regression"],
                category_results={},
                error_type=None,
                duration_ms=1.0,
                created_at=datetime.now(UTC) + timedelta(minutes=5),
                **kw,
            )
        )
        session.commit()


# ---- scenarios ---------------------------------------------------------------------------------

Observed = dict[str, Any]


def s_approve_clean(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    outcome = _approve(f, analysis)  # type: ignore[arg-type]
    return {"approved": outcome.recorded, "reasons": list(outcome.blocking), **_state(f, w.page_id)}


def s_promote_clean(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    result = _promote(f, approval.approval_id, w.page_id)
    state = _state(f, w.page_id)
    with f() as session:
        promoted = session.get(UISpecVersion, result.to_spec_id) if result.changed else None
        candidate = session.get(UISpecVersion, w.candidate_ids["pass"])
        gen0 = session.get(UISpecVersion, w.control_id)
        assert candidate is not None and gen0 is not None
        checks = {
            "promoted_is_generation_1": promoted is not None
            and (promoted.status, promoted.generation) == ("promoted", 1),
            "content_equals_candidate_except_generation": promoted is not None
            and {k: v for k, v in promoted.spec.items() if k != "generation"}
            == {k: v for k, v in candidate.spec.items() if k != "generation"},
            "promoted_parent_is_candidate": promoted is not None
            and promoted.parent_id == candidate.id,
            "candidate_row_unchanged": candidate.status == "candidate"
            and content_hash(candidate.spec) == candidate.content_hash,
            "generation_0_preserved": gen0.status == "baseline"
            and gen0.generation == 0
            and content_hash(gen0.spec) == gen0.content_hash,
            "pointer_names_promotion": state["active_spec_id"] == str(result.to_spec_id),
        }
    return {
        "approved": approval.recorded,
        "promoted": result.changed,
        "reasons": list(result.reasons),
        "checks": checks,
        **state,
    }


def _approve_and_try(f: SessionFactory, w: World, analysis: uuid.UUID | None) -> Observed:
    """Approve; if an approval was (wrongly or rightly) recorded, also TRY to promote, so a
    gate that fails open shows up as an unauthorized promotion, not only a wrong verdict."""
    assert analysis is not None
    outcome = _approve(f, analysis)
    promoted = False
    if outcome.recorded:
        promoted = _promote(f, outcome.approval_id, w.page_id).changed
    return {
        "approved": outcome.recorded,
        "promoted": promoted,
        "reasons": list(outcome.blocking),
        **_state(f, w.page_id),
    }


def _blocked_by_analysis(**kwargs: Any) -> Callable[[SessionFactory, World, str], Observed]:
    def scenario(f: SessionFactory, w: World, key: str) -> Observed:
        _, analysis = _experiment(f, w, key, **kwargs)
        return _approve_and_try(f, w, analysis)

    return scenario


def s_sandbox_label(label: str) -> Callable[[SessionFactory, World, str], Observed]:
    def scenario(f: SessionFactory, w: World, key: str) -> Observed:
        with f() as session:
            candidate = session.get(UISpecVersion, w.candidate_ids[label])
            run = session.get(CandidateEvaluationRun, w.evaluation_ids[label])
            assert candidate is not None and run is not None
            overrides = {
                "candidate_evaluation_run_id": run.id,
                "mutation_run_id": run.mutation_run_id,
                "candidate_spec_id": candidate.id,
                "candidate_spec_hash": candidate.content_hash,
            }
        _, analysis = _forged_experiment(f, w, key, **overrides)
        return _approve_and_try(f, w, analysis)

    return scenario


def s_wrong_candidate(f: SessionFactory, w: World, key: str) -> Observed:
    with f() as session:
        other = session.get(UISpecVersion, w.candidate_ids["human_review"])
        assert other is not None
        overrides = {"candidate_spec_id": other.id, "candidate_spec_hash": other.content_hash}
    _, analysis = _forged_experiment(f, w, key, **overrides)
    return _approve_and_try(f, w, analysis)


def s_candidate_hash_mismatch(f: SessionFactory, w: World, key: str) -> Observed:
    claimed = hashlib.sha256(b"not the evaluated candidate").hexdigest()
    _, analysis = _forged_experiment(f, w, key, candidate_spec_hash=claimed)
    return _approve_and_try(f, w, analysis)


def s_experiment_active_on_page(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    other = create_experiment(
        f,
        experiment_key=key + "_other",
        candidate_evaluation_run_id=w.evaluation_ids["pass"],
        candidate_allocation_bp=1000,
        primary_metric="rage_click_session_rate",
        guardrail_metrics=["form_error_session_rate"],
        minimum_sample_per_variant=100,
        traffic_source="simulated",
    )
    assert other.created and start_experiment(f, other.experiment_id).changed  # type: ignore[arg-type]
    return _approve_and_try(f, w, analysis)


def s_newer_reject_after_approval(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    with f() as session:
        run = session.get(CandidateEvaluationRun, w.evaluation_ids["pass"])
        assert run is not None
        mutation_run_id = run.mutation_run_id
    _forge_evaluation(f, w.candidate_ids["pass"], mutation_run_id)  # TOCTOU: after the approval
    result = _promote(f, approval.approval_id, w.page_id)
    return {
        "approved": approval.recorded,
        "promoted": result.changed,
        "reasons": list(result.reasons),
        **_state(f, w.page_id),
    }


def s_newer_analysis_after_approval(f: SessionFactory, w: World, key: str) -> Observed:
    experiment, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    # Late data arrives and a new analysis reports different evidence.
    _seed_arm(f, experiment, "control", 5, {"rage_click_session_rate": 5}, f"{key}:late")
    analyze_experiment(f, experiment.id, AS_OF)
    result = _promote(f, approval.approval_id, w.page_id)
    return {
        "approved": approval.recorded,
        "promoted": result.changed,
        "reasons": list(result.reasons),
        **_state(f, w.page_id),
    }


def s_stale_source_generation(f: SessionFactory, w: World, key: str) -> Observed:
    """Two approved candidates from Generation 0; the first promotion makes the second stale."""
    _, first = _experiment(f, w, key + "_a")
    a = _approve(f, first)  # type: ignore[arg-type]
    # A second, different eligible candidate on the same page: evaluated fresh.
    _, second = _second_candidate_experiment(f, w, key + "_b")
    b = _approve(f, second)
    assert a.recorded and b.recorded
    first_result = _promote(f, a.approval_id, w.page_id)
    assert first_result.changed, first_result.reasons
    result = _promote(f, b.approval_id, w.page_id, target=2)
    return {
        "approved": b.recorded,
        "promoted": result.changed,
        "reasons": list(result.reasons),
        **_state(f, w.page_id),
    }


def _second_candidate_experiment(
    f: SessionFactory, w: World, key: str
) -> tuple[Experiment, uuid.UUID]:
    """The rage fix plus a spacing change is still a Step 13 human_review, so instead reuse the
    pass candidate through a NEW experiment: a second approval of different evidence."""
    experiment, analysis = _experiment(f, w, key)
    assert analysis is not None
    return experiment, analysis


def s_duplicate_approval(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    first = _approve(f, analysis)  # type: ignore[arg-type]
    second = _approve(f, analysis)  # type: ignore[arg-type]
    return {
        "approved": second.recorded,
        "first_recorded": first.recorded,
        "reasons": list(second.blocking),
        **_state(f, w.page_id),
    }


def s_replay_promotion(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    first = _promote(f, approval.approval_id, w.page_id)
    replay = _promote(f, approval.approval_id, w.page_id, target=2)
    state = _state(f, w.page_id)
    return {
        "approved": approval.recorded,
        "first_promoted": first.changed,
        "promoted": replay.changed,
        "reasons": list(replay.reasons),
        **state,
    }


def s_confirmation_mismatch(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    result = promote(f, approval.approval_id, REVIEWER, f"{w.page_id}:7")
    return {
        "approved": approval.recorded,
        "promoted": result.changed,
        "reasons": list(result.reasons),
        **_state(f, w.page_id),
    }


def s_rejected_after_approval(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    rejection = decide(f, analysis, "reject", "second-reviewer", "Changed my mind.")  # type: ignore[arg-type]
    result = _promote(f, approval.approval_id, w.page_id)
    return {
        "approved": approval.recorded,
        "rejected": rejection.recorded,
        "promoted": result.changed,
        "reasons": list(result.reasons),
        **_state(f, w.page_id),
    }


def s_reject_is_recorded_for_ineligible(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key, per_arm=40)
    rejection = decide(f, analysis, "reject", REVIEWER, "Not enough data.")  # type: ignore[arg-type]
    return {
        "rejected": rejection.recorded,
        "reasons": list(rejection.blocking),
        **_state(f, w.page_id),
    }


def s_promotion_failure_is_atomic(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]

    def fail(_session: Session) -> None:
        raise RuntimeError("simulated crash just before commit")

    crashed = False
    try:
        promote(f, approval.approval_id, REVIEWER, f"{w.page_id}:1", before_commit=fail)
    except RuntimeError:
        crashed = True
    return {
        "approved": approval.recorded,
        "promoted": False,
        "crashed": crashed,
        **_state(f, w.page_id),
    }


def _promoted(f: SessionFactory, w: World, key: str) -> None:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]
    assert _promote(f, approval.approval_id, w.page_id).changed


def s_rollback_to_generation_0(f: SessionFactory, w: World, key: str) -> Observed:
    _promoted(f, w, key)
    with f() as session:
        pointer = session.get(ActiveGeneration, w.page_id)
        assert pointer is not None
        gen1_id = pointer.ui_spec_version_id
    result = rollback(f, w.page_id, REVIEWER, "Rollback drill.", f"{w.page_id}:0")
    state = _state(f, w.page_id)
    with f() as session:
        gen1 = session.get(UISpecVersion, gen1_id)
        checks = {
            "generation_1_still_exists": gen1 is not None and gen1.status == "promoted",
            "generation_1_unchanged": gen1 is not None
            and content_hash(gen1.spec) == gen1.content_hash,
            "pointer_is_generation_0": state["active_spec_id"] == str(w.control_id),
            "promotion_record_kept": state["promotion_records"] == 1,
        }
    return {
        "rolled_back": result.changed,
        "reasons": list(result.reasons),
        "checks": checks,
        **state,
    }


def s_rollback_unknown_target(f: SessionFactory, w: World, key: str) -> Observed:
    _promoted(f, w, key)
    result = rollback(f, w.page_id, REVIEWER, "Bad target.", f"{w.page_id}:7", to_generation=7)
    return {"rolled_back": result.changed, "reasons": list(result.reasons), **_state(f, w.page_id)}


def s_rollback_at_generation_0(f: SessionFactory, w: World, key: str) -> Observed:
    result = rollback(f, w.page_id, REVIEWER, "Nothing earlier.", f"{w.page_id}:0")
    return {"rolled_back": result.changed, "reasons": list(result.reasons), **_state(f, w.page_id)}


def s_rollback_forward_refused(f: SessionFactory, w: World, key: str) -> Observed:
    _promoted(f, w, key)
    assert rollback(f, w.page_id, REVIEWER, "Back.", f"{w.page_id}:0").changed
    result = rollback(f, w.page_id, REVIEWER, "Forward?", f"{w.page_id}:1", to_generation=1)
    return {"rolled_back": result.changed, "reasons": list(result.reasons), **_state(f, w.page_id)}


def _db_refusal(work: Callable[[Session], None], f: SessionFactory) -> tuple[bool, str | None]:
    session = f()
    try:
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))  # independent of earlier cases
        work(session)
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.flush()
        return True, None
    except DBAPIError as error:
        diag = getattr(error.orig, "diag", None)
        message = str(getattr(diag, "message_primary", "") or "")
        return False, message.split(" (")[0]
    finally:
        session.rollback()
        session.close()


def s_candidate_as_rollback_target(f: SessionFactory, w: World, key: str) -> Observed:
    _promoted(f, w, key)

    def forge(session: Session) -> None:
        pointer = session.get(ActiveGeneration, w.page_id)
        assert pointer is not None
        record_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO generation_rollback (id, page_id, from_spec_id, from_generation, "
                "to_spec_id, to_generation, reviewer, reason) VALUES "
                "(:id, :page, :frm, 1, :cand, 0, 'forger', 'x')"
            ),
            {
                "id": record_id,
                "page": w.page_id,
                "frm": pointer.ui_spec_version_id,
                "cand": w.candidate_ids["pass"],
            },
        )
        session.execute(
            text(
                "UPDATE active_generation SET ui_spec_version_id = :cand, generation = 0, "
                "change_kind = 'rollback', change_id = :id WHERE page_id = :page"
            ),
            {"cand": w.candidate_ids["pass"], "id": record_id, "page": w.page_id},
        )

    accepted, message = _db_refusal(forge, f)
    return {"rolled_back": accepted, "db_message": message, **_state(f, w.page_id)}


def s_pointer_without_record(f: SessionFactory, w: World, key: str) -> Observed:
    """Point at a (legitimately) promoted row on ANOTHER page's promotion? Simplest forgery:
    move the pointer with a made-up change id."""

    def forge(session: Session) -> None:
        session.execute(
            text(
                "UPDATE active_generation SET change_kind = 'promotion', change_id = :id "
                "WHERE page_id = :page"
            ),
            {"id": uuid.uuid4(), "page": w.page_id},
        )

    accepted, message = _db_refusal(forge, f)
    return {"promoted": accepted, "db_message": message, **_state(f, w.page_id)}


def s_record_without_pointer(f: SessionFactory, w: World, key: str) -> Observed:
    _, analysis = _experiment(f, w, key)
    approval = _approve(f, analysis)  # type: ignore[arg-type]

    def forge(session: Session) -> None:
        session.execute(
            text(
                "INSERT INTO generation_promotion (id, approval_id, page_id, candidate_spec_id, "
                "promoted_spec_id, from_spec_id, from_generation, to_generation, reviewer, "
                "policy_version, evidence_hash) VALUES (:id, :appr, :page, :cand, :cand, :gen0, "
                "0, 1, 'forger', 'promotion_policy.v1', :h)"
            ),
            {
                "id": uuid.uuid4(),
                "appr": approval.approval_id,
                "page": w.page_id,
                "cand": w.candidate_ids["pass"],
                "gen0": w.control_id,
                "h": "a" * 64,
            },
        )

    accepted, message = _db_refusal(forge, f)
    return {"promoted": accepted, "db_message": message, **_state(f, w.page_id)}


def s_forged_promoted_row(f: SessionFactory, w: World, key: str) -> Observed:
    def forge(session: Session) -> None:
        with session.no_autoflush:
            candidate = session.get(UISpecVersion, w.candidate_ids["pass"])
            assert candidate is not None
            spec = {**candidate.spec, "generation": 1}
        session.execute(
            insert(UISpecVersion).values(
                id=uuid.uuid4(),
                page_id=w.page_id,
                status="promoted",
                generation=1,
                parent_id=candidate.id,
                schema_version=1,
                spec=spec,
                content_hash=content_hash(spec),
                source="forged",
            )
        )

    accepted, message = _db_refusal(forge, f)
    return {"promoted": accepted, "db_message": message, **_state(f, w.page_id)}


def _events_for(
    f: SessionFactory,
    sid: uuid.UUID,
    specs: Sequence[UISpecVersion | None],
    claimed_hash: str | None = None,
) -> None:
    for i, spec in enumerate(specs):
        at = T0 + timedelta(milliseconds=300 * i)
        body: dict[str, Any] = {
            "schema_version": 2,
            "event_id": str(uuid.uuid4()),
            "event_type": "button_click",
            "session_id": str(sid),
            "occurred_at": at.isoformat(),
            "payload": {
                "generation": spec.generation if spec else 0,
                "component": "plan_team_pro_cta",
            },
        }
        if spec is not None:
            body |= {
                "ui_generation": spec.generation,
                "ui_spec_version_id": str(spec.id),
                "ui_spec_hash": claimed_hash or spec.content_hash,
            }
        from darwin.telemetry.service import process_telemetry_message

        with f() as session:
            process_telemetry_message(session, body)


def _signal(f: SessionFactory, sid: uuid.UUID) -> tuple[str | None, str | None]:
    with f() as session:
        signal = session.scalar(
            select(BehaviorSignal).where(
                BehaviorSignal.session_id == sid, BehaviorSignal.superseded_at.is_(None)
            )
        )
        if signal is None:
            return None, None
        return signal.ui_attribution, (
            str(signal.ui_spec_version_id) if signal.ui_spec_version_id else None
        )


def _verified(f: SessionFactory, sid: uuid.UUID) -> list[str | None]:
    with f() as session:
        rows = session.scalars(
            select(UserEvent.ui_spec_version_id)
            .where(UserEvent.session_id == sid)
            .order_by(UserEvent.occurred_at)
        ).all()
    return [str(r) if r else None for r in rows]


def s_telemetry_generation_1(f: SessionFactory, w: World, key: str) -> Observed:
    _promoted(f, w, key)
    with f() as session:
        pointer = session.get(ActiveGeneration, w.page_id)
        assert pointer is not None
        gen1 = session.get(UISpecVersion, pointer.ui_spec_version_id)
        assert gen1 is not None
        session.expunge(gen1)
    sid = uuid.uuid5(uuid.NAMESPACE_URL, key)
    _events_for(f, sid, [gen1] * 4)
    attribution, spec = _signal(f, sid)
    verified = _verified(f, sid)
    return {
        "checks": {
            "events_verified_as_generation_1": verified == [str(gen1.id)] * 4,
            "signal_single_generation_1": (attribution, spec) == ("single", str(gen1.id)),
        }
    }


def s_telemetry_mixed(f: SessionFactory, w: World, key: str) -> Observed:
    _promoted(f, w, key)
    with f() as session:
        pointer = session.get(ActiveGeneration, w.page_id)
        assert pointer is not None
        gen1 = session.get(UISpecVersion, pointer.ui_spec_version_id)
        gen0 = session.get(UISpecVersion, w.control_id)
        assert gen1 is not None and gen0 is not None
        session.expunge_all()
    sid = uuid.uuid5(uuid.NAMESPACE_URL, key)
    _events_for(f, sid, [gen0, gen0, gen1, gen1])
    attribution, spec = _signal(f, sid)
    return {"checks": {"signal_mixed": (attribution, spec) == ("mixed", None)}}


def s_telemetry_legacy_unknown(f: SessionFactory, w: World, key: str) -> Observed:
    sid = uuid.uuid5(uuid.NAMESPACE_URL, key)
    _events_for(f, sid, [None] * 4)
    attribution, spec = _signal(f, sid)
    return {
        "checks": {
            "events_unknown": _verified(f, sid) == [None] * 4,
            "signal_unknown": (attribution, spec) == ("unknown", None),
        }
    }


def s_telemetry_unverified_claim(f: SessionFactory, w: World, key: str) -> Observed:
    with f() as session:
        gen0 = session.get(UISpecVersion, w.control_id)
        assert gen0 is not None
        session.expunge(gen0)
    sid = uuid.uuid5(uuid.NAMESPACE_URL, key)
    _events_for(f, sid, [gen0] * 4, claimed_hash="f" * 64)  # the hash does not match the id
    attribution, _ = _signal(f, sid)
    with f() as session:
        claims = session.scalars(
            select(UserEvent.ui_spec_hash).where(UserEvent.session_id == sid)
        ).all()
    return {
        "checks": {
            "claims_stored_raw": list(claims) == ["f" * 64] * 4,
            "not_verified": _verified(f, sid) == [None] * 4,
            "signal_unknown": attribution == "unknown",
        }
    }


def s_active_api(f: SessionFactory, w: World, key: str) -> Observed:
    before = active_generation(f, w.page_id)
    _promoted(f, w, key)
    after = active_generation(f, w.page_id)
    assert rollback(f, w.page_id, REVIEWER, "Drill.", f"{w.page_id}:0").changed
    back = active_generation(f, w.page_id)
    return {
        "checks": {
            "before_is_generation_0": (before.status, before.generation) == ("active", 0),
            "after_promotion_is_generation_1": (after.status, after.generation) == ("active", 1),
            "hash_matches_spec": after.spec is not None
            and content_hash(after.spec) == after.spec_hash,
            "after_rollback_is_generation_0": (back.status, back.generation) == ("active", 0),
        }
    }


def s_active_api_failure(f: SessionFactory, w: World, key: str) -> Observed:
    def broken() -> Session:
        raise RuntimeError("database unavailable")

    answer = active_generation(broken, w.page_id)
    return {"checks": {"fails_to_none": answer.model_dump(exclude_none=True) == {"status": "none"}}}


SCENARIOS: dict[str, Callable[[SessionFactory, World, str], Observed]] = {
    "approve_clean": s_approve_clean,
    "promote_clean": s_promote_clean,
    "sandbox_human_review": s_sandbox_label("human_review"),
    "sandbox_reject": s_sandbox_label("reject"),
    "experiment_running": _blocked_by_analysis(finish="running"),
    "experiment_paused": _blocked_by_analysis(finish="paused"),
    "insufficient_data": _blocked_by_analysis(per_arm=40),
    "needs_review": _blocked_by_analysis(fallbacks=3),
    "stop_recommended": _blocked_by_analysis(
        arms={
            "control": {"rage_click_session_rate": 40, "form_error_session_rate": 10},
            "candidate": {"rage_click_session_rate": 10, "form_error_session_rate": 60},
        }
    ),
    "analysis_error": _blocked_by_analysis(fault=True),
    "candidate_hash_mismatch": s_candidate_hash_mismatch,
    "wrong_candidate": s_wrong_candidate,
    "experiment_active_on_page": s_experiment_active_on_page,
    "newer_reject_after_approval": s_newer_reject_after_approval,
    "newer_analysis_after_approval": s_newer_analysis_after_approval,
    "stale_source_generation": s_stale_source_generation,
    "duplicate_approval": s_duplicate_approval,
    "replay_promotion": s_replay_promotion,
    "confirmation_mismatch": s_confirmation_mismatch,
    "rejected_after_approval": s_rejected_after_approval,
    "reject_recorded_for_ineligible": s_reject_is_recorded_for_ineligible,
    "promotion_failure_atomic": s_promotion_failure_is_atomic,
    "rollback_to_generation_0": s_rollback_to_generation_0,
    "rollback_unknown_target": s_rollback_unknown_target,
    "rollback_at_generation_0": s_rollback_at_generation_0,
    "rollback_forward_refused": s_rollback_forward_refused,
    "candidate_as_rollback_target": s_candidate_as_rollback_target,
    "pointer_without_record": s_pointer_without_record,
    "record_without_pointer": s_record_without_pointer,
    "forged_promoted_row": s_forged_promoted_row,
    "telemetry_generation_1": s_telemetry_generation_1,
    "telemetry_mixed_signal": s_telemetry_mixed,
    "telemetry_legacy_unknown": s_telemetry_legacy_unknown,
    "telemetry_unverified_claim": s_telemetry_unverified_claim,
    "active_api_follows_pointer": s_active_api,
    "active_api_failure_falls_back": s_active_api_failure,
}


# ---- dataset, scoring, running -----------------------------------------------------------------


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approved: bool | None = None
    promoted: bool | None = None
    rolled_back: bool | None = None
    rejected: bool | None = None
    active_generation: int | None = None
    promoted_rows: int | None = None
    reasons_include: tuple[str, ...] = ()
    db_message: str | None = None
    checks: bool = False  # every observed check must be true


class PromotionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9_]{3,48}$")
    group: str = Field(pattern=r"^(eligible|ineligible|toctou|rollback|forgery|telemetry|failure)$")
    scenario: str
    description: str = Field(min_length=10, max_length=300)
    expected: Expected

    @field_validator("scenario")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in SCENARIOS:
            raise ValueError(f"unknown scenario {value}")
        return value


class PromotionDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=1)
    cases: tuple[PromotionCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique(cls, cases: tuple[PromotionCase, ...]) -> tuple[PromotionCase, ...]:
        if len({c.id for c in cases}) != len(cases):
            raise ValueError("golden case ids must be unique")
        return cases


def load_dataset(path: Path = GOLDEN_PATH) -> PromotionDataset:
    return PromotionDataset.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass
class CaseResult:
    id: str
    group: str
    observed: dict[str, Any]
    correct: bool
    unauthorized_promotion: bool
    invalid_rollback: bool
    failed_checks: list[str] = field(default_factory=list)


def score(case: PromotionCase, observed: Observed) -> CaseResult:
    e = case.expected
    failed: list[str] = []
    for name in (
        "approved",
        "promoted",
        "rolled_back",
        "rejected",
        "active_generation",
        "promoted_rows",
    ):
        want = getattr(e, name)
        if want is not None and observed.get(name) != want:
            failed.append(name)
    for reason in e.reasons_include:
        if reason not in observed.get("reasons", []):
            failed.append(f"reason:{reason}")
    if e.db_message is not None and observed.get("db_message") != e.db_message:
        failed.append("db_message")
    if e.checks:
        checks = observed.get("checks", {})
        failed += [f"check:{k}" for k, v in checks.items() if not v]
        if not checks:
            failed.append("checks_missing")
    unauthorized = (
        (e.promoted is False and observed.get("promoted") is True)
        or (
            e.active_generation is not None
            and (observed.get("active_generation") or 0) > e.active_generation
        )
        or (e.promoted_rows is not None and (observed.get("promoted_rows") or 0) > e.promoted_rows)
    )
    invalid_rollback = (e.rolled_back is False and observed.get("rolled_back") is True) or (
        e.active_generation is not None
        and observed.get("active_generation") is not None
        and case.group == "rollback"
        and observed["active_generation"] != e.active_generation
    )
    return CaseResult(
        case.id, case.group, observed, not failed, unauthorized, invalid_rollback, failed
    )


def _savepointed(connection: Connection, work: Callable[[], Any]) -> Any:
    savepoint = connection.begin_nested()
    try:
        return work()
    finally:
        savepoint.rollback()


def run_evaluation(
    engine: Engine, dataset: PromotionDataset, facts: Mapping[str, SpecFacts] | None = None
) -> list[CaseResult]:
    facts = facts if facts is not None else harness_facts()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

            results = []
            for case in dataset.cases:

                def work(case: PromotionCase = case) -> CaseResult:
                    world = build_world(factory, f"p_{case.id}"[:40], facts)
                    bootstrap_active(factory, world.page_id)
                    key = f"golden_{case.id}"[:64]
                    return score(case, SCENARIOS[case.scenario](factory, world, key))

                results.append(_savepointed(connection, work))
            return results
        finally:
            transaction.rollback()


def metrics(results: Sequence[CaseResult]) -> dict[str, Any]:
    groups = sorted({r.group for r in results})
    correct = sum(r.correct for r in results)
    return {
        "cases_meeting_all_expectations": {
            "passed": correct,
            "of": len(results),
            "rate": round(correct / len(results), 4),
        },
        **{
            f"{g}_accuracy": {
                "passed": sum(r.correct for r in results if r.group == g),
                "of": sum(r.group == g for r in results),
            }
            for g in groups
        },
        "unauthorized_promotion_count": sum(r.unauthorized_promotion for r in results),
        "invalid_rollback_count": sum(r.invalid_rollback for r in results),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden promotion / rollback evaluation.")
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
        "schema": "darwinux.promotion-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "policy_version": "promotion_policy.v1",
        "metrics": m,
        "cases": [asdict(r) for r in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"golden: {len(dataset.cases)} cases   promotion_policy.v1")
    for r in results:
        mark = "ok  " if r.correct else "FAIL"
        extra = f"  failed: {','.join(r.failed_checks)}" if r.failed_checks else ""
        print(f"  {mark} {r.group:<10} {r.id:<40}{extra}")
    for name, v in m.items():
        if isinstance(v, dict):
            print(f"{name:<34} {v['passed']}/{v['of']}")
    print(f"{'UNAUTHORIZED_PROMOTION_COUNT':<34} {m['unauthorized_promotion_count']}")
    print(f"{'INVALID_ROLLBACK_COUNT':<34} {m['invalid_rollback_count']}")
    print(f"report: {args.output.relative_to(REPO_ROOT)}")
    ok = m["unauthorized_promotion_count"] == 0 and m["invalid_rollback_count"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
