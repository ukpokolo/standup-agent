# standup-agent

Generates a standup message (Yesterday / Today / Blockers) from your recent git commits and your
Jira Cloud tickets, using an OpenAI-compatible chat model.

A small script collects the data and passes it to the model as text, so it works with any
gateway model, with or without tool calling.

## Setup

1. Install Python 3.9+ (on Windows: https://www.python.org/downloads/, tick "Add to PATH").
2. Install the dependency:
   ```
   pip install -r requirements.txt
   ```
3. Copy `config.example.json` to `config.json` and edit it:
   - `teams`: one entry per team you work on (see "Working on several teams" below). Each entry has
     `name`, `repos` (paths of the repositories to read commits from), `board` (Jira board name or id)
     and an optional `webhook_env` (the env var holding that team's Teams webhook).
   - `standup_time`: your standup time, 24h `HH:MM`. The report covers everything since the previous
     working day's standup time (Monday rolls back to Friday).
   - `gateway.model`: run `python standup.py --list-models` and copy the exact model id to use

   A single-team config can skip `teams` and use top-level `repos` + `jira.board` instead; that still works.
4. Set secrets as environment variables (never put them in `config.json` or commit them).

   PowerShell:
   ```powershell
   $env:JIRA_EMAIL = "you@company.com"
   $env:JIRA_API_TOKEN = "<jira api token>"
   $env:AI_GATEWAY_API_KEY = "<gateway key>"
   $env:TEAMS_WEBHOOK_URL = "<teams workflow webhook url>"   # optional, see below
   ```
   Linux / macOS:
   ```bash
   export JIRA_EMAIL="you@company.com"
   export JIRA_API_TOKEN="<jira api token>"
   export AI_GATEWAY_API_KEY="<gateway key>"
   export TEAMS_WEBHOOK_URL="<teams workflow webhook url>"   # optional, see below
   ```
   Create the Jira token at https://id.atlassian.com/manage-profile/security/api-tokens
   (use the plain "Create API token" button, not the scoped one).

## Usage

```
python standup.py --list-models          # find the exact model id on the gateway
python standup.py --dry-run              # show collected commits/tickets, no model call
python standup.py                        # print the standup
python standup.py --copy                 # also copy it to the clipboard
python standup.py --standup-time 10:00   # override the standup time
python standup.py --since "2026-09-22 09:30"   # explicit window start
python standup.py --skip-jira            # git only
python standup.py --list-boards          # boards you can see: id, type, name, project
python standup.py --board "My Scrum Board"    # read tickets from one board (id or name)
python standup.py --board all            # tickets from every board
python standup.py --team "Alpha"         # run only one configured team
python standup.py --all-teams            # run every configured team (the default)
```

### Working on several teams

If you work on more than one team, list each one under `teams` in `config.json`. Every team has its own
repos, its own Jira board and (optionally) its own Teams webhook, and gets its own standup:

```json
"teams": [
  { "name": "Alpha", "board": "Alpha Scrum Board",
    "repos": ["C:/work/alpha-ui"], "webhook_env": "TEAMS_WEBHOOK_URL_ALPHA" },
  { "name": "Beta", "board": "Beta Scrum Board",
    "repos": ["C:/work/beta-api"], "webhook_env": "TEAMS_WEBHOOK_URL_BETA" }
]
```

- `python standup.py` runs **every** team and prints one standup per team.
- `python standup.py --team "Beta"` runs just that team.
- Each team posts to the webhook named by its `webhook_env` (falling back to `TEAMS_WEBHOOK_URL` if unset),
  so different teams can deliver to different chats/channels.
- `--board` overrides the board for every team in that run.

### Choosing the Jira board

Set `jira.board` in `config.json` to a board name or id; it is the default for every run, and `--board` overrides
it for a single run (`--board all` removes the restriction). Only tickets on that board are read. Names are
matched exactly first, then by substring; if several boards match (for example three boards are named
"QR Service") the tool lists them and asks for an id. Leave `board` out to read all boards.
The board does not affect which git repos are read; those come from `repos`.

For each ticket the tool also reads status changes and comments made inside the window, by you or by anyone
else (for example QA sending a ticket back with a comment late in the day). Tickets are written in the standup
by title, never by ID. Use `--until "YYYY-MM-DD HH:MM"` to replay a past standup.

Commits not yet pushed are marked `[NOT PUSHED]` in the data given to the model.
Ticket keys such as `ABC-123` in commit messages or branch names are linked to Jira tickets.
Both the commit subject and its body are read, so detail written in the body is used too.

## Posting to Teams

If `TEAMS_WEBHOOK_URL` is set, the standup is also posted to Teams as an Adaptive Card, in
addition to being printed. There is no flag: the env var turns it on.

### To a channel (free, simplest)

Use the classic **Incoming Webhook** connector, which accepts the Adaptive Card payload directly:

1. In Teams, open the target channel, click **...** > **Connectors** (or **Manage team** > **Apps** > **Connectors**).
2. Find **Incoming Webhook** > **Configure**, give it a name, and **Create**.
3. Copy the URL (looks like `https://<tenant>.webhook.office.com/webhookb2/...`) and set it as
   `TEAMS_WEBHOOK_URL` (see Setup above).
4. Run `python standup.py` — the card appears in the channel.

### To a chat (free, via Workflows)

1. In Teams, open the **Workflows** app > **Create** > **Start from blank**.
2. Trigger: **When a Teams webhook request is received**.
3. Action: **Post card in a chat or channel** (or **Post message in a chat or channel**):
   - **Post as:** `Flow bot`
   - **Post in:** `Chat with Flow bot` (recipient = your email) or `Group chat`
   - **Adaptive Card:** the trigger's **Adaptive Card** output
4. **Save**, then copy the trigger's **HTTP POST URL** (use the copy icon; it must end with
   `&sp=...&sv=...&sig=...`). Set it as `TEAMS_WEBHOOK_URL`.

Note: the generic **HTTP Request** trigger is a premium connector; the Teams webhook trigger above is not.

### Testing the webhook

```
python standup.py --test-teams           # post a fixed test card and exit
```

Notes:
- `--dry-run`, `--list-models` and `--list-boards` never post.
- If the post fails (bad URL, network), a warning is printed but the standup is still printed and
  the script exits normally.
- The webhook URL contains a signature and is a credential: keep it in the env var only, never in
  `config.json` or a commit.

## Running it automatically before standup

Windows (Task Scheduler), 10 minutes before a 09:30 standup:
```powershell
schtasks /Create /SC WEEKLY /D MON,TUE,WED,THU,FRI /TN "standup-agent" /ST 09:20 `
  /TR "python C:\path\to\standup-agent\standup.py --copy"
```
The task must run with the environment variables set (set them as user environment variables).
If `TEAMS_WEBHOOK_URL` is among them, the scheduled run posts the standup to Teams automatically.

Linux (cron):
```
20 9 * * 1-5  cd ~/standup-agent && python3 standup.py >> standup.log 2>&1
```
With `TEAMS_WEBHOOK_URL` exported, the standup is delivered to Teams on each run.

## Corporate network

The script honours `HTTPS_PROXY` / `HTTP_PROXY`. If the gateway uses an internal certificate authority,
set `gateway.ca_bundle` in `config.json` to the path of the CA bundle file.
