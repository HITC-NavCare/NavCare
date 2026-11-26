"""request_log: one Firestore document per /v1/recommendations request (offline, fake client)."""

import logging

import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.agent_v2 import ai_agent_recommender as agent
from app.api import facade
from app.api.main import app

PARAMS = {"lat": 45.50891, "lon": -73.56174, "limit": 3, "radius_km": 10}

FAKE_V1 = {
    "data_updated_at": "2026-11-25T12:00:00-05:00",
    "origin": {"lat": 45.50891, "lon": -73.56174},
    "radius_km": 10,
    "weather_now": {},
    "travel_time_multiplier": 1.0,
    "recommendations": [{"hospital": {"id": "11111111"}}, {"hospital": {"id": "22222222"}}],
    "unranked_nearby": [],
    "disclaimer": orchestrator.DISCLAIMER,
}
FAKE_USAGE = {"input_tokens": 1000, "output_tokens": 200}  # use your real shape


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def use_engine(monkeypatch):
    def _use(engine):
        monkeypatch.setattr(facade, "choose_engine", lambda _header: (engine, "test"))
    return _use


@pytest.fixture
def v1_ok(monkeypatch):
    monkeypatch.setattr(orchestrator, "build_recommendations", lambda *a, **k: dict(FAKE_V1))


def _raise(error):
    def _f(*args, **kwargs):
        raise error
    return _f


def test_200_v1_writes_one_document(client, fake_firestore, use_engine, v1_ok):
    use_engine("v1")
    r = client.get("/v1/recommendations",
                   params={**PARAMS, "message": "twisted my ankle playing hockey"})
    assert r.status_code == 200
    body = r.json()

    assert list(fake_firestore.docs) == [body["request_id"]]
    doc = fake_firestore.docs[body["request_id"]]
    assert doc["status"] == 200
    assert doc["engine"] == "v1"
    assert doc["lat"] == 45.51 and doc["lon"] == -73.56
    assert doc["limit"] == 3 and doc["radius_km"] == 10
    assert doc["has_message"] is True
    assert "ankle" not in str(doc)  # no raw message text anywhere
    assert doc["hospital_ids"] == [h["hospital"]["id"] for h in body["recommendations"]]
    assert doc["call_911"] is None  # v1 did not read the message
    assert doc["latency_ms"] is not None


def test_200_v2_stores_usage_and_cost(client, fake_firestore, use_engine, monkeypatch):
    use_engine("v2")
    monkeypatch.setattr(agent, "run_agent", lambda *a, **k: {
        **FAKE_V1, "call_911": False, "note": None, "model_usage": FAKE_USAGE,
    })
    r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 200
    (doc,) = fake_firestore.docs.values()
    assert doc["engine"] == "v2"
    assert doc["model_usage"] == FAKE_USAGE
    assert "model_cost_usd" in doc or "model_cost_unknown" in doc
    assert doc["call_911"] is False


def test_503_writes_document_without_hospitals(client, fake_firestore, use_engine, monkeypatch):
    use_engine("v1")
    monkeypatch.setattr(orchestrator, "build_recommendations", _raise(RuntimeError("ER down")))
    r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 503
    (doc,) = fake_firestore.docs.values()
    assert doc["status"] == 503
    assert doc["hospital_ids"] == []
    assert doc["limit"] == 3  # inputs are still recorded


def test_503_after_v2_failure_keeps_model_usage(client, fake_firestore, use_engine, monkeypatch):
    use_engine("v2")
    agent_error = agent.AgentError("model gave an invalid answer")
    agent_error.model_usage = FAKE_USAGE
    monkeypatch.setattr(agent, "run_agent", _raise(agent_error))
    monkeypatch.setattr(orchestrator, "build_recommendations", _raise(RuntimeError("ER down")))
    r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 503
    (doc,) = fake_firestore.docs.values()
    assert doc["model_usage"] == FAKE_USAGE


def test_500_writes_document(fake_firestore, use_engine, monkeypatch):
    use_engine("v1")
    monkeypatch.setattr(orchestrator, "build_recommendations", _raise(ValueError("bug")))
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 500
    (doc,) = fake_firestore.docs.values()
    assert doc["status"] == 500
    assert doc["request_id"] == r.json()["error"]["request_id"]


def test_firestore_failure_does_not_fail_request(client, fake_firestore, use_engine, v1_ok,
                                                 caplog):
    use_engine("v1")
    fake_firestore.fail = True
    with caplog.at_level(logging.WARNING):
        r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 200
    assert "request_log write failed" in caplog.text


def test_disabled_writes_nothing(client, fake_firestore, use_engine, v1_ok, monkeypatch):
    use_engine("v1")
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 200
    assert fake_firestore.docs == {}

def test_bad_record_does_not_fail_request(client, fake_firestore, use_engine, monkeypatch):
    use_engine("v1")
    broken = {**FAKE_V1, "recommendations": [{"no_id_here": 1}]}
    monkeypatch.setattr(orchestrator, "build_recommendations", lambda *a, **k: dict(broken))
    r = client.get("/v1/recommendations", params=PARAMS)
    assert r.status_code == 200
    assert fake_firestore.docs == {}