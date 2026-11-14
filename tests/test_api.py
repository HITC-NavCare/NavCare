import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.api.main import app

client = TestClient(app)
MONTREAL = {"lat": 45.5017, "lon": -73.5673}


@pytest.fixture
def fake_recommendations(monkeypatch):
    calls = []

    def build(lat, lon, limit, radius_km):
        calls.append((lat, lon, limit, radius_km))
        return {"data_updated_at": "2026-11-13T12:00", "recommendations": []}

    monkeypatch.setattr(orchestrator, "build_recommendations", build)
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
        "message": "ER status request failed: timeout",
        "request_id": response.headers["X-Request-ID"],
    }
