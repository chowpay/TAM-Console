# TAM Console

Technical account context, environments, tickets, staff, hardware, meetings,
architecture, and evidence in one local console.

This repository is intended to be safe to publish as application code. Do not
commit the local `data/` directory or real customer exports.

## Run

```bash
git clone https://github.com/chowpay/TAM-Console.git
cd TAM-Console
python3 app.py
```

To run it in the background and write logs to `data/server.log`:

```bash
cd /home/ubuntu/TAM-Console
setsid -f python3 app.py >> data/server.log 2>&1
```

Check whether it is running:

```bash
ps -eo pid,args | grep 'python3 app.py' | grep -v grep
```

Watch logs:

```bash
tail -f /home/ubuntu/TAM-Console/data/server.log
```

Defaults:

- host: `0.0.0.0`
- port: `8787`
- database: `data/casefiles.db`

Override:

```bash
CASEFILES_HOST=127.0.0.1 CASEFILES_PORT=8787 python3 app.py
```

Or copy `.env.sample` to `.env` and edit it.

Then open:

```text
http://127.0.0.1:8787
```

## Demo Data

The app starts with an empty database. To create public-safe demo data:

```bash
python3 scripts/seed_demo.py
```

This creates a fictional customer, environments, one demo ticket, and one demo
staff entry.

## Private Data

Ignored local/private paths:

- `data/`
- `backups/`
- `*.db`
- `config/atlassian_config.py`

Real customer data, Jira exports, email/Slack summaries, diagrams, and runtime
artifacts should stay in local ignored storage unless explicitly sanitized.

## Atlassian Config

For Jira sync, copy:

```bash
cp config/atlassian_config_sample.py config/atlassian_config.py
```

Then fill in:

- `EMAIL`
- `API_TOKEN`
- `BASE_URL`

You can also point to a config with:

```bash
CASEFILES_ATLASSIAN_CONFIG=/path/to/config.py python3 app.py
```

## systemd Deployment

Recommended simple deployment path:

```bash
sudo mkdir -p /opt
sudo git clone https://github.com/chowpay/TAM-Console.git /opt/tam-console
sudo chown -R ubuntu:ubuntu /opt/tam-console
cd /opt/tam-console
cp .env.sample .env
python3 scripts/seed_demo.py
sudo ./scripts/install_systemd.sh
sudo systemctl start tam-console
```

Service logs:

```bash
sudo journalctl -u tam-console -f
```

## Backup

Private runtime data lives under `data/`. Back up the SQLite DB with:

```bash
./scripts/backup_data.sh
```

Set `BACKUP_DIR=/some/path` to choose a backup location.

## First Model

- `customers`: account overview and architecture notes
- `tickets`: Jira/ESD/CS issue links and local notes
- `meetings`: meeting summaries and action items
- `notes`: general, architecture, risk, next action, and finding notes
- `artifacts`: local paths or URLs for PCAPs, logs, diagrams, findings, docs
- `environments`: deployment/site/cloud areas under a customer
- `hardware`: environment/customer hardware inventory
- `staff`: people and environment responsibilities

## Future Sync

Routine Jira refresh should use the Jira REST API for known linked ticket keys
and organization-based discovery. AI/Claude should be reserved for bounded
summaries and messy-source extraction from Slack, email, Confluence, BOMs, and
meeting notes.

## Agent Handoff

For AI-assisted continuation, read these tracked project notes first:

- `docs/agent-handoff.md`
- `docs/decisions.md`
- `docs/connector-roadmap.md`
- `docs/health-evaluation-rubric.md`
- `docs/backlog.md`

These files are the shared handoff state for Codex, Claude, and future sessions.
Do not commit real customer data, credentials, `data/`, `notes/`, exports, or
runtime logs.
