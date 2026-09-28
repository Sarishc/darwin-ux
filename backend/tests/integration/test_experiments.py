"""Controlled experiments against darwin_test: the real chain, the real API and worker, the
real frontend harness. Rolled back, except the migration test, which cleans up after itself."""

import logging
import subprocess
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, func, insert, inspect, select, text
from sqlalchemy.orm import Session

from darwin.api.experiments import get_session_factory
from darwin.db.models import (
    Experiment,
    ExperimentAnalysis,
    ExperimentExposure,
    ExperimentLifecycleEvent,
    UISpecVersion,
    UserEvent,
)
from darwin.decisions.evaluation import Artifact, write_artifact
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.experiments.evaluation import (
    AS_OF,
    CREATED,
    STARTED,
    T0,
    World,
    build_world,
    exposure_payload,
    harness_facts,
    load_dataset,
    run_evaluation,
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
    stop_experiment,
)
from darwin.experiments.vocabulary import Variant
from darwin.memory.corpus import REPO_ROOT
from darwin.mutations.apply import content_hash
from darwin.mutations.fixture import FixtureMutationGenerator
from darwin.mutations.service import generate_candidate
from darwin.mutations.specs import import_generation_zero
from darwin.sandbox.harness import NodeHarnessRunner, SpecFacts
from darwin.sandbox.service import evaluate_candidate

pytestmark = pytest.mark.integration

SessionFactory = Callable[[], Session]
UI_SPEC_DIR = REPO_ROOT / "frontend" / "src" / "ui-spec"
CONFIG: dict[str, Any] = {
    "candidate_allocation_bp": 5000,
    "primary_metric": "rage_click_session_rate",
    "guardrail_metrics": ["form_error_session_rate", "signup_submit_session_rate"],
    "minimum_sample_per_variant": 100,
    "traffic_source": "simulated",
}


@pytest.fixture(scope="module")
def facts() -> dict[str, SpecFacts]:
    return harness_facts()  # one real frontend-harness run for the module


@pytest.fixture
def factory(test_session_factory: SessionFactory) -> SessionFactory:
    return test_session_factory


@pytest.fixture
def world(factory: SessionFactory, facts: dict[str, SpecFacts]) -> World:
    return build_world(factory, "itest", facts)


@pytest.fixture
def client(api: TestClient, factory: SessionFactory) -> Iterator[TestClient]:
    """The real API (assignment + telemetry) on the rolled-back connection, worker drained."""
    api.app.dependency_overrides[get_session_factory] = lambda: factory  # type: ignore[attr-defined]
    yield api


def _running(factory: SessionFactory, world: World, key: str, **config: Any) -> Experiment:
    outcome = create_experiment(
        factory,
        experiment_key=key,
        candidate_evaluation_run_id=world.evaluation_ids["pass"],
        at=CREATED,
        **(CONFIG | config),
    )
    assert outcome.created, outcome.reasons
    assert start_experiment(factory, outcome.experiment_id, at=STARTED).changed  # type: ignore[arg-type]
    with factory() as session:
        experiment = session.get(Experiment, outcome.experiment_id)
        assert experiment is not None
        session.expunge(experiment)
        return experiment


def _post_event(
    client: TestClient,
    session_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    at: datetime,
) -> None:
    response = client.post(
        "/api/v1/telemetry/events",
        json={
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "session_id": str(session_id),
            "occurred_at": at.isoformat(),
            "payload": payload,
        },
    )
    assert response.status_code == 202


