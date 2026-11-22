"""Tests for the correctness checker. Hospitals are real rows from data/hospitals.csv."""

import copy
import json

import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.agent_v2 import ai_agent_recommender as agent
from app.api import facade
from app.api.main import app
from app.quality import correctness
from app.recommender_v1 import rules_based_recommender as recommender
from app.services import nearest_hospital_getter
from tests.factories import make_er_data, make_status
from tests.test_ai_agent_recommender import FIND, STATUS, WEATHER, FakeClient, submit, turn

ORIGIN = {"lat": 45.5017, "lon": -73.5673}  # downtown Montréal
REQUEST = {**ORIGIN, "limit": 4, "radius_km": 50}
VERDUN = "51218097"
MAISONNEUVE = "12934659"
SACRE_COEUR = "12475976"
CHILDREN_ID = "51222800"  # CHU Sainte-Justine
USAGE = {"model": "gemini-test", "model_calls": 3, "tool_calls": 3,
         "prompt_tokens": 100, "output_tokens": 20}


def summary(hospital_id):
    return {"id": hospital_id, **correctness.load_hospitals()[hospital_id]}


def entry(hospital_id, rank, travel=10.0, stay=3.0, occupancy=1.0):
    wait = recommender.estimated_er_minutes(
        {"avgStayAmbulatoryHours": stay, "occupancyPct": occupancy * 100}
    )
    return {
        "rank": rank,
        "hospital": summary(hospital_id),
        "travel_minutes": travel,
        "distance_km": round(travel * 0.8, 1),
        "estimated_wait_minutes": round(wait),
        "expected_minutes": round(travel + wait),
        "occupancy_rate": occupancy,
        "patients_waiting": 10,
        "patients_over_24h": 1,
        "avg_ambulatory_stay_hours": stay,
        "reason": "Short drive and a quiet ER.",
    }


def response(engine="v1", **changes):
    """A correct response. v2 decides call_911 and records model usage."""
    body = {
        "request_id": "7f3c2a9e-51b4-4c1e-9a0d-2e6f1b8c4d10",
        "requested_at": "2026-11-20T14:05:12-05:00",
        "engine": engine,
        "call_911": False if engine == "v2" else None,
        "note": None,
        "model_usage": dict(USAGE) if engine == "v2" else None,
        "data_updated_at": "2026-11-20T18:45:00.000Z",
        "origin": dict(ORIGIN),
        "radius_km": 50,
        "weather_now": None,
        "travel_time_multiplier": 1.0,
        "recommendations": [entry(VERDUN, 1, travel=10), entry(MAISONNEUVE, 2, travel=15)],
        "unranked_nearby": [{"hospital": summary(SACRE_COEUR), "cause": "no_current_data"}],
        "disclaimer": orchestrator.DISCLAIMER,
    }
    body.update(changes)
    return body


def broken(change, engine="v1"):
    body = response(engine)
    change(body)
    return body


def set_er_fields(entry_, value, fields=correctness.ER_FIELDS):
    for field in fields:
        entry_[field] = value


def test_correct_v1_and_v2_responses_have_no_problems():
    assert correctness.check_response(REQUEST, response("v1")) == []
    assert correctness.check_response(REQUEST, response("v2")) == []
    assert correctness.check_response(REQUEST, response("v1_fallback")) == []


