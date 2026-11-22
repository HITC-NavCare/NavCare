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


# --- v2 (AI agent) -------------------------------------------------------------------

AGENT_RESULT = {
    "data_updated_at": "2026-11-10T20:45:00.000Z",
    "radius_km": 50,
    "weather_now": None,
    "travel_time_multiplier": 1.0,
    "recommendations": [{"hospital": {"id": "A", "name": "HÔPITAL A"}, "rank": 1}],
    "unranked_nearby": [],
    "call_911": False,
    "note": None,
    "model_usage": {"model": "gemini-test", "model_calls": 3},
}


def test_agent_response_adds_engine_origin_and_disclaimer(monkeypatch):
    calls = []

    def run_agent(lat, lon, message, limit, started):
        calls.append((lat, lon, message, limit, started))
        return AGENT_RESULT

    monkeypatch.setattr(orchestrator.agent, "run_agent", run_agent)

    output = orchestrator.get_agent_recommended_hospitals(45.5, -73.6, "fever", limit=2)
    result = json.loads(output)

    assert calls == [(45.5, -73.6, "fever", 2, None)]  # no fault: the agent's own limit
    assert result == {
        "engine": "v2",
        "origin": {"lat": 45.5, "lon": -73.6},
        **AGENT_RESULT,
        "disclaimer": orchestrator.DISCLAIMER,
    }
    assert "HÔPITAL A" in output


def test_build_agent_recommendations_returns_a_dict_for_the_api(monkeypatch):
    monkeypatch.setattr(orchestrator.agent, "run_agent", lambda *args, **kwargs: AGENT_RESULT)

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever")

    assert result["engine"] == "v2"
    assert result == json.loads(orchestrator.get_agent_recommended_hospitals(45.5, -73.6, "fever"))


def test_agent_response_has_every_v1_field(fake_services, monkeypatch):
    monkeypatch.setattr(orchestrator.agent, "run_agent", lambda *args, **kwargs: AGENT_RESULT)

    v1 = json.loads(orchestrator.get_recommended_hospitals(45.5, -73.6))
    v2 = json.loads(orchestrator.get_agent_recommended_hospitals(45.5, -73.6))

    assert set(v1) <= set(v2)


def test_safety_net_raises_call_911_when_the_model_did_not(monkeypatch):
    monkeypatch.setattr(orchestrator.agent, "run_agent", lambda *args, **kwargs: AGENT_RESULT)

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "chest pain since an hour")

    assert AGENT_RESULT["call_911"] is False
    assert result["call_911"] is True


def test_safety_net_never_lowers_call_911(monkeypatch):
    monkeypatch.setattr(orchestrator.agent, "run_agent",
                        lambda *args, **kwargs: {**AGENT_RESULT, "call_911": True})

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "my wrist hurts a lot")

    assert result["call_911"] is True


@pytest.mark.parametrize(
    ("message", "expected"), [("il ne respire plus", True), ("fever", None), (None, None)]
)
def test_fallback_call_911_comes_from_the_safety_net(fake_services, monkeypatch, message,
                                                      expected):
    def failing(*args, **kwargs):
        raise orchestrator.agent.AgentError("Gemini request failed: 503")

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = orchestrator.build_agent_recommendations(45.5, -73.6, message)

    assert result["call_911"] is expected


def test_agent_failure_falls_back_to_v1_ranking(fake_services, monkeypatch):
    def failing(*args, **kwargs):
        raise orchestrator.agent.AgentError("Gemini request failed: 503")

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = json.loads(orchestrator.get_agent_recommended_hospitals(45.5, -73.6, "fever", 1))
    v1 = json.loads(orchestrator.get_recommended_hospitals(45.5, -73.6, limit=1))

    assert result == {
        **v1,
        "engine": "v1_fallback",
        "fallback_reason": "Gemini request failed: 503",
        "call_911": None,
        "note": orchestrator.FALLBACK_NOTE,  # the message was not read, so say so
        "model_usage": None,
    }
    assert [r["hospital"]["id"] for r in result["recommendations"]] == ["B"]


