from datetime import date

import pytest

from app import cost_estimates as costs
from app.agent_v2 import ai_agent_recommender as agent

DAY = date(2026, 11, 22)
USAGE = {"model": "synthetic-model", "model_calls": 3, "tool_calls": 4,
         "prompt_tokens": 10_000, "output_tokens": 2_000}
ZERO = {**USAGE, "prompt_tokens": 0, "output_tokens": 0}
# Synthetic prices for testing the arithmetic only. Not real Google prices.
SYNTHETIC_PRICES = (
    {"model": "synthetic-model", "location": "global", "from": None, "until": date(2026, 12, 31),
     "input_per_million": 1.0, "output_per_million": 4.0,
     "source": "synthetic test data", "checked_on": DAY},
    {"model": "synthetic-model", "location": "global", "from": date(2027, 1, 1), "until": None,
     "input_per_million": 2.0, "output_per_million": 8.0,
     "source": "synthetic test data", "checked_on": DAY},
)


def estimate(usage=USAGE, location="global", on=DAY):
    return costs.estimate_gemini_cost(usage, location, on, prices=SYNTHETIC_PRICES)


def test_estimate_uses_input_and_output_prices():
    result = estimate()

    assert result["estimated_usd"] == pytest.approx(10_000 * 1.0 / 1e6 + 2_000 * 4.0 / 1e6)
    assert result["unknown_reason"] is None
    assert result["price"] is SYNTHETIC_PRICES[0]


@pytest.mark.parametrize(("on", "expected"), [
    (date(2026, 12, 31), 0.018),  # last day of the first synthetic price
    (date(2027, 1, 1), 0.036),
])
def test_price_follows_the_request_date(on, expected):
    assert estimate(on=on)["estimated_usd"] == pytest.approx(expected)


def test_recorded_zero_tokens_cost_zero_for_a_priced_model():
    assert estimate(ZERO)["estimated_usd"] == 0


REAL = {**USAGE, "model": agent.DEFAULT_MODEL}  # the agent's default model


def real_estimate(on, location=agent.DEFAULT_LOCATION, usage=REAL):
    return costs.estimate_gemini_cost(usage, location, on)


@pytest.mark.parametrize(("on", "expected"), [
    # 10,000 input and 2,000 output tokens, Vertex AI Standard, global
    (date(2026, 11, 22), 10_000 * 0.75 / 1e6 + 2_000 * 3.75 / 1e6),  # first checked day
    (date(2026, 12, 31), 10_000 * 0.75 / 1e6 + 2_000 * 3.75 / 1e6),  # last introductory day
    (date(2027, 1, 1), 10_000 * 1.50 / 1e6 + 2_000 * 7.50 / 1e6),  # standard price starts
    (date(2028, 6, 1), 10_000 * 1.50 / 1e6 + 2_000 * 7.50 / 1e6),
])
def test_verified_gemini_3_8_flash_prices(on, expected):
    result = real_estimate(on)

    assert result["estimated_usd"] == pytest.approx(expected)
    assert result["price"]["source"] == costs.VERTEX_PRICING_URL


def test_dates_before_the_checked_price_are_unknown():
    result = real_estimate(date(2026, 11, 21))

    assert result["estimated_usd"] is None
    assert result["unknown_reason"].startswith("no known price for model")


@pytest.mark.parametrize("location", ["us", "eu", "northamerica-northeast1"])
def test_regional_locations_are_unknown(location):
    assert real_estimate(date(2026, 11, 22), location)["estimated_usd"] is None


def test_other_real_models_are_unknown():
    usage = {**REAL, "model": "gemini-3.7-flash"}

    assert real_estimate(date(2026, 11, 22), usage=usage)["estimated_usd"] is None


def test_production_rows_name_their_source_and_do_not_overlap():
    for price in costs.GEMINI_PRICES:
        assert price["source"] == costs.VERTEX_PRICING_URL
        assert isinstance(price["checked_on"], date)
    rows = sorted(costs.GEMINI_PRICES, key=lambda p: p["from"])
    for earlier, later in zip(rows, rows[1:]):
        if (earlier["model"], earlier["location"]) == (later["model"], later["location"]):
            assert earlier["until"] < later["from"]


@pytest.mark.parametrize(("usage", "reason"), [
    (None, "no model usage recorded"),
    ("3 calls", "model usage is not an object"),
    ({**USAGE, "model": None}, "model missing"),
    ({**USAGE, "model": ""}, "model missing"),
    ({**USAGE, "prompt_tokens": None}, "prompt_tokens missing"),
    ({k: v for k, v in USAGE.items() if k != "output_tokens"}, "output_tokens missing"),
])
def test_missing_usage_is_unknown_not_zero(usage, reason):
    assert estimate(usage) == {"estimated_usd": None, "unknown_reason": reason, "price": None}


@pytest.mark.parametrize("bad", [-1, 1.5, "100", True, float("nan")])
def test_invalid_token_counts_are_unknown(bad):
    result = estimate({**USAGE, "output_tokens": bad})

    assert result["estimated_usd"] is None
    assert "output_tokens is not a whole number" in result["unknown_reason"]


@pytest.mark.parametrize(("usage", "location"), [
    ({**USAGE, "model": "other-model"}, "global"),
    ({**USAGE, "model": "synthetic-model"}, "northamerica-northeast1"),
])
def test_unsupported_pricing_is_unknown(usage, location):
    result = estimate(usage, location)

    assert result["estimated_usd"] is None
    assert result["unknown_reason"].startswith("no known price for model")


@pytest.mark.parametrize(("usage", "reason"), [
    ({**ZERO, "model": "other-model"}, "no known price for model"),
    ({**ZERO, "model": None}, "model missing"),
    ({**ZERO, "output_tokens": -1}, "output_tokens is not a whole number"),
])
def test_zero_tokens_do_not_skip_model_or_usage_checks(usage, reason):
    result = estimate(usage)

    assert result["estimated_usd"] is None
    assert result["unknown_reason"].startswith(reason)


def test_date_is_required():
    with pytest.raises(TypeError):
        estimate(on="2026-11-22")


def test_summary_keeps_unknown_requests_separate():
    estimates = [estimate(), estimate({**USAGE, "output_tokens": 0}), estimate(None)]

    summary = costs.summarize(estimates)

    assert (summary["requests"], summary["known"], summary["unknown"]) == (3, 2, 1)
    assert summary["known_total_usd"] == pytest.approx(0.018 + 0.010)
    assert summary["known_average_usd"] == pytest.approx((0.018 + 0.010) / 2)


def test_summary_with_nothing_known_is_none_not_zero():
    assert costs.summarize([estimate(None)])["known_total_usd"] is None
    assert costs.summarize([estimate(None)])["known_average_usd"] is None
    assert costs.summarize([])["known_total_usd"] is None
