# Customer Health Evaluation Rubric

Use this rubric when summarizing Slack, email, Jira, meetings, and notes into a customer health suggestion.

Manual health is authoritative. AI-generated health is a suggestion and must include evidence.

## Inputs

- Recent unresolved tickets, escalations, outages, regressions, upgrade blockers, and service degradations.
- Recently resolved issues and whether the customer confirmed recovery.
- Positive indicators such as successful upgrades, customer thanks, quick acknowledgement, stable post-resolution period, and no active follow-up.
- Relationship context such as last topic discussed, unanswered asks, tone, urgency, and repeated friction.
- Recency. A high-risk issue from yesterday should matter more than a resolved issue from months ago.

## Suggested Output

Return concise structured fields:

- `suggested_health`: `Green`, `Yellow`, `Red`, or `Unknown`
- `last_topic_discussed`: short factual topic summary
- `recently_resolved`: most relevant recently closed issue, if any
- `positive_indicators`: short list of positive signals, if any
- `risk_indicators`: short list of current risks, if any
- `reasoning`: one short paragraph explaining the suggestion
- `evidence`: source links, ticket keys, message dates, or meeting note references

## Base Interpretation

- `Green`: no current open customer-impacting issue, recent issue resolved or aging out, or positive confirmation/stability exists.
- `Yellow`: current concern exists but is bounded, has a workaround, or is waiting on follow-up.
- `Red`: active outage, escalation, repeated unresolved friction, major upgrade blocker, or customer-impacting regression.
- `Unknown`: not enough recent evidence.

Do not mark a customer Red solely because old Slack messages describe a severe incident. Resolved tickets, explicit recovery, successful upgrade, and age should reduce current risk.