@pytest.mark.parametrize("change, expected", [
    (lambda r: r.pop("disclaimer"), "schema: (top level): 'disclaimer' is a required"),
    (lambda r: r.update(engine="v3"), "schema: engine"),
    (lambda r: r["recommendations"][0].pop("expected_minutes"), "schema: recommendations/0"),
    (lambda r: r["recommendations"][0]["hospital"].update(id="H0001"), "unknown hospital id"),
    (lambda r: r["recommendations"][0]["hospital"].update(name="Other"), "name 'Other' does not"),
    (lambda r: r["recommendations"][0]["hospital"].update(lat=45.6), "coordinates do not match"),
    (lambda r: r["recommendations"][1].update(hospital=summary(VERDUN)), "recommended 2 times"),
    (lambda r: r["recommendations"][1].update(rank=3), "ranks [1, 3]"),
    (lambda r: r["recommendations"][0].update(expected_minutes=999), "expected_minutes 999 is"),
    (lambda r: r["recommendations"][0].update(estimated_wait_minutes=500, expected_minutes=510),
     "does not match its average stay and occupancy"),
    (lambda r: r["recommendations"][0].update(travel_minutes=-1), "negative travel time"),
    (lambda r: set_er_fields(r["recommendations"][0], None), "v1 ranked hospital 51218097 without"),
    (lambda r: r.update(radius_km=2), "v1 used radius_km 2, not the requested 50"),
    (lambda r: r.pop("radius_km"), "v1 response has no radius_km"),
    (lambda r: r.update(request_id="abc"), "schema: request_id: 'abc' is not a 'uuid'"),
    (lambda r: r.update(requested_at="yesterday"), "schema: requested_at: 'yesterday' is not"),
    (lambda r: r.pop("call_911"), "'call_911' is a required property"),
    (lambda r: r.pop("model_usage"), "'model_usage' is a required property"),
    (lambda r: r["recommendations"][0].pop("avg_ambulatory_stay_hours"),
     "'avg_ambulatory_stay_hours' is a required property"),
    (lambda r: r["origin"].update(lat=46.8), "is not the requested location"),
    (lambda r: r["recommendations"][1].update(hospital=summary(CHILDREN_ID)),
     "v1 recommended specialised ER 51222800"),
    (lambda r: r["recommendations"][0].update(reason="  "), "empty reason"),
    (lambda r: r["unranked_nearby"][0].update(hospital=summary(VERDUN)),
     "both recommended and unranked"),
    (lambda r: r["unranked_nearby"][0].update(cause="specialised"), "unranked as specialised"),
    (lambda r: r.update(call_911=True), "v1 cannot read the message, so call_911 should be null"),
    (lambda r: r.update(model_usage=USAGE), "v1 response has model_usage"),
    (lambda r: r.update(note="Some note."), "v1 has a note but there was no message"),
])
def test_each_rule_reports_its_problem(change, expected):
    problems = correctness.check_response(REQUEST, broken(change))
    assert any(expected in problem for problem in problems), problems


def test_v1_must_be_ordered_by_expected_minutes():
    body = response(recommendations=[entry(MAISONNEUVE, 1, travel=15), entry(VERDUN, 2, travel=10)])
    assert correctness.check_response(REQUEST, body) == [
        "v1 recommendations are not ordered by expected_minutes"
    ]
    # v2 may order differently
    assert correctness.check_response(REQUEST, {**body, **response("v2", recommendations=body[
        "recommendations"])}) == []


def test_more_recommendations_than_the_limit():
    problems = correctness.check_response({**REQUEST, "limit": 1}, response())
    assert problems == ["2 recommendations for a limit of 1"]


def test_v1_hospitals_must_be_within_the_requested_radius():
    # Maisonneuve-Rosemont is about 8 km from the origin.
    request = {**REQUEST, "radius_km": 5}
    assert correctness.check_response(request, response(radius_km=50)) == [
        "v1 used radius_km 50, not the requested 5",
        f"hospital {MAISONNEUVE} is 8.1 km away, outside radius_km 5",
    ]


def test_v2_chooses_its_radius_up_to_the_maximum():
    request = {**REQUEST, "radius_km": 5}  # only v1 uses the requested radius
    assert correctness.check_response(request, response("v2", radius_km=10)) == []
    assert correctness.check_response(request, response("v2", radius_km=5)) == [
        f"hospital {MAISONNEUVE} is 8.1 km away, outside radius_km 5"
    ]
    too_far = agent.MAX_RADIUS_KM + 50
    assert correctness.check_response(REQUEST, response("v2", radius_km=too_far)) == [
        f"v2 radius_km {too_far} is more than the maximum {agent.MAX_RADIUS_KM}"
    ]
    no_radius = response("v2")
    no_radius.pop("radius_km")
    assert correctness.check_response(REQUEST, no_radius) == [
        "v2 recommended hospitals but gave no radius_km"
    ]


