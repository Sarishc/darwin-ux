"""Candidate sandbox evaluation against darwin_test with the REAL frontend harness
(the app's Zod schema, registry and SpecPage in jsdom). Rolled back, except the
migration test, which cleans up after itself."""

import logging
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, delete, func, insert, inspect, select, text
from sqlalchemy.orm import Session

from darwin.db.models import (
    BehaviorSignal,
    CandidateEvaluationRun,
    DecisionRun,
    Hypothesis,
    HypothesisRun,
    MutationRun,
    ResearchRun,
    UISpecVersion,
    UserEvent,
)
from darwin.decisions.evaluation import Artifact, write_artifact
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.llm.fake import FakeLLMProvider
from darwin.memory.corpus import REPO_ROOT
from darwin.mutations.fixture import FixtureMode, FixtureMutationGenerator
from darwin.mutations.llm import LLMMutationGenerator
from darwin.mutations.port import MutationGenerator
from darwin.mutations.service import generate_candidate
from darwin.mutations.specs import import_generation_zero
from darwin.sandbox.evaluation import load_dataset, run_evaluation
from darwin.sandbox.harness import HarnessError, NodeHarnessRunner, SpecFacts
from darwin.sandbox.provenance import CandidateNotFoundError
from darwin.sandbox.service import evaluate_candidate

pytestmark = pytest.mark.integration

SessionFactory = Callable[[], Session]
UI_SPEC_DIR = REPO_ROOT / "frontend" / "src" / "ui-spec"


class CountingRunner:
    def __init__(self, error: HarnessError | None = None) -> None:
        self.calls, self.error, self.real = 0, error, NodeHarnessRunner()

    def run(self, specs: Any) -> dict[str, SpecFacts]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.real.run(specs)


def _candidate(
    factory: SessionFactory,
    key: str,
    artifact: Artifact | None = None,
    generator: MutationGenerator | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(session, key, artifact or Artifact())
    decision = decide_research_run(factory, research_run_id, RulesDecider())
    outcome = generate_candidate(
        factory, decision.decision_run_id, generator or FixtureMutationGenerator()
    )
    assert outcome.candidate_spec_id is not None
    return outcome.candidate_spec_id, outcome.mutation_run_id


@pytest.fixture
def factory(test_session_factory: SessionFactory) -> SessionFactory:
    return test_session_factory


# ---- the real chain ------------------------------------------------------------------------


def test_rage_click_fix_passes_every_gate(factory: SessionFactory, connection: Connection) -> None:
    candidate_id, mutation_id = _candidate(factory, "rage")
    events_before = connection.scalar(select(func.count()).select_from(UserEvent))
    specs_before = connection.execute(select(UISpecVersion.id, UISpecVersion.content_hash)).all()

    outcome = evaluate_candidate(factory, candidate_id)

    assert (outcome.status, outcome.recommendation) == ("completed", "pass")
    assert {c["status"] for c in outcome.categories.values()} == {"pass"}
    assert outcome.mutation_run_id == mutation_id and outcome.harness_called
    with factory() as session:
        row = session.get(CandidateEvaluationRun, outcome.evaluation_run_id)
        assert row is not None
        assert (row.evaluator_version, row.harness_version, row.reason_codes) == (
            "candidate_eval.v1",
            "sandbox_harness.v1",
            ["all_gates_passed"],
        )
        assert row.error_type is None and row.duration_ms > 0
        ux = row.category_results["ux_intent"]["checks"][0]
        assert ux["aligned"] == ["plan_team_pro_cta.feedback"]
    # telemetry was captured, never sent; specs untouched
    assert connection.scalar(select(func.count()).select_from(UserEvent)) == events_before
    assert (
        connection.execute(select(UISpecVersion.id, UISpecVersion.content_hash)).all()
        == specs_before
    )


def test_harmful_but_safe_candidate_is_rejected(factory: SessionFactory) -> None:
    candidate_id, _ = _candidate(
        factory,
        "harmful",
        Artifact(affected_component="plan_starter_cta"),
        LLMMutationGenerator(FakeLLMProvider()),
    )
    with factory() as session:
        run = session.scalars(
            select(MutationRun).where(MutationRun.candidate_spec_id == candidate_id)
        ).one()
        assert run.status == "succeeded"  # Step 12: safe and valid

    outcome = evaluate_candidate(factory, candidate_id)

    assert outcome.recommendation == "reject" and "ux_intent_regression" in outcome.reason_codes
    for category in ("schema", "render", "functional", "accessibility", "regression"):
        assert outcome.categories[category]["status"] == "pass"  # safe...
    assert outcome.categories["ux_intent"]["status"] == "fail"  # ...not useful


@pytest.mark.parametrize(
    ("mode", "recommendation"),
    [("auto", "pass"), ("per_field_only", "pass"), ("inline_only", "human_review")],
)
def test_error_burst_candidates(
    factory: SessionFactory, mode: FixtureMode, recommendation: str
) -> None:
    candidate_id, _ = _candidate(
        factory,
        f"burst-{mode}",
        Artifact(signal_type="error_burst", affected_component="signup_form"),
        FixtureMutationGenerator(mode),
    )
    assert evaluate_candidate(factory, candidate_id).recommendation == recommendation


# ---- provenance and evaluator failures ------------------------------------------------------


def test_provenance_failures_reject_before_the_harness(factory: SessionFactory) -> None:
    candidate_id, _ = _candidate(factory, "prov")
    runner = CountingRunner()
    with factory() as session:
        baseline_id = session.scalars(
            select(UISpecVersion.id).where(UISpecVersion.generation == 0)
        ).one()
        failed = MutationRun(
            decision_run_id=session.scalars(select(DecisionRun.id)).first(),
            source_spec_id=baseline_id,
            generator="fixture",
            generator_version="test",
            request_version="mutation_request.v1",
            request_hash="a" * 64,
            status="validation_failed",
            error_type="protected_property",
        )
        session.add(failed)
        session.commit()
        failed_id = failed.id
    as_baseline = evaluate_candidate(factory, baseline_id, runner)
    failed_run = evaluate_candidate(factory, candidate_id, runner, mutation_run_id=failed_id)
    for outcome, code in (
        (as_baseline, "not_a_candidate"),
        (failed_run, "mutation_run_not_succeeded"),
    ):
        assert (outcome.status, outcome.recommendation, outcome.error_type) == (
            "provenance_failed",
            "reject",
            code,
        )
    assert runner.calls == 0
    with pytest.raises(CandidateNotFoundError):
        evaluate_candidate(factory, uuid.uuid4(), runner)


def test_evaluator_unavailable_fails_closed(factory: SessionFactory) -> None:
    candidate_id, _ = _candidate(factory, "unavailable")
    outcome = evaluate_candidate(
        factory, candidate_id, CountingRunner(HarnessError("harness_unavailable"))
    )
    assert (outcome.status, outcome.recommendation, outcome.error_type) == (
        "evaluator_error",
        "human_review",
        "harness_unavailable",
    )


# ---- persistence ----------------------------------------------------------------------------


def test_repeated_evaluations_are_separate_immutable_rows(
    factory: SessionFactory, connection: Connection
) -> None:
    candidate_id, _ = _candidate(factory, "repeat")
    first = evaluate_candidate(factory, candidate_id)
    second = evaluate_candidate(factory, candidate_id)
    assert first.evaluation_run_id != second.evaluation_run_id
    assert first.categories == second.categories
    with pytest.raises(Exception, match="immutable"), connection.begin_nested():
        connection.execute(
            text("UPDATE candidate_evaluation_run SET recommendation = 'reject' WHERE id = :id"),
            {"id": first.evaluation_run_id},
        )


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        (
            {"status": "evaluator_error", "recommendation": "pass", "error_type": "x"},
            "pass_only_when_completed",
        ),
        (
            {"status": "provenance_failed", "recommendation": "human_review", "error_type": "x"},
            "provenance_failure_rejects",
        ),
    ],
)
def test_database_refuses_fail_open_evaluations(
    factory: SessionFactory, connection: Connection, changes: dict[str, str], constraint: str
) -> None:
    candidate_id, _ = _candidate(factory, "db")
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "candidate_spec_id": candidate_id,
        "evaluator_version": "candidate_eval.v1",
        "status": "completed",
        "recommendation": "pass",
        "reason_codes": ["all_gates_passed"],
        "category_results": {},
        "error_type": None,
        "duration_ms": 1.0,
    } | changes
    with pytest.raises(Exception, match=constraint), connection.begin_nested():
        connection.execute(insert(CandidateEvaluationRun).values(**row))


