"""Load test for the canary: sends recommendation requests at a fixed rate and saves the results.

Each result is one JSON line in the format the correctness checker reads
(app/quality/correctness.py), with the client-side latency added. At the end it prints the
error rate and p50/p95 latency per engine, then runs the correctness checker on the file.

    python -m loadtest.run --url http://localhost:8000 --engine v1 --rate 30 --duration 60

Cost: every request uses the real paid services, even against a local server. The Routes API
is billed per origin-hospital pair (up to MAX_CANDIDATES per request, more when v2 widens its
search), and v2 makes up to MAX_MODEL_CALLS Gemini calls. So the run is capped by
--max-requests (default 20), and anything bigger, or against a server that is not local, needs
--yes after reading the plan it prints. --dry-run prints the plan and sends nothing.

Engines: --engine v1 or v2 forces one with the X-Engine header, which the server honours only
where ENGINE_HEADER_ENABLED=true. --engine split sends no header, so the canary split
(V2_TRAFFIC_PERCENT) decides, as for real users. For v2 answers to requests without a message,
the same request is also sent to v1 (forced) so the checker can compare them; --no-compare
turns that off. That also needs ENGINE_HEADER_ENABLED=true; the run warns when the server
ignored the header.

Results go to loadtest/results/ (ignored by Git) unless --out says otherwise.
"""

import argparse
import asyncio
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.agent_v2.ai_agent_recommender import MAX_MODEL_CALLS
from app.services.nearest_hospital_getter import MAX_CANDIDATES

RECOMMENDATIONS_PATH = "/v1/recommendations"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Without --yes, a run may send at most this many requests, and only to a local server.
SAFE_MAX_REQUESTS = 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

# The API answers within about 60 s for v2, plus the v1 fallback.
DEFAULT_TIMEOUT_S = 90.0

# If the API stops returning `engine` in the body (so clients cannot tell the versions apart),
# it can still say which engine answered in this header where ENGINE_HEADER_ENABLED=true.
ENGINE_SERVED_HEADER = "X-Engine-Served"

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


def plan(args):
    """How many requests the run sends: min(--max-requests, rate x duration)."""
    by_rate = math.floor(args.rate * args.duration / 60)
    return max(0, min(args.max_requests, by_rate))


def is_local(url):
    return urlsplit(url).hostname in LOCAL_HOSTS


def describe_plan(args, total):
    compare = "" if args.no_compare or args.engine == "v1" else (
        f" plus up to {total} v1 calls to compare v2 answers without a message,"
    )
    return (
        f"Plan: {total} requests to {args.url} (engine: {args.engine}) at {args.rate:g}/min "
        f"for up to {args.duration:g} s,{compare} timeout {args.timeout:g} s.\n"
        f"Each request uses the Routes API for up to {MAX_CANDIDATES} origin-hospital pairs "
        f"(v2 can search twice), and each v2 request up to {MAX_MODEL_CALLS} Gemini calls."
    )


def needs_confirmation(args, total):
    """Big runs and runs against a server that is not local need --yes."""
    return total > SAFE_MAX_REQUESTS or not is_local(args.url)


def served_engine(response, body):
    """The engine that answered: the header if the API sends it, else the body's `engine`."""
    engine = response.headers.get(ENGINE_SERVED_HEADER)
    if not engine and isinstance(body, dict) and isinstance(body.get("engine"), str):
        engine = body["engine"]
    return engine or None


async def send(client, request, forced_engine, timeout):
    """One request. Returns (status, body, engine served, latency in ms, request id, error).

    A timeout or connection error gives status 0, which the checker counts as a non-200.
    """
    params = {key: value for key, value in request.items() if value is not None}
    headers = {"X-Engine": forced_engine} if forced_engine else {}
    started = time.perf_counter()
    try:
        response = await client.get(RECOMMENDATIONS_PATH, params=params, headers=headers,
                                    timeout=timeout)
    except httpx.HTTPError as error:
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return 0, None, None, latency_ms, None, f"{type(error).__name__}: {error}"
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        body = response.json()
        error = None
    except ValueError:
        body, error = None, "the response is not JSON"
    return (response.status_code, body, served_engine(response, body), latency_ms,
            response.headers.get("X-Request-ID"), error)


async def run_one(client, name, request, args):
    forced = args.engine if args.engine in ("v1", "v2") else None
    started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    status, body, engine, latency_ms, request_id, error = await send(
        client, request, forced, args.timeout
    )
    record = {
        "scenario": name,
        "started_at": started_at,
        "request": request,
        "status": status,
        "response": body,
        "latency_ms": latency_ms,
        "request_id": request_id,
        "served_engine": engine,
    }
    if forced:
        # Lets the checker count non-200s (whose body has no engine) under this engine.
        record["engine"] = forced
    if error:
        record["error"] = error

    # Compare v2 with v1 for the same request, as the canary's correctness criterion asks.
    if (not args.no_compare and status == 200 and engine == "v2"
            and not request.get("message")):
        v1_status, v1_body, _, _, _, v1_error = await send(client, request, "v1", args.timeout)
        record["v1_status"] = v1_status
        record["v1_response"] = v1_body
        if v1_error:
            record["v1_error"] = v1_error
    return record


