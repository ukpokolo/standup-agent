# Standup generator: git commits + Jira tickets -> AI gateway -> standup message

## Context
Goal: one command that gathers your recent commits (several repos) and your Jira Cloud tickets,
sends them to a model on the company AI gateway, and prints a ready-to-paste standup
(Yesterday / Today / Blockers). The model never fetches anything itself; a small Python script collects
the data and passes it in as text, so it works with any gateway model, with or without tool-calling.

Decisions made: Python, Jira Cloud, OpenAI-compatible gateway (base URL from `config.json`, for example
`https://ai-gateway.example.com/v1`), several repos from a config list. Runs on Windows and on a colleague's Linux laptop.

## Location and setup (updated)
Jira auth now works (user confirmed), so the Jira integration is built in full; `--skip-jira` stays as a fallback flag.

New repo `standup-agent` in a local working folder.

Steps once approved:
1. Create the folder, run `git init` (local only; **no remote is created or pushed**, since creating a Bitbucket repo needs
   your workspace/credentials and is outward-facing. Say so if you want that added.)
2. Write the files below, make an initial commit.
3. Open it in VS Code: `code "C:\Users\you\Documents\repos\standup-agent"` (`code.cmd` found on this machine).
4. Chat transfer: a conversation can't be moved between sessions, so carry the context over as files in the repo:
   - `docs/design-notes.md`: decisions, Jira facts, gateway details (base URL and model from config), standup-time window, open items.
   - `docs/plan.md`: a copy of this plan.
   - `CLAUDE.md`: short project brief so a new Claude Code session opened in that folder starts with the context.
   - **Excluded on purpose:** the API token (it was pasted in chat and should be revoked/rotated).

    standup.py            # single-file script, only dependency: requests
    config.example.json   # copy to config.json (git-ignored) and fill in
    requirements.txt      # requests
    README.md             # setup + usage (Windows and Linux)
    .gitignore            # config.json, .env, __pycache__
    CLAUDE.md, docs/      # carried-over context (see above)

## Design (standup.py)
1. **Config + secrets**
   - `config.json`: `repos` (list of paths), `standup_time` (e.g. `"09:30"`, 24h local time),
     `jira.base_url` (https://<co>.atlassian.net), `gateway.base_url`,
     `gateway.model` (model id from config; confirmed via `--list-models`), optional `git.author`.
   - Secrets only from env vars: `JIRA_EMAIL`, `JIRA_API_TOKEN`, `AI_GATEWAY_API_KEY`. Never in the config or prompt.
2. **Git** (per repo, via `subprocess` `git`)
   - Author defaults to `git config user.email` in that repo.
   - Time window is driven by the standup time, not midnight: **start = previous standup** (`standup_time` on the
     previous working day; Monday goes back to Friday), **end = now**. Override with `--standup-time HH:MM`
     or an explicit `--since "YYYY-MM-DD HH:MM"`.
   - Commits: `git log --all --author=<email> --since=<window start> --pretty=%h|%ad|%s`.
   - Unpushed commits: `git log --branches --not --remotes --author=<email>` so "latest push" and local-only work are both covered.
3. **Jira Cloud** (REST v3, Basic auth email:token)
   - `GET /rest/api/3/search/jql` (the current endpoint; the old `/search` is removed) with two queries:
     in-progress = `assignee = currentUser() AND statusCategory != Done`, and recently-touched =
     `assignee = currentUser() AND updated >= "<window start>"` (same standup-based window as git).
   - Keep only key, summary, status, updated.
4. **Correlate**: regex ticket keys (`[A-Z]+-\d+`) out of commit messages/branch names so the prompt can link commits to tickets.
5. **Gateway call**: `POST {base_url}/chat/completions`, `Authorization: Bearer $AI_GATEWAY_API_KEY`,
   short system prompt ("write a concise standup: Yesterday / Today / Blockers, no invented work"), user message = collected data.
6. **CLI flags**: `--dry-run` (print collected context, no model call), `--since YYYY-MM-DD`,
   `--list-models` (GET `/models`, since the model name is not yet known), `--copy` (clipboard).
7. **Corporate network**: honour `HTTPS_PROXY`/`HTTP_PROXY` (requests does this by default) and add a
   `gateway.ca_bundle` option in case the gateway uses an internal CA. See the corp-proxy note in memory.

## Jira facts (from the Example Corp Jira API doc)
- Base URL `https://your-company.atlassian.net`, REST v3, Basic auth = Atlassian email + API token.
- The doc lists `GET /rest/api/3/search`, which Atlassian has removed from Jira Cloud; use `GET /rest/api/3/search/jql` (same JQL, `maxResults`, `fields`).
- JQL uses `assignee = currentUser()`, so no account ID is needed.
- Status at planning time: the user's token returned 401 on `/myself`; needs a fresh plain (unscoped) token. The script must
  fail clearly on 401/403 and support `--skip-jira` so git-only standups work while auth is being sorted.

## Scheduling
The script itself runs on demand. README documents running it automatically shortly before standup
(e.g. 10 min before `standup_time`): Windows Task Scheduler on your machine, `cron` on Linux.

## Open items to confirm at build time
- Exact gateway model id (find with `--list-models`).
- Whether Jira Cloud API tokens are allowed at your company (create at id.atlassian.com -> Security -> API tokens).
- Repo paths for the config list.

## Verification
1. `python standup.py --dry-run` shows real commits from each repo and your real Jira tickets, with correct dates and ticket-key matching.
2. `python standup.py --list-models` returns models from the gateway (proves URL + key + network).
3. `python standup.py` prints a standup that only mentions work present in the dry-run output.
4. Change `standup_time` (or pass `--standup-time`) and confirm the window start in the dry-run output moves accordingly; check a Monday case rolls back to Friday.
5. Repeat on a Linux shell (WSL is fine) to confirm the same script works there.
