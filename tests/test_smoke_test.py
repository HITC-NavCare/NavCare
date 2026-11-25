import csv

import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.api import facade
from app.api.main import app
from app.services import nearest_hospital_getter as routes
from deployment import smoke_test
from tests.factories import make_er_data, make_status

client = TestClient(app)
WEATHER_ROWS = [{"date": "2026-11-10 21:00:00+00:00", "precipitation": 0.0, "snowfall": 0.0}]


@pytest.fixture(autouse=True)
def real_v1_with_faked_services(monkeypatch):
    """The real API and v1 ranking on our hospital data; only the paid web services are faked."""
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")
    monkeypatch.setenv(facade.HEADER_ENABLED_ENV_VAR, "true")
    with open(routes.HOSPITALS_CSV_PATH, encoding="utf-8") as file:
        ids = [row["id"] for row in csv.DictReader(file)]

    def drive_times(lat, lon, destinations):
        return [{"duration_min": 10.0 + i,
                 "distance_km": 1.3 * routes.straight_line_km(lat, lon, *destination)}
                for i, destination in enumerate(destinations)]

    monkeypatch.setattr(routes, "get_drive_times", drive_times)
    monkeypatch.setattr(orchestrator, "get_er_data",
                        lambda: make_er_data({hospital_id: make_status() for hospital_id in ids}))
    monkeypatch.setattr(orchestrator, "get_weather_data", lambda lat, lon: WEATHER_ROWS)


def get(path, params=None, headers=None):
    return client.get(path, params=params, headers=headers)


def test_health_passes():
    assert smoke_test.check_health(get) == []


def test_real_v1_answer_passes_the_checker():
    assert smoke_test.check_recommendation(get, "v1") == []
    body = get("/v1/recommendations", params=smoke_test.MONTREAL).json()
    assert len(body["recommendations"]) == 4  # real Montréal hospitals, not an empty list


def test_v2_fallback_fails_even_with_status_200(monkeypatch):
    def failing(*args, **kwargs):
        raise orchestrator.agent.AgentError("Gemini request failed: 503")

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    problems = smoke_test.check_recommendation(get, "v2", {"X-Engine": "v2"})

    assert problems == ["engine 'v1_fallback', expected 'v2'"]


def test_header_ignored_shows_as_wrong_engine(monkeypatch):
    monkeypatch.setenv(facade.HEADER_ENABLED_ENV_VAR, "false")

    problems = smoke_test.check_recommendation(get, "v2", {"X-Engine": "v2"})

    assert problems[0] == "engine 'v1', expected 'v2'"


def test_schema_or_data_problems_fail():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"engine": "v1", "recommendations": "not a list"}

    problems = smoke_test.check_recommendation(lambda *a, **k: Response(), "v1")

    assert problems  # reported by the correctness checker


def test_non_200_fails(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_er_data", lambda: (_ for _ in ()).throw(
        RuntimeError("ER status request failed")))

    assert smoke_test.check_recommendation(get, "v1") == ["status 503"]


def test_main_exit_code_and_output(monkeypatch, capsys):
    monkeypatch.setattr(smoke_test, "make_get", lambda url: get)

    assert smoke_test.main(["http://test"]) == 0
    output = capsys.readouterr().out
    assert "PASS health" in output and "PASS v1 recommendation" in output
    assert "v2" not in output  # v2 is only checked with --v2


def test_api_key_and_token_go_on_the_session(monkeypatch):
    monkeypatch.setenv(smoke_test.API_KEY_ENV_VAR, "secret-key")
    monkeypatch.setenv("ID_TOKEN", "token")
    sent = {}

    def fake_get(session, url, **kwargs):
        sent.update(session.headers)
        raise SystemExit  # stop before any network call

    monkeypatch.setattr(smoke_test.requests.Session, "get", fake_get)
    get = smoke_test.make_get("http://test")
    with pytest.raises(SystemExit):
        get("/v1/health")
    assert sent["X-API-Key"] == "secret-key"
    assert sent["Authorization"] == "Bearer token"
