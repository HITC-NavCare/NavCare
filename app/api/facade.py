"""Strangler-pattern facade: decides whether v1 (rules) or v2 (AI agent) serves a request.

The canary is controlled by one setting, V2_TRAFFIC_PERCENT (0 to 100). Setting it to 0 sends
all traffic back to v1, which is the rollback. A client can force one engine with the
X-Engine header, for tests and the demo.
"""

import logging
import os
import random

V2_TRAFFIC_ENV_VAR = "V2_TRAFFIC_PERCENT"
ENGINES = ("v1", "v2")

logger = logging.getLogger("navcare.facade")


def v2_traffic_percent():
    """The share of requests v2 should serve, from the environment.

    Read on every request, so tests and local runs can change it. Unset means 0. A value that
    is not a number between 0 and 100 also means 0: when in doubt, v1 serves (fail safe).
    """
    value = os.environ.get(V2_TRAFFIC_ENV_VAR, "").strip()
    if not value:
        return 0.0
    try:
        percent = float(value)
    except ValueError:
        percent = None
    if percent is None or not 0 <= percent <= 100:
        logger.warning("%s=%r is not a number from 0 to 100; using 0 (all v1)",
                       V2_TRAFFIC_ENV_VAR, value)
        return 0.0
    return percent


def choose_engine(forced_engine=None, draw=random.random):
    """Return (engine, how) for one request: engine is "v1" or "v2", how is "header" or "split".

    draw returns a number in [0, 1); tests pass a fixed one.
    """
    if forced_engine in ENGINES:
        return forced_engine, "header"
    return ("v2" if draw() * 100 < v2_traffic_percent() else "v1"), "split"
