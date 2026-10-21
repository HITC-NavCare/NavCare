# AI usage log

Add a row whenever an AI tool produces something we keep. Summarise the instructions given,
write "not recorded" for anything unknown, and never paste keys, tokens or personal information.

| Date | Tool/model | Purpose | What was used/checked |
| --- | --- | --- | --- |
| Not recorded (before draft 1) | Not recorded | Suggest a project folder structure | Draft 1 says AI recommended `deployment/`, `tests/`, `.github/`, the Dockerfile and `requirements.txt`. The team reviewed it; it was simplified on 2026-10-21. Prompt not recorded. |
| Not recorded | Claude (version not recorded) | Help draft presentation slides | Which slides, the prompt, and what was kept or checked: not recorded. |
| 2026-10-21 | Claude Code in VS Code, Claude Opus 5.5 (`claude-opus-5-5`) | Repository setup (Task 2). Inputs: lecturer's project description PDF and team draft-1 PDF. Instructions: Google Cloud instead of AWS; Routes API instead of OSRM; simple student-readable scaffold for app, v1, v2, integrations, data, deployment, tests, docs; `.gitignore` for secrets; Ruff lint on every push/PR; no fake implementations or invented results. Follow-ups: simplify the docs and READMEs; add a README tech stack (confirmed vs proposed) and Python naming conventions. | All files in branch `chore/repo-setup`. Checked: `ruff check .` passes locally; secret files confirmed ignored with `git check-ignore`. Team review in the PR: pending. |

**Missing information to fill in:** date, tool/model and prompt for the folder-structure suggestion;
date, model, prompt, author and checks for the slide drafting.