def test_agent_fallback_uses_the_requested_radius(fake_services, monkeypatch):
    def failing(*args, **kwargs):
        raise orchestrator.agent.AgentError("Gemini request failed: 503")

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = orchestrator.build_agent_recommendations(45.5, -73.6, radius_km=20)

    assert result["radius_km"] == 20


def test_agent_fallback_keeps_the_model_usage_so_far(fake_services, monkeypatch):
    usage = {"model": "gemini-test", "model_calls": 2, "tool_calls": 1}

    def failing(*args, **kwargs):
        error = orchestrator.agent.AgentError("Gemini request failed: 503")
        error.model_usage = usage
        raise error

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = orchestrator.build_agent_recommendations(45.5, -73.6)

    assert result["engine"] == "v1_fallback"
    assert result["model_usage"] == usage


@pytest.mark.parametrize("bug", [KeyError("hospital"), TypeError("bad args"), RuntimeError("x")])
def test_unexpected_agent_error_falls_back_to_v1(fake_services, monkeypatch, caplog, bug):
    # A bug in v2 must not leave the person without a hospital list.
    def failing(*args, **kwargs):
        bug.model_usage = {"model": "gemini-test", "model_calls": 1, "tool_calls": 0}
        raise bug

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "il ne respire plus")

    assert result["engine"] == "v1_fallback"
    assert result["fallback_reason"].startswith(f"unexpected {type(bug).__name__} in v2: ")
    assert [r["hospital"]["id"] for r in result["recommendations"]]
    assert result["call_911"] is True  # the safety net still works
    assert result["note"] == orchestrator.FALLBACK_NOTE
    assert result["model_usage"]["model_calls"] == 1
    # The bug's stack trace is logged, so it is not hidden by the fallback.
    [record] = [r for r in caplog.records if r.name == "navcare.orchestrator"]
    assert record.exc_info[1] is bug


def test_unexpected_agent_error_without_usage_still_falls_back(fake_services, monkeypatch):
    def failing(*args, **kwargs):
        raise ValueError("no usage attached")

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = orchestrator.build_agent_recommendations(45.5, -73.6)

    assert result["engine"] == "v1_fallback"
    assert result["model_usage"] is None


@pytest.mark.parametrize("message", [None, "", "   "])
def test_agent_fallback_has_no_note_without_a_message(fake_services, monkeypatch, message):
    def failing(*args, **kwargs):
        raise orchestrator.agent.AgentError("Gemini request failed: 503")

    monkeypatch.setattr(orchestrator.agent, "run_agent", failing)

    result = json.loads(orchestrator.get_agent_recommended_hospitals(45.5, -73.6, message))

    assert result["note"] is None


def test_agent_fallback_raises_when_services_fail_too(monkeypatch):
    def agent_failing(*args, **kwargs):
        raise orchestrator.agent.AgentError("Gemini request failed: 503")

    def service_failing(*args, **kwargs):
        raise RuntimeError("Route request failed: timed out")

    monkeypatch.setattr(orchestrator.agent, "run_agent", agent_failing)
    monkeypatch.setattr(orchestrator, "get_candidate_hospitals", service_failing)

    with pytest.raises(RuntimeError, match="Route request failed"):
        orchestrator.get_agent_recommended_hospitals(45.5, -73.6)


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


@pytest.mark.parametrize(
    ("argv", "expected_message"),
    [
        (["main.py", "v2", "my", "ankle", "hurts"], "my ankle hurts"),
        (["main.py", "v2"], None),
    ],
)
def test_main_v2_passes_the_message_to_the_agent(monkeypatch, capsys, argv, expected_message):
    calls = []

    def agent_recommendations(lat, lon, message):
        calls.append(message)
        return '{"engine": "v2"}'

    monkeypatch.setattr(orchestrator, "get_agent_recommended_hospitals", agent_recommendations)
    monkeypatch.setattr("sys.argv", argv)

    runpy.run_path(str(MAIN_PATH), run_name="__main__")

    assert calls == [expected_message]
    assert capsys.readouterr().out.strip() == '{"engine": "v2"}'
