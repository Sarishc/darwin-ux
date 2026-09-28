"""Observability contract evaluation (make observability-eval). Rolled back; nothing persists.

    python -m darwin.observability.evaluation [--output artifacts/observability-eval.json]

Each scenario runs real DarwinUX code under an in-memory capture (no network, no
stdout parsing) inside a SAVEPOINT on the configured database. SENTINEL strings are
planted where sensitive content lives — the telemetry payload, the retrieval query,
the LLM evidence, the candidate spec, the reviewer's reason — and every exported span
attribute, span event and metric label is scanned for them.

Safety metrics (all must be 0):
  FORBIDDEN_ATTRIBUTE_COUNT     exported span attributes with a non-allowlisted key, a
                                sentinel, a session id, or any span event
  FORBIDDEN_METRIC_LABEL_COUNT  metric points with a non-allowlisted label key, an id-like
                                value or a sentinel
  OBSERVABILITY_CAUSED_OPERATION_FAILURE_COUNT
                                operations whose result differs when the span exporter
                                fails on every export
"""

import argparse
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, delete, insert, select
from sqlalchemy.orm import Session

from darwin.api.experiments import get_session_factory
from darwin.api.telemetry import get_queue
from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import KnowledgeDocument, QueueMessage
from darwin.logging_config import JsonFormatter, configure_logging
from darwin.memory.corpus import REPO_ROOT
from darwin.memory.embeddings import HashingEmbeddingProvider
from darwin.memory.ingest import ingest_corpus
from darwin.memory.retrieval import retrieve
from darwin.queue.postgres import PostgresQueue
from darwin.telemetry.messages import TELEMETRY_EVENT
from darwin.worker import Outcome, handle, run_once, telemetry_processor

from . import span
from .attributes import METRIC_LABELS, SPAN_ATTRIBUTES, Rejected
from .testing import Captured, FailingSpanExporter, capture
from .tracing import setup_observability, shutdown

GOLDEN_PATH = REPO_ROOT / "backend" / "tests" / "evals" / "golden" / "observability.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "observability-eval.json"
SENTINELS = (
    "SENTINELPAYLOAD",
    "SENTINELQUERY",
    "SENTINELEVIDENCE",
    "SENTINELREASON",
    "Notewise",  # candidate/generation spec content
    "sentinel-reviewer",
)
SessionFactory = Callable[[], Session]
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@dataclass
class Run:
    """What one scenario produced: the exported spans/metrics plus scenario checks."""

    spans: list[Any] = field(default_factory=list)
    metrics: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    session_ids: set[str] = field(default_factory=set)


# ---- helpers ------------------------------------------------------------------------------------


def _event(
    sid: uuid.UUID, event_type: str, payload: dict[str, Any], at: datetime
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "session_id": str(sid),
        "occurred_at": at.isoformat(),
        "payload": payload,
    }


def _api(factory: SessionFactory) -> tuple[TestClient, PostgresQueue]:
    """The real API app, created BEFORE a capture starts (app creation configures
    observability from Settings, which is off here), on the rolled-back connection."""
    from darwin.main import create_app

    app = create_app(Settings(env="test", log_level="WARNING"))
    queue = PostgresQueue(factory, visibility_timeout=timedelta(seconds=30))
    app.dependency_overrides[get_queue] = lambda: queue
    app.dependency_overrides[get_session_factory] = lambda: factory
    return TestClient(app), queue


def _drain(queue: PostgresQueue, factory: SessionFactory) -> list[Outcome]:
    processor = telemetry_processor(factory)
    outcomes = []
    while (outcome := run_once(queue, processor, max_attempts=5)) is not None:
        outcomes.append(outcome)
    return outcomes


def _by(spans: Sequence[Any], name: str) -> list[Any]:
    return [s for s in spans if s.name == name]


def _one(spans: Sequence[Any], name: str) -> Any:
    found = _by(spans, name)
    return found[0] if found else None


def _hex(value: int, width: int) -> str:
    return format(value, f"0{width}x")


@contextmanager
def _captured(run: Run, **kwargs: Any) -> Iterator[Captured]:
    with capture(**kwargs) as seen:
        yield seen
        run.spans.extend(seen.spans())
        run.metrics.extend(seen.metric_points())


