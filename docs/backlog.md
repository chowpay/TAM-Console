# Backlog

Concrete future work for TAM Console.

## Near Term

- Add a customer timeline combining tickets, meetings, notes, artifacts, and
  future connector signals.
- Add a formal Staff import workflow instead of one-off scripts.
- Tighten CS ticket discovery to reduce false positives from broad text search.
- Add customer-visible counts for hidden customers and possibly hidden tickets
  or archived records.
- Add edit/delete flows for manually entered records where missing.
- Add a proper service install path when sudo access is available.

## Connectors

- Add Slack read-only discovery for explicitly selected workspaces/channels or
  date windows.
- Add email read-only discovery for request tracking, meeting follow-ups,
  deployment details, and draft generation.
- Add Obsidian vault scanning for daily notes.
- Add Proxmox read-only inventory sync for clusters, nodes, VMs, containers,
  status, resource allocation, tags, and customer/environment mapping.
- Add review queues for connector suggestions.
- Add connector settings for trust level:
  - read-only
  - draft only
  - confirm before send
  - trusted reminders

## Health And Risk

- Add `suggested_health` and supporting evidence records.
- Add dashboard widget for health suggestions waiting for review.
- Add logic to fill `Unknown` health from strong source signals.
- Do not overwrite manual Green/Yellow/Red automatically.

## Response Tracking

- Model inbound requests from email and Jira comments.
- Track response status, owner, source, related ticket/environment, and stale
  age.
- Add dashboard widgets:
  - unanswered requests
  - stale requests
  - requests by customer
- Allow manual override: answered, ignored, not a request.

## Deployment And Operations

- Add optional systemd setup documentation for `/opt/tam-console`.
- Add backup/restore instructions for `data/casefiles.db`.
- Add smoke-test script for dashboard and common customer pages.
- Add schema migration tests or lightweight database upgrade checks.

## Data Model Ideas

- `customer_signals`: normalized source signals from Jira, Slack, email,
  Obsidian, and manual notes.
- `health_recommendations`: suggested health, evidence, confidence, and review
  state.
- `response_items`: inbound requests and whether they have been addressed.
- `timeline_events`: normalized customer timeline entries from all sources.
- `connector_settings`: per-connector and per-destination trust settings.
- `infrastructure_assets`: read-only inventory records such as Proxmox VMs,
  containers, nodes, IPs, tags, and customer/environment mappings.
