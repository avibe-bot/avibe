# Running Activity History Hydration

## Problem

Switching to a running Session can display only the latest streamed steps even
when the durable Activity group contains hundreds of rows. Both the detail-fetch
gate in ChatPage and the live reducer treat a nonempty stream buffer as complete
history. A single live event arriving before either HTTP response wins that race.
Settled groups use their durable details directly and are unaffected.

## Scope

Keep the existing Activity endpoint and live reducer as the owners. Read the
running group's history even when live rows exist, then merge by persisted row
identity in emission order with the newer live tail. Preserve already-loaded rows
as the durable endpoint's bounded window advances. Replay must not add duplicate
steps, and hydration must retain the earliest start time.

Generation checks must reject detail responses after settlement, a new turn, a
visibility reset, or a Session switch, including returning to the same Session.
Detail failures use the existing bounded refresh retry.

## Validation

- Reproduce both response orderings in ChatPage with 300 persisted steps.
- Assert the full history and live tail remain visible exactly once after replay
  and reconnect.
- Cover lifecycle invalidation and lazy loading of settled groups.
- Run focused UI tests, lint, and the production UI build before PR review.
