"""The canary dashboard (deployment/monitoring/) against the real log line and docs/canary.md."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import orchestrator
from app import cost_estimates
from app.agent_v2 import ai_agent_recommender as agent
from app.api import facade, main
from app.api.main import app

REPO = Path(__file__).resolve().parents[1]
MONITORING = REPO / "deployment" / "monitoring"
METRICS = {path.stem: yaml.safe_load(path.read_text(encoding="utf-8"))
           for path in sorted((MONITORING / "metrics").glob("*.yaml"))}
DASHBOARD = json.loads((MONITORING / "dashboard.json").read_text(encoding="utf-8"))
CHARTS = {tile["widget"]["title"]: tile["widget"] for tile in DASHBOARD["mosaicLayout"]["tiles"]}
BASE_FILTER = ('resource.type="cloud_run_revision" AND resource.labels.service_name='
               '"navcare-api-dev" AND jsonPayload.event="recommendation"')
MONTREAL = {"lat": 45.5017, "lon": -73.5673}
USAGE = {"model": agent.DEFAULT_MODEL, "model_calls": 3, "tool_calls": 4,
         "prompt_tokens": 4628, "output_tokens": 1508, "calls_without_usage": 0}
V1_RESULT = {"data_updated_at": "2026-11-13T12:00", "recommendations": []}
client = TestClient(app)


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
    caplog.set_level("INFO", logger="navcare")
    client.get("/v1/recommendations", params=MONTREAL,
               headers={"X-Engine": "v1" if engine == "v1" else "v2"})
    (record,) = [r for r in caplog.records
                 if getattr(r, "fields", {}).get("event") == "recommendation"]
    return record.fields


# The model-cost fields in the log line


def test_known_cost_comes_from_the_estimator(monkeypatch, caplog):
    fields = log_line(monkeypatch, caplog)

    expected = cost_estimates.estimate_gemini_cost(USAGE, "global", datetime.now(main.MONTREAL))
    assert fields["model_cost_usd"] == pytest.approx(expected["estimated_usd"])
    assert "model_cost_unknown" not in fields


def test_v1_lines_have_no_cost_fields(monkeypatch, caplog):
    fields = log_line(monkeypatch, caplog, engine="v1", usage=None)

    assert "model_cost_usd" not in fields and "model_cost_unknown" not in fields


@pytest.mark.parametrize(("engine", "usage", "reason"), [
    ("v1_fallback", None, "no model usage recorded"),  # e.g. no model call was made
    ("v2", {**USAGE, "calls_without_usage": 1}, "1 of 3 model calls reported no tokens"),
    ("v2", {**USAGE, "prompt_tokens": 0, "output_tokens": 0}, "3 model calls but no tokens"),
])
def test_unknown_cost_is_logged_with_its_reason_never_as_zero(monkeypatch, caplog, engine, usage,
                                                              reason):
    fields = log_line(monkeypatch, caplog, engine=engine, usage=usage)

    assert "model_cost_usd" not in fields
    assert fields["model_cost_unknown"].startswith(reason)


def test_a_v2_503_counts_as_an_unknown_cost(monkeypatch, caplog):
    fields = log_line(monkeypatch, caplog, fail=RuntimeError("Route request failed"))

    assert (fields["status"], fields["engine"]) == (503, "v2")
    assert fields["model_cost_unknown"] == "no model usage recorded"


def test_an_unpriced_location_is_unknown(monkeypatch, caplog):
    monkeypatch.setenv(agent.LOCATION_ENV_VAR, "northamerica-northeast1")

    fields = log_line(monkeypatch, caplog)

    assert fields["model_cost_unknown"].startswith("no known price for model")


# Metric definitions


def test_every_metric_reads_this_services_recommendation_lines_by_engine():
    assert set(METRICS) == {"navcare_requests", "navcare_latency_ms", "navcare_prompt_tokens",
                            "navcare_output_tokens", "navcare_model_cost_usd",
                            "navcare_model_cost_unknown"}
    for metric in METRICS.values():
        assert metric["filter"].startswith(BASE_FILTER)
        assert metric["labelExtractors"]["engine"] == "EXTRACT(jsonPayload.engine)"


def test_metrics_only_use_fields_the_app_logs(monkeypatch, caplog):
    lines = [log_line(monkeypatch, caplog)]
    caplog.clear()
    lines.append(log_line(monkeypatch, caplog, engine="v1_fallback", usage=None))

    def has(fields, path):
        for key in path.split("."):
            if not isinstance(fields, dict) or key not in fields:
                return False
            fields = fields[key]
        return True

    for name, metric in METRICS.items():
        text = " ".join([metric["filter"], metric.get("valueExtractor", ""),
                         *metric["labelExtractors"].values()])
        for path in re.findall(r"jsonPayload\.([\w.]+)", text):
            assert any(has(line, path) for line in lines), f"{name}: {path} is never logged"


def test_metrics_follow_the_canary_rules():
    assert "status!=503" in METRICS["navcare_latency_ms"]["filter"]  # criteria 3 and 4
    assert "model_cost_usd>=0" in METRICS["navcare_model_cost_usd"]["filter"]  # known only
    assert "model_cost_unknown:*" in METRICS["navcare_model_cost_unknown"]["filter"]
    assert {5000, 30000} <= set(
        METRICS["navcare_latency_ms"]["bucketOptions"]["explicitBuckets"]["bounds"])
    assert 0.05 in METRICS["navcare_model_cost_usd"]["bucketOptions"]["explicitBuckets"]["bounds"]


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

    assert thresholds("Criterion 1: v2 failure rate (%), shown from 20 v2 requests") == [rate]
    assert thresholds("Criterion 1 (small steps): v2 failures") == [max_failures]
    assert thresholds("Requests that went to v2 (without 503s)") == [small_step]
    assert f">= {small_step})" in queries(
        "Criterion 1: v2 failure rate (%), shown from 20 v2 requests")[0]
    assert thresholds("Criterion 3: v2 p95 latency (v2 and v1_fallback, ms)") == [v2_p95_s * 1000]
    assert thresholds("Criterion 4: v1 p95 latency (ms)") == [v1_p95_s * 1000]
    assert thresholds("Criterion 6: estimated model-token cost per v2 request "
                      "(USD, known estimates only)") == [cost]


def test_v2_failures_count_fallbacks_and_500s_over_v2_routed_requests_without_503s():
    (rate,) = queries("Criterion 1: v2 failure rate (%), shown from 20 v2 requests")
    numerator, denominator = rate.split(" / ", 1)
    assert 'engine="v1_fallback",status!="503"' in numerator
    assert 'engine="v2",status="500"' in numerator
    assert 'engine=~"v2|v1_fallback",status!="503"' in denominator


def test_no_traffic_never_draws_a_pass():
    # Zero failures are drawn only when some request went to v2; rates need traffic to divide by.
    (failures,) = queries("Criterion 1 (small steps): v2 failures")
    assert failures.endswith('engine=~"v2|v1_fallback",status!="503"}[10m])) > 0)')
    # Elsewhere a zero fallback must sit over a traffic count with none, so no traffic is a gap.
    for title, chart in CHARTS.items():
        if "xyChart" in chart and title != "Criterion 1 (small steps): v2 failures":
            for query in queries(title):
                if "vector(0)" in query:
                    assert " / " in query, title
                    assert "vector(0)" not in query.split(" / ", 1)[1], title


def test_cost_chart_is_labelled_as_an_estimate_with_unknowns_shown():
    assert any("estimated model-token cost" in title for title in CHARTS)
    known_unknown = queries("Cost estimates: known vs unknown (requests that went to v2)")
    assert any("model_cost_unknown" in q for q in known_unknown)


def test_dashboard_name_matches_the_apply_script():
    script = (MONITORING / "apply.sh").read_text(encoding="utf-8")
    assert f'DASHBOARD_NAME="{DASHBOARD["displayName"]}"' in script


# apply.sh with a fake gcloud (no cloud calls)

FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *" logging metrics describe "*) exit "$FAKE_DESCRIBE_EXIT" ;;
  *" monitoring dashboards list "*)
    if [ "$FAKE_DESCRIBE_EXIT" = 0 ]; then echo "projects/1/dashboards/abc"; fi ;;
esac
exit 0
"""
BASH = shutil.which("bash") if os.name != "nt" or os.environ.get("MSYSTEM") else None


