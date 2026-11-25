"""The Quebec ERs we know (data/hospitals.csv) with their live status, for GET /v1/hospitals.

Free to call: it reads our CSV and the public patient.quebec feed (MSSS data), with no Google
Routes or Gemini request. The usual-occupancy-by-hour pattern needs the stored hourly snapshots
(#21), so it is not here yet.
"""

import csv
import unicodedata
from functools import lru_cache

from app.recommender_v1.rules_based_recommender import SPECIALISED_ER_IDS, estimated_er_minutes
from app.services import hospital_status_getter
from app.services.nearest_hospital_getter import HOSPITALS_CSV_PATH


@lru_cache(maxsize=1)
def load_hospitals():
    """Every ER in data/hospitals.csv, in file order. Read once: the file only changes with a
    new deploy."""
    with open(HOSPITALS_CSV_PATH, encoding="utf-8", newline="") as file:
        return tuple(
            {
                "id": row["id"],
                "name": row["name"],
                "address": row["address"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "region": row["region"],
                # Children's, mental-health and cardiology ERs: v1 never ranks them.
                "specialised": row["id"] in SPECIALISED_ER_IDS,
            }
            for row in csv.DictReader(file)
        )


def live_status(emergency):
    """Our field names for patient.quebec's `emergency` object, or None when MSSS publishes no
    current data for this ER. Same rule as the recommenders (occupancy and average stay both
    needed), so an ER "live" here is never "no_current_data" in /v1/recommendations."""
    if estimated_er_minutes(emergency) is None:
        return None
    occupancy_pct = emergency["occupancyPct"]
    return {
        "stretchers": emergency.get("functionalStretchers"),
        "on_stretcher": emergency.get("occupiedStretchers"),
        "occupancy_rate": round(occupancy_pct / 100, 3),
        "present": emergency.get("patientsTotal"),
        "waiting": emergency.get("patientsWaitingForPhysician"),
        "over_24h": emergency.get("patientsOver24h"),
        "over_48h": emergency.get("patientsOver48h"),
        "avg_ambulatory_stay_hours": emergency.get("avgStayAmbulatoryHours"),
        "avg_stretcher_stay_hours": emergency.get("avgStayStretcherHours"),
    }


def with_status(hospital, er_data):
    status = live_status(er_data["by_id"].get(hospital["id"]))
    return {**hospital, "has_live_data": status is not None, "status": status}


def normalized(text):
    """For comparing names: trimmed, one Unicode form (a macOS "é" can be two characters), any
    letter case."""
    return unicodedata.normalize("NFC", (text or "").strip()).casefold()


def list_hospitals(region=None, has_live_data=None):
    """All ERs with their status, filtered by region (any case) and by has_live_data.

    Raises RuntimeError when the ER status feed fails (the API answers 503).
    """
    er_data = hospital_status_getter.get_er_data()
    hospitals = [with_status(hospital, er_data) for hospital in load_hospitals()]
    wanted = normalized(region)
    if wanted:
        hospitals = [h for h in hospitals if normalized(h["region"]) == wanted]
    if has_live_data is not None:
        hospitals = [h for h in hospitals if h["has_live_data"] == has_live_data]
    return {
        "data_updated_at": er_data["updated_at"],
        "stale": er_data["stale"],
        "count": len(hospitals),
        "hospitals": hospitals,
    }


def get_hospital(hospital_id):
    """One ER with its status, or None if the id is not in data/hospitals.csv.

    Raises RuntimeError when the ER status feed fails.
    """
    hospital = next((h for h in load_hospitals() if h["id"] == hospital_id), None)
    if hospital is None:
        return None
    er_data = hospital_status_getter.get_er_data()
    return {
        "data_updated_at": er_data["updated_at"],
        "stale": er_data["stale"],
        "hospital": with_status(hospital, er_data),
    }
