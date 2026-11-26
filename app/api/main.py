"""NAVCare REST API. Run locally with:  uvicorn app.api.main:app --reload

The /v1 in the paths is the API version, not the recommender engine. Both engines
(v1 rules, v2 agent) answer behind the same paths; the facade (app/api/facade.py) picks one
for each request, and `engine` in the response says which one did.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, Path, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from fastapi.security import APIKeyHeader

import orchestrator
from app import cost_estimates
from app.agent_v2.ai_agent_recommender import DEFAULT_LOCATION, LOCATION_ENV_VAR, MAX_MESSAGE_CHARS
from app.api import auth, facade
from app.api.logs import configure_logging
from app.recommender_v1 import rules_based_recommender as recommender
from app.services import hospital_directory
from app.services.nearest_hospital_getter import DEFAULT_RADIUS_KM
from app.storage import request_log

# Read local API keys from .env (not committed). In Cloud Run they come from Secret Manager.
load_dotenv()
configure_logging()

MONTREAL = ZoneInfo("America/Montreal")
# Every response has these fields, whichever engine answered. v1 cannot fill them.
V2_ONLY_FIELDS = {"call_911": None, "note": None, "model_usage": None}

# Shown to clients instead of our internal error text. The details go to the logs,
# with the request id, so a failed request can still be traced.
DATA_UNAVAILABLE_MESSAGE = "A data source is temporarily unavailable. Please try again later."
INTERNAL_ERROR_MESSAGE = "Unexpected server error."

logger = logging.getLogger("navcare.api")


def log_recommendation(request, status, engine, level=logging.INFO, message="recommendation",
                       model_usage=None, exc_info=None):
    """The one log line per recommendation request that the canary metrics are built from:
    error rate (status), p95 latency (latency_ms) and cost (model_usage), for each engine.
    """
    started = getattr(request.state, "started", None)

    fields = {
       "event": "recommendation",
        "request_id": request.state.request_id,
        "status": status,
        "engine": engine,  # v1, v2 or v1_fallback
        "chosen_by": getattr(request.state, "chosen_by", None),  # split or header
        "api_key_name": getattr(request.state, "api_key_name", None),  # who called, never the key
        "latency_ms": None if started is None else round((time.monotonic() - started) * 1000),
        "model_usage": model_usage,
        **model_cost_fields(engine, model_usage),
    }

    logger.log(level, message, exc_info=exc_info, extra={"fields": fields})
    return fields  # for request_log, which is written asynchronously in the background



def model_cost_fields(engine, model_usage, now=None):
    """Estimated model-token cost of a line that went to v2 (canary criterion 6), else nothing.

    model_cost_usd when the estimate is known, otherwise model_cost_unknown with the reason, so
    missing estimates can be counted. Prices and rules are in app/cost_estimates.py.
    The estimator prices a timestamp on its Cloud Billing day (Pacific). now: for tests.
    """
    if engine not in ("v2", "v1_fallback"):
        return {}
    location = os.environ.get(LOCATION_ENV_VAR, DEFAULT_LOCATION)  # as the agent reads it
    now = now or datetime.now(timezone.utc)
    estimate = cost_estimates.estimate_gemini_cost(model_usage, location, now)
    if estimate["estimated_usd"] is None:
        return {"model_cost_unknown": estimate["unknown_reason"]}
    return {"model_cost_usd": estimate["estimated_usd"]}

app = FastAPI(
    title="NAVCare API",
    version="0.1.0",
    description="Ranks nearby Quebec ERs by expected time-to-care. "
    "Not for life-threatening emergencies. Call 911.",
)


def error_response(status_code, code, message, request_id, headers=None):
    """The standard error body from docs/openapi.yaml (components/schemas/Error)."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={"X-Request-ID": request_id, **(headers or {})},
    )


# Declared with FastAPI's security helper so /docs gets an "Authorize" button and
# /openapi.json lists the X-API-Key scheme. auto_error=False: auth.check_request decides.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                              description="Your NAVCare API key.")


def require_api_key(request: Request, api_key: str | None = Security(api_key_header)):
    """Runs before /v1/recommendations: 401 without a valid key, 429 over the rate limit."""
    request.state.api_key_name, request.state.rate_limit_headers = auth.check_request(api_key)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    # One id per request: returned to the client and, later, stored in request_log.
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    # X-RateLimit-Limit and -Remaining on every answer to a key that passed the check.
    response.headers.update(getattr(request.state, "rate_limit_headers", {}))
    return response


@app.exception_handler(auth.ApiKeyError)
async def api_key_refused(request: Request, error: auth.ApiKeyError):
    # Not a "recommendation" log line: refused callers are not part of the canary error rate.
    logger.info("api key refused", extra={"fields": {
        "event": "api_key_refused", "request_id": request.state.request_id,
        "status": error.status_code, "code": error.code,
    }})
    return error_response(error.status_code, error.code, error.message,
                          request.state.request_id, error.headers)


@app.exception_handler(RequestValidationError)
async def invalid_parameter(request: Request, error: RequestValidationError):
    first = error.errors()[0]
    field = first["loc"][-1]
    return error_response(400, "invalid_parameter", f"{field}: {first['msg']}",
                          request.state.request_id)


