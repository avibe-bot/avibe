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
- Hermetic state roots, focused regression tests, changed-file lint, exact-head
  Codex review and CI. No local production mutation or automatic merge.

## Progress

- [x] Inspect backup, migration/import, readiness, restore, and maintenance owners.
- [ ] Implement durable lifecycle admission and 72-hour expiry.
- [ ] Wire producers, readiness, restore protection, and maintenance.
- [ ] Add focused regression and lifecycle integration coverage.
- [ ] Validate and publish a non-draft PR; retain review/CI observation.
