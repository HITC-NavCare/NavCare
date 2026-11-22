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
from tests.factories import make_er_data, make_hospital, make_status

client = TestClient(app)
ENABLED = fault_injection.ENABLED_ENV_VAR
MODE = fault_injection.FAULT_MODE_ENV_VAR
SECONDS = fault_injection.SLOW_SECONDS_ENV_VAR
MONTREAL = {"lat": 45.5017, "lon": -73.5673}
CLOCK_START = 1000.0
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


def turn_on(monkeypatch, mode, seconds=None):
    monkeypatch.setenv(ENABLED, "true")
    monkeypatch.setenv(MODE, mode)
    if seconds is not None:
        monkeypatch.setenv(SECONDS, seconds)


@pytest.fixture
def sleeps(monkeypatch):
    """Fake clock: records each wait and moves the time forward instead of sleeping."""
    calls = []
    clock = SimpleNamespace(now=CLOCK_START)

    def sleep(seconds):
        calls.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(fault_injection, "time",
                        SimpleNamespace(sleep=sleep, monotonic=lambda: clock.now))
    return calls


@pytest.fixture
def engines(monkeypatch, sleeps):
    """Fake v2 agent and v1 ranking. Records each call, the waits before it and `started`."""
    calls = []

    def run_agent(*args, started=None, **kwargs):
        calls.append(("agent", len(sleeps), started))
        return AGENT_RESULT

    def build_recommendations(*args, **kwargs):
        calls.append(("v1", len(sleeps), None))
        return dict(V1_RESULT)

    monkeypatch.setattr(orchestrator.agent, "run_agent", run_agent)
    monkeypatch.setattr(orchestrator, "build_recommendations", build_recommendations)
    return calls


def recommendation_events(caplog):
    return [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "recommendation"]


def fault_logs(caplog):
    return [r for r in caplog.records if r.name == "navcare.fault_injection"]


# Reading the settings


def test_off_by_default():
    assert not fault_injection.faults_enabled()
    assert fault_injection.fault_mode() == "off"


@pytest.mark.parametrize("value", ["", "  ", "off", "OFF", " Off "])
def test_off_values(monkeypatch, caplog, value):
    turn_on(monkeypatch, value)

    assert fault_injection.fault_mode() == "off"
    assert fault_logs(caplog) == []


@pytest.mark.parametrize(("value", "expected"), [
    ("error", "error"), (" ERROR ", "error"), ("slow", "slow"), ("Slow", "slow"),
])
def test_modes_ignore_case_and_spaces(monkeypatch, value, expected):
    turn_on(monkeypatch, value)
    assert fault_injection.fault_mode() == expected


@pytest.mark.parametrize("enabled", [None, "", "false", "0", "yes", "on"])
def test_a_mode_without_the_enable_flag_is_off(monkeypatch, caplog, sleeps, enabled):
    monkeypatch.setenv(MODE, "error")
    if enabled is not None:
        monkeypatch.setenv(ENABLED, enabled)

    assert fault_injection.fault_mode() == "off"
    assert fault_injection.apply_fault() is None  # neither raises nor waits
    assert sleeps == []
    (record,) = fault_logs(caplog)
    assert ENABLED in record.getMessage()


@pytest.mark.parametrize("enabled", ["true", " TRUE ", "1"])
def test_enable_flag_values(monkeypatch, enabled):
    monkeypatch.setenv(ENABLED, enabled)
    monkeypatch.setenv(MODE, "slow")
    assert fault_injection.fault_mode() == "slow"


def test_the_enable_flag_alone_does_nothing(monkeypatch, caplog, sleeps):
    monkeypatch.setenv(ENABLED, "true")

    assert fault_injection.apply_fault() is None
    assert sleeps == []
    assert fault_logs(caplog) == []


@pytest.mark.parametrize("value", ["eror", "true", "1", "error,slow"])
def test_unknown_mode_is_off_with_a_warning(monkeypatch, caplog, sleeps, value):
    turn_on(monkeypatch, value)

    assert fault_injection.fault_mode() == "off"
    assert fault_injection.apply_fault() is None
    assert sleeps == []
    (record,) = fault_logs(caplog)
    assert record.levelname == "WARNING" and MODE in record.getMessage()


def test_a_bad_setting_is_logged_once_and_a_changed_one_again(monkeypatch, caplog):
    turn_on(monkeypatch, "eror")
    for _ in range(3):
        fault_injection.fault_mode()
    monkeypatch.setenv(MODE, "erorr")
    for _ in range(3):
        fault_injection.fault_mode()

    assert ["'eror'" in r.getMessage() for r in fault_logs(caplog)] == [True, False]


def test_slow_seconds_default():
    assert fault_injection.slow_seconds() == fault_injection.DEFAULT_SLOW_SECONDS


@pytest.mark.parametrize(("value", "expected"), [("5", 5.0), (" 0.5 ", 0.5), ("60", 60.0)])
def test_slow_seconds_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv(SECONDS, value)
    assert fault_injection.slow_seconds() == expected


@pytest.mark.parametrize("value", ["abc", "0", "-1", "60.5", "120", "nan", "inf"])
def test_invalid_slow_seconds_use_the_default_with_one_warning(monkeypatch, caplog, value):
    monkeypatch.setenv(SECONDS, value)

    for _ in range(3):
        assert fault_injection.slow_seconds() == fault_injection.DEFAULT_SLOW_SECONDS
    (record,) = fault_logs(caplog)
    assert SECONDS in record.getMessage()


def test_max_wait_is_the_agents_time_limit():
    assert fault_injection.MAX_SLOW_SECONDS == orchestrator.agent.MAX_TOTAL_SECONDS


# v2 path (orchestrator)


def test_disabled_v2_runs_normally(engines, sleeps):
    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever")

    assert result["engine"] == "v2"
    assert engines == [("agent", 0, None)]  # None: the agent's limit starts as usual
    assert sleeps == []


def test_error_mode_uses_the_existing_fallback_without_calling_gemini(
    monkeypatch, engines, caplog
):
    turn_on(monkeypatch, "error")

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever", radius_km=20)

    assert engines == [("v1", 0, None)]  # the agent (Gemini) was never called
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


def test_slow_mode_wait_counts_against_the_agents_time_limit(monkeypatch, engines, sleeps):
    turn_on(monkeypatch, "slow", "7.5")

    result = orchestrator.build_agent_recommendations(45.5, -73.6, "fever")

    assert sleeps == [7.5]
    # The agent ran after the wait, with its limit starting when the wait began.
    assert engines == [("agent", 1, CLOCK_START)]
    assert result["engine"] == "v2"
    assert result["model_usage"] == AGENT_RESULT["model_usage"]


def test_slow_mode_uses_the_default_wait(monkeypatch, engines, sleeps):
    turn_on(monkeypatch, "slow")

    orchestrator.build_agent_recommendations(45.5, -73.6)

    assert sleeps == [fault_injection.DEFAULT_SLOW_SECONDS]


@pytest.mark.parametrize("mode", ["error", "slow"])
def test_an_active_fault_is_logged_once_not_per_request(monkeypatch, engines, caplog, mode):
    turn_on(monkeypatch, mode)

    for _ in range(3):
        orchestrator.build_agent_recommendations(45.5, -73.6)

    (record,) = fault_logs(caplog)
    assert f"{MODE}={mode}" in record.getMessage()


# Through the API


@pytest.mark.parametrize("headers", [{}, {"X-Engine": "v1"}], ids=["split_at_0", "header_v1"])
def test_v1_requests_never_read_fault_settings(monkeypatch, sleeps, headers):
    turn_on(monkeypatch, "error")
    reads = []

    def forbidden(name):
        def stub(*args, **kwargs):
            reads.append(name)
            raise AssertionError(f"v1 request used fault_injection.{name}")
        return stub

    for name in ("apply_fault", "fault_mode", "slow_seconds", "faults_enabled"):
        monkeypatch.setattr(fault_injection, name, forbidden(name))
    # Also catches any other read of the environment from the fault module.
    monkeypatch.setattr(fault_injection, "os",
                        SimpleNamespace(environ=SimpleNamespace(get=forbidden("os.environ.get"))))
    # The real v1 path, with only the data services faked.
    monkeypatch.setattr(orchestrator, "get_candidate_hospitals",
                        lambda lat, lon, radius_km: [make_hospital("A", 5)])
    monkeypatch.setattr(orchestrator, "get_er_data", lambda: make_er_data({"A": make_status()}))
    monkeypatch.setattr(orchestrator, "get_weather_data", lambda lat, lon: [
        {"date": "2026-11-10 21:00:00+00:00", "precipitation": 0.0, "snowfall": 0.0},
    ])

    response = client.get("/v1/recommendations", params=MONTREAL, headers=headers)

    assert reads == []
    assert response.status_code == 200
    assert response.json()["engine"] == "v1"
    assert sleeps == []


@pytest.mark.parametrize("mode", ["error", "slow"])
@pytest.mark.parametrize("headers", [{}, {"X-Engine": "v1"}], ids=["split_at_0", "header_v1"])
def test_v1_requests_are_unaffected(monkeypatch, engines, sleeps, caplog, mode, headers):
    caplog.set_level("INFO", logger="navcare")
    turn_on(monkeypatch, mode)

    response = client.get("/v1/recommendations", params=MONTREAL, headers=headers)

    assert response.status_code == 200
    assert response.json()["engine"] == "v1"
    assert engines == [("v1", 0, None)]
    assert sleeps == []
    (event,) = recommendation_events(caplog)
    assert (event.levelname, event.fields["engine"]) == ("INFO", "v1")


def test_error_mode_v2_request_gets_the_v1_list_and_one_fallback_line(
    monkeypatch, engines, caplog
):
    turn_on(monkeypatch, "error")

    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"})

    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "v1_fallback"
    assert "fallback_reason" not in body  # the reason is logged, not returned
    assert engines == [("v1", 0, None)]
    (event,) = recommendation_events(caplog)
    assert (event.levelname, event.fields["status"]) == ("WARNING", 200)
    assert event.fields["engine"] == "v1_fallback"
    assert "injected fault (V2_FAULT_MODE=error)" in event.getMessage()


def test_split_v2_request_is_also_faulted(monkeypatch, engines):
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "100")
    turn_on(monkeypatch, "error")

    response = client.get("/v1/recommendations", params=MONTREAL)

    assert response.json()["engine"] == "v1_fallback"


def test_slow_mode_v2_request_answers_normally_after_the_wait(monkeypatch, engines, sleeps):
    turn_on(monkeypatch, "slow", "3")

    response = client.get("/v1/recommendations", params=MONTREAL, headers={"X-Engine": "v2"})

    assert response.status_code == 200
    assert response.json()["engine"] == "v2"
    assert sleeps == [3.0]
    assert engines == [("agent", 1, CLOCK_START)]


def test_a_request_cannot_turn_the_fault_on(engines, sleeps):
    response = client.get(
        "/v1/recommendations",
        params={**MONTREAL, "v2_fault_mode": "error", MODE: "error", ENABLED: "true"},
        headers={"X-Engine": "v2", "V2-Fault-Mode": "error", MODE: "error", ENABLED: "true"},
    )

    assert response.json()["engine"] == "v2"
    assert engines == [("agent", 0, None)]


def test_slow_v2_request_does_not_block_other_requests(monkeypatch, engines):
    # The sync endpoint runs in a worker thread, so a waiting v2 request leaves v1 served.
    turn_on(monkeypatch, "slow")
    v2_waiting = threading.Event()
    release = threading.Event()

    def held_sleep(seconds):
        v2_waiting.set()
        release.wait(timeout=10)

    monkeypatch.setattr(fault_injection, "time",
                        SimpleNamespace(sleep=held_sleep, monotonic=lambda: CLOCK_START))
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
