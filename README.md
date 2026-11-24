# NavCare

NavCare helps people find emergency rooms in Québec. The app sends the user’s location coordinates to our cloud API. The cloud API uses MSSS emergency-room data, estimated travel times and weather conditions to rank hospitals. The app shows the four options. These options are chosen by travel time, ER occupancy and weather.

COEN424/6313 Fall 2026 group project. The recommender will have two versions behind the REST API. Version **v1** is a rule-based microservice. Version **v2** is an AI agent. A canary release will move traffic from **v1** to **v2** in steps. If needed, traffic can be rolled back to **v1**.

## Status

The REST API (FastAPI) has GET /v1/health and GET /v1/recommendations?lat=..&lon=... Both recommenders answer behind it: v1 (rules) and the v2 AI agent (see [v2 AI agent](#v2-ai-agent)), with a facade that splits traffic between them for the canary. It runs locally, in Docker and on Cloud Run: https://navcare-api-dev-471599020734.northamerica-northeast1.run.app (Swagger UI at [`/docs`](https://navcare-api-dev-471599020734.northamerica-northeast1.run.app/docs)). The service is public; API keys and rate limiting are not in place yet (#22). See [Deploy to Cloud Run](#deploy-to-cloud-run).

## Folders

```
app/integrations/    ER-data download script
app/services/        Routing (Google Routes), weather (Open-Meteo) and live ER status (patient.quebec) clients
app/recommender_v1/  v1: rule-based ranking
app/agent_v2/        v2: AI agent (Gemini on Vertex AI chooses the tools to call and the ERs)
data/                Small sample data files (no personal information)
deployment/          Google Cloud deployment files
tests/               Automated tests
docs/                Requirements checklist and AI usage log
.github/workflows/   Lint and test checks on every push and pull request
```

## Setup, lint and tests

Requires Python 3.12+.

```bash
git clone https://github.com/HITC-NavCare/NavCare.git
cd NavCare
python -m venv .venv
```

Activate the virtual environment with the command for your system:

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Then install the tools and run the lint check and tests:

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

Tests never call the real web services (responses are faked), so they run offline in about a second.
For a coverage report: `pytest --cov=app --cov=orchestrator --cov=main --cov-report=term-missing`.

`requirements.txt` is for app packages; `requirements-dev.txt` adds dev tools (Ruff, pytest).
For local API keys, copy `.env.example` to `.env`. Never commit `.env` or key files.

`python main.py` needs a Google Maps key for driving times: in the team's Google Cloud project, enable the
**Routes API**, create an API key restricted to it, and put it in `.env` as `GOOGLE_MAPS_API_KEY`. Driving times
use live traffic, billed per user-to-hospital pair (up to 15 per recommendation; 5,000 free per month). Set a daily
quota cap on the Routes API in the Cloud console so usage can never go past the free amount.

## Run the API

Copy .env.example to .env and set GOOGLE_MAPS_API_KEY (choose the right key for prod or dev).
Then, in terminal run: `uvicorn app.api.main:app --reload` (Reminder to restart uvicorn whenever .env is modified)
Next, open `http://localhost:8000/docs` to try the endpoints in Swagger UI. 

With Docker (same image Cloud Run will use):

`docker build -t navcare .`
`docker run --rm -p 8080:8080 --env-file .env navcare`
Then open http://localhost:8080/docs. Docker's --env-file keeps inline comments and quotes as part of the value, so in .env put comments on their own line and write KEY=value with no quotes or spaces.

Errors always look like {"error": {"code": "...", "message": "...", "request_id": "..."}}: 400 invalid_parameter for bad query parameters, 503 data_unavailable when a data source fails, 500 internal_error for anything unexpected. 503 and 500 give clients a generic message; the real error is logged with the request_id.

The `/v1` in the paths is the API version, not the recommender engine. v1 (rules) and v2 (AI agent) answer behind the same paths, and the `engine` field in each response says which one answered (`v1`, `v2`, or `v1_fallback` when v2 failed).

### Choosing v1 or v2 (facade)

The facade ([app/api/facade.py](app/api/facade.py)) picks the engine for each request (strangler pattern):

- `V2_TRAFFIC_PERCENT` (0 to 100, in `.env` or the Cloud Run settings) is the share of requests v2 serves,
  picked at random per request. `0` sends everything to v1, which is also the rollback. Unset or invalid means 0.
- The `X-Engine: v1` or `X-Engine: v2` header forces one engine for that request, for tests and the demo.
  It only works where `ENGINE_HEADER_ENABLED=true` (as in `.env.example`). Leave it off in production:
  otherwise anyone could skip the split, including the 0% rollback, and force the costlier v2.
- `message` (optional, up to 500 characters) is the person's own words. Only v2 reads it; when v1 answers,
  `note` says the message was not taken into account.
- Both engines return the same fields. `note` and `model_usage` are `null` when v1 answers; `call_911` is too,
  unless the message has a clear emergency phrase (see the safety net below).
- When v2 falls back to v1, the response says `engine: "v1_fallback"`; the reason is logged with the
  request id, not returned.

Try it (v2 needs the Vertex AI setup below):

```bash
curl "http://localhost:8000/v1/recommendations?lat=45.5017&lon=-73.5673&message=my%20son%20has%20a%20high%20fever" -H "X-Engine: v2"
```

In Swagger UI, `message` and `X-Engine` are fields on the same endpoint.

**Logs for the canary.** Each recommendation request prints one JSON line (Cloud Logging reads it as
`jsonPayload`), including failed ones:

```json
{"severity": "INFO", "message": "recommendation", "event": "recommendation", "request_id": "…", "status": 200,
 "engine": "v2", "chosen_by": "split", "latency_ms": 8421, "model_usage": {"model_calls": 3, "prompt_tokens": 5120, "...": "..."}}
```

`status` (200, 503 or 500), `latency_ms` and `model_usage` per `engine` give the canary's error rate, p95 latency
and cost per request. Because the split happens inside the app, Cloud Run's own per-revision charts cannot tell v1
from v2; the dashboard uses these lines. Fallbacks and errors are the same line at WARNING or ERROR, with the
real error in `message`. Lines that went to v2 also carry `model_cost_usd` (estimated Gemini token cost, from
[app/cost_estimates.py](app/cost_estimates.py), priced on the Cloud Billing day in Pacific Time) or, when it can't be estimated, `model_cost_unknown` with the reason.

**Correctness for the canary.** A 200 can still be wrong, so [app/quality/correctness.py](app/quality/correctness.py)
checks each response: it must match the response schema in `docs/openapi.yaml`, use real hospital ids whose
name, address and coordinates match `data/hospitals.csv`, stay within the radius, have numbers that add up,
follow each engine's rules (no specialised ER without a message, `call_911` for emergency phrases, `note`
when v1 did not read the message, `model_usage` for v2) and, for v2 requests without a message, share at
least 2 of v1's top 4 hospitals for the same request. The full list is at the top of the file.

```bash
python -m app.quality.correctness results.jsonl --min-percent 95
```

`results.jsonl` has one JSON object per line: `{"request": {"lat", "lon", "limit", "radius_km", "message"},
"status": 200, "response": {...}, "v1_response": {...}, "engine": "v2"}`. `lat` and `lon` are required;
`limit`, `radius_km` and `message` may be missing or null. `v1_response` is optional, but without a valid
one v2 is not compared with v1 (the summary counts these). `engine` is optional: the engine forced with `X-Engine`, so that non-200 responses,
whose error body doesn't name the engine, are counted under it. Only record it where `ENGINE_HEADER_ENABLED=true`;
elsewhere the canary split ignores the header and the engine would be wrong. It prints the % correct per engine and the
problems found. Non-200 responses are not checked; they count towards the error rate, and so does a
`status` of `0` or `null` (no HTTP response, e.g. a timeout). A line that can't be
checked (including one that isn't valid JSON or UTF-8) is reported as a bad record and the run goes on.
Exit code: 0 when every engine reaches `--min-percent` (default 100), each engine has at least one 200 of its
own to check (so a v2 that always fell back to v1 fails) and there are no bad records; 1 otherwise, including
for an empty file; 2 when the file can't be opened.
It needs `requirements-dev.txt` and is not part of the deployed API.

**Load test for the canary.** [loadtest/run.py](loadtest/run.py) starts requests at a fixed rate (slow v2
answers overlap instead of slowing the test), cycling through 5 places (Montréal to rural Mont-Laurier) × 5
messages (none, a child with a fever in French, a mental-health crisis, chest pain, a sprained ankle). It saves
each answer as it arrives, with its client-side latency, to `loadtest/results/` (ignored by Git) in the checker's
format, so a run stopped with Ctrl-C keeps what it already sent. It prints the error rate (non-200s, timeouts,
connection errors and 200s that aren't JSON) and p50/p95 latency per engine, then runs the correctness checker.
It also prints a `v2-routed` line for [canary criterion 1](docs/canary.md): v2 failures, counting v1 fallbacks,
over the requests that went to v2 (503s, timeouts and connection errors left out). With `--engine split` a non-200
doesn't say which engine failed, so the request logs stay the source for that criterion.

With `--engine v2`, it also asks v1 the same question for v2 answers without a message, so the checker can
compare them. With `--engine split` that is off by default: production ignores `X-Engine`, so the "v1" request
would go through the split again and add unplanned traffic to the canary. `--compare`/`--no-compare` override it.

```bash
# Against a local server (start it with ENGINE_HEADER_ENABLED=true to force an engine)
python -m loadtest.run --engine v1 --rate 30 --duration 60 --max-requests 20
# A canary step on Cloud Run (docs/canary.md): 12/min for 10 min = 120 requests; the split decides
python -m loadtest.run --url https://<service-url> --engine split --rate 12 --duration 600 --max-requests 120 --no-compare --yes
```

Every request uses the real paid services, even against a local server: the Routes API is billed per
origin-hospital pair and each v2 request makes up to 8 Gemini calls. So `--max-requests` caps the load requests
(default 20), and the plan printed first also counts the v1 comparison requests and the most Routes elements the
run can use, with no drive-time cache: 15 per v1 request, up to 45 per v2 request (2 searches of 15, plus 15 if it
falls back to v1), and for `split` as if every request went to v2. For the canary step above that is 5,400
elements, up to $54 once the 5,000 free ones of the month are used, which is why [docs/canary.md](docs/canary.md)
requires the drive-time cache first. A run of more than 20 requests in all or one against a server that is not
local needs `--yes`, and `--dry-run` prints the plan without sending anything. Options are checked before
anything is sent (`--radius-km` 1–200, `--min-percent` 0–100). The default client timeout (130 s) is longer than
Cloud Run's (120 s), so a request the service gives up on shows as its 504, not as a client timeout.
`--engine v1`/`v2` needs `ENGINE_HEADER_ENABLED=true` on the server; the run warns when the header was ignored.
The exit code is the checker's (`--min-percent` is passed to it), 1 when the run was interrupted, or 2 when it
did not start.

### Fault injection (canary demo)

Makes v2 fail or slow down on purpose ([fault_injection.py](app/agent_v2/fault_injection.py)). Set only by
environment variables; off by default; affects only requests routed to v2 (and `python main.py v2`).

- Needs both `V2_FAULT_ENABLED=true` and a `V2_FAULT_MODE`; either alone does nothing.
- `error`: v2 fails before calling Gemini; v1 answers with HTTP 200, `engine: "v1_fallback"` and a WARNING log line. The canary must count `v1_fallback` as a v2 failure.
- `slow`: v2 waits `V2_FAULT_SLOW_SECONDS` (default 35, max 60), using up part of the agent's 60 s Gemini budget, then runs. The wait is in `latency_ms`. A slow 200 is likely but not guaranteed: if the budget runs out, v2 falls back to v1.
- The budget is not a deadline for the whole request: tool calls and the v1 fallback can run past it.
- Unknown mode means off; an invalid wait uses 35. Each distinct setting, and an active fault, is logged once.
- Each waiting request holds a worker thread from a shared pool (40 by default). Enough of them at once make all other requests queue.

```bash
# Local: set both in .env and restart uvicorn; remove them and restart to stop.
# Cloud Run (each change creates a new revision):
gcloud run services update <service> --update-env-vars V2_FAULT_ENABLED=true,V2_FAULT_MODE=error                # on
gcloud run services update <service> --remove-env-vars V2_FAULT_ENABLED,V2_FAULT_MODE,V2_FAULT_SLOW_SECONDS # off
```

## v2 AI agent

v2 takes the same location as v1 plus an optional message in the person's own words. Through the API
(see [Choosing v1 or v2](#choosing-v1-or-v2-facade)), or from the command line:

```bash
python main.py v2 my 6-year-old has a high fever
```

Gemini (on Vertex AI) decides which of our services to call (nearby hospitals with driving times, live ER
status, weather), then chooses up to four ERs and writes a reason for each. Unlike v1 it can recommend a
children's, mental-health or cardiology ER when the message calls for one, sets `call_911` when the message
sounds life-threatening, and answers in French or English.

- **911 safety net:** besides the model, our code looks for clear emergency phrases in English and French
  ("chest pain", "can't breathe", "AVC", "ne respire plus"…, in [app/red_flags.py](app/red_flags.py)). A match sets
  `call_911` to `true` whichever engine answered; it can raise the model's answer, never lower it. It matches
  phrases, not meaning: it misses other wordings (the model covers those) and also flags "no chest pain".
- **Same output as v1, plus:** `engine` (`v2` or `v1_fallback`), `call_911`, `note` and `model_usage`
  (model calls, tool calls and tokens for the request). Model calls include retried attempts, and
  `model_usage` is kept when v2 falls back, so the cost of every v2 request is recorded.
  `calls_without_usage` (answers with no token counts) and `failed_calls` (calls that failed, possibly
  still billed) mean the token totals may be too low, so the cost estimate is unknown.
- **Checked by our code:** the model only picks hospital ids and writes reasons. Every number comes from our
  data, and an id that was not returned by our services is rejected.
- **Specialised ERs are checked too:** the model must say which specialised care the message shows is
  needed (`children`, `mental_health`, `cardiology` or none). A children's, mental-health or cardiology ER
  is only accepted for a need it declared, and with no message only general ERs are accepted. The code
  cannot tell from the text whether the patient is a child; it makes sure the model's choice matches what
  it declared.
- **Fallback:** if Gemini fails or gives an invalid answer, the v1 ranking is returned with
  `engine: "v1_fallback"`. `python main.py v2` also prints a `fallback_reason`; the API logs it instead.
- **Not deterministic:** the same request can give a different order or wording each time.
- **Limits per request:** 8 model calls, 2 hospital searches (each search is one Google Routes request),
  30 seconds per model call and 60 seconds in total (each call only gets the time that is left).
  Temporary Gemini errors (429, 500, 503) are retried once.
- **No hospitals, no answer:** if the first hospital search fails, v2 stops and falls back, instead of
  returning an empty list as if nothing were nearby.
- **Fallback note:** when v2 falls back after the person wrote a message, `note` says the message was not read
  and points to the specialised ERs in `unranked_nearby`.

Setup (once per machine):

1. In the team's Google Cloud project, enable the **Vertex AI API**. Your account needs the *Vertex AI User* role.
2. Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install) and run
   `gcloud auth application-default login`. Vertex AI uses this sign-in, not an API key.
3. In `.env`, set `GOOGLE_CLOUD_PROJECT` to the project id.

Model calls are billed to the project (covered by the trial credit while it lasts). Tests fake Gemini, so they
cost nothing.

## Deploy to Cloud Run

Target: dev service `navcare-api-dev` in `northamerica-northeast1` (deployed 2026-11-22: https://navcare-api-dev-471599020734.northamerica-northeast1.run.app), run as `navcare-run`; the Maps key comes from Secret Manager.

- `deployment/deploy.sh` builds the committed code (`HEAD`) with Cloud Build, pushes it to Artifact Registry (`navcare`) and deploys it. Run it from Git Bash or a Unix shell with gcloud signed in (`CLOUDSDK_CORE_ACCOUNT` picks the account; `GCLOUD` the gcloud path).
- Settings: 1 vCPU, 512 MiB (the app uses about 200 MiB), concurrency 40 (the server's worker threads), timeout 120 s (v2's 60 s Gemini budget plus tools and the v1 fallback), 0 to 2 instances. The instance cap limits exposure; it is not a spending cap.
- `V2_TRAFFIC_PERCENT=0` is set only when the service is created, so a redeploy keeps the canary share. Fault injection stays off. `ENGINE_HEADER_ENABLED` is off unless set to `true` for a controlled dev test; turn it off again afterwards (`gcloud run services update navcare-api-dev --region=northamerica-northeast1 --update-env-vars=ENGINE_HEADER_ENABLED=false`).
- The script never changes who can call the service. The first deploy needs two project-owner steps: give Cloud Build's default service account `roles/cloudbuild.builds.builder` (the organization blocks automatic grants), and after a private smoke test give `allUsers` `roles/run.invoker` on the service.
- Check a deploy with `python -m deployment.smoke_test <url>` (one paid v1 request); add `--v2` in dev for one forced v2 request. A 200 is not enough: the engine and the correctness checker must pass.

## Canary dashboard

`deployment/monitoring/` holds templates for the log-based metrics and the Cloud Monitoring dashboard for the canary criteria in [docs/canary.md](docs/canary.md).

- `deployment/monitoring/apply.sh` creates them for one service (default `navcare-api-dev`), or updates them in place (the dashboard with its current etag). Same sign-in as `deploy.sh`. Metrics only count log lines written after they exist.
- Another service gets its own metrics and dashboard: `SERVICE=navcare-api-prod deployment/monitoring/apply.sh`. Names start with the service (`navcare_api_dev_requests`, dashboard "NavCare canary: navcare-api-dev"); the script refuses a metric that reads another service.
- Charts use 10-minute windows (one canary step), read at the step's end time: traffic per engine, v2 failure rate (criterion 1), v2 and v1 p95 latency (3, 4), estimated model-token cost (6) and tokens.
- Went to v2: `v2` or `v1_fallback`. A v2 failure: `v1_fallback`, or `v2` with status 500. 503s are left out of criteria 1 and 3.
- No traffic shows a gap, not a pass. Counts come from `increase()`, which extrapolates and can be fractional, so the 20-request and 1-failure lines are approximate; `loadtest/step_check.py` counts exactly and decides.
- Cost covers Gemini tokens only, over every line that went to v2 (criterion 6). A v2 503 or 500, a failed model call or one without token counts has an unknown cost. The known average is drawn as "complete", or as "INCOMPLETE: hold" when anything in the step is unknown; unknown counts by status are next to it.
- Criteria 2, 5 and 7 come from `results.jsonl` and the billing report, not the dashboard.

## Hospital and ER data

Download the latest hourly ER file (standard library only; works from any folder):

```bash
python app/integrations/ingestion.py
```

It saves `data/snapshots/Releve_horaire_urgences_7jours_nbpers.csv` (UTF-8, ignored by Git) and prints the
column names and first five rows. If accents look wrong in Git Bash, run with `PYTHONUTF8=1`.

**Sources** (both MSSS, CC-BY 4.0, from Données Québec):

- [Fichier horaire des données de la situation à l'urgence](https://www.donneesquebec.ca/recherche/dataset/fichier-horaire-des-donnees-de-la-situation-a-l-urgence):
  hourly ER file. We use the
  [`_nbpers` resource](https://www.msss.gouv.qc.ca/professionnels/statistiques/documents/urgences/Releve_horaire_urgences_7jours_nbpers.csv)
  because it adds region, people present and people waiting for a doctor. Data are extracted every hour and published
  about 45 minutes later; average-stay (DMS) columns are from the previous day. It is Windows-1252 encoded, and some
  header names contain stray tabs or spaces. It has facility names, `No_permis_installation`, `RSS` and `Region`,
  plus 16 regional-total rows and 1 province-total row (blank permit number). No addresses, coordinates or pediatric information.
- [Fichiers cartographiques M02 des installations et établissements](https://www.donneesquebec.ca/recherche/dataset/fichiers-cartographiques-m02-des-installations-et-etablissements):
  "Liste des installations" CSV with civic address and longitude/latitude (geocoded from the address with Adresses Québec).

**`data/hospitals.csv`** lists the 120 facilities in the hourly file (checked 2026-10-22). `id` is the MSSS 8-digit
installation code: `No_permis_installation` in the ER file, `INSTAL_COD` in M02. Every ID matched M02 exactly,
and the name and region code also agreed for all 120. `address` is M02 `ADRESSE, MUN_NOM, QC CODE_POSTA`; `lat`/`lon` are M02
values unchanged; `region` is the ER file's `Region`. `pediatric` is blank for all rows: no official source found
says which ERs treat children, and blank does not mean "no".

**Known limitations**

- The plain `Releve_horaire_urgences_7jours.csv` resource still uses older codes for 29 of the same 120 facilities
  (for example Hôpital général juif `12685608` vs `51248250`); those older codes are not in M02. Use the `_nbpers` file.
- Codes can change when facilities are reorganised, so re-check `hospitals.csv` against both sources periodically.
- On 2026-10-22 the occupancy columns read `pas d'information disponible` for 12 facilities: all 8 in
  Mauricie-et-Centre-du-Québec and 4 of Nord-de-l'Île-de-Montréal (Sacré-Coeur, Fleury, Jean-Talon, Albert-Prévost),
  matching the outage notice on the dataset page. Value columns are text, so parse them defensively.
- Coordinates are address geocodes, not ER entrances. The list includes small CLSC/multiservice ERs and specialised
  sites (for example psychiatric and cardiology institutes) because the MSSS file includes them.

## Tech stack

| Part | Choice | Status |
| --- | --- | --- |
| Backend language (API, integrations, v1, v2) | Python | Proposed |
| Web framework | FastAPI, a small Python framework for REST APIs | In use (app/api/main.py)
| Frontend | Android app that calls the Python backend's REST API. App language (Kotlin or Java) not yet chosen. Until the app exists, the API can be tried in Swagger UI. | Decided: Android; language pending |
| Hosting | Google Cloud (team decision). Cloud is where the app runs. | Planned |

Planned Google Cloud services: Cloud Run (API, v1, v2), Firestore (NoSQL data), Vertex AI Gemini (v2 agent),
Secret Manager (API keys), Cloud Scheduler (ER data refresh), Google Maps Routes API (travel time),
Cloud Monitoring and billing budgets (canary dashboard, costs).
External data: WeatherAPI and Québec ER data (patient.quebec).

## Coding conventions

| What | Style | Example |
| --- | --- | --- |
| Files, functions, variables | snake_case | `travel_time.py`, `get_hospitals()`, `hospital_id` |
| Classes (only when useful) | PascalCase | `HospitalClient` |
| Constants | UPPER_SNAKE_CASE | `REQUEST_TIMEOUT_SECONDS` |

Write `get_hospitals`, not `GetHospitals` (PascalCase) or `getHospitals`
(JavaScript/Java style). Following one standard (PEP 8) means everyone can guess a name without looking it up.
`ruff check .` flags most naming mistakes.

- Indent with 4 spaces.
- Use clear, descriptive names; avoid unexplained abbreviations (`travel_minutes`, not `tm`).
- Keep functions short with one clear purpose. Prefer simple functions over classes or abstractions you don't need yet.
- Comments explain *why*, not what the code already says.
- Documentation states actual behaviour, inputs, outputs and limitations in plain English.
- Never silently invent data or hide failures: if an API call fails, return or log a clear error.
- Keep external field names as the API or data source defines them (for example a field from the ER data or the
  Routes API response); don't rename them just to match Python style.

## Contributing

1. Create a branch from `main`: `git switch -c feature/short-description`
2. Commit small changes with clear messages.
3. Run `ruff check .` and `pytest`, then push and open a pull request into `main`.
4. Merge after the lint and test checks pass and a teammate reviews it.
5. If AI helped, add a row to the AI usage log in the same pull request.

## Documentation

- [Course requirements checklist](docs/requirements.md)
- [AI usage log](docs/ai-usage/log.md)