# ---- scenarios -----------------------------------------------------------------------------------


def s_telemetry_api(f: SessionFactory) -> Run:
    run = Run()
    client, queue = _api(f)
    sid = uuid.uuid4()
    run.session_ids.add(str(sid))
    with _captured(run) as seen:
        body = _event(
            sid, "button_click", {"component": "plan_team_pro_cta", "note": "SENTINELPAYLOAD"}, T0
        )
        accepted = client.post("/api/v1/telemetry/events", json=body).status_code
        invalid = client.post("/api/v1/telemetry/events", json={"event_type": "x"}).status_code
        client.get("/api/v1/health/live")
        spans: list[Any] = seen.spans()
        server = [s for s in spans if s.name.startswith("POST ")]
        ingest, enqueue = _one(spans, "telemetry.ingest"), _one(spans, "queue.enqueue")
        run.checks = {
            "accepted_202": accepted == 202,
            "validation_422_traced": invalid == 422
            and any(s.attributes.get("http.response.status_code") == 422 for s in server),
            "server_span_uses_route_template": bool(server)
            and all(s.attributes.get("http.route") == "/api/v1/telemetry/events" for s in server),
            "ingest_child_of_server": ingest is not None
            and ingest.parent is not None
            and ingest.parent.span_id in {s.context.span_id for s in server},
            "enqueue_child_of_ingest": enqueue is not None
            and ingest is not None
            and enqueue.parent.span_id == ingest.context.span_id,
            "enqueue_result_recorded": enqueue is not None
            and enqueue.attributes.get("darwin.queue.result") == "accepted",
            "health_not_traced": not any("health" in s.name for s in spans),
            "no_url_or_query": all(
                "http.url" not in (s.attributes or {}) and "url.full" not in (s.attributes or {})
                for s in spans
            ),
        }
    _drain(queue, f)
    return run


def s_queue_propagation(f: SessionFactory) -> Run:
    run = Run()
    client, queue = _api(f)
    sid = uuid.uuid4()
    run.session_ids.add(str(sid))
    with _captured(run) as seen:
        body = _event(sid, "button_click", {"component": "plan_team_pro_cta"}, T0)
        client.post("/api/v1/telemetry/events", json=body)
        with f() as session:
            stored = session.scalar(
                select(QueueMessage.traceparent).where(
                    QueueMessage.message_id == uuid.UUID(body["event_id"])
                )
            )
            payload = session.scalar(
                select(QueueMessage.body).where(
                    QueueMessage.message_id == uuid.UUID(body["event_id"])
                )
            )
        _drain(queue, f)
        spans: list[Any] = seen.spans()
        enqueue, worker = _one(spans, "queue.enqueue"), _one(spans, "worker.process")
        persist, reconcile = _one(spans, "telemetry.persist"), _one(spans, "signals.reconcile")
        run.checks = {
            "traceparent_stored_as_metadata": isinstance(stored, str) and stored.startswith("00-"),
            "not_in_payload": "traceparent" not in json.dumps(payload),
            "worker_same_trace": worker is not None
            and enqueue is not None
            and worker.context.trace_id == enqueue.context.trace_id,
            "worker_parent_is_enqueue": worker is not None
            and worker.parent is not None
            and worker.parent.span_id == enqueue.context.span_id,
            "worker_marked_continued": worker is not None
            and worker.attributes.get("darwin.trace.context") == "continued",
            "persist_and_reconcile_children": persist is not None
            and reconcile is not None
            and persist.parent.span_id == worker.context.span_id
            and reconcile.parent.span_id == worker.context.span_id,
        }
    return run


