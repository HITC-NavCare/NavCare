"""Logging for the API: one JSON object per line, which Cloud Logging reads as jsonPayload.

Only our own "navcare.*" loggers are set up here. Configuring the root logger instead (for
example with logging.basicConfig) would also print every INFO line from libraries such as
httpx, one per outgoing HTTP request.
"""

import json
import logging
import sys

LOGGER_NAME = "navcare"


class JsonFormatter(logging.Formatter):
    """Formats a record as one JSON line. Cloud Logging maps "severity" to the log level.

    Fields passed as extra={"fields": {...}} become top-level keys, so they can be used
    directly in log-based metrics (for example jsonPayload.engine or jsonPayload.latency_ms).
    """

    def format(self, record):
        entry = {
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(stream=sys.stdout):
    """Print navcare.* records from INFO up, as JSON lines. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    if not any(getattr(handler, "navcare", False) for handler in logger.handlers):
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.navcare = True
        logger.addHandler(handler)
    # Records still propagate to the root logger, which has no handler under uvicorn, so
    # nothing is printed twice. (pytest's caplog listens there.)
    return logger
