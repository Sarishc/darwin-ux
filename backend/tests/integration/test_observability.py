"""Observability against darwin_test: API -> durable queue -> worker in one trace, the queue's
trace-context columns, the golden contract, and migration 0012. Rolled back, except the
migration test, which cleans up after itself."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, insert, inspect, select, text
from sqlalchemy.orm import Session

from darwin.db.models import QueueMessage
from darwin.observability.evaluation import (
    forbidden_attributes,
    forbidden_labels,
    load_dataset,
    metrics,
    run_evaluation,
)
from darwin.observability.testing import FailingSpanExporter, capture

pytestmark = pytest.mark.integration


def _event(session_id: uuid.UUID) -> dict[str, object]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "button_click",
        "session_id": str(session_id),
        "occurred_at": "2026-09-01T12:00:00Z",
        "payload": {"component": "plan_team_pro_cta", "free_text": "SENTINELPAYLOAD"},
    }


def test_one_trace_from_http_request_to_signal_reconciliation(
    producer: TestClient, drain: Callable[[], int], connection: Connection
) -> None:
    session_id = uuid.uuid4()
    with capture() as seen:
        body = _event(session_id)
        assert producer.post("/api/v1/telemetry/events", json=body).status_code == 202
        stored = connection.execute(
            select(QueueMessage.traceparent, QueueMessage.body).where(
                QueueMessage.message_id == uuid.UUID(str(body["event_id"]))
            )
        ).one()
        assert drain() == 1
        spans = {s.name: s for s in seen.spans()}
        points = seen.metric_points()
        all_spans = seen.spans()
    http = spans["POST /api/v1/telemetry/events"]
    trace_ids = {s.context.trace_id for s in all_spans}
    assert trace_ids == {http.context.trace_id}  # API, queue, worker, persistence: one trace
    chain = [
        ("telemetry.ingest", "POST /api/v1/telemetry/events"),
        ("queue.enqueue", "telemetry.ingest"),
        ("worker.process", "queue.enqueue"),
        ("telemetry.persist", "worker.process"),
        ("signals.reconcile", "worker.process"),
    ]
    for child, parent in chain:
        link = spans[child].parent
        assert link is not None and link.span_id == spans[parent].context.span_id, child
    traceparent, message_body = stored
    assert traceparent is not None and traceparent.split("-")[1] == format(
        http.context.trace_id, "032x"
    )
    assert "traceparent" not in str(message_body)  # metadata, never the payload
    assert forbidden_attributes(all_spans, {str(session_id)}) == []
    assert forbidden_labels(points, {str(session_id)}) == []


def test_exporter_failure_does_not_change_ingest_or_processing(
    producer: TestClient, drain: Callable[[], int], connection: Connection
) -> None:
    with capture(exporter=FailingSpanExporter()):
        body = _event(uuid.uuid4())
        assert producer.post("/api/v1/telemetry/events", json=body).status_code == 202
        assert drain() == 1
    status = connection.scalar(
        select(QueueMessage.status).where(
            QueueMessage.message_id == uuid.UUID(str(body["event_id"]))
        )
    )
    assert status == "done"


def test_database_refuses_malformed_trace_context(connection: Connection) -> None:
    base = {"message_type": "telemetry.event", "body": {}}
    for values, match in (
        ({"traceparent": "garbage"}, "traceparent_is_w3c"),
        ({"traceparent": None, "tracestate": "vendor=1"}, "tracestate_needs_traceparent"),
    ):
        with pytest.raises(Exception, match=match), connection.begin_nested():
            connection.execute(
                insert(QueueMessage).values(message_id=uuid.uuid4(), **base, **values)
            )


def test_observability_golden_contract(migrated_engine: Engine) -> None:
    results = run_evaluation(migrated_engine, load_dataset())
    assert all(r.correct for r in results), [
        (r.id, r.failed_checks, r.forbidden_attributes, r.forbidden_labels)
        for r in results
        if not r.correct
    ]
    m = metrics(results)
    assert (
        m["forbidden_attribute_count"],
        m["forbidden_metric_label_count"],
        m["observability_caused_operation_failure_count"],
    ) == (0, 0, 0)


def test_migration_0012_round_trip_keeps_queue_messages(
    migrated_engine: Engine, alembic_cfg: Config
) -> None:
    message_id = uuid.uuid4()
    with Session(migrated_engine) as session:
        session.execute(
            insert(QueueMessage).values(
                message_id=message_id,
                message_type="telemetry.event",
                body={"schema_version": 1},
                traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    try:
        command.downgrade(alembic_cfg, "0011")
        columns = {c["name"] for c in inspect(migrated_engine).get_columns("queue_message")}
        assert not {"traceparent", "tracestate"} & columns
        with migrated_engine.connect() as conn:
            kept = conn.scalar(
                text("SELECT count(*) FROM queue_message WHERE message_id = :id"),
                {"id": message_id},
            )
        assert kept == 1
        command.upgrade(alembic_cfg, "head")
        columns = {c["name"] for c in inspect(migrated_engine).get_columns("queue_message")}
        assert {"traceparent", "tracestate"} <= columns
    finally:
        command.upgrade(alembic_cfg, "head")
        with Session(migrated_engine) as session:
            session.execute(
                text("DELETE FROM queue_message WHERE message_id = :id"), {"id": message_id}
            )
            session.commit()
