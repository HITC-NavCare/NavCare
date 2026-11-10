"""Rule-based (v1) recommender: rank ERs by expected minutes until care, lowest first.

    expected_minutes = drive time (adjusted for weather) + estimated time in the ER

Estimated time in the ER starts from the ER's average stay for ambulatory (walk-in)
patients, published by MSSS for the previous day, and is scaled by the ER's current
stretcher occupancy, so a crowded ER today counts as slower than its usual average.

Limitations, stated plainly:
- MSSS publishes no "wait to see a doctor" time. Average ambulatory stay includes treatment,
  so the estimate is longer than the real wait; it is used to compare ERs, not as a promise.
- Occupancy measures stretchers, not walk-in patients; it is used as a sign of how busy the ER is.
- An ER missing either value is not ranked (listed in unranked_nearby) rather than guessed.
"""

# Occupancy 100% = no adjustment. The factor is capped so one odd reading
# (for example 0% or 260%) can't dominate the ranking.
OCCUPANCY_FACTOR_MIN = 0.5
OCCUPANCY_FACTOR_MAX = 2.0

# Extra driving time in bad weather. Team-tunable assumptions, not measured values.
RAIN_TRAVEL_MULTIPLIER = 1.15
SNOW_TRAVEL_MULTIPLIER = 1.3

DEFAULT_LIMIT = 4

# ERs that serve only certain patients. They are in the MSSS file, and their short stays
# would otherwise put them at the top for everyone. Identified from the facility's own
# name/mission; kept out of general recommendations and listed as unranked "specialised".
SPECIALISED_ER_IDS = {
    "51222800",  # CHU Sainte-Justine (children)
    "51223279",  # L'Hôpital de Montréal pour enfants (children)
    "51235190",  # Hôpital Douglas (mental health)
    "51235307",  # Institut universitaire en santé mentale de Montréal (mental health)
    "51848752",  # Hôpital en santé mentale Albert-Prévost (mental health)
    "51248201",  # Institut de cardiologie de Montréal (cardiology)
    "51232130",  # Institut universitaire de cardiologie et de pneumologie de Québec
}


def travel_multiplier(weather_now):
    """Multiplier for driving time given the current weather at the user's location."""
    if weather_now is None:
        return 1.0
    if weather_now.get("snowfall", 0) > 0:
        return SNOW_TRAVEL_MULTIPLIER
    if weather_now.get("precipitation", 0) > 0:
        return RAIN_TRAVEL_MULTIPLIER
    return 1.0


def estimated_er_minutes(status):
    """Estimated minutes in the ER, or None when the data needed is missing."""
    if not status:
        return None
    stay_hours = status.get("avgStayAmbulatoryHours")
    occupancy_pct = status.get("occupancyPct")
    if stay_hours is None or occupancy_pct is None:
        return None
    factor = min(max(occupancy_pct / 100, OCCUPANCY_FACTOR_MIN), OCCUPANCY_FACTOR_MAX)
    return stay_hours * 60 * factor


def hospital_summary(hospital):
    return {
        "id": hospital["id"],
        "name": hospital["name"],
        "address": hospital["address"],
        "lat": hospital["latitude"],
        "lon": hospital["longitude"],
    }


def explain(entry, best):
    """One-line reason, comparing this ER with the top-ranked one."""
    if entry is best:
        return (
            f"Fastest expected care: about {entry['travel_minutes']:.0f} min drive "
            f"+ {entry['estimated_wait_minutes']:.0f} min in the ER."
        )
    extra_drive = entry["travel_minutes"] - best["travel_minutes"]
    extra_wait = entry["estimated_wait_minutes"] - best["estimated_wait_minutes"]
    drive_text = f"{abs(extra_drive):.0f} min {'longer' if extra_drive >= 0 else 'shorter'} drive"
    wait_text = f"{abs(extra_wait):.0f} min {'more' if extra_wait >= 0 else 'less'} in the ER"
    return f"Compared with #1: {drive_text}, {wait_text}."


def recommend(candidates, er_data, weather_now, limit=DEFAULT_LIMIT):
    """Rank candidate hospitals by expected minutes until care.

    candidates: from get_candidate_hospitals (with travel_time_min, may be None).
    er_data: from get_er_data ({"stale", "by_id", ...}).
    weather_now: current-hour weather at the user's location (or None).
    Returns (recommendations, unranked_nearby).
    """
    multiplier = travel_multiplier(weather_now)
    ranked = []
    unranked = []

    for hospital in candidates:
        status = er_data["by_id"].get(hospital["id"])
        if hospital["id"] in SPECIALISED_ER_IDS:
            unranked.append({"hospital": hospital_summary(hospital), "cause": "specialised"})
            continue
        if hospital["travel_time_min"] is None:
            unranked.append({"hospital": hospital_summary(hospital), "cause": "no_route"})
            continue
        if er_data["stale"]:
            unranked.append({"hospital": hospital_summary(hospital), "cause": "stale_data"})
            continue
        er_minutes = estimated_er_minutes(status)
        if er_minutes is None:
            unranked.append({"hospital": hospital_summary(hospital), "cause": "no_current_data"})
            continue

        travel_minutes = hospital["travel_time_min"] * multiplier
        # Sort on the unrounded total so near-ties are ordered correctly;
        # travel time breaks exact ties.
        sort_key = (travel_minutes + er_minutes, travel_minutes)
        ranked.append((sort_key, {
            "hospital": hospital_summary(hospital),
            "travel_minutes": round(travel_minutes, 1),
            "distance_km": round(hospital["road_distance_km"], 1),
            "estimated_wait_minutes": round(er_minutes),
            "expected_minutes": round(travel_minutes + er_minutes),
            "occupancy_rate": status["occupancyPct"] / 100,
            "patients_waiting": status.get("patientsWaitingForPhysician"),
            "patients_over_24h": status.get("patientsOver24h"),
            "avg_ambulatory_stay_hours": status["avgStayAmbulatoryHours"],
        }))

    ranked.sort(key=lambda pair: pair[0])
    ranked = [entry for _, entry in ranked[:limit]]
    for rank, entry in enumerate(ranked, start=1):
        entry["rank"] = rank
        entry["reason"] = explain(entry, ranked[0])

    return ranked, unranked
