import json
import logging

from app import red_flags
from app.agent_v2 import ai_agent_recommender as agent
from app.agent_v2 import fault_injection
from app.recommender_v1 import rules_based_recommender as recommender
from app.services.hospital_status_getter import get_er_data
from app.services.nearest_hospital_getter import DEFAULT_RADIUS_KM, get_candidate_hospitals
from app.services.weather_getter import get_weather_data

DISCLAIMER = "Not for life-threatening emergencies. Call 911."
logger = logging.getLogger("navcare.orchestrator")
# v1 cannot read the person's message. These notes say so when v1 answers a request that
# had one: MESSAGE_NOT_READ_NOTE when the facade chose v1, FALLBACK_NOTE when v2 failed.
_SPECIALISED_HINT = (
    "These are general ERs. If the patient is a child, or the problem is mental health "
    "or the heart, a specialised ER listed under unranked_nearby may be more suitable."
)
MESSAGE_NOT_READ_NOTE = "Your message was not taken into account. " + _SPECIALISED_HINT
FALLBACK_NOTE = (
    "The assistant was unavailable, so your message was not taken into account. "
    + _SPECIALISED_HINT
)


def call_911_without_model(message):
    """call_911 when v1 answers or v2 fell back: True if the message has a clear emergency
    phrase (app/red_flags.py), otherwise None, because nobody read the rest of the message.
    """
    return True if red_flags.mentions_emergency(message) else None


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
    user_lat, user_lon, message=None, limit=recommender.DEFAULT_LIMIT,
    radius_km=DEFAULT_RADIUS_KM,
):
    """v2 (AI agent): like v1, but Gemini chooses, using the person's optional message.

    Returns a dict with the v1 fields plus engine, call_911, note and model_usage. If Gemini
    fails or gives an invalid answer, the v1 ranking is returned instead, marked as a fallback.
    The same happens on any unexpected error in v2 (a bug): a person still gets a hospital
    list instead of a 500, and the error's stack trace is logged.
    radius_km is only used by that fallback; the agent chooses its own search radius.
    Raises RuntimeError if the fallback's data services fail too.
    """
    try:
        started = fault_injection.apply_fault()  # off unless enabled; slow mode's wait start
        result = agent.run_agent(user_lat, user_lon, message, limit, started=started)
    except Exception as error:
        if isinstance(error, agent.AgentError):
            reason = str(error)
        else:
            reason = f"unexpected {type(error).__name__} in v2: {error}"
            logger.error("v2 failed unexpectedly; v1 answers instead", exc_info=error)
        response = build_recommendations(user_lat, user_lon, limit=limit, radius_km=radius_km)
        response.update({
            "engine": "v1_fallback",
            "fallback_reason": reason,
            "call_911": call_911_without_model(message),  # the model did not read it
            "note": FALLBACK_NOTE if message and message.strip() else None,
            # What v2 used before failing, for cost. run_agent adds it to any error it raises.
            "model_usage": getattr(error, "model_usage", None),
        })
        return response

    return {
        "engine": "v2",
        "origin": {"lat": user_lat, "lon": user_lon},
        **result,
        # The safety net can raise the model's call_911, never lower it.
        "call_911": result["call_911"] or red_flags.mentions_emergency(message),
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
