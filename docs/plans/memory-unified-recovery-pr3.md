# Memory unified recovery PR 3

Status: implemented locally from `origin/dev` at
`f020c38b0d0199134e2910c91c01f27836ffb6c7`.

This plan replaces the active Memory recovery ladder with one non-destructive
availability operation and two explicitly authorized destructive intents. It is
the current contract for the PR 3 `recovery` and `fsm` workstreams.

## Product surface

- `Wake` validates or reinstalls the admitted artifact, stops the old owned
  process, starts the same EverOS root, and verifies native readiness with
  bounded backoff. Startup and unexpected exit use this path. It never deletes
  data.
- `Repair(confirm_loss)` exists only for `needs_repair`. It proves the old
  process tree stopped, preserves stable identity/config, performs the confined
  data reset, then calls Wake and requires native readiness.
- `Delete data(confirm_loss)` is a distinct explicit user intent using the same
  stop-before-delete and confined reset primitive.
- An Embedding identity change uses the same accepted-loss reset instead of a
  candidate plus rebuild marker.
- Status is one state in `disabled`, `starting`, `running`, `degraded`, or
  `needs_repair`, plus one sanitized reason across API, CLI, and UI.

Provider, credential, disk, and permission failures are `degraded`; they do not
authorize Repair. Failed or interrupted destructive operations retain no stage
or replay instruction. Startup reevaluates local state.

## Destructive boundary

Every route and client uses the exact `confirm_loss` field. The Controller also
checks it, acquires the process-level Memory operation lease, proves the old
owned sidecar and runtime stopped, then invokes a pathless deletion primitive.

That primitive can remove only:

- `<effective_home>/memory`, the native EverOS root and Memory attachment area;
- fixed, retired Clear journal/backup names under `state/memory`.

It cannot accept a caller path, follow symlinks, recursively erase an unproved
root, or remove `state/memory/memory.sqlite`. The identity store rotates its data
generation atomically while preserving scope identity, provider-root identity,
and the project catalog. Unsafe or foreign content fails closed and remains
visible in the operation response.

## Removed protocol

New code no longer writes or executes `MemoryConfig.recovery_intent`,
`MemoryRecoveryIntent`, `embedding_change_pending`,
`cloud.transition_rebuild_owned`, rebuild/factory-reset retry flags, retained
request owners, or cascade sync/rebuild children. Standalone Restart, Rebuild,
Clear, and Factory Reset routes, clients, controls, types, and scenario harnesses
are removed.

Released workflow fields collapse on load into the durable `repair_required`
compatibility fence. Ordinary saves preserve that fence until a successful
destructive reset clears it. Released Clear state is only classified; it never
authorizes deletion or resumes a workflow. No workflow journal, pending stage,
or fallback executor is introduced.

## Scenario contract

- `MEMORY-WAKE-001`, `MEMORY-WAKE-002`, and `MEMORY-WAKE-201`: Wake is
  non-destructive and bounded; unexpected exits re-enter Wake and external faults
  remain degraded.
- `MEMORY-REPAIR-201` through `MEMORY-REPAIR-206`: exact loss confirmation,
  eligibility, stop-before-delete, native readiness, no stage resume, and
  identity preservation.
- `MEMORY-DELETE-DATA-001` and `MEMORY-DELETE-DATA-002`: distinct user intent, exact
  confirmation, shared confinement, and truthful partial failure.

The `memory_rebuild_recovery`, `memory_factory_reset`, and
`memory_clear_recovery` catalogs are retired. Their process identity,
authorization, exclusion, and confinement assertions move to the unified
Repair/Delete-data coverage; stage continuation assertions are deleted.

## Validation

Focused unit and contract suites cover config compatibility, runtime Wake,
process ownership/reaping, Controller reset, internal/UI routes and clients, CLI
status, and native Processing Record. UI tests cover status, controls, exact
confirmation, copy, and settings identity changes. Ruff, compileall, Vitest,
UI lint/build, and `git diff --check` are required before the local commit.

Incus and full-repository regression remain residual checks for PR delivery and
are not required for this local preparation.
