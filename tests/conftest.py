import pytest

from app.agent_v2 import fault_injection
from app.storage import request_log


@pytest.fixture(autouse=True)
def no_injected_faults(monkeypatch):
    # The API loads .env on import; don't let a local fault setting leak into tests.
    for name in (fault_injection.ENABLED_ENV_VAR, fault_injection.FAULT_MODE_ENV_VAR,
                 fault_injection.SLOW_SECONDS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fault_injection, "_logged", set())  # each test sees its own log-once

# for tests in test_request_log.py: a function that raises the given error when called
class FakeFirestore:
    """Stands in for google.cloud.firestore.Client: stores documents in a dict, offline."""

    def __init__(self, fail: bool = False):
        self.docs: dict[str, dict] = {}
        self.fail = fail
        self._doc_id = None

    def collection(self, name):
        assert name == "request_log"
        return self

    def document(self, doc_id):
        self._doc_id = doc_id
        return self

    def set(self, data, timeout=None):
        if self.fail:
            raise RuntimeError("firestore down")
        self.docs[self._doc_id] = data


@pytest.fixture(autouse=True)
def fake_firestore(monkeypatch):
    """Every test gets a fake Firestore, so no test can reach the real one."""
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "true")
    fake = FakeFirestore()
    request_log.set_client(fake)
    return fake