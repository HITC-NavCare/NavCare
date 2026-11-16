import json

from app.agent_v2 import ai_agent_recommender as agent
from app.recommender_v1 import rules_based_recommender as recommender
from app.services.hospital_status_getter import get_er_data
from app.services.nearest_hospital_getter import DEFAULT_RADIUS_KM, get_candidate_hospitals
from app.services.weather_getter import get_weather_data

DISCLAIMER = "Not for life-threatening emergencies. Call 911."
# Shown when v2 falls back to v1 after the person wrote a message, since v1 cannot read it.
FALLBACK_NOTE = (
    "The assistant was unavailable, so your message was not taken into account. "
    "These are general ERs. If the patient is a child, or the problem is mental health "
    "or the heart, a specialised ER listed under unranked_nearby may be more suitable."
)


def build_recommendations(
    user_lat, user_lon, limit=recommender.DEFAULT_LIMIT, radius_km=DEFAULT_RADIUS_KM
):
    """v1 (rules): return ranked ER recommendations for a location as a dict (used by the API).

    Raises RuntimeError if a data service (routing, weather or ER status) fails.
    """
    # Hospitals near the user, with driving time from one routing request
    candidates = get_candidate_hospitals(user_lat, user_lon, radius_km=radius_km)

    # ER status for all hospitals, keyed by MSSS id (same id as data/hospitals.csv)
    er_data = get_er_data()

    # Weather at the user's location for the current hour; it affects the drive.
    weather_now = get_weather_data(user_lat, user_lon)[0]

    recommendations, unranked = recommender.recommend(candidates, er_data, weather_now, limit)

    return {
        "data_updated_at": er_data["updated_at"],
        "origin": {"lat": user_lat, "lon": user_lon},
        "radius_km": radius_km,
        "weather_now": weather_now,
        "travel_time_multiplier": recommender.travel_multiplier(weather_now),
        "recommendations": recommendations,
        "unranked_nearby": unranked,
        "disclaimer": DISCLAIMER,
    }


def build_agent_recommendations(
    user_lat, user_lon, message=None, limit=recommender.DEFAULT_LIMIT
):
    """v2 (AI agent): like v1, but Gemini chooses, using the person's optional message.

    Returns a dict with the v1 fields plus engine, call_911, note and model_usage. If Gemini
    fails or gives an invalid answer, the v1 ranking is returned instead, marked as a fallback.
    Raises RuntimeError if the fallback's data services fail too.
    """
    try:
        result = agent.run_agent(user_lat, user_lon, message, limit)
    except agent.AgentError as error:
        response = build_recommendations(user_lat, user_lon, limit=limit)
        response.update({
            "engine": "v1_fallback",
            "fallback_reason": str(error),
            "call_911": None,  # the message was not read
            "note": FALLBACK_NOTE if message and message.strip() else None,
            "model_usage": None,
        })
        return response

    return {
        "engine": "v2",
        "origin": {"lat": user_lat, "lon": user_lon},
        **result,
        "disclaimer": DISCLAIMER,
    }


def get_recommended_hospitals(
    user_lat, user_lon, limit=recommender.DEFAULT_LIMIT, radius_km=DEFAULT_RADIUS_KM
):
    """Same as build_recommendations, as a JSON string for printing (main.py)."""
    result = build_recommendations(user_lat, user_lon, limit=limit, radius_km=radius_km)
    return _to_json(result)


def get_agent_recommended_hospitals(
    user_lat, user_lon, message=None, limit=recommender.DEFAULT_LIMIT
):
    """Same as build_agent_recommendations, as a JSON string for printing (main.py)."""
    return _to_json(build_agent_recommendations(user_lat, user_lon, message, limit))


def _to_json(response):
    return json.dumps(response, indent=4, ensure_ascii=False)