def _assignment(client: TestClient, session_id: uuid.UUID, page: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/experiments/assignment", json={"session_id": str(session_id), "page": page}
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


# ---- the realistic end-to-end run ------------------------------------------------------------


def test_end_to_end_candidate_better_is_evidence_not_a_promotion(
    factory: SessionFactory,
    client: TestClient,
    connection: Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Generation 0 -> signal -> decision -> Step 12 candidate -> REAL Step 13 evaluation.
    with factory() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(session, "e2e", Artifact())
    decision = decide_research_run(factory, research_run_id, RulesDecider())
    mutation = generate_candidate(factory, decision.decision_run_id, FixtureMutationGenerator())
    assert mutation.candidate_spec_id is not None
    evaluation = evaluate_candidate(factory, mutation.candidate_spec_id)
    assert evaluation.recommendation == "pass"
    specs_before = connection.execute(select(UISpecVersion.id, UISpecVersion.content_hash)).all()

    # A draft experiment, then the explicit start.
    created = create_experiment(
        factory,
        experiment_key="e2e_rage_fix",
        candidate_evaluation_run_id=evaluation.evaluation_run_id,
        at=CREATED,
        **CONFIG,
    )
    assert created.created and created.experiment_id
    with factory() as session:
        draft = session.get(Experiment, created.experiment_id)
        assert draft is not None and draft.status == "draft"
        assert draft.page_id == "pricing_signup" and draft.hypothesis_id is not None
    assert _assignment(client, uuid.uuid4(), "pricing_signup") == {"status": "none"}  # not started
    assert start_experiment(factory, created.experiment_id, at=STARTED).changed
    with factory() as session:
        experiment = session.get(Experiment, created.experiment_id)
        assert experiment is not None
        session.expunge(experiment)

    # Session A -> control, session B -> candidate, through the real API; same answer twice.
    key = experiment.experiment_key
    session_a = sessions_for(key, 5000, "control", 1, "e2e-a")[0]
    session_b = sessions_for(key, 5000, "candidate", 1, "e2e-b")[0]
    served_a = _assignment(client, session_a, "pricing_signup")
    served_b = _assignment(client, session_b, "pricing_signup")
    assert _assignment(client, session_b, "pricing_signup") == served_b
    assert (served_a["variant"], served_b["variant"]) == ("control", "candidate")
    assert set(served_b) == {
        "status",
        "experiment_key",
        "variant",
        "spec_hash",
        "spec_version_id",  # Step 15: for server-verified telemetry attribution
        "spec",
    }
    assert served_a["spec_hash"] == experiment.control_spec_hash == content_hash(served_a["spec"])
    assert served_b["spec_hash"] == experiment.candidate_spec_hash == content_hash(served_b["spec"])

    # Both served specs render through the REAL frontend (Zod + registry + SpecPage).
    rendered = NodeHarnessRunner().run(
        {served_a["spec_hash"]: served_a["spec"], served_b["spec_hash"]: served_b["spec"]}
    )
    for spec_hash in (served_a["spec_hash"], served_b["spec_hash"]):
        assert rendered[spec_hash].schema_.ok and rendered[spec_hash].render.ok
    delay = {h: {c.component_id: c.reveal_delay_ms for c in f.ctas} for h, f in rendered.items()}
    assert delay[served_a["spec_hash"]]["plan_team_pro_cta"] == 1500  # the friction ...
    assert delay[served_b["spec_hash"]]["plan_team_pro_cta"] == 0  # ... and the fix

    # Exposure telemetry after the render, through the real API + worker; B's is repeated.
    with caplog.at_level(logging.DEBUG, logger="darwin"):
        _post_event(
            client, session_a, "experiment_exposure", exposure_payload(experiment, "control"), T0
        )
        for _ in range(3):
            _post_event(
                client,
                session_b,
                "experiment_exposure",
                exposure_payload(experiment, "candidate"),
                T0,
            )
        # Outcome telemetry: A rage-clicks the delayed CTA; B clicks once and submits.
        for i in range(4):
            _post_event(
                client,
                session_a,
                "button_click",
                {"generation": 0, "component": "plan_team_pro_cta"},
                T0 + timedelta(seconds=2, milliseconds=300 * i),
            )
        _post_event(
            client,
            session_b,
            "button_click",
            {"generation": 1, "component": "plan_team_pro_cta"},
            T0 + timedelta(seconds=2),
        )
        _post_event(
            client,
            session_b,
            "button_click",
            {"generation": 1, "component": "signup_form_submit"},
            T0 + timedelta(seconds=9),
        )
    with factory() as session:
        exposures = session.execute(
            select(ExperimentExposure.variant, func.count())
            .where(ExperimentExposure.experiment_id == experiment.id)
            .group_by(ExperimentExposure.variant)
        ).all()
    assert dict(exposures) == {"control": 1, "candidate": 1}  # repeated exposure: still 1

    # Enough simulated traffic for the floor: 110 more sessions per arm via the worker path.
    bulk: tuple[tuple[Variant, int], ...] = (("control", 55), ("candidate", 11))
    for variant, rage in bulk:
        sids = sessions_for(key, 5000, variant, 110, f"e2e-bulk-{variant}")
        for sid in sids:
            payload = exposure_payload(experiment, variant)
            telemetry(factory, sid, "experiment_exposure", payload, T0)
        seed_outcomes(factory, sids, {"rage_click_session_rate": rage}, T0)

    analysis = analyze_experiment(factory, experiment.id, AS_OF)
    primary = analysis.report["primary"]
    assert analysis.status == "completed" and analysis.assessment == "evidence_ready"
    assert analysis.report["exposed_sessions"] == {"control": 111, "candidate": 111}
    assert (primary["control"]["successes"], primary["candidate"]["successes"]) == (56, 11)
    assert primary["absolute_difference"] < 0 and primary["interval_excludes_zero"] is True
    assert analysis.report["data_sufficiency"] == "sufficient"
    assert analysis.report["guardrail_status"] == "ok"

    # The candidate is numerically better — and NOTHING was promoted or changed.
    with factory() as session:
        stored = session.get(ExperimentAnalysis, analysis.analysis_id)
        assert stored is not None and stored.report_hash == analysis.report_hash
        assert (
            session.scalar(select(Experiment.status).where(Experiment.id == experiment.id))
            == "running"
        )
        gen0 = session.scalar(
            select(UISpecVersion).where(
                UISpecVersion.page_id == "pricing_signup", UISpecVersion.generation == 0
            )
        )
        assert gen0 is not None and content_hash(gen0.spec) == experiment.control_spec_hash
        assert (
            session.scalar(
                select(func.count()).select_from(UISpecVersion).where(UISpecVersion.generation == 1)
            )
            == 0
        )  # no Generation 1 exists
    assert (
        connection.execute(select(UISpecVersion.id, UISpecVersion.content_hash)).all()
        == specs_before
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(UI_SPEC_DIR)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
    logged = " ".join(str(r.__dict__.get("context", "")) + r.getMessage() for r in caplog.records)
    for sid in (session_a, session_b):
        assert str(sid) not in logged  # no session ids in logs


# ---- serving fallbacks and exposure rules -------------------------------------------------------


def test_candidate_not_as_evaluated_falls_back_to_control(
    factory: SessionFactory, client: TestClient, world: World
) -> None:
    experiment = _running(factory, world, "itest_fallback")
    with factory() as session:  # the gate refuses this, so bypass it: a tampered active row
        session.execute(text("ALTER TABLE experiment DISABLE TRIGGER experiment_guard"))
        session.execute(
            text("UPDATE experiment SET candidate_spec_hash = :h WHERE id = :id"),
            {"h": "f" * 64, "id": experiment.id},
        )
        session.execute(text("ALTER TABLE experiment ENABLE TRIGGER experiment_guard"))
        session.commit()
    candidate_session = sessions_for(experiment.experiment_key, 5000, "candidate", 1, "fb")[0]
    control_session = sessions_for(experiment.experiment_key, 5000, "control", 1, "fb")[0]
    fallback = _assignment(client, candidate_session, world.page_id)
    assert fallback == {
        "status": "fallback",
        "experiment_key": "itest_fallback",
        "reason": "spec_hash_mismatch",
    }
    assert _assignment(client, control_session, world.page_id)["variant"] == "control"


def test_render_failure_is_a_fallback_not_an_exposure(
    factory: SessionFactory, client: TestClient, world: World
) -> None:
    experiment = _running(factory, world, "itest_render_failure")
    sid = sessions_for(experiment.experiment_key, 5000, "candidate", 1, "rf")[0]
    assert _assignment(client, sid, world.page_id)["variant"] == "candidate"
    payload = {"generation": 0, "experiment": experiment.experiment_key, "reason": "render_error"}
    _post_event(client, sid, "experiment_fallback", payload, T0)
    analysis = analyze_experiment(factory, experiment.id, AS_OF)
    assert analysis.report["exposed_sessions"] == {"control": 0, "candidate": 0}
    assert analysis.report["integrity"]["fallback_sessions"] == {"control": 0, "candidate": 1}
    assert analysis.assessment == "needs_review" and "candidate_fallbacks" in analysis.reason_codes


def test_exposure_rules_through_the_real_pipeline(
    factory: SessionFactory, client: TestClient, world: World
) -> None:
    experiment = _running(factory, world, "itest_exposure")
    key = experiment.experiment_key
    control, candidate = (sessions_for(key, 5000, v, 2, "ex") for v in ("control", "candidate"))
    for sid in control + candidate:  # assigned only
        _assignment(client, sid, world.page_id)
    _post_event(
        client, control[0], "experiment_exposure", exposure_payload(experiment, "candidate"), T0
    )
    wrong_hash = exposure_payload(experiment, "control") | {"spec_hash": "0" * 64}
    _post_event(client, control[1], "experiment_exposure", wrong_hash, T0)
    _post_event(
        client, candidate[0], "experiment_exposure", exposure_payload(experiment, "candidate"), T0
    )
    assert stop_experiment(factory, experiment.id, "human_decision").changed
    _post_event(
        client, candidate[1], "experiment_exposure", exposure_payload(experiment, "candidate"), T0
    )
    with factory() as session:
        stored = session.scalars(
            select(ExperimentExposure.session_id).where(
                ExperimentExposure.experiment_id == experiment.id
            )
        ).all()
        raw = session.scalar(
            select(func.count())
            .select_from(UserEvent)
            .where(UserEvent.event_type == "experiment_exposure")
        )
    assert stored == [candidate[0]]  # the only valid exposure while running
    assert raw == 4  # every event is still stored raw; only exposures are filtered
    # In this rolled-back test every row shares one transaction clock (now()), so the stopped
    # experiment's cutoff still includes all four events: three were refused exposures.
    report = analyze_experiment(factory, experiment.id, AS_OF).report
    assert report["integrity"]["rejected_exposure_sessions"] == 3


# ---- lifecycle, immutability, constraints --------------------------------------------------------


def test_lifecycle_is_explicit_and_configuration_immutable(
    factory: SessionFactory, connection: Connection, world: World
) -> None:
    created = create_experiment(
        factory,
        experiment_key="itest_lifecycle",
        candidate_evaluation_run_id=world.evaluation_ids["pass"],
        **CONFIG,
    )
    experiment_id = created.experiment_id
    assert experiment_id is not None
    assert pause_experiment(factory, experiment_id).reasons == ("transition_not_allowed",)
    assert start_experiment(factory, experiment_id).changed
    assert start_experiment(factory, experiment_id).reasons == ("experiment_already_running",)
    assert pause_experiment(factory, experiment_id).changed
    assert start_experiment(factory, experiment_id).changed  # resume re-runs the start gate
    assert complete_experiment(factory, experiment_id).changed
    assert not start_experiment(factory, experiment_id).changed  # terminal
    with factory() as session:
        row = session.get(Experiment, experiment_id)
        assert row is not None and row.status == "completed" and row.stop_reason == "planned_end"
        assert row.started_at is not None and row.stopped_at is not None
    for statement in (
        "UPDATE experiment SET candidate_allocation_bp = 100, control_allocation_bp = 9900",
        "UPDATE experiment SET primary_metric = 'form_error_session_rate', "
        "guardrail_metrics = '[\"rage_click_session_rate\"]'",
        "UPDATE experiment SET status = 'running', stopped_at = NULL",
        "UPDATE experiment SET started_at = now()",
    ):
        with (
            pytest.raises(Exception, match="immutable|not allowed|set once"),
            connection.begin_nested(),
        ):
            connection.execute(text(f"{statement} WHERE id = :id"), {"id": experiment_id})


def test_analysis_records_are_immutable_and_reruns_add_rows(
    factory: SessionFactory, connection: Connection, world: World
) -> None:
    experiment = _running(factory, world, "itest_analysis")
    first = analyze_experiment(factory, experiment.id, AS_OF)
    second = analyze_experiment(factory, experiment.id, AS_OF)
    assert first.analysis_id != second.analysis_id and first.report_hash == second.report_hash
    assert first.assessment == "insufficient_data"
    with pytest.raises(Exception, match="immutable"), connection.begin_nested():
        connection.execute(
            text("UPDATE experiment_analysis SET assessment = 'evidence_ready' WHERE id = :id"),
            {"id": first.analysis_id},
        )

    def boom() -> None:
        raise RuntimeError("simulated")

    failed = analyze_experiment(factory, experiment.id, AS_OF, fault=boom)
    assert (failed.status, failed.assessment, failed.reason_codes) == (
        "analysis_error",
        "needs_review",
        ("analysis_error",),
    )


def test_exposures_are_unique_and_immutable(
    factory: SessionFactory, connection: Connection, world: World
) -> None:
    experiment = _running(factory, world, "itest_unique")
    sid = sessions_for(experiment.experiment_key, 5000, "control", 1, "u")[0]
    telemetry(factory, sid, "experiment_exposure", exposure_payload(experiment, "control"), T0)
    with factory() as session:
        exposure = session.scalars(select(ExperimentExposure)).one()
        values = {
            "experiment_id": exposure.experiment_id,
            "session_id": exposure.session_id,
            "spec_hash": exposure.spec_hash,
            "event_id": exposure.event_id,
            "exposed_at": exposure.exposed_at,
        }
    with (
        pytest.raises(Exception, match="uq_experiment_exposure_session"),
        connection.begin_nested(),
    ):
        connection.execute(insert(ExperimentExposure).values(variant="candidate", **values))
    with pytest.raises(Exception, match="variant_is_known"), connection.begin_nested():
        connection.execute(
            insert(ExperimentExposure).values(
                variant="treatment", **(values | {"session_id": uuid.uuid4()})
            )
        )
    with pytest.raises(Exception, match="immutable"), connection.begin_nested():
        connection.execute(text("UPDATE experiment_exposure SET variant = 'candidate'"))


def test_database_refuses_fail_open_rows(
    factory: SessionFactory, connection: Connection, world: World
) -> None:
    experiment = _running(factory, world, "itest_db")
    base = {
        "experiment_id": experiment.id,
        "analysis_version": "experiment_analysis.v1",
        "as_of": AS_OF,
        "control_exposures": 0,
        "candidate_exposures": 0,
        "reason_codes": ["analysis_error"],
        "report": {},
        "report_hash": "a" * 64,
    }
    for row, constraint in (
        (
            {"status": "analysis_error", "assessment": "evidence_ready", "error_type": "X"},
            "analysis_error_needs_review",
        ),
        (
            {"status": "completed", "assessment": "winner", "error_type": None},
            "assessment_is_known",
        ),
    ):
        with pytest.raises(Exception, match=constraint), connection.begin_nested():
            connection.execute(insert(ExperimentAnalysis).values(**(base | row)))
    # One active experiment per page.
    second = create_experiment(
        factory,
        experiment_key="itest_db_second",
        candidate_evaluation_run_id=world.evaluation_ids["pass"],
        **CONFIG,
    )
    assert second.experiment_id is not None
    assert start_experiment(factory, second.experiment_id).reasons == ("another_experiment_active",)
    with (
        pytest.raises(Exception, match="uq_experiment_one_active_per_page"),
        connection.begin_nested(),
    ):
        connection.execute(
            text(
                "UPDATE experiment SET status = 'running', started_at = now(), "
                "status_changed_at = status_changed_at + interval '1 hour' WHERE id = :id"
            ),
            {"id": second.experiment_id},
        )


def test_ineligible_candidates_never_become_experiments(
    factory: SessionFactory, world: World
) -> None:
    for label in ("reject", "human_review"):
        outcome = create_experiment(
            factory,
            experiment_key=f"itest_{label}",
            candidate_evaluation_run_id=world.evaluation_ids[label],
            **CONFIG,
        )
        assert not outcome.created and outcome.reasons == ("evaluation_not_pass",)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Experiment)) == 0


# ---- golden evaluation + migration ----------------------------------------------------------


def test_experiment_golden_evaluation_never_fails_open(
    migrated_engine: Engine, facts: dict[str, SpecFacts]
) -> None:
    dataset = load_dataset()
    first = run_evaluation(migrated_engine, dataset, facts)
    assert all(r.correct for r in first), [(r.id, r.failed_checks) for r in first if not r.correct]
    assert sum(r.fail_open for r in first) == 0
    analysis_only = dataset.model_copy(
        update={"cases": tuple(c for c in dataset.cases if c.kind == "analysis")}
    )
    second = run_evaluation(migrated_engine, analysis_only, facts)
    hashes = {r.id: r.observed["report_hash"] for r in first if r.kind == "analysis"}
    assert {r.id: r.observed["report_hash"] for r in second} == hashes  # deterministic reports
    with migrated_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Experiment)) == 0  # rolled back


