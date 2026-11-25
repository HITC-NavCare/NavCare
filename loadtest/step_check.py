"""Step check for the canary: PASS or FAIL for each criterion in docs/canary.md, and what to do.

Run it after every step, on that step's load-test results and its log lines from Cloud Logging:

    python -m loadtest.step_check results.jsonl --percent 10 --logs logs.json --baseline step0.jsonl

- results: the load test's results.jsonl for the step. Pass two files to evaluate a step that
  was run again because it had too few v2 requests (the hold rule in docs/canary.md).
- --percent: the step's V2_TRAFFIC_PERCENT (0 for the baseline step).
- --logs: the step's request log lines, from `gcloud logging read ... --format=json` (run
  without --logs to print the command for the step's time window), or the JSON lines a local
  server prints. Results are matched to log lines by request id, so other traffic is left out.
  The log lines say which engine served every request, including errors, and give the server
  latency and the model cost. Without them, criteria 1 and 3 use the results alone (an error
  that does not say its engine leaves criterion 1 not checked), and criterion 6 is not checked.
  Criterion 6 is also not checked (so hold) when any v2 request's cost is unknown, unless the
  known costs alone are already over the limit.
- --baseline: the 0 % step's results, for criterion 2 ("at most 1 error more than the baseline").
- --publish: also write the step's correctness counts (criterion 5) to Cloud Logging with
  gcloud, one entry per group, so the dashboard can chart them and the criterion 5 alert can
  fire. Their timestamp is when they are written, so publish right after the step. Each step
  is published once: the entries carry a step id from the results, and a run whose step is
  already in Cloud Logging (the re-run with --logs, say) does not write it again, since the
  counts would add up and could fire an alert for a step that passed.

Decision: ROLLBACK if any criterion whose action is rollback fails; else HOLD if one whose
action is hold fails, a criterion could not be checked, or the step had fewer than
MIN_V2_REQUESTS v2 requests; else PROMOTE, once criterion 7 (billing, checked by hand) passes.
In the 0 % step there is nothing to roll back, so every failure means hold.

Exit code: 0 promote, 1 rollback, 3 hold, 2 when a file cannot be read. A failed --publish is
reported but does not change it: the decision still stands.
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

from app.quality import correctness
from loadtest.run import percentile

STEPS = (0, 10, 25, 50, 100)  # V2_TRAFFIC_PERCENT of each step in docs/canary.md
MIN_V2_REQUESTS = 8           # fewer: hold and run the step again
SMALL_STEP = 20               # fewer v2 requests or answers: count failures instead of a %
V2_FAILURE_MAX_PERCENT = 5
USER_ERROR_MAX_PERCENT = 1
V2_P95_MAX_MS = 30_000
V1_P95_MAX_MS = 5_000
V2_MIN_CORRECT_PERCENT = 95
V2_MAX_COST_USD = 0.05
SPEND_LIMIT_USD = 40

PASS, FAIL, NOT_APPLICABLE, NOT_CHECKED, BY_HAND = (
    "PASS", "FAIL", "n/a", "NOT CHECKED", "CHECK BY HAND")
PROMOTE, HOLD, ROLLBACK = "PROMOTE", "HOLD", "ROLLBACK"
EXIT_CODES = {PROMOTE: 0, ROLLBACK: 1, HOLD: 3}
V2_ENGINES = ("v2", "v1_fallback")  # the request went to v2 (a fallback is a v2 failure)
DATA_UNAVAILABLE = 503
CORRECTNESS_LOG = "navcare-canary"        # the Cloud Logging log --publish writes to
CORRECTNESS_EVENT = "canary_correctness"  # jsonPayload.event of those entries


class InputError(Exception):
    """A file that cannot be read."""


# ---------------------------------------------------------------- reading the inputs

def read_results(paths):
    """Records from the load test's results files, in order. Raises InputError."""
    records = []
    for path in paths:
        try:
            records += [record for _, record in correctness.read_records(path)]
        except (OSError, ValueError) as error:
            raise InputError(f"Could not read results {path}: {error}") from error
    return records


