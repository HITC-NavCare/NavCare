"""Tests for the canary step check (loadtest/step_check.py). Offline: made-up results and logs."""

import json
import logging
from itertools import count

import pytest

from app.agent_v2 import fault_injection
from app.api import facade
from app.api.logs import LOGGER_NAME, JsonFormatter
from loadtest import run, step_check
from loadtest.step_check import FAIL, HOLD, NOT_APPLICABLE, NOT_CHECKED, PASS, PROMOTE, ROLLBACK
from tests import test_correctness as samples
from tests.test_load_test import local_api  # noqa: F401 (a fixture)

IDS = count()
WRONG = {"expected_minutes": 999}  # breaks the first recommendation (the checker finds it)


def request_result(engine="v1", status=200, latency=1000, wrong=False, cost=0.01,
                   chosen_by="split", logged=True):
    """One load-test result and its log line (None when there is no answer or no line)."""
    request_id = f"00000000-0000-4000-8000-{next(IDS):012d}" if status else None
    if status == 200:
        body = samples.response(engine)
        if wrong:
            body["recommendations"][0].update(WRONG)
    else:
        body = {"error": {"code": "x", "message": "x", "request_id": request_id}} if status \
            else None
    record = {"index": 0, "scenario": "Montréal, no message",
              "started_at": "2026-11-24T20:00:00.000+00:00", "request": dict(samples.REQUEST),
              "status": status, "response": body, "latency_ms": latency,
              "request_id": request_id, "served_engine": engine if status == 200 else None}
    if not (status and logged):
        return record, None
    line = {"severity": "INFO", "message": "recommendation", "event": "recommendation",
            "request_id": request_id, "status": status, "engine": engine,
            "chosen_by": chosen_by, "latency_ms": latency, "model_usage": None}
    if engine in ("v2", "v1_fallback"):
        if cost is None:
            line["model_cost_unknown"] = "no model usage recorded"
        else:
            line["model_cost_usd"] = cost
    return record, line


def step(*groups):
    """groups: (how many, keyword arguments for request_result). Returns (records, logs)."""
    records, logs = [], {}
    for number, options in groups:
        for _ in range(number):
            record, line = request_result(**options)
            records.append(record)
            if line:
                logs[line["request_id"]] = line
    return records, logs


def healthy(v1=108, v2=12, **v2_options):
    return step((v1, {}), (v2, {"engine": "v2", "latency": 16_000, **v2_options}))


BASELINE, _ = step((120, {}))


def check(records, logs, percent=10, baseline=BASELINE):
    result = step_check.evaluate(records, percent, logs, baseline)
    return result, {item["number"]: item for item in result["criteria"]}


def test_a_healthy_step_is_promoted_to_the_next_one():
    result, by_number = check(*healthy())

    assert [by_number[n]["status"] for n in range(1, 7)] == [PASS] * 6
    assert by_number[7]["status"] == step_check.BY_HAND
    assert result["decision"] == PROMOTE and result["reasons"] == []
    assert result["v2_requests"] == 12
    assert step_check.action_line(result).startswith("deployment/canary.sh set 25")


def test_fault_injection_error_mode_is_a_rollback():
    # V2_FAULT_MODE=error: every v2 request falls back to v1 and records no model usage.
    records, logs = step((108, {}), (12, {"engine": "v1_fallback", "latency": 1200,
                                          "cost": None}))
    result, by_number = check(records, logs)

    assert by_number[1]["status"] == FAIL and by_number[1]["action"] == ROLLBACK
    assert "12 of 12" in by_number[1]["measured"]
    assert by_number[2]["status"] == PASS  # the person still got an answer
    assert by_number[6]["status"] == NOT_CHECKED
    assert result["decision"] == ROLLBACK
    assert step_check.action_line(result).startswith("deployment/canary.sh rollback")


def test_fault_injection_slow_mode_breaks_the_v2_p95():
    result, by_number = check(*healthy(latency=35_000))

    assert by_number[3]["status"] == FAIL and by_number[3]["action"] == ROLLBACK
    assert result["decision"] == ROLLBACK