def test_migration_0010_round_trip_keeps_earlier_data(
    migrated_engine: Engine, alembic_cfg: Config
) -> None:
    event_id = uuid.uuid4()
    with Session(migrated_engine) as session:
        session.execute(
            insert(UserEvent).values(
                event_id=event_id,
                event_type="page_view",
                session_id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                payload={"page": "pricing_signup"},
            )
        )
        session.commit()
    try:
        command.downgrade(alembic_cfg, "0009")
        tables = inspect(migrated_engine).get_table_names()
        assert not {
            "experiment",
            "experiment_exposure",
            "experiment_analysis",
            "experiment_lifecycle_event",
        } & set(tables)
        assert "candidate_evaluation_run" in tables
        with migrated_engine.connect() as conn:
            assert (
                conn.scalar(
                    select(func.count())
                    .select_from(UserEvent)
                    .where(UserEvent.event_id == event_id)
                )
                == 1
            )
            functions = conn.scalars(
                text("SELECT proname FROM pg_proc WHERE proname LIKE 'experiment_%'")
            ).all()
            assert functions == []
        command.upgrade(alembic_cfg, "head")
        with migrated_engine.connect() as conn:
            triggers = set(
                conn.scalars(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'experiment%'"))
            )
        assert triggers == {
            "experiment_guard",
            "experiment_lifecycle_record",
            "experiment_lifecycle_event_guard",
            "experiment_lifecycle_event_immutable",
            "experiment_exposure_no_update",
            "experiment_analysis_no_update",
        }
    finally:
        command.upgrade(alembic_cfg, "head")
        with Session(migrated_engine) as session:
            session.execute(delete(UserEvent).where(UserEvent.event_id == event_id))
            session.commit()


# ---- pause / resume / terminal integrity ---------------------------------------------------------


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _exposures(factory: SessionFactory, experiment_id: uuid.UUID) -> int:
    with factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ExperimentExposure)
                .where(ExperimentExposure.experiment_id == experiment_id)
            )
            or 0
        )


