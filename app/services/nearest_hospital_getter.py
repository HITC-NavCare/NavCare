import math
from pathlib import Path

import pandas as pd
import requests

HOSPITALS_CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "hospitals.csv"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/"
REQUEST_TIMEOUT_SECONDS = 15
EARTH_RADIUS_KM = 6371.0
DEFAULT_RADIUS_KM = 50
# Enough candidates that a quieter ER a bit farther away is still considered,
# while keeping it to one routing request.
MAX_CANDIDATES = 15


def straight_line_km(lat1, lon1, lat2, lon2):
    """Great-circle (haversine) distance in km.

    Plain Pythagoras on degrees overstates east-west distance: in Québec a degree of
    longitude is about 30% shorter than a degree of latitude.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def osrm_drive_times(user_lat, user_lon, destinations):
    """Driving time and distance from the user to each destination, in one OSRM request.

    `destinations` is a list of (lat, lon). Returns a list of
    {"duration_min", "distance_km"} in the same order, or None where OSRM finds no route.
    Raises RuntimeError if OSRM fails.
    """
    # OSRM wants lon,lat. The user is coordinate 0; we only need rows from the user.
    coordinates = ";".join(
        f"{lon},{lat}" for lat, lon in [(user_lat, user_lon), *destinations]
    )
    url = f"{OSRM_TABLE_URL}{coordinates}?sources=0&annotations=duration,distance"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "Ok":
            raise ValueError(f"OSRM returned code {data.get('code')!r}")
        durations = data["durations"][0][1:]
        distances = data["distances"][0][1:]
    except (requests.RequestException, ValueError, KeyError, IndexError) as error:
        raise RuntimeError(f"Route request failed: {error}") from error

    return [
        None if duration is None or distance is None
        else {"duration_min": duration / 60, "distance_km": distance / 1000}
        for duration, distance in zip(durations, distances)
    ]


def get_candidate_hospitals(
    user_lat, user_lon, radius_km=DEFAULT_RADIUS_KM, max_candidates=MAX_CANDIDATES
):
    """Return hospitals within `radius_km` (straight line), nearest first, at most
    `max_candidates`, each with its road distance and driving time.

    `road_distance_km` and `travel_time_min` are None when OSRM finds no road route.
    """
    # Read id as text so it keeps its exact 8-digit form and matches the ER status ids.
    df = pd.read_csv(HOSPITALS_CSV_PATH, dtype={"id": str})

    df["straight_line_km"] = df.apply(
        lambda row: straight_line_km(user_lat, user_lon, row["lat"], row["lon"]),
        axis=1,
    )
    nearby = df[df["straight_line_km"] <= radius_km].nsmallest(
        max_candidates, "straight_line_km"
    )
    if nearby.empty:
        return []

    routes = osrm_drive_times(
        user_lat, user_lon, list(zip(nearby["lat"], nearby["lon"]))
    )

    results = []
    for (_, row), route in zip(nearby.iterrows(), routes):
        results.append({
            "id": row["id"],
            "name": row["name"],
            "address": row["address"],
            "latitude": float(row["lat"]),
            "longitude": float(row["lon"]),
            "road_distance_km": route["distance_km"] if route else None,
            "travel_time_min": route["duration_min"] if route else None,
        })
    return results
