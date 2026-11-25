"""The canary dashboard (deployment/monitoring/) against the real log line and docs/canary.md."""

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import orchestrator
from app import cost_estimates
from app.agent_v2 import ai_agent_recommender as agent
from app.api import facade, main
from app.api.main import app
from loadtest import step_check
from tests import test_step_check as step_samples

REPO = Path(__file__).resolve().parents[1]
MONITORING = REPO / "deployment" / "monitoring"
SERVICE = "navcare-api-dev"  # apply.sh's default


def render(text, service=SERVICE):
    """What apply.sh does to a template before sending it."""
    return text.replace("__SERVICE__", service).replace("__PREFIX__", service.replace("-", "_"))


TEMPLATES = {path.stem: path.read_text(encoding="utf-8")
             for path in sorted((MONITORING / "metrics").glob("*.yaml"))}
METRICS = {f"navcare_api_dev_{stem}": yaml.safe_load(render(text))
           for stem, text in TEMPLATES.items()}
DASHBOARD_TEMPLATE = (MONITORING / "dashboard.json").read_text(encoding="utf-8")
DASHBOARD = json.loads(render(DASHBOARD_TEMPLATE))
CHARTS = {tile["widget"]["title"]: tile["widget"] for tile in DASHBOARD["mosaicLayout"]["tiles"]}
BASE_FILTER = ('resource.type="cloud_run_revision" AND resource.labels.service_name='
               '"navcare-api-dev" AND jsonPayload.event="recommendation"')
CORRECTNESS_FILTER = ('resource.type="global" AND jsonPayload.event="canary_correctness" AND '
                      'jsonPayload.service="navcare-api-dev"')
REQUEST_METRICS = {"requests", "latency_ms", "prompt_tokens", "output_tokens", "model_cost_usd",
                   "model_cost_unknown"}
CORRECTNESS_METRICS = {"correctness_checked", "correctness_correct"}  # step_check.py --publish
ALERT_TEMPLATES = {path.stem: path.read_text(encoding="utf-8")
                   for path in sorted((MONITORING / "alerts").glob("*.json"))}
ALERTS = {stem: json.loads(render(text)) for stem, text in ALERT_TEMPLATES.items()}
C5_TITLE = "Criterion 5: correctness % per group (from step_check.py --publish)"
COST_TITLE = "Criterion 6: estimated model-token cost per v2 request (USD, known estimates)"
COUNTS_TITLE = "Cost estimates: known vs unknown by status (requests that went to v2)"
MONTREAL = {"lat": 45.5017, "lon": -73.5673}
USAGE = {"model": agent.DEFAULT_MODEL, "model_calls": 3, "tool_calls": 4,
         "prompt_tokens": 4628, "output_tokens": 1508, "calls_without_usage": 0,
         "failed_calls": 0}
V1_RESULT = {"data_updated_at": "2026-11-13T12:00", "recommendations": []}
client = TestClient(app, raise_server_exceptions=False)  # a 500 is answered, as in production


@pytest.fixture(autouse=True)
def engine_header_on(monkeypatch):
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")
    monkeypatch.setenv(facade.HEADER_ENABLED_ENV_VAR, "true")
    monkeypatch.delenv(agent.LOCATION_ENV_VAR, raising=False)


def log_line(monkeypatch, caplog, engine="v2", usage=USAGE, fail=None):
    """The fields of the one recommendation log line for a request answered by `engine`."""
    def build(*args, **kwargs):
        if fail:
            raise fail
        return {**V1_RESULT, "engine": engine, "call_911": None, "note": None,
                "model_usage": usage}

    monkeypatch.setattr(orchestrator, "build_agent_recommendations", build)
    monkeypatch.setattr(orchestrator, "build_recommendations", lambda *a, **k: dict(V1_RESULT))
    caplog.clear()
    caplog.set_level("INFO", logger="navcare")
    client.get("/v1/recommendations", params=MONTREAL,
               headers={"X-Engine": "v1" if engine == "v1" else "v2"})
    (record,) = [r for r in caplog.records
                 if getattr(r, "fields", {}).get("event") == "recommendation"]
    return record.fields


# The model-cost fields in the log line


def test_known_cost_comes_from_the_estimator(monkeypatch, caplog):
    fields = log_line(monkeypatch, caplog)

    expected = cost_estimates.estimate_gemini_cost(USAGE, "global", datetime.now(timezone.utc))
    assert fields["model_cost_usd"] == pytest.approx(expected["estimated_usd"])
    assert "model_cost_unknown" not in fields


