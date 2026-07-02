# Agent Handoff

Use this file when continuing TAM Console work in a fresh AI session.

## Start Here

Read these tracked files first:

- `README.md`
- `docs/agent-handoff.md`
- `docs/decisions.md`
- `docs/connector-roadmap.md`
- `docs/backlog.md`

Local private notes may exist under `notes/`, but that directory is ignored by
git and may contain operator-specific context. Do not assume it exists in a
fresh clone.

## Current Project

TAM Console is a local technical account management console for tracking
customers, environments, architecture, tickets, staff, hardware, software,
meetings, notes, and artifacts.

The current implementation is a single Python standard-library web app:

- App: `app.py`
- Local database: `data/casefiles.db`
- Default URL: `http://127.0.0.1:8787`
- Current lab URL: use the local deployment host and configured `CASEFILES_PORT`.
- Public repo: `https://github.com/chowpay/TAM-Console.git`

The app is intended to be public-safe code. Real customer data must stay out of
git.

## Run Locally

```bash
cd /home/ubuntu/TAM-Console
python3 app.py
```

If a long-running copy is needed:

```bash
setsid -f python3 /home/ubuntu/TAM-Console/app.py >> /home/ubuntu/TAM-Console/data/server.log 2>&1
```

## Restart Safely

Avoid broad `pkill -f` commands that can match the current shell command. Use an
exact process argument match when possible:

```bash
python3 - <<'PY'
import os, signal, subprocess, time
needle = '/home/ubuntu/TAM-Console/' + 'app.py'
out = subprocess.check_output(['ps', '-eo', 'pid=,args='], text=True)
for line in out.splitlines():
    parts = line.strip().split(None, 1)
    if len(parts) == 2 and parts[1] == 'python3 ' + needle:
        os.kill(int(parts[0]), signal.SIGTERM)
time.sleep(0.5)
PY
setsid -f python3 /home/ubuntu/TAM-Console/app.py >> /home/ubuntu/TAM-Console/data/server.log 2>&1
```

## Current Implemented Highlights

- Dashboard with customer/ticket/risk/data-quality widgets.
- Custom hover/focus help tooltips for dashboard metric labels.
- Collapsible customer sidebar.
- Customer search.
- Customer pinning and manual up/down ordering.
- Bulk hide for visible customers and a collapsible `Hidden` group.
- Customer health values: `Unknown`, `Green`, `Yellow`, `Red`.
- Inline health picker from the customer page header.
- Customer page header shows health, status, next action, due date, owner, and
  products on every section.
- Customer sections: Overview, Environments, Architecture, Tickets, Staff,
  Hardware, Software, Meetings, Notes, Artifacts.
- Environments support type/location/status/products/source-type tags.
- Ticket table supports sorting, search, ESD/CS filters, clickable Jira keys,
  direct add, Jira discovery, and known-ticket sync.
- Staff records support multiple environment mappings.
- Jira import can import assigned tickets by Organization and reconcile moved
  ticket organization links.
- Jira feature-request tickets may use `FR-*` browse links that resolve to
  current `MB-*` keys.
- Acquisition/brand routing is an open problem: selected tickets may need to be
  routed to a managed account based on requester/person/text context rather
  than only the Jira organization value.
- Local artifacts under approved roots can be linked and previewed.

## Data Boundaries

Tracked:

- Source code
- Public-safe docs
- Demo seed script
- Sample config

Ignored/private:

- `data/`
- `notes/`
- `backups/`
- `*.db`, `*.sqlite`, `*.sqlite3`
- `config/atlassian_config.py`
- Real customer exports, Slack/email summaries, diagrams, and runtime logs

## Working Agreement For Agents

- Do not commit real customer data.
- Prefer small, testable changes.
- Run `python3 -m py_compile app.py` after app edits.
- Restart the local server after UI/app behavior changes.
- Verify with `curl` or a browser-visible page response where practical.
- Commit and push public-safe code/docs changes.
- Keep private operational notes in ignored local files, or sanitize them before
  putting them under `docs/`.

## Claude Continuation Prompt

Use this when handing the project to Claude:

```text
You are continuing work on TAM Console.
Clone or open https://github.com/chowpay/TAM-Console.git.
Read README.md, docs/agent-handoff.md, docs/decisions.md,
docs/connector-roadmap.md, and docs/backlog.md first.
Do not commit customer data, data/, notes/, credentials, logs, or exports.
Use the tracked docs as the shared handoff state.
```
