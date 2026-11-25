"""Smoke test for a deployed NavCare API. Uses paid services: one v1 request, plus one v2 with --v2.

    python -m deployment.smoke_test https://<service-url>          health and a v1 answer
    python -m deployment.smoke_test https://<service-url> --v2     also a forced v2 (dev only)

A recommendation passes only with the expected engine and no problems from the correctness
checker (schema and data rules), since a 200 can still be a v1 fallback. For a private service,
set ID_TOKEN (for example from `gcloud auth print-identity-token`); it is never printed.
Set NAVCARE_API_KEY to one of the service's API keys (sent as X-API-Key); never printed either.
Needs requirements-dev.txt.
"""

import argparse
import os
import sys
import time

import requests

from app.quality.correctness import check_response

MONTREAL = {"lat": 45.5017, "lon": -73.5673}
API_KEY_ENV_VAR = "NAVCARE_API_KEY"  # one key, for the X-API-Key header (app/api/auth.py)
TIMEOUT_S = 130  # above the service's 120 s request timeout


def check_health(get):
    """Problems with GET /v1/health; empty means it passed."""
    response = get("/v1/health")
    if response.status_code != 200:
        return [f"status {response.status_code}"]
    if response.json().get("status") != "ok":
        return ["status is not ok"]
    return []


def check_recommendation(get, expected_engine, headers=None):
    """Problems with one recommendation for Montréal; empty means it passed."""
    response = get("/v1/recommendations", params=MONTREAL, headers=headers or {})
    if response.status_code != 200:
        return [f"status {response.status_code}"]
    body = response.json()
    problems = []
    if body.get("engine") != expected_engine:
        problems.append(f"engine {body.get('engine')!r}, expected {expected_engine!r}")
    return problems + check_response(MONTREAL, body)


def make_get(base_url):
    session = requests.Session()
    token = os.environ.get("ID_TOKEN")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if api_key:
        session.headers["X-API-Key"] = api_key

    def get(path, params=None, headers=None):
        return session.get(base_url.rstrip("/") + path, params=params, headers=headers,
                           timeout=TIMEOUT_S)
    return get


def main(argv=None):
    parser = argparse.ArgumentParser(description="Smoke test a deployed NavCare API.")
    parser.add_argument("url")
    parser.add_argument("--v2", action="store_true", help="also force one v2 request (dev)")
    args = parser.parse_args(argv)
    get = make_get(args.url)

    checks = [("health", lambda: check_health(get)),
              ("v1 recommendation", lambda: check_recommendation(get, "v1"))]
    if args.v2:
        checks.append(("v2 recommendation (X-Engine: v2)",
                       lambda: check_recommendation(get, "v2", {"X-Engine": "v2"})))
    failed = False
    for name, check in checks:
        started = time.monotonic()
        problems = check()
        seconds = time.monotonic() - started
        print(f"{'PASS' if not problems else 'FAIL'} {name} ({seconds:.1f} s)")
        for problem in problems:
            print(f"  - {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
