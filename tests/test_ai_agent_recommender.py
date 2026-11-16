"""Tests for the v2 agent. Gemini is faked: each test scripts the tool calls the model makes."""

import pytest
from google.genai import types

from app.agent_v2 import ai_agent_recommender as agent
from app.recommender_v1 import rules_based_recommender as recommender
from tests.factories import make_er_data, make_hospital, make_status

CHILDREN_ID = "51222800"  # CHU Sainte-Justine
WEATHER_ROWS = [
    {"date": "2026-11-10 21:00:00+00:00", "precipitation": 0.0, "snowfall": 1.0},
    {"date": "2026-11-10 22:00:00+00:00", "precipitation": 0.0, "snowfall": 0.0},
]


def turn(*calls, prompt_tokens=100, output_tokens=20, thoughts_tokens=5):
    """One model reply that makes the given tool calls, as (name, args) pairs."""
    parts = [
        types.Part(function_call=types.FunctionCall(id=f"call-{i}", name=name, args=args))
        for i, (name, args) in enumerate(calls)
    ]
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            thoughts_token_count=thoughts_tokens,
        ),
    )


def submit(*choices, call_911=False, note=None):
    args = {
        "recommendations": [{"hospital_id": i, "reason": r} for i, r in choices],
        "call_911": call_911,
    }
    if note is not None:
        args["note"] = note
    return ("submit_recommendations", args)


FIND = ("find_hospitals", {})
STATUS = ("get_er_status", {})
WEATHER = ("get_weather", {})