@pytest.mark.parametrize(("utc_time", "price_day"), [
    # 23:30 on Dec 31 in Pacific Time is already Jan 1 in UTC and in Montréal.
    (datetime(2027, 1, 1, 7, 30, tzinfo=timezone.utc), date(2026, 12, 31)),
    (datetime(2027, 1, 1, 8, 30, tzinfo=timezone.utc), date(2027, 1, 1)),
])
def test_price_day_is_the_cloud_billing_day_in_pacific_time(utc_time, price_day):
    fields = main.model_cost_fields("v2", USAGE, now=utc_time)

    expected = cost_estimates.estimate_gemini_cost(USAGE, "global", price_day)["estimated_usd"]
    assert fields["model_cost_usd"] == pytest.approx(expected)


def test_the_price_change_shows_at_pacific_midnight():
    before = main.model_cost_fields("v2", USAGE, datetime(2027, 1, 1, 7, 59, tzinfo=timezone.utc))
    after = main.model_cost_fields("v2", USAGE, datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc))

    assert after["model_cost_usd"] == pytest.approx(2 * before["model_cost_usd"])


def test_v1_lines_have_no_cost_fields(monkeypatch, caplog):
    fields = log_line(monkeypatch, caplog, engine="v1", usage=None)

    assert "model_cost_usd" not in fields and "model_cost_unknown" not in fields


@pytest.mark.parametrize(("engine", "usage", "reason"), [
    ("v1_fallback", None, "no model usage recorded"),  # e.g. no model call was made
    ("v2", {**USAGE, "calls_without_usage": 1}, "1 of 3 model calls have unknown usage"),
    ("v2", {**USAGE, "failed_calls": 1}, "1 of 3 model calls have unknown usage (1 failed"),
    ("v2", {**USAGE, "prompt_tokens": 0, "output_tokens": 0}, "3 model calls but no tokens"),
])
def test_unknown_cost_is_logged_with_its_reason_never_as_zero(monkeypatch, caplog, engine, usage,
                                                              reason):
    fields = log_line(monkeypatch, caplog, engine=engine, usage=usage)

    assert "model_cost_usd" not in fields
    assert fields["model_cost_unknown"].startswith(reason)


@pytest.mark.parametrize(("fail", "status"), [
    (RuntimeError("Route request failed"), 503),  # the fallback's data services failed
    (KeyError("bug"), 500),
])
def test_v2_errors_log_an_unknown_cost(monkeypatch, caplog, fail, status):
    # Gemini may already have been paid for, but these paths log no model usage.
    fields = log_line(monkeypatch, caplog, fail=fail)

    assert (fields["status"], fields["engine"]) == (status, "v2")
    assert fields["model_cost_unknown"] == "no model usage recorded"


def test_an_unpriced_location_is_unknown(monkeypatch, caplog):
    monkeypatch.setenv(agent.LOCATION_ENV_VAR, "northamerica-northeast1")

    fields = log_line(monkeypatch, caplog)

    assert fields["model_cost_unknown"].startswith("no known price for model")


# Metric definitions, and what they count from real log lines


def matches(log_filter, fields, service=SERVICE, resource="cloud_run_revision"):
    """Evaluate one of our metric filters (AND of simple clauses) on a log line's fields.

    resource: the entry's resource type. The API's lines are Cloud Run's; `gcloud logging write`
    (step_check.py --publish) writes "global" entries.
    """
    for clause in log_filter.split(" AND "):
        name, op, value = re.match(r"(\S+?)(:\*|!=|>=|=)(.*)", clause.strip()).groups()
        if name == "resource.type":
            ok = value == f'"{resource}"'
        elif name == "resource.labels.service_name":
            ok = value == f'"{service}"'
        else:
            found = fields
            for key in name.removeprefix("jsonPayload.").split("."):
                found = found.get(key) if isinstance(found, dict) else None
            if op == ":*":
                ok = found is not None
            elif op == "=":
                ok = str(found) == value.strip('"')
            elif op == "!=":
                ok = found is not None and str(found) != value.strip('"')
            else:  # >=
                ok = isinstance(found, (int, float)) and found >= float(value)
        if not ok:
            return False
    return True


def labels(metric, fields):
    return tuple(str(fields[extractor[len("EXTRACT(jsonPayload."):-1]])
                 for extractor in metric["labelExtractors"].values())


def count(lines, stem):
    """Lines a metric would count, by its labels (one log line, one count)."""
    metric = METRICS[f"navcare_api_dev_{stem}"]
    return Counter(labels(metric, f) for f in lines if matches(metric["filter"], f))


def cost_chart_state(lines):
    """What the criterion 6 chart draws for one step, following its query gates."""
    known = sum(count(lines, "model_cost_usd").values())
    unknown = sum(count(lines, "model_cost_unknown").values())
    if not known:
        return "no average"
    return "INCOMPLETE: hold" if unknown else "complete"


def unknown_by_status(lines):
    by_status = Counter()
    for (_, status), n in count(lines, "model_cost_unknown").items():
        by_status[status] += n  # summed over engines, as the chart's `sum by (status)`
    return dict(by_status)


