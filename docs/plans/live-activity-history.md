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
The detail response exposes `order_micros`, the exact emission key already used
by storage. Migrated event IDs are opaque hashes; only storage can recover their
original Message clock. Durable hydration owns that key even when it overlaps a
live row. Equal emission keys use the endpoint's deterministic ID tie-break.

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
- Verify the shared ordering fixture against real SQLite detail output and the
  frontend wire mapper/reducer, including migration-only clocks and live overlap.
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
- Head `f0a1427071`: one P2 finding (migrated event order is unavailable in the
  detail wire). This repeats the ordering-contract class from `190c6ef87f`.
  Three findings-bearing heads total; the repeated-class circuit breaker fired.
- Orchestrator diagnosis: frontend time/ID reconstruction cannot match storage
  for migration-generated hashes whose original clock exists only in metadata.
  The previous tie-break fix addressed the visible tie, not the missing contract.
  The phase-boundary fix is independent and remains in place.
- Scope decision before further implementation: add the existing storage
  emission key to Activity detail rows as `order_micros`; let durable rows own
  that key during hydration. Keep ID-clock fallback only for rows without a
  durable key (including current SSE envelopes). Verify a shared fixture against
  real SQLite grouping and the consuming frontend reducer across window shifts,
  both snapshot orders, mixed sources, migration hashes, and overlapping live rows.
  No migration, configuration change, new dependency, or alternate grouping owner.
