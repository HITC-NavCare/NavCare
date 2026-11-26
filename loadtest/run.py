"""Load test for the canary: sends recommendation requests at a fixed rate and saves the results.

Each result is one JSON line in the format the correctness checker reads
(app/quality/correctness.py), with the client-side latency added. Lines are written as answers
arrive, so an interrupted run (Ctrl-C) keeps what it already paid for. At the end it prints the
error rate and p50/p95 latency per engine, then runs the correctness checker on the file.

    python -m loadtest.run --url http://localhost:8000 --engine v1 --rate 30 --duration 60

Cost: every request uses the real paid services, even against a local server. The Routes API
is billed per origin-hospital pair, and v2 makes up to MAX_MODEL_CALLS Gemini calls. So
--max-requests caps the run (default 20), the plan printed first counts every request including
v1 comparisons and the most Routes elements they can use, and a run of more than
SAFE_MAX_REQUESTS in all, or against a server that is not local, needs --yes. --dry-run prints
the plan and sends nothing.

Engines: --engine v1 or v2 forces one with the X-Engine header, which the server honours only
where ENGINE_HEADER_ENABLED=true. --engine split sends no header, so the canary split
(V2_TRAFFIC_PERCENT) decides, as for real users.

Comparison with v1: for v2 answers to requests without a message, the same request can also be
sent to v1 (forced with X-Engine) so the checker can compare them. It is on by default only for
--engine v2. With --engine split it is off: production does not honour X-Engine, so the
"v1" request would go through the split again (sometimes to v2), cost money and add traffic
the canary did not plan. --compare / --no-compare override the default.

API key: set NAVCARE_API_KEY and it is sent as X-API-Key on every request (never saved). The
service allows each key 30 requests per minute by default, and the v1 comparison requests count
too: keep --rate x (1 + comparisons per request) at or under the limit. The plan warns when not.

Results go to loadtest/results/ (ignored by Git) unless --out says otherwise.
"""

import argparse
import asyncio
import itertools
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.agent_v2.ai_agent_recommender import MAX_MODEL_CALLS, MAX_ROUTE_SEARCHES
from app.api.auth import DEFAULT_LIMIT_PER_MINUTE
from app.services.nearest_hospital_getter import MAX_CANDIDATES

RECOMMENDATIONS_PATH = "/v1/recommendations"
API_KEY_ENV_VAR = "NAVCARE_API_KEY"  # sent as X-API-Key (app/api/auth.py)
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Without --yes, a run may send at most this many requests in all, and only to a local server.
SAFE_MAX_REQUESTS = 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

# Longer than Cloud Run's request timeout (120 s, deployment/deploy.sh): a request the service
# gives up on is then its 504 in the results, which says the server failed, not a client timeout.
DEFAULT_TIMEOUT_S = 130.0

# The API's limits for radius_km (app/api/main.py): outside them every request gets a 422.
MIN_RADIUS_KM, MAX_RADIUS_KM = 1, 200

# Most Routes API elements (billed origin-hospital pairs) one request can use, with no
# drive-time cache. v1: one route matrix of up to MAX_CANDIDATES. v2: up to MAX_ROUTE_SEARCHES of
# them, plus one more when it falls back to v1 (orchestrator.build_agent_recommendations).
V1_MAX_ROUTES_ELEMENTS = MAX_CANDIDATES
V2_MAX_ROUTES_ELEMENTS = (MAX_ROUTE_SEARCHES + 1) * MAX_CANDIDATES
# Routes price per 1,000 elements once the 5,000 free ones of the month are used (docs/canary.md).
ROUTES_USD_PER_1000 = 10

# Places across Québec: a dense city (Montréal has more ERs within 50 km than MAX_CANDIDATES),
# mid-size cities and a rural area with few ERs.
LOCATIONS = {
    "montreal": (45.5017, -73.5673),
    "quebec-city": (46.8139, -71.2080),
    "sherbrooke": (45.4042, -71.8929),
    "gatineau": (45.4765, -75.7013),
    "mont-laurier": (46.5520, -75.4990),
}

# None: the most common request (v2 is then compared with v1). The others exercise what only
# v2 does: a children's ER, a mental-health ER, the call_911 rule (chest pain is a red flag in
# app/red_flags.py), and French and English.
MESSAGES = {
    "no-message": None,
    "child-fever-fr": "Mon enfant de 6 ans a une forte fièvre depuis hier",
    "mental-health-en": "I'm having a severe panic attack and I feel like I'm losing control",
    "chest-pain-en": "My father has chest pain and he is sweating a lot",
    "ankle-en": "I twisted my ankle playing soccer and it is swollen",
}


