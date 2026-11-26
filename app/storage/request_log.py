"""Store one document per recommendation request in Firestore (`request_log`)."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from fastapi import BackgroundTasks, FastAPI, Header, Query, Request
from starlette.background import BackgroundTask

from app.storage import request_log

logger = logging.getLogger("navcare.request_log")

COLLECTION = "request_log"
COORD_DECIMALS = 2  # about 1 km; match docs/data-model.md
WRITE_TIMEOUT_SECONDS = 3.0

_client: Any = None
_client_failed = False


def _enabled() -> bool:
    return os.getenv("REQUEST_LOG_ENABLED", "true").lower() not in {"0", "false", "no"}


def set_client(client: Any) -> None:
    """Inject a client (tests use a fake)."""
    global _client, _client_failed
    _client, _client_failed = client, False


def _get_client() -> Any:
    global _client, _client_failed
    if _client is None and not _client_failed:
        try:
            from google.cloud import firestore
            _client = firestore.Client(database=os.getenv("FIRESTORE_DATABASE", "(default)"))
            #use the default database unless specified otherwise
        except Exception as error:  # no credentials, no project, etc.
            _client_failed = True
            logger.warning("request_log disabled: cannot create Firestore client: %s", error)
    return _client


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, COORD_DECIMALS)


def build_record(fields: dict, inputs: dict, result: dict | None = None) -> dict:
    """One request_log document: the log line's fields, the request's inputs and, for a 200,
    what was recommended. No message text; coordinates rounded.
    """
    record = {k: v for k, v in fields.items() if k != "event"}
    record.update({
        "timestamp": inputs.get("requested_at"),
        "lat": _round(inputs.get("lat")),
        "lon": _round(inputs.get("lon")),
        "limit": inputs.get("limit"),
        "radius_km": inputs.get("radius_km"),
        "has_message": inputs.get("has_message"),
        "hospital_ids": [r["hospital"]["id"] for r in (result or {}).get("recommendations", [])],
        # true / false / null: null means nobody read the message (v1 or fallback).
        "call_911": (result or {}).get("call_911"),
    })
    return record


def save(fields: dict, inputs: dict, result: dict | None = None) -> None:
    """Build and write the record. Never raises: request_log must not fail a request."""
    if not _enabled():
        return
    record = {}
    try:
        record = build_record(fields, inputs, result)
        client = _get_client()
        if client is None:
            return
        client.collection(COLLECTION).document(record["request_id"]).set(
            record, timeout=WRITE_TIMEOUT_SECONDS
        )
    except Exception as error:
        logger.warning("request_log write failed for %s: %s",
                       record.get("request_id") or fields.get("request_id"), error)

def outputs_from_response(response: dict) -> dict:
    """Fields of a 200 response that go into the request_log document."""
    return {
        "hospital_ids": [r["id"] for r in response.get("recommendations", [])],
        "call_911": response.get("call_911"),
        "fallback_reason": response.get("fallback_reason"),
        "data_updated_at": response.get("data_updated_at"),
    }