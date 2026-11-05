import requests

url = "https://patient.quebec/api/public/v1/urgences"

def get_er_data():

    response = requests.get(url)

    data = response.json()

    print("Hospitals:", data["count"])

    print(data["hospitals"][0])
    return data 