# ---- sandbox hygiene -----------------------------------------------------------------------


def test_no_files_left_or_written_and_logs_clean(
    factory: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    candidate_id, _ = _candidate(factory, "hygiene")
    tmp = Path(tempfile.gettempdir())
    before = {p.name for p in tmp.glob("darwin-sandbox-*")}
    with caplog.at_level(logging.DEBUG, logger="darwin"):
        evaluate_candidate(factory, candidate_id)
    assert {p.name for p in tmp.glob("darwin-sandbox-*")} == before  # temp dir removed
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(UI_SPEC_DIR)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
    records = [r for r in caplog.records if r.name == "darwin.sandbox.service"]
    everything = " ".join(str(r.__dict__.get("context")) for r in records)
    assert records and "Notewise" not in everything and "Get started" not in everything


# ---- golden evaluation + migration ------------------------------------------------------------


def test_sandbox_golden_evaluation_is_deterministic_and_never_fails_open(
    migrated_engine: Engine,
) -> None:
    dataset = load_dataset()
    first = run_evaluation(migrated_engine, dataset)
    second = run_evaluation(migrated_engine, dataset)
    assert [r.__dict__ for r in first] == [r.__dict__ for r in second]
    assert all(not r.failed_checks for r in first), [
        (r.id, r.failed_checks) for r in first if r.failed_checks
    ]
    assert sum(r.fail_open for r in first) == 0
    with migrated_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(CandidateEvaluationRun)) == 0


def test_migration_0009_round_trip_keeps_earlier_data(
    migrated_engine: Engine, alembic_cfg: Config
) -> None:
    def committed() -> Session:
        return Session(migrated_engine)

    try:
        candidate_id, mutation_id = _candidate(committed, "migration")
        evaluate_candidate(committed, candidate_id)
        command.downgrade(alembic_cfg, "0008")
        assert not inspect(migrated_engine).has_table("candidate_evaluation_run")
        with committed() as session:
            assert session.get(UISpecVersion, candidate_id) is not None
            assert session.get(MutationRun, mutation_id) is not None
        command.upgrade(alembic_cfg, "head")
        assert evaluate_candidate(committed, candidate_id).recommendation == "pass"
    finally:
        command.upgrade(alembic_cfg, "head")
        with committed() as session:
            session.execute(delete(CandidateEvaluationRun))
            session.execute(delete(MutationRun))
            session.execute(text("DELETE FROM ui_spec_version WHERE parent_id IS NOT NULL"))
            session.execute(delete(UISpecVersion))
            session.execute(delete(DecisionRun))
            session.execute(delete(ResearchRun))
            session.execute(delete(Hypothesis))
            session.execute(delete(HypothesisRun))
            session.execute(delete(BehaviorSignal))
            session.commit()