def _raw_exposure_events(factory: SessionFactory, session_id: uuid.UUID) -> int:
    with factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(UserEvent)
                .where(
                    UserEvent.event_type == "experiment_exposure",
                    UserEvent.session_id == session_id,
                )
            )
            or 0
        )


def _rage(client: TestClient, sid: uuid.UUID, at: datetime) -> None:
    for i in range(4):
        payload = {"generation": 1, "component": "plan_team_pro_cta"}
        _post_event(client, sid, "button_click", payload, at + timedelta(milliseconds=300 * i))


def _form_error(client: TestClient, sid: uuid.UUID, at: datetime) -> None:
    payload = {"generation": 1, "component": "signup_form", "field": "email", "reason": "required"}
    _post_event(client, sid, "form_error", payload, at)


def test_exposure_counts_only_while_running(
    factory: SessionFactory, client: TestClient, world: World
) -> None:
    experiment = _running(factory, world, "itest_pause_exposure")
    s = sessions_for(experiment.experiment_key, 5000, "candidate", 4, "pe")
    payload = exposure_payload(experiment, "candidate")
    _post_event(client, s[0], "experiment_exposure", payload, _at(10))  # A: running -> counted
    assert _exposures(factory, experiment.id) == 1
    assert pause_experiment(factory, experiment.id, at=_at(30)).changed
    assert _assignment(client, s[1], world.page_id) == {"status": "none"}  # paused: Generation 0
    _post_event(client, s[1], "experiment_exposure", payload, _at(25))  # B: delayed, after pause
    _post_event(client, s[2], "experiment_exposure", payload, _at(35))  # B: during the pause
    assert _exposures(factory, experiment.id) == 1
    assert _raw_exposure_events(factory, s[1]) == 1  # raw telemetry is kept ...
    assert start_experiment(factory, experiment.id, at=_at(45)).changed  # resume
    _post_event(client, s[2], "experiment_exposure", payload, _at(40))  # happened while paused
    assert _exposures(factory, experiment.id) == 1  # ... but is never an exposure
    assert complete_experiment(factory, experiment.id, at=_at(60)).changed
    assert _assignment(client, s[3], world.page_id) == {"status": "none"}
    _post_event(client, s[3], "experiment_exposure", payload, _at(50))  # C: after completion
    assert _exposures(factory, experiment.id) == 1
    # Refusals that happened inside a window are integrity findings (s1 at 25, s3 at 50);
    # the ones that happened while paused (s2 at 35 and 40) are expected, not findings.
    report = analyze_experiment(factory, experiment.id, AS_OF).report
    assert report["exposed_sessions"] == {"control": 0, "candidate": 1}
    assert report["integrity"]["rejected_exposure_sessions"] == 2


