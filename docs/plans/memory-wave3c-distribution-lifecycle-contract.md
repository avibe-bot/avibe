# Memory Wave 3c Distribution Lifecycle Contract

> Status: proposed; implementation and merge require owner/orchestrator approval
>
> Baseline: `origin/dev` at `d87a3941522e21dd1fe2f5973fac55e1b3b4c79a`
>
> Scope: Wave 3c Phase 0 specification only

## Decision

Wave 3c will make package readiness, mutation, rollback, restart, and recovery one
closed distribution-lifecycle contract. The contract has one cross-process
coordination primitive, `PackageLifecycleTransaction`, owned from admission to a
terminal state by the existing detached restart supervisor. Controller, Web,
CLI, and Settings submit an immutable intent; none of them acquires a package
lock, executes an install plan, owns a pending marker, or schedules a follow-up
restart.

Rollback for a split release will install the exact base core distribution
without the `memory` extra and, when present in the captured shape, install an
independent exact `avibe-memory` pin. This is the only model that both preserves
a mismatched pre-mutation shape and gives the resolver satisfiable requirements.
An executable rollback plan does not exist until the full requirement set has
resolved successfully before mutation.

This document reserves `MEMORY-INDEP-018` through `MEMORY-INDEP-021`. It does
not add their catalog entries or evidence yet.

## Background

PR #1736 established the current Wave 3b base. PR #1739 then attempted a narrow
Settings-only Python-distribution repair. It closed unmerged after five Codex
reviews across four heads and 15 review threads showed that repair cannot be
correct in isolation: readiness, rollback shape, cross-process admission, and
post-restart observation are one lifecycle contract.

The retained #1739 branch at
`b6793eae46127aa251b82a372d43904d1e5680ea` is recovery evidence, not an
implementation base. No code is cherry-picked from it.

## Scope

Wave 3c owns:

- packaged Memory readiness and probe ownership;
- one cross-process package transaction for automatic, Web, CLI, and Settings
  mutation entrypoints;
- exact forward and rollback package shapes through activation or rollback;
- state-derived UI recovery after the UI process restarts;
- the first non-bundled release dependency, Memory manifest ownership, publish
  ordering, and removal of the transition gate; and
- retirement or reconciliation of `MEMORY-INDEP-018-KBD-1`, `KBD-5`, `KBD-6`,
  and `KBD-10` through the contract below.

Non-goals:

- no `PluginHost`, plugin discovery, UDS/RPC service, or second Memory process;
- no generic lifecycle lock for start/stop, UI reload, QR, or Doctor;
- no caller-local package lock, pending-restart marker, acknowledgement handoff,
  or durable UI job protocol;
- no installation during Avibe startup;
- no Memory data/config migration or product workflow redesign; and
- no product, test, catalog, workflow, release, manifest, or config change in
  this Phase 0 PR.

If implementation needs a second coordination primitive or makes callers
coordinate install and restart themselves, the design has escaped this scope and
must return to owner review.

## Invariant 1: Readiness And Probe Ownership

`core.memory_loader` is the sole owner of the runtime entrypoint contract. It
exports a non-constructing probe that imports the fixed
`avibe_memory.runtime` entrypoint, compares its protocol to the host protocol,
and verifies that `create_memory_runtime` is callable. `load_memory_runtime`
and the probe share one private resolver. The probe never calls the factory or
constructs a runtime. No API, CLI, Doctor, or dependency-status module copies
the entrypoint name, protocol constant, or factory validation.

Every status path follows this order:

1. Read the persisted `memory_required` decision.
2. If that decision is unreadable, return a distinct fail-closed
   `memory_requirement_unreadable` error. It is neither `not_required` nor
   `ready`, imports zero `avibe_memory` implementation modules, and prevents
   every package-mutation intent until a readable decision is available.
3. If Memory is not required, return a `not_required` projection without
   importing any `avibe_memory` implementation module. Distribution metadata
   may be inspected with metadata APIs, but it makes no runtime-readiness claim.
4. If Memory is required on a packaged build, inspect distribution presence and
   version, call the loader-owned runtime probe, then import the separate
   `avibe_memory.artifact` contract.
5. Only after the artifact import succeeds may the artifact manager be asked for
   EverOS status.

For a required packaged installation, `memory-package` is `ready` if and only if:

- the `avibe-memory` distribution is installed;
- its readable normalized version equals the running Avibe release;
- the loader-owned runtime entrypoint probe succeeds; and
- the artifact contract imports successfully.

Version mismatch takes precedence over import errors. Missing distribution and
unreadable metadata retain distinct machine reasons. A later artifact-manager
or EverOS status failure affects only `memory-runtime`; it cannot reclassify an
importable Python distribution. A required non-ready package remains eligible
for the centrally admitted repair action.

Source/unpublished builds never advertise package mutation. They still use the
loader when Memory is enabled, but package status must not imply that a source
tree can be repaired through a package manager.

`MEMORY-INDEP-021` reserves the executable invariant: a disabled or otherwise
not-required packaged installation imports zero `avibe_memory` implementation
modules, including both `runtime` and `artifact`, while metadata-only inspection
remains allowed.

## Invariant 2: Resolver-Satisfiable Rollback

The transaction captures the complete pre-mutation package shape before it
builds or executes a forward plan:

- running core version, distribution name, and service launcher;
- bundled/pre-split versus split release family;
- whether `avibe-memory` is installed; and
- the exact normalized Memory version when installed.

A split-release rollback has these requirement shapes:

| Captured shape | Exact rollback requirements |
| --- | --- |
| Core only | base core pin; explicitly uninstall any split Memory introduced by the forward mutation; verify it is absent |
| Optional-era core plus Memory, matching versions | base core pin plus independent exact Memory pin |
| Optional-era core plus Memory, mismatched versions | base core pin plus the independently captured exact Memory pin |
| Hard-dependency transition core plus mismatched Memory | no plan; the captured shape is resolver-inconsistent and fails closed |

Core-only rollback is an active cleanup operation, not merely omission of a
Memory requirement. Its resolved plan installs the base core pin, explicitly
uninstalls any `avibe-memory` distribution left by the forward mutation, and
verifies that distribution metadata is absent. Cleanup or absence-verification
failure makes rollback fail; residue may not be reported as restored because it
would pollute later readiness and admission decisions.

The core requirement deliberately has no `[memory]` extra. The extra expresses
a forward matched-release policy and would constrain a mismatched rollback back
to equality, making the recorded target unsatisfiable. Forward installs may use
the extra; rollback restores distributions independently.

The alternative, failing closed on every core/Memory mismatch, is rejected. It
is simpler in the planner but strands optional-era broken shapes that Settings
is supposed to repair, encouraging manual package mutation outside the
transaction. The chosen model is already supported by current pip and uv plan
forms: a base core requirement can be combined with a separately pinned Memory
requirement. The one transition release is deliberately stricter: its base core
metadata hard-pins Memory, so a different Memory pin is inherently
unsatisfiable and must be rejected rather than disguised with `--no-deps`.

Fail closed before mutation when the shape cannot be expressed exactly: the
core release or distribution is not publishable, installed Memory metadata is
missing or invalid, a hard-dependency transition target is mismatched, or a
pre-split bundled target also claims a separate Memory distribution. A passable
target can never contain `memory_package=True, memory_version=None`.

There are two types, not one partially valid plan:

- `CapturedPackageShape` is the immutable observation taken before mutation.
- `ResolvedRollbackPlan` is created only after the exact requirement set and all
  needed artifacts resolve into transaction-owned staging.

The constructor for `ResolvedRollbackPlan` is private to the lifecycle owner.
No mutation starts unless that object exists. This makes the hard invariant
literal: every constructed rollback plan is resolver-satisfiable and can run
without discovering a new package-index dependency.

Legacy transitions use an explicit family rule. A captured pre-split release is
restored as its exact legacy core distribution with no split Memory package.
The transaction stages the complete legacy install before mutation, stops the
failed generation, removes overlapping split-package ownership, and installs
from the staged set. A split-era target is restored with the independent pins
above. Older planners that cannot serialize Memory shape are covered by the
first non-bundled release dependency described under Migration.

Failure semantics are closed:

- capture or resolution failure rejects the request with no mutation;
- install failure after mutation begins runs the already-resolved exact rollback;
- activation or health failure restores the exact package shape, database
  recovery point, and captured launcher through the supervisor's existing path;
- a killed supervisor leaves its last state and resolved target in the existing
  restart status/audit record; the next lifecycle owner reconciles that record
  and current installed metadata before admitting a new forward mutation; and
- no recovery step substitutes an unpinned install or recomputes captured facts
  from the replacement release.

`MEMORY-INDEP-019` reserves the shape and failure matrix, including core-only,
matching split packages, recoverable optional-era mismatch, rejected transition
mismatch, unreadable metadata, pre-split rollback, resolver failure, partial
mutation, and activation failure.

## Invariant 3: One Server Admission Owner

`PackageLifecycleTransaction` is the only cross-process coordination primitive.
It is implemented at the existing detached restart-supervisor boundary, which
already survives replacement of the service/UI processes and owns activation
and rollback.

Controller automatic/IM upgrade, Web upgrade, CLI upgrade, and Settings Memory
repair submit an intent such as `upgrade` or `repair_memory_package`. They do
not build or execute an install plan. The supervisor process starts from the
pre-mutation launcher, acquires the single package-lifecycle OS reservation, and
returns a one-shot admission receipt to the submitting caller. The supervisor,
not the caller, then captures shape, resolves both directions, mutates packages,
stops and starts the runtime when required, verifies the result, and releases
the reservation. Concurrent supervisors can be spawned, but exactly one can be
admitted; losers return a stable busy result without touching packages.

The reservation is held by the supervisor from `admitted` through a terminal
state and is inherited by its package-manager child so a supervisor crash cannot
make a still-running mutation appear unlocked. The existing restart status and
audit log carry the transaction id, captured shape, resolved rollback identity,
and last transition for crash recovery. They are written only by the lifecycle
owner. This is not a second UI job store or a caller-visible pending protocol.

### Reservation Mechanism

`PackageLifecycleTransaction` uses exactly one lock file,
`package-lifecycle.lock`, under the Avibe runtime directory. On Unix the
supervisor holds one advisory lock on its open file descriptor and the
package-manager child inherits that same descriptor. On Windows the supervisor
holds the equivalent exclusive file lock on one inheritable handle, which the
package-manager child likewise inherits. Unrelated child processes inherit
neither descriptor nor handle.

The only test for "no live reservation" is a nonblocking acquisition attempt on
that same lock file: success means no transaction owns it, while contention
means a live transaction still owns it. PIDs, status timestamps, and audit
records are recovery evidence, never reservation liveness. These platform
implementations are the same single `PackageLifecycleTransaction` primitive.
A second lock, heartbeat, ownership token, or lease/renewal protocol is
forbidden.

State transitions are:

```text
requested
  -> rejected | busy
  -> admitted -> captured -> resolved -> mutating
  -> activating -> verifying -> succeeded
  -> rolling_back -> restored | failed
```

`rejected`, `busy`, `succeeded`, `restored`, and `failed` are terminal. Failures
before `mutating` leave the installation untouched. Failures from `mutating`
onward cannot release the reservation until exact rollback reaches a terminal
state. Stale status with no live reservation is recovery input, never proof of
success and never permission to overwrite it with a new forward request.

Admission is also the server-side policy boundary:

- source/unpublished deployments reject every package-mutation intent before
  capture, resolution, or subprocess execution;
- an unreadable persisted `memory_required` decision rejects every
  package-mutation intent with `memory_requirement_unreadable` before optional
  imports or subprocess execution;
- repair of a running Memory installation is accepted only as a transaction
  whose supervisor owns quiescence, mutation, and restart, eliminating the
  frontend status-check TOCTOU; and
- automatic, Web, CLI, and Settings requests receive the same busy, unsupported,
  failure, and terminal result shapes.

This replaces `MEMORY-INDEP-018-KBD-10`. Operators do not overlap package
mutations during rollout; after an active transaction settles they may retry the
same intent. The implementation must not reproduce PR #1710's spread: no lock
acquisition in callers, no pending marker, no UI-reload handoff, and no QR,
Doctor, or ordinary lifecycle command taught to coordinate package ownership.
Their existing behavior is outside this contract; only a package transaction's
own restart belongs to this supervisor state machine.

`MEMORY-INDEP-020` reserves multiprocess admission evidence: simultaneous
requests from all four entrypoint classes admit exactly one owner through
restart, source deployments reject server-side, and owner death either recovers
the recorded transaction or fails closed before another mutation.

## Invariant 4: UI Recovery Is State-Derived

The existing dependency-install deadline remains the only UI time budget. Every
poll request is inside a `try`/`catch`; there is no naked awaited poll. A network
disconnect, UI restart, transient 404, or temporary 5xx records the last error,
sleeps, and retries until the same deadline instead of rejecting the whole
Settings action.

When the replacement UI is reachable, the dependency-specific job adapter first
returns a live in-memory job unchanged. If that job disappeared with the old UI
process, `memory-package` alone re-runs the offline/read-only dependency status.
It returns synthetic terminal success with the original job/dependency identity
only when the complete required readiness invariant above is currently true.
Otherwise it preserves `job_not_found` and the current non-ready reason. Other
dependencies do not gain this recovery behavior.

At the deadline, the UI performs one final read-only dependency refresh. A ready
Memory package converges to recovered success. If the refresh remains non-ready
and polling observed a structured terminal job failure, that result and its
machine reason are returned unchanged. If the deadline was exhausted by
transport failures without any structured terminal result, the UI returns
`status=failed`, reason `dependency_poll_transport_exhausted`, and carries the
sanitized last transport error in `last_transport_error`. A reachable
`job_not_found` plus a non-ready state is a structured job failure, not transport
exhaustion. The UI never reads lifecycle status, never acknowledges a restart,
and does not require a durable job, pending flag, or delayed restart.

This continues `MEMORY-INDEP-018`: packaged Settings repair remains observable
through the all-process restart, poll transport failures are contained, and the
result converges on installed state rather than process-local job memory.

## Scenario And Evidence Matrix

| Scenario | Contract | Required automated evidence | Packaged/operational evidence |
| --- | --- | --- | --- |
| `MEMORY-INDEP-018` | Upgrade/repair preserves package shape; UI polling survives restart and recovers from current readiness | Poll truth table distinguishes transport exhaustion from structured failure; dependency-specific recovery route; exact-head package planner/rollback tests | Real core and Memory wheels: Settings repair, enabled upgrade, all-process restart, recovery, and rollback |
| `MEMORY-INDEP-019` | Every rollback plan is exact and resolver-satisfiable | Shape property table, explicit core-only residue cleanup, private resolved-plan construction, full-tree caller inventory, failure injection | Wheelhouse matrix for core-only, matching, optional-era mismatch, rejected transition mismatch, pre-split, resolver failure, and activation rollback |
| `MEMORY-INDEP-020` | One supervisor-owned admission/reservation spans mutation through restart | Real multiprocess contention across controller/Web/CLI/Settings adapters, source rejection, cross-platform inherited-lock and nonblocking no-owner probes, crash/recovery state transitions | Packaged concurrent requests admit one transaction; service health and exact package shape verified after success/rollback |
| `MEMORY-INDEP-021` | Not-required status imports no optional implementation | Subprocess import guard for disabled/not-required/unreadable-decision status; loader probe contract and non-construction tests | Packaged disabled/core-only smoke with blocked `avibe_memory` imports |

