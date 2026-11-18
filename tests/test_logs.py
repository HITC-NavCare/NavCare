import io
import json
import logging
import logging.config

from uvicorn.config import LOGGING_CONFIG

from app.api import logs


def test_info_lines_are_printed_under_uvicorn_logging():
    # uvicorn leaves the root logger at WARNING; our loggers must still print INFO.
    logging.config.dictConfig(LOGGING_CONFIG)
    logs.configure_logging()

    assert logging.getLogger("navcare.api").isEnabledFor(logging.INFO)


def test_records_are_one_json_line_with_fields_at_the_top():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    logger = logging.getLogger("navcare.test_json")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.warning("fell back", extra={"fields": {"engine": "v1_fallback", "latency_ms": 12}})

    line = stream.getvalue()
    assert line.count("\n") == 1
    assert json.loads(line) == {
        "severity": "WARNING", "logger": "navcare.test_json", "message": "fell back",
        "engine": "v1_fallback", "latency_ms": 12,
    }


def test_exceptions_include_the_stack_trace():
    try:
        raise KeyError("occupancyPct")
    except KeyError:
        record = logging.LogRecord("navcare", logging.ERROR, "", 0, "bug", None, True)
        record.exc_info = __import__("sys").exc_info()

    entry = json.loads(logs.JsonFormatter().format(record))

    assert "KeyError: 'occupancyPct'" in entry["stack_trace"]


def test_configure_logging_adds_its_handler_once():
    logger = logs.configure_logging(io.StringIO())
    logs.configure_logging(io.StringIO())

    assert sum(getattr(h, "navcare", False) for h in logger.handlers) == 1
