import pytest

from app.agent_v2 import fault_injection


@pytest.fixture(autouse=True)
def no_injected_faults(monkeypatch):
    # The API loads .env on import; don't let a local fault setting leak into tests.
    for name in (fault_injection.ENABLED_ENV_VAR, fault_injection.FAULT_MODE_ENV_VAR,
                 fault_injection.SLOW_SECONDS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fault_injection, "_logged", set())  # each test sees its own log-once


@pytest.fixture(autouse=True)
def api_keys_off_by_default(monkeypatch):
    # Most tests are about recommendations, not keys: turn keys off unless a test turns them on
    # (tests/test_auth.py), and start every test with an empty rate-limit count.
    from app.api import auth

    monkeypatch.setenv(auth.DISABLED_ENV_VAR, "true")
    monkeypatch.delenv(auth.KEYS_ENV_VAR, raising=False)
    monkeypatch.delenv(auth.LIMIT_ENV_VAR, raising=False)
    auth.limiter.reset()
