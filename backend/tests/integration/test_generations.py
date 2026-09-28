"""Human approval, promotion and rollback against darwin_test: the real chain, the real API,
worker and frontend harness. Rolled back, except the concurrency and migration tests,
which commit and clean up after themselves."""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, func, insert, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from darwin.api.experiments import get_session_factory
from darwin.db.models import (
    ActiveGeneration,
    BehaviorSignal,
    GenerationPromotion,
    GenerationRollback,
    PromotionApproval,
    UISpecVersion,
    UserEvent,
)
from darwin.decisions.evaluation import Artifact, write_artifact
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.experiments.evaluation import T0, World, build_world, harness_facts
from darwin.experiments.service import create_experiment
from darwin.generations.evaluation import _experiment
from darwin.generations.service import bootstrap_active, decide, promote, rollback
from darwin.mutations.apply import content_hash
from darwin.mutations.fixture import FixtureMutationGenerator
from darwin.mutations.service import generate_candidate
from darwin.mutations.specs import import_generation_zero
from darwin.sandbox.harness import NodeHarnessRunner, SpecFacts
from darwin.sandbox.service import evaluate_candidate

pytestmark = pytest.mark.integration

SessionFactory = Callable[[], Session]
PAGE = "pricing_signup"
REVIEWER = "itest-reviewer"


@pytest.fixture(scope="module")
def facts() -> dict[str, SpecFacts]:
    return harness_facts()


@pytest.fixture
def factory(test_session_factory: SessionFactory) -> SessionFactory:
    return test_session_factory


@pytest.fixture
def world(factory: SessionFactory, facts: dict[str, SpecFacts]) -> World:
    built = build_world(factory, "gtest", facts)
    bootstrap_active(factory, built.page_id)
    return built


@pytest.fixture
def client(api: TestClient, factory: SessionFactory) -> Iterator[TestClient]:
    api.app.dependency_overrides[get_session_factory] = lambda: factory  # type: ignore[attr-defined]
    yield api


def _active(client: TestClient, page: str = PAGE) -> dict[str, Any]:
    response = client.get("/api/v1/generations/active", params={"page": page})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _post(client: TestClient, sid: uuid.UUID, at: datetime, **fields: Any) -> None:
    body = {
        "event_id": str(uuid.uuid4()),
        "event_type": "button_click",
        "session_id": str(sid),
        "occurred_at": at.isoformat(),
        "payload": {"generation": fields.get("ui_generation", 0), "component": "plan_team_pro_cta"},
        **fields,
    }
    assert client.post("/api/v1/telemetry/events", json=body).status_code == 202


# ---- the realistic end-to-end run ------------------------------------------------------------