def test_every_metric_reads_this_services_lines_by_engine():
    assert set(TEMPLATES) == {*REQUEST_METRICS, *CORRECTNESS_METRICS}
    for stem, metric in zip(TEMPLATES, METRICS.values()):
        base = BASE_FILTER if stem in REQUEST_METRICS else CORRECTNESS_FILTER
        assert metric["filter"].startswith(base)
        assert metric["labelExtractors"]["engine"] == "EXTRACT(jsonPayload.engine)"
    for text in [*TEMPLATES.values(), DASHBOARD_TEMPLATE, *ALERT_TEMPLATES.values()]:
        assert "navcare-api-dev" not in text  # the service comes from apply.sh


def test_metrics_only_use_fields_the_app_logs(monkeypatch, caplog):
    lines = [log_line(monkeypatch, caplog),
             log_line(monkeypatch, caplog, engine="v1_fallback", usage=None)]
    for name, metric in METRICS.items():
        if name.removeprefix("navcare_api_dev_") in CORRECTNESS_METRICS:
            continue  # written by the step check: test_correctness_metrics_read_the_step_check

        text = " ".join([metric["filter"], metric.get("valueExtractor", ""),
                         *metric["labelExtractors"].values()])
        for path in re.findall(r"jsonPayload\.([\w.]+)", text):
            def has(fields, keys=path.split(".")):
                for key in keys:
                    if not isinstance(fields, dict) or key not in fields:
                        return False
                    fields = fields[key]
                return True
            assert any(has(line) for line in lines), f"{name}: {path} is never logged"


def test_metrics_follow_the_canary_rules():
    latency = METRICS["navcare_api_dev_latency_ms"]
    assert "status!=503" in latency["filter"]  # criteria 3 and 4
    assert {5000, 30000} <= set(latency["bucketOptions"]["explicitBuckets"]["bounds"])
    cost = METRICS["navcare_api_dev_model_cost_usd"]
    assert 0.05 in cost["bucketOptions"]["explicitBuckets"]["bounds"]
    # Criterion 6 is over every line that went to v2: no status filter on either cost metric.
    for stem in ("model_cost_usd", "model_cost_unknown"):
        assert "status" not in METRICS[f"navcare_api_dev_{stem}"]["filter"]


def scenario(monkeypatch, caplog, *kinds):
    builders = {
        "valid": lambda: log_line(monkeypatch, caplog),
        "503": lambda: log_line(monkeypatch, caplog, fail=RuntimeError("Route request failed")),
        "500": lambda: log_line(monkeypatch, caplog, fail=KeyError("bug")),
        "no tokens": lambda: log_line(monkeypatch, caplog,
                                      usage={**USAGE, "calls_without_usage": 1}),
        "fallback": lambda: log_line(monkeypatch, caplog, engine="v1_fallback", usage=None),
        "v1": lambda: log_line(monkeypatch, caplog, engine="v1", usage=None),
    }
    return [builders[kind]() for kind in kinds]


@pytest.mark.parametrize(("kinds", "state", "unknown"), [
    (["valid", "valid", "valid"], "complete", {}),
    # A 503 keeps the known average visible, marked incomplete, and shows up as unknown.
    (["valid", "valid", "503", "503"], "INCOMPLETE: hold", {"503": 2}),
    (["valid", "valid", "500"], "INCOMPLETE: hold", {"500": 1}),
    (["valid", "no tokens", "fallback"], "INCOMPLETE: hold", {"200": 2}),
    (["503", "500"], "no average", {"503": 1, "500": 1}),  # everything unknown
    (["v1", "v1"], "no average", {}),  # no eligible traffic
])
def test_criterion_6_from_real_log_lines(monkeypatch, caplog, kinds, state, unknown):
    lines = scenario(monkeypatch, caplog, *kinds)

    assert cost_chart_state(lines) == state
    assert unknown_by_status(lines) == unknown
    assert sum(count(lines, "model_cost_usd").values()) == kinds.count("valid")


def test_criteria_1_and_3_still_leave_503s_out(monkeypatch, caplog):
    lines = scenario(monkeypatch, caplog, "valid", "503", "500")

    assert sum(count(lines, "latency_ms").values()) == 2  # the 503 is left out
    assert count(lines, "requests") == {("v2", "200"): 1, ("v2", "503"): 1, ("v2", "500"): 1}


# Dashboard


def queries(title):
    return [d["timeSeriesQuery"]["prometheusQuery"] for d in CHARTS[title]["xyChart"]["dataSets"]]


def thresholds(title):
    return [t["value"] for t in CHARTS[title]["xyChart"]["thresholds"]]


def test_charts_only_use_defined_metrics():
    used = {re.sub(r"_(bucket|sum|count)$", "", name) for title, chart in CHARTS.items()
            if "xyChart" in chart for q in queries(title)
            for name in re.findall(r"logging_googleapis_com:user_(\w+)", q)}
    assert used == set(METRICS)


