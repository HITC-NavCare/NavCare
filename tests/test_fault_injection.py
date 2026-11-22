import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.agent_v2 import fault_injection
from app.api import facade
from app.api.main import app

client = TestClient(app)
MODE = fault_injection.FAULT_MODE_ENV_VAR
SECONDS = fault_injection.SLOW_SECONDS_ENV_VAR
MONTREAL = {"lat": 45.5017, "lon": -73.5673}
V1_RESULT = {"data_updated_at": "2026-11-13T12:00", "recommendations": []}
AGENT_RESULT = {
    "data_updated_at": "2026-11-13T12:00",
    "recommendations": [],
    "unranked_nearby": [],
    "call_911": False,
    "note": None,
    "model_usage": {"model": "gemini-test", "model_calls": 2},
}


@pytest.fixture(autouse=True)
def all_traffic_to_v1(monkeypatch):
    # A local .env may set these.
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")
    monkeypatch.setenv(facade.HEADER_ENABLED_ENV_VAR, "true")


@pytest.fixture
def sleeps(monkeypatch):
    """Fake wait: records the seconds instead of sleeping."""
    calls = []
    monkeypatch.setattr(fault_injection, "time", SimpleNamespace(sleep=calls.append))
    return calls


@pytest.fixture
def engines(monkeypatch, sleeps):
    """Fake v2 agent and v1 ranking. Records each call with the number of waits before it."""
    calls = []

    def run_agent(*args):
        calls.append(("agent", len(sleeps)))
        return AGENT_RESULT

    def build_recommendations(*args, **kwargs):
        calls.append(("v1", len(sleeps)))
        return dict(V1_RESULT)

    monkeypatch.setattr(orchestrator.agent, "run_agent", run_agent)
    monkeypatch.setattr(orchestrator, "build_recommendations", build_recommendations)
    return calls


def recommendation_events(caplog):
    return [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "recommendation"]


# Reading the settings


def test_off_when_unset():
    assert fault_injection.fault_mode() == "off"


@pytest.mark.parametrize("value", ["", "  ", "off", "OFF", " Off "])
def test_off_values(monkeypatch, value):
    monkeypatch.setenv(MODE, value)
    assert fault_injection.fault_mode() == "off"


@pytest.mark.parametrize(("value", "expected"), [
    ("error", "error"), (" ERROR ", "error"), ("slow", "slow"), ("Slow", "slow"),
])
def test_modes_ignore_case_and_spaces(monkeypatch, value, expected):
    monkeypatch.setenv(MODE, value)
    assert fault_injection.fault_mode() == expected


@pytest.mark.parametrize("value", ["eror", "true", "1", "error,slow"])
def test_unknown_mode_is_off_with_a_warning(monkeypatch, caplog, sleeps, value):
    monkeypatch.setenv(MODE, value)

    assert fault_injection.fault_mode() == "off"
    fault_injection.apply_fault()  # neither raises nor waits

    assert sleeps == []
    assert any(r.levelname == "WARNING" and MODE in r.getMessage() for r in caplog.records)


def test_slow_seconds_default():
    assert fault_injection.slow_seconds() == fault_injection.DEFAULT_SLOW_SECONDS


@pytest.mark.parametrize(("value", "expected"), [("5", 5.0), (" 0.5 ", 0.5), ("120", 120.0)])
def test_slow_seconds_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv(SECONDS, value)
    assert fault_injection.slow_seconds() == expected


@pytest.mark.parametrize("value", ["abc", "0", "-1", "120.5", "nan", "inf"])
def test_invalid_slow_seconds_use_the_default_with_a_warning(monkeypatch, caplog, value):
    monkeypatch.setenv(SECONDS, value)

    assert fault_injection.slow_seconds() == fault_injection.DEFAULT_SLOW_SECONDS
    assert any(r.levelname == "WARNING" and SECONDS in r.getMessage() for r in caplog.records)


# v2 path (orchestrator)


def test_disabled_v2_runs_normally(engines, sleeps):
    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever")

    assert result["engine"] == "v2"
    assert engines == [("agent", 0)]
    assert sleeps == []


def test_error_mode_uses_the_existing_fallback_without_calling_gemini(
    monkeypatch, engines, caplog
):
    monkeypatch.setenv(MODE, "error")

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever", radius_km=20)

    assert engines == [("v1", 0)]  # the agent (Gemini) was never called
    assert result == {
        **V1_RESULT,
        "engine": "v1_fallback",
        "fallback_reason": "injected fault (V2_FAULT_MODE=error)",
        "call_911": None,
        "note": orchestrator.FALLBACK_NOTE,
        "model_usage": None,
    }
    # Handled as an AgentError, not logged as a bug.
    assert not [r for r in caplog.records if r.name == "navcare.orchestrator"]


