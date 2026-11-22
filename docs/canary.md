# Canary release criteria (v1 → v2)

These rules were written **before** the first canary step. The date of the commit that adds this file proves it. Every promote or rollback decision follows this page. Changing a threshold during the canary needs a new commit and a note explaining why.

## What is being released

`GET /v1/recommendations` has two engines behind the same API:

- **v1**: the rules engine
- **v2**: the Gemini agent

The facade (`app/api/facade.py`) picks an engine for each request at random. `V2_TRAFFIC_PERCENT` is the share that goes to v2. **Rollback = set `V2_TRAFFIC_PERCENT=0`** on the Cloud Run service. That deploys a new revision in about a minute, and all traffic goes back to v1. `ENGINE_HEADER_ENABLED` stays off in the canary service, so the split alone decides.

## Where the numbers come from

| Source | Gives |
| --- | --- |
| Load test results, `results.jsonl` (Issue #25) | status and client-side latency for every request, the full response, and its `engine` |
| `python -m app.quality.correctness results.jsonl --min-percent 95` (Issue #24) | % correct per engine, which includes the data-consistency rules |
| One JSON log line per request in Cloud Logging (`jsonPayload.engine`, `status`, `latency_ms`, `model_usage`) and the dashboard (Issue #27) | error rate, p95 latency and tokens per engine, server side |
| Billing report for project NavCare (Issues #16, #26) | actual spend during the step |

**How requests are counted.** A request was *sent to v2* when the facade chose v2. Its response then has `engine` `v2` or `v1_fallback`. A 5xx or a timeout on such a request also counts as sent to v2. Requests with `engine: "v1"` are the control group. They get the same load at the same time, so the two engines are compared under identical conditions.

## Criteria (checked at the end of every step)

| # | Metric | How it is measured | Threshold | Action when violated |
| --- | --- | --- | --- | --- |
| 1 | **v2 failure rate** (availability) | (v2 requests with a 5xx, a timeout, or `engine: "v1_fallback"`) ÷ requests sent to v2. A fallback is a v2 failure, even though the person still got an answer. | ≤ 5 % | Rollback |
| 2 | **User-facing error rate** | Requests with a 5xx or timeout ÷ all requests, both engines together. This is the separate user-outcome measure: fallbacks are not errors for the person. | ≤ 1 %, and not higher than in the 0 % baseline step | Rollback |
| 3 | **v2 p95 latency** | 95th percentile of `latency_ms` for v2 responses (`engine: "v2"`). The client-side latency from the load test is reported next to it. | ≤ 30 s (typical run is about 16 s; the agent's hard cap is 60 s) | Rollback |
| 4 | **v1 p95 latency** (control) | Same, for `engine: "v1"` | ≤ 5 s | Hold the step and investigate: if v1 is slow too, the problem is the shared data services, not v2 |
| 5 | **Correctness and data consistency** | `app/quality/correctness.py` on the step's `results.jsonl`. It checks the schema, real hospital ids, numbers that add up, radius, the specialised-ER rules, `call_911` on emergency phrases, and overlap with v1's top 4 for requests without a message. | v2 ≥ 95 % correct, and v1 = 100 % | v2 below 95 %: rollback. v1 below 100 %: hold (it is a checker or data problem, not a v2 one) |
| 6 | **v2 model cost per request** | Average of `model_usage.prompt_tokens` and `output_tokens` × the Gemini price per token, over v2 requests | ≤ $0.03 (measured: $0.01 to $0.03) | Rollback |
| 7 | **Total spend** | Billing report for the project since the canary started, and the budget alerts | Under $40 (the 80 % budget alert). Any budget alert email = violated | Rollback and **stop** the canary |

A step is **promoted only if every criterion passes** and the minimum number of v2 requests was reached.

## Steps

| Step | `V2_TRAFFIC_PERCENT` | Load | Expected v2 requests |
| --- | --- | --- | --- |
| 0 (baseline) | 0 | 12 requests/min for 10 min = 120 requests | 0 |
| 1 | 10 | same | ~12 |
| 2 | 25 | same | ~30 |
| 3 | 50 | same | ~60 |
| 4 | 100 | same | 120 |

- **Same load at every step.** The load test (#25) replays the same fixed list of scenarios at the same rate. The scenarios are locations across Québec, with and without a message, in French and English. It also has a hard cap of 120 requests per step.
- **Minimum v2 requests: 12 per step.** If a step ends with fewer v2 requests than that, which can happen because the split is random, we **hold**. We run the same step for another 10 minutes and evaluate both runs together. We never promote on too little data.
- **Between steps**, we check the criteria, write the results in the canary log below, and only then change `V2_TRAFFIC_PERCENT`.
- **For the demo**, one promotion and one rollback are enough. The rollback is shown with fault injection (#28), or by setting the split back to 0.

## Cost of one run (estimate, check before running)

Google Routes dominates the cost, not Gemini. Each recommendation sends one live-traffic route matrix with up to 15 billed elements. v2 may search twice, so up to 30. Pro rate: about $10 per 1,000 elements, after 5,000 free elements per month.

| | Elements | Gemini |
| --- | --- | --- |
| Steps 0 to 4, 600 requests in total (about 220 to v2) | about 9,500 (v1 × 15, v2 × ~20) | about 220 × $0.02 ≈ $4.50 |
| Cost | about $45 if this month's free 5,000 are already used, about $0 to $5 more if not | |

**That is too close to the $50 budget.** Before running, do one of these:

1. Add a short drive-time cache, keyed on the origin rounded to about 1 km, with a 10-minute lifetime. The load test reuses a few fixed locations, so this drops the Routes cost to a few dollars.
2. Halve the load: 6 requests/min, with 6 v2 requests as the minimum.

Re-check the billing report after each step.

## Canary log

| Date and time | Step | v2 requests | v2 failure % | User error % | v2 p95 | v1 p95 | v2 correct % | v1 correct % | v2 cost/request | Spend so far | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | | | | |
