"""Tests for the load test (loadtest/run.py). No real service is called: requests go to the
API in memory (with routing, ER status and weather faked) or to a fake transport."""

import json

import httpx
import pytest

import orchestrator
from app import red_flags
from app.agent_v2 import ai_agent_recommender as agent
from app.api import facade
from app.api.main import app
from app.quality import correctness
from app.services import nearest_hospital_getter
from loadtest import run
from tests import test_correctness as samples
from tests.factories import make_er_data, make_status


def args_for(tmp_path, *extra):
    """Fast settings: requests start every millisecond."""
    return ["--url", "http://localhost:8000", "--rate", "60000", "--duration", "1",
            "--out", str(tmp_path / "results.jsonl"), *extra]


def read_results(tmp_path):
    """The saved results in the order they were sent (the file is in the order they arrived)."""
    lines = (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return sorted((json.loads(line) for line in lines), key=lambda record: record["index"])


def fake_api(answer):
    """A transport that answers every request with answer(request) -> (status, body)."""
    calls = []

    def handler(request):
        calls.append(request)
        status, body = answer(request)
        return httpx.Response(status, json=body, headers={"X-Request-ID": f"id-{len(calls)}"})

    return httpx.MockTransport(handler), calls


def no_calls(request):
    raise AssertionError(f"the run should not have sent {request.url}")


# Scenarios


def test_scenarios_cover_every_place_and_message_and_repeat():
    names = run.scenarios()
    first = [next(names) for _ in range(len(run.LOCATIONS) * len(run.MESSAGES))]
    assert len({name for name, _, _ in first}) == 25
    assert next(names) == first[0]  # then it starts again


def test_scenarios_are_valid_requests():
    for lat, lon in run.LOCATIONS.values():
        assert 44.9 <= lat <= 62.6 and -79.8 <= lon <= -57.1  # the API's Québec bounds
    assert run.MESSAGES["no-message"] is None
    assert red_flags.mentions_emergency(run.MESSAGES["chest-pain-en"])
    others = [m for label, m in run.MESSAGES.items() if m and label != "chest-pain-en"]
    assert not any(red_flags.mentions_emergency(message) for message in others)


# Plan and safety


@pytest.mark.parametrize("extra, total", [
    ([], 20),  # capped by the default --max-requests
    (["--max-requests", "5"], 5),
    (["--rate", "6", "--duration", "60"], 6),  # rate x duration is smaller
    (["--rate", "1", "--duration", "30"], 0),
    (["--rate", "8.2", "--duration", "900", "--max-requests", "500"], 123),  # not 122
])
def test_plan_is_the_smaller_of_the_cap_and_rate_times_duration(tmp_path, extra, total):
    assert run.plan(run.parse_args(args_for(tmp_path)[:4] + extra)) == (total, 0)


@pytest.mark.parametrize("extra, comparisons", [
    (["--engine", "v2"], 4),  # 20 requests: 4 of the 5 places with no message
    (["--engine", "v2", "--no-compare"], 0),
    (["--engine", "split"], 0),  # off by default: production ignores X-Engine
    (["--engine", "split", "--compare"], 4),
    (["--engine", "v1", "--compare"], 0),  # v1 answers never need comparing
])
def test_plan_counts_the_v1_comparison_requests(tmp_path, extra, comparisons):
    assert run.plan(run.parse_args(args_for(tmp_path, "--max-requests", "20", *extra))) == (
        20, comparisons)


def test_comparison_requests_count_towards_needing_yes(tmp_path, capsys):
    transport = httpx.MockTransport(no_calls)
    # 20 load requests are allowed without --yes, but with v2 they come with 4 comparisons
    assert run.main(args_for(tmp_path, "--engine", "v2"), transport=transport) == 2
    assert "plus up to 4 v1 requests" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [
    ["--max-requests", "21"],
    ["--url", "https://navcare-abc.a.run.app"],
])
def test_big_or_remote_runs_need_yes(tmp_path, capsys, extra):
    transport = httpx.MockTransport(no_calls)
    assert run.main(args_for(tmp_path, *extra), transport=transport) == 2
    assert "add --yes" in capsys.readouterr().err
    assert not (tmp_path / "results.jsonl").exists()


def test_dry_run_sends_nothing(tmp_path, capsys):
    transport = httpx.MockTransport(no_calls)
    assert run.main(args_for(tmp_path, "--dry-run", "--max-requests", "500"),
                    transport=transport) == 0
    output = capsys.readouterr().out
    assert "Plan: 500 requests to http://localhost:8000" in output
    assert f"up to {nearest_hospital_getter.MAX_CANDIDATES} origin-hospital pairs" in output


