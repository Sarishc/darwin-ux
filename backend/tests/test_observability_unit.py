"""Step 16 unit tests: attribute/label policy, safe spans, propagation, sampling, log
correlation, exporter failure containment, configuration, HTTP and LLM instrumentation.
No database, no network (the OTLP endpoint is an unused local port)."""

import io
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from darwin.api.telemetry import get_queue
from darwin.config import Settings
from darwin.llm.port import ProviderFailureError, StructuredGenerationRequest
from darwin.llm.traced import generate_structured
from darwin.logging_config import JsonFormatter
from darwin.main import create_app
from darwin.observability import (
    SPAN_NAMES,
    current_ids,
    is_enabled,
    record,
    setup_observability,
    shutdown,
    span,
    stage,
)
from darwin.observability.attributes import (
    METRIC_LABELS,
    SPAN_ATTRIBUTES,
    clean_attributes,
    clean_labels,
)
from darwin.observability.exporters import CompactConsoleSpanExporter
from darwin.observability.propagation import extract, inject_current, valid_traceparent
from darwin.observability.testing import FailingSpanExporter, capture
from darwin.queue.base import ReceivedMessage
from darwin.worker import Outcome, handle

SRC = Path(__file__).resolve().parents[1] / "src" / "darwin"
SESSION = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _off() -> Any:
    shutdown()
    yield
    shutdown()


# ---- attribute / label policy -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attributes", "kept"),
    [
        ({"darwin.status": "succeeded"}, {"darwin.status": "succeeded"}),
        ({"darwin.queue.attempt": 2}, {"darwin.queue.attempt": 2}),
        ({"darwin.decision.fail_closed": True}, {"darwin.decision.fail_closed": True}),
        ({"darwin.research_run.id": SESSION}, {"darwin.research_run.id": SESSION}),
        ({"prompt": "You are a helpful assistant"}, {}),
        ({"darwin.status": "free text with spaces"}, {}),  # prose never passes
        ({"darwin.status": f"id-{SESSION}"}, {}),  # ids only on *.id keys
        ({"darwin.status": "a" * 64}, {}),  # hashes / hex tokens refused
        ({"darwin.status": "x" * 129}, {}),
        ({"darwin.queue.attempt": "2"}, {}),  # wrong type
        ({"darwin.research_run.id": "not-a-uuid"}, {}),
        ({"http.url": "http://x/y?session=1"}, {}),
    ],
)
def test_attribute_policy(attributes: dict[str, Any], kept: dict[str, Any]) -> None:
    assert clean_attributes(attributes) == kept


def test_no_sensitive_attribute_keys_exist() -> None:
    # `*.reason` keys carry bounded reason CODES (identifier-shaped values only).
    forbidden = re.compile(r"prompt|\.text$|query|payload|\.spec$|session|email|reviewer|secret")
    assert not [key for key in SPAN_ATTRIBUTES if forbidden.search(key)]


@pytest.mark.parametrize(
    ("metric", "labels", "ok"),
    [
        ("darwin.stage.runs", {"stage": "mutation", "outcome": "succeeded"}, True),
        ("darwin.stage.runs", {"stage": "not_a_stage", "outcome": "ok"}, False),
        ("darwin.stage.runs", {"stage": "mutation", "session_id": SESSION}, False),
        ("darwin.worker.messages", {"message_type": "telemetry.event", "outcome": SESSION}, False),
        ("darwin.llm.calls", {"provider": "fake", "status": "has spaces"}, False),
        ("darwin.llm.calls", {"provider": "fake", "status": "a" * 32}, False),
        ("darwin.unknown", {"status": "ok"}, False),
    ],
)
def test_metric_label_policy(metric: str, labels: dict[str, Any], ok: bool) -> None:
    assert (clean_labels(metric, labels) is not None) is ok


def test_metric_labels_contain_no_high_cardinality_keys() -> None:
    forbidden = {
        "session_id",
        "event_id",
        "research_run_id",
        "hypothesis_id",
        "candidate_spec_id",
        "experiment_id",
        "experiment_key",
        "trace_id",
        "reviewer",
        "source_key",
        "chunk_id",
    }
    for labels in METRIC_LABELS.values():
        assert not labels & forbidden
        assert not any(label.endswith("_id") or label.endswith(".id") for label in labels)


def test_every_span_name_used_in_code_is_in_the_vocabulary() -> None:
    used = set()
    for path in SRC.rglob("*.py"):
        if "observability" in path.parts:
            continue
        used |= set(re.findall(r'(?:span|stage)\(\s*"([a-z_.]+)"', path.read_text()))
    assert used and used <= SPAN_NAMES, used - SPAN_NAMES


# ---- spans, errors, disabled, sampling ----------------------------------------------------------


def test_disabled_by_default_is_a_no_op() -> None:
    assert not setup_observability(Settings(env="test"), "darwin-test")
    assert not is_enabled()
    with span("worker.process") as s:
        s.set(darwin__status="ok")
        assert current_ids() == (None, None)
    record("darwin.stage.runs", 1, stage="mutation", outcome="ok")  # no error


