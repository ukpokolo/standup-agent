# Plan: support more services (Slack, GitHub, ClickUp, ...)

Status: implemented. Existing teams/Jira/git behaviour is unchanged; every new service is opt-in
via config and env vars.

## Guiding idea

Separate the two jobs the script already does:

- **Sources** produce the context text: `collect_git`, `collect_jira`.
- **Sinks** receive the finished standup: `send_to_teams`.

Each optional service is one small adapter behind the existing shape, so nothing current changes
and a new service is added without touching the others. `resolve_teams` (standup.py:475) already
runs several configured teams, so multiple sources and multiple sinks can coexist per team.

## Config shape (back-compatible)

Current top-level keys and `teams[]` keep working. New optional keys are read only when present:

```jsonc
{
  "sources": ["git", "jira"],            // which context sources to run; default ["git","jira"]
  "outputs": ["teams"],                  // which sinks to post to; default ["teams"]

  "github":   { "repos": ["owner/name"], "issues": false },   // commits via API; issues as a source
  "clickup":  { "team": "workspace-id", "list": null },       // tasks as a source
  "slack":    { "webhook_env": "SLACK_WEBHOOK_URL" }          // sink

  // teams[] gains optional per-team overrides:
  // "github_repos": [...], "clickup_list": "...", "slack_webhook_env": "..."
}
```

If `sources`/`outputs` are absent, the script behaves exactly as today (git + Jira in, Teams out).

## New env vars (never in files)

- `GITHUB_TOKEN` — classic/PAT token with `repo` scope (you already have one via `gh`).
- `CLICKUP_API_TOKEN` — ClickUp personal token.
- `SLACK_WEBHOOK_URL` — Slack incoming webhook URL.

## 1. GitHub commits source (API, no clone)

- `collect_github(repos, author, since, until)`: `GET /repos/{owner}/{repo}/commits`
  with `since`, `until`, `author`, per repo, paginated. Map to the same row shape `collect_git`
  produces (`sha, date, subject, body, pushed=True, tickets[]`) so `render_context` is reused as-is.
- Auth header `Authorization: Bearer $GITHUB_TOKEN`; add to the `HTTP` session.
- Reuse `TICKET_RE` on subject+body to link commits to tickets.
- Setup: nothing beyond the token. Default stays local-clone git; GitHub API is used only when a
  repo is written as `owner/name` (heuristic) or listed under `github.repos`.

## 2. GitHub Issues source

- `collect_github_issues(...)`: `GET /search/issues?q=assignee:@me+repo:owner/name+updated:>=<since>`
  for open issues, plus issues closed in the window. Map to the Jira ticket shape
  (`key`=`#123`, `summary`, `status`, `updated`, `activity`) so `render_context` and the free-text
  prompt work unchanged.
- Activity (state changes / comments) via the issue timeline API, mirroring `fetch_activity`;
  keep it best-effort and cap like `MAX_TICKETS_WITH_ACTIVITY`.
- Prompt note: the model must not print Jira-style keys; the existing rule already says to use
  titles in plain words, so `#123` keys are handled by the same instruction.

## 3. ClickUp source

- `collect_clickup(...)`: `GET /api/v2/team/{team_id}/task` filtered by assignee and
  `date_updated_gt`, or by an explicit list id. Token via `Authorization: $CLICKUP_API_TOKEN`.
- Map tasks to the same ticket shape (key = task id short, summary = name, status, updated,
  comments as activity via `GET /task/{id}/comment`).
- Setup: a ClickUp personal API token; note the workspace/team id and optional list id.

## 4. Slack output

- `send_to_slack(text, url=None)`: near-copy of `send_to_teams` (standup.py:434) but the payload
  is simply `{"text": text}` to the incoming webhook. Same non-fatal error handling.
- URL from `slack.webhook_env` / team override, else `SLACK_WEBHOOK_URL`.
- Setup: create a Slack incoming webhook; paste the URL into the env var.

## 5. WhatsApp output (not in scope now, noted for later)

- No simple webhook. Requires Twilio or WhatsApp Business Cloud API (account, sender number,
  approved template). Larger setup, so deferred. Same sink interface would apply.

## Wiring changes in `main()`

1. Read `sources`/`outputs` (defaults = today) from config.
2. Build `git_sections` from whichever enabled sources apply (git local, github commits).
3. Build `jira` tickets from whichever ticket source is enabled (jira, github issues, clickup).
4. After `generate`, loop over enabled sinks (`send_to_teams`, `send_to_slack`, ...).

Each adapter keeps the existing failure style: report, never fatal.

## Docs / config updates

- `config.example.json`: add commented `sources`, `outputs`, `github`, `clickup`, `slack` blocks.
- `README.md`: a short per-service setup section (token, env var, config keys).
- `CLAUDE.md`: add the new env var names to the secrets list.

## Verification

1. With no new config, `--dry-run` output is byte-identical to today (regression check).
2. `sources:["github"]` + `GITHUB_TOKEN`: `--dry-run` shows commits from a GitHub repo with no local clone.
3. `sources:["github-issues"]` / `["clickup"]`: `--dry-run` shows your issues/tasks in the ticket section.
4. `outputs:["slack"]` + `SLACK_WEBHOOK_URL`: standup posts to a test Slack channel; a bad URL prints a
   non-fatal message and the standup still prints.
5. Both `teams` and `slack` outputs at once: both receive the same text.

## Open questions

- GitHub commits: rely on an `owner/name` heuristic to pick the API path, or an explicit `github.repos` list only?
- GitHub Issues and ClickUp: should they replace Jira or merge with it for one team? (Plan assumes both
  can be on; merging means de-duping by title.)
- WhatsApp: worth the Twilio/Business setup, or is Slack enough?