def canary_criteria():
    rows = {}
    for line in (REPO / "docs" / "canary.md").read_text(encoding="utf-8").splitlines():
        cells = line.split("|")
        match = re.match(r"\| (\d) \|", line)
        if match and len(cells) == 7:  # the criteria table has 5 columns; the steps table 4
            rows[int(match.group(1))] = cells[4]
    return rows


def test_thresholds_match_docs_canary_md():
    rows = canary_criteria()
    rate = float(re.search(r"≤ (\d+(?:\.\d+)?) %", rows[1]).group(1))
    small_step, max_failures = map(int, re.search(
        r"fewer than (\d+) v2 requests: at most (\d+) failure", rows[1]).groups())
    v2_p95_s = float(re.search(r"≤ (\d+) s", rows[3]).group(1))
    v1_p95_s = float(re.search(r"≤ (\d+) s", rows[4]).group(1))
    cost = float(re.search(r"≤ \$(\d+\.\d+)", rows[6]).group(1))
    rate_title = "Criterion 1: v2 failure rate (%), shown from about 20 v2 requests"

    assert thresholds(rate_title) == [rate]
    assert thresholds("Criterion 1 (small steps): v2 failures (approx.)") == [max_failures]
    assert thresholds("Requests that went to v2 (without 503s, approx.)") == [small_step]
    assert f">= {small_step})" in queries(rate_title)[0]
    assert thresholds("Criterion 3: v2 p95 latency (v2 and v1_fallback, ms)") == [v2_p95_s * 1000]
    assert thresholds("Criterion 4: v1 p95 latency (ms)") == [v1_p95_s * 1000]
    assert thresholds(COST_TITLE) == [cost]
    assert thresholds(C5_TITLE) == [float(re.search(r"v2 ≥ (\d+) %", rows[5]).group(1))]


def test_v2_failures_count_fallbacks_and_500s_over_v2_routed_requests_without_503s():
    (rate,) = queries("Criterion 1: v2 failure rate (%), shown from about 20 v2 requests")
    numerator, denominator = rate.split(" / ", 1)
    assert 'engine="v1_fallback",status!="503"' in numerator
    assert 'engine="v2",status="500"' in numerator
    assert 'engine=~"v2|v1_fallback",status!="503"' in denominator


def test_no_traffic_never_draws_a_pass():
    # Zero failures are drawn only when some request went to v2; rates need traffic to divide by.
    (failures,) = queries("Criterion 1 (small steps): v2 failures (approx.)")
    assert failures.endswith('engine=~"v2|v1_fallback",status!="503"}[10m])) > 0)')
    # Elsewhere a zero fallback must sit over a traffic count with none, so no traffic is a gap.
    # (A zero inside an `and on()` condition only hides a line, so it can't draw one.)
    for title, chart in CHARTS.items():
        if "xyChart" in chart and title != "Criterion 1 (small steps): v2 failures (approx.)":
            for query in queries(title):
                drawn = query.split(" and on() ")[0]
                if "vector(0)" in drawn:
                    assert " / " in drawn, title
                    assert "vector(0)" not in drawn.split(" / ", 1)[1], title


def all_queries():
    """(where, query) for every chart and alert condition."""
    for title, chart in CHARTS.items():
        if "xyChart" in chart:
            for query in queries(title):
                yield title, query
    for stem, policy in ALERTS.items():
        for condition in policy["conditions"]:
            yield stem, condition["conditionPrometheusQueryLanguage"]["query"]


def test_every_selector_names_the_monitored_resource():
    # Log-based metrics need it in PromQL (Cloud Monitoring's PromQL mapping docs). The step
    # check's entries are written with `gcloud logging write`, so their resource is "global".
    for where, query in all_queries():
        for metric, selector in re.findall(r"user_navcare_api_dev_(\w+?)(\{[^}]*\}|\[)", query):
            stem = re.sub(r"_(bucket|sum|count)$", "", metric)
            resource = "global" if stem in CORRECTNESS_METRICS else "cloud_run_revision"
            assert f'monitored_resource="{resource}"' in selector, where


def test_cost_average_stays_visible_and_is_marked_incomplete_when_any_cost_is_unknown():
    complete, incomplete = CHARTS[COST_TITLE]["xyChart"]["dataSets"]
    assert complete["legendTemplate"].startswith("complete")
    assert incomplete["legendTemplate"].startswith("INCOMPLETE: hold")
    average = "sum(increase(logging_googleapis_com:user_navcare_api_dev_model_cost_usd_sum"
    unknown = "sum(increase(logging_googleapis_com:user_navcare_api_dev_model_cost_unknown"
    complete_q = complete["timeSeriesQuery"]["prometheusQuery"]
    incomplete_q = incomplete["timeSeriesQuery"]["prometheusQuery"]
    assert complete_q.startswith(f"({average}") and incomplete_q.startswith(f"({average}")
    # Exactly one of the two is drawn: no unknown, or some unknown.
    assert complete_q.endswith(" or vector(0)) == 0)")
    assert unknown in complete_q.split("and on()")[1]
    assert incomplete_q.endswith(" > 0)") and unknown in incomplete_q.split("and on()")[1]
    counts = queries(COUNTS_TITLE)
    assert any(q.startswith("sum by (status) (increase(") and "model_cost_unknown" in q
               for q in counts)