def test_missing_or_null_request_values_use_the_api_defaults():
    request = {"lat": str(ORIGIN["lat"]), "lon": ORIGIN["lon"],
               "limit": None, "radius_km": None, "message": None}
    assert correctness.check_response(request, response("v1")) == []
    assert correctness.check_response(ORIGIN, response("v1")) == []


@pytest.mark.parametrize("request_, expected", [
    (None, "request is not a JSON object"),
    ({"lon": ORIGIN["lon"]}, "request has no lat"),
    ({**ORIGIN, "lat": "north"}, "request lat 'north' is not a number"),
    ({**ORIGIN, "limit": 2.5}, "request limit 2.5 is not a whole number"),
    ({**ORIGIN, "lat": float("nan")}, "request lat nan is not a finite number"),
    ({**ORIGIN, "limit": "nan"}, "request limit 'nan' is not a finite number"),
    ({**ORIGIN, "radius_km": "inf"}, "request radius_km 'inf' is not a finite number"),
    ({**ORIGIN, "message": 42}, "request message 42 is not text"),
])
def test_request_values_that_cannot_be_read(request_, expected):
    with pytest.raises(correctness.RecordError, match=expected):
        correctness.check_response(request_, response())


def test_v2_may_leave_er_numbers_null_but_not_only_some():
    body = response("v2")
    set_er_fields(body["recommendations"][0], None)
    body["recommendations"][0].update(patients_waiting=None, patients_over_24h=None)
    assert correctness.check_response(REQUEST, body) == []

    body = response("v2")
    set_er_fields(body["recommendations"][0], None, fields=["occupancy_rate"])
    problems = correctness.check_response(REQUEST, body)
    assert any("has some ER numbers but not ['occupancy_rate']" in p for p in problems), problems


def test_v2_specialised_er_needs_a_message():
    body = response("v2", recommendations=[entry(CHILDREN_ID, 1), entry(VERDUN, 2)])
    assert correctness.check_response(REQUEST, body) == [
        "specialised ER 51222800 recommended without a message"
    ]
    with_message = {**REQUEST, "message": "my 6-year-old has a high fever"}
    assert correctness.check_response(with_message, body) == []


def test_emergency_phrase_requires_call_911():
    request = {**REQUEST, "message": "my father has chest pain"}
    assert correctness.check_response(request, response("v2", call_911=False)) == [
        "the message has an emergency phrase but call_911 is not true"
    ]
    assert correctness.check_response(request, response("v2", call_911=True)) == []
    # v1 cannot read the message, but the code safety net sets call_911 for it
    v1 = response("v1", call_911=True, note=orchestrator.MESSAGE_NOT_READ_NOTE)
    assert correctness.check_response(request, v1) == []


def test_v2_must_decide_call_911():
    assert correctness.check_response(REQUEST, response("v2", call_911=None)) == [
        "v2 did not decide call_911 (null)"
    ]


def test_v1_must_say_it_did_not_read_the_message():
    request = {**REQUEST, "message": "my son has a fever"}
    assert correctness.check_response(request, response("v1")) == [
        "v1 did not say that the message was not read"
    ]
    fallback = response("v1_fallback", note=orchestrator.FALLBACK_NOTE)
    assert correctness.check_response(request, fallback) == []


@pytest.mark.parametrize("engine, usage, expected", [
    ("v2", None, ["v2 response has no model_usage"]),
    ("v2", {**USAGE, "model_calls": 0}, ["v2 answered with 0 model calls"]),
    ("v2", {**USAGE, "model_calls": agent.MAX_MODEL_CALLS + 1},
     [f"model_usage.model_calls {agent.MAX_MODEL_CALLS + 1} is not between 0 and "
      f"{agent.MAX_MODEL_CALLS}"]),
    ("v1_fallback", None, []),  # failed before any model call
    ("v1_fallback", USAGE, []),  # what v2 used before falling back
])
def test_model_usage(engine, usage, expected):
    assert correctness.check_response(REQUEST, response(engine, model_usage=usage)) == expected


