# Change contract: idle persistence writes

## Goal and boundaries

Make recurring Watch runtime snapshots proportional to real process-state
changes, and coalesce adjacent Model Hub usage submissions without changing
persist-before-acknowledgement. No schema, release, credential, deployment, or
SQLite durability-setting changes.

## Watch runtime invariants

- A complete snapshot atomically inserts new runtime rows, changes existing
  runtime rows, and terminalizes only previously active rows absent from it.
- Unchanged process state produces no row updates, even when snapshot timestamps
  or JSON key order differ. `updated_at` describes the last persisted process
  state change rather than a heartbeat caused by unrelated Watches.
- PID, start time, process identity, running state, and other runtime metadata
  changes persist immediately. Present terminal rows can become running again;
  obsolete terminal stamps are cleared as before.
- Only changed columns appear in UPDATE, avoiding unrelated index maintenance.
- Recovery still receives the saved process identity and running state. The
  legacy PID-reuse fallback compares process creation time with the last saved
  observation, not an expiry threshold; a state-change timestamp remains valid.
- Queries are bounded by active runtime rows or snapshot primary keys, never
  historical run volume. Read/diff/write retains the writer reservation the old
  first UPDATE held, using the existing `reserve_write_lock` helper.
- Existing databases require no migration; older binaries can read the same rows.

## Findings checked against master

- `write_watch_runtime` terminalizes all active runtime rows and then performs
  full-column updates for every snapshot entry, even unchanged peers.
- `core/watches.py` stamps all active entries with the same fresh snapshot time.
- Old background-run index names can survive the legacy table rename; current
  schema also has source-actor prefix overlap. Leave index deletion to a separate
  migration with old-schema fixtures and query-plan validation rather than make
  a broad planner change part of the primary write fix.
- The engine sets WAL, foreign keys, and busy timeout, but does not override page
  cache or auto-checkpoint defaults. Keep those defaults and synchronous
  durability; remove unnecessary dirty pages instead.
- The usage writer already batches arrivals before its worker starts, but has
  no bounded batching window for arrivals spread across loop turns.
- `storage.loop_writers` intentionally measures controller-loop blocking, not
  process-wide physical writes. Worker-thread write-volume attribution is a
  separate observability follow-up, not a change to this metric's meaning.

## Validation

Primary storage regressions belong to `test_harness_definition_lifecycle.py`:
unchanged snapshots issue no DML; changed snapshots affect only the changed and
missing rows, with narrow SET columns; disappearance settles once; recovery
reactivates the same runtime row. Capture query plans to guard bounded searches.
The new tests failed against pre-fix master for repeated full-row writes.

Focused Watch lifecycle, recovery, live status, and CLI read-projection tests
must pass. Usage writer tests cover batching and persist-before-ack semantics.
Before/after service bytes/minute and WAL frames/call are measured only in an
isolated fresh local Incus worktree environment, never on the owner's desktop.
