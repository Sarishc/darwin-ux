"""Structured (JSON-lines) logging using only the standard library.

Each log record becomes one JSON object per line, so logs are readable by a
human and parseable by machines (CloudWatch, jq) without a logging framework.

Usage::

    logger = logging.getLogger(__name__)          # e.g. "darwin.main"
    logger.info("application started", extra={"context": {"env": "local"}})

Only the ``darwin`` logger tree is configured. Uvicorn keeps its own log
format for server/access lines. When an OpenTelemetry span is active, each line
also carries ``trace_id`` and ``span_id`` (absent otherwise) — no caller plumbing.
"""

import json
import logging
from datetime import UTC, datetime

from darwin.config import LogLevel
from darwin.observability.tracing import current_ids

ROOT_LOGGER_NAME = "darwin"


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = context
        # Step 16: correlate with the active OpenTelemetry span, when there is one.
        trace_id, span_id = current_ids()
        if trace_id is not None:
            payload["trace_id"], payload["span_id"] = trace_id, span_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: never crash while logging because a value isn't JSON-serialisable.
        return json.dumps(payload, default=str)


def configure_logging(level: LogLevel) -> None:
    """Configure the ``darwin`` logger tree. Safe to call more than once."""
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    # Replace rather than append, so repeated app creation (e.g. in tests)
    # does not print every line several times.
    logger.handlers = [handler]
    logger.propagate = False