def test_v2_is_compared_with_v1_only_without_a_message():
    v1 = response("v1", recommendations=[
        entry(VERDUN, 1, travel=10), entry(MAISONNEUVE, 2, travel=15),
    ])
    shares_two = response("v2", recommendations=[
        entry(MAISONNEUVE, 1, travel=15), entry(VERDUN, 2, travel=10),
    ])
    shares_one = response("v2", recommendations=[
        entry(VERDUN, 1, travel=10), entry(SACRE_COEUR, 2, travel=20),
    ], unranked_nearby=[])

    assert correctness.check_response(REQUEST, shares_two, v1) == []
    assert correctness.check_response(REQUEST, shares_one, v1) == [
        f"only 1 of v1's top 2 ({VERDUN}, {MAISONNEUVE}) are in v2's top 4; at least 2 expected"
    ]
    # With a message, v2 is meant to choose differently: not compared
    with_message = {**REQUEST, "message": "I can't drive far"}
    assert correctness.check_response(with_message, shares_one, v1) == []


def test_v2_is_not_compared_when_v1_failed():
    error_body = {"error": {"code": "data_unavailable", "message": "Try again.",
                            "request_id": "7f3c2a9e-51b4-4c1e-9a0d-2e6f1b8c4d10"}}
    v2 = response("v2", recommendations=[entry(SACRE_COEUR, 1, travel=20)], unranked_nearby=[])
    assert correctness.check_response(REQUEST, v2, error_body) == []
    assert correctness.check_response(REQUEST, v2, "not a response") == []

    result = correctness.check_record(
        {"request": REQUEST, "status": 200, "response": v2, "v1_response": error_body}
    )
    assert result["correct"] is True
    assert result["v1_comparison_skipped"] is True
    assert correctness.summarise([result])["v2"]["v1_comparison_skipped"] == 1


def test_comparison_needs_no_more_than_v1_recommended():
    v1_one = response("v1", recommendations=[entry(VERDUN, 1)])
    v1_none = response("v1", recommendations=[])
    v2 = response("v2")
    assert correctness.compare_with_v1(v2, v1_one) == []
    assert correctness.compare_with_v1(v2, v1_none) == []
    assert correctness.compare_with_v1(response("v2", recommendations=[]), v1_one) != []


def test_openapi_nullable_becomes_a_null_type():
    converted = correctness.openapi_to_json_schema({
        "properties": {
            "a": {"type": "string", "nullable": True},
            "b": {"type": "string", "enum": ["x"], "nullable": True},
            "c": {"$ref": "#/components/schemas/X", "nullable": True},
            "d": {"type": "number"},
        },
    })
    assert converted["properties"] == {
        "a": {"type": ["string", "null"]},
        "b": {"type": ["string", "null"], "enum": ["x", None]},
        "c": {"anyOf": [{"$ref": "#/components/schemas/X"}, {"type": "null"}]},
        "d": {"type": "number"},
    }


ERROR_BODY = {"error": {"code": "data_unavailable", "message": "Try again.",
                        "request_id": "7f3c2a9e-51b4-4c1e-9a0d-2e6f1b8c4d10"}}


def test_records_that_are_not_200_are_not_checked():
    result = correctness.check_record({"request": REQUEST, "status": 503, "response": {}})
    assert result == {"engine": "unknown", "forced_engine": None, "status": 503,
                      "correct": None, "problems": [], "bad": False,
                      "v1_comparison_skipped": False}


def test_non_200_is_counted_under_the_engine_the_load_test_forced():
    record = {"request": REQUEST, "status": 503, "response": ERROR_BODY, "engine": "v2"}
    assert correctness.check_record(record)["engine"] == "v2"
    # A 200 says which engine answered, which wins (v2 can fall back to v1)
    fallback = {"request": REQUEST, "status": 200, "response": response("v1_fallback"),
                "engine": "v2"}
    assert correctness.check_record(fallback)["engine"] == "v1_fallback"


def test_status_given_as_a_string_is_still_checked():
    record = {"request": REQUEST, "status": "200", "response": response("v2", call_911=None)}
    result = correctness.check_record(record)
    assert result["status"] == 200
    assert result["correct"] is False


