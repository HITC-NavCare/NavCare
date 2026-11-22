import pytest

from app.agent_v2 import fault_injection


@pytest.fixture(autouse=True)
def no_injected_faults(monkeypatch):
    # The API loads .env on import; don't let a local fault setting leak into tests.
    monkeypatch.delenv(fault_injection.FAULT_MODE_ENV_VAR, raising=False)
    monkeypatch.delenv(fault_injection.SLOW_SECONDS_ENV_VAR, raising=False)
