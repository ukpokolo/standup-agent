#!/usr/bin/env python3
"""Generate a standup message from recent git commits and Jira Cloud tickets
using a model on the company AI gateway (OpenAI-compatible API).

Secrets come from environment variables only:
    JIRA_EMAIL, JIRA_API_TOKEN, AI_GATEWAY_API_KEY
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
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

SYSTEM_PROMPT = (
    "You write concise daily standup updates for a software engineer. "
    "Use exactly three sections: Yesterday, Today, Blockers. Use short bullet points. "
    "Only mention work that appears in the data provided; never invent work. "
    "Refer to Jira tickets by their title, written in plain readable words, and never output ticket IDs or keys "
    "(like ABC-123): readers find IDs meaningless. Use the ticket keys in the data only to match commits to "
    "their ticket title. If a commit mentions a key with no known title, describe the work instead. "
    "YESTERDAY = what I did in the report window: commits, tickets I moved between statuses, tickets closed, "
    "and comments I made. Also include feedback I received: if someone else (for example QA) commented on a "
    "ticket or moved it back to development in the window, say that feedback was received on that ticket and "
    "summarise it, even if it happened late in the day. "
    "TODAY = what I will work on now: tickets currently in progress or in development, and any ticket sent back "
    "with feedback (state what needs fixing, taken from the comment). Do not list tickets that are Ready for QA, "
    "in review, or closed as work to do, unless feedback sent them back. Put tickets with no activity in the "
    "window into one final bullet 'Also in progress: ...' instead of listing each separately. "
    "BLOCKERS = only things the data shows are blocking work (for example a comment saying blocked or waiting on "
    "someone). If there are none, write 'None'."
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
    until_str = until.strftime("%Y-%m-%d %H:%M")
    fmt = "%h|%ad|%D|%s"
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
        for line in commits.splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            sha, date, refs, subject = parts
            rows.append({
                "sha": sha, "date": date, "subject": subject,
                "pushed": sha not in unpushed_set,
                "tickets": sorted(set(TICKET_RE.findall(subject + " " + refs))),
            })
        sections.append({"repo": name, "commits": rows})
    return sections


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


def jira_search(cfg, email, token, jql, max_results=50):
    data = jira_get(cfg, email, token, "/rest/api/3/search/jql",
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


def collect_jira(cfg, since, until):
    email = require_env("JIRA_EMAIL")
    token = require_env("JIRA_API_TOKEN")
    me_id = verify_jira_auth(cfg, email, token)
    since_str = since.strftime("%Y-%m-%d %H:%M")
    until_str = until.strftime("%Y-%m-%d %H:%M")
    open_issues = jira_search(
        cfg, email, token,
        "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
    )
    # Comments by others (e.g. QA) also bump "updated", so feedback received in the window shows up here.
    # "assignee was" also catches tickets reassigned to QA while I am waiting on their feedback.
    touched = jira_search(
        cfg, email, token,
        f'(assignee = currentUser() OR assignee was currentUser()) '
        f'AND updated >= "{since_str}" AND updated <= "{until_str}" ORDER BY updated DESC',
    )
    tickets = {}
    for issue in open_issues:
        tickets[issue["key"]] = {**issue, "open": True}
    for issue in touched:
        tickets.setdefault(issue["key"], {**issue, "open": False})
    for ticket in list(tickets.values())[:MAX_TICKETS_WITH_ACTIVITY]:
        ticket["activity"] = fetch_activity(cfg, email, token, ticket["key"], me_id, since, until)
    return list(tickets.values())


def render_context(since, now, git_sections, jira):
    lines = [f"Report window: {since:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M}", "", "== GIT COMMITS =="]
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
    lines += ["", "== JIRA TICKETS ASSIGNED TO ME =="]
    if jira is None:
        lines.append("(Jira skipped)")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--standup-time", help="HH:MM, overrides standup_time from config")
    ap.add_argument("--since", help="explicit window start: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--until", help="window end 'YYYY-MM-DD HH:MM' (default: now); use to replay a past standup")
    ap.add_argument("--dry-run", action="store_true", help="print collected data, do not call the model")
    ap.add_argument("--skip-jira", action="store_true", help="git only")
    ap.add_argument("--list-models", action="store_true", help="list models on the gateway and exit")
    ap.add_argument("--copy", action="store_true", help="copy the result to the clipboard")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.list_models:
        list_models(cfg)
        return

    try:
        now = datetime.strptime(args.until, "%Y-%m-%d %H:%M") if args.until else datetime.now()
    except ValueError:
        sys.exit(f"Invalid --until '{args.until}', expected 'YYYY-MM-DD HH:MM'.")
    standup_time = parse_hhmm(args.standup_time or cfg.get("standup_time", "09:30"))
    since = window_start(standup_time, args.since, now)

    git_sections = collect_git(cfg.get("repos", []), (cfg.get("git") or {}).get("author"), since, now)
    jira = None if args.skip_jira else collect_jira(cfg, since, now)
    context = render_context(since, now, git_sections, jira)

    if args.dry_run:
        print(context)
        return

    standup = generate(cfg, context)
    print(standup)
    if args.copy:
        copy_to_clipboard(standup)


if __name__ == "__main__":
    main()
