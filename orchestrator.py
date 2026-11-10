import json

from app.recommender_v1 import rules_based_recommender as recommender
from app.services.hospital_status_getter import get_er_data
from app.services.nearest_hospital_getter import DEFAULT_RADIUS_KM, get_candidate_hospitals
from app.services.weather_getter import get_weather_data

DISCLAIMER = "Not for life-threatening emergencies. Call 911."


def get_recommended_hospitals(
    user_lat, user_lon, limit=recommender.DEFAULT_LIMIT, radius_km=DEFAULT_RADIUS_KM
):
    """Return ranked ER recommendations for a location as a JSON string.

    Raises RuntimeError if a data service (routing, weather or ER status) fails.
    """
    # Hospitals near the user, with driving time from one routing request
    candidates = get_candidate_hospitals(user_lat, user_lon, radius_km=radius_km)

    # ER status for all hospitals, keyed by MSSS id (same id as data/hospitals.csv)
    er_data = get_er_data()

    # Weather at the user's location for the current hour; it affects the drive.
    weather_now = get_weather_data(user_lat, user_lon)[0]

    recommendations, unranked = recommender.recommend(candidates, er_data, weather_now, limit)

    return json.dumps(
        {
            "data_updated_at": er_data["updated_at"],
            "origin": {"lat": user_lat, "lon": user_lon},
            "radius_km": radius_km,
            "weather_now": weather_now,
            "travel_time_multiplier": recommender.travel_multiplier(weather_now),
            "recommendations": recommendations,
            "unranked_nearby": unranked,
            "disclaimer": DISCLAIMER,
        },
        indent=4,
        ensure_ascii=False,
    )