def scenarios():
    """Every (name, location, message) pair, in a fixed order, repeated forever."""
    pairs = [
        (f"{place}/{label}", LOCATIONS[place], message)
        for place in LOCATIONS
        for label, message in MESSAGES.items()
    ]
    return itertools.cycle(pairs)


def comparing(args):
    """Whether v2 answers without a message are also asked of v1 (see the module docstring)."""
    if args.compare is not None:
        return args.compare
    return args.engine == "v2"


def plan(args):
    """(load requests, most v1 comparison requests on top) for this run.

    Load requests: min(--max-requests, rate x duration). Comparisons: at most one per load
    request without a message, and only when comparing (v1 never needs comparing).
    """
    # Rounded first: 0.29 * 600 is 173.99999999999997 in floating point
    by_rate = math.floor(round(args.rate * args.duration / 60, 9))
    total = max(0, min(args.max_requests, by_rate))
    if not comparing(args) or args.engine == "v1":
        return total, 0
    names = scenarios()
    comparisons = sum(1 for _ in range(total) if next(names)[2] is None)
    return total, comparisons


def is_local(url):
    return urlsplit(url).hostname in LOCAL_HOSTS


def max_routes_elements(args, total, comparisons):
    """The most Routes elements this run can use. With split, as if every request went to v2."""
    per_request = V1_MAX_ROUTES_ELEMENTS if args.engine == "v1" else V2_MAX_ROUTES_ELEMENTS
    return total * per_request + comparisons * V1_MAX_ROUTES_ELEMENTS


def describe_plan(args, total, comparisons):
    compare = (f" plus up to {comparisons} v1 requests to compare v2 answers without a "
               "message," if comparisons else "")
    elements = max_routes_elements(args, total, comparisons)
    split = "; split: as if every request went to v2" if args.engine == "split" else ""
    return (
        f"Plan: {total} requests to {args.url} (engine: {args.engine}) at {args.rate:g}/min "
        f"for up to {args.duration:g} s,{compare} timeout {args.timeout:g} s.\n"
        f"Routes API: at most {elements:,} billed elements, with no drive-time cache "
        f"(v1: {V1_MAX_ROUTES_ELEMENTS} per request; v2: up to {V2_MAX_ROUTES_ELEMENTS}, "
        f"{MAX_ROUTE_SEARCHES} searches of {MAX_CANDIDATES} plus {MAX_CANDIDATES} if it falls "
        f"back to v1{split}). That is up to ${elements * ROUTES_USD_PER_1000 / 1000:,.2f} once "
        f"the 5,000 free elements of the month are used (${ROUTES_USD_PER_1000} per 1,000).\n"
        f"Gemini: up to {MAX_MODEL_CALLS} calls per v2 request."
    )


def requests_per_minute(args, total, comparisons):
    """Requests per minute on the API key, comparisons included (they use the same key)."""
    return args.rate * (total + comparisons) / total if total else 0


def rate_limit_warning(args, total, comparisons):
    """A warning when the run would go over the service's default per-key limit, else None.
    Over the limit the service answers 429, which counts as a failure of the run."""
    per_minute = requests_per_minute(args, total, comparisons)
    if per_minute <= DEFAULT_LIMIT_PER_MINUTE:
        return None
    return (f"Warning: about {per_minute:.0f} requests/min on one API key (comparisons "
            f"included) is over the service's default limit of {DEFAULT_LIMIT_PER_MINUTE}/min: "
            "the extra requests get 429 and count as failures. Lower --rate or use --no-compare.")


def needs_confirmation(args, total, comparisons):
    """Big runs (comparisons included) and runs against a server that is not local need --yes."""
    return total + comparisons > SAFE_MAX_REQUESTS or not is_local(args.url)


def served_engine(body):
    """The engine that answered, from the body's `engine` (v1, v2 or v1_fallback), or None."""
    if isinstance(body, dict) and isinstance(body.get("engine"), str) and body["engine"]:
        return body["engine"]
    return None