@pytest.mark.parametrize("v2, failures, expected", [
    (12, 1, PASS), (12, 2, FAIL),    # under 20 v2 requests: at most 1 failure
    (60, 3, PASS), (60, 4, FAIL),    # otherwise at most 5 %
])
def test_v2_failure_threshold(v2, failures, expected):
    records, logs = step((60, {}), (v2 - failures, {"engine": "v2"}),
                         (failures, {"engine": "v1_fallback"}))
    _, by_number = check(records, logs)
    assert by_number[1]["status"] == expected


def test_a_v2_500_is_a_v2_failure_but_a_503_is_left_out():
    records, logs = step((100, {}), (10, {"engine": "v2"}), (2, {"engine": "v2", "status": 500}),
                         (5, {"engine": "v2", "status": 503}))
    result, by_number = check(records, logs)

    assert "2 of 12" in by_number[1]["measured"]
    assert result["v2_requests"] == 12


def test_v2_failures_with_503s_mean_hold_and_investigate_not_rollback():
    records, logs = step((100, {}), (10, {"engine": "v2"}), (2, {"engine": "v1_fallback"}),
                         (1, {"engine": "v1", "status": 503}))
    result, by_number = check(records, logs)

    assert by_number[1]["status"] == FAIL and by_number[1]["action"] == HOLD
    assert "503s" in by_number[1]["note"]
    assert result["decision"] == HOLD


def test_too_few_v2_requests_hold_and_two_runs_are_evaluated_together():
    first = healthy(v1=113, v2=7)
    result, _ = check(*first)
    assert result["decision"] == HOLD
    assert any("only 7 v2 requests" in reason for reason in result["reasons"])

    second = healthy(v1=113, v2=7)
    records = first[0] + second[0]
    result, _ = check(records, {**first[1], **second[1]})
    assert result["decision"] == PROMOTE and result["v2_requests"] == 14


def test_slow_v1_is_a_hold():
    result, by_number = check(*step((108, {"latency": 6000}), (12, {"engine": "v2"})))

    assert by_number[4]["status"] == FAIL and by_number[4]["action"] == HOLD
    assert result["decision"] == HOLD


@pytest.mark.parametrize("baseline_errors, step_errors, expected", [
    (0, 1, PASS),   # 1 of 120 is under 1 % and only 1 more than the baseline
    (0, 2, FAIL),   # 2 of 120 is over 1 %
    (1, 1, PASS),
])
def test_user_facing_errors_compare_with_the_baseline(baseline_errors, step_errors, expected):
    baseline, _ = step((120 - baseline_errors, {}), (baseline_errors, {"status": 500}))
    records, logs = step((108 - step_errors, {}), (12, {"engine": "v2"}),
                         (step_errors, {"status": 0}))  # client timeouts count here
    result, by_number = check(records, logs, baseline=baseline)

    assert by_number[2]["status"] == expected
    if expected == FAIL:
        assert by_number[2]["action"] == ROLLBACK and result["decision"] == ROLLBACK


def test_one_error_more_than_a_clean_baseline_but_over_one_percent_fails():
    baseline, _ = step((200, {}))
    records, logs = step((49, {}), (50, {"engine": "v2"}), (1, {"status": 0}))
    _, by_number = check(records, logs, baseline=baseline)
    assert by_number[2]["status"] == PASS  # 1 of 100 = 1 %

    records, logs = step((48, {}), (50, {"engine": "v2"}), (2, {"status": 0}))
    _, by_number = check(records, logs, baseline=baseline)
    assert by_number[2]["status"] == FAIL  # 2 %, and 2 more than the baseline


def test_two_errors_more_than_the_baseline_fail_even_under_one_percent():
    baseline, _ = step((400, {}))
    records, logs = step((350, {}), (48, {"engine": "v2"}), (2, {"status": 500}))
    _, by_number = check(records, logs, baseline=baseline)

    assert by_number[2]["status"] == FAIL  # 0.5 %, but 2 more than the clean baseline
    assert "baseline 0 of 400" in by_number[2]["measured"]


