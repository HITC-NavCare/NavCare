import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.api import facade
from app.api.main import app

client = TestClient(app)
MONTREAL = {"lat": 45.5017, "lon": -73.5673}
V1_RESULT = {"data_updated_at": "2026-11-13T12:00", "recommendations": []}
V2_RESULT = {
    "engine": "v2",
    "data_updated_at": "2026-11-13T12:00",
    "recommendations": [],
    "call_911": False,
    "note": "Go to a children's ER.",
    "model_usage": {"model": "gemini-test", "model_calls": 2},
}


@pytest.fixture(autouse=True)
def all_traffic_to_v1(monkeypatch):
    # A local .env may set the split; tests that need another value set it themselves.
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")


@pytest.fixture
def fake_recommendations(monkeypatch):
    calls = []

    def build(lat, lon, limit, radius_km):
        calls.append((lat, lon, limit, radius_km))
        return V1_RESULT

    monkeypatch.setattr(orchestrator, "build_recommendations", build)
    return calls


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []

    def build(lat, lon, message, limit, radius_km):
        calls.append((lat, lon, message, limit, radius_km))
        return V2_RESULT

    monkeypatch.setattr(orchestrator, "build_agent_recommendations", build)
    return calls


def test_health():
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"]


def test_recommendations_passes_parameters_and_adds_request_id(fake_recommendations):
    response = client.get("/v1/recommendations", params={**MONTREAL, "limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["engine"] == "v1"
    assert body["data_updated_at"] == "2026-11-13T12:00"
    assert fake_recommendations == [(45.5017, -73.5673, 2, 50)]


@pytest.mark.parametrize(
    "params",
    [{"lon": -73.5}, {"lat": 10, "lon": -73.5}, {**MONTREAL, "limit": 0}],
    ids=["missing_lat", "outside_quebec", "limit_too_small"],
)
def test_bad_parameters_return_standard_400(fake_recommendations, params):
    response = client.get("/v1/recommendations", params=params)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"
    assert fake_recommendations == []


def test_service_failure_returns_503(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("ER status request failed: timeout")

    monkeypatch.setattr(orchestrator, "build_recommendations", fail)
    response = client.get("/v1/recommendations", params=MONTREAL)

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "data_unavailable",
        "message": "A data source is temporarily unavailable. Please try again later.",
        "request_id": response.headers["X-Request-ID"],
    }


def test_503_does_not_show_internal_details_but_logs_them(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("Route request failed: GOOGLE_MAPS_API_KEY is not set")

    monkeypatch.setattr(orchestrator, "build_recommendations", fail)
    response = client.get("/v1/recommendations", params=MONTREAL)

    assert "GOOGLE_MAPS_API_KEY" not in response.text
    request_id = response.headers["X-Request-ID"]
    assert any(
        request_id in record.getMessage() and "GOOGLE_MAPS_API_KEY" in record.getMessage()
        for record in caplog.records
    )


def test_unexpected_error_returns_standard_500(monkeypatch, caplog):
    def bug(*args, **kwargs):
        raise KeyError("occupancyPct")

    monkeypatch.setattr(orchestrator, "build_recommendations", bug)
    # Starlette re-raises after sending the 500; a real server only logs it.
    safe_client = TestClient(app, raise_server_exceptions=False)
    response = safe_client.get("/v1/recommendations", params=MONTREAL)

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "Unexpected server error.",
        "request_id": request_id,
    }
    assert "occupancyPct" not in response.text
    assert any(request_id in record.getMessage() for record in caplog.records)


# --- facade: v1 and v2 behind the same endpoint ---------------------------------------

def test_split_at_zero_sends_requests_to_v1(fake_recommendations, fake_agent):
    response = client.get("/v1/recommendations", params={**MONTREAL, "message": "fever"})

    assert response.json()["engine"] == "v1"
    assert len(fake_recommendations) == 1
    assert fake_agent == []


def test_split_at_100_sends_requests_to_v2(monkeypatch, fake_recommendations, fake_agent):
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "100")

    response = client.get("/v1/recommendations", params={**MONTREAL, "message": "fever"})

    assert response.json()["engine"] == "v2"
    assert fake_agent == [(45.5017, -73.5673, "fever", 4, 50)]
    assert fake_recommendations == []


def test_header_forces_v2_and_passes_the_message(fake_agent):
    response = client.get(
        "/v1/recommendations",
        params={**MONTREAL, "limit": 2, "radius_km": 30, "message": "mon enfant a de la fièvre"},
        headers={"X-Engine": "v2"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "v2"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["call_911"] is False
    assert body["note"] == "Go to a children's ER."
    assert fake_agent == [(45.5017, -73.5673, "mon enfant a de la fièvre", 2, 30)]


def test_header_forces_v1_even_at_100(monkeypatch, fake_recommendations, fake_agent):
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "100")

    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v1"})

    assert response.json()["engine"] == "v1"
    assert fake_agent == []


def test_v1_and_v2_responses_have_the_same_fields(fake_recommendations, fake_agent):
    v1 = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v1"}).json()
    v2 = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"}).json()

    assert set(v1) == set(v2)
    assert v1["call_911"] is None
    assert v1["model_usage"] is None


def test_v1_says_when_it_did_not_read_the_message(fake_recommendations):
    with_message = client.get("/v1/recommendations", params={**MONTREAL, "message": "fever"})
    without = client.get("/v1/recommendations", params={**MONTREAL, "message": "  "})

    assert with_message.json()["note"] == orchestrator.MESSAGE_NOT_READ_NOTE
    assert without.json()["note"] is None


def test_fallback_reason_is_logged_not_returned(monkeypatch, caplog):
    def fallback(*args, **kwargs):
        return {**V1_RESULT, "engine": "v1_fallback",
                "fallback_reason": "Gemini request failed: 403 PERMISSION_DENIED",
                "call_911": None, "note": orchestrator.FALLBACK_NOTE, "model_usage": None}

    monkeypatch.setattr(orchestrator, "build_agent_recommendations", fallback)
    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"})

    body = response.json()
    assert body["engine"] == "v1_fallback"
    assert "fallback_reason" not in body
    assert "PERMISSION_DENIED" not in response.text
    request_id = response.headers["X-Request-ID"]
    assert any(
        request_id in record.getMessage() and "PERMISSION_DENIED" in record.getMessage()
        for record in caplog.records
    )


def test_v2_service_failure_returns_503(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Route request failed: timeout")

    monkeypatch.setattr(orchestrator, "build_agent_recommendations", fail)
    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "data_unavailable"


@pytest.mark.parametrize(
    ("params", "headers"),
    [({**MONTREAL, "message": "x" * 501}, {}), (MONTREAL, {"X-Engine": "v3"})],
    ids=["message_too_long", "unknown_engine"],
)
def test_bad_message_or_engine_returns_400(fake_recommendations, fake_agent, params, headers):
    response = client.get("/v1/recommendations", params=params, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"
    assert fake_recommendations == [] and fake_agent == []
