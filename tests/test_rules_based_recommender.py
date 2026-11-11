import pytest

from app.recommender_v1 import rules_based_recommender as rec
from tests.factories import make_er_data, make_hospital, make_status

NO_WEATHER = {"precipitation": 0.0, "snowfall": 0.0}


def ids(entries):
    return [entry["hospital"]["id"] for entry in entries]


def causes(unranked):
    return {entry["hospital"]["id"]: entry["cause"] for entry in unranked}


# --- travel_multiplier ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("weather", "expected"),
    [
        (None, 1.0),
        ({}, 1.0),
        ({"precipitation": 0.0, "snowfall": 0.0}, 1.0),
        ({"precipitation": 0.4, "snowfall": 0.0}, rec.RAIN_TRAVEL_MULTIPLIER),
        ({"precipitation": 0.4, "snowfall": 0.2}, rec.SNOW_TRAVEL_MULTIPLIER),
        ({"snowfall": 0.2}, rec.SNOW_TRAVEL_MULTIPLIER),
    ],
)
def test_travel_multiplier(weather, expected):
    assert rec.travel_multiplier(weather) == expected


# --- estimated_er_minutes ------------------------------------------------------------

@pytest.mark.parametrize(
    ("occupancy_pct", "expected_minutes"),
    [
        (100, 240),  # no adjustment
        (125, 300),
        (65, 156),
        (30, 120),   # capped at OCCUPANCY_FACTOR_MIN (0.5)
        (0, 120),
        (260, 480),  # capped at OCCUPANCY_FACTOR_MAX (2.0)
    ],
)
def test_estimated_er_minutes_scales_stay_by_occupancy(occupancy_pct, expected_minutes):
    status = make_status(occupancy_pct=occupancy_pct, stay_hours=4.0)
    assert rec.estimated_er_minutes(status) == pytest.approx(expected_minutes)


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        make_status(occupancy_pct=None),
        make_status(stay_hours=None),
    ],
)
def test_estimated_er_minutes_is_none_when_data_missing(status):
    assert rec.estimated_er_minutes(status) is None


# --- recommend: ranking --------------------------------------------------------------

def test_far_quiet_er_beats_near_crowded_er():
    """The product goal: A is 5 min away at 125%, B is 25 min at 65% -> B first."""
    candidates = [make_hospital("A", 5), make_hospital("B", 25)]
    er_data = make_er_data({"A": make_status(125), "B": make_status(65)})

    ranked, unranked = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ids(ranked) == ["B", "A"]
    assert [entry["expected_minutes"] for entry in ranked] == [181, 305]
    assert unranked == []


def test_near_er_wins_when_far_one_is_only_slightly_quieter():
    candidates = [make_hospital("A", 5), make_hospital("B", 25)]
    er_data = make_er_data({"A": make_status(105), "B": make_status(100)})

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ids(ranked) == ["A", "B"]


def test_ranks_are_sequential_and_sorted_by_expected_minutes():
    candidates = [make_hospital(str(i), 5 + i) for i in range(6)]
    occupancies = [180, 90, 140, 60, 120, 100]
    er_data = make_er_data(
        {str(i): make_status(occ) for i, occ in enumerate(occupancies)}
    )

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER, limit=6)

    assert [entry["rank"] for entry in ranked] == [1, 2, 3, 4, 5, 6]
    totals = [entry["expected_minutes"] for entry in ranked]
    assert totals == sorted(totals)


def test_limit_caps_the_number_of_recommendations():
    candidates = [make_hospital(str(i), 5 + i) for i in range(6)]
    er_data = make_er_data({str(i): make_status() for i in range(6)})

    ranked, unranked = rec.recommend(candidates, er_data, NO_WEATHER)

    assert len(ranked) == rec.DEFAULT_LIMIT == 4
    assert ids(ranked) == ["0", "1", "2", "3"]
    # Hospitals cut by the limit are not "unranked": they just ranked lower.
    assert unranked == []


def test_exact_tie_is_broken_by_shorter_drive():
    # A: 10 + 240 = 250. B: 70 + 180 = 250.
    candidates = [make_hospital("B", 70), make_hospital("A", 10)]
    er_data = make_er_data({"A": make_status(100), "B": make_status(75)})

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ids(ranked) == ["A", "B"]


def test_sorting_uses_unrounded_minutes():
    # A: 10.0 + 240.4 = 250.4. B: 10.2 + 240.0 = 250.2. Both round to 250, and a
    # rounded sort would then put A first on its shorter drive. B is really faster.
    candidates = [make_hospital("A", 10.0), make_hospital("B", 10.2)]
    er_data = make_er_data({
        "A": make_status(100, stay_hours=240.4 / 60),
        "B": make_status(100, stay_hours=4.0),
    })

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ids(ranked) == ["B", "A"]
    assert [entry["expected_minutes"] for entry in ranked] == [250, 250]


def test_snow_lengthens_drive_time():
    candidates = [make_hospital("A", 10)]
    er_data = make_er_data({"A": make_status(100)})

    ranked, _ = rec.recommend(candidates, er_data, {"precipitation": 1.0, "snowfall": 0.5})

    assert ranked[0]["travel_minutes"] == pytest.approx(13.0)
    assert ranked[0]["expected_minutes"] == 253


def test_bad_weather_can_change_the_winner():
    # A is 40 min closer, B saves 45 min in the ER.
    # Dry:  A 10 + 240 = 250, B 50 + 195 = 245 -> B.
    # Snow: A 13 + 240 = 253, B 65 + 195 = 260 -> A (the longer drive now costs more).
    candidates = [make_hospital("A", 10), make_hospital("B", 50)]
    er_data = make_er_data({
        "A": make_status(100, stay_hours=4.0),
        "B": make_status(100, stay_hours=195 / 60),
    })

    dry, _ = rec.recommend(candidates, er_data, NO_WEATHER)
    snowy, _ = rec.recommend(candidates, er_data, {"precipitation": 1.0, "snowfall": 1.0})

    assert ids(dry) == ["B", "A"]
    assert ids(snowy) == ["A", "B"]


# --- recommend: output fields --------------------------------------------------------

def test_recommendation_fields():
    candidates = [make_hospital("51236313", 7.34, distance_km=3.456, name="HÔPITAL NOTRE-DAME")]
    er_data = make_er_data(
        {"51236313": make_status(131, stay_hours=6.21, waiting=31, over_24h=4)}
    )

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER)
    entry = ranked[0]

    assert entry["rank"] == 1
    assert entry["hospital"] == {
        "id": "51236313",
        "name": "HÔPITAL NOTRE-DAME",
        "address": "1 Example St, Montréal, QC (51236313)",
        "lat": 45.5,
        "lon": -73.6,
    }
    assert entry["travel_minutes"] == 7.3
    assert entry["distance_km"] == 3.5
    assert entry["estimated_wait_minutes"] == 488  # 6.21 h * 60 * 1.31
    assert entry["expected_minutes"] == 495
    assert entry["occupancy_rate"] == pytest.approx(1.31)
    assert entry["patients_waiting"] == 31
    assert entry["patients_over_24h"] == 4
    assert entry["avg_ambulatory_stay_hours"] == 6.21


def test_reasons_compare_with_the_top_ranked_er():
    candidates = [make_hospital("A", 5), make_hospital("B", 25)]
    er_data = make_er_data({"A": make_status(125), "B": make_status(65)})

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ranked[0]["reason"] == "Fastest expected care: about 25 min drive + 156 min in the ER."
    assert ranked[1]["reason"] == "Compared with #1: 20 min shorter drive, 144 min more in the ER."


def test_reason_for_longer_drive_but_less_time_in_er():
    # #1: 5 + 200 = 205. #2: 30 + 190 = 220.
    candidates = [make_hospital("A", 5), make_hospital("B", 30)]
    er_data = make_er_data({
        "A": make_status(100, stay_hours=200 / 60),
        "B": make_status(100, stay_hours=190 / 60),
    })

    ranked, _ = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ranked[1]["reason"] == "Compared with #1: 25 min longer drive, 10 min less in the ER."


# --- recommend: hospitals that can't be ranked ---------------------------------------

def test_unrankable_hospitals_are_listed_with_a_cause():
    specialised_id = "51235190"  # Hôpital Douglas
    candidates = [
        make_hospital("OK", 9),
        make_hospital("NO_ROUTE", None),
        make_hospital("NO_STATUS", 5),
        make_hospital("NO_OCCUPANCY", 6),
        make_hospital(specialised_id, 3),
    ]
    er_data = make_er_data({
        "OK": make_status(100),
        "NO_ROUTE": make_status(90),
        "NO_OCCUPANCY": make_status(occupancy_pct=None),
        specialised_id: make_status(30),
    })

    ranked, unranked = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ids(ranked) == ["OK"]
    assert causes(unranked) == {
        "NO_ROUTE": "no_route",
        "NO_STATUS": "no_current_data",
        "NO_OCCUPANCY": "no_current_data",
        specialised_id: "specialised",
    }
    assert set(unranked[0]) == {"hospital", "cause"}


def test_specialised_check_comes_before_route_check():
    specialised_id = "51222800"  # CHU Sainte-Justine
    candidates = [make_hospital(specialised_id, None)]

    _, unranked = rec.recommend(candidates, make_er_data({}), NO_WEATHER)

    assert causes(unranked) == {specialised_id: "specialised"}


def test_stale_feed_ranks_nothing():
    candidates = [make_hospital("A", 5), make_hospital("B", None)]
    er_data = make_er_data({"A": make_status(), "B": make_status()}, stale=True)

    ranked, unranked = rec.recommend(candidates, er_data, NO_WEATHER)

    assert ranked == []
    assert causes(unranked) == {"A": "stale_data", "B": "no_route"}


def test_no_candidates():
    assert rec.recommend([], make_er_data({}), NO_WEATHER) == ([], [])


def test_works_without_weather():
    candidates = [make_hospital("A", 10)]

    ranked, _ = rec.recommend(candidates, make_er_data({"A": make_status()}), None)

    assert ranked[0]["travel_minutes"] == 10.0


def test_does_not_modify_inputs():
    candidates = [make_hospital("A", 5)]
    er_data = make_er_data({"A": make_status()})
    before = (repr(candidates), repr(er_data))

    rec.recommend(candidates, er_data, NO_WEATHER)

    assert (repr(candidates), repr(er_data)) == before
