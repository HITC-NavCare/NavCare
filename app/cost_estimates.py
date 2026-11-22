"""Gemini cost calculation from the model_usage the v2 agent records. Foundation only.

- GEMINI_PRICES has no production prices yet, so every real model returns unknown.
- Add a row only after checking it on VERTEX_PRICING_URL, with source and checked_on.
- The result covers model tokens only, not the total request cost (Routes, Cloud Run, credits).
- prompt_tokens includes cached tokens, so input cost at the full rate is an upper bound.
- Missing or invalid usage, or no price for the model, location and date, gives None, never 0.
"""

import math
from datetime import date

VERTEX_PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
TOKENS_PER_PRICE_UNIT = 1_000_000
CURRENCY = "USD"

# Rows: model, location, from, until (dates, None = open), input_per_million,
# output_per_million (output includes thinking tokens), source, checked_on.
GEMINI_PRICES = ()
TOKEN_FIELDS = ("prompt_tokens", "output_tokens")


def gemini_price(model, location, on, prices=GEMINI_PRICES):
    """The price row for a model and Vertex AI location on a date, or None if there is none."""
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
    if not isinstance(on, date):
        raise TypeError("on must be a date")
    if model_usage is None:
        return _unknown("no model usage recorded")
    if not isinstance(model_usage, dict):
        return _unknown("model usage is not an object")

    model = model_usage.get("model")
    if not isinstance(model, str) or not model:
        return _unknown("model missing")
    tokens = {}
    for field in TOKEN_FIELDS:
        value = model_usage.get(field)
        if value is None:
            return _unknown(f"{field} missing")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _unknown(f"{field} is not a whole number of tokens: {value!r}")
        tokens[field] = value

    price = gemini_price(model, location, on, prices)
    if price is None:
        return _unknown(f"no known price for model {model!r} in {location!r} on {on.isoformat()}")

    usd = (tokens["prompt_tokens"] * price["input_per_million"]
           + tokens["output_tokens"] * price["output_per_million"]) / TOKENS_PER_PRICE_UNIT
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


def _unknown(reason):
    return {"estimated_usd": None, "unknown_reason": reason, "price": None}
