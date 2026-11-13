import math
import os
from pathlib import Path

import pandas as pd
import requests

HOSPITALS_CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "hospitals.csv"
ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
API_KEY_ENV_VAR = "GOOGLE_MAPS_API_KEY"
# TRAFFIC_AWARE uses live traffic and is billed at the Pro rate (5,000 free elements a
# month; each recommendation uses one element per candidate hospital).
ROUTING_PREFERENCE = "TRAFFIC_AWARE"
# Google returns only the fields listed here. `status` is needed to see per-route errors.
FIELD_MASK = "originIndex,destinationIndex,status,condition,distanceMeters,duration"
REQUEST_TIMEOUT_SECONDS = 15
EARTH_RADIUS_KM = 6371.0
DEFAULT_RADIUS_KM = 50
# Enough candidates that a quieter ER a bit farther away is still considered,
# while keeping it to one routing request (and its cost to 15 billed elements).
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


def get_drive_times(user_lat, user_lon, destinations):
    """Driving time (with live traffic) and distance from the user to each destination,
    in one Google Routes request.

    `destinations` is a list of (lat, lon). Returns a list of
    {"duration_min", "distance_km"} in the same order, or None where there is no route.
    Raises RuntimeError if the key is missing or the request fails.
    """
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(f"Route request failed: {API_KEY_ENV_VAR} is not set")

    body = {
        "origins": [_waypoint(user_lat, user_lon)],
        "destinations": [_waypoint(lat, lon) for lat, lon in destinations],
        "travelMode": "DRIVE",
        "routingPreference": ROUTING_PREFERENCE,
    }
    # The key goes in a header, not the URL, so it never shows up in error messages.
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    try:
        response = requests.post(
            ROUTE_MATRIX_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        elements = response.json()
        if not isinstance(elements, list):
            raise ValueError("unexpected response format")
        # Elements can come back in any order, and JSON leaves out an index of 0.
        by_destination = {}
        for element in elements:
            index = element.get("destinationIndex", 0)
            if element.get("status", {}).get("code"):
                # Google could not route this one destination. Treat it as "no route"
                # so the other hospitals can still be ranked, instead of failing them all.
                by_destination[index] = {"condition": "ROUTE_NOT_FOUND"}
            else:
                by_destination[index] = element
        return [_route(by_destination[index]) for index in range(len(destinations))]
    except (requests.RequestException, ValueError, KeyError, AttributeError) as error:
        raise RuntimeError(f"Route request failed: {error}") from error


def _waypoint(lat, lon):
    return {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lon}}}}


def _route(element):
    """One matrix element as {"duration_min", "distance_km"}, or None if no route."""
    if element.get("condition") != "ROUTE_EXISTS":
        return None
    # Duration is a string of seconds such as "160s".
    seconds = float(element["duration"].removesuffix("s"))
    return {
        "duration_min": seconds / 60,
        "distance_km": element.get("distanceMeters", 0) / 1000,
    }


def get_candidate_hospitals(
    user_lat, user_lon, radius_km=DEFAULT_RADIUS_KM, max_candidates=MAX_CANDIDATES
):
    """Return hospitals within `radius_km` (straight line), nearest first, at most
    `max_candidates`, each with its road distance and driving time.

    `road_distance_km` and `travel_time_min` are None when there is no road route.
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

    routes = get_drive_times(
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
