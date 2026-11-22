"""v2 fault injection for the canary demo. Set only by environment variables; off by default.

- V2_FAULT_MODE=error: v2 fails before calling Gemini; the existing fallback answers (v1_fallback).
- V2_FAULT_MODE=slow: v2 waits V2_FAULT_SLOW_SECONDS (default 35, max 120), then runs normally.
- Unknown mode means off; invalid wait means the default. Both log a warning.
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
MAX_SLOW_SECONDS = 120.0  # each wait holds a worker thread; keep it under the request timeout

logger = logging.getLogger("navcare.fault_injection")


def fault_mode():
    """Return "off", "error" or "slow". Read on every v2 request."""
    value = os.environ.get(FAULT_MODE_ENV_VAR, "").strip().lower()
    if not value:
        return "off"
    if value not in FAULT_MODES:
        logger.warning("%s=%r is not one of %s; fault injection is off",
                       FAULT_MODE_ENV_VAR, value, "/".join(FAULT_MODES))
        return "off"
    return value


def slow_seconds():
    """Return the slow-mode wait: a number above 0 and up to MAX_SLOW_SECONDS, else the default."""
    value = os.environ.get(SLOW_SECONDS_ENV_VAR, "").strip()
    if not value:
        return DEFAULT_SLOW_SECONDS
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is None or not math.isfinite(seconds) or not 0 < seconds <= MAX_SLOW_SECONDS:
        logger.warning("%s=%r is not a number of seconds above 0 and up to %g; using %g",
                       SLOW_SECONDS_ENV_VAR, value, MAX_SLOW_SECONDS, DEFAULT_SLOW_SECONDS)
        return DEFAULT_SLOW_SECONDS
    return seconds


def apply_fault():
    """Apply the fault to the current v2 request. The slow wait blocks one worker thread."""
    mode = fault_mode()
    if mode == "error":
        raise AgentError(f"injected fault ({FAULT_MODE_ENV_VAR}=error)")
    if mode == "slow":
        seconds = slow_seconds()
        logger.warning("injected fault (%s=slow): v2 waits %g s before starting",
                       FAULT_MODE_ENV_VAR, seconds)
        time.sleep(seconds)
