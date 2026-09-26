"""Structured (JSON-lines) logging using only the standard library.

Each log record becomes one JSON object per line, so logs are readable by a
human and parseable by machines (CloudWatch, jq) without a logging framework.

Usage::

    logger = logging.getLogger(__name__)          # e.g. "darwin.main"
    logger.info("application started", extra={"context": {"env": "local"}})

Only the ``darwin`` logger tree is configured. Uvicorn keeps its own log
format for server/access lines.
"""

import json
import logging
from datetime import UTC, datetime

from darwin.config import LogLevel

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
