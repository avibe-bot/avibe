# Memory Embedding Rebuild Recovery

> Historical design. Superseded by
> [`memory-unified-recovery-pr3.md`](./memory-unified-recovery-pr3.md); an
> identity-changing save now uses the confirmed unified reset without a rebuild
> marker or retry workflow.

> Status: implementation review
>
> Issue: #1314
>
> Target: `dev`

## Problem and goal

Changing an established embedding base URL or model can leave Memory fenced forever when vector-bearing
data already exists. The confirmed candidate must remain durable, existing Markdown must remain intact,
and the user must have one truthful Retry path.

This plan defines only that recovery path. It supersedes the older blanket deferment of embedding rebuild
in `everos-memory-adjustment.md`; it does not authorize a generic operation system or broad data repair.

## Contract

1. Any unconfirmed embedding identity write returns `memory_embedding_rebuild_required` and writes
   nothing. This includes the first non-empty `base_url` or `model`: it must follow the same confirmation,
   durable-intent, and rebuild path as a later identity change. API-key-only writes do not change identity.
2. A confirmed change atomically persists the candidate and `recovery_intent: rebuild` before destructive
   work. Memory config writes use one cross-process transaction so stale UI or Controller snapshots cannot
   overwrite a newer candidate or marker.
3. `MemoryRuntime` owns one retained, shielded rebuild task. Duplicate requests join it, concurrent Memory
   mutations return `memory_operation_in_progress`, and callers wait for a closed result rather than poll a
   job.
4. Artifact, endpoint, data-state, and quiesce checks complete before the existing sidecar is stopped.
   Empty data settles as `completed_empty`; non-empty data runs the pinned role-aware rebuild child.
5. Only `completed` and `completed_empty` clear the marker. `root_busy`, interruption, timeout, and other
   failures retain the candidate and marker so Retry is honest. Boot with a marker keeps claims fenced and
   never starts destructive work automatically.
6. The browser route accepts exactly `{"confirm": true}` through the existing CSRF and signed-user chain.
   Settings expose only `rebuild_required`, confirmation, Retry, and restart admission state; secrets,
   progress, job IDs, and operation history remain private or nonexistent.

## Boundaries

- Preserve Markdown and never mix old and new vector spaces.
- Do not fall back to Clear or factory reset.
- Do not add manual rebuild without a pending marker, automatic repair, `cascade sync`, `fix --apply`,
  generic operation records, polling, or EverOS source changes.
- Keep the implementation in the Memory config/runtime and existing UI/internal route boundaries.

## Evidence

- Scenario: `MEMORY-REBUILD-101`, `MEMORY-REBUILD-001`, and `MEMORY-REBUILD-201` cover the browser settings
  journey at the service boundary.
- Unit: config transaction, runtime fencing/preflight/settlement, retained-task concurrency, and UI state
  tests cover local invariants.
- Contract: internal client/server tests prove the exact signed rebuild request and closed response shape;
  child tests prove admitted argv and result mapping.
- Residual manual check: inspect the Settings confirmation, pending warning, Retry action, and disabled
  Restart state in the packaged UI. No live Memory service or network call is required for automated tests.
