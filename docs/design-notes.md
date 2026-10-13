# Design notes (carried over from the planning chat)

## Goal
One command that gathers my recent commits and Jira tickets and has a company-gateway model write a
standup (Yesterday / Today / Blockers). The model never fetches data itself; the script collects it and passes
it in as text.

## Decisions
- Language: Python (single script, `requests` only), runs on Windows and on a colleague's Linux laptop.
- Jira: Jira Cloud, `https://your-company.atlassian.net`, REST v3, Basic auth with Atlassian email + API token.
  The internal Jira API doc lists `GET /rest/api/3/search`, but that endpoint has been removed by Atlassian, so the
  script uses `GET /rest/api/3/search/jql`. JQL uses `assignee = currentUser()`, so no account ID is needed.
- Gateway: OpenAI-compatible API, base URL from `config.json` (for example `https://ai-gateway.example.com/v1`).
  Model id from config; confirm the exact id with `--list-models`.
- Repos: several, listed in `config.json`.
- Time window: driven by the standup time. Start = the previous working day's standup time (Monday goes back to
  Friday), end = now. Overridable with `--standup-time` or `--since`.
- Unpushed local commits are included and flagged, so "latest push" and local-only work are both covered.

## Jira token troubleshooting notes
- Use the plain "Create API token" button, not the scoped one.
- Site URL is the company site name, not the user's email address.
- 401 = credentials rejected; 403 = credentials fine but blocked by company policy.
- A token was pasted into the planning chat and should be revoked and replaced.

## Open items
- Confirm the model id and get an `AI_GATEWAY_API_KEY`.
- Python was not installed on the build machine, so the script has not been run yet. First run: `--list-models`,
  then `--dry-run`, then the real thing.
- No Bitbucket remote has been created; add one when wanted.