@pytest.mark.parametrize("engine", [3, ["v1"], {"name": "v1"}, ""])
def test_an_engine_that_is_not_a_name_does_not_break_the_summary(engine):
    results = [
        correctness.check_record({"request": REQUEST, "status": 200,
                                  "response": {**response("v2"), "engine": engine}}),
        correctness.check_record({"request": REQUEST, "status": 200, "response": response()}),
    ]
    assert results[0]["engine"] == "unknown"
    assert results[0]["correct"] is False  # the schema check reports it
    assert sorted(correctness.summarise(results)) == ["unknown", "v1"]


@pytest.mark.parametrize("record, expected", [
    ([1, 2], "record is not a JSON object"),
    ({"request": REQUEST, "status": 200, "response": "ok"}, "the 200 response is not a JSON"),
    ({"request": {**REQUEST, "lat": None}, "status": 200, "response": response()},
     "bad record: request has no lat"),
    ({"request": REQUEST, "status": "OK", "response": response()},
     "bad record: status 'OK' is not an HTTP status code"),
    ({"request": REQUEST, "response": response()},
     "bad record: status None is not an HTTP status code"),
    ({"request": REQUEST, "status": "²", "response": response()},
     "bad record: status '²' is not an HTTP status code"),
    ({"request": REQUEST, "status": 2000, "response": response()},
     "bad record: status 2000 is not an HTTP status code"),
])
def test_records_that_cannot_be_checked_are_bad(record, expected):
    result = correctness.check_record(record)
    assert result["bad"] is True
    assert result["correct"] is None
    assert expected in result["problems"][0]


def test_summary_per_engine():
    results = [
        correctness.check_record({"request": REQUEST, "status": 200, "response": response("v2")}),
        correctness.check_record({"request": REQUEST, "status": 200,
                                  "response": response("v2", call_911=None)}),
        correctness.check_record({"request": REQUEST, "status": 200, "response": response("v1")}),
        correctness.check_record({"request": REQUEST, "status": 500,
                                  "response": ERROR_BODY, "engine": "v2"}),
        correctness.check_record({"request": {}, "status": 200, "response": response("v1")}),
    ]
    assert correctness.summarise(results) == {
        "v2": {"checked": 2, "correct": 1, "not_checked": 1, "bad_records": 0,
               "v1_comparison_skipped": 0, "percent_correct": 50.0},
        "v1": {"checked": 1, "correct": 1, "not_checked": 0, "bad_records": 1,
               "v1_comparison_skipped": 0, "percent_correct": 100.0},
    }


def write_results(tmp_path, lines):
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n\n", encoding="utf-8")
    return str(path)


def test_command_line_prints_summary_and_problems(tmp_path, capsys):
    path = write_results(tmp_path, [
        {"request": REQUEST, "status": 200, "response": response("v1")},
        {"request": REQUEST, "status": 200, "response": response("v2", call_911=None)},
    ])
    assert correctness.main([path]) == 1  # v2 is below 100%
    output = capsys.readouterr().out
    assert "v1: 1/1 correct (100.0%), 0 non-200 not checked" in output
    assert "v2: 0/1 correct (0.0%)" in output
    assert "line 2 (v2):\n  - v2 did not decide call_911 (null)" in output


def test_command_line_exit_code_follows_min_percent(tmp_path):
    lines = [{"request": REQUEST, "status": 200, "response": response("v2")}] * 3
    lines.append({"request": REQUEST, "status": 200, "response": response("v2", call_911=None)})
    lines.append({"request": REQUEST, "status": 503, "response": ERROR_BODY, "engine": "v2"})
    path = write_results(tmp_path, lines)
    assert correctness.main([path]) == 1
    assert correctness.main([path, "--min-percent", "75"]) == 0  # 3 of 4 correct
    assert correctness.main([path, "--min-percent", "80"]) == 1


def test_command_line_goes_on_after_a_bad_record(tmp_path, capsys):
    path = write_results(tmp_path, [
        {"request": {**REQUEST, "limit": None}, "status": 200, "response": response("v1")},
        {"request": {**REQUEST, "lat": "north"}, "status": 200, "response": response("v1")},
        {"request": REQUEST, "status": 200, "response": response("v2"),
         "v1_response": ERROR_BODY},
    ])
    assert correctness.main([path]) == 1  # a bad record
    output = capsys.readouterr().out
    assert "v1: 1/1 correct (100.0%), 0 non-200 not checked, 1 bad records" in output
    assert ("v2: 1/1 correct (100.0%), 0 non-200 not checked, 1 not compared with v1"
            in output)
    assert "line 2: not checked: bad record: request lat 'north' is not a number" in output


