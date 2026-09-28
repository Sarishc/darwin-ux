"""The safe vocabulary: span names, span attributes, metric labels.

Span attributes and metric labels are different things:

  SPAN ATTRIBUTE  describes ONE operation. May carry an id (a research run, an
                  experiment) so an engineer can jump from a trace to the audit
                  record. Only keys listed here; ids only on keys ending in ".id".
  METRIC LABEL    becomes a time-series dimension; every distinct value is a new
                  series kept forever by the backend. Only bounded vocabularies
                  (status, provider, stage, variant ...) — never an id, never free text.

Values must look like identifiers (letters, digits and `_ . : / - { }`, at most
128 characters): prose, prompts, payloads, specs and reasons cannot pass. A value
that fails is dropped, never truncated into something that might leak.
"""

import re
from collections.abc import Mapping
from typing import Any

# Stable span names (docs/OBSERVABILITY.md). HTTP server spans are "{METHOD} {route}".
SPAN_NAMES = frozenset(
    {
        "http.server",  # renamed to "{METHOD} {route template}" once the route is known
        "telemetry.ingest",
        "queue.enqueue",
        "worker.process",
        "telemetry.persist",
        "signals.reconcile",
        "memory.ingest",
        "memory.retrieve",
        "llm.generate",
        "hypothesis.generate",
        "research.run",
        "research.node",
        "decision.run",
        "mutation.generate",
        "sandbox.evaluate",
        "sandbox.frontend_harness",
        "experiment.assign",
        "experiment.record_exposure",
        "experiment.analyze",
        "experiment.transition",
        "promotion.decide",
        "generation.promote",
        "generation.rollback",
    }
)

_STRING = "str"
_INT = "int"
_BOOL = "bool"
_ID = "id"  # a UUID-shaped identifier, span attributes only

SPAN_ATTRIBUTES: dict[str, str] = {
    # generic
    "darwin.status": _STRING,
    "darwin.outcome": _STRING,
    "darwin.error.type": _STRING,
    # HTTP (OpenTelemetry semantic conventions; no URL, no query, no body)
    "http.request.method": _STRING,
    "http.route": _STRING,
    "http.response.status_code": _INT,
    # queue / worker
    "messaging.system": _STRING,
    "darwin.message.type": _STRING,
    "darwin.queue.result": _STRING,
    "darwin.queue.attempt": _INT,
    "darwin.queue.outcome": _STRING,
    "darwin.trace.context": _STRING,  # "continued" | "fresh" | "invalid"
    # telemetry / signals
    "darwin.event.type": _STRING,
    "darwin.ingest.result": _STRING,
    "darwin.ui.attribution": _STRING,  # "claimed" | "none" (verification is the audit row's)
    "darwin.signals.canonical": _INT,
    "darwin.signals.created": _INT,
    "darwin.signals.superseded": _INT,
    # Product Memory
    "darwin.embedding.provider": _STRING,
    "darwin.memory.top_k": _INT,
    "darwin.memory.result_count": _INT,
    "darwin.memory.chunk_count": _INT,
    "darwin.memory.document_count": _INT,
    "darwin.memory.source_type": _STRING,
    # LLM (GenAI semantic conventions where they fit)
    "gen_ai.system": _STRING,
    "gen_ai.request.model": _STRING,
    "gen_ai.usage.input_tokens": _INT,
    "gen_ai.usage.output_tokens": _INT,
    "darwin.llm.request_version": _STRING,
    # hypothesis / research
    "darwin.hypothesis_run.id": _ID,
    "darwin.research_run.id": _ID,
    "darwin.research.graph_version": _STRING,
    "darwin.research.node": _STRING,
    "darwin.research.sequence": _INT,
    "darwin.research.retrieval_attempts": _INT,
    "darwin.research.llm_calls": _INT,
    "darwin.research.max_retrieval_attempts": _INT,
    "darwin.research.max_llm_calls": _INT,
    "darwin.research.stop_reason": _STRING,
    "darwin.signal.type": _STRING,
    # decision
    "darwin.decision_run.id": _ID,
    "darwin.decider.type": _STRING,
    "darwin.decider.version": _STRING,
    "darwin.decision": _STRING,
    "darwin.decision.fail_closed": _BOOL,
    # mutation
    "darwin.mutation_run.id": _ID,
    "darwin.generator.type": _STRING,
    "darwin.generator.version": _STRING,
    "darwin.mutation.operation_count": _INT,
    # sandbox
    "darwin.evaluation_run.id": _ID,
    "darwin.evaluator.version": _STRING,
    "darwin.harness.version": _STRING,
    "darwin.sandbox.recommendation": _STRING,
    **{
        f"darwin.sandbox.category.{name}": _STRING
        for name in (
            "schema",
            "render",
            "functional",
            "accessibility",
            "regression",
            "ux_intent",
            "performance",
        )
    },
    # experiments
    "darwin.experiment.id": _ID,
    "darwin.experiment.status": _STRING,
    "darwin.experiment.allocation_bp": _INT,
    "darwin.experiment.assessment": _STRING,
    "darwin.experiment.transition": _STRING,
    "darwin.variant": _STRING,
    "darwin.exposure.result": _STRING,
    "darwin.exposure.reason": _STRING,
    # generations
    "darwin.page": _STRING,
    "darwin.generation.from": _INT,
    "darwin.generation.to": _INT,
    "darwin.approval.decision": _STRING,
    "darwin.promotion.eligible": _BOOL,
    "darwin.promotion.reason": _STRING,
}

