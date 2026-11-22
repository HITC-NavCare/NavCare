"""Fault injection for v2, so the canary demo can show a rollback on purpose. Off by default.

Only the environment turns it on (V2_FAULT_MODE), never a request header or parameter, so no
caller can make v2 fail. It runs at the start of the v2 path in orchestrator.py, so it only
affects requests the facade sent to v2; v1 requests never read it.

- off (unset or empty): nothing changes.
- error: v2 raises an AgentError before calling Gemini, so the existing fallback answers with
  the v1 ranking, engine "v1_fallback" and the reason in the logs. No model calls are made.
- slow: v2 waits V2_FAULT_SLOW_SECONDS, then runs normally. The wait comes before the agent
  starts, so the agent still has its full time budget and the latency is the wait plus the
  usual v2 time.

Invalid values fail safe and are logged as warnings: an unknown mode means off, and a wait
that is not a number of seconds from more than 0 up to MAX_SLOW_SECONDS uses the default.
"""

import logging
import math
import os
import time

from app.agent_v2.ai_agent_recommender import AgentError

FAULT_MODE_ENV_VAR = "V2_FAULT_MODE"
SLOW_SECONDS_ENV_VAR = "V2_FAULT_SLOW_SECONDS"
FAULT_MODES = ("off", "error", "slow")
DEFAULT_SLOW_SECONDS = 35.0
# Each delayed request holds one of the server's worker threads while it waits, and a wait
# longer than the Cloud Run request timeout would show up as a timeout, not a slow answer.
MAX_SLOW_SECONDS = 120.0

logger = logging.getLogger("navcare.fault_injection")


def fault_mode():
    """The fault mode from the environment: "off", "error" or "slow".

    Read on every v2 request, like the facade's settings. Case and spaces are ignored.
    Unset or empty means off; any other unknown value also means off (fail safe), with a warning.
    """
    value = os.environ.get(FAULT_MODE_ENV_VAR, "").strip().lower()
    if not value:
        return "off"
    if value not in FAULT_MODES:
        logger.warning("%s=%r is not one of %s; fault injection is off",
                       FAULT_MODE_ENV_VAR, value, "/".join(FAULT_MODES))
        return "off"
    return value


def slow_seconds():
    """How long the slow mode waits. Unset means DEFAULT_SLOW_SECONDS.

    A value that is not a number above 0 and up to MAX_SLOW_SECONDS also means the default,
    with a warning.
    """
    value = os.environ.get(SLOW_SECONDS_ENV_VAR, "").strip()
    if not value:
        return DEFAULT_SLOW_SECONDS
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    # math.isfinite also rejects "nan", which compares false with every bound.
    if seconds is None or not math.isfinite(seconds) or not 0 < seconds <= MAX_SLOW_SECONDS:
        logger.warning("%s=%r is not a number of seconds above 0 and up to %g; using %g",
                       SLOW_SECONDS_ENV_VAR, value, MAX_SLOW_SECONDS, DEFAULT_SLOW_SECONDS)
        return DEFAULT_SLOW_SECONDS
    return seconds


def apply_fault():
    """Apply the configured fault to the current v2 request. Does nothing when off.

    Raises AgentError in error mode. In slow mode it blocks the calling thread: the API's
    recommendations endpoint is a plain (sync) function, which FastAPI runs in a shared worker
    thread pool, so the wait holds one of those threads, not the event loop. Other requests are
    served while threads are free; enough waiting requests at once can use up the pool.
    """
    mode = fault_mode()
    if mode == "error":
        raise AgentError(f"injected fault ({FAULT_MODE_ENV_VAR}=error)")
    if mode == "slow":
        seconds = slow_seconds()
        logger.warning("injected fault (%s=slow): v2 waits %g s before starting",
                       FAULT_MODE_ENV_VAR, seconds)
        time.sleep(seconds)
