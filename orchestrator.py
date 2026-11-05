import json
from app.services.nearestHospitalGetter import get_candidate_hospitals
from app.services.hospitalStatusGetter import get_er_data
from app.services.weatherGetter import get_weather_data
from app.recommender_v1 import rules_based_recommender as recommender

user_lat = 45.5017
user_lon = -73.5673

def get_recommended_hospitals(user_lat, user_lon):


    #getting 4 candidate hospitals based on user location
    top4_hospitals = get_candidate_hospitals(user_lat, user_lon)
    top4_weather = []
    top4_status = []


    #getting weather data for each option
    for i in range(4):
        lat = top4_hospitals[i]["latitude"]
        lon = top4_hospitals[i]["longitude"]
        weather_data = get_weather_data(lat, lon)
        top4_weather.append(weather_data)
        #print(weather_data)

    # getting er/hospital status for each candidate hospital
    top4_status = []
    er_data = get_er_data()
    er_lookup = {
        hospital["name"]: hospital["emergency"]
        for hospital in er_data["hospitals"]
    }

    for hospital in top4_hospitals:
        top4_status.append(
            er_lookup.get(hospital["name"])
        )

    # combine all result to a single list of dictionaries for each hospital, weather and status
    bundled_results = [
        {
            "hospital": top4_hospitals[0],
            "weather": top4_weather[0],
            "status": top4_status[0]
        },
        {
            "hospital": top4_hospitals[1],
            "weather": top4_weather[1],
            "status": top4_status[1]
        },
        {
            "hospital": top4_hospitals[2],
            "weather": top4_weather[2],
            "status": top4_status[2]
        },
        {
            "hospital": top4_hospitals[3],
            "weather": top4_weather[3],
            "status": top4_status[3]
        },
    ]
    scores = recommender.recommend(bundled_results)
    for result, score in zip(bundled_results, scores):
        result["score"] = score
    #sorting result based on score, the lower the score the better the hospital is
    bundled_results.sort(key=lambda x: x["score"], reverse=False)
    return json.dumps(bundled_results, indent=4)
    
    