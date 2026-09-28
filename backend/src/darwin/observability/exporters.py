"""Compact console exporters for local development: one short line per span / metric point.

The SDK's ConsoleSpanExporter prints a large indented JSON document per span; these
print only what a developer needs to read a trace, on stderr (stdout stays for the
program's own output):

  [otel] span   worker.process  trace=4bf9…  span=00f0…  parent=a3ce…  12.4ms  OK  {...}
  [otel] metric darwin.worker.messages  {"message_type": "telemetry.event", ...}  3
"""

import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.metrics.view import Aggregation
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class CompactConsoleSpanExporter(SpanExporter):
    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        out = self._out or sys.stderr
        for item in spans:
            context = item.get_span_context()
            if context is None:
                continue
            parent = format(item.parent.span_id, "016x") if item.parent else "-"
            duration = ((item.end_time or 0) - (item.start_time or 0)) / 1e6
            service = item.resource.attributes.get("service.name", "?")
            out.write(
                f"[otel] span {service} {item.name}  "
                f"trace={format(context.trace_id, '032x')}  "
                f"span={format(context.span_id, '016x')}  parent={parent}  "
                f"{duration:.1f}ms  {item.status.status_code.name}  "
                f"{json.dumps(dict(item.attributes or {}), sort_keys=True)}\n"
            )
        out.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


class CompactConsoleMetricExporter(MetricExporter):
    def __init__(self, out: TextIO | None = None) -> None:
        super().__init__(
            preferred_temporality=None,
            preferred_aggregation=dict[type, Aggregation](),
        )
        self._out = out

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        out = self._out or sys.stderr
        for resource_metrics in metrics_data.resource_metrics:
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        value = getattr(point, "value", None)
                        if value is None:
                            count, total = getattr(point, "count", 0), getattr(point, "sum", 0.0)
                            value = f"count={count} sum={total:.1f}"
                        labels = json.dumps(dict(point.attributes or {}), sort_keys=True)
                        out.write(f"[otel] metric {metric.name}  {labels}  {value}\n")
        out.flush()
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        return None
