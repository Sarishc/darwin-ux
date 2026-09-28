"""In-memory capture of spans and metrics for tests and the observability evaluation.

    with capture() as seen:
        ...                       # run a DarwinUX operation
        seen.spans()              # finished spans (ReadableSpan), in end order
        seen.metric_points()      # [(metric name, labels dict, value)]

Read results INSIDE the block: leaving it shuts the providers down.

No network, no stdout parsing. Leaves observability disabled afterwards.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from .tracing import install, shutdown


class FailingSpanExporter(SpanExporter):
    """An exporter that always raises — exporter failure must never reach DarwinUX code."""

    calls = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        FailingSpanExporter.calls += 1
        raise ConnectionError("collector unreachable (simulated)")

    def shutdown(self) -> None:
        return None


@dataclass
class Captured:
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def names(self) -> list[str]:
        return [s.name for s in self.spans()]

    def by_name(self, name: str) -> list[ReadableSpan]:
        return [s for s in self.spans() if s.name == name]

    def metric_points(self) -> list[tuple[str, dict[str, Any], Any]]:
        data = self.reader.get_metrics_data()
        points: list[tuple[str, dict[str, Any], Any]] = []
        if data is None:
            return points
        for resource_metrics in data.resource_metrics:
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        value = getattr(point, "value", None)
                        if value is None:
                            value = getattr(point, "count", None)
                        points.append((metric.name, dict(point.attributes or {}), value))
        return points


@contextmanager
def capture(
    service_name: str = "darwin-test",
    sample_ratio: float = 1.0,
    exporter: SpanExporter | None = None,
) -> Iterator[Captured]:
    memory = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    install(service_name, SimpleSpanProcessor(exporter or memory), reader, sample_ratio)
    try:
        yield Captured(memory, reader)
    finally:
        shutdown()
