import math
import pandas as pd
import requests

# User location
user_lat = 45.5017
user_lon = -73.5673

#hospitals.csv path
hospitals_csv_path = "./../data/hospitals.csv"
    
def distance_approx(lat1, lon1, lat2, lon2):#pythagoras
    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = dlat**2 + dlon**2
    return a


def osrm_route(user_lat, user_lon, hosp_lat, hosp_lon):
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{user_lon},{user_lat};{hosp_lon},{hosp_lat}"
        "?overview=false"
    )

    r = requests.get(url)
    data = r.json()

    route = data["routes"][0]

    return {
        "distance_km": route["distance"] / 1000,
        "duration_min": route["duration"] / 60
    }



def get_candidate_hospitals(user_lat, user_lon):
    # Load CSV
    df = pd.read_csv(hospitals_csv_path)


    # Straight-line distance
    df["crow_distance"] = df.apply(
        lambda row:
            distance_approx(
                user_lat,
                user_lon,
                row["lat"],
                row["lon"]
            ),
        axis=1
    )

    # Take closest 4 hospitals
    candidates = df.nsmallest(4, "crow_distance")

    results = []

    for _, row in candidates.iterrows():

        route = osrm_route(
            user_lat,
            user_lon,
            row["lat"],
            row["lon"]
        )

        results.append({
            "name": row["name"],
            "address": row["address"],
            "latitude": row["lat"],
            "longitude": row["lon"],
            "road_distance_km": route["distance_km"],
            "travel_time_min": route["duration_min"]
        })

    results = sorted(
        results,
        key=lambda x: x["travel_time_min"]
    )

    return results