def s_invalid_traceparent(f: SessionFactory) -> Run:
    run = Run()
    queue = PostgresQueue(f, visibility_timeout=timedelta(seconds=30))
    sid = uuid.uuid4()
    run.session_ids.add(str(sid))
    event = _event(sid, "page_view", {"page": "pricing_signup"}, T0)
    with f() as session:  # passes the DB's format CHECK, fails the stricter propagation rules
        session.execute(
            insert(QueueMessage).values(
                message_id=uuid.UUID(event["event_id"]),
                message_type=TELEMETRY_EVENT,
                body={**event, "schema_version": 1},
                traceparent="00-" + "0" * 32 + "-" + "0" * 16 + "-01",
            )
        )
        session.commit()
    with _captured(run) as seen:
        outcomes = _drain(queue, f)
        worker = _one(seen.spans(), "worker.process")
        run.checks = {
            "processed": outcomes == [Outcome.ACKED],
            "fresh_trace": worker is not None and worker.parent is None,
            "marked_invalid": worker is not None
            and worker.attributes.get("darwin.trace.context") == "invalid",
        }
    return run


def s_retry_and_dead(f: SessionFactory) -> Run:
    run = Run()
    queue = PostgresQueue(f, visibility_timeout=timedelta(seconds=30))
    from darwin.queue.base import OutgoingMessage, PermanentMessageError, ReceivedMessage

    calls = {"n": 0}

    def flaky(_type: str, _body: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("postgresql://user:SECRET@db/x down")  # message must not leak

    def permanent(_type: str, _body: dict[str, Any]) -> None:
        raise PermanentMessageError("invalid telemetry message: payload:missing")

    with _captured(run) as seen:
        with span("telemetry.ingest"):
            queue.enqueue(OutgoingMessage(uuid.uuid4(), TELEMETRY_EVENT, {"x": 1}, *_ctx()))
        received = queue.receive()
        assert received is not None
        first = handle(queue, received, flaky, max_attempts=5)
        again = ReceivedMessage(
            received.message_id,
            received.message_type,
            received.body,
            2,
            received.receipt_handle,
            received.traceparent,
            received.tracestate,
        )
        second = handle(queue, again, flaky, max_attempts=5)  # simulated redelivery
        with span("telemetry.ingest"):
            queue.enqueue(OutgoingMessage(uuid.uuid4(), TELEMETRY_EVENT, {"x": 2}, *_ctx()))
        dead_msg = queue.receive()
        assert dead_msg is not None
        dead = handle(queue, dead_msg, permanent, max_attempts=5)
        workers = _by(seen.spans(), "worker.process")
        attempts = [
            (w.attributes.get("darwin.queue.attempt"), w.attributes.get("darwin.queue.outcome"))
            for w in workers
        ]
        retry_span = workers[0] if workers else None
        run.checks = {
            "outcomes": (first, dead) == (Outcome.RETRY, Outcome.DEAD),
            "retry_observable": (1, "retry") in attempts,
            "redelivery_same_trace_higher_attempt": len(workers) >= 2
            and workers[0].context.trace_id == workers[1].context.trace_id
            and workers[1].attributes.get("darwin.queue.attempt") == 2,
            "dead_observable": (1, "dead") in attempts,
            "error_is_category_only": retry_span is not None
            and retry_span.status.description == "message_retry"
            and "SECRET" not in json.dumps(dict(retry_span.attributes)),
            "no_exception_events": all(not s.events for s in seen.spans()),
        }
        _ = second
    return run


def _ctx() -> tuple[str | None, str | None]:
    from .propagation import inject_current

    return inject_current()


def _research_setup(f: SessionFactory, key: str = "observability:research") -> tuple[Any, Any]:
    from darwin.hypotheses.evaluation import corpus_documents, synthetic_signal

    embedder = HashingEmbeddingProvider()
    signal = synthetic_signal(key, "rage_click", "plan_team_pro_cta")
    with f() as session:
        session.execute(delete(KnowledgeDocument))
        session.add(signal)
        session.commit()
        ingest_corpus(session, embedder, corpus_documents("product"), prune=False)
        signal_id = signal.signal_id
    return _SignalRef(signal_id), embedder


@dataclass(frozen=True)
class _SignalRef:
    signal_id: uuid.UUID


def s_research_and_llm(f: SessionFactory) -> Run:
    from darwin.llm.fake import FakeLLMProvider
    from darwin.research.service import run_research

    run = Run()
    signal, embedder = _research_setup(f)
    with _captured(run) as seen:
        with f() as session:
            chunks = retrieve(
                session, embedder, "SENTINELQUERY rage clicks on the team pro plan", top_k=3
            )
        outcome = run_research(f, signal.signal_id, FakeLLMProvider(), embedder)
        spans: list[Any] = seen.spans()
        root = _one(spans, "research.run")
        nodes = _by(spans, "research.node")
        llm = _by(spans, "llm.generate")
        retrievals = _by(spans, "memory.retrieve")
        run.checks = {
            "research_run_span": root is not None
            and root.attributes.get("darwin.status") == outcome.status
            and root.attributes.get("darwin.research.max_llm_calls") is not None,
            "node_spans_under_run": bool(nodes)
            and all(n.context.trace_id == root.context.trace_id for n in nodes),
            "node_names": {n.attributes.get("darwin.research.node") for n in nodes}
            >= {"load_signal", "retrieve", "assess_evidence", "generate_hypothesis", "finalize"},
            "sequence_counters": sorted(n.attributes.get("darwin.research.sequence") for n in nodes)
            == list(range(1, len(nodes) + 1)),
            "retrieval_counts_no_text": bool(retrievals)
            and all("darwin.memory.result_count" in r.attributes for r in retrievals)
            and len(retrievals) >= 1 + (1 if chunks is not None else 0),
            "llm_tokens_present": bool(llm)
            and all("gen_ai.usage.input_tokens" in s.attributes for s in llm)
            and all(s.attributes.get("gen_ai.system") == "fake" for s in llm),
        }
    return run


def _world(f: SessionFactory, tag: str) -> Any:
    from darwin.experiments.evaluation import build_world, harness_facts
    from darwin.generations.service import bootstrap_active

    world = build_world(f, tag, _FACTS.get() or _FACTS.set(harness_facts()))
    bootstrap_active(f, world.page_id)
    return world


class _Facts:
    value: Any = None

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> Any:
        self.value = value
        return value


_FACTS = _Facts()


def s_decision_mutation_sandbox(f: SessionFactory) -> Run:
    from darwin.decisions.evaluation import Artifact, write_artifact
    from darwin.decisions.rules import RulesDecider
    from darwin.decisions.service import decide_research_run
    from darwin.mutations.fixture import FixtureMutationGenerator
    from darwin.mutations.service import generate_candidate
    from darwin.mutations.specs import import_generation_zero
    from darwin.sandbox.evaluation import CachedRunner
    from darwin.sandbox.service import evaluate_candidate

    run = Run()
    world = _world(f, "obs_chain")  # harness facts for the cached runner
    with f() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(
            session, "observability:chain", Artifact(statement="SENTINELEVIDENCE delayed feedback.")
        )
    with _captured(run) as seen:
        decision = decide_research_run(f, research_run_id, RulesDecider())
        mutation = generate_candidate(f, decision.decision_run_id, FixtureMutationGenerator())
        assert mutation.candidate_spec_id is not None
        evaluation = evaluate_candidate(f, mutation.candidate_spec_id, CachedRunner(_FACTS.get()))
        spans: list[Any] = seen.spans()
        d, m, sb = (
            _one(spans, "decision.run"),
            _one(spans, "mutation.generate"),
            _one(spans, "sandbox.evaluate"),
        )
        run.checks = {
            "decision_span": d is not None
            and d.attributes.get("darwin.decider.type") == "rules"
            and d.attributes.get("darwin.decision") == decision.decision,
            "mutation_span_counts_not_values": m is not None
            and m.attributes.get("darwin.mutation.operation_count") == len(mutation.changes)
            and not any(v in ("immediate", "delayed") for v in m.attributes.values()),
            "sandbox_recommendation": sb is not None
            and sb.attributes.get("darwin.sandbox.recommendation") == evaluation.recommendation
            and sb.attributes.get("darwin.sandbox.category.ux_intent") is not None,
            "harness_child_span": _one(spans, "sandbox.frontend_harness") is not None,
        }
    _ = world
    return run


def s_experiment_and_promotion(f: SessionFactory) -> Run:
    from darwin.experiments.evaluation import sessions_for
    from darwin.experiments.serving import resolve_variant
    from darwin.generations.evaluation import _experiment
    from darwin.generations.service import decide, promote, rollback

    run = Run()
    world = _world(f, "obs_promo")
    with _captured(run) as seen:
        experiment, analysis_id = _experiment(f, world, "obs_promo_exp")
        assert analysis_id is not None
        sid = sessions_for(experiment.experiment_key, 5000, "candidate", 1, "obs")[0]
        run.session_ids.add(str(sid))
        with f() as session:
            resolve_variant(session, sid, world.page_id)  # completed -> none (no session in span)
        approval = decide(f, analysis_id, "approve", "sentinel-reviewer", "SENTINELREASON clean")
        promoted = promote(f, approval.approval_id, "sentinel-reviewer", f"{world.page_id}:1")  # type: ignore[arg-type]
        back = rollback(
            f, world.page_id, "sentinel-reviewer", "SENTINELREASON drill", f"{world.page_id}:0"
        )
        spans: list[Any] = seen.spans()
        p, r = _one(spans, "generation.promote"), _one(spans, "generation.rollback")
        run.checks = {
            "analysis_span": _one(spans, "experiment.analyze") is not None,
            "assign_span_no_session": _one(spans, "experiment.assign") is not None,
            "transitions_traced": len(_by(spans, "experiment.transition")) >= 2,
            "promote_generations": promoted.changed
            and p is not None
            and (
                p.attributes.get("darwin.generation.from"),
                p.attributes.get("darwin.generation.to"),
            )
            == (0, 1),
            "rollback_generations": back.changed
            and r is not None
            and (
                r.attributes.get("darwin.generation.from"),
                r.attributes.get("darwin.generation.to"),
            )
            == (1, 0),
            "decide_span": _one(spans, "promotion.decide") is not None,
        }
    return run


def s_log_correlation(f: SessionFactory) -> Run:
    run = Run()
    formatter = JsonFormatter()
    logger = logging.getLogger("darwin.observability.eval")
    with _captured(run):
        with span("worker.process"):
            inside = json.loads(
                formatter.format(
                    logger.makeRecord(logger.name, logging.INFO, "x", 0, "inside", (), None)
                )
            )
        outside = json.loads(
            formatter.format(
                logger.makeRecord(logger.name, logging.INFO, "x", 0, "outside", (), None)
            )
        )
    with capture() as seen:
        with span("worker.process"):
            record = json.loads(
                formatter.format(
                    logger.makeRecord(logger.name, logging.INFO, "x", 0, "m", (), None)
                )
            )
        exported = seen.spans()[0]
        run_spans = (_hex(exported.context.trace_id, 32), _hex(exported.context.span_id, 16))
    run.checks = {
        "ids_inside_span": "trace_id" in inside and "span_id" in inside,
        "absent_outside_span": "trace_id" not in outside,
        "ids_match_exported_span": (record.get("trace_id"), record.get("span_id")) == run_spans,
    }
    return run


def s_disabled_mode(f: SessionFactory) -> Run:
    run = Run()
    shutdown()
    client, queue = _api(f)
    body = _event(uuid.uuid4(), "page_view", {"page": "pricing_signup"}, T0)
    status = client.post("/api/v1/telemetry/events", json=body).status_code
    outcomes = _drain(queue, f)
    from .tracing import current_ids, is_enabled

    with span("worker.process"):
        ids = current_ids()
    with f() as session:
        stored = session.scalar(
            select(QueueMessage.traceparent).where(
                QueueMessage.message_id == uuid.UUID(body["event_id"])
            )
        )
    run.checks = {
        "disabled": not is_enabled(),
        "operation_ok": status == 202 and outcomes == [Outcome.ACKED],
        "no_span_context": ids == (None, None),
        "no_traceparent_stored": stored is None,
    }
    return run


def s_sampling_zero(f: SessionFactory) -> Run:
    run = Run()
    client, queue = _api(f)
    with capture(sample_ratio=0.0) as seen:
        body = _event(uuid.uuid4(), "page_view", {"page": "pricing_signup"}, T0)
        status = client.post("/api/v1/telemetry/events", json=body).status_code
        outcomes = _drain(queue, f)
        run.checks = {
            "operation_ok": status == 202 and outcomes == [Outcome.ACKED],
            "no_spans_exported": seen.spans() == [],
            "metrics_still_recorded": any(
                name == "darwin.worker.messages" for name, _, _ in seen.metric_points()
            ),
        }
    return run


def _operations(f: SessionFactory, tag: str) -> dict[str, Any]:
    """A representative set of operations whose RESULTS must not depend on tracing."""
    client, queue = _api(f)
    body = _event(uuid.uuid5(uuid.NAMESPACE_URL, tag), "page_view", {"page": "pricing_signup"}, T0)
    status = client.post("/api/v1/telemetry/events", json=body).status_code
    outcomes = [o.value for o in _drain(queue, f)]
    signal, embedder = _research_setup(f, f"observability:{tag}")
    from darwin.llm.fake import FakeLLMProvider
    from darwin.research.service import run_research

    research = run_research(f, signal.signal_id, FakeLLMProvider(), embedder)
    return {"http": status, "worker": outcomes, "research": (research.status, research.llm_calls)}


def s_exporter_failure(f: SessionFactory) -> Run:
    run = Run()
    baseline = _operations(f, "obs_baseline")
    FailingSpanExporter.calls = 0
    with capture(exporter=FailingSpanExporter()):
        failing = _operations(f, "obs_failing")
    run.checks = {
        "exporter_really_failed": FailingSpanExporter.calls > 0,
        "same_results": baseline == failing,
    }
    return run


def s_otlp_unreachable(f: SessionFactory) -> Run:
    run = Run()
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=True,
        otel_exporter="otlp",
        otel_endpoint="http://127.0.0.1:9",  # nothing listens here
        otel_metric_interval_seconds=3600,
    )
    enabled = setup_observability(settings, "darwin-eval")
    ok = _operations(f, "obs_otlp")
    from .tracing import _state

    flushed = True
    try:
        if _state.tracer_provider is not None:
            flushed = _state.tracer_provider.force_flush(timeout_millis=3000)
    except Exception:  # noqa: BLE001
        flushed = False
    shutdown()
    run.checks = {
        "setup_did_not_fail": enabled,
        "operations_ok": ok["http"] == 202 and ok["worker"] == ["acked"],
        "flush_returned_without_raising": flushed in (True, False),
    }
    return run