def test_errors_record_type_only_and_propagate() -> None:
    with capture() as seen:
        with pytest.raises(ValueError, match="password=hunter2"):
            with span("decision.run"):
                raise ValueError("password=hunter2 postgresql://u:p@h/db")
        [failed] = seen.spans()
        assert failed.status.status_code.name == "ERROR"
        assert failed.status.description == "ValueError"
        assert dict(failed.attributes or {}) == {"darwin.error.type": "ValueError"}
        assert failed.events == ()  # no exception event (it would carry the message)


def test_spans_nest_and_stages_record_bounded_metrics() -> None:
    with capture() as seen:
        with span("research.run") as outer:
            with stage("mutation.generate", "mutation") as inner:
                inner.outcome = "succeeded"
            outer.set(**{"darwin.status": "succeeded"})
        child, parent = seen.spans()
        assert child.parent is not None and child.parent.span_id == parent.context.span_id
        points = seen.metric_points()
    assert ("darwin.stage.runs", {"stage": "mutation", "outcome": "succeeded"}, 1) in points


def test_a_failing_stage_counts_as_error_outcome() -> None:
    with capture() as seen:
        with pytest.raises(RuntimeError):
            with stage("sandbox.evaluate", "sandbox"):
                raise RuntimeError("boom")
        assert ("darwin.stage.runs", {"stage": "sandbox", "outcome": "error"}, 1) in (
            seen.metric_points()
        )


def test_sampling_zero_exports_no_spans_but_keeps_metrics() -> None:
    with capture(sample_ratio=0.0) as seen:
        with stage("mutation.generate", "mutation") as s:
            s.outcome = "succeeded"
        assert seen.spans() == []
        assert any(name == "darwin.stage.runs" for name, _, _ in seen.metric_points())


def test_exporter_failure_never_reaches_the_caller() -> None:
    FailingSpanExporter.calls = 0
    with capture(exporter=FailingSpanExporter()):
        for _ in range(3):
            with span("worker.process") as s:
                s.set(**{"darwin.status": "ok"})
    assert FailingSpanExporter.calls == 3


# ---- propagation --------------------------------------------------------------------------------


def test_inject_and_extract_continue_the_same_trace() -> None:
    with capture() as seen:
        with span("queue.enqueue"):
            traceparent, _ = inject_current()
        context, how = extract(traceparent, None)
        with span("worker.process", context=context):
            pass
        producer, consumer = seen.by_name("queue.enqueue")[0], seen.by_name("worker.process")[0]
    assert how == "continued"
    assert consumer.context.trace_id == producer.context.trace_id
    assert consumer.parent is not None and consumer.parent.span_id == producer.context.span_id


def test_no_active_span_means_no_trace_context() -> None:
    assert inject_current() == (None, None)
    assert extract(None, None) == (None, "fresh")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        "01-" + "a" * 32 + "-" + "b" * 16 + "-01",  # unsupported version
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",  # all-zero trace id
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",  # all-zero parent id
        "00-" + "A" * 32 + "-" + "b" * 16 + "-01",  # upper case
        "00-" + "a" * 32 + "-" + "b" * 16 + "-01-extra",
        123,
    ],
)
def test_malformed_trace_context_is_ignored(value: object) -> None:
    assert valid_traceparent(value) is None
    assert extract(value, "vendor=1")[0] is None


def test_worker_processes_a_message_with_garbage_trace_context() -> None:
    class Queue:
        def ack(self, _m: Any) -> bool:
            return True

    received = ReceivedMessage(uuid.uuid4(), "telemetry.event", {}, 1, uuid.uuid4(), "junk", "x")
    with capture() as seen:
        outcome = handle(Queue(), received, lambda _t, _b: None, max_attempts=5)  # type: ignore[arg-type]
        [worker] = seen.by_name("worker.process")
    assert outcome == Outcome.ACKED
    assert (
        worker.parent is None and (worker.attributes or {}).get("darwin.trace.context") == "invalid"
    )


# ---- log correlation ----------------------------------------------------------------------------


def _log_line(formatter: JsonFormatter) -> dict[str, Any]:
    logger = logging.getLogger("darwin.test")
    record_ = logger.makeRecord(logger.name, logging.INFO, "x", 0, "hello", (), None)
    parsed: dict[str, Any] = json.loads(formatter.format(record_))
    return parsed


def test_log_lines_carry_trace_and_span_ids_only_inside_spans() -> None:
    formatter = JsonFormatter()
    assert "trace_id" not in _log_line(formatter)
    with capture() as seen:
        with span("worker.process"):
            line = _log_line(formatter)
        exported = seen.spans()[0]
    assert line["trace_id"] == format(exported.context.trace_id, "032x")
    assert line["span_id"] == format(exported.context.span_id, "016x")
    assert set(line) == {"timestamp", "level", "logger", "message", "trace_id", "span_id"}