def test_command_line_goes_on_after_a_line_that_is_not_json(tmp_path, capsys):
    path = tmp_path / "results.jsonl"
    good = json.dumps({"request": REQUEST, "status": 200, "response": response("v1")})
    path.write_text(f'{good}\n{{"status": 200, "resp\n', encoding="utf-8")  # cut off
    assert correctness.main([str(path)]) == 1
    output = capsys.readouterr().out
    assert "v1: 1/1 correct (100.0%)" in output
    assert "line 2: not checked: bad record: not valid JSON" in output


def test_command_line_goes_on_after_a_line_that_is_not_utf8(tmp_path, capsys):
    path = tmp_path / "results.jsonl"
    good = json.dumps({"request": REQUEST, "status": 200, "response": response("v1")})
    path.write_bytes(good.encode() + b"\n\xff\xfe\n")
    assert correctness.main([str(path)]) == 1
    output = capsys.readouterr().out
    assert "v1: 1/1 correct (100.0%)" in output
    assert "line 2: not checked: bad record: not valid UTF-8" in output


def test_command_line_fails_when_v2_always_fell_back(tmp_path, capsys):
    path = write_results(tmp_path, [
        {"request": REQUEST, "status": 200, "response": response("v1_fallback"), "engine": "v2"},
    ])
    assert correctness.main([path]) == 1
    output = capsys.readouterr().out
    assert "v1_fallback: 1/1 correct (100.0%)" in output
    assert "v2: 0/0 correct (n/a), 0 non-200 not checked" in output
    assert "No 200 responses to check for: v2" in output


@pytest.mark.parametrize("lines", [
    [],
    [{"request": REQUEST, "status": 503, "response": ERROR_BODY}],
])
def test_command_line_fails_when_nothing_was_checked(tmp_path, capsys, lines):
    assert correctness.main([write_results(tmp_path, lines)]) == 1
    assert "No 200 responses from v1 or v2 were checked" in capsys.readouterr().out


def test_command_line_rejects_a_negative_show(tmp_path):
    path = write_results(tmp_path, [{"request": REQUEST, "status": 200, "response": response()}])
    with pytest.raises(SystemExit):
        correctness.main([path, "--show", "-1"])
    assert correctness.main([path, "--show", "0"]) == 0


def test_command_line_cannot_open_the_file(tmp_path, capsys):
    assert correctness.main([str(tmp_path / "missing.jsonl")]) == 2
    assert "Could not read results" in capsys.readouterr().err


