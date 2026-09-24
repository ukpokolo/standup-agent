# standup-agent

Generates a standup message (Yesterday / Today / Blockers) from your recent git commits and your
Jira Cloud tickets, using a model on the company AI gateway.

A small script collects the data and passes it to the model as text, so it works with any
gateway model, with or without tool calling.

## Setup

1. Install Python 3.9+ (on Windows: https://www.python.org/downloads/, tick "Add to PATH").
2. Install the dependency:
   ```
   pip install -r requirements.txt
   ```
3. Copy `config.example.json` to `config.json` and edit it:
   - `repos`: paths of the repositories to read commits from
   - `standup_time`: your standup time, 24h `HH:MM`. The report covers everything since the previous
     working day's standup time (Monday rolls back to Friday).
   - `gateway.model`: run `python standup.py --list-models` and copy the exact the model id
4. Set secrets as environment variables (never put them in `config.json` or commit them).

   PowerShell:
   ```powershell
   $env:JIRA_EMAIL = "you@company.com"
   $env:JIRA_API_TOKEN = "<jira api token>"
   $env:AI_GATEWAY_API_KEY = "<gateway key>"
   ```
   Linux / macOS:
   ```bash
   export JIRA_EMAIL="you@company.com"
   export JIRA_API_TOKEN="<jira api token>"
   export AI_GATEWAY_API_KEY="<gateway key>"
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
python standup.py --board "Smart Trade Scrum Board"   # read tickets from one board (id or name)
python standup.py --board all            # tickets from every board
```

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
Ticket keys such as `CR-123` in commit messages or branch names are linked to Jira tickets.

## Running it automatically before standup

Windows (Task Scheduler), 10 minutes before a 09:30 standup:
```powershell
schtasks /Create /SC WEEKLY /D MON,TUE,WED,THU,FRI /TN "standup-agent" /ST 09:20 `
  /TR "python C:\path\to\standup-agent\standup.py --copy"
```
The task must run with the environment variables set (set them as user environment variables).

Linux (cron):
```
20 9 * * 1-5  cd ~/standup-agent && python3 standup.py >> standup.log 2>&1
```

## Corporate network

The script honours `HTTPS_PROXY` / `HTTP_PROXY`. If the gateway uses an internal certificate authority,
set `gateway.ca_bundle` in `config.json` to the path of the CA bundle file.
