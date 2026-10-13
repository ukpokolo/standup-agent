#!/usr/bin/env python3
"""Generate a standup message from recent git commits and Jira Cloud tickets
using a model on the company AI gateway (OpenAI-compatible API).

Secrets come from environment variables only:
    JIRA_EMAIL, JIRA_API_TOKEN, AI_GATEWAY_API_KEY, TEAMS_WEBHOOK_URL

If TEAMS_WEBHOOK_URL is set, the generated standup is also posted to a Microsoft
Teams channel via a Power Automate workflow webhook (Adaptive Card).

Several teams can be configured (see config.example.json): each team has its own
repos, Jira board and optional Teams webhook, and gets its own standup.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).resolve().parent

# One session with retries: corporate networks occasionally drop a connection or fail an SSL handshake.
HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=None,  # also retry POST (the gateway call is idempotent for our purposes)
)))
HTTP.mount("http://", HTTP.adapters["https://"])
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")

# Which context sources run and which sinks receive the standup, when config.json
# says nothing. These match the original behaviour, so existing setups are unaffected.
DEFAULT_SOURCES = ("git", "jira")
DEFAULT_OUTPUTS = ("teams",)

SYSTEM_PROMPT = (
    "You write a daily standup update that a software engineer will read aloud in a standup meeting. "
    "Write in the first person ('I'), in plain, natural, spoken English, as if the engineer is talking to "
    "their team. Keep it short and easy to say out loud: no jargon, no corporate filler, no long sentences. "
    "Use exactly three sections, each as a heading on its own line: Yesterday, Today, Blockers. "
    "Under each heading use short bullet points, at most 5 per section, each one a single natural sentence "
    "that starts with a verb (for example 'Finished ...', 'Started ...', 'Fixed ...'). "
    "Only mention work that appears in the data provided; never invent work. "
    "Refer to tickets (Jira issues, GitHub issues, ClickUp tasks) by their title, written in plain readable words, "
    "and never output IDs or keys (like ABC-123 or owner/repo#12): readers find IDs meaningless. Use the keys in "
    "the data only to match commits to their ticket title. If a commit mentions a key with no known title, "
    "describe the work instead. "
    "YESTERDAY = what I did in the report window: commits, tickets I moved between statuses, tickets closed, "
    "and comments I made. Also include feedback I received: if someone else (for example QA) commented on a "
    "ticket or moved it back to development in the window, say that feedback was received on that ticket and "
    "summarise it, even if it happened late in the day. "
    "TODAY = what I will work on now: tickets currently in progress or in development, and any ticket sent back "
    "with feedback (state what needs fixing, taken from the comment). Do not list tickets that are Ready for QA, "
    "in review, or closed as work to do, unless feedback sent them back. Put tickets with no activity in the "
    "window into one final bullet 'Also in progress: ...' instead of listing each separately. "
    "BLOCKERS = only things the data shows are blocking work (for example a comment saying blocked or waiting on "
    "someone). If there are none, write 'None'. "
    "Do not add any preamble, sign-off, or extra commentary: output only the three sections."
)


def load_config(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"Config not found: {path}\nCopy config.example.json to config.json and fill it in.")
    with open(path, encoding="utf-8-sig") as f:  # tolerate a BOM from Windows editors
        return json.load(f)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Environment variable {name} is not set.")
    return value


def parse_hhmm(text):
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError:
        sys.exit(f"Invalid time '{text}', expected HH:MM (24h).")


def previous_working_day(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:  # Sat/Sun
        day -= timedelta(days=1)
    return day


def ceil_to_minute(dt):
    """Round up to the next whole minute.

    The window end is 'now', which has seconds; git and Jira compare at minute
    precision, so a commit or update made in the current minute would otherwise
    fall just outside the window. Rounding up includes the whole current minute.
    """
    if dt.second or dt.microsecond:
        return dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return dt


def window_start(standup_time, since_arg, now):
    """Start of the report window = the previous standup."""
    if since_arg:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(since_arg, fmt)
            except ValueError:
                continue
        sys.exit(f"Invalid --since '{since_arg}', expected 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.")
    # Previous working day's standup (Monday rolls back to Friday), so the report
    # covers everything since the last standup.
    return datetime.combine(previous_working_day(now.date()), standup_time)


def run_git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout, None


def collect_git(repos, author_override, since, until):
    since_str = since.strftime("%Y-%m-%d %H:%M")
    until_str = ceil_to_minute(until).strftime("%Y-%m-%d %H:%M")
    # \x1f separates fields and \x1e separates commits, so multi-line bodies are safe.
    fmt = "%h%x1f%ad%x1f%D%x1f%s%x1f%b%x1e"
    sections = []
    for repo in repos:
        repo = Path(repo).expanduser()
        name = repo.name
        if not (repo / ".git").exists():
            sections.append({"repo": name, "error": f"not a git repository: {repo}"})
            continue
        author = author_override
        if not author:
            author, _ = run_git(repo, "config", "user.email")
            author = (author or "").strip()
        commits, err = run_git(
            repo, "log", "--all", f"--author={author}", f"--since={since_str}", f"--until={until_str}",
            "--date=format:%Y-%m-%d %H:%M", f"--pretty=format:{fmt}",
        )
        if commits is None:
            sections.append({"repo": name, "error": err})
            continue
        unpushed, _ = run_git(
            repo, "log", "--branches", "--not", "--remotes", f"--author={author}",
            f"--since={since_str}", "--pretty=format:%h",
        )
        unpushed_set = set((unpushed or "").split())
        rows = []
        for record in commits.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            parts = record.split("\x1f")
            if len(parts) < 5:
                continue
            sha, date, refs, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4]
            body = " ".join(body.split())  # collapse newlines/indentation into one line
            rows.append({
                "sha": sha, "date": date, "subject": subject, "body": body,
                "pushed": sha not in unpushed_set,
                "tickets": sorted(set(TICKET_RE.findall(subject + " " + body + " " + refs))),
            })
        sections.append({"repo": name, "commits": rows})
    return sections


GITHUB_API = "https://api.github.com"


def github_headers():
    token = require_env("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_get(path, params=None):
    resp = HTTP.get(GITHUB_API + path, params=params, headers=github_headers(), timeout=30)
    if resp.status_code == 401:
        sys.exit("GitHub rejected GITHUB_TOKEN (HTTP 401). Check the token has not expired.")
    resp.raise_for_status()
    return resp


def github_login():
    """The authenticated user's login, used to filter commits when no author is configured."""
    return github_get("/user").json().get("login")