@pytest.mark.parametrize("option, value", [
    ("--rate", "0"), ("--rate", "-5"), ("--rate", "nan"), ("--rate", "inf"),
    ("--radius-km", "0.5"), ("--radius-km", "500"), ("--radius-km", "nan"),
    ("--min-percent", "95%"), ("--min-percent", "101"), ("--min-percent", "nan"),
])
def test_bad_options_are_rejected_before_anything_is_sent(tmp_path, option, value):
    transport = httpx.MockTransport(no_calls)
    with pytest.raises(SystemExit):
        run.main(args_for(tmp_path, option, value, "--max-requests", "1"), transport=transport)


# What is recorded


def test_v2_answers_without_a_message_are_compared_with_v1(tmp_path):
    def answer(request):
        engine = request.headers.get("X-Engine", "v2")
        return 200, {"engine": engine, "recommendations": []}

    transport, calls = fake_api(answer)
    # On by default with --engine v2
    assert run.main(args_for(tmp_path, "--engine", "v2", "--max-requests", "5", "--no-check"),
                    transport=transport) == 0
    records = read_results(tmp_path)
    assert [r["scenario"] for r in records] == [
        "montreal/no-message", "montreal/child-fever-fr", "montreal/mental-health-en",
        "montreal/chest-pain-en", "montreal/ankle-en",
    ]
    first = records[0]
    assert first["request"] == {"lat": 45.5017, "lon": -73.5673, "limit": None,
                                "radius_km": None, "message": None}
    assert first["engine"] == "v2" and first["served_engine"] == "v2"
    assert first["v1_status"] == 200
    assert first["v1_response"]["engine"] == "v1"
    assert all("v1_response" not in r for r in records[1:])  # they have a message
    assert len(calls) == 6  # 5 requests + 1 comparison
    # Unset values are not sent, so the API's defaults apply
    assert dict(calls[0].url.params) == {"lat": "45.5017", "lon": "-73.5673"}


def test_split_sends_no_header_records_no_forced_engine_and_does_not_compare(tmp_path):
    transport, calls = fake_api(lambda request: (200, {"engine": "v2"}))
    run.main(args_for(tmp_path, "--max-requests", "2", "--no-check"), transport=transport)
    assert len(calls) == 2  # no comparison request, though the first has no message
    assert all("X-Engine" not in call.headers for call in calls)
    assert all("engine" not in record for record in read_results(tmp_path))


def test_timeouts_and_errors_are_recorded_as_non_200(tmp_path, capsys):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("too slow", request=request)
        if count == 2:
            return httpx.Response(503, json={"error": {"code": "data_unavailable"}})
        if count == 3:
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(200, json={"engine": "v2"}, headers={"X-Engine-Served": "v2"})

    result = run.main(args_for(tmp_path, "--engine", "v2", "--max-requests", "4", "--no-check",
                               "--no-compare"), transport=httpx.MockTransport(handler))
    assert result == 0
    records = read_results(tmp_path)
    assert [r["status"] for r in records] == [0, 503, 200, 200]
    assert records[0]["error"].startswith("ReadTimeout")
    assert records[0]["failure"] == "timeout"
    assert records[0]["response"] is None and records[0]["engine"] == "v2"
    assert records[2]["error"] == "the response is not JSON"
    assert records[2]["failure"] == "bad_body"
    output = capsys.readouterr().out
    # The 200 that is not JSON has no engine: counted under the forced engine, as a failure
    assert ("v2: 4 requests, 3 failed (75.0%: 1 non-200, 1 timeouts, 0 connection errors, "
            "1 200s that are not JSON)") in output


def test_connection_errors_are_not_called_timeouts(tmp_path, capsys):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    run.main(args_for(tmp_path, "--max-requests", "2", "--no-check"),
             transport=httpx.MockTransport(handler))
    assert all(r["failure"] == "connection" for r in read_results(tmp_path))
    assert ("unknown: 2 requests, 2 failed (100.0%: 0 non-200, 0 timeouts, "
            "2 connection errors") in capsys.readouterr().out


def test_results_are_saved_as_they_arrive_and_kept_when_interrupted(tmp_path, capsys,
                                                                     monkeypatch):
    async def stopped_by_ctrl_c(args, total, on_result, transport=None):
        for index in range(2):
            on_result({"index": index, "scenario": "x", "status": 200, "latency_ms": 10,
                       "served_engine": "v1", "response": {}})
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "run", stopped_by_ctrl_c)
    assert run.main(args_for(tmp_path, "--max-requests", "5")) == 1
    assert len(read_results(tmp_path)) == 2
    output = capsys.readouterr().out
    assert "Interrupted: saved 2 of 5 results" in output
    assert "no correctness verdict" in output


def test_a_bug_in_one_request_does_not_lose_the_others(tmp_path, capsys):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("unexpected")  # not an httpx error
        return httpx.Response(200, json={"engine": "v1"})

    run.main(args_for(tmp_path, "--max-requests", "3", "--no-check"),
             transport=httpx.MockTransport(handler))
    assert len(read_results(tmp_path)) == 2
    output = capsys.readouterr()
    assert "failed in the load test itself: RuntimeError: unexpected" in output.err
    assert "1 requests failed in the load test itself" in output.out


