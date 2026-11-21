"""Correctness checker for /v1/recommendations responses (canary criterion: correctness).

An agent can return a successful (200) answer that is wrong, so the error rate alone is not
enough. check_response() returns the list of problems found in one response; an empty list
means the response is correct. The rules:

Schema
- The response matches RecommendationResponse in docs/openapi.yaml.

Data consistency (against data/hospitals.csv and the response's own numbers)
- Every hospital id is a known MSSS id, and its name, address and coordinates match our data.
- No hospital is listed twice, or both recommended and unranked.
- Ranks are 1, 2, 3... in order, with no more recommendations than the requested limit.
- Every recommended hospital is within radius_km (straight line) of the origin, and the origin
  is the requested location.
- expected_minutes = travel_minutes + estimated_wait_minutes, and the estimated wait agrees
  with the average stay and occupancy it was computed from (v1's formula).
- ER numbers are either all present or all null (null only allowed for v2).

Rules each engine must follow
- No children's, mental-health or cardiology ER is recommended by v1, or by v2 without a
  message. "specialised" is only given as an unranked cause for those ERs.
- call_911 is true when the message has a clear emergency phrase (app/red_flags.py). v2 must
  always decide it (true or false); v1 leaves it null otherwise.
- v1 (and v1_fallback) say in `note` when they did not read a message.
- model_usage is set for v2 (1 to MAX_MODEL_CALLS model calls) and null for v1.
- v1 is ordered by expected_minutes, lowest first. v2 may order differently.

Compared with v1 (only for v2 requests without a message, since v2 reads the message)
- At least MIN_TOP_OVERLAP of v1's top hospitals are also in v2's top hospitals, for the
  same request. Different wording or order is not an error.

Dev and canary tool: it needs requirements-dev.txt (jsonschema, PyYAML) and docs/openapi.yaml,
which are not in the Docker image, so the API must not import it.

Check a batch of results (one JSON object per line, as written by the load test):
    python -m app.quality.correctness results.jsonl

Each line: {"request": {"lat", "lon", "limit", "radius_km", "message"}, "status": 200,
            "response": {...}, "v1_response": {...}}   (v1_response is optional)
"""

import argparse
import csv
import json
import sys
from collections import Counter
from functools import cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from app import red_flags
from app.agent_v2.ai_agent_recommender import MAX_MODEL_CALLS
from app.recommender_v1 import rules_based_recommender as recommender
from app.services.nearest_hospital_getter import HOSPITALS_CSV_PATH, straight_line_km

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"
RESPONSE_SCHEMA = "RecommendationResponse"

# Each number in a response is rounded on its own (travel to 0.1 min, wait to 1 min), so sums
# can differ from the rounded total by up to about 1 minute.
MINUTES_TOLERANCE = 1.0
RADIUS_TOLERANCE_KM = 0.1
COORDINATE_TOLERANCE = 1e-6
# v2 passes the v1 comparison when at least this many of v1's top hospitals are in its top
# (fewer if v1 itself recommended fewer). A starting value: the team sets the threshold.
MIN_TOP_OVERLAP = 2
TOP_K = recommender.DEFAULT_LIMIT

ER_FIELDS = [
    "estimated_wait_minutes", "expected_minutes", "occupancy_rate",
    "avg_ambulatory_stay_hours",
]
V1_ENGINES = ("v1", "v1_fallback")


