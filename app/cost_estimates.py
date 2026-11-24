"""Estimated Gemini model-token cost of one request, from the model_usage the v2 agent records.

- Model tokens only, not the total request cost (Routes, Cloud Run, credits are left out).
- Cached-token counts are not recorded, so all input is priced at the full rate. This may
  overestimate input cost.
- Priced: gemini-3.8-flash, Vertex AI Standard PayGo, global endpoint, verified on
  VERTEX_PRICING_URL. Other models, locations and dates give unknown.
- Missing, invalid or incomplete usage, or no price for the model, location and date, gives
  None, never 0. A recorded zero (no model call) gives 0.
- `on` is a date; a datetime counts as its own calendar date (in its own timezone, if any).
"""

import math
from collections import defaultdict
from datetime import date, datetime

VERTEX_PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
TOKENS_PER_PRICE_UNIT = 1_000_000
CURRENCY = "USD"

# Rows: model, location, from, until (dates, None = open), input_per_million,
# output_per_million (output includes thinking tokens), source, checked_on.
# Standard PayGo, global. The page gives no start date for the introductory price, so it starts
# on the day it was checked; earlier dates are unknown.
PRICES_CHECKED_ON = date(2026, 11, 22)
GEMINI_PRICES = (
    {"model": "gemini-3.8-flash", "location": "global",
     "from": PRICES_CHECKED_ON, "until": date(2026, 12, 31),
     "input_per_million": 0.75, "output_per_million": 3.75,
     "source": VERTEX_PRICING_URL, "checked_on": PRICES_CHECKED_ON},
    {"model": "gemini-3.8-flash", "location": "global",
     "from": date(2027, 1, 1), "until": None,
     "input_per_million": 1.50, "output_per_million": 7.50,
     "source": VERTEX_PRICING_URL, "checked_on": PRICES_CHECKED_ON},
)
# Whole numbers in model_usage. calls_without_usage counts answers that reported no tokens.
COUNT_FIELDS = ("model_calls", "calls_without_usage", "prompt_tokens", "output_tokens")


def gemini_price(model, location, on, prices=GEMINI_PRICES):
    """The price row for a model and Vertex AI location on a date, or None if there is none."""
    on = _as_date(on)
    for price in prices:
        if (price["model"], price["location"]) != (model, location):
            continue
        if (price["from"] is None or price["from"] <= on) and (
            price["until"] is None or on <= price["until"]
        ):
            return price
    return None


def estimate_gemini_cost(model_usage, location, on, prices=GEMINI_PRICES):
    """Estimate one request's Gemini token cost from its model_usage.

    location is the Vertex AI location used (GEMINI_LOCATION; model_usage does not record it).
    on is the date of the request, since prices change over time.
    Returns {"estimated_usd": float or None, "unknown_reason": str or None, "price": row or None}.
    """
    on = _as_date(on)
    if model_usage is None:
        return _unknown("no model usage recorded")
    if not isinstance(model_usage, dict):
        return _unknown("model usage is not an object")

    model = model_usage.get("model")
    if not isinstance(model, str) or not model:
        return _unknown("model missing")
    counts = {}
    for field in COUNT_FIELDS:
        value = model_usage.get(field)
        if value is None:
            return _unknown(f"{field} missing")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _unknown(f"{field} is not a whole number: {value!r}")
        counts[field] = value
    if counts["calls_without_usage"]:
        return _unknown(f"{counts['calls_without_usage']} of {counts['model_calls']} model calls "
                        "reported no tokens")
    if counts["model_calls"] and not (counts["prompt_tokens"] or counts["output_tokens"]):
        return _unknown(f"{counts['model_calls']} model calls but no tokens recorded")

    price = gemini_price(model, location, on, prices)
    if price is None:
        return _unknown(f"no known price for model {model!r} in {location!r} on {on.isoformat()}")

    usd = (counts["prompt_tokens"] * price["input_per_million"]
           + counts["output_tokens"] * price["output_per_million"]) / TOKENS_PER_PRICE_UNIT
    return {"estimated_usd": usd, "unknown_reason": None, "price": price}


def summarize(estimates):
    """Total and average over the known estimates only, with the count of unknown ones.

    Both are None when no estimate is known. A non-zero unknown count means the total is partial.
    """
    known = [e["estimated_usd"] for e in estimates if e["estimated_usd"] is not None]
    total = math.fsum(known) if known else None
    return {
        "requests": len(estimates),
        "known": len(known),
        "unknown": len(estimates) - len(known),
        "known_total_usd": total,
        "known_average_usd": total / len(known) if known else None,
    }


def price_table_problems(prices=GEMINI_PRICES):
    """Rows that end before they start, and overlapping rows for the same model and location.

    from/until None means open-ended (from the start, or for good). An empty list means valid.
    """
    problems = []
    groups = defaultdict(list)
    for row in prices:
        if row["from"] and row["until"] and row["until"] < row["from"]:
            problems.append(f"{row['model']} in {row['location']}: ends {row['until']} "
                            f"before it starts {row['from']}")
        groups[(row["model"], row["location"])].append(row)
    for (model, location), rows in groups.items():
        rows.sort(key=lambda row: row["from"] or date.min)
        for earlier, later in zip(rows, rows[1:]):
            open_ended = earlier["until"] is None or later["from"] is None
            if open_ended or earlier["until"] >= later["from"]:
                problems.append(f"{model} in {location}: the rows from {earlier['from']} and "
                                f"{later['from']} overlap")
    return problems


def _as_date(on):
    if isinstance(on, datetime):  # also a date, but cannot be compared with one
        return on.date()
    if isinstance(on, date):
        return on
    raise TypeError(f"on must be a date or datetime, not {type(on).__name__}")


def _unknown(reason):
    return {"estimated_usd": None, "unknown_reason": reason, "price": None}
