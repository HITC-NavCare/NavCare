"""Correctness checker for /v1/recommendations responses (canary criterion: correctness).

An agent can return a successful (200) answer that is wrong, so the error rate alone is not
enough. check_response() returns the list of problems found in one response; an empty list
means the response is correct. The rules:

Schema
- The response matches RecommendationResponse in docs/openapi.yaml, including its formats
  (uuid, date-time).

Data consistency (against data/hospitals.csv and the response's own numbers)
- Every hospital id is a known MSSS id, and its name, address and coordinates match our data.
- No hospital is listed twice, or both recommended and unranked.
- Ranks are 1, 2, 3... in order, with no more recommendations than the requested limit.
- The origin is the requested location. v1 (and v1_fallback) use the requested radius_km and
  say so; v2 gives the radius it searched, at most the agent's MAX_RADIUS_KM.
- Every recommended hospital is within that radius (straight line) of the origin.
- expected_minutes = travel_minutes + estimated_wait_minutes, and the estimated wait agrees
  with the average stay and occupancy it was computed from (v1's formula).
- ER numbers are either all present or all null (null only allowed for v2). When they are all
  null, patients_waiting and patients_over_24h are null too.

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
  same request. Different wording or order is not an error. When v1's answer is missing or
  is not a valid v1 response (for example a 503), v2 is not compared; the summary counts it.

Dev and canary tool: it needs requirements-dev.txt (jsonschema, PyYAML, rfc3339-validator) and
docs/openapi.yaml, which are not in the Docker image, so the API must not import it.

Check a batch of results (one JSON object per line, as written by the load test):
    python -m app.quality.correctness results.jsonl [--min-percent 95]

Each line: {"request": {"lat", "lon", "limit", "radius_km", "message"}, "status": 200,
            "response": {...}, "v1_response": {...}, "engine": "v2"}
lat and lon are required; limit, radius_km and message may be missing or null (the API
defaults are used). v1_response is optional. engine is optional: the engine the load test
forced with the X-Engine header, used to count non-200 responses, whose error body does not
say which engine failed. Record it only where ENGINE_HEADER_ENABLED=true: elsewhere the
canary split ignores the header, and the engine would be wrong. status may be a number
(200 or 200.0) or a string like "200". A line that cannot be
checked (not valid JSON, bad request values, a 200 body that is not an object) is reported as
a bad record, with its line number in the file, instead of stopping the run.

Exit code: 0 when every engine's % correct is at least --min-percent (default 100), every
engine named in the file (in a response or as the forced engine) has at least one 200 of its
own to check, and there are no bad records; 1 otherwise, including for a file with nothing
to check; 2 when the file cannot be opened.
"""

import argparse
import csv
import json
import math
import sys
from collections import Counter
from functools import cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from app import red_flags
from app.agent_v2.ai_agent_recommender import MAX_MODEL_CALLS, MAX_RADIUS_KM
from app.recommender_v1 import rules_based_recommender as recommender
from app.services.nearest_hospital_getter import (
    DEFAULT_RADIUS_KM,
    HOSPITALS_CSV_PATH,
    straight_line_km,
)

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
# Live counts that can be null on their own (the MSSS feed may leave them out), but must be
# null when the ER has no data at all.
ER_COUNT_FIELDS = ["patients_waiting", "patients_over_24h"]
V1_ENGINES = ("v1", "v1_fallback")


class RecordError(ValueError):
    """A load-test record that cannot be checked. A problem with the record, not the response."""


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
    """Validator for RecommendationResponse, with $refs resolved inside the spec.

    `format` is checked too (uuid, and date-time through rfc3339-validator).
    """
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    schemas = openapi_to_json_schema(spec["components"]["schemas"])
    schema = {
        "$ref": f"#/components/schemas/{RESPONSE_SCHEMA}",
        "components": {"schemas": schemas},
    }
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def schema_problems(response):
    problems = []
    for error in sorted(response_validator().iter_errors(response), key=lambda e: list(e.path)):
        where = "/".join(str(part) for part in error.path) or "(top level)"
        problems.append(f"schema: {where}: {error.message}")
    return problems


