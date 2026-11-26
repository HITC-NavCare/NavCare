# Data model (Firestore, `(default)` database)

## `request_log`

One document per `/v1/recommendations` request (statuses 200, 503 and 500), written by
`app/storage/request_log.py` after the response is sent. Used to measure correctness and
cost per request for v1 and v2.

Document id: `request_id` (same as the `X-Request-ID` response header).

| Field | Type | Notes |
|---|---|---|
| `request_id` | string | UUID |
| `timestamp` | timestamp | When the request started (UTC) |
| `status` | int | 200, 503 or 500 |
| `engine` | string | `v1`, `v2` or `v1_fallback` |
| `chosen_by` | string | `split` or `header` |
| `latency_ms` | int | Measured before the write |
| `model_usage` | map or null | Gemini token usage; null for v1 |
| `model_cost_usd` | double | v2 and v1_fallback, when the estimate is known |
| `model_cost_unknown` | string | Instead of `model_cost_usd`, with the reason |
| `lat`, `lon` | double | Rounded to 2 decimals (about 1 km) |
| `limit` | int | Requested number of recommendations |
| `radius_km` | double | Requested search radius |
| `has_message` | bool | Whether a non-blank message was sent. The text is never stored. |
| `hospital_ids` | array of string | Recommended MSSS ids, in rank order; empty for 503 and 500 |
| `call_911` | bool or null | null means nobody read the message (v1 or fallback) |

## `hospitals`

To do (ER snapshots issue).

## `er_snapshots`

To do (ER snapshots issue).