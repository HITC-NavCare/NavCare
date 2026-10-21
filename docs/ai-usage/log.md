# AI usage log

Newest entries at the bottom. Format: see [README.md](README.md).

## Date unknown (before draft 1): folder structure suggestions

- **Tool / model:** not recorded
- **Who:** not recorded
- **Task:** suggest a folder structure for the project.
- **Prompt:** not recorded.
- **Output used:** the draft-1 document says the `deployment/`, `tests/`, `.github/` folders, the Dockerfile and `requirements.txt` were recommended by AI. They were included in the draft's sample file structure (made with UtilityCove).
- **Human verification:** the team reviewed the structure for draft 1. It was simplified during repository setup on 2026-10-21 (see the next entries).

## Date unknown: drafting presentation slides

- **Tool / model:** Claude (model version not recorded)
- **Who:** not recorded
- **Task:** help draft presentation slides.
- **Prompt:** not recorded.
- **Output used:** not recorded.
- **Human verification:** not recorded.

## 2026-10-21: repository setup (Task 2)

- **Tool / model:** Claude Code in VS Code, model Claude Opus 5.5 (`claude-opus-5-5`)
- **Who:** George
- **Task:** set up the repository: folder structure, `.gitignore`, README, Ruff lint workflow, AI-usage records and the project backlog.
- **Prompt:** [prompts/2026-10-21-repository-setup.md](prompts/2026-10-21-repository-setup.md). The lecturer's project description and our draft-1 document were attached as PDFs.
- **Output used:** all files added in the `chore/repo-setup` branch.
- **Human verification:** `ruff check .` was run locally and passed. Team review in the pull request is pending.
