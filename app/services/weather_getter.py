from pathlib import Path

import openmeteo_requests
import pandas as pd
import requests_cache
from retry_requests import retry

# Inside data/snapshots/, which Git ignores, so the cache file is never committed.
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "snapshots" / "weather_cache"
REQUEST_TIMEOUT_SECONDS = 15
HOURS_RETURNED = 3

CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
# Setup the Open-Meteo API client with cache and retry on error (once, shared by all calls)
cache_session = requests_cache.CachedSession(str(CACHE_PATH), expire_after=3600)
retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
openmeteo = openmeteo_requests.Client(session=retry_session)


def get_weather_data(lat, lon):
    """Return the forecast for the current hour and the next 2 hours (first item = now).

    Raises RuntimeError if Open-Meteo fails.
    """

    # Make sure all required weather variables are listed here
    # The order of variables in hourly or daily is important to assign them correctly below
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": [
            "temperature_2m", 
            "rain", "showers", 
            "snowfall", 
            "snow_depth", 
            "apparent_temperature", 
            "precipitation"],
    }
    try:
        responses = openmeteo.weather_api(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except openmeteo_requests.OpenMeteoRequestsError as error:
        raise RuntimeError(f"Weather request failed: {error}") from error

    # Process first location. Add a for-loop for multiple locations or weather models
    response = responses[0]

    # Process hourly data. The order of variables needs to be the same as requested.
    hourly = response.Hourly()
    hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()
    hourly_rain = hourly.Variables(1).ValuesAsNumpy()
    hourly_showers = hourly.Variables(2).ValuesAsNumpy()
    hourly_snowfall = hourly.Variables(3).ValuesAsNumpy()
    hourly_snow_depth = hourly.Variables(4).ValuesAsNumpy()
    hourly_apparent_temperature = hourly.Variables(5).ValuesAsNumpy()
    hourly_precipitation = hourly.Variables(6).ValuesAsNumpy()

    hourly_data = {
        "date": pd.date_range(
            start = pd.to_datetime(hourly.Time(), unit = "s", utc = True),
            end =  pd.to_datetime(hourly.TimeEnd(), unit = "s", utc = True),
            freq = pd.Timedelta(seconds = hourly.Interval()),
            inclusive = "left"
        )
    }

    hourly_data["temperature_2m"] = hourly_temperature_2m
    hourly_data["rain"] = hourly_rain
    hourly_data["showers"] = hourly_showers
    hourly_data["snowfall"] = hourly_snowfall
    hourly_data["snow_depth"] = hourly_snow_depth
    hourly_data["apparent_temperature"] = hourly_apparent_temperature
    hourly_data["precipitation"] = hourly_precipitation

    hourly_dataframe = pd.DataFrame(data = hourly_data)
    # The forecast starts at 00:00 UTC today, so skip ahead to the current hour.
    current_hour = pd.Timestamp.now(tz="UTC").floor("h")
    upcoming = hourly_dataframe[hourly_dataframe["date"] >= current_hour].head(HOURS_RETURNED)
    if upcoming.empty:
        raise RuntimeError("Weather forecast has no data for the current hour")
    upcoming = upcoming.copy()
    upcoming["date"] = upcoming["date"].astype(str)
    return upcoming.to_dict(orient="records")
