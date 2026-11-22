"""Estimated Gemini model-token cost of one request, from the model_usage the v2 agent records.

- Model tokens only, not the total request cost (Routes, Cloud Run, credits are left out).
- Cached-token counts are not recorded, so all input is priced at the full rate. This may
  overestimate input cost.
- Priced: gemini-3.8-flash, Vertex AI Standard PayGo, global endpoint, verified on
  VERTEX_PRICING_URL. Other models, locations and dates give unknown.
- Missing or invalid usage, or no price for the model, location and date, gives None, never 0.
"""

import math
from datetime import date

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
