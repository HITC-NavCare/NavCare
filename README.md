# NavCare

NavCare helps people find emergency rooms in Québec. The app sends the user’s location coordinates to our cloud API. The cloud API uses MSSS emergency-room data, estimated travel times and weather conditions to rank hospitals. The app shows the four options. These options are chosen by travel time, ER occupancy and weather.

COEN424/6313 Fall 2026 group project. The recommender will have two versions behind the REST API. Version **v1** is a rule-based microservice. Version **v2** is an AI agent. A canary release will move traffic from **v1** to **v2** in steps. If needed, traffic can be rolled back to **v1**.

## Status

Scaffold only: folders, lint check and documentation. There is no application code yet. Nothing runs and nothing is deployed.

## Folders

```
app/integrations/    Weather, travel-time and ER-data clients
app/recommender_v1/  v1: rule-based ranking
app/agent_v2/        v2: AI agent
data/                Small sample data files (no personal information)
deployment/          Google Cloud deployment files
tests/               Automated tests
docs/                Requirements checklist and AI usage log
.github/workflows/   Lint check on every push and pull request
```

## Setup and lint check

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

Then install the tools and run the lint check:

```bash
pip install -r requirements-dev.txt
ruff check .
```

`requirements.txt` is for app packages; `requirements-dev.txt` adds dev tools (Ruff).
For local API keys, copy `.env.example` to `.env`. Never commit `.env` or key files.

## Tech stack

| Part | Choice | Status |
| --- | --- | --- |
| Backend language (API, integrations, v1, v2) | Python | Proposed |
| Web framework | FastAPI, a small Python framework for REST APIs | Proposed, not installed |
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
3. Run `ruff check .`, then push and open a pull request into `main`.
4. Merge after the lint check passes and a teammate reviews it.
5. If AI helped, add a row to the AI usage log in the same pull request.

## Documentation

- [Course requirements checklist](docs/requirements.md)
- [AI usage log](docs/ai-usage/log.md)