SCENARIOS: dict[str, Callable[[SessionFactory], Run]] = {
    "telemetry_api_spans": s_telemetry_api,
    "queue_context_propagates": s_queue_propagation,
    "invalid_traceparent_ignored": s_invalid_traceparent,
    "retry_and_dead_observable": s_retry_and_dead,
    "research_llm_retrieval_spans": s_research_and_llm,
    "decision_mutation_sandbox_spans": s_decision_mutation_sandbox,
    "experiment_and_promotion_spans": s_experiment_and_promotion,
    "log_correlation": s_log_correlation,
    "disabled_mode_no_spans": s_disabled_mode,
    "sampling_zero_no_spans": s_sampling_zero,
    "exporter_failure_contained": s_exporter_failure,
    "otlp_unreachable_contained": s_otlp_unreachable,
}


# ---- safety scans --------------------------------------------------------------------------------


def forbidden_attributes(spans: Sequence[Any], session_ids: set[str]) -> list[str]:
    problems = []
    for item in spans:
        if item.events:
            problems.append(f"{item.name}:event")
        for key, value in (item.attributes or {}).items():
            text = str(value)
            if key not in SPAN_ATTRIBUTES:
                problems.append(f"{item.name}:{key}")
            elif any(s in text for s in SENTINELS) or text in session_ids:
                problems.append(f"{item.name}:{key}=<sensitive>")
        if any(s in item.name for s in SENTINELS):
            problems.append(f"{item.name}:name")
    return problems