def test_outcomes_count_only_inside_collection_windows(
    factory: SessionFactory, client: TestClient, world: World
) -> None:
    experiment = _running(factory, world, "itest_pause_outcomes")
    s = sessions_for(experiment.experiment_key, 5000, "candidate", 6, "po")
    payload = exposure_payload(experiment, "candidate")
    for i, sid in enumerate(s):
        _post_event(client, sid, "experiment_exposure", payload, _at(10 + i / 10))
    _rage(client, s[0], _at(20))  # D: before the pause -> counted
    assert pause_experiment(factory, experiment.id, at=_at(30)).changed
    _rage(client, s[1], _at(35))  # D: during the pause -> not counted
    _form_error(client, s[1], _at(36))
    assert start_experiment(factory, experiment.id, at=_at(45)).changed
    _form_error(client, s[2], _at(50))  # D: after resume -> counted
    _form_error(client, s[3], _at(40))  # delayed: happened during the pause -> not counted
    assert pause_experiment(factory, experiment.id, at=_at(60)).changed  # F: second cycle
    _rage(client, s[4], _at(65))
    assert start_experiment(factory, experiment.id, at=_at(75)).changed
    _rage(client, s[4], _at(80))  # counted (second resume)
    assert complete_experiment(factory, experiment.id, at=_at(90)).changed
    _rage(client, s[5], _at(100))  # E: after completion -> not counted
    _form_error(client, s[5], _at(101))

    analysis = analyze_experiment(factory, experiment.id, AS_OF)
    report = analysis.report
    assert report["exposed_sessions"] == {"control": 0, "candidate": 6}
    assert report["primary"]["candidate"]["successes"] == 2  # s0 (20) and s4 (80)
    form = next(g for g in report["guardrails"] if g["metric"] == "form_error_session_rate")
    assert form["candidate"]["successes"] == 1  # s2 (50)
    assert report["collection_windows"] == [
        [STARTED.isoformat(), _at(30).isoformat()],
        [_at(45).isoformat(), _at(60).isoformat()],
        [_at(75).isoformat(), _at(90).isoformat()],
    ]
    # H: a rerun with the same as_of is identical, and the history is fully auditable.
    assert analyze_experiment(factory, experiment.id, AS_OF).report_hash == analysis.report_hash
    with factory() as session:
        history = session.execute(
            select(
                ExperimentLifecycleEvent.sequence,
                ExperimentLifecycleEvent.from_status,
                ExperimentLifecycleEvent.to_status,
                ExperimentLifecycleEvent.occurred_at,
            )
            .where(ExperimentLifecycleEvent.experiment_id == experiment.id)
            .order_by(ExperimentLifecycleEvent.sequence)
        ).all()
    assert [tuple(h) for h in history] == [
        (0, None, "draft", CREATED),
        (1, "draft", "running", STARTED),
        (2, "running", "paused", _at(30)),
        (3, "paused", "running", _at(45)),
        (4, "running", "paused", _at(60)),
        (5, "paused", "running", _at(75)),
        (6, "running", "completed", _at(90)),
    ]


