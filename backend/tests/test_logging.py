import json
import logging

from darwin.logging_config import ROOT_LOGGER_NAME, JsonFormatter, configure_logging


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="darwin.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_log_lines_are_json_with_standard_fields() -> None:
    line = JsonFormatter().format(_record("hello"))

    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "darwin.test"
    assert "timestamp" in payload


def test_context_is_included_as_structured_data() -> None:
    line = JsonFormatter().format(_record("started", context={"env": "test", "port": 8000}))

    assert json.loads(line)["context"] == {"env": "test", "port": 8000}


def test_non_serialisable_values_do_not_break_logging() -> None:
    line = JsonFormatter().format(_record("odd", context={"value": object()}))

    assert json.loads(line)["message"] == "odd"


def test_configure_logging_sets_level_and_is_idempotent() -> None:
    configure_logging("DEBUG")
    configure_logging("ERROR")

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    assert logger.level == logging.ERROR
    assert len(logger.handlers) == 1
