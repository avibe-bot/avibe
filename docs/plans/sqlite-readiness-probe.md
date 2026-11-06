# SQLite readiness probe (CLI runtime path Phase 2)

## Change contract

On the first `ensure_sqlite_state()` for a database/state-directory pair in a
process, check the exact installed Alembic head and existing import/data receipts
through an existing-file SQLite `mode=ro` connection. Only a proven current state
skips repair. The probe takes no migration lock, creates no engine or directory,
performs no backup bookkeeping, and scans no history/report counts. Existing CLI
and ordinary service-store callers inherit this through the shared
`ensure_sqlite_state()`.

**Service startup exception:** `main.prepare_sqlite_state()` runs the full repair
and backup validation once per service start. The existing 72-hour backup-expiry
lifecycle requires the service to validate its own attempt before recording
readiness, including when another CLI/UI process upgraded first or a previous
service died before readiness. A schema probe must not replace that lifecycle
owner. This keeps backup bookkeeping out of every cold CLI invocation.

Missing, old, future/unknown, malformed, incomplete, or unreadable state enters
`repair_sqlite_state()`, the existing conservative lock/migrate/import/backup path.
No schema migration, new readiness stamp, identity check, service IPC, service
restart, or deployment is part of this change.

Existing process-lifetime memoization stays intact. The check-to-use window
(including a replacement or newer-version migration after a successful check)
already exists today and is unchanged; this change does not pin later operations
to a connection, add leases, or revalidate every operation. Replacement tests
cover a fresh process/startup, not a stronger live-replacement guarantee.

## Existing completion evidence

- `alembic_version`: exactly one revision, equal to the installed script head.
- `json_import_completed_at` and `background_json_import_completed_at`: valid
  JSON completion timestamps.
- `migrations.legacy_deferred_resource_contexts.v1`: a completed v3 receipt or
  the current terminal v2 `sealed_unattributed` receipt. Pending and older
  completed receipts still need the existing migration decision path.

The data migration deliberately leaves a fresh unpaired install without a receipt
when no unbound deferred snapshots exist. Its docstring's unpaired seal describes
an existing migration opportunity, not this fresh-install exception. Proving that
exception uses `_has_unbound_deferred_snapshots()`, which scans definitions and
live run/delivery metadata and reads configured binding state. That is not the
bounded receipt-only probe. A missing receipt therefore stays on the slow path
once per process; no stamp/migration is added to optimize it.

## Validation

Protect the owning boundary with current-state read-only/held-lock tests and
old/future/missing/corrupt/malformed/incomplete/replaced-file cases. Synchronize a
real concurrent upgrade before its final version commit and verify repair waits
rather than returning an old schema. Keep existing importer, migration-lock,
backup-lifecycle, and startup behavior tests. The expiry owner test also covers a
CLI process upgrading first, followed by a fresh service validating that same
attempt, reaching `ready`, and expiring the backup at the existing deadline.

Measure hermetically with synthetic HOME/AVIBE_HOME/XDG state: cold commands and
200 mixed full-dispatch CLI calls at 20-way concurrency with fixed-rate real
transcript writes. Report wall latency, bootstrap time, lock errors, and transcript
commit latency. The phase benchmark uses local stubs, not the owner's state or
credentials. Full warm-service/Relay acceptance belongs to the later service
phases, not a claim this phase can make.

## Rollback

Revert this PR to restore the first-process unconditional repair path. No data
format changes or migration downgrade are needed. Repair itself remains directly
available for explicit maintenance and diagnostics that require its report.