def collect_github(repos, author_override, since, until):
    """Commits straight from the GitHub API, so no local clone is needed.

    Produces the same section/row shape as collect_git, so render_context is shared.
    """
    author = author_override or github_login()
    since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_str = ceil_to_minute(until).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = []
    for slug in repos:
        slug = str(slug).strip()
        ref = None
        if "@" in slug:
            slug, ref = slug.split("@", 1)  # owner/name@branch
        name = slug.split("/")[-1]
        rows = []
        url = f"/repos/{slug}/commits"
        params = {"since": since_str, "until": until_str, "author": author, "per_page": 100}
        if ref:
            params["sha"] = ref
        try:
            while url:
                resp = github_get(url, params)
                for item in resp.json():
                    commit = item.get("commit") or {}
                    message = commit.get("message", "")
                    subject, _, body = message.partition("\n")
                    rows.append({
                        "sha": (item.get("sha") or "")[:7],
                        "date": (commit.get("author") or {}).get("date", "")[:16].replace("T", " "),
                        "subject": subject,
                        "body": " ".join(body.split()),
                        "pushed": True,
                        "tickets": sorted(set(TICKET_RE.findall(subject + " " + body))),
                    })
                url = resp.links.get("next", {}).get("url")
                params = None  # the next link already carries the query string
        except requests.RequestException as exc:
            sections.append({"repo": f"github:{slug}", "error": str(exc)})
            continue
        sections.append({"repo": f"github:{name}", "commits": rows})
    return sections