async def run(args, total, transport=None):
    """Send `total` requests, one every 60/rate seconds, without waiting for answers.

    A fixed start rate (not one request after another) keeps the load the same however slow
    the server is, as real users would: slow v2 answers overlap instead of slowing the test.
    """
    interval = 60 / args.rate
    names = scenarios()
    tasks = []
    async with httpx.AsyncClient(base_url=args.url, transport=transport) as client:
        start = time.monotonic()
        for index in range(total):
            delay = start + index * interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            name, (lat, lon), message = next(names)
            request = {"lat": lat, "lon": lon, "limit": args.limit,
                       "radius_km": args.radius_km, "message": message}
            tasks.append(asyncio.create_task(run_one(client, name, request, args)))
        return list(await asyncio.gather(*tasks))


def percentile(values, share):
    """Nearest-rank percentile (share 0-100) of a list of numbers, or None if it is empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(share / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise(records):
    """Per engine: requests, non-200 (timeouts included), error rate, p50 and p95 latency.

    A 200 is counted under the engine that answered (v1_fallback is separate from v2). A
    non-200 under the forced engine if there was one, else "unknown": the split decides on the
    server, and the error body does not say which engine failed (the request logs do).
    """
    rows = defaultdict(lambda: {"requests": 0, "non_200": 0, "timeouts": 0, "latencies": []})
    for record in records:
        served = record["served_engine"] if record["status"] == 200 else None
        engine = served or record.get("engine") or "unknown"
        row = rows[engine]
        row["requests"] += 1
        row["latencies"].append(record["latency_ms"])
        if record["status"] != 200:
            row["non_200"] += 1
        if record["status"] == 0:
            row["timeouts"] += 1
    summary = {}
    for engine, row in sorted(rows.items()):
        summary[engine] = {
            "requests": row["requests"],
            "non_200": row["non_200"],
            "timeouts": row["timeouts"],
            "error_rate_percent": round(100 * row["non_200"] / row["requests"], 1),
            "p50_ms": percentile(row["latencies"], 50),
            "p95_ms": percentile(row["latencies"], 95),
        }
    return summary


def header_warnings(records):
    """Say when the server ignored X-Engine (ENGINE_HEADER_ENABLED not set there)."""
    warnings = []
    for record in records:
        forced, served = record.get("engine"), record["served_engine"]
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


def write_results(records, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


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
                        help=f"hard cap on requests sent (default {SAFE_MAX_REQUESTS})")
    parser.add_argument("--timeout", type=positive_number, default=DEFAULT_TIMEOUT_S,
                        help=f"seconds before a request counts as a timeout "
                             f"(default {DEFAULT_TIMEOUT_S:g})")
    parser.add_argument("--limit", type=int, choices=range(1, 11), metavar="1-10",
                        help="recommendations per answer (default: the API's, 4). Below 4 "
                             "the v1 comparison gets stricter")
    parser.add_argument("--radius-km", type=positive_number,
                        help="search radius (default: the API's)")
    parser.add_argument("--no-compare", action="store_true",
                        help="do not ask v1 the same question for v2 answers without a message")
    parser.add_argument("--out", type=Path, help="results file (default loadtest/results/...)")
    parser.add_argument("--min-percent", default="100",
                        help="passed to the correctness checker (default 100)")
    parser.add_argument("--no-check", action="store_true",
                        help="do not run the correctness checker at the end")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    parser.add_argument("--yes", action="store_true",
                        help=f"confirm a run of more than {SAFE_MAX_REQUESTS} requests or "
                             "against a server that is not local")
    return parser.parse_args(argv)


def main(argv=None, transport=None):
    """Exit code: the correctness checker's (0 pass, 1 fail), or 0 with --no-check;
    2 when the run was not started (nothing to send, or --yes missing)."""
    args = parse_args(argv)
    total = plan(args)
    print(describe_plan(args, total))
    if args.dry_run:
        return 0
    if total == 0:
        print("Nothing to send: --max-requests or rate x duration is 0.", file=sys.stderr)
        return 2
    if needs_confirmation(args, total) and not args.yes:
        print(f"Not started: more than {SAFE_MAX_REQUESTS} requests, or a server that is not "
              "local, costs real money. Check the plan and add --yes.", file=sys.stderr)
        return 2

    records = asyncio.run(run(args, total, transport))
    path = args.out or default_output(args.engine)
    write_results(records, path)

    print(f"\nSaved {len(records)} results to {path}")
    for engine, row in summarise(records).items():
        p50 = "n/a" if row["p50_ms"] is None else f"{row['p50_ms']:.0f} ms"
        p95 = "n/a" if row["p95_ms"] is None else f"{row['p95_ms']:.0f} ms"
        print(f"{engine}: {row['requests']} requests, {row['non_200']} non-200 "
              f"({row['error_rate_percent']}%, {row['timeouts']} timeouts), "
              f"p50 {p50}, p95 {p95}")
    for warning in header_warnings(records):
        print(f"\nWarning: {warning}")
    if args.no_check:
        return 0

    # Imported here: the checker needs requirements-dev.txt (jsonschema, PyYAML).
    from app.quality import correctness

    print("\nCorrectness:")
    return correctness.main([str(path), "--min-percent", args.min_percent])


if __name__ == "__main__":
    sys.exit(main())
