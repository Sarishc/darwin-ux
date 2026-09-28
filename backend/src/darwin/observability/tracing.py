"""Tracer/meter providers and the safe span helpers DarwinUX code uses.

Design rules:
- DarwinUX never calls `trace.set_tracer_provider`: this module owns its provider,
  so configuration is replaceable (tests) and no library can hijack it.
- Everything here is best effort. Any exception raised by OpenTelemetry — building
  a span, setting an attribute, exporting — is swallowed. Business code keeps its
  own behaviour, including the exceptions it raises.
- Exceptions are NEVER recorded with `record_exception` (its event carries the
  message, which can contain SQL, URLs or payload fragments). A failed span gets
  status ERROR with the exception TYPE as description and `darwin.error.type`.
- Disabled (the default) means no-op tracer and meter: zero spans, zero exports.
"""

import atexit
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.metrics import Meter, NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import NoOpTracerProvider, SpanKind, Status, StatusCode, Tracer

from .attributes import clean_attributes, clean_labels

logger = logging.getLogger("darwin.observability")
INSTRUMENTATION = "darwin"


@dataclass
class _State:
    tracer: Tracer = field(default_factory=lambda: NoOpTracerProvider().get_tracer(INSTRUMENTATION))
    meter: Meter = field(default_factory=lambda: NoOpMeterProvider().get_meter(INSTRUMENTATION))
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    enabled: bool = False
    service_name: str | None = None
    instruments: dict[str, Any] = field(default_factory=dict)


_state = _State()


def is_enabled() -> bool:
    return _state.enabled


def install(
    service_name: str,
    span_processor: SpanProcessor | None,
    metric_reader: MetricReader | None,
    sample_ratio: float = 1.0,
) -> None:
    """Replace the providers (shutting down the previous ones). Used by setup and tests."""
    shutdown()
    resource = Resource.create({"service.name": service_name, "service.namespace": "darwinux"})
    sampler = ParentBased(root=TraceIdRatioBased(sample_ratio))
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    if span_processor is not None:
        tracer_provider.add_span_processor(span_processor)
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[metric_reader] if metric_reader else []
    )
    _state.tracer_provider, _state.meter_provider = tracer_provider, meter_provider
    _state.tracer = tracer_provider.get_tracer(INSTRUMENTATION)
    _state.meter = meter_provider.get_meter(INSTRUMENTATION)
    _state.instruments = {}
    _state.enabled = True
    _state.service_name = service_name


def shutdown() -> None:
    """Flush and drop the providers; back to no-op. Never raises."""
    for provider in (_state.tracer_provider, _state.meter_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001 — observability must never break shutdown
            logger.warning("observability shutdown failed")
    _state.tracer = NoOpTracerProvider().get_tracer(INSTRUMENTATION)
    _state.meter = NoOpMeterProvider().get_meter(INSTRUMENTATION)
    _state.tracer_provider = _state.meter_provider = None
    _state.instruments = {}
    _state.enabled = False
    _state.service_name = None


def setup_observability(settings: Any, service_name: str) -> bool:
    """Configure from Settings for one process (darwin-api / darwin-worker / darwin-cli).

    Returns True if enabled. A broken exporter configuration is logged and leaves
    observability OFF — it never stops the process from starting.
    """
    if not settings.otel_enabled or settings.otel_exporter == "none":
        return False  # leave whatever is installed alone (e.g. a test capture)
    try:
        from .exporters import CompactConsoleMetricExporter, CompactConsoleSpanExporter

        span_exporter: SpanExporter
        if settings.otel_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            base = settings.otel_endpoint or "http://localhost:4318"
            span_exporter = OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=5)
            metric_exporter: Any = OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=5)
        else:
            span_exporter = CompactConsoleSpanExporter()
            metric_exporter = CompactConsoleMetricExporter()
        reader = PeriodicExportingMetricReader(
            metric_exporter,
            export_interval_millis=int(settings.otel_metric_interval_seconds * 1000),
        )
        install(
            service_name,
            BatchSpanProcessor(span_exporter),
            reader,
            settings.otel_sample_ratio,
        )
    except Exception as error:  # noqa: BLE001
        shutdown()
        logger.warning(
            "observability disabled: exporter setup failed",
            extra={"context": {"error": type(error).__name__}},
        )
        return False
    atexit.register(shutdown)
    return True


# ---- spans -------------------------------------------------------------------------------------


