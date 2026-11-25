"""API keys and the per-key rate limit for the paid endpoint (/v1/recommendations).

Keys come from the NAVCARE_API_KEYS environment variable, which Cloud Run fills from the Secret
Manager secret of the same name. Its value is comma-separated name:key pairs:

    loadtest:3f9c...,demo:a71b...,prof:0d4e...

The name says who called (it goes in the logs); the key is the secret part and is never logged.

Fail closed: when NAVCARE_API_KEYS is missing or empty, every request is refused (401), unless
API_KEYS_DISABLED=true. Only local development and the tests set that; a forgotten secret on
Cloud Run must never mean "open to everyone".

The rate limit counts in memory, per key, in fixed one-minute windows. Each Cloud Run instance
counts on its own, so with N instances a key can make up to N times the limit.
"""

import hmac
import math
import os
import threading
import time

KEYS_ENV_VAR = "NAVCARE_API_KEYS"
DISABLED_ENV_VAR = "API_KEYS_DISABLED"
LIMIT_ENV_VAR = "API_RATE_LIMIT_PER_MINUTE"
DEFAULT_LIMIT_PER_MINUTE = 30
WINDOW_SECONDS = 60
# Shown as the key name when keys are disabled (local development and tests).
NO_KEY_NAME = "keys-disabled"


class ApiKeyError(Exception):
    """A request refused for its key (401) or its rate (429). main.py turns it into the
    standard error body."""

    def __init__(self, status_code, code, message, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def keys_disabled():
    return os.environ.get(DISABLED_ENV_VAR, "").strip().lower() in ("true", "1")


def load_keys(value=None):
    """Parse "name:key,name:key" into {name: key}. Entries without a name or a key are skipped.
    Read on every request, so a test (or a new revision) can change it."""
    if value is None:
        value = os.environ.get(KEYS_ENV_VAR, "")
    keys = {}
    for entry in value.split(","):
        name, _, key = entry.strip().partition(":")
        if name.strip() and key.strip():
            keys[name.strip()] = key.strip()
    return keys


def key_name_for(given_key, keys):
    """The name of the key that matches, or None.

    hmac.compare_digest takes the same time whether the first or the last character is wrong,
    so an attacker can't guess a key one character at a time by timing our answers. We compare
    against every key (no early return), for the same reason.
    """
    if not given_key:
        return None
    found = None
    for name, key in keys.items():
        if hmac.compare_digest(given_key.encode(), key.encode()):
            found = name
    return found


def limit_per_minute():
    try:
        limit = int(os.environ.get(LIMIT_ENV_VAR, DEFAULT_LIMIT_PER_MINUTE))
    except ValueError:
        return DEFAULT_LIMIT_PER_MINUTE
    return limit if limit > 0 else DEFAULT_LIMIT_PER_MINUTE


class RateLimiter:
    """Fixed-window counter: each key gets `limit` requests per window; the count resets when
    a new window starts. clock: for tests."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()  # FastAPI runs sync code on several threads at once
        self._windows = {}  # key name -> (window start, requests counted in it)

    def allow(self, name, limit):
        """Return (allowed, remaining, retry_after_seconds) and count the request if allowed."""
        now = self._clock()
        with self._lock:
            start, count = self._windows.get(name, (now, 0))
            if now - start >= WINDOW_SECONDS:  # the window is over: start a new one
                start, count = now, 0
            if count >= limit:
                retry_after = max(1, math.ceil(start + WINDOW_SECONDS - now))
                self._windows[name] = (start, count)
                return False, 0, retry_after
            count += 1
            self._windows[name] = (start, count)
            return True, limit - count, 0

    def reset(self):
        with self._lock:
            self._windows.clear()


limiter = RateLimiter()


def check_request(given_key):
    """Decide one request. Returns (key name, headers to add); raises ApiKeyError to refuse it."""
    if keys_disabled():
        return NO_KEY_NAME, {}
    name = key_name_for(given_key, load_keys())
    if name is None:
        raise ApiKeyError(401, "unauthorized", "Missing or invalid X-API-Key header.")
    limit = limit_per_minute()
    allowed, remaining, retry_after = limiter.allow(name, limit)
    headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining)}
    if not allowed:
        raise ApiKeyError(429, "rate_limited", f"Limit of {limit} requests per minute exceeded.",
                          {**headers, "Retry-After": str(retry_after)})
    return name, headers
