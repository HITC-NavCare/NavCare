import pytest

from app.agent_v2 import fault_injection


@pytest.fixture(autouse=True)
def no_injected_faults(monkeypatch):
    # The API loads .env on import; don't let a local fault setting leak into tests.
    for name in (fault_injection.ENABLED_ENV_VAR, fault_injection.FAULT_MODE_ENV_VAR,
                 fault_injection.SLOW_SECONDS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fault_injection, "_logged", set())  # each test sees its own log-once