# Metric name -> its only allowed label keys. Everything else is refused.
METRIC_LABELS: dict[str, frozenset[str]] = {
    "darwin.http.server.requests": frozenset({"http.request.method", "http.route", "status_class"}),
    "darwin.http.server.duration": frozenset({"http.request.method", "http.route", "status_class"}),
    "darwin.queue.enqueued": frozenset({"message_type", "result"}),
    "darwin.queue.depth": frozenset({"state"}),
    "darwin.worker.messages": frozenset({"message_type", "outcome"}),
    "darwin.worker.duration": frozenset({"message_type", "outcome"}),
    "darwin.llm.calls": frozenset({"provider", "request_version", "status"}),
    "darwin.llm.duration": frozenset({"provider", "status"}),
    "darwin.llm.tokens": frozenset({"provider", "direction"}),
    "darwin.stage.runs": frozenset({"stage", "outcome"}),
    "darwin.stage.duration": frozenset({"stage", "outcome"}),
}

STAGES = frozenset(
    {
        "memory_ingest",
        "memory_retrieve",
        "hypothesis",
        "research",
        "decision",
        "mutation",
        "sandbox",
        "experiment_assign",
        "experiment_exposure",
        "experiment_analysis",
        "experiment_transition",
        "approval",
        "promotion",
        "rollback",
    }
)

_VALUE = re.compile(r"^[A-Za-z0-9_.:/{}-]{0,128}$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.:/{}-]{1,64}$")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UUID_ONLY = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_ID = re.compile(r"[0-9a-fA-F]{16,}")  # trace ids, hashes, long hex tokens


class Rejected:
    """Counts what the policy refused (the observability evaluation reads it)."""

    attributes = 0
    labels = 0


def clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only allowlisted keys with values of the declared shape."""
    out: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        kind = SPAN_ATTRIBUTES.get(key)
        if kind is None or value is None:
            if kind is None:
                Rejected.attributes += 1
            continue
        if kind == _BOOL and isinstance(value, bool):
            out[key] = value
        elif kind == _INT and isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
        elif kind == _ID and _UUID_ONLY.fullmatch(str(value)):
            out[key] = str(value).lower()
        elif (
            kind == _STRING
            and isinstance(value, str)
            and _VALUE.fullmatch(value)
            and not _UUID.search(value)
            and not _HEX_ID.search(value)
        ):
            out[key] = value
        else:
            Rejected.attributes += 1
    return out


def clean_labels(metric: str, labels: Mapping[str, Any]) -> dict[str, str] | None:
    """Bounded labels for `metric`, or None (the measurement is dropped, never widened)."""
    allowed = METRIC_LABELS.get(metric)
    if allowed is None:
        Rejected.labels += 1
        return None
    out: dict[str, str] = {}
    for key, value in labels.items():
        text = str(value)
        if (
            key not in allowed
            or not _LABEL_VALUE.fullmatch(text)
            or _UUID.search(text)
            or _HEX_ID.search(text)
        ):
            Rejected.labels += 1
            return None
        out[key] = text
    if "stage" in out and out["stage"] not in STAGES:
        Rejected.labels += 1
        return None
    return out


def bounded(value: object, allowed: frozenset[str] | set[str], other: str = "other") -> str:
    """Map an open vocabulary (e.g. client event types) onto a bounded one."""
    text = str(value)
    return text if text in allowed else other