def read_logs(path):
    """{request_id: log line} for the recommendation log lines in a file.

    Accepts `gcloud logging read --format=json` output (a JSON list of entries, the line in
    jsonPayload) or JSON lines as a local server prints them. Other lines are skipped.
    """
    try:
        with open(path, encoding="utf-8-sig") as file:
            text = file.read()
    except (OSError, ValueError) as error:
        raise InputError(f"Could not read logs {path}: {error}") from error
    if text.lstrip().startswith("["):
        try:
            entries = json.loads(text)
        except ValueError as error:
            raise InputError(f"Could not read logs {path}: not valid JSON ({error})") from error
    else:
        entries = []
        for line in text.splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # uvicorn's own lines, for example
    lines = {}
    for entry in entries:
        payload = entry.get("jsonPayload", entry) if isinstance(entry, dict) else None
        if (isinstance(payload, dict) and payload.get("event") == "recommendation"
                and isinstance(payload.get("request_id"), str)):
            lines[payload["request_id"]] = payload
    return lines


def status_of(record):
    """The HTTP status a result got, 0 when there was no HTTP response."""
    if not isinstance(record, dict):
        return None
    return correctness._status(record.get("status")) or 0


def requests_of(records, logs):
    """One row per result: what the person got (status) and which engine served it.

    The engine comes from the log line when there is one, else from a 200's body. A non-200
    without a log line has engine None: the split chose on the server and the error body does
    not say. latency_ms is the server's when logged (as docs/canary.md measures it), else the
    client's.
    """
    rows = []
    for record in records:
        status = status_of(record)
        if status is None:
            continue  # unreadable line: criterion 5 reports it
        line = (logs or {}).get(record.get("request_id"))
        served = record.get("served_engine") if status == 200 else None
        engine = line.get("engine") if line else served
        latency = line.get("latency_ms") if line else None
        rows.append({
            "status": status,
            "engine": engine if engine in ("v1", *V2_ENGINES) else None,
            "logged": line is not None,
            "latency_ms": latency if latency is not None else record.get("latency_ms"),
            "chosen_by": line.get("chosen_by") if line else None,
            "cost_usd": line.get("model_cost_usd") if line else None,
            "cost_unknown": bool(line) and "model_cost_unknown" in line,
            "started_at": record.get("started_at"),
        })
    return rows


# ---------------------------------------------------------------- the criteria

def criterion(number, name, status, measured, threshold, action=None, note=None, cell="n/a"):
    """One criterion's result. cell: its value in the canary log table (docs/canary.md)."""
    return {"number": number, "name": name, "status": status, "measured": measured,
            "threshold": threshold, "action": action if status == FAIL else None, "note": note,
            "cell": cell}


def went_to_v2(rows):
    """Criterion 1's requests: served by v2 or its fallback, without 503s."""
    return [row for row in rows
            if row["engine"] in V2_ENGINES and row["status"] != DATA_UNAVAILABLE]


def is_v2_failure(row):
    return row["engine"] == "v1_fallback" or row["status"] != 200


def small_or_percent(failed, total, max_percent):
    """True when within the threshold: at most 1 in a small step, else at most max_percent."""
    if total < SMALL_STEP:
        return failed <= 1
    return 100 * failed <= max_percent * total


def v2_threshold(max_percent, what):
    return f"≤ {max_percent} % (at most 1 {what} under {SMALL_STEP} v2 requests)"


