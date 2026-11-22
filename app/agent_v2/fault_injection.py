"""v2 fault injection for the canary demo. Set only by environment variables; off by default.

- Needs V2_FAULT_ENABLED=true and a V2_FAULT_MODE; either alone does nothing.
- V2_FAULT_MODE=error: v2 fails before calling Gemini; the existing fallback answers (v1_fallback).
- V2_FAULT_MODE=slow: v2 waits V2_FAULT_SLOW_SECONDS (default 35, max 60), using up part of
  the agent's 60 s Gemini budget; if the rest runs out, v2 falls back to v1. The budget is not
  a deadline for the whole request: tool calls and the fallback can run past it.
- Unknown mode means off; invalid wait means the default. Each distinct setting is logged once.
"""

import logging
import math
import os
import threading
import time

from app.agent_v2.ai_agent_recommender import MAX_TOTAL_SECONDS, AgentError

ENABLED_ENV_VAR = "V2_FAULT_ENABLED"
FAULT_MODE_ENV_VAR = "V2_FAULT_MODE"
SLOW_SECONDS_ENV_VAR = "V2_FAULT_SLOW_SECONDS"
FAULT_MODES = ("off", "error", "slow")
DEFAULT_SLOW_SECONDS = 35.0
MAX_SLOW_SECONDS = float(MAX_TOTAL_SECONDS)  # a longer wait would leave the agent no time

logger = logging.getLogger("navcare.fault_injection")
_logged = set()  # settings already logged, so each distinct one is logged once
_logged_lock = threading.Lock()


def faults_enabled():
    """True only when V2_FAULT_ENABLED is "true" (or "1"). Anything else means off."""
    return os.environ.get(ENABLED_ENV_VAR, "").strip().lower() in ("true", "1")


def fault_mode():
    """Return "off", "error" or "slow". Read on every v2 request."""
    value = os.environ.get(FAULT_MODE_ENV_VAR, "").strip().lower()
    if not value or value == "off":
        return "off"
    if value not in FAULT_MODES:
        _log_once("%s=%r is not one of %s; fault injection is off",
                  FAULT_MODE_ENV_VAR, value, "/".join(FAULT_MODES))
        return "off"
    if not faults_enabled():
        _log_once("%s=%s is set but %s is not true; fault injection is off",
                  FAULT_MODE_ENV_VAR, value, ENABLED_ENV_VAR)
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
        _log_once("%s=%r is not a number of seconds above 0 and up to %g; using %g",
                  SLOW_SECONDS_ENV_VAR, value, MAX_SLOW_SECONDS, DEFAULT_SLOW_SECONDS)
        return DEFAULT_SLOW_SECONDS
    return seconds


def apply_fault():
    """Apply the fault to the current v2 request.

    Returns the time.monotonic() at which slow mode's wait began, so the agent's limit can
    start there, or None when there was no wait. The wait blocks one worker thread.
    """
    mode = fault_mode()
    if mode == "error":
        _log_once("fault injection on (%s=error): v2 requests fall back to v1", FAULT_MODE_ENV_VAR)
        raise AgentError(f"injected fault ({FAULT_MODE_ENV_VAR}=error)")
    if mode == "slow":
        seconds = slow_seconds()
        _log_once("fault injection on (%s=slow): v2 requests wait %g s first",
                  FAULT_MODE_ENV_VAR, seconds)
        started = time.monotonic()
        time.sleep(seconds)
        return started
    return None


def _log_once(message, *args):
    key = (message, args)
    with _logged_lock:
        if key in _logged:
            return
        _logged.add(key)
    logger.warning(message, *args)