def parse_github_time(text):
    """GitHub timestamps are ISO 8601 UTC; convert to naive local time like Jira."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def github_search_issues(query):
    items, url = [], "/search/issues"
    params = {"q": query, "per_page": 100}
    while url:
        resp = github_get(url, params)
        items += resp.json().get("items", [])
        url = resp.links.get("next", {}).get("url")
        params = None
    return items


def github_activity(slug, number, login, since, until):
    """Comments and state changes on one issue inside the window, via the timeline API."""
    events = []
    timeline = github_get(f"/repos/{slug}/issues/{number}/timeline", {"per_page": 100}).json()
    for ev in timeline:
        when = parse_github_time(ev.get("created_at") or ev.get("submitted_at") or "")
        if not (since <= when <= until):
            continue
        kind = ev.get("event")
        if kind == "commented":
            text = " ".join((ev.get("body") or "").split())
            who = (ev.get("actor") or ev.get("user") or {}).get("login") or "someone"
            events.append({"when": when, "kind": "comment", "by": "me" if who == login else who,
                           "text": text[:500] + ("..." if len(text) > 500 else "")})
        elif kind in ("closed", "reopened"):
            who = (ev.get("actor") or {}).get("login") or "someone"
            events.append({"when": when, "kind": "status", "by": "me" if who == login else who,
                           "text": f"-> {kind}"})
    return sorted(events, key=lambda e: e["when"])


def collect_github_issues(repos, since, until):
    """Issues assigned to me, in the same shape collect_jira returns, so rendering is shared."""
    login = github_login()
    since_date = since.date().isoformat()
    tickets = {}
    for slug in repos:
        slug = str(slug).split("@", 1)[0].strip()  # issues are branch-independent
        open_items = github_search_issues(f"is:open assignee:{login} repo:{slug}")
        touched = github_search_issues(f"assignee:{login} repo:{slug} updated:>={since_date}")
        for item in open_items + touched:
            number = item["number"]
            key = f"{slug}#{number}"
            tickets.setdefault(key, {
                "key": key,
                "summary": item.get("title", ""),
                "status": "open" if (item.get("state") == "open") else "closed",
                "updated": parse_github_time(item["updated_at"]).strftime("%Y-%m-%d %H:%M"),
                "open": item.get("state") == "open",
                "_slug": slug,
                "_number": number,
            })
    for ticket in list(tickets.values())[:MAX_TICKETS_WITH_ACTIVITY]:
        ticket["activity"] = github_activity(ticket["_slug"], ticket["_number"], login, since, until)
    for ticket in tickets.values():
        ticket.pop("_slug", None)
        ticket.pop("_number", None)
    return None, list(tickets.values())


def jira_get(cfg, email, token, path, params=None):
    url = cfg["jira"]["base_url"].rstrip("/") + path
    resp = HTTP.get(
        url, params=params, auth=(email, token), headers={"Accept": "application/json"}, timeout=30,
    )
    if resp.status_code in (401, 403):
        sys.exit(f"Jira rejected the credentials (HTTP {resp.status_code}). Check JIRA_EMAIL / JIRA_API_TOKEN.")
    resp.raise_for_status()
    return resp.json()


def parse_jira_time(text):
    """Jira timestamps carry an offset; convert to naive local time to compare with the report window."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone().replace(tzinfo=None)