def test_reading_note_explains_end_time_approximate_counts_and_incomplete_cost():
    note = next(t["widget"]["text"]["content"] for t in DASHBOARD["mosaicLayout"]["tiles"]
                if "text" in t["widget"])
    for phrase in ("read a step at its end time", "can be fractional", "step_check.py",
                   "INCOMPLETE: hold", "503 or 500"):
        assert phrase in note


# Criterion 5: correctness counts published by the step check


def published(records, percent=10):
    """The entries step_check.py --publish would write for these results."""
    result = step_check.evaluate(records, percent)
    return result, step_check.correctness_entries(result, SERVICE)


def sums(entries, stem):
    """What a correctness metric's _sum adds up per group, from the entries it matches."""
    metric = METRICS[f"navcare_api_dev_{stem}"]
    field = metric["valueExtractor"][len("EXTRACT(jsonPayload."):-1]
    totals = Counter()
    for entry in entries:
        if matches(metric["filter"], entry, resource="global"):
            totals[labels(metric, entry)[0]] += entry[field]
    return totals


def test_correctness_metrics_read_the_step_check_entries():
    records, _ = step_samples.step((10, {}), (2, {"engine": "v1_fallback"}),
                                   (18, {"engine": "v2"}), (2, {"engine": "v2", "wrong": True}))
    result, entries = published(records)

    assert sums(entries, "correctness_checked") == {"v2": 20, "v1": 12}
    assert sums(entries, "correctness_correct") == {"v2": 18, "v1": 12}
    # The chart's ratio of sums is criterion 5's own figure.
    v2_cell, v1_cell = next(c["cell"] for c in result["criteria"] if c["number"] == 5)
    assert (v2_cell, v1_cell) == ("90.0 %", "100.0 %")
    # Another service's entries, and the API's own lines, are left out.
    other = step_check.correctness_entries(result, "navcare-api-prod")
    assert sums(other, "correctness_checked") == {}
    for stem in CORRECTNESS_METRICS:
        assert not matches(METRICS[f"navcare_api_dev_{stem}"]["filter"], entries[0])


def test_a_group_with_no_answers_publishes_nothing():
    records, _ = step_samples.step((12, {}))  # the 0 % baseline: v1 only

    _, entries = published(records, percent=0)

    assert [(e["engine"], e["checked"]) for e in entries] == [("v1", 12)]


def test_publish_writes_one_gcloud_entry_per_group(monkeypatch):
    calls = []
    monkeypatch.setenv("PROJECT_ID", "p-1")
    monkeypatch.setenv("SERVICE", "navcare-api-prod")
    records, _ = step_samples.step((3, {}), (2, {"engine": "v2"}))
    result = step_check.evaluate(records, 10)

    entries = step_check.publish_correctness(result, run=lambda args, **kw: calls.append(args))

    assert len(calls) == len(entries) == 2
    for args, entry in zip(calls, entries):
        assert args[1:6] == ["--project=p-1", "--quiet", "logging", "write",
                             step_check.CORRECTNESS_LOG]
        assert json.loads(args[6]) == entry and entry["service"] == "navcare-api-prod"
        assert args[7] == "--payload-type=json"


def test_publish_failure_is_exit_code_2(tmp_path, monkeypatch, capsys):
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="PERMISSION_DENIED")

    monkeypatch.setattr(step_check.subprocess, "run", fail)
    records, _ = step_samples.step((12, {}))
    results = step_samples.write_results(tmp_path / "step0.jsonl", records)

    assert step_check.main([results, "--percent", "0", "--publish"]) == 2
    assert "Could not publish the correctness counts: PERMISSION_DENIED" in capsys.readouterr().err


def test_correctness_chart_follows_criterion_5():
    v2, v1 = queries(C5_TITLE)
    for query, group in ((v2, "v2"), (v1, "v1")):
        numerator, denominator = query.split(" / ")
        assert numerator.startswith("100 * sum(increase(")
        assert "correctness_correct_sum" in numerator
        assert "correctness_checked_sum" in denominator
        assert all(f'engine="{group}"' in part for part in (numerator, denominator))
    correct = float(re.search(r"v2 ≥ (\d+) %", canary_criteria()[5]).group(1))
    assert thresholds(C5_TITLE) == [correct]


# Alert policies


def conditions(stem):
    return [c["conditionPrometheusQueryLanguage"]["query"] for c in ALERTS[stem]["conditions"]]