def parse_request(request):
    """The request with the API's defaults filled in. Raises RecordError for bad values.

    lat and lon are required. limit, radius_km and message may be missing or null. Numbers
    may be strings, as when copied from query parameters.
    """
    if not isinstance(request, dict):
        raise RecordError("request is not a JSON object")
    limit = _number(request, "limit", recommender.DEFAULT_LIMIT)
    if limit != int(limit):
        raise RecordError(f"request limit {request['limit']!r} is not a whole number")
    message = request.get("message")
    if message is not None and not isinstance(message, str):
        raise RecordError(f"request message {message!r} is not text")
    return {
        "lat": _number(request, "lat"),
        "lon": _number(request, "lon"),
        "limit": int(limit),
        "radius_km": _number(request, "radius_km", DEFAULT_RADIUS_KM),
        "message": message,
    }


def _number(request, key, default=None):
    value = request.get(key)
    if value is None:
        if default is None:
            raise RecordError(f"request has no {key}")
        return default
    if isinstance(value, bool):
        raise RecordError(f"request {key} {value!r} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RecordError(f"request {key} {value!r} is not a number") from None
    if not math.isfinite(number):  # NaN would pass every comparison
        raise RecordError(f"request {key} {value!r} is not a finite number")
    return number


def is_v1_answer(v1_response):
    """True when v1_response is a valid v1 response that v2 can be compared with."""
    return (isinstance(v1_response, dict) and v1_response.get("engine") in V1_ENGINES
            and not schema_problems(v1_response))


def check_response(request, response, v1_response=None):
    """Return the problems found in one 200 response; an empty list means it is correct.

    request: what was asked, {"lat", "lon", "limit", "radius_km", "message"} (limit,
    radius_km and message are optional). v1_response: v1's answer to the same request,
    used for the v1 comparison of v2 answers without a message; skipped when it is not a
    valid v1 response. Raises RecordError when the request cannot be read.
    """
    request = parse_request(request)
    problems = schema_problems(response)
    if problems:
        return problems  # the checks below assume the documented shape

    engine = response["engine"]
    message = request["message"]
    has_message = bool(message and message.strip())
    recommendations = response["recommendations"]

    problems += _check_origin(request, response)
    radius_problems, radius_km = _check_radius(request, response, engine)
    problems += radius_problems
    problems += _check_recommendations(request, response, engine, has_message, radius_km)
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
    if engine == "v2" and not has_message and is_v1_answer(v1_response):
        problems += compare_with_v1(response, v1_response)
    return problems


def _check_origin(request, response):
    origin = response["origin"]
    for key in ("lat", "lon"):
        if abs(origin[key] - request[key]) > COORDINATE_TOLERANCE:
            return [f"origin {origin} is not the requested location"]
    return []


def _check_radius(request, response, engine):
    """Return (problems, the radius the recommendations must be within).

    v1 must use the requested radius. v2 chooses its own, up to MAX_RADIUS_KM.
    """
    radius_km = response.get("radius_km")
    if engine in V1_ENGINES:
        requested = request["radius_km"]
        if radius_km is None:
            return [f"{engine} response has no radius_km"], requested
        if abs(radius_km - requested) > RADIUS_TOLERANCE_KM:
            problem = f"{engine} used radius_km {radius_km:g}, not the requested {requested:g}"
            return [problem], requested
        return [], requested
    if radius_km is None:
        problems = (["v2 recommended hospitals but gave no radius_km"]
                    if response["recommendations"] else [])
        return problems, MAX_RADIUS_KM
    if radius_km > MAX_RADIUS_KM + RADIUS_TOLERANCE_KM:
        return [f"v2 radius_km {radius_km} is more than the maximum {MAX_RADIUS_KM}"], radius_km
    return [], radius_km


def _check_recommendations(request, response, engine, has_message, radius_km):
    problems = []
    hospitals = load_hospitals()
    recommendations = response["recommendations"]
    limit = request["limit"]
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
        problems += _check_distance(response["origin"], hospital, radius_km)
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


def _check_distance(origin, hospital, radius_km):
    distance = straight_line_km(origin["lat"], origin["lon"], hospital["lat"], hospital["lon"])
    if distance > radius_km + RADIUS_TOLERANCE_KM:
        return [
            f"hospital {hospital['id']} is {distance:.1f} km away, outside radius_km {radius_km:g}"
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
        else:
            counts = [field for field in ER_COUNT_FIELDS if entry[field] is not None]
            if counts:
                problems.append(f"hospital {hospital_id} has no ER data but has {counts}")
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
    """Check one load-test result. Never raises.

    Non-200 results are not checked (correct is None): the availability criterion counts
    them. A record that cannot be checked has bad=True and its reason in problems.
    v1_comparison_skipped is True for a v2 answer without a message whose v1_response is
    there but is not a valid v1 response. forced_engine is the record's engine field.
    """
    result = {"engine": "unknown", "forced_engine": None, "status": None, "correct": None,
              "problems": [], "bad": False, "v1_comparison_skipped": False}
    if not isinstance(record, dict):
        return bad_record("record is not a JSON object")

    response = record.get("response")
    # The response says which engine answered (v1_fallback included). An error body does
    # not, so use the engine the load test forced, if it recorded one.
    engine = _engine_name(response.get("engine") if isinstance(response, dict) else None)
    result["forced_engine"] = forced = _engine_name(record.get("engine"))
    result["engine"] = engine or forced or "unknown"
    result["status"] = status = _status(record.get("status"))
    if status is None:
        return {**result, "bad": True,
                "problems": [f"bad record: status {record.get('status')!r} is not an HTTP "
                             "status code"]}
    if status != 200:
        return result
    if not isinstance(response, dict):
        return {**result, "bad": True, "problems": ["the 200 response is not a JSON object"]}

    v1_response = record.get("v1_response")
    try:
        problems = check_response(record.get("request"), response, v1_response)
    except RecordError as error:
        return {**result, "bad": True, "problems": [f"bad record: {error}"]}
    except Exception as error:  # one odd record must not stop the batch
        return {**result, "bad": True,
                "problems": [f"checker error: {type(error).__name__}: {error}"]}

    message = (record.get("request") or {}).get("message")
    # check_response stops after schema problems, before the comparison: then v2's own
    # answer is the reason, not v1's.
    reached_comparison = not any(problem.startswith("schema:") for problem in problems)
    result["v1_comparison_skipped"] = (
        reached_comparison and response.get("engine") == "v2"
        and not (isinstance(message, str) and message.strip())
        and v1_response is not None and not is_v1_answer(v1_response)
    )
    result["correct"] = not problems
    result["problems"] = problems
    return result


def bad_record(problem, engine="unknown"):
    """The result for a record that cannot be checked."""
    return {"engine": engine, "forced_engine": None, "status": None, "correct": None,
            "problems": [problem], "bad": True, "v1_comparison_skipped": False}


def _engine_name(value):
    """An engine name to group results by, or None. A wrong type is reported by the schema
    check; it must not break the summary (it is used as a key and sorted)."""
    return value if isinstance(value, str) and value else None


def _status(value):
    """The HTTP status as an int from 100 to 599 (200.0 and "200" are accepted), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        value = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        text = value.strip()
        # isdigit() alone accepts characters like "²" that int() rejects
        value = int(text) if text.isascii() and text.isdigit() else None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def summarise(results):
    """Per engine: responses checked, correct, % correct, non-200 responses skipped, bad
    records, and v2 answers not compared with v1 because v1's answer was not valid.

    An engine the load test forced also gets a row when no response came from it (for
    example every v2 request fell back to v1), so that it shows 0 checked.
    """
    def empty_row():
        return {"checked": 0, "correct": 0, "not_checked": 0, "bad_records": 0,
                "v1_comparison_skipped": 0}

    summary = {}
    for result in results:
        if result.get("forced_engine"):
            summary.setdefault(result["forced_engine"], empty_row())
        row = summary.setdefault(result["engine"], empty_row())
        if result.get("bad"):
            row["bad_records"] += 1
            continue
        if result["correct"] is None:
            row["not_checked"] += 1
            continue
        row["checked"] += 1
        row["correct"] += result["correct"]
        row["v1_comparison_skipped"] += result.get("v1_comparison_skipped", False)
    for row in summary.values():
        row["percent_correct"] = (
            round(100 * row["correct"] / row["checked"], 1) if row["checked"] else None
        )
    return summary


def read_records(path):
    """Read a JSON Lines file: [(line number in the file, record)], blank lines skipped.

    A line that is not valid UTF-8 or JSON (for example cut off when the load test was
    stopped) gives a RecordError as its record, so the other lines are still checked.
    """
    records = []
    with open(path, "rb") as file:
        for line_number, raw in enumerate(file, start=1):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                records.append((line_number, RecordError(f"not valid UTF-8 ({error})")))
                continue
            if not line.strip():
                continue
            try:
                records.append((line_number, json.loads(line)))
            except json.JSONDecodeError as error:
                records.append((line_number, RecordError(f"not valid JSON ({error})")))
    return records


def check_line(record):
    """check_record for one item from read_records."""
    if isinstance(record, RecordError):
        return bad_record(f"bad record: {record}")
    return check_record(record)


def _count(text):
    """argparse type: a whole number, 0 or more."""
    number = int(text)
    if number < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check load-test results for correctness, per engine."
    )
    parser.add_argument("results", help="JSON Lines file, one request and response per line")
    parser.add_argument("--show", type=_count, default=10,
                        help="number of incorrect responses to print (default 10)")
    parser.add_argument("--min-percent", type=float, default=100.0,
                        help="exit with 1 when an engine's %% correct is below this "
                             "(default 100)")
    args = parser.parse_args(argv)

    try:
        records = read_records(args.results)
    except (OSError, ValueError) as error:
        print(f"Could not read results: {error}", file=sys.stderr)
        return 2

    lines = [(line_number, check_line(record)) for line_number, record in records]
    summary = summarise([result for _, result in lines])
    for engine, row in sorted(summary.items()):
        percent = "n/a" if row["percent_correct"] is None else f"{row['percent_correct']}%"
        line = (f"{engine}: {row['correct']}/{row['checked']} correct ({percent}), "
                f"{row['not_checked']} non-200 not checked")
        if row["bad_records"]:
            line += f", {row['bad_records']} bad records"
        if row["v1_comparison_skipped"]:
            line += (f", {row['v1_comparison_skipped']} not compared with v1 "
                     "(v1's answer was not valid)")
        print(line)

    wrong = [(n, r) for n, r in lines if r["correct"] is False]
    for line_number, result in wrong[:args.show]:
        print(f"\nline {line_number} ({result['engine']}):")
        for problem in result["problems"]:
            print(f"  - {problem}")
    if len(wrong) > args.show:
        print(f"\n... and {len(wrong) - args.show} more incorrect responses")

    bad = [(n, r) for n, r in lines if r["bad"]]
    for line_number, result in bad[:args.show]:
        print(f"\nline {line_number}: not checked: {result['problems'][0]}")
    if len(bad) > args.show:
        print(f"\n... and {len(bad) - args.show} more bad records")

    # Compare the exact share, not the rounded percent: 1999/2000 prints as 100.0% but is
    # below 100.
    below = sorted(engine for engine, row in summary.items()
                   if row["checked"] and 100 * row["correct"] < args.min_percent * row["checked"])
    # An engine with no 200 at all has not shown it is correct. "unknown" only holds errors
    # the load test did not say the engine of, which the error rate counts instead.
    unchecked = sorted(engine for engine, row in summary.items()
                       if row["checked"] == 0 and engine != "unknown")
    if below:
        print(f"\nBelow --min-percent {args.min_percent:g}: {', '.join(below)}")
    if unchecked:
        print(f"\nNo 200 responses to check for: {', '.join(unchecked)}")
    # An empty file, or one with only errors, must not pass the gate.
    nothing_checked = not any(row["checked"] for engine, row in summary.items()
                              if engine != "unknown")
    if nothing_checked:
        print("\nNo 200 responses from v1 or v2 were checked")
    return 1 if below or unchecked or bad or nothing_checked else 0


if __name__ == "__main__":
    sys.exit(main())
