import requests

ER_STATUS_URL = "https://patient.quebec/api/public/v1/urgences"
REQUEST_TIMEOUT_SECONDS = 15


def get_er_data():
    """Return live ER status from patient.quebec (MSSS data).

    Result: {"updated_at": MSSS snapshot time, "stale": bool, "by_id": {id: emergency fields}}.
    The ids are the same 8-digit codes as the `id` column in data/hospitals.csv.
    Raises RuntimeError if the service is unreachable or returns unexpected data.
    """
    try:
        response = requests.get(ER_STATUS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        return {
            "updated_at": data["msssUpdatedAt"],
            # patient.quebec sets this when MSSS has stopped publishing new data.
            "stale": bool(data["stale"]),
            "by_id": {
                hospital["id"]: hospital["emergency"]
                for hospital in data["hospitals"]
            },
        }
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"ER status request failed: {error}") from error
