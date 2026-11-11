import json
import runpy
from pathlib import Path

import pytest

import orchestrator
from tests.factories import make_er_data, make_hospital, make_status

MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
WEATHER_ROWS = [
    {"date": "2026-11-10 21:00:00+00:00", "precipitation": 0.0, "snowfall": 0.0},
    {"date": "2026-11-10 22:00:00+00:00", "precipitation": 2.0, "snowfall": 1.0},
]


@pytest.fixture
def fake_services(monkeypatch):
    """Replace the three web services. Records what the orchestrator asked for."""
    calls = {}

    def candidates(lat, lon, radius_km):
        calls["candidates"] = (lat, lon, radius_km)
        return [
            make_hospital("A", 5, name="HÔPITAL A"),
            make_hospital("B", 25),
            make_hospital("C", 8),
        ]

    def weather(lat, lon):
        calls["weather"] = (lat, lon)
        return WEATHER_ROWS

    monkeypatch.setattr(orchestrator, "get_candidate_hospitals", candidates)
    monkeypatch.setattr(
        orchestrator,
        "get_er_data",
        lambda: make_er_data({"A": make_status(125), "B": make_status(65)}),
    )
    monkeypatch.setattr(orchestrator, "get_weather_data", weather)
    return calls


def test_returns_full_response_json(fake_services):
    result = json.loads(orchestrator.get_recommended_hospitals(45.5, -73.6))

    assert set(result) == {
        "data_updated_at", "origin", "radius_km", "weather_now", "travel_time_multiplier",
        "recommendations", "unranked_nearby", "disclaimer",
    }
    assert result["data_updated_at"] == "2026-11-10T20:45:00.000Z"
    assert result["origin"] == {"lat": 45.5, "lon": -73.6}
    assert result["radius_km"] == 50
    assert result["disclaimer"] == "Not for life-threatening emergencies. Call 911."
    assert [r["hospital"]["id"] for r in result["recommendations"]] == ["B", "A"]
    assert result["unranked_nearby"] == [
        {"hospital": result["unranked_nearby"][0]["hospital"], "cause": "no_current_data"}
    ]
    assert result["unranked_nearby"][0]["hospital"]["id"] == "C"


def test_uses_current_hour_weather_at_user_location(fake_services):
    result = json.loads(orchestrator.get_recommended_hospitals(45.5, -73.6))

    assert fake_services["weather"] == (45.5, -73.6)
    assert result["weather_now"] == WEATHER_ROWS[0]  # not the later snowy hour
    assert result["travel_time_multiplier"] == 1.0


def test_passes_limit_and_radius(fake_services):
    result = json.loads(orchestrator.get_recommended_hospitals(45.5, -73.6, limit=1, radius_km=20))

    assert fake_services["candidates"] == (45.5, -73.6, 20)
    assert result["radius_km"] == 20
    assert len(result["recommendations"]) == 1


def test_keeps_french_accents_readable(fake_services):
    output = orchestrator.get_recommended_hospitals(45.5, -73.6)

    assert "HÔPITAL A" in output
    assert "Montréal" in output


def test_service_failure_is_raised(monkeypatch):
    def failing(*args, **kwargs):
        raise RuntimeError("Route request failed: timed out")

    monkeypatch.setattr(orchestrator, "get_candidate_hospitals", failing)

    with pytest.raises(RuntimeError, match="Route request failed"):
        orchestrator.get_recommended_hospitals(45.5, -73.6)


# --- main.py -------------------------------------------------------------------------

def test_main_prints_recommendations(monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "get_recommended_hospitals", lambda lat, lon: '{"ok": 1}')

    runpy.run_path(str(MAIN_PATH), run_name="__main__")

    assert capsys.readouterr().out.strip() == '{"ok": 1}'


def test_main_reports_service_failure(monkeypatch, capsys):
    def failing(lat, lon):
        raise RuntimeError("ER status request failed: 503")

    monkeypatch.setattr(orchestrator, "get_recommended_hospitals", failing)

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(MAIN_PATH), run_name="__main__")

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Could not get recommendations: ER status request failed: 503" in captured.err