def test_slow_mode_waits_then_runs_v2_normally(monkeypatch, engines, sleeps):
    monkeypatch.setenv(MODE, "slow")
    monkeypatch.setenv(SECONDS, "7.5")

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever")

    assert sleeps == [7.5]
    assert engines == [("agent", 1)]  # waited first
    assert result["engine"] == "v2"
    assert result["model_usage"] == AGENT_RESULT["model_usage"]


def test_slow_mode_uses_the_default_wait(monkeypatch, engines, sleeps):
    monkeypatch.setenv(MODE, "slow")

    orchestrator.build_agent_recommendations(45.5, -73.6)

    assert sleeps == [fault_injection.DEFAULT_SLOW_SECONDS]


@pytest.mark.parametrize("mode", ["error", "slow"])
def test_v1_path_never_reads_the_fault(monkeypatch, engines, sleeps, mode):
    monkeypatch.setenv(MODE, mode)

    def must_not_run():
        raise AssertionError("v1 applied a v2 fault")

    monkeypatch.setattr(fault_injection, "apply_fault", must_not_run)

    orchestrator.build_recommendations(45.5, -73.6)

    assert engines == [("v1", 0)]
    assert sleeps == []


# Through the API


@pytest.mark.parametrize("mode", ["error", "slow"])
@pytest.mark.parametrize("headers", [{}, {"X-Engine": "v1"}], ids=["split_at_0", "header_v1"])
def test_v1_requests_are_unaffected(monkeypatch, engines, sleeps, caplog, mode, headers):
    caplog.set_level("INFO", logger="navcare")
    monkeypatch.setenv(MODE, mode)

    response = client.get("/v1/recommendations", params=MONTREAL, headers=headers)

    assert response.status_code == 200
    assert response.json()["engine"] == "v1"
    assert engines == [("v1", 0)]
    assert sleeps == []
    (event,) = recommendation_events(caplog)
    assert (event.levelname, event.fields["engine"]) == ("INFO", "v1")


def test_error_mode_v2_request_gets_the_v1_list_and_one_fallback_line(
    monkeypatch, engines, caplog
):
    monkeypatch.setenv(MODE, "error")

    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"})

    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "v1_fallback"
    assert "fallback_reason" not in body  # the reason is logged, not returned
    assert engines == [("v1", 0)]
    (event,) = recommendation_events(caplog)
    assert (event.levelname, event.fields["status"]) == ("WARNING", 200)
    assert event.fields["engine"] == "v1_fallback"
    assert "injected fault (V2_FAULT_MODE=error)" in event.getMessage()


def test_split_v2_request_is_also_faulted(monkeypatch, engines):
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "100")
    monkeypatch.setenv(MODE, "error")

    response = client.get("/v1/recommendations", params=MONTREAL)

    assert response.json()["engine"] == "v1_fallback"


def test_slow_mode_v2_request_answers_normally_after_the_wait(monkeypatch, engines, sleeps):
    monkeypatch.setenv(MODE, "slow")
    monkeypatch.setenv(SECONDS, "3")

    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"})

    assert response.status_code == 200
    assert response.json()["engine"] == "v2"
    assert sleeps == [3.0]
    assert engines == [("agent", 1)]


def test_a_request_cannot_turn_the_fault_on(engines, sleeps):
    response = client.get(
        "/v1/recommendations",
        params={**MONTREAL, "v2_fault_mode": "error", MODE: "error"},
        headers={"X-Engine": "v2", "V2-Fault-Mode": "error", MODE: "error"},
    )

    assert response.json()["engine"] == "v2"
    assert engines == [("agent", 0)]


def test_slow_v2_request_does_not_block_other_requests(monkeypatch, engines):
    # The sync endpoint runs in a worker thread, so a waiting v2 request leaves v1 served.
    monkeypatch.setenv(MODE, "slow")
    v2_waiting = threading.Event()
    release = threading.Event()

    def held_sleep(seconds):
        v2_waiting.set()
        release.wait(timeout=10)

    monkeypatch.setattr(fault_injection, "time", SimpleNamespace(sleep=held_sleep))
    url = "/v1/recommendations"

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            slow = asyncio.create_task(
                async_client.get(url, params=MONTREAL, headers={"X-Engine": "v2"})
            )
            assert await asyncio.to_thread(v2_waiting.wait, 10)
            fast = await asyncio.wait_for(
                async_client.get(url, params=MONTREAL, headers={"X-Engine": "v1"}), timeout=10
            )
            v2_still_waiting = not slow.done()
            release.set()
            return fast, v2_still_waiting, await slow

    try:
        fast, v2_still_waiting, slow = asyncio.run(scenario())
    finally:
        release.set()  # unblock the thread even if an assert failed

    assert fast.status_code == 200 and fast.json()["engine"] == "v1"
    assert v2_still_waiting
    assert slow.status_code == 200 and slow.json()["engine"] == "v2"
