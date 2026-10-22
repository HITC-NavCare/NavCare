# NavCare

NavCare helps people find emergency rooms in Québec. The app sends the user’s location coordinates to our cloud API. The cloud API uses MSSS emergency-room data, estimated travel times and weather conditions to rank hospitals. The app shows the four options. These options are chosen by travel time, ER occupancy and weather.

COEN424/6313 Fall 2026 group project. The recommender will have two versions behind the REST API. Version **v1** is a rule-based microservice. Version **v2** is an AI agent. A canary release will move traffic from **v1** to **v2** in steps. If needed, traffic can be rolled back to **v1**.

## Status

Scaffold plus the hospital list and a local ER-data download script (see [Hospital and ER data](#hospital-and-er-data)).
There is no API or recommender yet. Nothing is deployed.

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

- [Copy-paste terminal commands (PowerShell)](docs/commands/) for setup, ingestion, data checks, Git checks and lint
- [Course requirements checklist](docs/requirements.md)
- [AI usage log](docs/ai-usage/log.md)