class SpanHandle:
    """What callers get inside `span(...)`: safe setters. No-op when tracing is off."""

    def __init__(self, span: trace.Span | None) -> None:
        self._span = span
        self.outcome: str | None = None  # stages: the bounded outcome for metrics

    def set(self, **attributes: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attributes(clean_attributes(_dotted(attributes)))
        except Exception:  # noqa: BLE001
            pass

    def rename(self, name: str) -> None:
        if self._span is None:
            return
        try:
            self._span.update_name(name)
        except Exception:  # noqa: BLE001
            pass

    def error(self, error: BaseException | str) -> None:
        """Mark failed with a bounded category — the exception TYPE, never its message."""
        category = error if isinstance(error, str) else type(error).__name__
        if self._span is None:
            return
        try:
            self._span.set_attributes(clean_attributes({"darwin.error.type": category[:64]}))
            self._span.set_status(Status(StatusCode.ERROR, category[:64]))
        except Exception:  # noqa: BLE001
            pass


def _dotted(attributes: Mapping[str, Any]) -> dict[str, Any]:
    # Python keyword arguments cannot contain dots: darwin__run__status -> darwin.run.status.
    return {key.replace("__", "."): value for key, value in attributes.items()}


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    context: Context | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator[SpanHandle]:
    """A current span that can never break the code inside it."""
    manager: Any = None
    handle = SpanHandle(None)
    try:
        manager = _state.tracer.start_as_current_span(
            name,
            context=context,
            kind=kind,
            attributes=clean_attributes(attributes),
            record_exception=False,
            set_status_on_exception=False,
        )
        handle = SpanHandle(manager.__enter__())
    except Exception:  # noqa: BLE001
        manager = None
    try:
        yield handle
    except BaseException as error:
        handle.error(error)
        _exit(manager, error)
        raise
    _exit(manager, None)


def _exit(manager: Any, error: BaseException | None) -> None:
    if manager is None:
        return
    try:
        if error is None:
            manager.__exit__(None, None, None)
        else:
            manager.__exit__(type(error), error, error.__traceback__)
    except Exception:  # noqa: BLE001
        pass


# ---- metrics ------------------------------------------------------------------------------------

_KINDS = {
    "darwin.http.server.requests": ("counter", "1", "HTTP requests (excluding health)"),
    "darwin.http.server.duration": ("histogram", "ms", "HTTP request duration"),
    "darwin.queue.enqueued": ("counter", "1", "Messages offered to the queue"),
    "darwin.worker.messages": ("counter", "1", "Messages handled by the worker"),
    "darwin.worker.duration": ("histogram", "ms", "Worker processing duration"),
    "darwin.llm.calls": ("counter", "1", "LLM provider calls"),
    "darwin.llm.duration": ("histogram", "ms", "LLM provider call duration"),
    "darwin.llm.tokens": ("counter", "1", "LLM tokens as reported by the provider"),
    "darwin.stage.runs": ("counter", "1", "Pipeline stage executions"),
    "darwin.stage.duration": ("histogram", "ms", "Pipeline stage duration"),
}


def record(metric: str, value: float, **labels: Any) -> None:
    """Add to a counter / histogram with allowlisted, bounded labels. Never raises."""
    if not _state.enabled:
        return
    try:
        clean = clean_labels(metric, _dotted(labels))
        if clean is None:
            return
        instrument = _state.instruments.get(metric)
        if instrument is None:
            kind, unit, description = _KINDS[metric]
            factory = (
                _state.meter.create_counter if kind == "counter" else _state.meter.create_histogram
            )
            instrument = factory(metric, unit=unit, description=description)
            _state.instruments[metric] = instrument
        if hasattr(instrument, "add"):
            instrument.add(value, clean)
        else:
            instrument.record(value, clean)
    except Exception:  # noqa: BLE001
        pass


def observe_gauge(metric: str, callback: Any) -> None:
    """Register an observable gauge (e.g. queue depth); the callback must be cheap."""
    if not _state.enabled:
        return
    try:
        from opentelemetry.metrics import CallbackOptions, Observation

        def safe(options: CallbackOptions) -> list[Observation]:
            try:
                out = []
                for value, labels in callback():
                    clean = clean_labels(metric, labels)
                    if clean is not None:
                        out.append(Observation(value, clean))
                return out
            except Exception:  # noqa: BLE001 — a failing gauge reports nothing
                return []

        _state.meter.create_observable_gauge(metric, callbacks=[safe], unit="1")
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def stage(
    name: str, stage_label: str, attributes: Mapping[str, Any] | None = None
) -> Iterator[SpanHandle]:
    """A span plus `darwin.stage.runs` / `darwin.stage.duration` for one pipeline stage.

    Set `handle.outcome` to a bounded outcome (e.g. the run status); an exception
    records outcome "error".
    """
    started = time.perf_counter()
    outcome = "error"
    with span(name, attributes) as handle:
        try:
            yield handle
            outcome = handle.outcome or "ok"
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            record("darwin.stage.runs", 1, stage=stage_label, outcome=outcome)
            record("darwin.stage.duration", elapsed, stage=stage_label, outcome=outcome)


def current_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) as hex of the active span, or (None, None). For log correlation."""
    try:
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None, None
        return format(context.trace_id, "032x"), format(context.span_id, "016x")
    except Exception:  # noqa: BLE001
        return None, None
