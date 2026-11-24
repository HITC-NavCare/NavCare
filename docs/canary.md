# Canary release criteria (v1 → v2)

These rules were agreed before the first canary step. The date this file's pull request was merged into main is the proof (commit dates can be edited, merge dates can't). Every promote or rollback decision follows this page. Changing a threshold during the canary needs a new pull request that explains why.

## What is being released

`GET /v1/recommendations` has two engines behind the same API:

- **v1**: the rules engine
- **v2**: the Gemini agent

The facade (`app/api/facade.py`) picks an engine for each request at random. `V2_TRAFFIC_PERCENT` is the share that goes to v2. **Rollback = set `V2_TRAFFIC_PERCENT=0`** on the Cloud Run service. That deploys a new revision in about a minute, and all traffic goes back to v1. `ENGINE_HEADER_ENABLED` stays off in the canary service, so the split alone decides and callers can't force an engine.

## Before the canary (required)

1. **Drive-time cache is merged and deployed.** Without it, one run costs more than the spend limit (see "Cost of one run").
2. **v1 comparison run passes.** The check that v2 picks hospitals similar to v1's needs both answers for the same request. That only works with `X-Engine`, so it runs once **before** the canary, on a dev revision with `ENGINE_HEADER_ENABLED=true`, never in the canary itself:
   ```
   python -m loadtest.run --url <dev-service-url> --engine v2 --rate 12 --duration 125 --max-requests 25 --min-percent 96 --yes
   ```
   This sends the 25 scenarios once each, to v2. For the 5 with no message, it also sends the same request to v1 and compares them. **Pass: at most 1 problem in total from the correctness checker** (1 out of 25 = 96 %, so `--min-percent 96` makes the command's PASS/FAIL and exit code match this rule). `--duration 125` is needed: the script sends `min(--max-requests, rate × duration / 60)` requests, so the default 60 s would send only 12.
3. The Cloud Run request timeout is at least 120 s, longer than the agent's 60 s cap plus the v1 fallback. The load test's client timeout is 130 s, longer than that, so a request the service gives up on shows as its 504 (a server error in criterion 2), not as a client timeout.

## Where the numbers come from

| Source | Gives |
| --- | --- |
| One JSON log line per request in Cloud Logging (`jsonPayload.engine`, `status`, `latency_ms`, `model_usage`) and the dashboard (#27) | **per-engine** request counts, failures, p95 latency and tokens. This is the main source for criteria 1, 3, 4 and 6. |
| Load test results, `results.jsonl` (#25) | every response with its status and client-side latency, including client timeouts (criterion 2) |
| `python -m app.quality.correctness results.jsonl` (#24) | % correct per engine, including the data-consistency rules (criterion 5) |
| Billing report for project NavCare (#16, #26) | actual spend (criterion 7) |

**How requests are counted (from the log lines):**

- A request **went to v2** when its log line has `engine` `v2` or `v1_fallback`.
- A **v2 failure** is a log line with `engine: "v1_fallback"`, or with `engine: "v2"` and `status` 500. A `503` is `data_unavailable`: a shared data service (routing, weather or ER status) failed, and v1 gets the same 503s, so it is **not** counted as a v2 failure (see criterion 1).
- Lines with `engine: "v1"` are the control group. They get the same load at the same time.
- A **client timeout** (status `0` in `results.jsonl`) can't be tied to an engine, because the server never answered. It only counts in criterion 2.

## Criteria (checked at the end of every step)

| # | Metric | How it is measured | Threshold | Action when violated |
| --- | --- | --- | --- | --- |
| 1 | **v2 failure rate** (availability) | v2 failures ÷ requests that went to v2, from Cloud Logging. A fallback is a v2 failure, even though the person still got an answer. 503 `data_unavailable` lines are left out of both counts. | ≤ 5 %. In a step with fewer than 20 v2 requests: at most 1 failure. | Rollback. If the step also has 503s (any engine), **hold and investigate** first: a shared data service is failing, not v2 |
| 2 | **User-facing error rate** | Responses with status 500+ or a client timeout ÷ all requests, both engines, from `results.jsonl`. Fallbacks are not errors for the person. | ≤ 1 % **and** at most 1 error more than in the 0 % baseline step (both must hold) | Rollback |
| 3 | **v2 p95 latency** | 95th percentile of `latency_ms` on lines with `engine` `v2` **or `v1_fallback`** (the same requests as criterion 1, without 503s). Fallbacks are included because they are usually the slowest: a request that hits the agent's 60 s limit is logged as `v1_fallback`. | ≤ 30 s (a typical run is about 16 s; the agent's hard cap is 60 s) | Rollback |
| 4 | **v1 p95 latency** (control) | Same, on `engine: "v1"` lines | ≤ 5 s | Hold and investigate: if v1 is slow too, the problem is the shared data services, not v2 |
| 5 | **Correctness and data consistency** | Correctness checker on the step's `results.jsonl`: schema, real hospital ids, numbers that add up, radius, specialised-ER rules, `call_911` on emergency phrases. The v1 comparison is done before the canary, not here. | v2 ≥ 95 % (in a step with fewer than 20 v2 answers: at most 1 wrong), and v1 = 100 % | v2 too low: rollback. v1 below 100 %: hold (it's a checker or data problem, not a v2 one) |
| 6 | **v2 model cost per request** | Average Gemini cost from `model_usage` tokens × the Vertex AI price per token (#26), over lines with `engine` `v2` **and `v1_fallback`** (a fallback still paid for the Gemini calls it made before failing) | ≤ $0.05. Measured: $0.01 to $0.03, so this leaves room for heavier messages (French, longer text) without a false rollback; criterion 7 still caps total spend | Rollback. If any v2 request's cost is unknown (a 503, a 500 or a failed model call, which may still have been billed) and the known average is within the limit: **hold and investigate**, since a pass on part of the requests is not a pass |
| 7 | **Total spend** | Billing report for the project since the canary started, and the budget alerts | Under $40 (the 80 % budget alert). Any budget alert email = violated | Rollback and **stop** the canary |

A step is **promoted only if every criterion passes** and the minimum number of v2 requests was reached.

**Small samples, on purpose.** Early steps have few v2 requests, so the rules are strict there. In step 1 (about 12 v2 requests), 2 failures or 2 wrong answers roll back, and the p95 is roughly the slowest v2 request. That's intended: a canary should be most careful while v2's share is small and the evidence is thin.

## Steps

| Step | `V2_TRAFFIC_PERCENT` | Load | Expected v2 requests |
| --- | --- | --- | --- |
| 0 (baseline) | 0 | 12 requests/min for 10 min = 120 requests | 0 |
| 1 | 10 | same | ~12 |
| 2 | 25 | same | ~30 |
| 3 | 50 | same | ~60 |
| 4 | 100 | same | 120 |

Every step uses the same command, against the canary service:

```
python -m loadtest.run --url <service-url> --engine split --rate 12 --duration 600 --max-requests 120 --no-compare --yes
```

- **Same load at every step.** It replays the same 25 scenarios (5 places across Québec × 5 messages, French and English) at the same rate. `--no-compare` because the v1 comparison needs the header, which is off here (see "Before the canary").
- **Minimum: 8 v2 requests per step.** At 10 % of 120 requests, a step ends with fewer than 8 about 8 % of the time. If that happens, we **hold**: we run the same step again for another 10 minutes and evaluate both runs together. We never promote on too little data.
- **Between steps**, we check the criteria, write the results in the canary log below, and only then change `V2_TRAFFIC_PERCENT`.
- **For the demo**, one promotion and one rollback are enough. The rollback is shown with fault injection (`V2_FAULT_ENABLED=true` with `V2_FAULT_MODE=error`, #39), or by setting the split back to 0.

## Cost of one run (estimate, check before running)

Google Routes costs far more than Gemini here. Each recommendation sends one live-traffic route matrix with up to 15 billed elements. v2 may search twice, so we count about 20. The Pro rate is $10 per 1,000 elements, after 5,000 free per month.

| | Without the cache | With the drive-time cache |
| --- | --- | --- |
| Route elements (steps 0 to 4: 378 v1 × 15 + 222 v2 × 20) | ≈ 10,100 | a few hundred: 5 places, re-fetched at most every 10 min |
| Routes cost, free tier unused | ≈ $51 | ≈ $0 |
| Routes cost, free tier already used | ≈ $101 | ≈ $5 to $10 |
| Gemini (222 v2 requests × ~$0.02) | ≈ $4.50 | ≈ $4.50 |
| **Allowed?** | **No**: over the $40 limit in criterion 7 | **Yes** |
| Pre-canary comparison run (25 v2 + 5 v1 requests) | ≈ $5 Routes + $0.50 Gemini | under $1 |

**The canary must not start without the cache.** Halving the load is not enough on its own: about 5,000 elements, so up to $51 if the free tier is used. The cache keeps driving times for each origin (rounded to about 1 km) for 10 minutes, so both engines get the same, slightly older drive times. That's fair for the comparison. Re-check the billing report after every step.

## Canary log

| Date and time | Step | v2 requests | v2 failure % | User error % | v2 p95 | v1 p95 | v2 correct % | v1 correct % | v2 cost/request | Spend so far | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | | | | |
