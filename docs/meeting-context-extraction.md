# Meeting Context Extraction

Use this workflow when turning a vid2kb meeting run into TAM Console customer context.

## Context Packet

Before asking AI to extract meeting meaning, gather a bounded packet:

- Customer profile: name, aliases, status, health, owner, products, environments, architecture.
- Current tickets: key, title, status, priority, updated, short summary.
- Current staff: names, roles, mapped environments, source tickets.
- Recent meetings and notes.
- Relevant advisories and health reasoning.
- vid2kb artifacts: cleaned transcript, timeline, overview, and source path.

Do not ask AI to browse broad private data. Give it only this packet and require evidence references.

## AI Output Shape

Require structured JSON or a clearly labeled draft with these fields:

- `meeting_title`
- `meeting_date`
- `attendees`
- `last_topic_discussed`
- `summary`
- `decisions`
- `action_items`: each with `task`, `owner`, `due_date`, `confidence`, `evidence`
- `risks_or_open_questions`
- `resolved_items`
- `ticket_references`
- `environment_references`
- `staff_updates`
- `customer_health_impact`: `positive`, `neutral`, `watch`, or `risk`
- `health_reasoning`
- `tam_console_updates`: proposed updates to next action, notes, staff, artifacts, or health

## Review Rule

AI output is a draft. The user must approve or edit before TAM Console updates durable fields such as health, next action, staff mapping, software versions, or environment facts.

## Evidence Rule

Every action item, risk, decision, or health-impact claim should cite at least one source:

- transcript timestamp
- vid2kb timeline timestamp
- Jira key
- existing TAM Console meeting/note/artifact

If evidence is weak, mark confidence as `low` and keep it as a review note.