def check_v2_failures(rows, percent, any_503):
    name, threshold = "v2 failure rate", v2_threshold(V2_FAILURE_MAX_PERCENT, "failure")
    v2 = went_to_v2(rows)
    failed = sum(is_v2_failure(row) for row in v2)
    fallbacks = sum(row["engine"] == "v1_fallback" for row in v2)
    # A server error that no log line or body ties to an engine may have been v2's.
    unattributed = sum(row["engine"] is None and row["status"] not in (0, 200, DATA_UNAVAILABLE)
                       for row in rows)
    if percent == 0 and not v2:
        return criterion(1, name, NOT_APPLICABLE, "no v2 requests", threshold)
    if not v2 and not unattributed:
        return criterion(1, name, NOT_CHECKED, "no v2 requests", threshold,
                         note="no request went to v2")
    cell = fmt_percent(failed, len(v2))
    measured = f"{failed} of {len(v2)} ({cell}), {fallbacks} fallbacks"
    action = HOLD if any_503 else ROLLBACK
    note = ("the step also has 503s: a shared data service is failing, so hold and investigate "
            "before rolling back") if any_503 else None
    if not small_or_percent(failed, len(v2), V2_FAILURE_MAX_PERCENT):
        return criterion(1, name, FAIL, measured, threshold, action, note, cell)
    if unattributed and not small_or_percent(failed + unattributed, len(v2) + unattributed,
                                             V2_FAILURE_MAX_PERCENT):
        return criterion(1, name, NOT_CHECKED, f"{measured}, {unattributed} errors of unknown "
                         "engine", threshold,
                         note=f"{unattributed} server errors have no log line saying which "
                         "engine failed; pass --logs")
    return criterion(1, name, PASS, measured, threshold, cell=cell)


def user_errors(rows):
    return sum(row["status"] == 0 or row["status"] >= 500 for row in rows)


def check_user_errors(rows, percent, baseline_rows):
    name = "User-facing error rate"
    threshold = f"≤ {USER_ERROR_MAX_PERCENT} % and at most 1 more than the baseline"
    errors, total = user_errors(rows), len(rows)
    cell = fmt_percent(errors, total)
    measured = f"{errors} of {total} ({cell})"
    if not total:
        return criterion(2, name, NOT_CHECKED, "no requests", threshold, note="no results")
    within_rate = 100 * errors <= USER_ERROR_MAX_PERCENT * total
    if percent == 0:
        threshold = f"≤ {USER_ERROR_MAX_PERCENT} % (this is the baseline)"
        return criterion(2, name, PASS if within_rate else FAIL, measured, threshold, ROLLBACK,
                         cell=cell)
    if not within_rate:
        return criterion(2, name, FAIL, measured, threshold, ROLLBACK, cell=cell)
    if baseline_rows is None:
        return criterion(2, name, NOT_CHECKED, measured, threshold,
                         note="pass --baseline with the 0 % step's results", cell=cell)
    base_errors, base_total = user_errors(baseline_rows), len(baseline_rows)
    if not base_total:
        return criterion(2, name, NOT_CHECKED, measured, threshold,
                         note="the baseline file has no requests")
    # Same load at every step; a step run twice is compared with the baseline scaled to it.
    allowed = base_errors * total / base_total + 1
    measured += f", baseline {base_errors} of {base_total}"
    return criterion(2, name, PASS if errors <= allowed else FAIL, measured, threshold, ROLLBACK,
                     cell=cell)


def check_p95(number, name, rows, limit_ms, action, applicable):
    threshold = f"≤ {limit_ms / 1000:g} s"
    latencies = [row["latency_ms"] for row in rows
                 if isinstance(row["latency_ms"], (int, float))]
    if not applicable and not latencies:
        return criterion(number, name, NOT_APPLICABLE, "no requests", threshold)
    if not latencies:
        return criterion(number, name, NOT_CHECKED, "no requests", threshold,
                         note=f"no {name.split()[0]} requests with a latency")
    p95 = percentile(latencies, 95)
    source = "server" if all(row["logged"] for row in rows) else "client"
    cell = f"{p95 / 1000:.1f} s"
    measured = f"{cell} over {len(latencies)} ({source})"
    return criterion(number, name, PASS if p95 <= limit_ms else FAIL, measured, threshold, action,
                     cell=cell)


def correctness_counts(records):
    """({"v2": (checked, correct), "v1": (checked, correct)}, unreadable lines) for criterion 5.

    A fallback is v1's code answering, so its answers count with v1's and must be right like
    v1's.
    """
    summary = correctness.summarise([correctness.check_line(record) for record in records])
    bad = sum(row["bad_records"] for row in summary.values())

    def totals(engines):
        checked = sum(summary.get(engine, {}).get("checked", 0) for engine in engines)
        correct = sum(summary.get(engine, {}).get("correct", 0) for engine in engines)
        return checked, correct

    return {"v2": totals(["v2"]), "v1": totals(["v1", "v1_fallback"])}, bad


