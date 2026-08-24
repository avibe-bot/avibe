# Memory Host Architecture Review

Issue 1392 Rev 2 removes the backup/snapshot state machines and makes Clear Memory
Data a marker-owned, idempotent operation.

## Verified ownership

- `MemoryRuntime` owns lifecycle admission, fences, and boot reconciliation.
- `MemoryMaintenance` owns the clear-intent marker and calls exactly four deletion
  primitives: queue reset, provider-root recreation, confined legacy-file cleanup,
  and attachment clear.
- `MemoryProcessingRecord` projects the marker as `clear_in_progress`; failures expose
  only the failed marker state and error code.
- UI routes and the Web UI only forward Clear and display read-only marker state.

## Persistence contract

`state/memory/clear-intent.json` is atomically written with a same-directory temporary
file, file fsync, replace, and parent-directory fsync. The marker is the source of truth;
the legacy clear journal is migrated at boot when it contains an open operation and then
removed. Corrupt marker/journal state fences Memory projection without blocking service
startup. Legacy backup journals, backup directories, and snapshot directories are
best-effort boot cleanup only.

## Safety properties

The runtime holds the exclusive maintenance fence while deleting, sets the queue clear
fence before the first surface, and removes the marker only after every surface succeeds.
An interrupted or failed marker is retried on the next reconcile, so no restore path or
operator action can accidentally resurrect partially deleted data.