# ---- configuration ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://collector:4318",
        "http://user:pass@collector:4318",
        "http://collector:4318?token=x",
        "http://collector:4318#x",
        "collector:4318",
    ],
)
def test_unsafe_otlp_endpoints_are_refused(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        Settings(otel_endpoint=endpoint)


@pytest.mark.parametrize("ratio", [-0.1, 1.5])
def test_sample_ratio_must_be_a_probability(ratio: float) -> None:
    with pytest.raises(ValidationError):
        Settings(otel_sample_ratio=ratio)


def test_console_exporter_prints_one_compact_line_per_span() -> None:
    out = io.StringIO()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from darwin.observability.tracing import install

    install("darwin-test", SimpleSpanProcessor(CompactConsoleSpanExporter(out)), None)
    with span("worker.process", {"darwin.queue.attempt": 1}):
        pass
    shutdown()
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("[otel] span darwin-test worker.process")
    assert '"darwin.queue.attempt": 1' in lines[0]


def test_unreachable_otlp_endpoint_never_fails_startup_or_operations() -> None:
    settings = Settings(otel_enabled=True, otel_exporter="otlp", otel_endpoint="http://127.0.0.1:9")
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
    assert setup_observability(settings, "darwin-test")
    with span("worker.process"):
        pass
    shutdown()  # flushes into a refused connection; must not raise


# ---- HTTP and LLM instrumentation ---------------------------------------------------------------


class _AcceptingQueue:
    def enqueue(self, _message: Any) -> bool:
        return True


def test_http_spans_use_route_templates_and_skip_health() -> None:
    app = create_app(Settings(env="test", log_level="WARNING"))
    app.dependency_overrides[get_queue] = lambda: _AcceptingQueue()
    client = TestClient(app)
    body = {
        "event_id": str(uuid.uuid4()),
        "event_type": "page_view",
        "session_id": SESSION,
        "occurred_at": "2026-09-01T12:00:00Z",
        "payload": {"page": "pricing_signup"},
    }
    with capture() as seen:
        assert client.post("/api/v1/telemetry/events", json=body).status_code == 202
        client.get("/api/v1/health/live")
        client.get(f"/api/v1/nothing/{SESSION}?q=secret")
        spans = seen.spans()
        names = [s.name for s in spans]
        points = seen.metric_points()
    assert "POST /api/v1/telemetry/events" in names
    assert "GET unmatched" in names  # the raw path (with an id) is never used
    assert not any("health" in n for n in names)
    assert not any(SESSION in json.dumps(dict(s.attributes or {})) for s in spans)
    requests = [labels for name, labels, _ in points if name == "darwin.http.server.requests"]
    assert {
        "http.request.method": "POST",
        "http.route": "/api/v1/telemetry/events",
        "status_class": "2xx",
    } in requests


def _request() -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        request_version="hypothesis.v1",
        instructions="SENTINEL instructions",
        evidence="SENTINEL evidence",
        output_schema={},
        max_output_tokens=10,
        timeout_seconds=1,
    )


class _Answering:
    name, model = "stub", "stub-model:v1"

    def generate_structured(self, _request: Any) -> Any:
        from darwin.llm.port import StructuredGenerationResult, Usage

        return StructuredGenerationResult(
            "stub", "stub-model:v1", '{"answer": "SENTINEL output"}', Usage(120, 30)
        )


def test_llm_span_has_tokens_but_never_prompt_or_output() -> None:
    with capture() as seen:
        result = generate_structured(_Answering(), _request())
        [llm] = seen.by_name("llm.generate")
        points = seen.metric_points()
    attributes = dict(llm.attributes or {})
    assert attributes["gen_ai.system"] == "stub"
    assert (attributes["gen_ai.usage.input_tokens"], attributes["gen_ai.usage.output_tokens"]) == (
        120,
        30,
    )
    assert attributes["darwin.llm.request_version"] == "hypothesis.v1"
    assert "gen_ai.usage.input_tokens" in attributes
    flat = json.dumps(attributes)
    assert "SENTINEL" not in flat and result.output_text[:20] not in flat
    assert any(name == "darwin.llm.calls" for name, _, _ in points)


def test_llm_failures_are_counted_and_re_raised() -> None:
    class Broken:
        name, model = "broken", "broken:v1"

        def generate_structured(self, _request: Any) -> Any:
            raise ProviderFailureError("provider said: <secret body>")

    with capture() as seen:
        with pytest.raises(ProviderFailureError):
            generate_structured(Broken(), _request())
        [llm] = seen.by_name("llm.generate")
        points = seen.metric_points()
    assert llm.status.description == "ProviderFailureError"
    assert (
        "darwin.llm.calls",
        {"provider": "broken", "request_version": "hypothesis.v1", "status": "error"},
        1,
    ) in points
