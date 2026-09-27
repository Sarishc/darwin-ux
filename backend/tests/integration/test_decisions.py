"""The decision gate against darwin_test: real research runs, real persistence, fail-closed CHECKs.

Real Step 10 research runs (fixture Product Memory, FakeLLMProvider) are
decided; synthetic artifacts (decisions.evaluation.write_artifact) cover shapes
Step 10 does not produce on its own. All on the rolled-back `connection`,
except the migration test, which cleans up after itself.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, delete, func, inspect, select, text, update
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.models import (
    BehaviorSignal,
    DecisionRun,
    Hypothesis,
    HypothesisRun,
    KnowledgeDocument,
    ResearchRun,
    ResearchStep,
)
from darwin.decisions.evaluation import (
    INJECTION,
    Artifact,
    load_dataset,
    run_evaluation,
    write_artifact,
)
from darwin.decisions.evaluation import deciders as evaluation_deciders
from darwin.decisions.fake import FakeDecider, FakeDeciderMode
from darwin.decisions.llm import LLMDecider
from darwin.decisions.request import DecisionInputError
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.llm.fake import CritiqueMode, FakeLLMProvider, FakeMode
from darwin.memory.corpus import CorpusEntry, load_corpus, signal_definitions_document
from darwin.memory.embeddings import HashingEmbeddingProvider
from darwin.memory.ingest import ingest_corpus
from darwin.research.service import resume_research, run_research

pytestmark = pytest.mark.integration

SessionFactory = Callable[[], Session]
FIXTURES = Path(__file__).parents[1] / "fixtures"
EMBEDDER = HashingEmbeddingProvider()
T0 = datetime(2026, 9, 27, 11, 0, tzinfo=UTC)


def _memory(factory: SessionFactory) -> None:
    docs = load_corpus(
        (CorpusEntry("repo_document", "pricing_spec.md", "markdown"),),
        root=FIXTURES / "hypotheses",
        include_generated=False,
    )
    with factory() as session:
        ingest_corpus(session, EMBEDDER, [*docs, signal_definitions_document()])


def _signal(factory: SessionFactory) -> uuid.UUID:
    signal_id = uuid.uuid4()
    with factory() as session:
        session.add(
            BehaviorSignal(
                signal_id=signal_id,
                signal_type="rage_click",
                detector_version="1",
                session_id=uuid.uuid4(),
                window_start=T0,
                window_end=T0 + timedelta(seconds=1.5),
                evidence={
                    "component": "plan_team_pro_cta",
                    "count": 4,
                    "threshold": 4,
                    "window_seconds": 2.0,
                    "event_ids": [],
                },
            )
        )
        session.commit()
    return signal_id


def _research(
    factory: SessionFactory,
    mode: FakeMode = "grounded",
    critique: CritiqueMode = "accept",
    resume: str | None = None,
) -> uuid.UUID:
    outcome = run_research(factory, _signal(factory), FakeLLMProvider(mode, critique), EMBEDDER)
    if resume:
        outcome = resume_research(factory, outcome.run_id, resume)
    return outcome.run_id


@pytest.fixture
def factory(test_session_factory: SessionFactory) -> SessionFactory:
    _memory(test_session_factory)
    return test_session_factory


def _snapshot(connection: Connection, run_id: uuid.UUID) -> tuple[Any, ...]:
    run = connection.execute(select(ResearchRun).where(ResearchRun.id == run_id)).one()
    hypothesis = connection.execute(
        select(Hypothesis).where(Hypothesis.id == run.hypothesis_id)
    ).one()
    steps = connection.scalar(
        select(func.count()).select_from(ResearchStep).where(ResearchStep.run_id == run_id)
    )
    return tuple(run), tuple(hypothesis), steps


# ---- 1-4. a real research run -----------------------------------------------------------------


def test_real_research_run_is_decided_and_persisted(
    factory: SessionFactory, connection: Connection
) -> None:
    run_id = _research(factory)
    before = _snapshot(connection, run_id)

    outcome = decide_research_run(factory, run_id, RulesDecider())

    assert (outcome.decision, outcome.status) == ("proceed", "decided")
    assert outcome.reason_codes == ("critique_accepted", "sufficient_evidence")
    with factory() as session:
        row = session.get(DecisionRun, outcome.decision_run_id)
        assert row is not None
        assert (row.research_run_id, row.hypothesis_id) == (run_id, outcome.hypothesis_id)
        assert (row.decider, row.decider_version, row.request_version) == (
            "rules",
            "rules.v1",
            "decision_request.v1",
        )
        assert row.request_hash == outcome.request.request_hash()
        assert row.error_type is None and row.validation_errors == []
    assert _snapshot(connection, run_id) == before  # research + hypothesis untouched
    assert outcome.request.signal.facts["component"] == "plan_team_pro_cta"


# ---- 5-8. outcomes from real and synthetic artifacts ---------------------------------------


def test_critique_reject_is_rejected(factory: SessionFactory) -> None:
    outcome = decide_research_run(factory, _research(factory, critique="reject"), RulesDecider())
    assert (outcome.decision, outcome.reason_codes) == ("reject", ("critique_rejected",))


def test_human_approved_and_rejected_research(factory: SessionFactory) -> None:
    approved = _research(factory, critique="human_review", resume="approve")
    rejected = _research(factory, critique="human_review", resume="reject")

    a = decide_research_run(factory, approved, RulesDecider())
    r = decide_research_run(factory, rejected, RulesDecider())

    assert (a.decision, a.reason_codes) == ("proceed", ("human_approved",))
    assert (r.decision, r.reason_codes) == ("reject", ("human_rejected",))


def test_low_confidence_without_a_human_needs_review(factory: SessionFactory) -> None:
    with factory() as session:
        run_id = write_artifact(session, "low-conf", Artifact(hypothesis_confidence="low"))

    outcome = decide_research_run(factory, run_id, RulesDecider())
    assert (outcome.decision, outcome.reason_codes) == ("human_review", ("low_confidence",))
    reckless = decide_research_run(factory, run_id, FakeDecider("proceed"))
    assert (reckless.decision, reckless.status) == ("human_review", "overridden")


# ---- 9-12. faults and repeat invocations ------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("malformed", "missing"),
        ("unknown_decision", "literal_error"),
        ("failure", "decider_error:DeciderFailureError"),
        ("timeout", "decider_timeout"),
        ("unavailable", "decider_unavailable"),
    ],
)
def test_decider_faults_are_recorded_as_failed_closed(
    factory: SessionFactory, mode: FakeDeciderMode, error_type: str
) -> None:
    outcome = decide_research_run(factory, _research(factory), FakeDecider(mode))

    assert (outcome.decision, outcome.status, outcome.error_type) == (
        "human_review",
        "failed_closed",
        error_type,
    )
    with factory() as session:
        row = session.get(DecisionRun, outcome.decision_run_id)
        assert row is not None and row.decider == "fake" and row.decider_decision is None
        assert row.reason_codes == ["decider_failure"]


def test_llm_provider_failure_fails_closed(factory: SessionFactory) -> None:
    decider = LLMDecider(FakeLLMProvider(decision_mode="failure"))
    outcome = decide_research_run(factory, _research(factory), decider)
    assert (outcome.decision, outcome.status) == ("human_review", "failed_closed")


def test_repeated_decisions_are_separate_audit_rows(
    factory: SessionFactory, connection: Connection
) -> None:
    run_id = _research(factory)
    first = decide_research_run(factory, run_id, RulesDecider())
    second = decide_research_run(factory, run_id, RulesDecider())

    assert first.decision_run_id != second.decision_run_id
    assert first.request_hash == second.request_hash  # same facts, same request
    count = connection.scalar(
        select(func.count()).select_from(DecisionRun).where(DecisionRun.research_run_id == run_id)
    )
    assert count == 2


# ---- 13-14. ineligible artifacts -------------------------------------------------------------


def test_ineligible_research_is_refused_without_a_decider_call(
    factory: SessionFactory, connection: Connection
) -> None:
    waiting = run_research(
        factory, _signal(factory), FakeLLMProvider(critique_mode="human_review"), EMBEDDER
    )
    decider = FakeDecider("proceed")
    with pytest.raises(DecisionInputError) as error:
        decide_research_run(factory, waiting.run_id, decider)
    assert error.value.code == "research_not_eligible" and decider.calls == 0
    with pytest.raises(DecisionInputError) as error:
        decide_research_run(factory, uuid.uuid4(), decider)
    assert error.value.code == "research_run_not_found"
    assert connection.scalar(select(func.count()).select_from(DecisionRun)) == 0


def test_superseded_signal_is_refused(factory: SessionFactory) -> None:
    run_id = _research(factory)
    with factory() as session:
        run = session.get(ResearchRun, run_id)
        assert run is not None
        session.execute(
            update(BehaviorSignal)
            .where(BehaviorSignal.signal_id == run.signal_id)
            .values(superseded_at=T0)
        )
        session.commit()
    with pytest.raises(DecisionInputError) as error:
        decide_research_run(factory, run_id, RulesDecider())
    assert error.value.code == "signal_superseded"


# ---- database-level fail-closed guarantees ------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"status": "failed_closed", "decision": "proceed"}, "failed_closed_reviews"),
        ({"status": "overridden", "decision": "proceed"}, "proceed_only_decided"),
        ({"decision": "deploy"}, "decision_is_known"),
        ({"decider": "jev_fake"}, "decider_is_known"),
    ],
)
def test_database_refuses_fail_open_rows(
    factory: SessionFactory, connection: Connection, changes: dict[str, str], constraint: str
) -> None:
    outcome = decide_research_run(factory, _research(factory), FakeDecider("failure"))
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    with pytest.raises(Exception, match=constraint), connection.begin_nested():
        connection.execute(
            text(f"UPDATE decision_run SET {sets} WHERE id = :id"),  # test-only literal SQL
            {**changes, "id": outcome.decision_run_id},
        )


# ---- injection / authority ------------------------------------------------------------------


def test_injected_text_cannot_widen_the_decision(
    factory: SessionFactory, connection: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    with factory() as session:
        run_id = write_artifact(
            session, "injected", Artifact(limitations=(INJECTION,), issues=(INJECTION,))
        )
    before = _snapshot(connection, run_id)
    llm = FakeLLMProvider(decision_mode="proceed_and_deploy")

    with caplog.at_level(logging.DEBUG, logger="darwin"):
        rules = decide_research_run(factory, run_id, RulesDecider())
        echoed = decide_research_run(factory, run_id, LLMDecider(llm))
        invented = decide_research_run(factory, run_id, FakeDecider("unknown_decision"))

    assert rules.decision == "human_review"  # critique issues, whatever the text says
    for outcome in (echoed, invented):
        assert (outcome.decision, outcome.status) == ("human_review", "failed_closed")
    [sent] = llm.requests
    assert INJECTION in sent.evidence and INJECTION not in sent.instructions
    assert _snapshot(connection, run_id) == before  # no route, budget or status changed
    everything = " ".join(str(r.__dict__.get("context")) + r.getMessage() for r in caplog.records)
    assert INJECTION not in everything and "Repeated clicks" not in everything
    decisions = connection.scalars(select(DecisionRun.decision)).all()
    assert set(decisions) <= {"proceed", "human_review", "reject"}


def test_no_mutation_tables_exist(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    assert not {t for t in tables if any(w in t for w in ("experiment", "deploy"))}
    # Step 12 adds candidate-only mutation data; nothing else mutation-related may exist.
    assert {t for t in tables if "mutation" in t} <= {"mutation_run"}


# ---- evaluation + migration ------------------------------------------------------------------


def test_decision_evaluation_is_deterministic(migrated_engine: Engine) -> None:
    dataset = load_dataset()
    makers = evaluation_deciders(False, Settings())

    first = run_evaluation(migrated_engine, dataset, makers)
    second = run_evaluation(migrated_engine, dataset, makers)

    strip = {name: [{**r.__dict__} for r in rs] for name, rs in first.items()}
    assert strip == {name: [{**r.__dict__} for r in rs] for name, rs in second.items()}
    assert all(r.correct for r in first["rules.v1"])
    assert all(not r.fail_open for rs in first.values() for r in rs if r.status == "failed_closed")
    with migrated_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(DecisionRun)) == 0


def test_migration_0007_round_trip_keeps_earlier_data(
    migrated_engine: Engine, alembic_cfg: Config
) -> None:
    def committed() -> Session:
        return Session(migrated_engine)

    try:
        _memory(committed)
        run_id = _research(committed)
        decision = decide_research_run(committed, run_id, RulesDecider())

        command.downgrade(alembic_cfg, "0006")
        assert not inspect(migrated_engine).has_table("decision_run")
        with committed() as session:
            run = session.get(ResearchRun, run_id)
            assert run is not None and run.status == "succeeded"
            assert session.get(Hypothesis, run.hypothesis_id) is not None
        command.upgrade(alembic_cfg, "head")
        with committed() as session:
            assert session.get(DecisionRun, decision.decision_run_id) is None  # dropped with table
            assert decide_research_run(committed, run_id, RulesDecider()).decision == "proceed"
    finally:
        command.upgrade(alembic_cfg, "head")
        with committed() as session:
            session.execute(delete(DecisionRun))
            session.execute(delete(ResearchStep))
            session.execute(delete(ResearchRun))
            session.execute(delete(Hypothesis))
            session.execute(delete(HypothesisRun))
            session.execute(delete(BehaviorSignal))
            session.execute(delete(KnowledgeDocument))
            session.commit()