def forbidden_labels(
    points: Sequence[tuple[str, dict[str, Any], Any]], session_ids: set[str]
) -> list[str]:
    import re

    idlike = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-|[0-9a-fA-F]{16,}")
    problems = []
    for name, labels, _ in points:
        allowed = METRIC_LABELS.get(name, frozenset())
        for key, value in labels.items():
            text = str(value)
            if (
                key not in allowed
                or idlike.search(text)
                or any(s in text for s in SENTINELS)
                or text in session_ids
            ):
                problems.append(f"{name}:{key}")
    return problems


# ---- dataset, running ---------------------------------------------------------------------


class ObservabilityCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9_]{3,48}$")
    scenario: str
    description: str = Field(min_length=10, max_length=300)
    expected_checks: tuple[str, ...] = Field(min_length=1)


class ObservabilityDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=1)
    cases: tuple[ObservabilityCase, ...] = Field(min_length=1)


def load_dataset(path: Path = GOLDEN_PATH) -> ObservabilityDataset:
    return ObservabilityDataset.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass
class CaseResult:
    id: str
    scenario: str
    checks: dict[str, bool]
    correct: bool
    forbidden_attributes: list[str]
    forbidden_labels: list[str]
    failed_checks: list[str]


def _savepointed(connection: Connection, work: Callable[[], Any]) -> Any:
    savepoint = connection.begin_nested()
    try:
        return work()
    finally:
        savepoint.rollback()