async def send(client, request, forced_engine, timeout):
    """One request. Returns a dict: status, body, engine, latency_ms, request_id, error, failure.

    A timeout or connection error gives status 0, which the checker counts as a non-200, and
    failure "timeout" or "connection". A 200 whose body is not JSON has failure "bad_body".
    """
    params = {key: value for key, value in request.items() if value is not None}
    headers = {"X-Engine": forced_engine} if forced_engine else {}
    result = {"status": 0, "body": None, "engine": None, "request_id": None,
              "error": None, "failure": None}
    started = time.perf_counter()
    try:
        response = await client.get(RECOMMENDATIONS_PATH, params=params, headers=headers,
                                    timeout=timeout)
    except httpx.HTTPError as error:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["error"] = f"{type(error).__name__}: {error}"
        result["failure"] = "timeout" if isinstance(error, httpx.TimeoutException) \
            else "connection"
        return result
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["status"] = response.status_code
    result["request_id"] = response.headers.get("X-Request-ID")
    try:
        result["body"] = response.json()
    except ValueError:
        result["error"] = "the response is not JSON"
        if response.status_code == 200:
            result["failure"] = "bad_body"
    result["engine"] = served_engine(result["body"])
    return result


async def run_one(client, index, name, request, args):
    forced = args.engine if args.engine in ("v1", "v2") else None
    started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    answer = await send(client, request, forced, args.timeout)
    record = {
        "index": index,
        "scenario": name,
        "started_at": started_at,
        "request": request,
        "status": answer["status"],
        "response": answer["body"],
        "latency_ms": answer["latency_ms"],
        "request_id": answer["request_id"],
        "served_engine": answer["engine"],
    }
    if forced:
        # Lets the checker count non-200s (whose body has no engine) under this engine.
        record["engine"] = forced
    if answer["error"]:
        record["error"] = answer["error"]
    if answer["failure"]:
        record["failure"] = answer["failure"]

    # Compare v2 with v1 for the same request, as the canary's correctness criterion asks.
    if (comparing(args) and answer["status"] == 200 and answer["engine"] == "v2"
            and not request.get("message")):
        v1 = await send(client, request, "v1", args.timeout)
        record["v1_status"] = v1["status"]
        record["v1_response"] = v1["body"]
        if v1["error"]:
            record["v1_error"] = v1["error"]
    return record


async def run(args, total, on_result, transport=None):
    """Send `total` requests, one every 60/rate seconds, without waiting for answers.

    A fixed start rate (not one request after another) keeps the load the same however slow
    the server is, as real users would: slow v2 answers overlap instead of slowing the test.
    on_result(record) is called as each answer arrives. Returns the number of requests that
    failed inside this script (a bug here, not an answer from the server); they are printed
    and not recorded.
    """
    interval = 60 / args.rate
    names = scenarios()
    client_errors = 0

    async def one(index, name, request):
        nonlocal client_errors
        try:
            on_result(await run_one(client, index, name, request, args))
        except Exception as error:  # one failure in this script must not lose the others
            client_errors += 1
            print(f"Request {index} ({name}) failed in the load test itself: "
                  f"{type(error).__name__}: {error}", file=sys.stderr)

    # No connection limit: with slow v2 answers, rate x answer time requests are open at once,
    # and a limit would make new ones wait here and add that wait to their latency.
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    tasks = []
    # The API key (app/api/auth.py) goes on every request; it is never printed or saved.
    api_key = os.environ.get(API_KEY_ENV_VAR)
    headers = {"X-API-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(base_url=args.url, transport=transport, limits=limits,
                                 headers=headers) as client:
        start = time.monotonic()
        for index in range(total):
            delay = start + index * interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            name, (lat, lon), message = next(names)
            request = {"lat": lat, "lon": lon, "limit": args.limit,
                       "radius_km": args.radius_km, "message": message}
            tasks.append(asyncio.create_task(one(index, name, request)))
        await asyncio.gather(*tasks)
    return client_errors


