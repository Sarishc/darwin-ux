"""W3C Trace Context through the durable queue.

The producer (API) injects the current trace context into the queue message's own
`traceparent` / `tracestate` columns — never into the telemetry payload, which is
untrusted client data and part of the product record. The worker extracts it and
continues the trace.

Trace context is operational metadata: it is never used for authorization, and a
missing or malformed value simply starts a fresh trace — it can never make a message
fail. Values are validated before they are stored and before they are used.
"""

import re

from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# version 00: 00-<32 hex trace id>-<16 hex parent id>-<2 hex flags>; all-zero ids invalid.
TRACEPARENT = re.compile(r"^00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$")
TRACESTATE_MAX = 512
_TRACESTATE = re.compile(r"^[\x20-\x7e]*$")  # printable ASCII only

_propagator = TraceContextTextMapPropagator()


def valid_traceparent(value: object) -> str | None:
    return value if isinstance(value, str) and TRACEPARENT.fullmatch(value) else None


def valid_tracestate(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > TRACESTATE_MAX:
        return None
    return value if _TRACESTATE.fullmatch(value) else None


def inject_current() -> tuple[str | None, str | None]:
    """(traceparent, tracestate) of the active span, validated; (None, None) without one."""
    try:
        carrier: dict[str, str] = {}
        _propagator.inject(carrier)
        traceparent = valid_traceparent(carrier.get("traceparent"))
        tracestate = valid_tracestate(carrier.get("tracestate")) if traceparent else None
        return traceparent, tracestate
    except Exception:  # noqa: BLE001
        return None, None


def extract(traceparent: object, tracestate: object) -> tuple[Context | None, str]:
    """The producer's context and how it was obtained: 'continued' | 'fresh' | 'invalid'."""
    if traceparent is None:
        return None, "fresh"
    parent = valid_traceparent(traceparent)
    if parent is None:
        return None, "invalid"
    try:
        carrier = {"traceparent": parent}
        state = valid_tracestate(tracestate)
        if state:
            carrier["tracestate"] = state
        return _propagator.extract(carrier), "continued"
    except Exception:  # noqa: BLE001
        return None, "invalid"
