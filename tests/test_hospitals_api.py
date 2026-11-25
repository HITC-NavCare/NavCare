import pytest
from fastapi.testclient import TestClient

from app.api import auth
from app.api.main import app
from app.services import hospital_directory, hospital_status_getter
from tests.factories import make_er_data, make_status

client = TestClient(app)
VERDUN = "51218097"  # in data/hospitals.csv, region Montréal
MATANE = "51218980"  # Bas-Saint-Laurent
SAINTE_JUSTINE = "51222800"  # children's ER (specialised)
NO_DATA = {"occupancyPct": None, "avgStayAmbulatoryHours": None}


@pytest.fixture
def er_feed(monkeypatch):
    """Live status for Verdun and Sainte-Justine; Matane publishes nothing current."""
    calls = []

    def get_er_data():
        calls.append(1)
        return make_er_data({VERDUN: make_status(occupancy_pct=125, waiting=12),
                             SAINTE_JUSTINE: make_status(),
                             MATANE: NO_DATA})

    monkeypatch.setattr(hospital_status_getter, "get_er_data", get_er_data)
    return calls


def by_id(body):
    return {hospital["id"]: hospital for hospital in body["hospitals"]}


def test_list_returns_every_er_with_live_status(er_feed):
    response = client.get("/v1/hospitals")
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["count"] == len(body["hospitals"]) == len(hospital_directory.load_hospitals())
    assert body["data_updated_at"] == "2026-11-10T20:45:00.000Z" and body["stale"] is False
    verdun = by_id(body)[VERDUN]
    assert verdun["region"] == "Montréal" and verdun["specialised"] is False
    assert verdun["has_live_data"] is True
    assert verdun["status"]["occupancy_rate"] == 1.25
    assert verdun["status"]["waiting"] == 12
    matane = by_id(body)[MATANE]
    assert matane["has_live_data"] is False and matane["status"] is None
    assert by_id(body)[SAINTE_JUSTINE]["specialised"] is True
    assert len(er_feed) == 1  # one feed request for the whole list


def test_filters(er_feed):
    montreal = client.get("/v1/hospitals", params={"region": "  montréal "}).json()
    assert montreal["count"] > 0
    assert all(h["region"] == "Montréal" for h in montreal["hospitals"])
    live = client.get("/v1/hospitals", params={"has_live_data": "true"}).json()
    assert {h["id"] for h in live["hospitals"]} == {VERDUN, SAINTE_JUSTINE}
    without = client.get("/v1/hospitals", params={"has_live_data": "false"}).json()
    assert MATANE in by_id(without) and VERDUN not in by_id(without)
    assert client.get("/v1/hospitals", params={"region": "Atlantis"}).json()["count"] == 0


def test_one_hospital(er_feed):
    response = client.get(f"/v1/hospitals/{VERDUN}")
    assert response.status_code == 200
    body = response.json()
    assert body["hospital"]["id"] == VERDUN and body["hospital"]["has_live_data"] is True
    assert body["data_updated_at"] and body["stale"] is False


def test_unknown_id_is_404_without_calling_the_feed(er_feed):
    response = client.get("/v1/hospitals/12345678")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert er_feed == []


def test_malformed_id_is_400():
    response = client.get("/v1/hospitals/H0001")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_feed_failure_is_503(monkeypatch):
    def fail():
        raise RuntimeError("ER status request failed: secret detail")

    monkeypatch.setattr(hospital_status_getter, "get_er_data", fail)
    for path in ("/v1/hospitals", f"/v1/hospitals/{VERDUN}"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "data_unavailable"
        assert "secret detail" not in response.text


def test_hospitals_need_a_key_and_share_the_rate_limit(er_feed, monkeypatch):
    monkeypatch.delenv(auth.DISABLED_ENV_VAR)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, "demo:k1")
    monkeypatch.setenv(auth.LIMIT_ENV_VAR, "2")
    assert client.get("/v1/hospitals").status_code == 401
    assert client.get(f"/v1/hospitals/{VERDUN}").status_code == 401
    key = {"X-API-Key": "k1"}
    assert client.get("/v1/hospitals", headers=key).status_code == 200
    assert client.get(f"/v1/hospitals/{VERDUN}", headers=key).status_code == 200
    assert client.get("/v1/hospitals", headers=key).status_code == 429


def test_live_status_mapping():
    assert hospital_directory.live_status(None) is None
    assert hospital_directory.live_status(NO_DATA) is None
    status = hospital_directory.live_status(make_status(occupancy_pct=80, over_24h=3))
    assert status == {
        "stretchers": 20, "on_stretcher": 20, "occupancy_rate": 0.8, "present": 40,
        "waiting": 10, "over_24h": 3, "over_48h": 0,
        "avg_ambulatory_stay_hours": 4.0, "avg_stretcher_stay_hours": 15.0,
    }