def check_correctness(counts, bad, percent):
    name = "Correctness and data consistency"
    threshold = (f"v2 ≥ {V2_MIN_CORRECT_PERCENT} % (at most 1 wrong under {SMALL_STEP} v2 "
                 "answers), v1 = 100 %")
    v2_checked, v2_correct = counts["v2"]
    v1_checked, v1_correct = counts["v1"]
    cell = (fmt_percent(v2_correct, v2_checked), fmt_percent(v1_correct, v1_checked))
    measured = (f"v2 {v2_correct}/{v2_checked} ({cell[0]}), "
                f"v1 {v1_correct}/{v1_checked} ({cell[1]})")
    v2_wrong = v2_checked - v2_correct
    v2_ok = small_or_percent(v2_wrong, v2_checked, 100 - V2_MIN_CORRECT_PERCENT)
    if v2_checked and not v2_ok:
        return criterion(5, name, FAIL, measured, threshold, ROLLBACK,
                         note=f"{v2_wrong} wrong v2 answers; see "
                         "`python -m app.quality.correctness <results>`", cell=cell)
    if v1_correct < v1_checked:
        return criterion(5, name, FAIL, measured, threshold, HOLD,
                         note="v1 below 100 %: a checker or data problem, not a v2 one", cell=cell)
    if bad:
        return criterion(5, name, NOT_CHECKED, measured, threshold,
                         note=f"{bad} result lines could not be checked", cell=cell)
    if percent > 0 and not v2_checked:
        return criterion(5, name, NOT_CHECKED, measured, threshold, note="no v2 answer to check",
                         cell=cell)
    if not v1_checked and not v2_checked:
        return criterion(5, name, NOT_CHECKED, measured, threshold, note="no answer to check")
    return criterion(5, name, PASS, measured, threshold, cell=cell)


def check_cost(rows, percent, have_logs):
    name, threshold = "v2 model cost per request", f"≤ ${V2_MAX_COST_USD:.2f} on average"
    v2 = [row for row in rows if row["engine"] in V2_ENGINES]  # a fallback still paid
    if percent == 0 and not v2:
        return criterion(6, name, NOT_APPLICABLE, "no v2 requests", threshold)
    if not have_logs:
        return criterion(6, name, NOT_CHECKED, "no log lines", threshold,
                         note="the cost is in the log lines; pass --logs")
    known = [row["cost_usd"] for row in v2 if isinstance(row["cost_usd"], (int, float))]
    unknown = sum(row["cost_unknown"] for row in v2)
    if not known:
        note = (f"all {unknown} v2 log lines have model_cost_unknown" if unknown else
                "the log lines have no model_cost_usd: deploy a revision that logs it "
                "(#41, #45)")
        return criterion(6, name, NOT_CHECKED, "unknown", threshold, note=note)
    average = math.fsum(known) / len(known)
    cell = f"${average:.4f}"
    measured = f"{cell} over {len(known)}"
    if unknown:
        measured += f", {unknown} unknown"
        cell += " (incomplete)"
    if average > V2_MAX_COST_USD:  # over the limit on the known costs alone: roll back
        return criterion(6, name, FAIL, measured, threshold, ROLLBACK, cell=cell)
    if unknown:  # a pass on part of the requests is not a pass, as on the dashboard (#45)
        return criterion(6, name, NOT_CHECKED, measured, threshold,
                         note=f"{unknown} v2 requests have no cost estimate (for example a 503, a "
                         "500 or a failed model call): hold and investigate", cell=cell)
    return criterion(6, name, PASS, measured, threshold, cell=cell)


def check_spend():
    return criterion(7, "Total spend", BY_HAND, "billing report", f"< ${SPEND_LIMIT_USD}, "
                     "no budget alert", note="check the billing report and budget alert emails; "
                     "if violated: deployment/canary.sh rollback, and stop the canary")