def test_invalid_lifecycle_moves_and_history_edits_are_refused(
    factory: SessionFactory, connection: Connection, world: World
) -> None:
    experiment = _running(factory, world, "itest_lifecycle_history")
    # G: duplicate / out-of-order transitions are refused by the service ...
    assert start_experiment(factory, experiment.id, at=_at(5)).reasons == (
        "experiment_already_running",
    )
    assert pause_experiment(factory, experiment.id, at=STARTED).reasons == (
        "transition_time_not_after",
    )
    assert pause_experiment(factory, experiment.id, at=_at(5)).changed
    assert pause_experiment(factory, experiment.id, at=_at(6)).reasons == (
        "transition_not_allowed",
    )
    # ... and by the database.
    for statement, match in (
        ("UPDATE experiment SET status='paused', status_changed_at=:t WHERE id=:id", "not allowed"),
        ("UPDATE experiment SET status='running', status_changed_at=:e WHERE id=:id", "increase"),
        (
            "INSERT INTO experiment_lifecycle_event (id, experiment_id, sequence, from_status, "
            "to_status, occurred_at) VALUES (gen_random_uuid(), :id, 3, 'paused', 'running', :t)",
            "does not match",
        ),
        (
            "INSERT INTO experiment_lifecycle_event (id, experiment_id, sequence, from_status, "
            "to_status, occurred_at) VALUES (gen_random_uuid(), :id, 2, 'running', 'paused', :e)",
            "does not match|uq_experiment_lifecycle_sequence|breaks",
        ),
        # I: the history itself is immutable.
        (
            "UPDATE experiment_lifecycle_event SET occurred_at = :t WHERE experiment_id = :id",
            "immutable",
        ),
        ("DELETE FROM experiment_lifecycle_event WHERE experiment_id = :id", "immutable"),
    ):
        with pytest.raises(Exception, match=match), connection.begin_nested():
            connection.execute(text(statement), {"id": experiment.id, "t": _at(20), "e": _at(5)})
    with factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ExperimentLifecycleEvent)
            .where(ExperimentLifecycleEvent.experiment_id == experiment.id)
        )
    assert count == 3  # draft, running, paused — nothing forged, nothing lost
