# Prompt: repository setup (Task 2)

- **Date:** 2026-10-21
- **Tool / model:** Claude Code in VS Code, Claude Opus 5.5 (`claude-opus-5-5`)
- **Attachments:** `Project_Description-Fall2026_Updated.pdf` (lecturer's requirements) and
  `COEN424_project_draft1 (1).pdf` (team draft). Not stored here.
- **Note:** saved as sent. The repository URL placeholder was not filled in; Claude found the
  repository from the local git remote.

---

Help me complete TASK 2: repository setup for our COEN424 NavCare group project.

Existing repository: [PASTE HITC-NavCare REPOSITORY URL HERE]

Read both attached PDFs first:
- Project_Description-Fall2026_Updated.pdf: teacher’s requirements.
- COEN424_project_draft1 (1).pdf: our team’s initial architecture and proposed folder structure.

The teacher’s document governs course requirements. The team document describes our proposed design, not completed implementation.

LATEST TEAM DECISIONS
- Google Cloud instead of AWS.
- Cloud Run, Firestore, Vertex AI/Gemini, Secret Manager and Cloud Scheduler are planned.
- Google Maps Routes API is the team’s current routing choice, replacing OSRM in the older draft.
- Hospital/ER data ingestion is a separate task.
- The repository already exists. Inspect it and preserve teammates’ work.

STYLE AND SCOPE
Make everything as simple, readable and understandable as possible for students.
Use clear names, short functions, plain English and consistent Python naming.
Comments should explain something useful, not repeat the code.
Avoid vague descriptions, unnecessary jargon, repetitive AI-style text and unnecessary files.
Use minimal dependencies and no elaborate abstractions or infrastructure.
Document AI assistance honestly.
Complete repository setup now; do not build the whole application.

TASK 2 REQUIREMENTS

1. Folder structure, .gitignore and README
- Inspect the existing repository before changing anything.
- Follow the team’s proposed structure where sensible, simplifying it where needed.
- Keep clear places for:
  application entry point,
  weather/travel/ER integrations,
  rule-based v1,
  AI-agent v2,
  data,
  deployment,
  tests,
  documentation,
  GitHub Actions.
- Briefly explain changes from the PDF’s structure. Do not include AWS deployment files in our Google Cloud setup.
- Use small README files or .gitkeep only where needed to preserve empty directories.
- Do not create fake implementations or functions that return invented hospital recommendations.
- Never commit APIKeys.txt, credentials, tokens, service-account keys or real .env files.
- Add an .env.example containing only clearly explained placeholders if needed.
- Configure .gitignore for Python caches, virtual environments, local secrets and generated files. Keep useful sample data and dependency files trackable.

Write a concise README covering:
- What NavCare does.
- Current status: what actually exists versus what is planned.
- Folder purposes.
- Exact local setup and check commands that work at this stage.
- Planned Google Cloud stack.
- How teammates contribute: branch, commit, pull request.
- Where requirements, tasks and AI-use records are documented.

Do not claim the application can run or is deployed if only the scaffold exists.

2. GitHub Projects board and issues
- Reuse an existing board and issues if available; avoid duplicates.
- Otherwise create a simple board: Todo, In progress, Done.
- Create concise, actionable issues covering the remaining project work:
  cloud access and API setup;
  budget alerts before deployment/testing;
  repository and CI setup;
  official hospital/ER data inspection and ingestion;
  hospital metadata;
  API contract and SwaggerHub;
  data model and Firestore;
  external service integrations;
  rule-based v1;
  AI-agent v2 with actual tool/backend selection;
  facade and canary routing;
  monitoring and cost tracking;
  meaningful tests and controlled load testing;
  D3 slides;
  final report, demonstration and source-code package.
- Group closely related work to keep the board manageable.
- Each issue needs a short purpose and a few concrete acceptance criteria.
- Only assign people when their ownership is confirmed. Otherwise leave unassigned.
- Only mark work Done when verified.

Course requirements that must appear in the backlog:
- v1 and v2 expose the same API.
- v2 uses a model to decide how to handle requests or call tools; merely rewording v1 output is insufficient.
- A NoSQL data store.
- Cost attribution for v1/v2 and model usage, including cost per request.
- A canary criteria table covering availability, p95 latency, correctness/data consistency, and cost.
- At least three canary traffic steps before full promotion.
- A final demonstration showing promotion and rollback.
- Monitoring dashboard.
- One explicitly selected extra feature from the teacher’s list. Record the team’s choice as pending unless already confirmed.
- Traceable AI-use documentation.
- Source code and reports accessible to the lecturer and PoD.

Do not invent measured performance, costs, thresholds or completed work. Record unconfirmed choices as pending.

3. GitHub Actions
- Add a small workflow running on every push and pull request.
- A basic Python lint check is sufficient.
- Use one lightweight tool, such as Ruff, with a minimal configuration.
- Keep development dependencies separate from application dependencies.
- Make the same check runnable locally with an exact documented command.
- Make it work for the current scaffold without manufacturing application code just to pass CI.
- Run the check locally. Verify the GitHub run if access permits.
- Do not add deployment automation or cloud credentials.

4. docs/ai-usage/
- Add a short explanation and a reusable log format recording:
  date if known, tool/model if known, task, prompt, output used, and human verification.
- Save this setup prompt as an actual prompt record.
- Record only prior AI use supported by the supplied documents or conversation.
- Known examples: AI suggestions for the original folder structure and Claude assistance drafting slides.
- If earlier exact prompts, dates or model versions are unavailable, say so. Do not reconstruct them and pretend they are original.
- Keep secrets and personal information out of the logs.

5. Lecturer and PoD access
- Check whether they already have access.
- Ask me for their exact GitHub usernames if unavailable; do not guess.
- Once identities are provided, invite them with suitable repository access.
- Clearly distinguish an invitation sent from an invitation accepted.
- If permissions prevent this, provide the precise manual steps and leave it pending.

WORKING RULES
- Work in the existing repository on a dedicated branch.
- Preserve existing files and teammates’ changes.
- Complete the authorized setup without asking about every routine choice.
- Do not deploy cloud resources, enable paid services or merge into the default branch.
- If GitHub access is available, push the branch and open a reviewable pull request.
- If a GitHub operation is blocked, complete the local work and clearly list the remaining manual steps.
- Keep documentation proportional: no large collection of mostly empty planning documents.

FINISH WITH
- Files created or changed.
- Checks run and actual results.
- Board, issues and pull-request links, where created.
- Any genuinely blocked steps.
- A short factual update I can send to my teammates.

Start by inspecting the PDFs and repository, then implement Task 2.