def adf_to_text(node):
    """Flatten Atlassian Document Format (used for comment bodies) to plain text."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "mention":
        return (node.get("attrs") or {}).get("text", "@user")
    if kind == "hardBreak":
        return "\n"
    inner = "".join(adf_to_text(child) for child in node.get("content") or [])
    if kind in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        return inner + "\n"
    return inner


def person(user, me_id):
    user = user or {}
    if user.get("accountId") and user.get("accountId") == me_id:
        return "me"
    return user.get("displayName") or "someone"


def fetch_activity(cfg, email, token, key, me_id, since, until):
    """Status changes and comments on one ticket inside the report window."""
    events = []
    start = 0
    while True:
        data = jira_get(cfg, email, token, f"/rest/api/3/issue/{key}/changelog",
                        {"startAt": start, "maxResults": 100})
        values = data.get("values", [])
        for entry in values:
            when = parse_jira_time(entry["created"])
            if not (since <= when <= until):
                continue
            for item in entry.get("items", []):
                if item.get("field") == "status":
                    events.append({
                        "when": when, "kind": "status", "by": person(entry.get("author"), me_id),
                        "text": f"{item.get('fromString')} -> {item.get('toString')}",
                    })
        if data.get("isLast", True) or not values:
            break
        start += len(values)
    comments = jira_get(cfg, email, token, f"/rest/api/3/issue/{key}/comment",
                        {"orderBy": "-created", "maxResults": 50}).get("comments", [])
    for c in comments:
        when = parse_jira_time(c["created"])
        if since <= when <= until:
            text = " ".join(adf_to_text(c.get("body")).split())
            events.append({
                "when": when, "kind": "comment", "by": person(c.get("author"), me_id),
                "text": text[:500] + ("..." if len(text) > 500 else ""),
            })
    return sorted(events, key=lambda e: e["when"])


def list_boards(cfg, email, token, name=None):
    boards, start = [], 0
    while True:
        params = {"startAt": start, "maxResults": 50}
        if name:
            params["name"] = name
        data = jira_get(cfg, email, token, "/rest/agile/1.0/board", params)
        values = data.get("values", [])
        boards += values
        if data.get("isLast", True) or not values:
            return boards
        start += len(values)


def resolve_board(cfg, email, token, wanted):
    """Turn a board id or (part of) a board name into a board dict. None/'all' means every board."""
    if wanted is None or str(wanted).strip().lower() in ("", "all", "none"):
        return None
    wanted = str(wanted).strip()
    if wanted.isdigit():
        boards = [b for b in list_boards(cfg, email, token) if str(b["id"]) == wanted]
    else:
        boards = list_boards(cfg, email, token, name=wanted)
        exact = [b for b in boards if b["name"].lower() == wanted.lower()]
        boards = exact or boards
    if not boards:
        sys.exit(f"No Jira board matches '{wanted}'. Run: python standup.py --list-boards")
    if len(boards) > 1:
        options = "\n".join(f"  {b['id']}  {b['name']}" for b in boards)
        sys.exit(f"'{wanted}' matches several boards, use the id or a longer name:\n{options}")
    return boards[0]


def jira_search(cfg, email, token, jql, max_results=50, board_id=None):
    # With a board selected, use the Agile API so only issues on that board are returned.
    path = f"/rest/agile/1.0/board/{board_id}/issue" if board_id else "/rest/api/3/search/jql"
    data = jira_get(cfg, email, token, path,
                    {"jql": jql, "maxResults": max_results, "fields": "summary,status,updated,issuetype"})
    issues = []
    for issue in data.get("issues", []):
        f = issue["fields"]
        issues.append({
            "key": issue["key"],
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "updated": (f.get("updated") or "")[:16].replace("T", " "),
        })
    return issues


def verify_jira_auth(cfg, email, token):
    """Search silently returns zero issues for unauthenticated (anonymous) calls,
    so confirm the credentials are real before trusting an empty result."""
    url = cfg["jira"]["base_url"].rstrip("/") + "/rest/api/3/myself"
    resp = HTTP.get(url, auth=(email, token), headers={"Accept": "application/json"}, timeout=30)
    if resp.status_code != 200:
        sys.exit(
            f"Jira login failed (HTTP {resp.status_code}) for {email}. "
            "The API token is invalid, revoked, or scoped. Create a new plain token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens and update JIRA_API_TOKEN."
        )
    return resp.json().get("accountId")


MAX_TICKETS_WITH_ACTIVITY = 40


def collect_jira(cfg, since, until, board_setting):
    """Returns (board name or None, tickets). board_setting: id, name or 'all'."""
    email = require_env("JIRA_EMAIL")
    token = require_env("JIRA_API_TOKEN")
    me_id = verify_jira_auth(cfg, email, token)
    board = resolve_board(cfg, email, token, board_setting)
    board_id = board["id"] if board else None
    since_str = since.strftime("%Y-%m-%d %H:%M")
    until_str = ceil_to_minute(until).strftime("%Y-%m-%d %H:%M")
    open_issues = jira_search(
        cfg, email, token,
        "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
        board_id=board_id,
    )
    # Comments by others (e.g. QA) also bump "updated", so feedback received in the window shows up here.
    # "assignee was" also catches tickets reassigned to QA while I am waiting on their feedback.
    touched = jira_search(
        cfg, email, token,
        f'(assignee = currentUser() OR assignee was currentUser()) '
        f'AND updated >= "{since_str}" AND updated <= "{until_str}" ORDER BY updated DESC',
        board_id=board_id,
    )
    tickets = {}
    for issue in open_issues:
        tickets[issue["key"]] = {**issue, "open": True}
    for issue in touched:
        tickets.setdefault(issue["key"], {**issue, "open": False})
    for ticket in list(tickets.values())[:MAX_TICKETS_WITH_ACTIVITY]:
        ticket["activity"] = fetch_activity(cfg, email, token, ticket["key"], me_id, since, until)
    return (board["name"] if board else None), list(tickets.values())


def render_context(since, now, git_sections, jira, board_name=None, team_name=None,
                   board_label="Jira", tickets_name="Jira", tickets_heading="JIRA TICKETS ASSIGNED TO ME"):
    lines = []
    if team_name:
        lines.append(f"Team: {team_name}")
    lines.append(f"Report window: {since:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M}")
    if board_label:
        lines.append(f"{board_label} board: {board_name}" if board_name else f"{board_label} board: all boards")
    lines += ["", "== GIT COMMITS =="]
    for sec in git_sections:
        lines.append(f"[{sec['repo']}]")
        if "error" in sec:
            lines.append(f"  (skipped: {sec['error']})")
            continue
        if not sec["commits"]:
            lines.append("  (no commits in window)")
        for c in sec["commits"]:
            tag = "" if c["pushed"] else " [NOT PUSHED]"
            tickets = f" ({', '.join(c['tickets'])})" if c["tickets"] else ""
            lines.append(f"  {c['date']} {c['sha']} {c['subject']}{tickets}{tag}")
            if c.get("body"):
                lines.append(f"      details: {c['body'][:300]}")
    lines += ["", f"== {tickets_heading} =="]
    if jira is None:
        lines.append(f"({tickets_name} skipped)")
    elif not jira:
        lines.append("(none)")
    else:
        for t in jira:
            state = "not done" if t["open"] else "done/closed"
            lines.append(f"{t['key']} | \"{t['summary']}\" | current status: {t['status']} ({state}) | last updated {t['updated']}")
            activity = t.get("activity")
            if activity is None:
                lines.append("    (activity not checked)")
            elif not activity:
                lines.append("    no activity in the window")
            for e in activity or []:
                label = f"status changed {e['text']}" if e["kind"] == "status" else f"comment: {e['text']}"
                lines.append(f"    {e['when']:%Y-%m-%d %H:%M} by {e['by']} - {label}")
    return "\n".join(lines)


def gateway_session(cfg):
    key = require_env("AI_GATEWAY_API_KEY")
    gw = cfg["gateway"]
    verify = gw.get("ca_bundle") or True
    return gw["base_url"].rstrip("/"), {"Authorization": f"Bearer {key}"}, verify


def list_models(cfg):
    base, headers, verify = gateway_session(cfg)
    resp = HTTP.get(base + "/models", headers=headers, timeout=30, verify=verify)
    resp.raise_for_status()
    for m in resp.json().get("data", []):
        print(m.get("id"))


def generate(cfg, context):
    base, headers, verify = gateway_session(cfg)
    payload = {
        "model": cfg["gateway"]["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        "temperature": 0.3,
    }
    resp = HTTP.post(base + "/chat/completions", headers=headers, json=payload, timeout=120, verify=verify)
    if resp.status_code >= 400:
        sys.exit(f"Gateway error HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


def copy_to_clipboard(text):
    try:
        if sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
        print("\n(copied to clipboard)")
    except (OSError, subprocess.CalledProcessError):
        print("\n(could not copy to clipboard)")


def send_to_teams(text, url=None):
    """Post the standup to a Teams channel via a Power Automate workflow webhook.

    Enabled by setting TEAMS_WEBHOOK_URL (or a per-team webhook env var). Failures
    are reported but never fatal, so a webhook problem cannot lose the standup that
    was already printed.
    """
    if url is None:
        url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        return
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "text": text, "wrap": True}],
            },
        }],
    }
    try:
        resp = HTTP.post(url, json=payload, timeout=30)
    except requests.RequestException as exc:
        print(f"\n(could not post to Teams: {exc})")
        return
    if resp.status_code >= 400:
        print(f"\n(could not post to Teams: HTTP {resp.status_code}: {resp.text[:300]})")
        return
    print("\n(posted to Teams)")


def test_teams():
    """Post a fixed hello card to TEAMS_WEBHOOK_URL to validate the webhook."""
    if not os.environ.get("TEAMS_WEBHOOK_URL"):
        sys.exit("TEAMS_WEBHOOK_URL is not set.")
    send_to_teams("standup-agent test\n\nIf you can read this, the webhook works.")


def test_slack():
    """Post a fixed hello message to SLACK_WEBHOOK_URL to validate the webhook."""
    if not os.environ.get("SLACK_WEBHOOK_URL"):
        sys.exit("SLACK_WEBHOOK_URL is not set.")
    send_to_slack("standup-agent test\n\nIf you can read this, the webhook works.")


def send_to_slack(text, url=None):
    """Post the standup to a Slack channel via an incoming webhook.

    Enabled by setting SLACK_WEBHOOK_URL (or a per-team webhook env var). As with
    Teams, failures are reported but never fatal, so a webhook problem cannot lose
    the standup that was already printed.
    """
    if url is None:
        url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        resp = HTTP.post(url, json={"text": text}, timeout=30)
    except requests.RequestException as exc:
        print(f"\n(could not post to Slack: {exc})")
        return
    if resp.status_code >= 400:
        print(f"\n(could not post to Slack: HTTP {resp.status_code}: {resp.text[:300]})")
        return
    print("\n(posted to Slack)")


def post_standup(cfg, team, text):
    """Send the finished standup to every enabled sink.

    Sinks are independent: a failure in one is reported but never stops another,
    so a broken webhook cannot lose the standup that was already printed.
    """
    for name in enabled_outputs(cfg):
        if name == "teams":
            send_to_teams(text, team_webhook_url(team))
        elif name == "slack":
            send_to_slack(text, team_slack_url(team))
        else:
            print(f"\n(unknown output '{name}' skipped)")


def resolve_teams(cfg, args):
    """The teams to run, honouring --team / --all-teams.

    A team is {name, repos, board, webhook_env}. With no 'teams' in config, one
    implicit team is built from the top-level repos + jira.board (back-compat).
    """
    if args.team and args.all_teams:
        sys.exit("Use either --team or --all-teams, not both.")
    raw = cfg.get("teams")
    if not raw:
        raw = [{"name": None, "repos": cfg.get("repos", []), "board": cfg["jira"].get("board")}]
    teams = [{
        "name": t.get("name"),
        "repos": t.get("repos", cfg.get("repos", [])),
        "board": t.get("board", cfg["jira"].get("board")),
        "webhook_env": t.get("webhook_env"),
        "slack_webhook_env": t.get("slack_webhook_env"),
        "github_repos": t.get("github_repos", (cfg.get("github") or {}).get("repos", [])),
    } for t in raw]
    if args.team:
        wanted = args.team.strip().lower()
        matches = [t for t in teams if (t["name"] or "").lower() == wanted]
        if not matches:
            names = ", ".join(t["name"] or "(unnamed)" for t in teams)
            sys.exit(f"No team named '{args.team}'. Configured teams: {names}")
        return matches
    return teams


def team_webhook_url(team):
    return os.environ.get(team.get("webhook_env") or "TEAMS_WEBHOOK_URL")


def team_slack_url(team):
    return os.environ.get(team.get("slack_webhook_env") or "SLACK_WEBHOOK_URL")


def splits(text, default):
    """A comma/space separated list from config, or the default when absent.

    Keeping the default means existing configs (which set neither) behave exactly
    as before: git + Jira in, Teams out.
    """
    if text is None:
        return list(default)
    if isinstance(text, (list, tuple)):
        return [str(x).strip() for x in text if str(x).strip()]
    return [p for p in re.split(r"[,\s]+", str(text).strip()) if p]


def enabled_sources(cfg):
    return splits(cfg.get("sources"), DEFAULT_SOURCES)


def enabled_outputs(cfg):
    return splits(cfg.get("outputs"), DEFAULT_OUTPUTS)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--standup-time", help="HH:MM, overrides standup_time from config")
    ap.add_argument("--since", help="explicit window start: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--until", help="window end 'YYYY-MM-DD HH:MM' (default: now); use to replay a past standup")
    ap.add_argument("--dry-run", action="store_true", help="print collected data, do not call the model")
    ap.add_argument("--skip-jira", action="store_true", help="git only")
    ap.add_argument("--board", help="Jira board id or name to read tickets from; 'all' for every board "
                                    "(default: jira.board from config)")
    ap.add_argument("--team", help="run only the named team from config 'teams'")
    ap.add_argument("--all-teams", action="store_true", help="run every configured team (the default)")
    ap.add_argument("--list-boards", action="store_true", help="list the Jira boards you can see and exit")
    ap.add_argument("--list-models", action="store_true", help="list models on the gateway and exit")
    ap.add_argument("--copy", action="store_true", help="copy the result to the clipboard")
    ap.add_argument("--test-teams", action="store_true", help="post a test card to TEAMS_WEBHOOK_URL and exit")
    ap.add_argument("--test-slack", action="store_true", help="post a test message to SLACK_WEBHOOK_URL and exit")
    args = ap.parse_args()

    if args.test_teams:
        test_teams()
        return

    if args.test_slack:
        test_slack()
        return

    cfg = load_config(args.config)

    if args.list_models:
        list_models(cfg)
        return

    if args.list_boards:
        email, token = require_env("JIRA_EMAIL"), require_env("JIRA_API_TOKEN")
        verify_jira_auth(cfg, email, token)
        for b in list_boards(cfg, email, token):
            project = (b.get("location") or {}).get("projectName", "")
            print(f"{b['id']}\t{b['type']}\t{b['name']}\t{project}")
        return

    try:
        now = datetime.strptime(args.until, "%Y-%m-%d %H:%M") if args.until else datetime.now()
    except ValueError:
        sys.exit(f"Invalid --until '{args.until}', expected 'YYYY-MM-DD HH:MM'.")
    standup_time = parse_hhmm(args.standup_time or cfg.get("standup_time", "09:30"))
    since = window_start(standup_time, args.since, now)

    teams = resolve_teams(cfg, args)
    if args.board is not None:
        for team in teams:
            team["board"] = args.board
    author = (cfg.get("git") or {}).get("author")
    sources = enabled_sources(cfg)
    multi = len(teams) > 1

    for team in teams:
        if multi and team["name"]:
            print(f"===== {team['name']} =====")
        git_sections = collect_git(team["repos"], author, since, now) if "git" in sources else []
        if "github" in sources:
            git_sections += collect_github(team["github_repos"], author, since, now)
        want_jira = "jira" in sources and not args.skip_jira
        want_gh_issues = "github-issues" in sources
        if want_jira:
            board_name, tickets = collect_jira(cfg, since, now, team["board"])
            tickets_label, board_label, tickets_heading = "Jira", "Jira", "JIRA TICKETS ASSIGNED TO ME"
        elif want_gh_issues:
            board_name, tickets = collect_github_issues(team["github_repos"], since, now)
            tickets_label, board_label, tickets_heading = "GitHub issues", None, "GITHUB ISSUES ASSIGNED TO ME"
        else:
            board_name, tickets = None, None
            tickets_label, board_label, tickets_heading = "Jira", "Jira", "JIRA TICKETS ASSIGNED TO ME"
        context = render_context(
            since, now, git_sections, tickets, board_name, team["name"],
            board_label=board_label, tickets_name=tickets_label, tickets_heading=tickets_heading,
        )

        if args.dry_run:
            print(context)
            continue

        standup = generate(cfg, context)
        print(standup)
        if args.copy:
            copy_to_clipboard(standup)
        post_standup(cfg, team, standup)


if __name__ == "__main__":
    main()