# ---------------------------------------------------------------- the decision

def evaluate(records, percent, logs=None, baseline_records=None):
    """Every criterion, the warnings and the decision for one step."""
    rows = requests_of(records, logs)
    baseline_rows = None if baseline_records is None else requests_of(baseline_records, None)
    v1_rows = [row for row in rows if row["engine"] == "v1" and row["status"] != DATA_UNAVAILABLE]
    any_503 = any(row["status"] == DATA_UNAVAILABLE for row in rows)
    v2_count = len(went_to_v2(rows))
    counts, bad = correctness_counts(records)

    criteria = [
        check_v2_failures(rows, percent, any_503),
        check_user_errors(rows, percent, baseline_rows),
        check_p95(3, "v2 p95 latency", went_to_v2(rows), V2_P95_MAX_MS, ROLLBACK, percent > 0),
        check_p95(4, "v1 p95 latency (control)", v1_rows, V1_P95_MAX_MS, HOLD, percent < 100),
        check_correctness(counts, bad, percent),
        check_cost(rows, percent, logs is not None),
        check_spend(),
    ]
    if percent == 0:  # nothing to roll back in the baseline: fix it before step 1
        for item in criteria:
            if item["action"] == ROLLBACK:
                item["action"] = HOLD

    reasons = []
    for item in criteria:
        if item["status"] == FAIL:
            reasons.append(f"criterion {item['number']} failed ({item['name']})")
        elif item["status"] == NOT_CHECKED:
            reasons.append(f"criterion {item['number']} not checked: {item['note']}")
    enough = percent == 0 or v2_count >= MIN_V2_REQUESTS
    if not enough:
        reasons.append(f"only {v2_count} v2 requests (minimum {MIN_V2_REQUESTS}): run the same "
                       "step again for 10 minutes and pass both results files")
    if any(item["action"] == ROLLBACK for item in criteria):
        decision = ROLLBACK
    elif reasons:
        decision = HOLD
    else:
        decision = PROMOTE

    return {"percent": percent, "requests": len(rows), "v2_requests": v2_count,
            "logged": sum(row["logged"] for row in rows), "criteria": criteria,
            "decision": decision, "reasons": reasons, "warnings": warnings_for(rows, percent),
            "first_started_at": first_started_at(rows), "correctness": counts,
            "step_id": step_id(records, percent)}


def step_id(records, percent):
    """The same id for the same step's results, however often the check runs on them.

    A re-run step (two results files) gets a new id, since its counts are new.
    """
    lines = sorted(json.dumps(record, sort_keys=True) for record in records)
    text = "\n".join([str(percent), *lines])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def warnings_for(rows, percent):
    warnings = []
    routed = [row for row in rows if row["engine"]]
    on_v2 = sum(row["engine"] in V2_ENGINES for row in routed)
    if percent == 0 and on_v2:
        warnings.append(f"{on_v2} requests went to v2 in a 0 % step: is V2_TRAFFIC_PERCENT 0? "
                        "(deployment/canary.sh status)")
    if percent == 100 and on_v2 < len(routed):
        warnings.append(f"{len(routed) - on_v2} requests went to v1 in a 100 % step: is "
                        "V2_TRAFFIC_PERCENT 100? (deployment/canary.sh status)")
    header = sum(row["chosen_by"] == "header" for row in rows)
    if header:
        warnings.append(f"{header} requests were routed by the X-Engine header, not the split. "
                        "Canary steps use --engine split, with ENGINE_HEADER_ENABLED off.")
    missing = sum(not row["logged"] and row["status"] != 0 for row in rows)
    if any(row["logged"] for row in rows) and missing:
        warnings.append(f"{missing} answered requests have no log line (fetched too early, or a "
                        "--limit too small?); they use the results alone")
    return warnings


def next_step(percent):
    later = [step for step in STEPS if step > percent]
    return later[0] if later else None


