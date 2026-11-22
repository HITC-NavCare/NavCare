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


def test_no_production_prices_so_the_real_model_is_unknown():
    assert costs.GEMINI_PRICES == ()
    usage = {**USAGE, "model": agent.DEFAULT_MODEL}

    result = costs.estimate_gemini_cost(usage, agent.DEFAULT_LOCATION, DAY)

    assert result["estimated_usd"] is None
    assert result["unknown_reason"].startswith("no known price for model")


def test_any_production_row_names_its_source_and_check_date():
    for price in costs.GEMINI_PRICES:
        assert price["source"] and isinstance(price["checked_on"], date)


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