@app.exception_handler(Exception)
async def internal_error(request: Request, error: Exception):
    # Any bug we did not expect: same error body and request id as other errors.
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    request.state.request_id = request_id
    engine = getattr(request.state, "engine", None)
    if engine is None:  # not a recommendation request
        logger.error("request_id=%s unexpected error", request_id, exc_info=error)
    else:  # counted in that engine's error rate
        fields = log_recommendation(request, 500, engine, logging.ERROR,
                                    f"request_id={request_id} unexpected error", exc_info=error)
        response = error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE, request_id)
        response.background = BackgroundTask(
            request_log.save, fields, getattr(request.state, "log_inputs", {})
        )
        return response
    return error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE, request_id)


@app.get("/v1/health")
def get_health():
    return {"status": "ok", "time": datetime.now(MONTREAL).isoformat()}


@app.get("/v1/recommendations", dependencies=[Depends(require_api_key)])
def get_recommendations(
    request: Request,
    background_tasks: BackgroundTasks,
    lat: float = Query(ge=44.9, le=62.6, description="Latitude, within Quebec"),
    lon: float = Query(ge=-79.8, le=-57.1, description="Longitude, within Quebec"),
    limit: int = Query(default=recommender.DEFAULT_LIMIT, ge=1, le=10),
    radius_km: float = Query(default=DEFAULT_RADIUS_KM, ge=1, le=200),
    message: str | None = Query(
        default=None, max_length=MAX_MESSAGE_CHARS,
        description="Optional, in the person's own words (French or English). "
        "Only v2 reads it.",
    ),
    x_engine: Literal["v1", "v2"] | None = Header(
        default=None, description="Force one engine (tests and demo). Only honoured where "
        "ENGINE_HEADER_ENABLED=true; otherwise, and without it, the canary split decides.",
    ),
):
    request_id = request.state.request_id
    request.state.started = time.monotonic()
    has_message = bool(message and message.strip())
    request.state.log_inputs = {
        "requested_at": datetime.now(timezone.utc),
        "lat": lat, "lon": lon, "limit": limit, "radius_km": radius_km,
        "has_message": has_message,
    }
    engine, request.state.chosen_by = facade.choose_engine(x_engine)
    request.state.engine = engine
    try:
        if engine == "v2":
            result = orchestrator.build_agent_recommendations(
                lat, lon, message, limit=limit, radius_km=radius_km
            )
        else:
            result = orchestrator.build_recommendations(lat, lon, limit=limit, radius_km=radius_km)
            has_message = bool(message and message.strip())
            result = {
                **result,
                "engine": "v1",
                "call_911": orchestrator.call_911_without_model(message),
                "note": orchestrator.MESSAGE_NOT_READ_NOTE if has_message else None,
            }
    except RuntimeError as error:
        # A data service (routing, weather or ER status) failed: say so, don't guess.
        fields = log_recommendation(request, 503, engine, logging.WARNING,
            f"request_id={request_id} data unavailable: {error}",
              model_usage=getattr(error, "model_usage", None))
        background_tasks.add_task(request_log.save, fields, request.state.log_inputs)
        return error_response(503, "data_unavailable", DATA_UNAVAILABLE_MESSAGE, request_id)

    result = dict(result)
    # Internal error text stays in the logs, like for 503s; clients see engine "v1_fallback".
    fallback_reason = result.pop("fallback_reason", None)
    if fallback_reason:
        fields = log_recommendation(request, 200, result["engine"], logging.WARNING,
                           f"request_id={request_id} v2 fell back to v1: {fallback_reason}",
                           model_usage=result.get("model_usage"))
    else:
        fields = log_recommendation(request, 200, result["engine"],
                           model_usage=result.get("model_usage"))
        background_tasks.add_task(request_log.save, fields, request.state.log_inputs, result)
    return {
        "request_id": request_id,
        "requested_at": datetime.now(MONTREAL).isoformat(),
        "engine": result.pop("engine"),
        **V2_ONLY_FIELDS,
        **result,
    }


# The ER list. Free for us (our CSV and the public ER feed), but it still needs a key, and the
# requests count in the same per-key limit, so the service stays closed to anonymous callers.
@app.get("/v1/hospitals", dependencies=[Depends(require_api_key)])
def get_hospitals(
    request: Request,
    region: str | None = Query(default=None, max_length=100,
                               description="Health region as published by MSSS, e.g. Montréal"),
    has_live_data: bool | None = Query(
        default=None, description="true: only ERs with current data; false: only ERs without"),
):
    try:
        result = hospital_directory.list_hospitals(region=region, has_live_data=has_live_data)
    except RuntimeError as error:
        return hospital_data_unavailable(request, error)
    return {"request_id": request.state.request_id, **result}


@app.get("/v1/hospitals/{hospital_id}", dependencies=[Depends(require_api_key)])
def get_hospital(
    request: Request,
    hospital_id: str = Path(pattern=r"^[0-9]{8}$",
                            description="MSSS 8-digit installation code, e.g. 51218097"),
):
    try:
        result = hospital_directory.get_hospital(hospital_id)
    except RuntimeError as error:
        return hospital_data_unavailable(request, error)
    if result is None:
        return error_response(404, "not_found", f"No ER with id {hospital_id}.",
                              request.state.request_id)
    return {"request_id": request.state.request_id, **result}


def hospital_data_unavailable(request, error):
    logger.warning("request_id=%s hospitals: data unavailable: %s",
                   request.state.request_id, error)
    return error_response(503, "data_unavailable", DATA_UNAVAILABLE_MESSAGE,
                          request.state.request_id)