C1_RATE = "Criterion 1: v2 failure rate (%), shown from about 20 v2 requests"
C1_SMALL = "Criterion 1 (small steps): v2 failures (approx.)"
C3_TITLE = "Criterion 3: v2 p95 latency (v2 and v1_fallback, ms)"
C4_TITLE = "Criterion 4: v1 p95 latency (ms)"


def test_one_policy_per_dashboard_criterion_with_the_docs_action():
    # Rollback criteria are CRITICAL; hold criteria (4, and v1 correctness) WARNING.
    assert {stem: policy["severity"] for stem, policy in ALERTS.items()} == {
        "criterion_1_v2_failure_rate": "CRITICAL",
        "criterion_1_small_step_failures": "CRITICAL",
        "criterion_3_v2_p95": "CRITICAL",
        "criterion_4_v1_p95": "WARNING",
        "criterion_5_v2_correctness": "CRITICAL",
        "criterion_5_small_step_wrong": "CRITICAL",
        "criterion_5_v1_correctness": "WARNING",
        "criterion_6_v2_cost": "CRITICAL",
    }
    for stem, policy in ALERTS.items():
        doc = policy["documentation"]["content"]
        action = "rollback" if policy["severity"] == "CRITICAL" else "hold and investigate"
        assert f"**Action (docs/canary.md): {action}" in doc, stem
        assert "step check" in doc, stem  # an alert prompts the check; it doesn't decide
    names = [policy["displayName"] for policy in ALERTS.values()]
    assert len(set(names)) == len(names)
    assert all(name.startswith("NavCare canary (navcare-api-dev): ") for name in names)


def test_policies_are_templates_apply_sh_can_fill():
    for stem, text in ALERT_TEMPLATES.items():
        policy = json.loads(text)
        assert policy["notificationChannels"] == [], stem  # apply.sh fills in ALERT_EMAILS
        assert '"notificationChannels": []' in text, stem  # the exact text its sed replaces
        assert re.search(r'^  "displayName": ".*",$', text, re.M), stem  # what it reads back
        assert policy["userLabels"] == {"app": "navcare", "service": "__SERVICE__"}
        assert policy["combiner"] == "OR"
        # Cloud Monitoring allows only one condition in a policy with a PromQL condition.
        assert len(policy["conditions"]) == 1, stem


def test_alert_conditions_are_the_dashboard_queries_at_its_thresholds():
    # The same query and line as the chart, so an alert and the chart can't disagree.
    def is_chart_over(stem, title, value):
        assert conditions(stem) == [f"({queries(title)[0]}) > {value:g}"], stem

    (rate,) = thresholds(C1_RATE)
    is_chart_over("criterion_1_v2_failure_rate", C1_RATE, rate)
    is_chart_over("criterion_3_v2_p95", C3_TITLE, *thresholds(C3_TITLE))
    is_chart_over("criterion_4_v1_p95", C4_TITLE, *thresholds(C4_TITLE))

    (small,) = conditions("criterion_1_small_step_failures")
    (requests_line,) = thresholds("Requests that went to v2 (without 503s, approx.)")
    went_to_v2 = queries("Requests that went to v2 (without 503s, approx.)")[0]
    assert small == (f"({queries(C1_SMALL)[0]} and on() ({went_to_v2} < {requests_line:g})) "
                     f"> {thresholds(C1_SMALL)[0]:g}")

    (cost,) = conditions("criterion_6_v2_cost")
    known_average = queries(COST_TITLE)[0].split(" and on() ")[0]
    assert cost == f"({known_average}) > {thresholds(COST_TITLE)[0]:g}"  # unknowns don't hide it

    (v2_correct,) = conditions("criterion_5_v2_correctness")
    (v2_wrong,) = conditions("criterion_5_small_step_wrong")
    (v2_pct, v1_pct), (line,) = queries(C5_TITLE), thresholds(C5_TITLE)
    assert v2_correct.startswith(f"({v2_pct} and on() (") and v2_correct.endswith(
        f" >= {requests_line:g})) < {line:g}")
    assert v2_wrong.endswith(f" < {requests_line:g})) > 1")
    assert conditions("criterion_5_v1_correctness") == [f"({v1_pct}) < 100"]


def test_alerts_only_use_defined_metrics():
    used = {re.sub(r"_(bucket|sum|count)$", "", name) for _, query in all_queries()
            for name in re.findall(r"logging_googleapis_com:user_(\w+)", query)}
    assert used <= set(METRICS)


# apply.sh with a fake gcloud (no cloud calls)

FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
keep() {  # keep each config that was sent, by the name of the file
  for arg in "$@"; do
    case "$arg" in
      --config-from-file=*) cp "${arg#--config-from-file=}" "$FAKE_DIR/" ;;
      --policy-from-file=*) cp "${arg#--policy-from-file=}" "$FAKE_DIR/" ;;
    esac
  done
}
case "$*" in
  *" logging metrics describe "*)
    [ "$FAKE_EXISTS" = 1 ] || exit 1
    echo "${FAKE_FILTER_KEY:-resource.labels.service_name}=\\"$FAKE_METRIC_SERVICE\\" AND more" ;;
  *" monitoring dashboards list "*) printf '%s' "$FAKE_DASHBOARDS" ;;
  *" monitoring dashboards describe "*) printf '%s' "$FAKE_ETAG" ;;
  *" monitoring channels list "*"email_address=\\"old@example.com\\""*)
    echo "projects/1/notificationChannels/old" ;;
  *" monitoring channels list "*) ;;
  *" monitoring channels create "*) echo "projects/1/notificationChannels/new" ;;
  *" monitoring policies list "*) printf '%s' "$FAKE_POLICIES" ;;
  *" logging metrics "*|*" monitoring dashboards "*|*" monitoring policies "*) keep "$@" ;;
