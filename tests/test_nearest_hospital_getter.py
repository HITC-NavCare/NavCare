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


# --- get_drive_times -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_api_key(monkeypatch):
    monkeypatch.setenv(nearest.API_KEY_ENV_VAR, "test-key")


def fake_post(response, calls):
    def post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response
    return post


def element(destination_index, seconds, meters):
    return {
        "originIndex": 0,
        "destinationIndex": destination_index,
        "status": {},
        "condition": "ROUTE_EXISTS",
        "distanceMeters": meters,
        "duration": f"{seconds}s",
    }


def test_get_drive_times_builds_one_matrix_request(monkeypatch):
    calls = []
    matrix = [element(0, 600, 5000), element(1, 1500, 12500)]
    monkeypatch.setattr(nearest.requests, "post", fake_post(FakeResponse(matrix), calls))

    result = nearest.get_drive_times(45.5, -73.6, [(45.6, -73.7), (45.4, -73.5)])

    assert result == [
        {"duration_min": 10, "distance_km": 5},
        {"duration_min": 25, "distance_km": 12.5},
    ]
    assert len(calls) == 1
    assert calls[0]["url"] == nearest.ROUTE_MATRIX_URL
    assert calls[0]["timeout"] == nearest.REQUEST_TIMEOUT_SECONDS
    assert calls[0]["headers"] == {
        "X-Goog-Api-Key": "test-key",
        "X-Goog-FieldMask": nearest.FIELD_MASK,
    }
    assert "status" in nearest.FIELD_MASK  # without it every route looks OK

    def waypoint(lat, lon):
        return {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lon}}}}

    assert calls[0]["json"] == {
        "origins": [waypoint(45.5, -73.6)],  # the user is the only origin
        "destinations": [waypoint(45.6, -73.7), waypoint(45.4, -73.5)],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }


def test_get_drive_times_matches_results_by_index_not_order(monkeypatch):
    # Google may return elements in any order, and JSON omits an index of 0.
    first = element(0, 600, 5000)
    del first["originIndex"], first["destinationIndex"]
    matrix = [element(2, 60, 1000), first, element(1, 1500, 12500)]
    monkeypatch.setattr(nearest.requests, "post", fake_post(FakeResponse(matrix), []))

    result = nearest.get_drive_times(
        45.5, -73.6, [(45.6, -73.7), (45.4, -73.5), (45.51, -73.61)]
    )

    assert [route["duration_min"] for route in result] == [10, 25, 1]


def test_get_drive_times_returns_none_when_no_route(monkeypatch):
    no_route = {"destinationIndex": 0, "status": {}, "condition": "ROUTE_NOT_FOUND"}
    matrix = [no_route, element(1, 600, 5000)]
    monkeypatch.setattr(nearest.requests, "post", fake_post(FakeResponse(matrix), []))

    result = nearest.get_drive_times(45.5, -73.6, [(50.0, -60.0), (45.6, -73.7)])

    assert result == [None, {"duration_min": 10, "distance_km": 5}]


@pytest.mark.parametrize(
    "response",
    [
        requests.Timeout("timed out"),
        requests.ConnectionError("no network"),
        FakeResponse(status_code=403),
        FakeResponse(json_error=ValueError("not JSON")),
        FakeResponse({"error": {"code": 400, "message": "bad"}}),
        FakeResponse([]),  # no element for the destination
        FakeResponse([{"destinationIndex": 0, "status": {"code": 3, "message": "bad"}}]),
        FakeResponse([{"destinationIndex": 0, "status": {}, "condition": "ROUTE_EXISTS"}]),
    ],
    ids=[
        "timeout", "connection", "http_403", "bad_json", "error_object", "empty",
        "element_error", "missing_duration",
    ],
)
def test_route_failures_raise_clear_error(monkeypatch, response):
    monkeypatch.setattr(nearest.requests, "post", fake_post(response, []))

    with pytest.raises(RuntimeError, match="Route request failed"):
        nearest.get_drive_times(45.5, -73.6, [(45.6, -73.7)])


def test_missing_api_key_raises_clear_error_without_calling_google(monkeypatch):
    calls = []
    monkeypatch.delenv(nearest.API_KEY_ENV_VAR)
    monkeypatch.setattr(nearest.requests, "post", fake_post(FakeResponse([]), calls))

    with pytest.raises(RuntimeError, match="GOOGLE_MAPS_API_KEY is not set"):
        nearest.get_drive_times(45.5, -73.6, [(45.6, -73.7)])
    assert calls == []


# --- get_candidate_hospitals ---------------------------------------------------------

@pytest.fixture
def fake_routes(monkeypatch):
    """Replace Google Routes: drive time = 2 min per straight-line km. Records each call."""
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

    monkeypatch.setattr(nearest, "get_drive_times", drive_times)
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
        "get_drive_times",
        lambda lat, lon, destinations: [{"duration_min": 3, "distance_km": 1}, None],
    )

    results = nearest.get_candidate_hospitals(45.5, -73.57)

    assert [h["id"] for h in results] == ["01234567", "51234567"]
    assert results[0]["travel_time_min"] == 3
    assert results[1]["travel_time_min"] is None
    assert results[1]["road_distance_km"] is None
