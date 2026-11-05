# SQLite contention: startup writers and Run persistence

## Evidence and scope (2026-11-06)

Refs #2706, #2725, and parked design PR #2726. The existing loop-writer
census includes lock acquisition time; a long census interval is not evidence
that its caller held the writer for that interval. Watchdog samples show a
trace INSERT on the loop and a canonical transcript `BEGIN IMMEDIATE` on a
worker waiting concurrently. Delivery-outcome and Run-activity errors are
already contained; canonical transcript writes are not optional and must not
be silently discarded.

Read-only OS lock-holder sampling identified independent CLI processes as
writers, including bursts of concurrent `vibe vault fetch` invocations. One
concrete source is ordinary SQLite bootstrap: the one-shot deferred-resource
migration reserves the writer before recognizing its completed v3 marker.
The caller then counts eight state tables before releasing that transaction.
A scratch-database probe confirmed that even a no-change bootstrap holds the
writer through those counts. This is unnecessary contention, not a reason to
introduce the global ordered writer discussed in #2726.

A separate size census shows that canonical transcript and conversation history
dominate a long-lived installation. Existing 30-day tool-trace retention works;
context rows are intentionally outside it. This work changes no retention
policy and deletes no user data.

## PR 1 contract: completed bootstrap does not reserve the writer

- An already-completed v3 deferred-resource migration is an immutable receipt.
  An ordinary open recognizes it without reserving SQLite's writer.
- The fast path performs no authorization or data mutation. Configuration-read
  failure continues to report unavailable, rather than granting authority.
- Unfinished/legacy Member-repair migrations still reserve the writer before
  their decision reads, re-read the marker, and retain all existing pairing,
  provenance, caller-transaction and fail-closed guarantees.
- Report-only row counts for an already-imported database run after the migration
  transaction closes. Initial import validation retains its atomic boundary.
- No schema revision, released data transformation, downgrade change, retention
  change, runtime restart or new dependency is introduced.

Primary regressions: a held independent SQLite writer cannot prevent reading
an already-completed migration; another writer can commit while warm-bootstrap
report counts run. Existing Member migration race tests continue to guard the
actual repair decision reads, not the new nonmutating precheck.

## Follow-up contract: contention recovery

Retry only rolled-back SQLite transactions, never model calls, tool effects or
network delivery. Preserve per-Session transcript order, commit-before-expose
and cancellation landing. Optional liveness/outcome projections stay nonfatal;
canonical context must either commit or fail visibly after a bounded retry
budget. Final implementation and exact retry budget belong to the follow-up PR.