def test_without_a_baseline_criterion_2_is_not_checked():
    result, by_number = check(*healthy(), baseline=None)

    assert by_number[2]["status"] == NOT_CHECKED
    assert result["decision"] == HOLD


@pytest.mark.parametrize("wrong_v2, wrong_v1, status, action", [
    (1, 0, PASS, None),        # under 20 v2 answers: 1 wrong is allowed
    (2, 0, FAIL, ROLLBACK),
    (0, 1, FAIL, HOLD),        # v1 must be 100 %: a checker or data problem
])
def test_correctness(wrong_v2, wrong_v1, status, action):
    records, logs = step((108 - wrong_v1, {}), (wrong_v1, {"wrong": True}),
                         (12 - wrong_v2, {"engine": "v2"}),
                         (wrong_v2, {"engine": "v2", "wrong": True}))
    _, by_number = check(records, logs)

    assert by_number[5]["status"] == status
    assert by_number[5]["action"] == action


def test_a_wrong_fallback_answer_counts_like_v1():
    records, logs = step((108, {}), (11, {"engine": "v2"}),
                         (1, {"engine": "v1_fallback", "wrong": True}))
    _, by_number = check(records, logs)
    assert by_number[5]["status"] == FAIL and by_number[5]["action"] == HOLD


def test_cost():
    _, by_number = check(*healthy(cost=0.06))
    assert by_number[6]["status"] == FAIL and by_number[6]["action"] == ROLLBACK

    # Some costs unknown: the known average alone is not a pass, so hold (as the dashboard does).
    records, logs = step((108, {}), (11, {"engine": "v2", "cost": 0.02}),
                         (1, {"engine": "v1_fallback", "cost": None}))
    result, by_number = check(records, logs)
    assert by_number[6]["status"] == NOT_CHECKED and by_number[6]["action"] is None
    assert "1 unknown" in by_number[6]["measured"] and "hold" in by_number[6]["note"]
    assert "incomplete" in by_number[6]["cell"]
    assert result["decision"] == HOLD
    assert any(reason.startswith("criterion 6 not checked") for reason in result["reasons"])

    # A v2 503 has an unknown cost too, so it holds criterion 6 even when criterion 1 passes.
    records, logs = step((108, {}), (11, {"engine": "v2", "cost": 0.02}),
                         (1, {"engine": "v2", "status": 503, "cost": None}))
    result, by_number = check(records, logs)
    assert by_number[1]["status"] == PASS
    assert by_number[6]["status"] == NOT_CHECKED and result["decision"] == HOLD

    # Over the limit on the known costs alone: still a rollback, whatever the unknown ones cost.
    records, logs = step((108, {}), (11, {"engine": "v2", "cost": 0.06}),
                         (1, {"engine": "v1_fallback", "cost": None}))
    result, by_number = check(records, logs)
    assert by_number[6]["status"] == FAIL and by_number[6]["action"] == ROLLBACK
    assert result["decision"] == ROLLBACK

    records, logs = healthy()
    for line in logs.values():
        line.pop("model_cost_usd", None)  # a revision that does not log the cost yet
    result, by_number = check(records, logs)
    assert by_number[6]["status"] == NOT_CHECKED and "#45" in by_number[6]["note"]
    assert result["decision"] == HOLD


def test_without_logs_the_results_are_used_and_unknown_errors_block_criterion_1():
    records, _ = healthy()
    result, by_number = check(records, None)
    assert by_number[1]["status"] == PASS
    assert "client" in by_number[3]["measured"]
    assert by_number[6]["status"] == NOT_CHECKED
    assert result["decision"] == HOLD  # cost needs the log lines

    # Two 500s that no body says the engine of: they may have been v2's.
    errors, _ = step((2, {"status": 500}))
    _, by_number = check(records + errors, None)
    assert by_number[1]["status"] == NOT_CHECKED
    assert "--logs" in by_number[1]["note"]


