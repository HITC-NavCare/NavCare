import pytest
from fastapi.testclient import TestClient

import orchestrator
from app.api import auth, facade
from app.api.main import app

client = TestClient(app)
MONTREAL = {"lat": 45.5017, "lon": -73.5673}
GOOD = "good-key-123"
OTHER = "other-key-456"


@pytest.fixture(autouse=True)
def keys_on(monkeypatch):
    # conftest.py turns keys off for every test; these tests turn them back on.
    monkeypatch.delenv(auth.DISABLED_ENV_VAR, raising=False)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, f"loadtest:{GOOD}, demo:{OTHER}")
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, "0")
    monkeypatch.setattr(orchestrator, "build_recommendations",
                        lambda lat, lon, limit, radius_km: {"data_updated_at": None,
                                                            "recommendations": []})


def get(key=None):
    headers = {"X-API-Key": key} if key is not None else {}
    return client.get("/v1/recommendations", params=MONTREAL, headers=headers)


# --- parsing and matching -------------------------------------------------------------------

def test_load_keys_parses_name_key_pairs_and_skips_bad_entries():
    assert auth.load_keys("a:1, b:2,,:3,c:, d") == {"a": "1", "b": "2"}


def test_key_name_for():
    keys = {"a": "1", "b": "2"}
    assert auth.key_name_for("2", keys) == "b"
    assert auth.key_name_for("3", keys) is None
    assert auth.key_name_for("", keys) is None
    assert auth.key_name_for(None, keys) is None


# --- the API --------------------------------------------------------------------------------

def test_missing_key_is_401_with_the_standard_error_body():
    response = get()
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_wrong_key_is_401():
    assert get("not-a-key").status_code == 401


def test_good_key_is_200_with_rate_limit_headers():
    response = get(GOOD)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == str(auth.DEFAULT_LIMIT_PER_MINUTE)
    assert response.headers["X-RateLimit-Remaining"] == str(auth.DEFAULT_LIMIT_PER_MINUTE - 1)


def test_over_the_limit_is_429_with_retry_after(monkeypatch):
    monkeypatch.setenv(auth.LIMIT_ENV_VAR, "3")
    assert [get(GOOD).status_code for _ in range(3)] == [200, 200, 200]
    response = get(GOOD)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(response.headers["Retry-After"]) <= auth.WINDOW_SECONDS
    assert response.headers["X-RateLimit-Remaining"] == "0"
    # Each key has its own count.
    assert get(OTHER).status_code == 200


def test_no_keys_configured_refuses_everyone(monkeypatch):
    # Fail closed: a missing secret must not open the API.
    monkeypatch.delenv(auth.KEYS_ENV_VAR)
    assert get(GOOD).status_code == 401
    assert get().status_code == 401


def test_keys_disabled_lets_requests_through(monkeypatch):
    monkeypatch.setenv(auth.DISABLED_ENV_VAR, "true")
    assert get().status_code == 200


def test_health_and_docs_need_no_key():
    assert client.get("/v1/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_generated_spec_declares_the_api_key():
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"


def test_recommendation_log_line_names_the_key_not_the_key_itself(caplog):
    caplog.set_level("INFO", logger="navcare.api")
    get(GOOD)
    [record] = [r for r in caplog.records
                if getattr(r, "fields", {}).get("event") == "recommendation"]
    assert record.fields["api_key_name"] == "loadtest"
    assert GOOD not in caplog.text


# --- the rate limiter on its own, with a fake clock -----------------------------------------

def test_limiter_resets_when_the_window_ends():
    now = [100.0]
    limiter = auth.RateLimiter(clock=lambda: now[0])
    assert limiter.allow("a", 2) == (True, 1, 0)
    assert limiter.allow("a", 2) == (True, 0, 0)
    assert limiter.allow("a", 2) == (False, 0, 60)
    now[0] += 59.5
    assert limiter.allow("a", 2) == (False, 0, 1)
    now[0] += 0.5  # 60 s after the window started
    assert limiter.allow("a", 2) == (True, 1, 0)


def test_missing_keys_are_logged_once(monkeypatch, caplog):
    monkeypatch.delenv(auth.KEYS_ENV_VAR)
    monkeypatch.setattr(auth, "_warned_no_keys", False)
    caplog.set_level("WARNING", logger="navcare.api")
    get(GOOD)
    get(GOOD)
    warnings = [r for r in caplog.records if auth.KEYS_ENV_VAR in r.getMessage()]
    assert len(warnings) == 1
