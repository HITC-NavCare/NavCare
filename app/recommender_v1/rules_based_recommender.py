
min_distance = float('inf')
max_distance = float('-inf')
min_travel_time = float('inf')
max_travel_time = float('-inf')
min_precipitation = float('inf')
max_precipitation = float('-inf')

def normalize(bundled_results):
    global min_distance, max_distance, min_travel_time
    global max_travel_time, min_precipitation, max_precipitation,occupancy_min
    global  occupancy_max, waiting_min, waiting_max, over24_min
    global over24_max, over48_min, over48_max, stay_min, stay_max
    distances = [
        hospital["hospital"]["road_distance_km"]
        for hospital in bundled_results
    ]
    min_distance = min(distances)
    max_distance = max(distances)

    travel_times = [
    h["hospital"]["travel_time_min"]
    for h in bundled_results
    ]
    min_travel_time = min(travel_times)
    max_travel_time = max(travel_times)

    precipitations = [
        h["weather"][0]["precipitation"]
        for h in bundled_results
    ]
    min_precipitation = min(precipitations)
    max_precipitation = max(precipitations)

    occupancies = []
    waitings = []
    over24s = []
    over48s = []
    stays = []

    for hospital in bundled_results:

        status = hospital["status"]
        if status is None:
            occupancies.append(1)
            waitings.append(1)
            over24s.append(1)
            over48s.append(1)
            stays.append(1)
            continue

        if status["occupancyPct"] is not None:
            occupancies.append(status["occupancyPct"])

        if status["patientsWaitingForPhysician"] is not None:
            waitings.append(
                status["patientsWaitingForPhysician"]
            )

        if status["patientsOver24h"] is not None:
            over24s.append(status["patientsOver24h"])

        if status["patientsOver48h"] is not None:
            over48s.append(status["patientsOver48h"])

        if status["avgStayStretcherHours"] is not None:
            stays.append(
                status["avgStayStretcherHours"]
            )
    occupancy_min = min(occupancies)
    occupancy_max = max(occupancies)

    waiting_min = min(waitings)
    waiting_max = max(waitings)

    over24_min = min(over24s)
    over24_max = max(over24s)

    over48_min = min(over48s)
    over48_max = max(over48s)

    stay_min = min(stays)
    stay_max = max(stays)

def distance_score(dist):
    s=(dist - min_distance) / (max_distance - min_distance)
    score =  s* 100 if max_distance != min_distance else 1
    return score

def precipitation_score(precipitation):
    a=(precipitation - min_precipitation) / (max_precipitation - min_precipitation)
    score =  a* 100 if max_precipitation != min_precipitation else 1
    return score

def traveltime_score(traveltime):
    s=(traveltime - min_travel_time) / (max_travel_time - min_travel_time)
    score =  s* 100 if max_travel_time != min_travel_time else 1
    return score

def er_status_score(status):
    score = 0
    if status is None:
        return 1
    occupancy = status.get("occupancyPct")
    waiting = status.get("patientsWaitingForPhysician")
    over24 = status.get("patientsOver24h")
    over48 = status.get("patientsOver48h")
    stay = status.get("avgStayStretcherHours")
    if occupancy is None:
        occupancy = occupancy_max
    if waiting is None:
        waiting = waiting_max
    if over24 is None:
        over24 = over24_max
    if over48 is None:
        over48 = over48_max
    if stay is None:
        stay = stay_max
    a= (occupancy - occupancy_min) / (occupancy_max - occupancy_min)
    b = (waiting - waiting_min) / (waiting_max - waiting_min)
    c = (over24 - over24_min) / (over24_max - over24_min)
    d = (over48 - over48_min) / (over48_max - over48_min)
    e = (stay - stay_min) / (stay_max - stay_min)

    occ_score =  a if occupancy_max != occupancy_min else 1
    wait_score = b if waiting_max != waiting_min else 1
    over24_score = c if over24_max != over24_min else 1
    over48_score = d if over48_max != over48_min else 1
    stay_score = e if stay_max != stay_min else 1

    f= 0.1 * occ_score + 0.3 * wait_score + 0.2
    g = 0.2 * over24_score + 0.2 * over48_score 
    h= 0.2 * stay_score
    score =  f+g+h

    return score

def recommend(bundled_results):
    # the higher the score, the worse the hospital is (its close to a penalty score)
    normalize(bundled_results)
    scores = []
    for hospital in bundled_results:
        distance_val = distance_score(hospital["hospital"]["road_distance_km"])
        precipitation_val = precipitation_score(hospital["weather"][0]["precipitation"])
        er_status_val = er_status_score(hospital["status"])
        traveltime_val = traveltime_score(hospital["hospital"]["travel_time_min"])
        final_score = distance_val + precipitation_val + er_status_val + traveltime_val
        scores.append(final_score)

    return scores