def run_evaluation(engine: Engine, dataset: ObservabilityDataset) -> list[CaseResult]:
    shutdown()
    results = []
    runs: dict[str, Run] = {}
    with engine.connect() as connection:
        transaction = connection.begin()
        try:

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

            for scenario in dict.fromkeys(c.scenario for c in dataset.cases):

                def work(name: str = scenario) -> Run:
                    return SCENARIOS[name](factory)

                runs[scenario] = _savepointed(connection, work)
        finally:
            transaction.rollback()
            shutdown()
    for case in dataset.cases:
        run = runs[case.scenario]
        failed = [c for c in case.expected_checks if run.checks.get(c) is not True]
        attrs = forbidden_attributes(run.spans, run.session_ids)
        labels = forbidden_labels(run.metrics, run.session_ids)
        results.append(
            CaseResult(
                case.id,
                case.scenario,
                run.checks,
                not failed and not attrs and not labels,
                attrs,
                labels,
                failed,
            )
        )
    return results


def metrics(results: Sequence[CaseResult]) -> dict[str, Any]:
    correct = sum(r.correct for r in results)
    failure = next((r for r in results if r.scenario == "exporter_failure_contained"), None)
    return {
        "cases_meeting_all_expectations": {"passed": correct, "of": len(results)},
        "forbidden_attribute_count": sum(len(set(r.forbidden_attributes)) for r in results),
        "forbidden_metric_label_count": sum(len(set(r.forbidden_labels)) for r in results),
        "observability_caused_operation_failure_count": 0
        if failure is None or failure.checks.get("same_results")
        else 1,
        "attributes_dropped_by_policy": Rejected.attributes,
        "labels_dropped_by_policy": Rejected.labels,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observability contract evaluation.")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)  # expected export failures
    dataset = load_dataset()
    engine = create_db_engine(str(Settings().database_url))
    try:
        results = run_evaluation(engine, dataset)
    finally:
        engine.dispose()
    m = metrics(results)
    data = {
        "schema": "darwinux.observability-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "metrics": m,
        "cases": [asdict(r) for r in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"golden: {len(dataset.cases)} cases")
    for r in results:
        mark = "ok  " if r.correct else "FAIL"
        extra = ""
        if not r.correct:
            extra = (
                f"  failed: {r.failed_checks} attrs={r.forbidden_attributes[:3]} "
                f"labels={r.forbidden_labels[:3]}"
            )
        print(f"  {mark} {r.id:<44}{extra}")
    cases = m["cases_meeting_all_expectations"]
    print(f"{'cases_meeting_all_expectations':<46} {cases['passed']}/{cases['of']}")
    for key in (
        "forbidden_attribute_count",
        "forbidden_metric_label_count",
        "observability_caused_operation_failure_count",
    ):
        print(f"{key.upper():<46} {m[key]}")
    print(
        f"{'attributes dropped by policy (not exported)':<46} {m['attributes_dropped_by_policy']}"
    )
    print(f"report: {args.output.relative_to(REPO_ROOT)}")
    ok = not any(
        m[k]
        for k in (
            "forbidden_attribute_count",
            "forbidden_metric_label_count",
            "observability_caused_operation_failure_count",
        )
    )
    return 0 if ok and m["cases_meeting_all_expectations"]["passed"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
