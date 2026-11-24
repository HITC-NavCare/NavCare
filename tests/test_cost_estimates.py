from datetime import date, datetime, timedelta, timezone

import pytest

from app import cost_estimates as costs
from app.agent_v2 import ai_agent_recommender as agent

DAY = date(2026, 11, 22)
USAGE = {"model": "synthetic-model", "model_calls": 3, "tool_calls": 4,
         "prompt_tokens": 10_000, "output_tokens": 2_000, "calls_without_usage": 0}
ZERO = {**USAGE, "model_calls": 0, "prompt_tokens": 0, "output_tokens": 0}  # no model call
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


def test_no_model_call_costs_zero_for_a_priced_model():
    assert estimate(ZERO)["estimated_usd"] == 0


@pytest.mark.parametrize(("usage", "reason"), [
    # All calls answered without token counts (the old agent recorded this as 0 tokens).
    ({**USAGE, "prompt_tokens": 0, "output_tokens": 0}, "3 model calls but no tokens recorded"),
    # Only some calls had counts: the totals are too low, so no partial estimate either.
    ({**USAGE, "calls_without_usage": 1}, "1 of 3 model calls reported no tokens"),
    # Recorded before calls_without_usage existed: completeness cannot be checked.
    ({k: v for k, v in USAGE.items() if k != "calls_without_usage"}, "calls_without_usage missing"),
    ({k: v for k, v in USAGE.items() if k != "model_calls"}, "model_calls missing"),
])
def test_incomplete_usage_is_unknown_not_a_low_estimate(usage, reason):
    assert estimate(usage) == {"estimated_usd": None, "unknown_reason": reason, "price": None}


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
    assert costs.price_table_problems() == []


def row(model, start, end):
    return {"model": model, "location": "global", "from": start, "until": end}


def test_overlap_hidden_by_another_models_row_is_found():
    # Sorted together by start date, the other model's row sits between the two overlapping rows.
    rows = [row("a", date(2026, 1, 1), date(2026, 12, 31)), row("b", date(2026, 6, 1), None),
            row("a", date(2026, 7, 1), None)]

    assert costs.price_table_problems(rows) == [
        "a in global: the rows from 2026-01-01 and 2026-07-01 overlap"]


@pytest.mark.parametrize("rows", [
    [row("a", None, date(2026, 12, 31)), row("a", date(2027, 1, 1), None)],  # adjacent, open ends
    [row("a", date(2027, 1, 1), None), row("a", None, date(2026, 12, 31))],  # any order
    [row("a", None, None), row("b", None, None)],  # open-ended, different models
    SYNTHETIC_PRICES,
])
def test_valid_tables_have_no_problems(rows):
    assert costs.price_table_problems(rows) == []


@pytest.mark.parametrize(("rows", "problem"), [
    ([row("a", None, None), row("a", date(2027, 1, 1), None)], "overlap"),  # open until
    # two rows that both start open-ended
    ([row("a", None, date(2026, 6, 1)), row("a", None, date(2026, 12, 31))], "overlap"),
    ([row("a", date(2026, 1, 1), date(2026, 6, 1)), row("a", date(2026, 6, 1), None)], "overlap"),
    ([row("a", date(2026, 6, 1), date(2026, 1, 1))], "ends 2026-01-01 before it starts 2026-06-01"),
])
def test_invalid_tables_are_reported(rows, problem):
    (found,) = costs.price_table_problems(rows)
    assert problem in found


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


@pytest.mark.parametrize("field", costs.COUNT_FIELDS)
@pytest.mark.parametrize("bad", [-1, 1.5, "100", True, float("nan")])
def test_invalid_counts_are_unknown(field, bad):
    result = estimate({**USAGE, field: bad})

    assert result["estimated_usd"] is None
    assert f"{field} is not a whole number" in result["unknown_reason"]


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


@pytest.mark.parametrize(("on", "expected"), [
    (datetime(2026, 12, 31, 23, 30), 0.018),  # naive: its own date
    (datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc), 0.018),
    # 23:30 on Dec 31 in Montréal is already Jan 1 in UTC; the timestamp's own date is used.
    (datetime(2026, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5))), 0.018),
    (datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc), 0.036),
])
def test_a_datetime_counts_as_its_own_calendar_date(on, expected):
    assert estimate(on=on)["estimated_usd"] == pytest.approx(expected)
    assert costs.gemini_price("synthetic-model", "global", on, SYNTHETIC_PRICES) is not None


@pytest.mark.parametrize("bad", ["2026-11-22", None, 20261122])
def test_anything_else_as_the_date_is_a_type_error_in_both_functions(bad):
    with pytest.raises(TypeError):
        estimate(on=bad)
    with pytest.raises(TypeError):
        costs.gemini_price("synthetic-model", "global", bad, SYNTHETIC_PRICES)


def test_summary_keeps_unknown_requests_separate():
    estimates = [estimate(), estimate({**USAGE, "output_tokens": 0}), estimate(None)]

    summary = costs.summarize(estimates)

    assert (summary["requests"], summary["known"], summary["unknown"]) == (3, 2, 1)
    assert summary["known_total_usd"] == pytest.approx(0.018 + 0.010)
    assert summary["known_average_usd"] == pytest.approx((0.018 + 0.010) / 2)


def test_summary_counts_incomplete_usage_as_unknown_not_as_zero():
    estimates = [estimate(),
                 estimate({**USAGE, "prompt_tokens": 0, "output_tokens": 0}),  # calls, no tokens
                 estimate({**USAGE, "calls_without_usage": 2}),
                 estimate(ZERO)]  # no model call: a real zero

    summary = costs.summarize(estimates)

    assert (summary["requests"], summary["known"], summary["unknown"]) == (4, 2, 2)
    assert summary["known_total_usd"] == pytest.approx(0.018)
    assert summary["known_average_usd"] == pytest.approx(0.009)


def test_summary_with_nothing_known_is_none_not_zero():
    assert costs.summarize([estimate(None)])["known_total_usd"] is None
    assert costs.summarize([estimate(None)])["known_average_usd"] is None
    assert costs.summarize([])["known_total_usd"] is None
