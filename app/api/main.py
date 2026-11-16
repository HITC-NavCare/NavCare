"""NAVCare REST API. Run locally with:  uvicorn app.api.main:app --reload

The /v1 in the paths is the API version, not the recommender engine. Both engines
(v1 rules, v2 agent) will answer behind the same paths; `engine` says which one did.
"""

import logging
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import orchestrator
from app.recommender_v1 import rules_based_recommender as recommender
from app.services.nearest_hospital_getter import DEFAULT_RADIUS_KM

# Read local API keys from .env (not committed). In Cloud Run they come from Secret Manager.
load_dotenv()

MONTREAL = ZoneInfo("America/Montreal")
ENGINE = "v1"  # the only engine behind the API for now

# Shown to clients instead of our internal error text. The details go to the logs,
# with the request id, so a failed request can still be traced.
DATA_UNAVAILABLE_MESSAGE = "A data source is temporarily unavailable. Please try again later."
INTERNAL_ERROR_MESSAGE = "Unexpected server error."

logger = logging.getLogger("navcare.api")

app = FastAPI(
    title="NAVCare API",
    version="0.1.0",
    description="Ranks nearby Quebec ERs by expected time-to-care. "
    "Not for life-threatening emergencies. Call 911.",
)


def error_response(status_code, code, message, request_id):
    """The standard error body from docs/openapi.yaml (components/schemas/Error)."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={"X-Request-ID": request_id},
    )


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    # One id per request: returned to the client and, later, stored in request_log.
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


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
    logger.error("request_id=%s unexpected error", request_id, exc_info=error)
    return error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE, request_id)


@app.get("/v1/health")
def get_health():
    return {"status": "ok", "time": datetime.now(MONTREAL).isoformat()}


@app.get("/v1/recommendations")
def get_recommendations(
    request: Request,
    lat: float = Query(ge=44.9, le=62.6, description="Latitude, within Quebec"),
    lon: float = Query(ge=-79.8, le=-57.1, description="Longitude, within Quebec"),
    limit: int = Query(default=recommender.DEFAULT_LIMIT, ge=1, le=10),
    radius_km: float = Query(default=DEFAULT_RADIUS_KM, ge=1, le=200),
):
    request_id = request.state.request_id
    try:
        result = orchestrator.build_recommendations(lat, lon, limit=limit, radius_km=radius_km)
    except RuntimeError as error:
        # A data service (routing, weather or ER status) failed: say so, don't guess.
        logger.warning("request_id=%s data unavailable: %s", request_id, error)
        return error_response(503, "data_unavailable", DATA_UNAVAILABLE_MESSAGE, request_id)
    return {
        "request_id": request_id,
        "requested_at": datetime.now(MONTREAL).isoformat(),
        "engine": ENGINE,
        **result,
    }