def test_command_line_prints_line_numbers_in_the_file(tmp_path, capsys):
    path = tmp_path / "results.jsonl"
    lines = [
        json.dumps({"request": REQUEST, "status": 200, "response": response("v1")}),
        "",  # e.g. two runs appended to one file
        json.dumps({"request": REQUEST, "status": 200, "response": response("v2", call_911=None)}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    correctness.main([str(path)])
    assert "line 3 (v2):\n  - v2 did not decide call_911 (null)" in capsys.readouterr().out


def test_command_line_fails_an_engine_with_no_200(tmp_path, capsys):
    path = write_results(tmp_path, [
        {"request": REQUEST, "status": 200, "response": response("v1")},
        {"request": REQUEST, "status": 503, "response": ERROR_BODY, "engine": "v2"},
        {"request": REQUEST, "status": 503, "response": ERROR_BODY},  # engine not recorded
    ])
    assert correctness.main([path]) == 1
    output = capsys.readouterr().out
    assert "v2: 0/0 correct (n/a), 1 non-200 not checked" in output
    assert "No 200 responses to check for: v2" in output  # "unknown" is not listed


# End to end: responses produced by the real API, v1 ranking and v2 agent code, with only the
# web services faked (driving times, ER status, weather) and Gemini scripted.

WEATHER_ROWS = [{"date": "2026-11-20 19:00:00+00:00", "temperature_2m": 2.0,
                 "precipitation": 0.0, "snowfall": 0.0}]


@pytest.fixture
def api(monkeypatch):
    def drive_times(lat, lon, destinations):
        routes = []
        for dest_lat, dest_lon in destinations:
            km = nearest_hospital_getter.straight_line_km(lat, lon, dest_lat, dest_lon)
            routes.append({"duration_min": 3 + km * 1.5, "distance_km": km * 1.3})
        return routes

    monkeypatch.setattr(nearest_hospital_getter, "get_drive_times", drive_times)
    candidates = nearest_hospital_getter.get_candidate_hospitals(ORIGIN["lat"], ORIGIN["lon"])
    found = [hospital["id"] for hospital in candidates]
    # Every ER has data except the farthest one, so unranked_nearby is not empty.
    er_data = make_er_data({
        hospital_id: make_status(occupancy_pct=70 + 7 * i, stay_hours=2.5 + 0.2 * (i % 4))
        for i, hospital_id in enumerate(found[:-1])
    })
    for module in (orchestrator, agent):
        monkeypatch.setattr(module, "get_er_data", lambda: er_data)
        monkeypatch.setattr(module, "get_weather_data", lambda lat, lon: WEATHER_ROWS)
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")
    monkeypatch.setenv(facade.HEADER_ENABLED_ENV_VAR, "true")
    return TestClient(app), found


def get(client, engine, message=None):
    params = dict(ORIGIN, message=message) if message else ORIGIN
    response = client.get("/v1/recommendations", params=params, headers={"X-Engine": engine})
    assert response.status_code == 200
    return response.json()


def script_gemini(monkeypatch, *replies):
    monkeypatch.setattr(agent, "make_client", lambda: FakeClient(*replies))


def test_real_v1_responses_are_correct(api):
    client, _ = api
    v1 = get(client, "v1")
    assert len(v1["recommendations"]) == recommender.DEFAULT_LIMIT
    assert correctness.check_response(ORIGIN, v1) == []

    request = {**ORIGIN, "message": "my son has a fever"}
    assert correctness.check_response(request, get(client, "v1", request["message"])) == []


def test_real_v2_response_is_correct_and_agrees_with_v1(api, monkeypatch):
    client, _ = api
    v1 = get(client, "v1")
    top_ids = [r["hospital"]["id"] for r in v1["recommendations"]]
    script_gemini(monkeypatch, turn(FIND), turn(STATUS, WEATHER), turn(submit(
        (top_ids[1], "Short drive and the quietest ER nearby."),
        (top_ids[0], "Close, a little busier."),
    )))

    v2 = get(client, "v2")
    assert v2["engine"] == "v2"
    assert correctness.check_response(ORIGIN, v2, v1) == []


def test_real_v2_children_er_for_a_child_is_correct(api, monkeypatch):
    client, found = api
    assert CHILDREN_ID in found
    message = "my 6-year-old has a high fever"
    script_gemini(monkeypatch, turn(FIND), turn(STATUS), turn(submit(
        (CHILDREN_ID, "A children's ER, a short drive away."), needs=["children"],
    )))

    v2 = get(client, "v2", message)
    assert v2["recommendations"][0]["hospital"]["id"] == CHILDREN_ID
    assert correctness.check_response({**ORIGIN, "message": message}, v2) == []


def test_real_v1_fallback_is_correct(api, monkeypatch):
    client, _ = api
    script_gemini(monkeypatch, RuntimeError("Gemini is down"))
    message = "j'ai mal au ventre"

    fallback = get(client, "v2", message)
    assert fallback["engine"] == "v1_fallback"
    assert correctness.check_response({**ORIGIN, "message": message}, fallback) == []


def test_example_in_the_spec_is_correct():
    """The 200 example in docs/openapi.yaml must pass the checker too."""
    import yaml

    spec = yaml.safe_load(correctness.SPEC_PATH.read_text(encoding="utf-8"))
    operation = spec["paths"]["/v1/recommendations"]["get"]
    example = copy.deepcopy(operation["responses"]["200"]["content"]["application/json"]["example"])
    assert correctness.check_response(example["origin"], example) == []
