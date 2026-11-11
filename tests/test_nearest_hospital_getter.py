import pytest
import requests

from app.services import nearest_hospital_getter as nearest
from tests.factories import FakeResponse

MONTREAL = (45.5017, -73.5673)
QUEBEC_CITY = (46.8139, -71.2080)


# --- straight_line_km ----------------------------------------------------------------

def test_distance_to_same_point_is_zero():
    assert nearest.straight_line_km(*MONTREAL, *MONTREAL) == 0


def test_montreal_to_quebec_city():
    # About 233 km in a straight line.
    assert nearest.straight_line_km(*MONTREAL, *QUEBEC_CITY) == pytest.approx(233, abs=2)


def test_distance_is_symmetric():
    there = nearest.straight_line_km(*MONTREAL, *QUEBEC_CITY)
    back = nearest.straight_line_km(*QUEBEC_CITY, *MONTREAL)
    assert there == pytest.approx(back)


def test_longitude_degree_is_shorter_than_latitude_degree_in_quebec():
    # The bug this replaced treated both as equal. At 45° N a degree of longitude is
    # about cos(45°) = 0.71 of a degree of latitude.
    one_degree_north = nearest.straight_line_km(45, -73, 46, -73)
    one_degree_east = nearest.straight_line_km(45, -73, 45, -72)
    assert one_degree_north == pytest.approx(111.2, abs=0.5)
    assert one_degree_east == pytest.approx(78.6, abs=0.5)


# --- osrm_drive_times ----------------------------------------------------------------

def fake_get(response, calls):
    def get(url, timeout):
        calls.append({"url": url, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response
    return get


def test_osrm_drive_times_builds_one_table_request(monkeypatch):
    calls = []
    table = {
        "code": "Ok",
        "durations": [[0, 600, 1500]],
        "distances": [[0, 5000, 12500]],
    }
    monkeypatch.setattr(nearest.requests, "get", fake_get(FakeResponse(table), calls))

    result = nearest.osrm_drive_times(45.5, -73.6, [(45.6, -73.7), (45.4, -73.5)])

    assert result == [
        {"duration_min": 10, "distance_km": 5},
        {"duration_min": 25, "distance_km": 12.5},
    ]
    assert len(calls) == 1
    # OSRM wants lon,lat; the user is first and the only source.
    assert calls[0]["url"] == (
        "https://router.project-osrm.org/table/v1/driving/"
        "-73.6,45.5;-73.7,45.6;-73.5,45.4?sources=0&annotations=duration,distance"
    )
    assert calls[0]["timeout"] == nearest.REQUEST_TIMEOUT_SECONDS


def test_osrm_drive_times_returns_none_when_no_route(monkeypatch):
    table = {"code": "Ok", "durations": [[0, None, 600]], "distances": [[0, None, 5000]]}
    monkeypatch.setattr(nearest.requests, "get", fake_get(FakeResponse(table), []))

    result = nearest.osrm_drive_times(45.5, -73.6, [(50.0, -60.0), (45.6, -73.7)])

    assert result == [None, {"duration_min": 10, "distance_km": 5}]


@pytest.mark.parametrize(
    "response",
    [
        requests.Timeout("timed out"),
        requests.ConnectionError("no network"),
        FakeResponse(status_code=500),
        FakeResponse(json_error=ValueError("not JSON")),
        FakeResponse({"code": "InvalidQuery", "message": "bad"}),
        FakeResponse({"code": "Ok"}),  # missing durations/distances
        FakeResponse({"code": "Ok", "durations": [], "distances": []}),
    ],
    ids=["timeout", "connection", "http_500", "bad_json", "osrm_error", "missing_keys", "empty"],
)
def test_osrm_failures_raise_clear_error(monkeypatch, response):
    monkeypatch.setattr(nearest.requests, "get", fake_get(response, []))

    with pytest.raises(RuntimeError, match="Route request failed"):
        nearest.osrm_drive_times(45.5, -73.6, [(45.6, -73.7)])


# --- get_candidate_hospitals ---------------------------------------------------------

@pytest.fixture
def fake_routes(monkeypatch):
    """Replace OSRM: drive time = 2 min per straight-line km. Records each call."""
    calls = []

    def drive_times(user_lat, user_lon, destinations):
        calls.append(destinations)
        return [
            {
                "duration_min": 2 * nearest.straight_line_km(user_lat, user_lon, lat, lon),
                "distance_km": 1.3 * nearest.straight_line_km(user_lat, user_lon, lat, lon),
            }
            for lat, lon in destinations
        ]

    monkeypatch.setattr(nearest, "osrm_drive_times", drive_times)
    return calls


def test_candidates_from_real_hospital_list(fake_routes):
    results = nearest.get_candidate_hospitals(*MONTREAL)

    assert 1 <= len(results) <= nearest.MAX_CANDIDATES
    assert len(fake_routes) == 1  # one routing request for all candidates
    for hospital in results:
        assert set(hospital) == {
            "id", "name", "address", "latitude", "longitude",
            "road_distance_km", "travel_time_min",
        }
        assert isinstance(hospital["id"], str) and len(hospital["id"]) == 8
        assert type(hospital["latitude"]) is float  # plain float, so JSON-safe
        distance = nearest.straight_line_km(*MONTREAL, hospital["latitude"], hospital["longitude"])
        assert distance <= nearest.DEFAULT_RADIUS_KM


def test_candidates_are_nearest_first(fake_routes):
    results = nearest.get_candidate_hospitals(*MONTREAL)

    distances = [
        nearest.straight_line_km(*MONTREAL, h["latitude"], h["longitude"]) for h in results
    ]
    assert distances == sorted(distances)


def test_radius_and_max_candidates_are_respected(fake_routes):
    assert len(nearest.get_candidate_hospitals(*MONTREAL, max_candidates=3)) == 3

    small = nearest.get_candidate_hospitals(*MONTREAL, radius_km=2)
    for hospital in small:
        assert nearest.straight_line_km(
            *MONTREAL, hospital["latitude"], hospital["longitude"]
        ) <= 2


def test_no_hospitals_in_radius_skips_routing(fake_routes):
    # Middle of the Gulf of St. Lawrence.
    assert nearest.get_candidate_hospitals(49.0, -62.0, radius_km=5) == []
    assert fake_routes == []


def test_ids_keep_leading_zeros_and_missing_routes_are_none(tmp_path, monkeypatch):
    csv_path = tmp_path / "hospitals.csv"
    csv_path.write_text(
        "id,name,address,lat,lon,region,pediatric\n"
        "01234567,NEAR,1 Near St,45.50,-73.57,Montréal,\n"
        "51234567,FAR,1 Far St,45.60,-73.57,Montréal,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(nearest, "HOSPITALS_CSV_PATH", csv_path)
    monkeypatch.setattr(
        nearest,
        "osrm_drive_times",
        lambda lat, lon, destinations: [{"duration_min": 3, "distance_km": 1}, None],
    )

    results = nearest.get_candidate_hospitals(45.5, -73.57)

    assert [h["id"] for h in results] == ["01234567", "51234567"]
    assert results[0]["travel_time_min"] == 3
    assert results[1]["travel_time_min"] is None
    assert results[1]["road_distance_km"] is None
