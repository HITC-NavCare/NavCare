# NavCare

NavCare helps people find emergency rooms in Québec. The app sends the user’s location coordinates to our cloud API. The cloud API uses MSSS emergency-room data, estimated travel times and weather conditions to rank hospitals. The app shows the four options. These options are chosen by travel time, ER occupancy and weather.

COEN424/6313 Fall 2026 group project. The recommender will have two versions behind the REST API. Version **v1** is a rule-based microservice. Version **v2** is an AI agent. A canary release will move traffic from **v1** to **v2** in steps. If needed, traffic can be rolled back to **v1**.

## Status

The REST API (FastAPI) has GET /v1/health and GET /v1/recommendations?lat=..&lon=... Both recommenders answer behind it: v1 (rules) and the v2 AI agent (see [v2 AI agent](#v2-ai-agent)), with a facade that splits traffic between them for the canary. It runs locally and in Docker. Not deployed yet (Cloud Run is next).

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
real error in `message`.

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

### Fault injection (canary demo)

Makes v2 fail or slow down on purpose ([fault_injection.py](app/agent_v2/fault_injection.py)). Set only by
environment variables; off by default; affects only requests routed to v2 (and `python main.py v2`).

- `V2_FAULT_MODE=error`: v2 fails before calling Gemini; v1 answers with `engine: "v1_fallback"` and a WARNING log line.
- `V2_FAULT_MODE=slow`: v2 waits `V2_FAULT_SLOW_SECONDS` (default 35, max 120), then answers normally. The wait is included in `latency_ms`.
- Unknown mode means off; an invalid wait uses 35. Both log a warning.
- Each waiting request holds a worker thread from a shared pool (40 by default). Enough of them at once make all other requests queue.

```bash
# Local: set V2_FAULT_MODE in .env and restart uvicorn; remove it and restart to stop.
# Cloud Run (each change creates a new revision):
gcloud run services update <service> --update-env-vars V2_FAULT_MODE=error                 # on
gcloud run services update <service> --remove-env-vars V2_FAULT_MODE,V2_FAULT_SLOW_SECONDS # off
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
