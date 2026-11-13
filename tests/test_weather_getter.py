import numpy as np
import openmeteo_requests
import pandas as pd
import pytest

from app.services import weather_getter

# Order must match the "hourly" list requested in get_weather_data.
VARIABLES = [
    "temperature_2m", "rain", "showers", "snowfall",
    "snow_depth", "apparent_temperature", "precipitation",
]
HOUR_SECONDS = 3600


class FakeVariable:
    def __init__(self, values):
        self._values = values

    def ValuesAsNumpy(self):  # noqa: N802 (name defined by the Open-Meteo SDK)
        return self._values


class FakeHourly:
    """Hourly forecast where variable i has value i*1000 + hour index (easy to check)."""

    def __init__(self, start, hours):
        self._start = start
        self._hours = hours

    def Time(self):  # noqa: N802
        return self._start

    def TimeEnd(self):  # noqa: N802
        return self._start + self._hours * HOUR_SECONDS

    def Interval(self):  # noqa: N802
        return HOUR_SECONDS

    def Variables(self, index):  # noqa: N802
        return FakeVariable(np.arange(self._hours, dtype=np.float32) + index * 1000)


class FakeResponse:
    def __init__(self, hourly):
        self._hourly = hourly

    def Hourly(self):  # noqa: N802
        return self._hourly


class FakeClient:
    def __init__(self, hourly=None, error=None):
        self._hourly = hourly
        self._error = error
        self.calls = []

    def weather_api(self, url, params, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self._error:
            raise self._error
        return [FakeResponse(self._hourly)]


def midnight_utc_today():
    return int(pd.Timestamp.now(tz="UTC").floor("D").timestamp())


def test_returns_three_hours_starting_now(monkeypatch):
    # Start the fake forecast 2 days back, so "first rows" and "current hour" differ
    # at any time of day (at 00:xx UTC, a forecast starting today would hide the bug).
    start = midnight_utc_today() - 2 * 24 * HOUR_SECONDS
    client = FakeClient(FakeHourly(start, hours=96))
    monkeypatch.setattr(weather_getter, "openmeteo", client)

    # Read the clock before and after, so the test can't fail if the hour changes mid-call.
    hour_before = pd.Timestamp.now(tz="UTC").floor("h")
    rows = weather_getter.get_weather_data(45.5, -73.6)
    hour_after = pd.Timestamp.now(tz="UTC").floor("h")

    assert len(rows) == weather_getter.HOURS_RETURNED == 3
    first_hour = pd.Timestamp(rows[0]["date"])
    assert first_hour in (hour_before, hour_after)
    assert pd.Timestamp(rows[1]["date"]) == first_hour + pd.Timedelta(hours=1)
    # Values line up with the requested variable order.
    first_index = int((first_hour.timestamp() - start) // HOUR_SECONDS)
    for offset, row in enumerate(rows):
        for index, name in enumerate(VARIABLES):
            assert row[name] == index * 1000 + first_index + offset


def test_sends_location_variables_and_timeout(monkeypatch):
    client = FakeClient(FakeHourly(midnight_utc_today(), hours=48))
    monkeypatch.setattr(weather_getter, "openmeteo", client)

    weather_getter.get_weather_data(45.5, -73.6)

    call = client.calls[0]
    assert call["url"] == "https://api.open-meteo.com/v1/forecast"
    assert call["params"]["latitude"] == 45.5
    assert call["params"]["longitude"] == -73.6
    assert call["params"]["hourly"] == VARIABLES
    assert call["timeout"] == weather_getter.REQUEST_TIMEOUT_SECONDS


def test_dates_are_strings_so_json_can_serialise_them(monkeypatch):
    client = FakeClient(FakeHourly(midnight_utc_today(), hours=48))
    monkeypatch.setattr(weather_getter, "openmeteo", client)

    rows = weather_getter.get_weather_data(45.5, -73.6)

    assert all(isinstance(row["date"], str) for row in rows)


def test_forecast_only_in_the_past_raises(monkeypatch):
    two_days_ago = midnight_utc_today() - 2 * 24 * HOUR_SECONDS
    monkeypatch.setattr(weather_getter, "openmeteo", FakeClient(FakeHourly(two_days_ago, 24)))

    with pytest.raises(RuntimeError, match="no data for the current hour"):
        weather_getter.get_weather_data(45.5, -73.6)


def test_api_error_raises_clear_error(monkeypatch):
    error = openmeteo_requests.OpenMeteoRequestsError("failed to request: timed out")
    monkeypatch.setattr(weather_getter, "openmeteo", FakeClient(error=error))

    with pytest.raises(RuntimeError, match="Weather request failed"):
        weather_getter.get_weather_data(45.5, -73.6)


def test_cache_is_inside_ignored_snapshots_folder():
    assert weather_getter.CACHE_PATH.parent.name == "snapshots"
    assert weather_getter.CACHE_PATH.parent.parent.name == "data"