@cache
def load_hospitals():
    """Our hospital data, keyed by MSSS id: {id: {"name", "address", "lat", "lon"}}."""
    with open(HOSPITALS_CSV_PATH, encoding="utf-8", newline="") as file:
        return {
            row["id"]: {
                "name": row["name"],
                "address": row["address"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
            }
            for row in csv.DictReader(file)
        }


def openapi_to_json_schema(node):
    """Turn an OpenAPI 3.0 schema into JSON Schema: `nullable: true` becomes a null type.

    jsonschema does not know `nullable`, so without this a null field would fail the check.
    """
    if isinstance(node, list):
        return [openapi_to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {key: openapi_to_json_schema(value) for key, value in node.items()
                 if key != "nullable"}
    if node.get("nullable"):
        if "type" in converted:
            converted["type"] = [converted["type"], "null"]
            if "enum" in converted:
                converted["enum"] = [*converted["enum"], None]
        else:  # a $ref or allOf: allow null alongside it
            converted = {"anyOf": [converted, {"type": "null"}]}
    return converted


@cache
def response_validator():
    """Validator for RecommendationResponse, with $refs resolved inside the spec."""
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    schemas = openapi_to_json_schema(spec["components"]["schemas"])
    schema = {
        "$ref": f"#/components/schemas/{RESPONSE_SCHEMA}",
        "components": {"schemas": schemas},
    }
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_problems(response):
    problems = []
    for error in sorted(response_validator().iter_errors(response), key=lambda e: list(e.path)):
        where = "/".join(str(part) for part in error.path) or "(top level)"
        problems.append(f"schema: {where}: {error.message}")
    return problems


def check_response(request, response, v1_response=None):
    """Return the problems found in one 200 response; an empty list means it is correct.

    request: what was asked, {"lat", "lon", "limit", "radius_km", "message"} (limit,
    radius_km and message are optional). v1_response: v1's answer to the same request,
    used for the v1 comparison of v2 answers without a message.
    """
    problems = schema_problems(response)
    if problems:
        return problems  # the checks below assume the documented shape

    engine = response["engine"]
    message = request.get("message")
    has_message = bool(message and message.strip())
    recommendations = response["recommendations"]

    problems += _check_origin(request, response)
    problems += _check_recommendations(request, response, engine, has_message)
    problems += _check_unranked(response)
    problems += _check_call_911(engine, message, response["call_911"])
    problems += _check_note(engine, has_message, response["note"])
    problems += _check_model_usage(engine, response["model_usage"])
    if engine in V1_ENGINES:
        # A missing total is reported above (v1 never ranks without ER data).
        totals = [entry["expected_minutes"] for entry in recommendations
                  if entry["expected_minutes"] is not None]
        if totals != sorted(totals):
            problems.append("v1 recommendations are not ordered by expected_minutes")
    if engine == "v2" and not has_message and v1_response is not None:
        problems += compare_with_v1(response, v1_response)
    return problems


def _check_origin(request, response):
    origin = response["origin"]
    for key in ("lat", "lon"):
        if key in request and abs(origin[key] - request[key]) > COORDINATE_TOLERANCE:
            return [f"origin {origin} is not the requested location"]
    return []


def _check_recommendations(request, response, engine, has_message):
    problems = []
    hospitals = load_hospitals()
    recommendations = response["recommendations"]
    limit = request.get("limit", recommender.DEFAULT_LIMIT)
    if len(recommendations) > limit:
        problems.append(f"{len(recommendations)} recommendations for a limit of {limit}")

    ranks = [entry["rank"] for entry in recommendations]
    if ranks != list(range(1, len(recommendations) + 1)):
        problems.append(f"ranks {ranks} are not 1, 2, 3... in order")

    ids = [entry["hospital"]["id"] for entry in recommendations]
    for hospital_id, count in Counter(ids).items():
        if count > 1:
            problems.append(f"hospital {hospital_id} is recommended {count} times")

    for entry in recommendations:
        hospital = entry["hospital"]
        hospital_id = hospital["id"]
        problems += _check_hospital(hospital, hospitals)
        if hospital_id in recommender.SPECIALISED_ER_IDS:
            if engine in V1_ENGINES:
                problems.append(f"{engine} recommended specialised ER {hospital_id}")
            elif not has_message:
                problems.append(f"specialised ER {hospital_id} recommended without a message")
        problems += _check_distance(response, hospital)
        problems += _check_numbers(entry, engine)
        if not entry["reason"].strip():
            problems.append(f"hospital {hospital_id} has an empty reason")
    return problems


def _check_hospital(hospital, hospitals):
    hospital_id = hospital["id"]
    known = hospitals.get(hospital_id)
    if known is None:
        return [f"unknown hospital id {hospital_id!r}"]
    problems = []
    for key in ("name", "address"):
        if key in hospital and hospital[key] != known[key]:
            problems.append(
                f"hospital {hospital_id} {key} {hospital[key]!r} does not match our data "
                f"{known[key]!r}"
            )
    if (abs(hospital["lat"] - known["lat"]) > COORDINATE_TOLERANCE
            or abs(hospital["lon"] - known["lon"]) > COORDINATE_TOLERANCE):
        problems.append(f"hospital {hospital_id} coordinates do not match our data")
    return problems


def _check_distance(response, hospital):
    radius_km = response.get("radius_km")
    if radius_km is None:
        return []
    origin = response["origin"]
    distance = straight_line_km(origin["lat"], origin["lon"], hospital["lat"], hospital["lon"])
    if distance > radius_km + RADIUS_TOLERANCE_KM:
        return [
            f"hospital {hospital['id']} is {distance:.1f} km away, outside radius_km {radius_km}"
        ]
    return []


def _check_numbers(entry, engine):
    """The numbers in one recommendation agree with each other."""
    hospital_id = entry["hospital"]["id"]
    problems = []
    if entry["travel_minutes"] < 0 or entry["distance_km"] < 0:
        problems.append(f"hospital {hospital_id} has a negative travel time or distance")

    missing = [field for field in ER_FIELDS if entry[field] is None]
    if missing:
        if engine in V1_ENGINES:
            problems.append(f"{engine} ranked hospital {hospital_id} without ER data")
        elif len(missing) != len(ER_FIELDS):
            problems.append(f"hospital {hospital_id} has some ER numbers but not {missing}")
        return problems

    total = entry["travel_minutes"] + entry["estimated_wait_minutes"]
    if abs(entry["expected_minutes"] - total) > MINUTES_TOLERANCE:
        problems.append(
            f"hospital {hospital_id}: expected_minutes {entry['expected_minutes']} is not "
            f"travel_minutes + estimated_wait_minutes ({total:.1f})"
        )
    computed_wait = recommender.estimated_er_minutes({
        "avgStayAmbulatoryHours": entry["avg_ambulatory_stay_hours"],
        "occupancyPct": entry["occupancy_rate"] * 100,
    })
    if abs(entry["estimated_wait_minutes"] - computed_wait) > MINUTES_TOLERANCE:
        problems.append(
            f"hospital {hospital_id}: estimated_wait_minutes {entry['estimated_wait_minutes']} "
            f"does not match its average stay and occupancy ({computed_wait:.0f})"
        )
    return problems


def _check_unranked(response):
    problems = []
    hospitals = load_hospitals()
    recommended = {entry["hospital"]["id"] for entry in response["recommendations"]}
    seen = set()
    for item in response.get("unranked_nearby") or []:
        hospital = item["hospital"]
        hospital_id = hospital["id"]
        problems += _check_hospital(hospital, hospitals)
        if hospital_id in recommended:
            problems.append(f"hospital {hospital_id} is both recommended and unranked")
        if hospital_id in seen:
            problems.append(f"hospital {hospital_id} is unranked twice")
        seen.add(hospital_id)
        if item["cause"] == "specialised" and hospital_id not in recommender.SPECIALISED_ER_IDS:
            problems.append(f"general ER {hospital_id} is unranked as specialised")
    return problems


def _check_call_911(engine, message, call_911):
    if red_flags.mentions_emergency(message) and call_911 is not True:
        return ["the message has an emergency phrase but call_911 is not true"]
    if engine == "v2" and call_911 is None:
        return ["v2 did not decide call_911 (null)"]
    if engine in V1_ENGINES and call_911 is not None and not red_flags.mentions_emergency(message):
        return [f"{engine} cannot read the message, so call_911 should be null"]
    return []


def _check_note(engine, has_message, note):
    if engine in V1_ENGINES and has_message and not note:
        return [f"{engine} did not say that the message was not read"]
    if engine in V1_ENGINES and not has_message and note is not None:
        return [f"{engine} has a note but there was no message"]
    return []


def _check_model_usage(engine, usage):
    if engine == "v1":
        return [] if usage is None else ["v1 response has model_usage"]
    if usage is None:
        return ["v2 response has no model_usage"] if engine == "v2" else []
    calls = usage.get("model_calls")
    if calls is None or not 0 <= calls <= MAX_MODEL_CALLS:
        return [f"model_usage.model_calls {calls} is not between 0 and {MAX_MODEL_CALLS}"]
    if engine == "v2" and calls == 0:
        return ["v2 answered with 0 model calls"]
    return []


def compare_with_v1(response, v1_response, top_k=TOP_K, min_overlap=MIN_TOP_OVERLAP):
    """v2's top hospitals must include enough of v1's top hospitals for the same request."""
    v1_top = [entry["hospital"]["id"] for entry in v1_response["recommendations"][:top_k]]
    v2_top = {entry["hospital"]["id"] for entry in response["recommendations"][:top_k]}
    needed = min(min_overlap, len(v1_top))
    shared = len(v2_top.intersection(v1_top))
    if shared < needed:
        return [
            f"only {shared} of v1's top {len(v1_top)} ({', '.join(v1_top)}) are in v2's top "
            f"{top_k}; at least {needed} expected"
        ]
    return []


def check_record(record):
    """Check one load-test result. Non-200 results are not checked: correct is None.

    They are counted by the availability criterion instead.
    """
    status = record.get("status")
    response = record.get("response") or {}
    result = {
        "engine": response.get("engine", "unknown"),
        "status": status,
        "correct": None,
        "problems": [],
    }
    if status != 200:
        return result
    problems = check_response(record.get("request", {}), response, record.get("v1_response"))
    result["correct"] = not problems
    result["problems"] = problems
    return result


def summarise(results):
    """Per engine: responses checked, correct, % correct, and non-200 responses skipped."""
    summary = {}
    for result in results:
        row = summary.setdefault(result["engine"],
                                 {"checked": 0, "correct": 0, "not_checked": 0})
        if result["correct"] is None:
            row["not_checked"] += 1
            continue
        row["checked"] += 1
        row["correct"] += result["correct"]
    for row in summary.values():
        row["percent_correct"] = (
            round(100 * row["correct"] / row["checked"], 1) if row["checked"] else None
        )
    return summary


def read_records(path):
    """Read a JSON Lines file. Raises ValueError naming the line that is not valid JSON."""
    records = []
    with open(path, encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}, line {line_number}: not valid JSON ({error})") from error
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check load-test results for correctness, per engine."
    )
    parser.add_argument("results", help="JSON Lines file, one request and response per line")
    parser.add_argument("--show", type=int, default=10,
                        help="number of incorrect responses to print (default 10)")
    args = parser.parse_args(argv)

    try:
        records = read_records(args.results)
    except (OSError, ValueError) as error:
        print(f"Could not read results: {error}", file=sys.stderr)
        return 2

    results = [check_record(record) for record in records]
    for engine, row in sorted(summarise(results).items()):
        percent = "n/a" if row["percent_correct"] is None else f"{row['percent_correct']}%"
        print(f"{engine}: {row['correct']}/{row['checked']} correct ({percent}), "
              f"{row['not_checked']} non-200 not checked")

    wrong = [(i, r) for i, r in enumerate(results, start=1) if r["correct"] is False]
    for line_number, result in wrong[:args.show]:
        print(f"\nline {line_number} ({result['engine']}):")
        for problem in result["problems"]:
            print(f"  - {problem}")
    if len(wrong) > args.show:
        print(f"\n... and {len(wrong) - args.show} more incorrect responses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
