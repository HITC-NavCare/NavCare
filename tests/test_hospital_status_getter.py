import pytest
import requests

from app.services import hospital_status_getter as status_getter
from tests.factories import FakeResponse, make_status

SAMPLE = {
    "schemaVersion": "1.0",
    "msssUpdatedAt": "2026-11-10T20:45:00.000Z",
    "stale": False,
    "count": 2,
    "hospitals": [
        {"id": "51236313", "name": "HÔPITAL NOTRE-DAME", "emergency": make_status(131)},
        {"id": "12475976", "name": "HÔPITAL DU SACRÉ-COEUR", "emergency": make_status(None)},
    ],
}


def patch_get(monkeypatch, response, calls=None):
    def get(url, timeout):
        if calls is not None:
            calls.append({"url": url, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(status_getter.requests, "get", get)


def test_returns_status_keyed_by_id(monkeypatch):
    calls = []
    patch_get(monkeypatch, FakeResponse(SAMPLE), calls)

    result = status_getter.get_er_data()

    assert result["updated_at"] == "2026-11-10T20:45:00.000Z"
    assert result["stale"] is False
    assert set(result["by_id"]) == {"51236313", "12475976"}
    assert result["by_id"]["51236313"]["occupancyPct"] == 131
    assert calls == [{
        "url": status_getter.ER_STATUS_URL,
        "timeout": status_getter.REQUEST_TIMEOUT_SECONDS,
    }]


def test_stale_flag_is_passed_on(monkeypatch):
    patch_get(monkeypatch, FakeResponse({**SAMPLE, "stale": True}))

    assert status_getter.get_er_data()["stale"] is True


@pytest.mark.parametrize(
    "response",
    [
        requests.Timeout("timed out"),
        requests.ConnectionError("no network"),
        FakeResponse(status_code=503),
        FakeResponse(json_error=ValueError("not JSON")),
        FakeResponse({"stale": False, "hospitals": []}),  # missing msssUpdatedAt
        FakeResponse({**SAMPLE, "hospitals": None}),
        FakeResponse({**SAMPLE, "hospitals": [{"name": "no id"}]}),
    ],
    ids=["timeout", "connection", "http_503", "bad_json", "missing_key", "null_list", "no_id"],
)
def test_failures_raise_clear_error(monkeypatch, response):
    patch_get(monkeypatch, response)

    with pytest.raises(RuntimeError, match="ER status request failed"):
        status_getter.get_er_data()
