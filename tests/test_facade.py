import pytest

from app.api import facade


@pytest.fixture(autouse=True)
def no_split_setting(monkeypatch):
    # A local .env or shell may set it; each test sets its own value.
    monkeypatch.delenv(facade.V2_TRAFFIC_ENV_VAR, raising=False)


def set_percent(monkeypatch, value):
    monkeypatch.setenv(facade.V2_TRAFFIC_ENV_VAR, value)


def test_unset_means_all_traffic_to_v1():
    assert facade.v2_traffic_percent() == 0
    assert facade.choose_engine(draw=lambda: 0.0) == ("v1", "split")


@pytest.mark.parametrize(
    ("percent", "draw", "engine"),
    [
        ("0", 0.0, "v1"),  # rollback: even the lowest draw goes to v1
        ("100", 0.999, "v2"),  # full promotion: even the highest draw goes to v2
        ("25", 0.249, "v2"),
        ("25", 0.25, "v1"),
        (" 10 ", 0.05, "v2"),
    ],
)
def test_split_sends_the_configured_share_to_v2(monkeypatch, percent, draw, engine):
    set_percent(monkeypatch, percent)

    assert facade.choose_engine(draw=lambda: draw) == (engine, "split")


@pytest.mark.parametrize("percent", ["0", "100"])
@pytest.mark.parametrize("forced", ["v1", "v2"])
def test_header_overrides_the_split(monkeypatch, percent, forced):
    set_percent(monkeypatch, percent)

    assert facade.choose_engine(forced, draw=lambda: 0.5) == (forced, "header")


@pytest.mark.parametrize("value", ["abc", "-5", "150", "nan"])
def test_invalid_setting_falls_back_to_v1_and_warns(monkeypatch, caplog, value):
    set_percent(monkeypatch, value)

    assert facade.v2_traffic_percent() == 0
    assert facade.choose_engine(draw=lambda: 0.0) == ("v1", "split")
    assert any(facade.V2_TRAFFIC_ENV_VAR in record.getMessage() for record in caplog.records)


def test_split_is_roughly_the_configured_share(monkeypatch):
    set_percent(monkeypatch, "30")
    draws = iter(i / 1000 for i in range(1000))

    engines = [facade.choose_engine(draw=lambda: next(draws))[0] for _ in range(1000)]

    assert engines.count("v2") == 300
