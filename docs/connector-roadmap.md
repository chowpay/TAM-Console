# Connector Roadmap

TAM Console should eventually use Jira, Slack, email, and Obsidian daily notes
as source context for customer records. Imported data should remain reviewed and
bounded; raw private source content must not be committed to git.

## Health Automation

- If customer health is blank or `Unknown`, automation may set an initial value.
- If customer health is already `Green`, `Yellow`, or `Red`, automation must not
  overwrite it automatically.
- For customers with manual health already set, automation should create a
  suggested health value with evidence and let the operator apply it.
- Evidence should reference source type, date, and link or file path where
  possible.

## Slack

- Default Slack access should be read-only.
- Start with bounded channel targets and date windows.
- Use Slack context to suggest staff, environment ownership, risks, and next
  actions.
- Future write support is allowed only for narrow, explicit workflows such as
  reminders to the operator or approved channels/people.
- Slack writes must require explicit confirmation and must never post broad
  customer updates automatically.

## Email

- Default email access should be read-only.
- Use email context for meeting outcomes, commitments, deployment details, BOM
  hints, and follow-up reminders.
- Future write support should generate draft text only by default.
- Customer email drafts should not include `To`, `Cc`, or `Bcc` recipients, so
  they cannot be sent accidentally.
- Real send actions should remain out of scope unless explicitly enabled later.
- Track inbound customer requests from email and identify whether each request
  has been answered.

## Response Tracking

- Add dashboard widgets for unanswered customer requests.
- Sources should include inbound customer emails and Jira ticket comments.
- Track request source, customer, related ticket or environment, request date,
  current owner, response status, and last response date.
- Surface stale items when no response has been found within a configurable
  window.
- Treat AI classification as a suggestion: it can identify likely unanswered
  requests, but the operator should be able to mark items answered, ignored, or
  not a request.

## Obsidian

- Treat Obsidian daily notes as a local source.
- Scan configured vault paths for dates, customer aliases, Jira keys,
  environments, people, and next actions.
- Import suggested timeline entries or notes only after review.
- Keep source links back to the original Markdown note.