def test_the_baseline_step():
    result, by_number = check(*step((120, {})), percent=0, baseline=None)

    assert by_number[1]["status"] == by_number[3]["status"] == by_number[6]["status"] \
        == NOT_APPLICABLE
    assert by_number[2]["status"] == by_number[4]["status"] == by_number[5]["status"] == PASS
    assert result["decision"] == PROMOTE
    assert step_check.action_line(result).startswith("deployment/canary.sh set 10")


def test_a_failing_baseline_is_a_hold_not_a_rollback():
    result, by_number = check(*step((118, {}), (2, {"status": 500})), percent=0, baseline=None)

    assert by_number[2]["status"] == FAIL and by_number[2]["action"] == HOLD
    assert result["decision"] == HOLD


def test_v2_traffic_in_the_baseline_is_a_warning():
    result, _ = check(*healthy(), percent=0, baseline=None)
    assert any("0 % step" in warning for warning in result["warnings"])


def test_the_last_step_has_no_v1_control_and_completes_the_canary():
    result, by_number = check(*step((120, {"engine": "v2"})), percent=100)

    assert by_number[4]["status"] == NOT_APPLICABLE
    assert result["decision"] == PROMOTE
    assert "canary is complete" in step_check.action_line(result)


def test_header_routing_and_missing_log_lines_are_warnings():
    records, logs = step((108, {}), (12, {"engine": "v2", "chosen_by": "header"}),
                         (3, {"logged": False}))
    result, _ = check(records, logs)

    assert any("X-Engine header" in warning for warning in result["warnings"])
    assert any("3 answered requests have no log line" in warning
               for warning in result["warnings"])


def test_log_row_matches_the_canary_log_columns():
    result, _ = check(*healthy())
    row = step_check.log_row(result)

    cells = [cell.strip() for cell in row.strip("|").split("|")]
    header = ("Date and time | Step | v2 requests | v2 failure % | User error % | v2 p95 | v1 p95 "
              "| v2 correct % | v1 correct % | v2 cost/request | Spend so far | Decision")
    assert len(cells) == len(header.split("|"))
    assert cells[:3] == ["2026-11-24 20:00 UTC", "1 (10 %)", "12"]
    assert cells[3] == "0.0 %" and cells[5] == "16.0 s" and cells[7] == "100.0 %"
    assert cells[9] == "$0.0100" and cells[-2:] == ["(fill in)", "Promote"]


# ---------------------------------------------------------------- files and the command line

