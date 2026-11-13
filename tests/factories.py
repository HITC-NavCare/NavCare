"""Small builders for test data, so each test only spells out what it cares about.

No test calls a real web service: responses are faked with FakeResponse.
"""

import requests


def make_hospital(hospital_id, travel_min, distance_km=None, name=None):
    """A candidate hospital as returned by get_candidate_hospitals."""
    if distance_km is None and travel_min is not None:
        distance_km = travel_min * 0.8
    return {
        "id": hospital_id,
        "name": name or f"Hospital {hospital_id}",
        "address": f"1 Example St, Montréal, QC ({hospital_id})",
        "latitude": 45.5,
        "longitude": -73.6,
        "road_distance_km": distance_km,
        "travel_time_min": travel_min,
    }


def make_status(occupancy_pct=100, stay_hours=4.0, waiting=10, over_24h=1):
    """ER status fields as returned by patient.quebec (`emergency` object)."""
    return {
        "functionalStretchers": 20,
        "occupiedStretchers": 20,
        "occupancyPct": occupancy_pct,
        "patientsTotal": 40,
        "patientsWaitingForPhysician": waiting,
        "patientsOver24h": over_24h,
        "patientsOver48h": 0,
        "avgStayStretcherHours": 15.0,
        "avgStayAmbulatoryHours": stay_hours,
    }


def make_er_data(by_id, stale=False, updated_at="2026-11-10T20:45:00.000Z"):
    """ER data as returned by get_er_data."""
    return {"updated_at": updated_at, "stale": stale, "by_id": by_id}


class FakeResponse:
    """Stands in for requests.Response."""

    def __init__(self, json_data=None, status_code=200, json_error=None):
        self._json_data = json_data
        self.status_code = status_code
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._json_data