class FakeClient:
    """Stands in for genai.Client: returns the scripted replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def tool_results(self, request_index):
        """The tool results sent to the model in the given request, as {name: response}."""
        last = self.requests[request_index]["contents"][-1]
        return {p.function_response.name: p.function_response.response for p in last.parts}


@pytest.fixture
def services(monkeypatch):
    """Replace the three data services. Records the calls made."""
    calls = {"candidates": [], "er": 0, "weather": 0}

    def candidates(lat, lon, radius_km):
        calls["candidates"].append((lat, lon, radius_km))
        return [
            make_hospital("A", 5, name="HÔPITAL A"),
            make_hospital("B", 25),
            make_hospital("C", 8),  # no ER status
            make_hospital("D", None),  # no road route
            make_hospital(CHILDREN_ID, 12),
        ]

    def er_data():
        calls["er"] += 1
        return make_er_data({
            "A": make_status(125),
            "B": make_status(65),
            CHILDREN_ID: make_status(100, stay_hours=3.0),
        })

    def weather(lat, lon):
        calls["weather"] += 1
        return WEATHER_ROWS

    monkeypatch.setattr(agent, "get_candidate_hospitals", candidates)
    monkeypatch.setattr(agent, "get_er_data", er_data)
    monkeypatch.setattr(agent, "get_weather_data", weather)
    monkeypatch.delenv(agent.MODEL_ENV_VAR, raising=False)
    return calls


# --- a normal run --------------------------------------------------------------------

def test_agent_looks_up_data_and_returns_v1_shaped_result(services):
    client = FakeClient(
        turn(FIND),
        turn(STATUS, WEATHER),
        turn(submit(("B", "Quiet ER."), ("A", "Closest."), note="  Both are open.  ")),
    )

    result = agent.run_agent(45.5, -73.6, "I twisted my ankle", client=client)

    assert services == {"candidates": [(45.5, -73.6, 50)], "er": 1, "weather": 1}
    assert set(result) == {
        "data_updated_at", "radius_km", "weather_now", "travel_time_multiplier",
        "recommendations", "unranked_nearby", "call_911", "note", "model_usage",
    }
    assert result["data_updated_at"] == "2026-11-10T20:45:00.000Z"
    assert result["radius_km"] == 50
    assert result["weather_now"] == WEATHER_ROWS[0]
    assert result["travel_time_multiplier"] == recommender.SNOW_TRAVEL_MULTIPLIER
    assert result["call_911"] is False
    assert result["note"] == "Both are open."

    first, second = result["recommendations"]
    assert first == {
        "hospital": {
            "id": "B", "name": "Hospital B",
            "address": "1 Example St, Montréal, QC (B)", "lat": 45.5, "lon": -73.6,
        },
        "travel_minutes": 32.5,  # 25 min in snow
        "distance_km": 20.0,
        "estimated_wait_minutes": 156,  # 4 h average stay at 65% occupancy
        "expected_minutes": 188,
        "occupancy_rate": 0.65,
        "patients_waiting": 10,
        "patients_over_24h": 1,
        "avg_ambulatory_stay_hours": 4.0,
        "rank": 1,
        "reason": "Quiet ER.",
    }
    assert (second["hospital"]["id"], second["rank"], second["reason"]) == ("A", 2, "Closest.")


def test_result_has_same_recommendation_fields_as_v1(services):
    client = FakeClient(turn(FIND), turn(STATUS), turn(submit(("A", "Closest."))))
    result = agent.run_agent(45.5, -73.6, client=client)

    v1_ranked, _ = recommender.recommend(
        [make_hospital("A", 5)], make_er_data({"A": make_status()}), None
    )
    assert set(result["recommendations"][0]) == set(v1_ranked[0])


def test_unranked_lists_hospitals_that_could_not_be_ranked(services):
    client = FakeClient(turn(FIND), turn(STATUS), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    # B was rankable but not chosen, so it is not listed.
    assert {u["hospital"]["id"]: u["cause"] for u in result["unranked_nearby"]} == {
        "C": "no_current_data", "D": "no_route", CHILDREN_ID: "specialised",
    }


def test_agent_can_recommend_a_children_hospital_which_v1_never_does(services):
    client = FakeClient(
        turn(FIND), turn(STATUS), turn(submit((CHILDREN_ID, "Children's ER."), ("A", "Closest.")))
    )

    result = agent.run_agent(45.5, -73.6, "my 6-year-old has a fever", client=client)

    assert [r["hospital"]["id"] for r in result["recommendations"]] == [CHILDREN_ID, "A"]
    assert result["recommendations"][0]["estimated_wait_minutes"] == 180
    assert result["unranked_nearby"] != []
    assert CHILDREN_ID not in {u["hospital"]["id"] for u in result["unranked_nearby"]}


def test_call_911_and_limit(services):
    client = FakeClient(
        turn(FIND),
        turn(submit(("A", "r"), ("B", "r"), (CHILDREN_ID, "r"), call_911=True)),
    )

    result = agent.run_agent(45.5, -73.6, "chest pain", limit=2, client=client)

    assert result["call_911"] is True
    assert [r["hospital"]["id"] for r in result["recommendations"]] == ["A", "B"]


def test_er_numbers_are_none_when_the_model_skipped_er_status(services):
    client = FakeClient(turn(FIND), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    entry = result["recommendations"][0]
    assert entry["travel_minutes"] == 5  # no weather fetched, so no multiplier
    assert entry["estimated_wait_minutes"] is None
    assert entry["expected_minutes"] is None
    assert entry["occupancy_rate"] is None
    assert result["data_updated_at"] is None
    assert result["weather_now"] is None
    assert services["er"] == 0


# --- what the model is sent ----------------------------------------------------------

def test_request_carries_instructions_tools_and_message(services):
    client = FakeClient(turn(FIND), turn(submit(("A", "Closest."))))

    agent.run_agent(45.5, -73.6, "  j'ai mal au ventre  ", limit=3, client=client)

    request = client.requests[0]
    assert request["model"] == agent.DEFAULT_MODEL
    config = request["config"]
    assert config.system_instruction == agent.SYSTEM_INSTRUCTION
    assert [d.name for d in config.tools[0].function_declarations] == [
        "find_hospitals", "get_er_status", "get_weather", "submit_recommendations",
    ]
    assert config.tool_config.function_calling_config.mode == "ANY"
    assert config.thinking_config.thinking_level == "LOW"
    text = request["contents"][0].parts[0].text
    assert "up to 3 emergency rooms" in text
    assert "<<<\nj'ai mal au ventre\n>>>" in text


def test_request_text_without_message_and_with_long_message():
    assert "no message" in agent.build_request_text(None, 4)
    assert "no message" in agent.build_request_text("   ", 4)

    text = agent.build_request_text("x" * 2000, 4)
    assert "x" * agent.MAX_MESSAGE_CHARS in text
    assert "x" * (agent.MAX_MESSAGE_CHARS + 1) not in text


def test_model_name_can_be_set_by_environment(services, monkeypatch):
    monkeypatch.setenv(agent.MODEL_ENV_VAR, "gemini-other")
    client = FakeClient(turn(FIND), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    assert client.requests[0]["model"] == "gemini-other"
    assert result["model_usage"]["model"] == "gemini-other"


def test_tool_results_are_sent_back_to_the_model(services):
    client = FakeClient(turn(FIND), turn(STATUS, WEATHER), turn(submit(("A", "Closest."))))

    agent.run_agent(45.5, -73.6, client=client)

    # Request 2 = first message, the model's find_hospitals call, and its result.
    contents = client.requests[1]["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].function_call.name == "find_hospitals"
    assert contents[2].parts[0].function_response.id == "call-0"
    found = client.tool_results(1)["find_hospitals"]
    assert found["radius_km"] == 50
    assert found["hospitals"][0] == {
        "id": "A", "name": "HÔPITAL A", "kind": "general",
        "drive_minutes": 5, "distance_km": 4.0,
    }
    assert found["hospitals"][3]["drive_minutes"] is None
    assert found["hospitals"][4]["kind"] == "children"

    results = client.tool_results(2)
    assert results["get_er_status"]["updated_at"] == "2026-11-10T20:45:00.000Z"
    assert results["get_er_status"]["hospitals"][0] == {
        "id": "A",
        "occupancy_pct": 125,
        "avg_walk_in_stay_hours": 4.0,
        "patients_waiting_for_doctor": 10,
        "patients_over_24h": 1,
        "estimated_minutes_in_er": 300,
    }
    assert results["get_er_status"]["hospitals"][2] == {"id": "C", "status": "no_current_data"}
    assert results["get_weather"]["snowfall"] == 1.0
    assert results["get_weather"]["drive_time_multiplier"] == recommender.SNOW_TRAVEL_MULTIPLIER


def test_model_usage_is_recorded(services):
    client = FakeClient(turn(FIND), turn(STATUS, WEATHER), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    assert result["model_usage"] == {
        "model": agent.DEFAULT_MODEL,
        "model_calls": 3,
        "tool_calls": 4,
        "prompt_tokens": 300,
        "output_tokens": 75,  # includes thinking tokens
    }


# --- tools: limits and failures ------------------------------------------------------

def test_radius_is_clamped_and_search_count_is_limited(services):
    client = FakeClient(
        turn(("find_hospitals", {"radius_km": 5})),
        turn(("find_hospitals", {"radius_km": 9999})),
        turn(("find_hospitals", {"radius_km": 80})),
        turn(submit(("A", "Closest."))),
    )

    result = agent.run_agent(45.5, -73.6, client=client)

    assert [call[2] for call in services["candidates"]] == [5, agent.MAX_RADIUS_KM]
    assert "Search limit reached" in client.tool_results(3)["find_hospitals"]["error"]
    assert result["radius_km"] == agent.MAX_RADIUS_KM


def test_failed_service_is_reported_to_the_model_which_can_carry_on(services, monkeypatch):
    def failing(lat, lon):
        raise RuntimeError("Weather request failed: timed out")

    monkeypatch.setattr(agent, "get_weather_data", failing)
    client = FakeClient(turn(FIND), turn(WEATHER), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    assert client.tool_results(2)["get_weather"] == {"error": "Weather request failed: timed out"}
    assert result["weather_now"] is None
    assert result["travel_time_multiplier"] == 1.0
    assert len(result["recommendations"]) == 1


def test_stale_er_data_is_reported_and_not_used(services, monkeypatch):
    monkeypatch.setattr(
        agent, "get_er_data", lambda: make_er_data({"A": make_status()}, stale=True)
    )
    client = FakeClient(turn(FIND), turn(STATUS), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    assert "unavailable" in client.tool_results(2)["get_er_status"]["error"]
    assert result["recommendations"][0]["estimated_wait_minutes"] is None
    assert {u["cause"] for u in result["unranked_nearby"]} == {
        "stale_data", "no_route", "specialised",
    }


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (STATUS, "Call find_hospitals first"),
        (("find_hospitals", {"radius_km": "far"}), "Invalid arguments"),
        (("find_hospitals", {"city": "Laval"}), "Invalid arguments"),
        (("delete_everything", {}), "Unknown tool"),
    ],
    ids=["status_before_find", "bad_value", "bad_name", "unknown_tool"],
)
def test_bad_tool_calls_get_an_error_result(services, call, expected):
    client = FakeClient(turn(call), turn(FIND), turn(submit(("A", "Closest."))))

    agent.run_agent(45.5, -73.6, client=client)

    assert expected in client.tool_results(1)[call[0]]["error"]


# --- checking the final answer -------------------------------------------------------

@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        (submit(("Z", "Made up.")), "Unknown hospital_id 'Z'"),
        (submit(("D", "No road.")), "no road route"),
        (submit(("A", "One."), ("A", "Two.")), "listed twice"),
        (submit(("A", "  ")), "Give a reason"),
        (submit(), "at least one"),
        (("submit_recommendations", {"recommendations": "A", "call_911": False}), "must be a list"),
        (("submit_recommendations", {"recommendations": ["A"], "call_911": False}), "Unknown"),
    ],
    ids=["unknown_id", "no_route", "duplicate", "empty_reason", "empty", "not_list", "not_dict"],
)
def test_invalid_answer_is_sent_back_for_correction(services, bad, expected):
    client = FakeClient(turn(FIND), turn(bad), turn(submit(("A", "Closest."))))

    result = agent.run_agent(45.5, -73.6, client=client)

    assert expected in client.tool_results(2)["submit_recommendations"]["error"]
    assert [r["hospital"]["id"] for r in result["recommendations"]] == ["A"]


def test_submitting_before_searching_is_rejected(services):
    client = FakeClient(turn(submit()), turn(FIND), turn(submit(("A", "Closest."))))

    agent.run_agent(45.5, -73.6, client=client)

    assert "find_hospitals" in client.tool_results(1)["submit_recommendations"]["error"]


def test_empty_answer_is_valid_when_nothing_is_reachable(services, monkeypatch):
    monkeypatch.setattr(agent, "get_candidate_hospitals", lambda lat, lon, radius_km: [])
    client = FakeClient(turn(FIND), turn(submit(note="No ER within 50 km.")))

    result = agent.run_agent(49.0, -62.0, client=client)

    assert result["recommendations"] == []
    assert result["unranked_nearby"] == []
    assert result["note"] == "No ER within 50 km."


# --- failures that end the run -------------------------------------------------------

def test_gemini_error_raises_agent_error(services):
    client = FakeClient(turn(FIND), ConnectionError("no network"))

    with pytest.raises(agent.AgentError, match="Gemini request failed: no network"):
        agent.run_agent(45.5, -73.6, client=client)


def test_reply_without_tool_call_raises_agent_error(services):
    text_reply = types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text="Go to A.")])
        )]
    )

    with pytest.raises(agent.AgentError, match="did not call a tool"):
        agent.run_agent(45.5, -73.6, client=FakeClient(text_reply))


def test_blocked_reply_raises_agent_error(services):
    with pytest.raises(agent.AgentError, match="did not call a tool"):
        agent.run_agent(45.5, -73.6, client=FakeClient(types.GenerateContentResponse()))


def test_model_that_never_submits_is_stopped(services):
    client = FakeClient(*[turn(STATUS) for _ in range(agent.MAX_MODEL_CALLS + 1)])

    with pytest.raises(agent.AgentError, match="did not finish"):
        agent.run_agent(45.5, -73.6, client=client)
    assert len(client.requests) == agent.MAX_MODEL_CALLS


def test_agent_stops_when_total_time_runs_out(services, monkeypatch):
    clock = iter([0, 10, agent.MAX_TOTAL_SECONDS + 1])
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(clock))
    client = FakeClient(turn(FIND), turn(STATUS), turn(submit(("A", "Closest."))))

    with pytest.raises(agent.AgentError, match="did not finish within 60 seconds"):
        agent.run_agent(45.5, -73.6, client=client)
    assert len(client.requests) == 1  # the second call was never started


def test_client_creation_failure_raises_agent_error(services, monkeypatch):
    def failing():
        raise ValueError("Project must be set")

    monkeypatch.setattr(agent, "make_client", failing)

    with pytest.raises(agent.AgentError, match="client could not be created"):
        agent.run_agent(45.5, -73.6)


# --- configuration -------------------------------------------------------------------

def test_hospital_kinds_cover_exactly_the_v1_specialised_list():
    assert set(agent.HOSPITAL_KINDS) == recommender.SPECIALISED_ER_IDS
    assert set(agent.HOSPITAL_KINDS.values()) == {"children", "mental_health", "cardiology"}


def test_make_client_uses_vertex_ai_with_project_and_location(monkeypatch):
    created = {}

    def fake_client(**kwargs):
        created.update(kwargs)
        return "client"

    monkeypatch.setattr(agent.genai, "Client", fake_client)
    monkeypatch.setenv(agent.PROJECT_ENV_VAR, "navcare-test")
    monkeypatch.delenv(agent.LOCATION_ENV_VAR, raising=False)

    assert agent.make_client() == "client"
    assert created["vertexai"] is True
    assert created["project"] == "navcare-test"
    assert created["location"] == "global"
    assert created["http_options"].timeout == agent.REQUEST_TIMEOUT_MS
    retry = created["http_options"].retry_options
    assert retry.attempts == 2
    assert 504 not in retry.http_status_codes  # a deadline retry would just wait again
    assert set(retry.http_status_codes) == {429, 500, 503}
