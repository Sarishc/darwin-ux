"""Operational observability (Step 16): OpenTelemetry traces and metrics, best effort.

AUDIT RECORDS != TELEMETRY. The database tables (user_event, research_run,
decision_run, mutation_run, candidate_evaluation_run, experiment_analysis,
promotion_approval, ...) are the durable product truth. Traces and metrics are
sampled, ephemeral diagnostics: how long things take, where they fail, how an
operation flows API -> queue -> worker. Nothing DarwinUX decides depends on them,
and if they fail, DarwinUX carries on.

DarwinUX code uses only these helpers — never exporter- or vendor-specific code:

    with span("worker.process", {...}) as s:   s.set(...); s.error(exc)
    with stage("mutation.generate", "mutation", {...}) as s:   s.outcome = status
    record("darwin.llm.calls", 1, provider=..., status=...)
    inject_current() / extract(...)              W3C trace context via the queue
    current_ids()                                trace/span ids for log lines

Attributes and labels pass an allowlist (attributes.py): no prompts, model output,
Product Memory text, specs, payloads, form values, session ids, reasons or secrets.
"""

from .attributes import SPAN_NAMES, bounded
from .propagation import extract, inject_current
from .tracing import (
    SpanHandle,
    current_ids,
    is_enabled,
    observe_gauge,
    record,
    setup_observability,
    shutdown,
    span,
    stage,
)

__all__ = [
    "SPAN_NAMES",
    "SpanHandle",
    "bounded",
    "current_ids",
    "extract",
    "inject_current",
    "is_enabled",
    "observe_gauge",
    "record",
    "setup_observability",
    "shutdown",
    "span",
    "stage",
]
