# Memory Clear Durable Intent

This document consolidates issue #1392 Rev 2 and the owner-approved Rev 2.1
amendment. It is the implementation contract for Memory Clear.

## Decision

Clear Memory Data is an irreversible, idempotent re-run. The backup/restore and
Clear snapshot stacks are removed. There is no user-facing Resume or Abort
choice: a Clear written by this implementation continues automatically until it
finishes.

The implementation keeps and orchestrates these four high-level deletion
primitives; it does not replace them with raw directory deletion:

1. queue: clear queue tables and call `store.reset_for_clear(target_epoch=...)`
2. provider: call `ProviderRootOwner.recreate_empty()`
3. call log: call `clear_call_log()`
4. attachments: call `MemoryModule.clear_attachments()`

The confined filesystem primitives remain shared infrastructure for Clear,
Reset, and artifact installation.

## Durable marker

The single authority is `<effective_home>/state/memory/clear-intent.json`. It is
written before the first surface deletion and removed only after all four
surfaces complete.

Schema version 1 contains:

- `operation_id`: a new UUIDv4
- `operator_ref`: the initiating operator provenance
- `pre_epoch` and `target_epoch`
- `state`: `deleting` or `failed`
- nullable `error_code`
- `created_at` and `updated_at`

`target_epoch` is fixed at marker creation and never recomputed. Replays pass
that value to the queue reset, whose two-state acceptance allows either
`pre_epoch` or `target_epoch` and prevents a crash from advancing the epoch
twice.

Every create or update uses a same-directory temporary file, file fsync,
atomic replace, and parent-directory fsync. Marker operations run under the
existing maintenance lifecycle lock and `MemoryOperationLease`; there is no
cross-process protocol beyond same-host lease mutual exclusion.

The marker is the source of truth for an open Clear. The queue's
`memory_meta.clear_in_progress` flag is set after the marker is durable. On
completion the four surfaces finish, the queue fence is released, and the
marker is removed. A crash in a terminal step remains recognizable and is
settled on the next reconcile.

## Boot and admission

Boot reconcile keeps Memory fenced whenever a readable marker exists. A marker
written by this implementation is changed from `failed` to `deleting`, the four
idempotent primitives run again, and Memory is unfenced only after terminal
settlement. A failed sweep persists a failed marker and leaves Memory fenced;
service startup continues.

An explicit user Clear may replace an unreadable marker or the failed marker
created for legacy state. It derives a fresh epoch pair from the current store,
writes a new UUIDv4 operation, and converges through the normal four-surface
sweep.

## Fault model

### Tier 1: exact recovery required

- Process crash or power loss at any instruction boundary, assuming POSIX
  same-directory rename atomicity and the marker fsync order above.
- Marker durability failures, including fsync, rename, unlink, and parent
  directory sync failures. Memory fails closed through a persisted failed
  marker when writable, or an in-memory authority error otherwise; service
  startup proceeds.
- Same-host mutual exclusion through the maintenance lifecycle lock and
  `MemoryOperationLease`. A lease loser defers and does not mutate shared state.

### Tier 2: fail-closed is sufficient

- Any corrupted, truncated, oversized, type-invalid, or otherwise
  uninterpretable marker or legacy journal content is refused. Memory is
  fenced, the failed projection is surfaced, and an explicit Clear replaces
  the state. The implementation does not distinguish corruption causes,
  preserve corrupt fields, or enumerate malformed shapes beyond one bounded
  read and schema check.
- Filesystem anomalies under `state/memory/`, including dangling symlinks,
  permission errors, and files disappearing during a read, follow the same
  unreadable-state rule.

### Tier 3: out of scope or best effort

- Legacy backup/restore residue and Clear snapshot cleanup is best effort:
  warn and continue without blocking Memory or service startup.
- Adversarial local tampering beyond Tier 2 refusal is out of scope because the
  local user owns these files.
- Behavior of foreign or older processes beyond lease mutual exclusion is out
  of scope.
- Byte-perfect semantic migration of the legacy Clear journal is not provided.

## Legacy Clear journal

There is no semantic migration and no preservation of legacy `operation_id`,
`operator_ref`, or `target_epoch`. The implementation does not validate legacy
state or resolution vocabularies, row cardinality, surface receipts, or field
types beyond this bounded probe:

1. Detect `clear-journal.sqlite` with `lexists`, so dangling symlinks count.
2. Attempt one bounded query asking whether any row has a non-null open slot.
3. If the query succeeds with no open row, best-effort confined-delete the
   journal and do not fence.
4. If the query fails for any reason or finds any open row, write a fresh failed
   marker with a new UUIDv4, epochs derived from current `memory_meta`, and
   `memory_clear_legacy_state_requires_rerun`; then best-effort delete the
   journal. Boot does not replay this marker. An explicit Clear replaces it.
5. If a marker and journal both exist, the marker wins and the journal is
   best-effort deleted.

`memory_clear_legacy_abort_unsupported` and all abort-specific handling are
removed.

## API, UI, and projection

The Resume and Abort methods and routes are removed across runtime,
maintenance, internal server/client, UI routes, authorization, API context, and
frontend types. A stale page receives 404 and refreshes into the current shape.

Processing Record replaces `clear_recovery` with:

```json
{
  "clear_in_progress": {
    "operation_id": "uuid",
    "state": "deleting",
    "occurred_at": "2026-08-13T00:00:00Z",
    "error_code": null
  }
}
```

`state` may be `deleting` or `failed`. Marker read failure projects `failed`
with `memory_clear_marker_unreadable`. A row appears in the failures projection
only when its state is `failed`. Backend, UI, and tests consume this one shape.

English and Chinese copy must include
`memory_clear_legacy_state_requires_rerun` and must not retain the abort-specific
error.

## Persisted residue

The retired backup/restore journal, backup directory, and Clear snapshot
directory are removed on boot with confined best-effort deletion. Failure logs
a warning and does not fence Memory or fail service startup.

## Required evidence

- `MEMORY-CLEAR-201`: Clear discards retained ambiguous local evidence through
  the four deletion primitives and permits later delivery.
- `MEMORY-CLEAR-202`: a marker written by this implementation is retried
  automatically on boot.
- The marker is durable before the first delete and survives interruption after
  any surface.
- Re-run does not advance the epoch twice, each primitive is repeatable, and
  marker removal occurs only after all surfaces.
- Terminal marker-removal failure stays recognizable and converges.
- Corrupt markers fail closed without stopping service startup.
- The legacy probe distinguishes only open versus no-open; probe failure and an
  open row create the fresh rerun-required marker, while terminal residue does
  not fence.
- Lease losers defer without mutation and legacy cleanup remains best effort.

Four-platform Incus verification is a residual post-merge check.