def action_line(result):
    percent, decision = result["percent"], result["decision"]
    if decision == ROLLBACK:
        return "deployment/canary.sh rollback   (then turn off any fault: canary.sh fault off)"
    if decision == HOLD:
        return "stay at this step; fix or re-run as below, then check again"
    after = next_step(percent)
    if after is None:
        return "v2 serves all traffic: the canary is complete (if criterion 7 passes)"
    return f"deployment/canary.sh set {after}   (if criterion 7 passes)"


# ---------------------------------------------------------------- output

def fmt_percent(part, whole):
    return f"{100 * part / whole:.1f} %" if whole else "n/a"


def first_started_at(rows):
    times = [row["started_at"] for row in rows if isinstance(row["started_at"], str)]
    return min(times) if times else None


def step_label(percent):
    return f"{STEPS.index(percent)} ({percent} %)" if percent in STEPS else f"{percent} %"


def log_row(result):
    """A row for the canary log table in docs/canary.md. Spend so far is filled in by hand."""
    started = result["first_started_at"]
    when = started[:16].replace("T", " ") + " UTC" if started else "(date and time)"
    cells = [item["cell"] for item in result["criteria"]]
    correct = cells[4] if isinstance(cells[4], tuple) else ("n/a", "n/a")
    row = [when, step_label(result["percent"]), str(result["v2_requests"]), cells[0], cells[1],
           cells[2], cells[3], correct[0], correct[1], cells[5], "(fill in)",
           result["decision"].capitalize()]
    return "| " + " | ".join(row) + " |"


def print_report(result, out=None):
    out = out or sys.stdout
    print(f"Canary step check: V2_TRAFFIC_PERCENT={result['percent']}, {result['requests']} "
          f"requests, {result['logged']} matched to log lines", file=out)
    minimum = f", minimum {MIN_V2_REQUESTS}" if result["percent"] else ""
    print(f"Went to v2: {result['v2_requests']} (503s left out){minimum}\n", file=out)
    print(f"{'#':<2} {'Criterion':<34} {'Measured':<44} {'Result':<16} Threshold", file=out)
    for item in result["criteria"]:
        result_text = item["status"] + (f" → {item['action'].lower()}" if item["action"] else "")
        print(f"{item['number']:<2} {item['name']:<34} {item['measured']:<44} {result_text:<16} "
              f"{item['threshold']}", file=out)
    notes = [item for item in result["criteria"] if item["note"]]
    if notes:
        print("", file=out)
    for item in notes:
        print(f"  {item['number']}: {item['note']}", file=out)
    for warning in result["warnings"]:
        print(f"\nWarning: {warning}", file=out)
    print(f"\nDecision: {result['decision']}", file=out)
    for reason in result["reasons"]:
        print(f"  - {reason}", file=out)
    print(f"Next: {action_line(result)}", file=out)
    print("\nRow for the canary log in docs/canary.md:", file=out)
    print(log_row(result), file=out)


