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
Equal emission times use the endpoint's deterministic ID tie-break, including
legacy IDs without a clock prefix; snapshot arrival order is not a sort key.

Generation checks must reject detail responses after settlement, a new turn, an
output phase boundary, a visibility reset, or a Session switch, including returning
to the same Session. The live generation belongs to one Activity phase, not the
entire logical Turn; detached completions do not advance that generation.
Detail failures use the existing bounded refresh retry.

## Validation

- Reproduce both response orderings in ChatPage with 300 persisted steps.
- Assert the full history and live tail remain visible exactly once after replay
  and reconnect.
- Cover lifecycle invalidation and lazy loading of settled groups.
- Run focused UI tests, lint, and the production UI build before PR review.

## Review Audit

- Head `190c6ef87f`: one P2 finding, one root-cause class (missing deterministic
  tie ordering when hydration windows advance). No repeated findings-bearing head.
- Scope decision: reuse storage's existing time/ID order in the live reducer;
  no API or data-model change. Regression tests cover every window split with
  overlap and without it, both arrival orders, and both legacy and clock-bearing
  IDs whose emission times tie.
- Head `0f54955369`: one P2 finding, a distinct root-cause class (phase boundaries
  changed durable group ownership without invalidating the live generation).
  Two findings-bearing heads total; no repeated root-cause class.
- Scope decision: reuse the existing generation reset at the shared effective
  boundary-role policy. Keep controller Turn state unchanged and keep detached
  completion handling refresh-only. Exercise old summary/detail responses and
  already-hydrated rows across the boundary; preserve settled-chip ownership.