def percentile(values, share):
    """Nearest-rank percentile (share 0-100) of a list of numbers, or None if it is empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(share / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise(records):
    """Per engine: requests, failures by kind, error rate, p50 and p95 latency.

    Failed: a non-200, no answer at all (timeout or connection error), or a 200 whose body is
    not JSON. A 200 is counted under the engine that answered (v1_fallback is separate from
    v2; v2_routed puts them together). Anything else under the forced engine if there was one,
    else "unknown": the split decides on the server, and an error body does not say which
    engine failed (the request logs do).
    """
    rows = defaultdict(lambda: {"requests": 0, "non_200": 0, "timeouts": 0,
                                "connection_errors": 0, "bad_bodies": 0, "latencies": []})
    for record in records:
        served = record.get("served_engine") if record["status"] == 200 else None
        row = rows[served or record.get("engine") or "unknown"]
        row["requests"] += 1
        row["latencies"].append(record["latency_ms"])
        failure = record.get("failure")
        if failure == "timeout":
            row["timeouts"] += 1
        elif failure == "connection":
            row["connection_errors"] += 1
        elif failure == "bad_body":
            row["bad_bodies"] += 1
        elif record["status"] != 200:
            row["non_200"] += 1
    summary = {}
    for engine, row in sorted(rows.items()):
        failed = row["non_200"] + row["timeouts"] + row["connection_errors"] + row["bad_bodies"]
        summary[engine] = {
            "requests": row["requests"],
            "failed": failed,
            "error_rate_percent": round(100 * failed / row["requests"], 1),
            "non_200": row["non_200"],
            "timeouts": row["timeouts"],
            "connection_errors": row["connection_errors"],
            "bad_bodies": row["bad_bodies"],
            "p50_ms": percentile(row["latencies"], 50),
            "p95_ms": percentile(row["latencies"], 95),
        }
    return summary


def v2_routed(records):
    """Criterion 1 of docs/canary.md: v2 failures over the requests that went to v2, or None.

    Went to v2: a 200 from v2 or v1_fallback, or any answer to a request forced to v2. Failed:
    a v1 fallback (the person got an answer, but v2 failed), a non-200 or a 200 that is not JSON.
    Left out of both counts, as in the criterion: 503s (a shared data service failed, and v1
    gets them too) and timeouts or connection errors (no answer, so no engine; criterion 2
    counts them). With --engine split a non-200 does not say which engine failed, so only the
    request logs give the full count there.
    """
    requests = failed = fallbacks = 0
    for record in records:
        status = record["status"]
        served = record.get("served_engine") if status == 200 else None
        went_to_v2 = served in ("v2", "v1_fallback") or record.get("engine") == "v2"
        if (not went_to_v2 or status == 503
                or record.get("failure") in ("timeout", "connection")):
            continue
        requests += 1
        if served == "v1_fallback":
            fallbacks += 1
            failed += 1
        elif status != 200 or record.get("failure") == "bad_body":
            failed += 1
    if not requests:
        return None
    return {"requests": requests, "failed": failed, "fallbacks": fallbacks,
            "failure_rate_percent": round(100 * failed / requests, 1)}


def header_warnings(records):
    """Say when the server ignored X-Engine (ENGINE_HEADER_ENABLED not set there)."""
    warnings = []
    for record in records:
        forced, served = record.get("engine"), record.get("served_engine")
        if record["status"] == 200 and forced and served and served != forced \
                and not (forced == "v2" and served == "v1_fallback"):
            warnings.append(
                f"Asked for {forced} but {served} answered: the server ignores X-Engine. "
                "Set ENGINE_HEADER_ENABLED=true there, or use --engine split. The engine "
                "recorded for non-200 responses is wrong in this file."
            )
            break
    for record in records:
        v1 = record.get("v1_response")
        if isinstance(v1, dict) and v1.get("engine") not in (None, "v1"):
            warnings.append(
                f"A comparison request forced to v1 was answered by {v1['engine']}: the server "
                "ignores X-Engine, so v2 could not be compared with v1 (the checker counts "
                "these as not compared). Set ENGINE_HEADER_ENABLED=true there."
            )
            break
    return warnings


def print_summary(records):
    for engine, row in summarise(records).items():
        p50 = "n/a" if row["p50_ms"] is None else f"{row['p50_ms']:.0f} ms"
        p95 = "n/a" if row["p95_ms"] is None else f"{row['p95_ms']:.0f} ms"
        kinds = [f"{row['non_200']} non-200", f"{row['timeouts']} timeouts",
                 f"{row['connection_errors']} connection errors",
                 f"{row['bad_bodies']} 200s that are not JSON"]
        print(f"{engine}: {row['requests']} requests, {row['failed']} failed "
              f"({row['error_rate_percent']}%: {', '.join(kinds)}), p50 {p50}, p95 {p95}")
    routed = v2_routed(records)
    if routed:
        print(f"v2-routed (canary criterion 1): {routed['requests']} requests, "
              f"{routed['failed']} failed including {routed['fallbacks']} v1 fallbacks "
              f"({routed['failure_rate_percent']}%); 503s, timeouts and connection errors "
              "left out")
    for warning in header_warnings(records):
        print(f"\nWarning: {warning}")


def default_output(engine):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return RESULTS_DIR / f"results-{stamp}-{engine}.jsonl"


def positive_number(text):
    number = float(text)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"{text} is not a positive number")
    return number


def whole_number(text):
    number = int(text)
    if number < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return number


def radius(text):
    number = float(text)
    if not MIN_RADIUS_KM <= number <= MAX_RADIUS_KM:  # also False for nan
        raise argparse.ArgumentTypeError(
            f"{text} is not between {MIN_RADIUS_KM} and {MAX_RADIUS_KM} (the API's limits)"
        )
    return number


def percent(text):
    """Checked here, before anything is sent, with the same rule as the checker (0 to 100)."""
    number = float(text)
    if not 0 <= number <= 100:  # also False for nan
        raise argparse.ArgumentTypeError(f"{text} is not between 0 and 100")
    return text


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Load test /v1/recommendations for the canary and check the answers."
    )
    parser.add_argument("--url", default="http://localhost:8000",
                        help="API base URL (default http://localhost:8000)")
    parser.add_argument("--engine", choices=["v1", "v2", "split"], default="split",
                        help="force v1 or v2 with X-Engine, or let the canary split decide "
                             "(default split)")
    parser.add_argument("--rate", type=positive_number, default=30,
                        help="requests started per minute (default 30)")
    parser.add_argument("--duration", type=positive_number, default=60,
                        help="seconds to keep starting requests (default 60)")
    parser.add_argument("--max-requests", type=whole_number, default=SAFE_MAX_REQUESTS,
                        help=f"most load requests to send (default {SAFE_MAX_REQUESTS}); "
                             "v1 comparison requests come on top and are shown in the plan")
    parser.add_argument("--timeout", type=positive_number, default=DEFAULT_TIMEOUT_S,
                        help=f"seconds before a request counts as a timeout (default "
                             f"{DEFAULT_TIMEOUT_S:g}, longer than Cloud Run's 120 s, so a "
                             "request the service gives up on is its 504, not a timeout)")
    parser.add_argument("--limit", type=int, choices=range(1, 11), metavar="1-10",
                        help="recommendations per answer (default: the API's, 4). Below 4 "
                             "the v1 comparison gets stricter")
    parser.add_argument("--radius-km", type=radius,
                        help=f"search radius, {MIN_RADIUS_KM}-{MAX_RADIUS_KM} "
                             "(default: the API's)")
    compare = parser.add_mutually_exclusive_group()
    compare.add_argument("--compare", dest="compare", action="store_true", default=None,
                         help="ask v1 the same question for v2 answers without a message "
                              "(default: only with --engine v2)")
    compare.add_argument("--no-compare", dest="compare", action="store_false",
                         help="never ask v1 for a comparison")
    parser.add_argument("--out", type=Path, help="results file (default loadtest/results/...)")
    parser.add_argument("--min-percent", type=percent, default="100",
                        help="passed to the correctness checker, 0-100 (default 100)")
    parser.add_argument("--no-check", action="store_true",
                        help="do not run the correctness checker at the end")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    parser.add_argument("--yes", action="store_true",
                        help=f"confirm a run of more than {SAFE_MAX_REQUESTS} requests or "
                             "against a server that is not local")
    return parser.parse_args(argv)


def main(argv=None, transport=None):
    """Exit code: the correctness checker's (0 pass, 1 fail), or 0 with --no-check;
    1 when the run was interrupted; 2 when it was not started (nothing to send, or --yes
    missing)."""
    args = parse_args(argv)
    total, comparisons = plan(args)
    print(describe_plan(args, total, comparisons))
    warning = rate_limit_warning(args, total, comparisons)
    if warning:
        print(warning, file=sys.stderr)
    if args.dry_run:
        return 0
    if total == 0:
        print("Nothing to send: --max-requests or rate x duration is 0.", file=sys.stderr)
        return 2
    if needs_confirmation(args, total, comparisons) and not args.yes:
        print(f"Not started: more than {SAFE_MAX_REQUESTS} requests, or a server that is not "
              "local, costs real money. Check the plan and add --yes.", file=sys.stderr)
        return 2

    path = args.out or default_output(args.engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    interrupted = False
    with open(path, "w", encoding="utf-8") as file:
        def save(record):
            records.append(record)
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()  # on disk now, even if the run is stopped

        try:
            client_errors = asyncio.run(run(args, total, save, transport))
        except KeyboardInterrupt:
            interrupted, client_errors = True, 0

    records.sort(key=lambda record: record["index"])
    print(f"\n{'Interrupted: saved' if interrupted else 'Saved'} {len(records)} of {total} "
          f"results to {path}")
    if client_errors:
        print(f"{client_errors} requests failed in the load test itself and are not in the file.")
    print_summary(records)
    if interrupted:
        print("\nThe run was interrupted: the step is incomplete, so no correctness verdict.")
        return 1
    if args.no_check:
        return 0

    # Imported here: the checker needs requirements-dev.txt (jsonschema, PyYAML).
    from app.quality import correctness

    print("\nCorrectness:")
    return correctness.main([str(path), "--min-percent", args.min_percent])


if __name__ == "__main__":
    sys.exit(main())