def logs_command(records):
    """The gcloud command that fetches the log lines for the results' time window."""
    times = []
    for record in records:
        try:
            times.append(datetime.fromisoformat(record["started_at"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not times:
        return None
    # A request may take up to Cloud Run's 120 s timeout after it starts.
    start = (min(times) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (max(times) + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    project, service = project_and_service()
    query = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
             f'AND jsonPayload.event="recommendation" AND timestamp>="{start}" '
             f'AND timestamp<="{end}"')
    return f"gcloud logging read '{query}' --project={project} --format=json > logs.json"


def project_and_service():
    """The Google Cloud project and Cloud Run service, as deploy.sh and apply.sh name them."""
    project = os.environ.get("PROJECT_ID", "project-69a1b17d-f37d-485c-912")
    service = os.environ.get("SERVICE", f"navcare-api-{os.environ.get('ENV', 'dev')}")
    return project, service


def correctness_entries(result, service):
    """The log entries --publish writes: one per group with answers (v2, and v1 with fallbacks).

    The dashboard's correctness tile and the criterion 5 alert read them through the
    correctness_checked and correctness_correct log-based metrics (deployment/monitoring/).
    """
    return [{"event": CORRECTNESS_EVENT, "service": service, "engine": group,
             "step_percent": result["percent"], "step_id": result["step_id"],
             "checked": checked, "correct": correct}
            for group, (checked, correct) in result["correctness"].items() if checked]


def published_query(project, service, step):
    """The Cloud Logging query for entries --publish already wrote for this step."""
    return (f'logName="projects/{project}/logs/{CORRECTNESS_LOG}" AND '
            f'jsonPayload.event="{CORRECTNESS_EVENT}" AND jsonPayload.service="{service}" AND '
            f'jsonPayload.step_id="{step}"')


def publish_correctness(result, run=None):
    """Write the step's correctness counts to Cloud Logging with gcloud, unless this step is
    already there. Returns (the entries for the step, whether they were written now).

    Raises OSError when gcloud can't be started, subprocess.CalledProcessError when it fails.
    """
    run = run or subprocess.run
    project, service = project_and_service()
    gcloud = os.environ.get("GCLOUD", "gcloud")
    executable = shutil.which(gcloud) or gcloud  # gcloud.cmd on Windows
    entries = correctness_entries(result, service)
    if not entries:
        return entries, False
    found = run([executable, f"--project={project}", "--quiet", "logging", "read",
                 published_query(project, service, result["step_id"]), "--freshness=7d",
                 "--limit=1", "--format=value(insertId)"],
                check=True, capture_output=True, text=True)
    if (found.stdout or "").strip():
        return entries, False
    for entry in entries:
        run([executable, f"--project={project}", "--quiet", "logging", "write",
             CORRECTNESS_LOG, json.dumps(entry), "--payload-type=json"],
            check=True, capture_output=True, text=True)
    return entries, True


def step_percent(text):
    """argparse type: a whole number from 0 to 100."""
    if not text.isdigit() or int(text) > 100:
        raise argparse.ArgumentTypeError(f"{text} is not a whole number from 0 to 100")
    return int(text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PASS or FAIL for each canary criterion in docs/canary.md, and the decision."
    )
    parser.add_argument("results", nargs="+", help="the step's results.jsonl (two for a re-run)")
    parser.add_argument("--percent", type=step_percent, required=True,
                        help="the step's V2_TRAFFIC_PERCENT (0 for the baseline)")
    parser.add_argument("--logs", help="the step's log lines (gcloud logging read "
                        "--format=json, or a local server's JSON lines)")
    parser.add_argument("--baseline", help="the 0 %% step's results.jsonl (criterion 2)")
    parser.add_argument("--publish", action="store_true",
                        help="also write the correctness counts to Cloud Logging (gcloud), for "
                        "the dashboard's criterion 5 tile and alert")
    args = parser.parse_args(argv)

    try:
        records = read_results(args.results)
        logs = read_logs(args.logs) if args.logs else None
        baseline = read_results([args.baseline]) if args.baseline else None
    except InputError as error:
        print(error, file=sys.stderr)
        return 2

    result = evaluate(records, args.percent, logs, baseline)
    print_report(result)
    if logs is None:
        command = logs_command(records)
        if command:
            print("\nFetch the log lines for this step (a minute or two after it ends), then run "
                  "again with --logs logs.json:\n  " + command)
    if args.publish:
        publish(result)
    return EXIT_CODES[result["decision"]]


def publish(result):
    """--publish, with what happened printed. A failure is reported; the decision stands."""
    try:
        entries, written = publish_correctness(result)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or error
        print(f"Could not publish the correctness counts (the decision above still stands; "
              f"run again with --publish to retry): {detail}", file=sys.stderr)
        return
    counts = ", ".join(f"{e['engine']} {e['correct']}/{e['checked']}" for e in entries)
    if not entries:
        print("\nPublished correctness to Cloud Logging: no answers to publish")
    elif written:
        print(f"\nPublished correctness to Cloud Logging: {counts}")
    else:
        print(f"\nNot published again: this step ({result['step_id']}) is already in Cloud "
              f"Logging ({counts}), and publishing it twice would double its counts.")


if __name__ == "__main__":
    sys.exit(main())
