# Decisions

This file records product and engineering decisions that should survive AI
session context limits.

## Repository And Data

- The public-safe application code lives in `TAM-Console`.
- Real customer data must not be committed.
- Runtime data lives in ignored local storage under `data/`.
- Operator/private notes live under ignored `notes/`.
- Public-safe product notes and handoff material live under tracked `docs/`.

## Product Name

- The product is called `TAM Console`.
- It is a technical account manager console, not a generic CRM.

## Customer Model

- A customer can have multiple environments, such as on-prem sites, cloud
  deployments, multi-host environments, labs, or regions.
- Customers can be pinned, manually ordered, hidden, and restored from a hidden
  group.
- Customer status is separate from sidebar visibility.

## Health

- Health values are `Unknown`, `Green`, `Yellow`, and `Red`.
- Manual health is authoritative.
- Automation may fill health only when the value is blank or `Unknown`.
- Automation must not overwrite existing `Green`, `Yellow`, or `Red` without
  explicit operator approval.
- Future automation should show suggested health and evidence separately from
  manual health.

## Jira

- Jira keys should remain clickable.
- ESD and CS tickets are both supported.
- Known ticket sync should use Jira REST API where possible.
- AI can help discover or summarize messy cases, but direct API sync should own
  routine refresh.
- Organization changes in Jira should reconcile local ticket/customer links.

## Connectors

- Slack and email access should start read-only.
- Future Slack writes must be limited to explicit reminders for approved people
  or approved destinations.
- Email should generate draft text only by default.
- Customer email drafts should omit recipients so they cannot be sent
  accidentally.
- Any future send/post/write action must require explicit confirmation unless a
  narrow trusted workflow is configured.

## Obsidian

- Obsidian daily notes are a future local source.
- The app should scan configured vault paths and suggest timeline entries,
  notes, next actions, staff, tickets, and environment links.
- Imported Obsidian content should preserve source links and support review
  before importing.

## Proxmox

- Proxmox is a future read-only infrastructure inventory source.
- TAM Console should ingest metadata about VMs, containers, nodes, tags, status,
  and resource allocation.
- Proxmox metadata should map infrastructure assets to customers and
  environments.
- TAM Console should not provide Proxmox host shell access.
- Proxmox lifecycle/configuration actions are out of scope by default.

## Response Tracking

- The dashboard should eventually track inbound customer requests from email and
  Jira comments.
- The app should surface unanswered or stale requests.
- The operator should be able to mark a request answered, ignored, or not a
  request.

## UI

- Dark mode is default.
- The first screen should be useful app functionality, not a marketing page.
- Dashboard shorthand labels should have hover/focus help.
- Editing controls should stay close to the data they modify.
