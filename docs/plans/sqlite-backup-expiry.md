# SQLite Migration Backup Expiry

## Contract

Managed SQLite migration backups are short-lived upgrade rollback points, not
permanent user archives. After their own migration has committed, database
validation has passed, and the owning core service has become ready, they expire
after 72 hours. The final eligible backup may expire; no minimum copy count
overrides expiry.

- Record the successful lifecycle durably and bind it to the actual migration
  attempt and database. A package-install exit code, matching revision label,
  file mtime, or later unrelated service startup is not proof of that attempt.
- Readiness starts the clock once. Restarts must not renew it. Failed,
  interrupted, unverified, or restored attempts do not qualify for age expiry.
- Retain the existing bounded rollback-position policy independently. Older
  backups can be evicted by that bound before 72 hours; this is a maximum normal
  retention age, not a promise that every copy survives for three days.
- Only recognized Avibe SQLite migration backups qualify. JSON import backups,
  hand-made files, symlinks, malformed manifests, and legacy backups without
  success evidence keep their existing policies.
- Do not manufacture completion evidence for old backups. Deploying this change
  must not bulk-expire existing copies based on their creation dates.
- A restore invalidates prior expiry authority before changing the database.
  Preserve displaced database/WAL recovery evidence and serialize migration,
  restore, and expiry against the same database lock.
- A failed or unconfirmed migration must not let an old readiness signal
  authorize expiry of the rollback evidence needed by that attempt.
- Reuse the controller's bounded maintenance worker: inspect on startup and
  hourly, independently of optional trace-retention/Skill settings. Offline
  machines catch up when started. Do not introduce a Watch or scheduled Task.
- Expiry is optional maintenance: contention or failure defers deletion and is
  logged, never makes a healthy service fail startup. Log actual removal and
  reasons for deferral without exposing database contents.

## Implementation Boundaries

The backup module owns lifecycle metadata, candidate admission, and deletion.
The migration/import path produces completion evidence only after its actual
work and validation succeed. The established service-readiness owner confirms
readiness; the existing maintenance worker only consumes that evidence.

Use additive, backward-compatible backup metadata and existing filesystem/lock
helpers. No new dependency, UI control, operating-system scheduler, production
cleanup, service restart, or deployment is part of this change.

### Lifecycle Evidence

The database-owned sidecar and the backup manifest share one attempt ID,
canonical database path, file identity, target revisions, and lifecycle phase.
Before a migration can mutate SQLite, it durably publishes a blocking
`migrating` sidecar. Only successful Alembic completion and a SQLite check advance
it to `migrated`. The importer records `validating` before data work and
`validated` only after commit and validation. Its report carries the exact
attempt ID in the existing process-local ensured-state cache. The actual
service-readiness owner consumes only an attempt validated by that process,
checks the live and packaged schema heads, and records `ready` once. It never
discovers a readiness attempt by selecting the latest on-disk receipt.

The service captures the exact attempt and its original readiness time. A
try-lock miss retains this pending event for the existing maintenance worker
to retry, without renewing the clock or adopting another attempt. Releasing
the service-instance lock drops the pending event. If the process dies before
confirmation, the next startup must perform validation and readiness again;
there is no inferred success or old timestamp to recover.

Manifest and sidecar updates use private temporary files, atomic replacement,
and file/directory synchronization. Disagreement after an interrupted write
does not grant expiry authority. Optional completion metadata failures keep
the service usable and retain backups; failure to establish the initial
blocking record aborts before SQL mutation.

The expiry worker takes the existing migration lock without waiting. It checks
the latest lifecycle and live revisions before considering individual ready
manifests. A replacement database cannot inherit the old file's authority.
Unknown directory contents, including databases displaced during restore,
prevent age deletion. Restore writes its blocking state before touching live
data and revokes all recorded ready attempts.

Relocated/symlinked backup roots remain usable by the existing backup producer,
but receive only a blocking sidecar and no age-expiry receipt. This preserves
upgrades on installations that moved their backup directory to another disk.

## Validation

- Real SQLite migration to readiness to expiry, including unchanged restarts.
- Strict 72-hour boundary, offset-aware timestamps, future/invalid timestamps.
- Last-copy expiry and interaction with the existing count bound.
- Migration/import failure, crash-before-confirmation, unrelated startup,
  repeated partial failures, and malformed/legacy metadata fail closed.
- Restore invalidation, displaced data preservation, and lock contention.
- Manual files, JSON backups, symlinks, and the live database are untouched.
- Startup/hourly maintenance continues with trace retention disabled and does
  not block readiness or outlive the controller's worker shutdown.
- A real lock miss at readiness is retried by controller maintenance using the
  same attempt ID and original timestamp; another process's validation or a
  newer attempt with identical revisions cannot borrow that readiness.
- Hermetic state roots, focused regression tests, changed-file lint, exact-head
  Codex review and CI. No local production mutation or automatic merge.

Local validation: 326 tests and four subtests passed across the focused expiry,
backup, migration/import, startup, service-lock, controller readiness/retention,
and development-state guard suites. Two Windows byte-range lock tests were
skipped on macOS. Changed-file Ruff and whitespace checks passed. The read-only
architecture review's exact-startup identity and deferred-readiness findings
are covered by the process-local attempt report and real contention/maintenance
consumer tests. Required remote CI and exact-head review are still separate
delivery gates.

## Progress

- [x] Inspect backup, migration/import, readiness, restore, and maintenance owners.
- [x] Implement durable lifecycle admission and 72-hour expiry.
- [x] Wire producers, readiness, restore protection, and maintenance.
- [x] Add focused regression and lifecycle integration coverage.
- [x] Complete focused local validation and independent architecture feedback.
- [ ] Publish a non-draft PR and satisfy exact-head review/CI gates.
