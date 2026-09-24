# standup-agent

Python script (`standup.py`) that builds a standup message from git commits (several repos) and Jira Cloud
tickets, then asks a model on the company AI gateway to write it. See `docs/design-notes.md` for the
decisions and `docs/plan.md` for the original plan.

- Single file, one dependency (`requests`). Must run on Windows and Linux.
- Secrets only via env vars (`JIRA_EMAIL`, `JIRA_API_TOKEN`, `AI_GATEWAY_API_KEY`). Never write tokens to files or commits.
- `config.json` is git-ignored; `config.example.json` is the template.
- Jira: use `/rest/api/3/search/jql` (the old `/search` endpoint is removed from Jira Cloud).
- Gateway: OpenAI-compatible, `https://ai-gateway.example.com/v1`, model the model (confirm id with `--list-models`).
- No remote is configured yet.