Scenario IDs must be visible in their executable test names and in the Memory
independence catalog when implementation begins. Release/migration guards must
scan the shipped tree and published artifact set rather than a curated file list.

## Migration And Release Ordering

The first release whose core wheel no longer bundles Memory is a transition
release. Its base `avibe-os` metadata has an exact hard dependency on the matched
`avibe-memory` release, regardless of whether Memory is currently enabled. This
is required because a pre-Wave-3c upgrader cannot submit the new transaction or
preserve optional package shape. No startup path installs a package.

Publication order is fixed:

1. Build and verify the `avibe-memory` wheel/sdist and its Memory-owned EverOS
   manifest/assets.
2. Publish those artifacts and prove their public URLs, hashes, and resolver
   availability.
3. Build the transition `avibe-os` artifacts with the exact hard dependency and
   with no Memory implementation or EverOS manifest in the core wheel.
4. Publish core only after the Memory availability gate passes.
5. Keep the transition gate until packaged upgrade and rollback evidence covers
   every supported pre-split and split origin.
6. In a later release, remove the base hard dependency and retain the matched
   `avibe-os[memory]` extra after owner approval of the gate evidence.

This retires `KBD-1` through the hard transition dependency, `KBD-5` through
fully staged legacy rollback, and `KBD-6` through transition availability plus
explicit split-era shape capture. `KBD-10` is replaced by the single admission
owner. Release manifests move with the distribution that owns their artifacts;
the core release consumes verified published availability and never publishes a
manifest that points at a draft/private or differently versioned asset.

Compatibility is one-way during the transition: old releases can upgrade
because the hard dependency is ordinary package metadata; the Wave 3c
transaction can roll back because it captures the old distribution name,
launcher, release family, and exact Memory presence/version before mutation.
Data and config formats do not change.

## Recovery Inventory From PR #1739

The retained branch is inspected behavior by behavior:

| Retained evidence | Wave 3c disposition |
| --- | --- |
| Loader-owned non-constructing runtime entrypoint probe | Reconcile into Invariant 1; preserve one protocol owner and add the not-required import fence |
| Fail-closed rollback target for unreadable Memory metadata | Reconcile into Invariant 2; retain fail-closed unreadable metadata while replacing mismatch dead-end semantics with independent exact pins |
| Structured API and CLI mapping for unsafe rollback metadata | Reuse the machine semantics at transaction adapters; callers only project the owner's result |
| `MEMORY-INDEP-018` packaged Settings repair and state-derived missing-job recovery | Reconcile into Invariant 4 and the expanded packaged matrix; do not copy process-local polling assumptions |

No commit from `b6793eae46127aa251b82a372d43904d1e5680ea` is cherry-picked. Each retained
behavior is re-derived from this contract and current `origin/dev`.

## Phase Gates

1. **Phase 0, this PR:** approve this document. No implementation and no spec PR
   merge occur without explicit owner/orchestrator approval.
2. **Contract owners:** land loader readiness and captured/resolved rollback
   types with `MEMORY-INDEP-019` and `021`; no caller-local coordination.
3. **Lifecycle owner:** move all four mutation entrypoint classes behind
   `PackageLifecycleTransaction` in one bounded vertical delivery with
   `MEMORY-INDEP-020`.
4. **Recovery:** land the dependency-specific UI/API convergence and packaged
   `MEMORY-INDEP-018` closed loop.
5. **Release transition:** move manifest ownership, publish Memory first, ship
   the hard-dependency transition core, then remove the release gate only after
   all packaged matrices and migration guards pass.

Each implementation PR must use exact packaged artifacts where package shape is
the behavior, keep the local Avibe service untouched, and stop if ownership
spreads beyond the named loader, lifecycle transaction, or dependency recovery
adapter.
