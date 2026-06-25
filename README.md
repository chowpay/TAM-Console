# TAG Casefiles

Local web app for customer case files: architecture, meetings, Jira/ESD links,
notes, artifacts, and eventually daily Atlassian sync.

This repository is intended to be safe to publish as application code. Do not
commit the local `data/` directory or real customer exports.

## Run

```bash
cd /home/ubuntu/tag_inspect/customer_casefiles
python3 app.py
```

Defaults:

- host: `0.0.0.0`
- port: `8787`
- database: `data/casefiles.db`

Override:

```bash
CASEFILES_HOST=127.0.0.1 CASEFILES_PORT=8787 python3 app.py
```

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

## First Model

- `customers`: customer overview and architecture notes
- `tickets`: Jira/ESD issue links and local notes
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