esac
exit 0
"""
BASH = shutil.which("bash") if os.name != "nt" or os.environ.get("MSYSTEM") else None
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash (Git Bash on Windows)")


def apply(tmp_path, service=None, exists=False, metric_service=SERVICE, dashboards="",
          etag="abc123", alert_emails=None, policies=""):
    fake = tmp_path / "gcloud"
    fake.write_text(FAKE_GCLOUD, newline="\n")
    fake.chmod(0o755)
    sent, temp = tmp_path / "sent", tmp_path / "tmp"
    sent.mkdir()
    temp.mkdir()
    log = tmp_path / "calls.log"
    env = {**os.environ, "GCLOUD": fake.as_posix(), "FAKE_LOG": log.as_posix(),
           "FAKE_DIR": sent.as_posix(), "FAKE_EXISTS": "1" if exists else "0",
           "FAKE_METRIC_SERVICE": metric_service, "FAKE_DASHBOARDS": dashboards,
           "FAKE_ETAG": etag, "FAKE_POLICIES": policies, "TMPDIR": temp.as_posix()}
    for name in ("PROJECT_ID", "SERVICE", "ALERT_EMAILS"):
        env.pop(name, None)
    if service:
        env["SERVICE"] = service
    if alert_emails is not None:
        env["ALERT_EMAILS"] = alert_emails
    result = subprocess.run([BASH, "deployment/monitoring/apply.sh"], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, sent, temp


@needs_bash
@pytest.mark.parametrize(("exists", "verb"), [(False, "create"), (True, "update")])
def test_apply_creates_or_updates_the_default_service(tmp_path, exists, verb):
    dashboards = "projects/1/dashboards/abc" if exists else ""
    result, calls, sent, temp = apply(tmp_path, exists=exists, dashboards=dashboards)

    assert result.returncode == 0, result.stderr
    assert all(c.startswith("--project=project-69a1b17d-f37d-485c-912 --quiet ") for c in calls)
    for name, metric in METRICS.items():
        assert any(f"logging metrics {verb} {name} --config-from-file=" in c for c in calls)
        assert yaml.safe_load((sent / f"{name}.yaml").read_text()) == metric
    if exists:  # an update sends the current etag, as the API expects; a create none
        assert json.loads((sent / "update.json").read_text()) == {"etag": "abc123", **DASHBOARD}
    else:
        assert json.loads((sent / "dashboard.json").read_text()) == DASHBOARD
    assert not any("delete" in c for c in calls)
    assert list(temp.iterdir()) == []  # the trap removed the temporary files


@needs_bash
def test_another_service_gets_its_own_metrics_and_dashboard(tmp_path):
    result, calls, sent, _ = apply(tmp_path, service="navcare-api-prod")

    assert result.returncode == 0, result.stderr
    names = {Path(p).stem for p in os.listdir(sent) if p.endswith(".yaml")}
    assert names == {f"navcare_api_prod_{stem}" for stem in TEMPLATES}
    for name in names:
        key = ("jsonPayload.service" if name.removeprefix("navcare_api_prod_")
               in CORRECTNESS_METRICS else "resource.labels.service_name")
        assert f'{key}="navcare-api-prod"' in (sent / f"{name}.yaml").read_text()
    for stem in ALERT_TEMPLATES:
        policy = json.loads((sent / f"{stem}.json").read_text())
        assert policy["displayName"].startswith("NavCare canary (navcare-api-prod): ")
        assert "navcare_api_dev" not in json.dumps(policy)
    prod = json.loads((sent / "dashboard.json").read_text())
    assert prod == json.loads(render(DASHBOARD_TEMPLATE, "navcare-api-prod"))
    assert prod["displayName"] == "NavCare canary: navcare-api-prod"
    assert "navcare_api_dev" not in json.dumps(prod)
    assert any('displayName="NavCare canary: navcare-api-prod"' in c for c in calls)


@needs_bash
def test_apply_refuses_a_metric_that_reads_another_service(tmp_path):
    result, calls, _, _ = apply(tmp_path, exists=True, metric_service="navcare-api-prod")

    assert result.returncode != 0 and "does not read navcare-api-dev" in result.stderr
    assert not any("metrics update" in c or "metrics create" in c for c in calls)


@needs_bash
def test_apply_stops_when_two_dashboards_have_the_name(tmp_path):
    two = "projects/1/dashboards/a\nprojects/1/dashboards/b"
    result, calls, _, _ = apply(tmp_path, dashboards=two)

    assert result.returncode != 0 and "2 dashboards are named" in result.stderr
    assert not any("dashboards update" in c or "dashboards create" in c for c in calls)


@needs_bash
def test_apply_stops_instead_of_updating_without_an_etag(tmp_path):
    result, calls, _, _ = apply(tmp_path, dashboards="projects/1/dashboards/abc", etag="")

    assert result.returncode != 0 and "No etag" in result.stderr
    assert not any("dashboards update" in c for c in calls)


@needs_bash
@pytest.mark.parametrize("service", ["Navcare", "navcare_api", "navcare-api-", "a/b"])
def test_apply_rejects_a_bad_service_name(tmp_path, service):
    result, calls, _, _ = apply(tmp_path, service=service)

    assert result.returncode != 0 and "SERVICE must be a Cloud Run service name" in result.stderr
    assert calls == []


def policy_calls(calls, verb):
    return [c for c in calls if f" beta monitoring policies {verb} " in f" {c} "]


@needs_bash
def test_apply_creates_every_alert_policy_emailing_alert_emails(tmp_path):
    result, calls, sent, _ = apply(tmp_path, alert_emails=" old@example.com, new@example.com ,")

    assert result.returncode == 0, result.stderr
    # An existing channel is reused; a missing one is created, once.
    creates = [c for c in calls if "monitoring channels create" in c]
    assert len(creates) == 1 and "--channel-labels=email_address=new@example.com" in creates[0]
    assert len(policy_calls(calls, "create")) == len(ALERTS) and not policy_calls(calls, "update")
    for stem, policy in ALERTS.items():
        assert json.loads((sent / f"{stem}.json").read_text()) == {
            **policy, "notificationChannels": ["projects/1/notificationChannels/old",
                                               "projects/1/notificationChannels/new"]}
        assert any(f'--filter=displayName="{policy["displayName"]}"' in c for c in calls)


@needs_bash
def test_apply_without_alert_emails_still_applies_the_policies_and_says_so(tmp_path):
    result, calls, sent, _ = apply(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "email no one" in result.stderr
    assert not any("monitoring channels" in c for c in calls)
    for stem, policy in ALERTS.items():
        assert json.loads((sent / f"{stem}.json").read_text()) == policy


@needs_bash
def test_apply_updates_existing_policies_in_place(tmp_path):
    result, calls, _, _ = apply(tmp_path, policies="projects/1/alertPolicies/7")

    assert result.returncode == 0, result.stderr
    updates = policy_calls(calls, "update")
    assert len(updates) == len(ALERTS) and not policy_calls(calls, "create")
    assert all(" projects/1/alertPolicies/7 --policy-from-file=" in c for c in updates)


@needs_bash
def test_apply_stops_when_two_policies_have_the_name(tmp_path):
    two = "projects/1/alertPolicies/a\nprojects/1/alertPolicies/b"
    result, calls, _, _ = apply(tmp_path, policies=two)

    assert result.returncode != 0 and "2 alert policies are named" in result.stderr
    assert not policy_calls(calls, "update") and not policy_calls(calls, "create")


@needs_bash
@pytest.mark.parametrize("emails", ["not-an-email", "a@b.ca|x", 'a"@b.ca'])
def test_apply_rejects_a_bad_alert_email(tmp_path, emails):
    result, calls, _, _ = apply(tmp_path, alert_emails=emails)

    assert result.returncode != 0 and "is not an email address" in result.stderr
    assert not policy_calls(calls, "create") and not any("channels create" in c for c in calls)


@needs_bash
@pytest.mark.parametrize(("metric_service", "ok"), [(SERVICE, True), ("navcare-api-prod", False)])
def test_apply_checks_the_service_of_metrics_that_name_it_in_the_payload(tmp_path, monkeypatch,
                                                                         metric_service, ok):
    # Correctness metrics name the service as jsonPayload.service, not the Cloud Run resource.
    monkeypatch.setenv("FAKE_FILTER_KEY", "jsonPayload.service")
    result, calls, _, _ = apply(tmp_path, exists=True, metric_service=metric_service)

    assert (result.returncode == 0) == ok, result.stderr
    if not ok:
        assert "does not read navcare-api-dev" in result.stderr
        assert not any("metrics update" in c for c in calls)
