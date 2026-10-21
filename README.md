# NavCare

NavCare recommends the best emergency rooms to go to in Québec. A user sends their location
(latitude, longitude) and NavCare returns the top 4 hospitals, based on travel time, current
ER occupancy and weather.

It is the COEN424/6313 (Fall 2026) group project. The recommender has two versions behind the
same REST API:

- **v1, rule-based microservice:** ranks hospitals with a fixed scoring formula.
- **v2, AI agent:** a Gemini model decides which data sources to call and how to rank hospitals.

A canary release moves traffic from v1 to v2 step by step, with rollback to v1 if v2 fails agreed criteria.

## Current status

**Only the repository scaffold exists.** There is no application code yet, nothing runs and nothing
is deployed. What exists:

- Folder structure, with a README in each folder that says what will go there.
- A lint check (Ruff) that runs locally and on GitHub for every push and pull request.
- The course requirements checklist and AI-usage records in `docs/`.

## Folders

```
app/                 Application code (see app/README.md)
  integrations/      Weather, travel-time and ER-data clients
  recommender_v1/    v1: rule-based ranking
  agent_v2/          v2: AI agent
data/                Small sample data files
deployment/          Google Cloud deployment files (none yet)
tests/               Automated tests
docs/                Requirements checklist and AI-usage records
.github/workflows/   GitHub Actions (lint check)
```

The entry point will be `app/main.py`; it is not created yet.

### Changes from the draft-1 file structure

- No AWS files (`task-definition.json`, `appspec.yaml`): we moved to Google Cloud. `deployment/` will hold Cloud Run files instead.
- `decisionAlgo/rulesBased.py` and `aiAgentBased.py` became the folders `recommender_v1/` and `agent_v2/`, because each version will need several files.
- `services/` became `integrations/`, with Python-style names (`travel_time.py`, not `travelTimeEstimate.py`). Travel time will come from the Google Maps Routes API instead of OSRM.
- `.gitignore` is a file, not a folder, and `APIKeys.txt` is ignored instead of stored. Keys go in a local `.env` file or Secret Manager.
- No `Dockerfile` yet; it will be added when there is an app to package.
- `tests/unitTest` and `integrationTest` will be added when there are tests to put in them.
- Added `data/` and `docs/`.

## Local setup

You need Python 3.12 or newer and git.

```bash
git clone https://github.com/HITC-NavCare/NavCare.git
cd NavCare
python -m venv .venv
```

Activate the virtual environment:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate
```

Install the development tools and run the lint check (the same check GitHub runs):

```bash
pip install -r requirements-dev.txt
ruff check .
```

`requirements.txt` holds the app's runtime packages; `requirements-dev.txt` adds tools such as Ruff.

For API keys, copy `.env.example` to `.env` and fill in your own values. Never commit `.env`.

## Planned Google Cloud stack

| Service | Use |
| --- | --- |
| Cloud Run | Hosts the API, v1 and v2 |
| Firestore | NoSQL store for hospital and ER data |
| Vertex AI (Gemini) | Model used by the v2 agent |
| Secret Manager | API keys (WeatherAPI, Google Maps) |
| Cloud Scheduler | Regular refresh of ER data |
| Google Maps Routes API | Travel time to each hospital |
| Cloud Monitoring and Billing budgets | Canary metrics dashboard, cost tracking and budget alerts |

External data: [WeatherAPI](https://www.weatherapi.com) and Québec ER data from [patient.quebec](https://patient.quebec/en/data).

## How to contribute

1. Pick or create an issue, assign yourself, and move it to **In progress** on the project board.
2. Create a branch from `main`: `git switch -c feature/short-description` (or `fix/...`, `docs/...`).
3. Commit small changes with clear messages, for example `Add WeatherAPI client`.
4. Run `ruff check .` before pushing.
5. Push and open a pull request into `main`. Write `Closes #<issue number>` in the description.
6. Merge after the lint check passes and a teammate has reviewed it.
7. If AI helped, add an entry to `docs/ai-usage/log.md` in the same pull request.

## Where things are documented

- **Course requirements:** [docs/requirements.md](docs/requirements.md) (summary of the lecturer's project description).
- **Tasks:** GitHub issues and the project board of the HITC-NavCare organization.
- **AI-use records:** [docs/ai-usage/](docs/ai-usage/).