def test_generation_0_to_1_by_human_promotion_and_back_by_human_rollback(
    factory: SessionFactory, client: TestClient, connection: Connection
) -> None:
    # Generation 0 -> signal -> decision -> Step 12 candidate -> REAL Step 13 evaluation.
    with factory() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(session, "gen-e2e", Artifact())
    decision = decide_research_run(factory, research_run_id, RulesDecider())
    mutation = generate_candidate(factory, decision.decision_run_id, FixtureMutationGenerator())
    assert mutation.candidate_spec_id is not None
    evaluation = evaluate_candidate(factory, mutation.candidate_spec_id)
    assert evaluation.recommendation == "pass"
    assert bootstrap_active(factory, PAGE) == ("created", 0)
    assert bootstrap_active(factory, PAGE) == ("unchanged", 0)  # idempotent
    gen0 = _active(client)
    assert (gen0["status"], gen0["generation"]) == ("active", 0)

    # A completed Step 14 experiment with an evidence_ready analysis (simulated traffic).
    with factory() as session:
        candidate = session.get(UISpecVersion, mutation.candidate_spec_id)
        assert candidate is not None
    world = World(
        "e2e",
        PAGE,
        uuid.UUID(gen0["spec_version_id"]),
        {"pass": candidate.id},
        {"pass": evaluation.evaluation_run_id},
        decision.decision_run_id,
    )
    experiment, analysis_id = _experiment(factory, world, "gen_e2e_rage_fix")
    assert analysis_id is not None

    # Human review and approval. Approval alone changes NOTHING.
    approval = decide(
        factory, analysis_id, "approve", REVIEWER, "Fewer rage-click sessions; guardrails ok."
    )
    assert approval.recorded and approval.target_generation == 1
    assert _active(client)["generation"] == 0
    refused = promote(factory, approval.approval_id, REVIEWER, f"{PAGE}:2")  # type: ignore[arg-type]
    assert refused.reasons == ("confirmation_mismatch",) and _active(client)["generation"] == 0

    # Explicit human promotion.
    promoted = promote(factory, approval.approval_id, REVIEWER, f"{PAGE}:1")  # type: ignore[arg-type]
    assert promoted.changed and (promoted.from_generation, promoted.to_generation) == (0, 1)
    gen1 = _active(client)
    assert (gen1["status"], gen1["generation"]) == ("active", 1)
    assert gen1["spec_hash"] == content_hash(gen1["spec"]) and gen1["spec"]["generation"] == 1
    assert set(gen1) == {"status", "generation", "spec_version_id", "spec_hash", "spec"}

    # Generation 1 renders through the REAL frontend (Zod + registry + SpecPage).
    rendered = NodeHarnessRunner().run({gen1["spec_hash"]: gen1["spec"]})[gen1["spec_hash"]]
    assert rendered.schema_.ok and rendered.render.ok
    assert {c.component_id: c.reveal_delay_ms for c in rendered.ctas}["plan_team_pro_cta"] == 0

    # Telemetry from Generation 1 is verified server-side and its signal is attributed.
    sid = uuid.uuid4()
    for i in range(4):
        _post(
            client,
            sid,
            T0 + timedelta(milliseconds=300 * i),
            ui_generation=1,
            ui_spec_hash=gen1["spec_hash"],
            ui_spec_version_id=gen1["spec_version_id"],
        )
    with factory() as session:
        verified = session.scalars(
            select(UserEvent.ui_spec_version_id).where(UserEvent.session_id == sid)
        ).all()
        signal = session.scalar(select(BehaviorSignal).where(BehaviorSignal.session_id == sid))
    assert [str(v) for v in verified] == [gen1["spec_version_id"]] * 4
    assert signal is not None and (signal.ui_attribution, str(signal.ui_spec_version_id)) == (
        "single",
        gen1["spec_version_id"],
    )

    # The rest of the system follows the active generation.
    stale = create_experiment(
        factory,
        experiment_key="gen_e2e_stale",
        candidate_evaluation_run_id=evaluation.evaluation_run_id,
        candidate_allocation_bp=1000,
        primary_metric="rage_click_session_rate",
        guardrail_metrics=["form_error_session_rate"],
        minimum_sample_per_variant=100,
        traffic_source="simulated",
    )
    assert stale.reasons == ("control_not_active_generation",)  # its control is Generation 0
    again = generate_candidate(factory, decision.decision_run_id, FixtureMutationGenerator())
    assert (again.status, again.error_type) == ("stale_provenance", "signal_generation_unknown")

    # Explicit human rollback: pointer back to Generation 0; Generation 1 stays.
    back = rollback(factory, PAGE, REVIEWER, "Rollback drill.", f"{PAGE}:0")
    assert back.changed and (back.from_generation, back.to_generation) == (1, 0)
    assert _active(client) == gen0
    with factory() as session:
        kept = session.get(UISpecVersion, uuid.UUID(gen1["spec_version_id"]))
        original = session.get(UISpecVersion, candidate.id)
        gen0_row = session.get(UISpecVersion, uuid.UUID(gen0["spec_version_id"]))
        assert (
            kept is not None
            and kept.status == "promoted"
            and content_hash(kept.spec) == kept.content_hash
        )
        assert original is not None and original.status == "candidate"  # never changed
        assert gen0_row is not None and content_hash(gen0_row.spec) == gen0["spec_hash"]
        records = [
            session.scalar(select(func.count()).select_from(m))
            for m in (PromotionApproval, GenerationPromotion, GenerationRollback)
        ]
    assert records == [1, 1, 1]


# ---- atomicity, immutability, forgery ----------------------------------------------------------


def test_a_failed_promotion_leaves_nothing_behind(factory: SessionFactory, world: World) -> None:
    _, analysis_id = _experiment(factory, world, "gtest_atomic")
    approval = decide(factory, analysis_id, "approve", REVIEWER, "ok")  # type: ignore[arg-type]

    def crash(_session: Session) -> None:
        raise RuntimeError("crash between the writes and the commit")

    with pytest.raises(RuntimeError):
        promote(factory, approval.approval_id, REVIEWER, f"{world.page_id}:1", before_commit=crash)  # type: ignore[arg-type]
    with factory() as session:
        pointer = session.get(ActiveGeneration, world.page_id)
        assert pointer is not None and (pointer.generation, pointer.change_kind) == (0, "bootstrap")
        assert session.scalar(select(func.count()).select_from(GenerationPromotion)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(UISpecVersion)
                .where(UISpecVersion.status == "promoted")
            )
            == 0
        )
    assert promote(factory, approval.approval_id, REVIEWER, f"{world.page_id}:1").changed  # type: ignore[arg-type]


def test_audit_records_and_pointer_cannot_be_rewritten(
    factory: SessionFactory, connection: Connection, world: World
) -> None:
    _, analysis_id = _experiment(factory, world, "gtest_immutable")
    approval = decide(factory, analysis_id, "approve", REVIEWER, "ok")  # type: ignore[arg-type]
    assert promote(factory, approval.approval_id, REVIEWER, f"{world.page_id}:1").changed  # type: ignore[arg-type]
    assert rollback(factory, world.page_id, REVIEWER, "drill", f"{world.page_id}:0").changed
    for statement in (
        "UPDATE promotion_approval SET decision = 'reject'",
        "DELETE FROM promotion_approval",
        "UPDATE generation_promotion SET to_generation = 5",
        "DELETE FROM generation_promotion",
        "UPDATE generation_rollback SET reason = 'rewritten'",
        "DELETE FROM generation_rollback",
    ):
        with pytest.raises(Exception, match="immutable"), connection.begin_nested():
            connection.execute(text(statement))
    for statement, match in (
        ("DELETE FROM active_generation", "cannot be deleted"),
        (
            f"UPDATE active_generation SET ui_spec_version_id = '{world.candidate_ids['pass']}', "
            "change_kind = 'promotion', change_id = gen_random_uuid()",
            "must be a generation",
        ),
        (
            "UPDATE active_generation SET change_kind = 'rollback', change_id = gen_random_uuid()",
            "does not match",
        ),
    ):
        with pytest.raises(Exception, match=match), connection.begin_nested():
            connection.execute(text(statement))
    with (
        pytest.raises(Exception, match="must be a generation|bootstrapped to Generation 0"),
        connection.begin_nested(),
    ):
        connection.execute(
            insert(ActiveGeneration).values(
                page_id="other_page",
                ui_spec_version_id=world.candidate_ids["pass"],
                generation=0,
                change_kind="bootstrap",
            )
        )


def test_active_generation_endpoint_fails_closed(api: TestClient) -> None:
    def broken() -> Session:
        raise RuntimeError("database unavailable")

    api.app.dependency_overrides[get_session_factory] = lambda: broken  # type: ignore[attr-defined]
    assert _active(api) == {"status": "none"}
    assert api.get("/api/v1/generations/active", params={"page": "../etc"}).status_code == 422
    assert api.post("/api/v1/generations/active", params={"page": PAGE}).status_code == 405


# ---- concurrency (committed data) --------------------------------------------------------------


@pytest.fixture
def committed(migrated_engine: Engine) -> Iterator[SessionFactory]:
    """Real committed sessions on darwin_test (empty between tests); truncated afterwards.
    TRUNCATE does not fire the row-level immutability triggers."""
    factory = sessionmaker(migrated_engine)
    try:
        yield factory
    finally:
        with migrated_engine.begin() as conn:
            tables = [t for t in inspect(conn).get_table_names() if t != "alembic_version"]
            conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


def test_concurrent_promotions_create_exactly_one_generation(
    committed: SessionFactory, facts: dict[str, SpecFacts]
) -> None:
    world = build_world(committed, "gconc", facts)
    bootstrap_active(committed, world.page_id)
    _, first = _experiment(committed, world, "gconc_a")
    _, second = _experiment(committed, world, "gconc_b")
    approvals = [
        decide(committed, analysis, "approve", REVIEWER, "ok").approval_id  # type: ignore[arg-type]
        for analysis in (first, second)
    ]
    # Two different approvals (both from Generation 0, both targeting Generation 1), and the
    # same approval twice: four simultaneous promotion attempts.
    attempts = [approvals[0], approvals[1], approvals[0], approvals[1]]
    barrier = threading.Barrier(len(attempts))
    results: list[Any] = [None] * len(attempts)

    def run(i: int, approval_id: uuid.UUID) -> None:
        barrier.wait()
        results[i] = promote(committed, approval_id, f"reviewer-{i}", f"{world.page_id}:1")

    threads = [threading.Thread(target=run, args=(i, a)) for i, a in enumerate(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(r is not None for r in results)
    winners = [r for r in results if r.changed]
    assert len(winners) == 1, [r.reasons for r in results]
    for loser in (r for r in results if not r.changed):
        assert set(loser.reasons) & {
            "approval_already_used",
            "stale_source_generation",
            "evidence_changed",
            "concurrent_or_duplicate_promotion",
        }, loser.reasons
    with committed() as session:
        pointer = session.get(ActiveGeneration, world.page_id)
        assert pointer is not None and pointer.generation == 1
        assert str(pointer.ui_spec_version_id) == str(winners[0].to_spec_id)
        generation_1 = session.scalars(
            select(UISpecVersion).where(
                UISpecVersion.page_id == world.page_id, UISpecVersion.generation == 1
            )
        ).all()
        assert len(generation_1) == 1  # no duplicate Generation 1
        assert session.scalar(select(func.count()).select_from(GenerationPromotion)) == 1


# ---- migration --------------------------------------------------------------------------------


def test_migration_0011_round_trip_keeps_earlier_data(
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
                payload={"page": PAGE},
            )
        )
        session.commit()
    try:
        command.downgrade(alembic_cfg, "0010")
        inspector = inspect(migrated_engine)
        tables = set(inspector.get_table_names())
        assert (
            not {
                "active_generation",
                "promotion_approval",
                "generation_promotion",
                "generation_rollback",
            }
            & tables
        )
        assert "experiment_lifecycle_event" in tables  # 0010 untouched
        assert "ui_generation" not in {c["name"] for c in inspector.get_columns("user_event")}
        with migrated_engine.connect() as conn:
            count = conn.scalar(
                text("SELECT count(*) FROM user_event WHERE event_id = :id"), {"id": event_id}
            )
            assert count == 1
        command.upgrade(alembic_cfg, "head")
        with migrated_engine.connect() as conn:
            triggers = set(
                conn.scalars(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgname ~ '(generation|promot|active)'"
                    )
                )
            )
        assert {
            "active_generation_guard",
            "generation_promotion_applied",
            "promoted_spec_matches_candidate",
        } <= triggers
    finally:
        command.upgrade(alembic_cfg, "head")
        with Session(migrated_engine) as session:
            session.execute(delete(UserEvent).where(UserEvent.event_id == event_id))
            session.commit()


def test_promotion_golden_evaluation_never_promotes_without_authority(
    migrated_engine: Engine, facts: dict[str, SpecFacts]
) -> None:
    from darwin.generations.evaluation import load_dataset, metrics, run_evaluation

    results = run_evaluation(migrated_engine, load_dataset(), facts)
    assert all(r.correct for r in results), [
        (r.id, r.failed_checks) for r in results if not r.correct
    ]
    m = metrics(results)
    assert (m["unauthorized_promotion_count"], m["invalid_rollback_count"]) == (0, 0)
    with migrated_engine.connect() as conn:
        assert (
            conn.scalar(select(func.count()).select_from(GenerationPromotion)) == 0
        )  # rolled back