@pytest.mark.skipif(BASH is None, reason="needs bash (Git Bash on Windows)")
@pytest.mark.parametrize(("existing", "metric_verb", "dashboard_call"), [
    (False, "create", "monitoring dashboards create --config-from-file="),
    (True, "update", "monitoring dashboards update projects/1/dashboards/abc --config-from-file="),
])
def test_apply_creates_or_updates(tmp_path, existing, metric_verb, dashboard_call):
    fake = tmp_path / "gcloud"
    fake.write_text(FAKE_GCLOUD, newline="\n")
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {**os.environ, "GCLOUD": fake.as_posix(), "FAKE_LOG": log.as_posix(),
           "FAKE_DESCRIBE_EXIT": "0" if existing else "1"}
    env.pop("PROJECT_ID", None)

    result = subprocess.run([BASH, "deployment/monitoring/apply.sh"], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert all(c.startswith("--project=project-69a1b17d-f37d-485c-912 --quiet ") for c in calls)
    for name in METRICS:
        assert any(f"logging metrics {metric_verb} {name} --config-from-file=" in c
                   and c.endswith(f"metrics/{name}.yaml") for c in calls)
    assert sum(dashboard_call in c for c in calls) == 1
    assert not any("dashboards delete" in c or "metrics delete" in c for c in calls)
