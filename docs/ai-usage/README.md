# AI usage records

The course asks us to document AI-assisted development so it can be traced and reproduced.
Add an entry to [log.md](log.md) whenever an AI tool produces something we keep
(code, configuration, documentation, slides, design ideas).

- Save long prompts in [prompts/](prompts/) as `YYYY-MM-DD-short-name.md` and link them from the log.
- Write "unknown" or "not recorded" when a detail is missing. Do not reconstruct old prompts from memory and present them as the original.
- Remove API keys, tokens, passwords and personal information before saving a prompt or output.

## Entry format

Copy this block to the end of `log.md`:

```markdown
## YYYY-MM-DD: short task name

- **Tool / model:** for example Claude Code (claude-opus-5-5), ChatGPT (GPT-x), unknown
- **Who:** team member who ran it
- **Task:** what the AI was asked to help with
- **Prompt:** the prompt, or a link to the file in prompts/
- **Output used:** which files or parts were kept, and what was changed
- **Human verification:** what a person checked (ran the code, reviewed the PR, compared with sources)
```
