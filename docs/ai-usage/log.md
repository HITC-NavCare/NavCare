# AI usage log

Record AI assistance briefly, including what was used and how it was checked. Use “N/A” for unknown details. Do not include secrets or personal information.

| Date | Tool/model | Purpose | What was used/checked | Instructions summary |
| --- | --- | --- | --- | --- |

| 2026-10-21 | Claude Code in VS Code; model: Opus 5.5 | The AI helped convert the team’s project outline into a repository scaffold. Later requests were about simplifying READMEs, documenting the proposed tech stack and coding rules, and checking linting and secret exclusions. | The AI reviewed the repository structure, documentation and basic CI setup in `chore/repo-setup`. Local lint and ignore checks passed based on the tool's output. | Help setup the rest of the project scafold using Google Cloud and the Routes API, a Ruff lint workflow and ignored secrets, with no fake implementations. Then enhance the docs, add the tech stack and naming conventions, and help cleanup the seupt frontend to Android. Review the NavCare scaffold in chore/repo-setup. Check .gitignore and .env.example for secret handling and validate requirements-dev.txt, ruff.toml and .github/workflows/lint.yml. Run ruff check if needed. and git check-ignore. Check README.md against the planned Google Cloud stack and Python naming rules. Suggest fixes, remove repetition and report any problems. Do not add features or expand the scope. |