def test_served_engine_header_wins_over_the_body():
    response = httpx.Response(200, headers={"X-Engine-Served": "v2"})
    assert run.served_engine(response, {"engine": "v1"}) == "v2"
    assert run.served_engine(httpx.Response(200), {"engine": "v1_fallback"}) == "v1_fallback"
    assert run.served_engine(httpx.Response(200), {"engine": 3}) is None
    assert run.served_engine(httpx.Response(200), None) is None


def test_warns_when_the_server_ignores_x_engine(tmp_path, capsys):
    transport, _ = fake_api(lambda request: (200, {"engine": "v2"}))  # header ignored
    run.main(args_for(tmp_path, "--engine", "v1", "--max-requests", "1", "--no-check"),
             transport=transport)
    assert "the server ignores X-Engine" in capsys.readouterr().out

    transport, _ = fake_api(lambda request: (200, {"engine": "v2"}))
    run.main(args_for(tmp_path, "--max-requests", "1", "--no-check", "--compare"),
             transport=transport)
    assert "v2 could not be compared with v1" in capsys.readouterr().out


# Summary


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert run.percentile(values, 50) == 50
    assert run.percentile(values, 95) == 95
    assert run.percentile([7], 95) == 7
    assert run.percentile([], 95) is None


def test_summary_counts_200s_under_the_engine_that_answered():
    def record(status, served, forced=None, latency=100, failure=None):
        base = {"status": status, "served_engine": served, "latency_ms": latency}
        if failure:
            base["failure"] = failure
        return {**base, "engine": forced} if forced else base

    summary = run.summarise([
        record(200, "v2", "v2", 1000), record(200, "v1_fallback", "v2", 3000),
        record(503, None, "v2", 50), record(0, None, "v2", 90000, "timeout"),
        record(200, "v1"), record(500, None),
    ])
    assert summary["v2"] == {"requests": 3, "failed": 2, "error_rate_percent": 66.7,
                             "non_200": 1, "timeouts": 1, "connection_errors": 0,
                             "bad_bodies": 0, "p50_ms": 1000, "p95_ms": 90000}
    assert summary["v1_fallback"]["requests"] == 1
    assert summary["v1"]["requests"] == 1
    assert summary["unknown"]["non_200"] == 1  # split: the error body has no engine


# End to end: the real API in memory, then the correctness checker on the results


@pytest.fixture
def local_api(monkeypatch):
    """The real app with driving times, ER status and weather faked for every hospital."""
    def drive_times(lat, lon, destinations):
        return [
            {"duration_min": 3 + km * 1.5, "distance_km": km * 1.3}
            for km in (nearest_hospital_getter.straight_line_km(lat, lon, *destination)
                       for destination in destinations)
        ]

    monkeypatch.setattr(nearest_hospital_getter, "get_drive_times", drive_times)
    ids = list(correctness.load_hospitals())
    er_data = make_er_data({
        hospital_id: make_status(occupancy_pct=60 + 5 * (i % 10), stay_hours=2 + 0.3 * (i % 5))
        for i, hospital_id in enumerate(ids)
    })
    for module in (orchestrator, agent):
        monkeypatch.setattr(module, "get_er_data", lambda: er_data)
        monkeypatch.setattr(module, "get_weather_data", lambda lat, lon: samples.WEATHER_ROWS)
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")
    monkeypatch.setenv(facade.HEADER_ENABLED_ENV_VAR, "true")
    return httpx.ASGITransport(app=app)


def test_every_scenario_through_the_real_v1_api_passes_the_checker(tmp_path, capsys, local_api):
    exit_code = run.main(args_for(tmp_path, "--engine", "v1", "--max-requests", "25"),
                         transport=local_api)
    # More than 20 requests needs --yes, even against a local server (the services cost money)
    output = capsys.readouterr()
    assert "add --yes" in output.err
    assert exit_code == 2

    exit_code = run.main(args_for(tmp_path, "--engine", "v1", "--max-requests", "25", "--yes"),
                         transport=local_api)
    output = capsys.readouterr().out
    assert exit_code == 0, output
    records = read_results(tmp_path)
    assert len(records) == 25
    assert all(record["status"] == 200 for record in records)
    assert {record["served_engine"] for record in records} == {"v1"}
    assert all(record["request_id"] for record in records)
    chest_pain = [r for r in records if r["scenario"].endswith("chest-pain-en")]
    assert all(r["response"]["call_911"] is True for r in chest_pain)  # the safety net
    assert "v1: 25 requests, 0 failed (0.0%" in output
    assert "v1: 25/25 correct (100.0%), 0 non-200 not checked" in output