def write_results(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return str(path)


def test_read_logs_accepts_gcloud_json_and_local_json_lines(tmp_path):
    _, logs = healthy(v1=2, v2=1)
    lines = list(logs.values())
    other = {"event": "recommendation", "request_id": "other", "engine": "v1", "status": 200}

    gcloud = tmp_path / "logs.json"
    gcloud.write_text(json.dumps([{"jsonPayload": line, "severity": "INFO"} for line in lines]
                                 + [{"textPayload": "Starting server"}]))
    assert step_check.read_logs(gcloud) == logs

    local = tmp_path / "server.log"
    local.write_text("INFO:     Uvicorn running\n" + "\n".join(json.dumps(line)
                                                              for line in [*lines, other]))
    assert step_check.read_logs(local) == {**logs, "other": other}


def test_other_traffic_in_the_logs_is_left_out():
    records, logs = healthy()
    _, others = step((50, {"engine": "v1_fallback"}))  # someone else's requests
    result, by_number = check(records, {**logs, **others})

    assert by_number[1]["status"] == PASS and result["v2_requests"] == 12


def test_command_line_exit_codes_and_report(tmp_path, capsys):
    records, logs = healthy()
    results = write_results(tmp_path / "step1.jsonl", records)
    logs_file = tmp_path / "logs.json"
    logs_file.write_text(json.dumps([{"jsonPayload": line} for line in logs.values()]))
    baseline = write_results(tmp_path / "step0.jsonl", BASELINE)

    code = step_check.main([results, "--percent", "10", "--logs", str(logs_file),
                            "--baseline", baseline])
    out = capsys.readouterr().out
    assert code == 0
    assert "Decision: PROMOTE" in out and "deployment/canary.sh set 25" in out
    assert "| 2026-11-24 20:00 UTC | 1 (10 %) | 12 |" in out

    faulty, faulty_logs = step((108, {}), (12, {"engine": "v1_fallback", "cost": None}))
    results = write_results(tmp_path / "fault.jsonl", faulty)
    logs_file.write_text(json.dumps([{"jsonPayload": line} for line in faulty_logs.values()]))
    assert step_check.main([results, "--percent", "10", "--logs", str(logs_file),
                            "--baseline", baseline]) == 1
    assert "Decision: ROLLBACK" in capsys.readouterr().out

    assert step_check.main([results, "--percent", "10", "--baseline", baseline]) == 1
    out = capsys.readouterr().out
    assert "gcloud logging read" in out
    assert 'timestamp>="2026-11-24T19:59:00Z"' in out and 'timestamp<="2026-11-24T20:03:00Z"' in out
    assert 'service_name="navcare-api-dev"' in out


def test_command_line_hold_and_bad_input(tmp_path, capsys):
    results = write_results(tmp_path / "step1.jsonl", healthy()[0])
    assert step_check.main([results, "--percent", "10"]) == 3  # no logs, no baseline: hold

    assert step_check.main([str(tmp_path / "missing.jsonl"), "--percent", "10"]) == 2
    assert "Could not read results" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stopped:
        step_check.main([results, "--percent", "101"])
    assert stopped.value.code == 2


# ---------------------------------------------------------------- end to end

def capture_api_logs(path):
    """Write the API's log lines to a file, as Cloud Run's stdout would have them."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logging.getLogger(LOGGER_NAME).addHandler(handler)
    return handler


def test_fault_injection_through_the_real_api_is_a_rollback(tmp_path, capsys, monkeypatch,
                                                           local_api):  # noqa: F811
    # Baseline: 0 %, the real v1 through the load test.
    logs_file = tmp_path / "server.log"
    handler = capture_api_logs(logs_file)
    try:
        baseline = tmp_path / "step0.jsonl"
        assert run.main(["--url", "http://localhost:8000", "--rate", "60000", "--duration", "1",
                         "--engine", "split", "--max-requests", "25", "--no-compare", "--yes",
                         "--out", str(baseline)], transport=local_api) == 0
        assert step_check.main([str(baseline), "--percent", "0", "--logs", str(logs_file)]) == 0
        out = capsys.readouterr().out
        assert "Decision: PROMOTE" in out and "deployment/canary.sh set 10" in out

        # The demo's rollback: v2 gets all traffic, and fault injection makes it fail before
        # Gemini is called, so v1 answers as v1_fallback (no paid call).
        monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "100")
        monkeypatch.setenv(fault_injection.ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(fault_injection.FAULT_MODE_ENV_VAR, "error")
        faulty = tmp_path / "step4.jsonl"
        assert run.main(["--url", "http://localhost:8000", "--rate", "60000", "--duration", "1",
                         "--engine", "split", "--max-requests", "25", "--no-compare", "--yes",
                         "--out", str(faulty)], transport=local_api) in (0, 1)
    finally:
        logging.getLogger(LOGGER_NAME).removeHandler(handler)
        handler.close()
    capsys.readouterr()

    code = step_check.main([str(faulty), "--percent", "100", "--logs", str(logs_file),
                            "--baseline", str(baseline)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "25 requests, 25 matched to log lines" in out  # the baseline's lines are left out
    assert "25 of 25 (100.0 %), 25 fallbacks" in out
    assert "Decision: ROLLBACK" in out and "deployment/canary.sh rollback" in out
