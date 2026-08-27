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
CLI, and Settings submit an immutable intent with a caller-generated
`intent_id`; none of them acquires a package lock, executes an install plan,
owns a pending marker, or schedules a follow-up restart. Before mutation the
supervisor resolves one in-process execution bundle containing every command and
datum needed for forward execution, activation, verification, and rollback. The
lifecycle owner is the sole writer of a schema-versioned, package-specific
transaction record; ordinary restarts retain their separate existing status.

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
- no generic lifecycle lock for start/stop, UI reload, QR, or Doctor; the only
  bounded exception is the ordinary `schedule_restart` entrypoint's one
  nonblocking, non-owning reservation-active probe before it touches restart
  state or processes;
- no caller-local package lock, pending-restart marker, acknowledgement handoff,
  or durable UI job protocol;
- no retained intent index or second transaction record; the one dedicated
  package record and the existing ten retained audit logs are the complete
  idempotency window;
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

1. Load the persisted configuration through `V2Config` and read its
   `memory_required` decision.
2. If only the optional Memory section is malformed, preserve `V2Config`'s
   safe-degraded behavior: disable Memory, retain its warning, and treat the
   recovered decision as readable `not_required`. This path imports zero
   `avibe_memory` implementation modules and does not block an unrelated core
   upgrade.
3. Only when the persisted configuration as a whole cannot be loaded safely,
   return the distinct fail-closed `memory_requirement_unreadable` error. It is
   neither `not_required` nor `ready`, imports zero `avibe_memory`
   implementation modules, and prevents every package-mutation intent until the
   configuration is readable.
4. If Memory is not required, return a `not_required` projection without
   importing any `avibe_memory` implementation module. Distribution metadata
   may be inspected with metadata APIs, but it makes no runtime-readiness claim.
5. If Memory is required on a packaged build, inspect distribution presence and
   version, call the loader-owned runtime probe, then import the separate
   `avibe_memory.artifact` contract.
6. Only after the artifact import succeeds may the artifact manager be asked for
   EverOS status.

For a required packaged installation, `memory-package` is `ready` if and only if:

- the `avibe-memory` distribution is installed;
- its readable normalized version equals the running Avibe release;
- the loader-owned runtime entrypoint probe succeeds; and
- the artifact contract imports successfully.

The provider-cardinality rule under Invariant 2 applies before choosing that
installed version. More than one canonical `avibe-memory` metadata provider is
non-ready `status=error`, reason `memory_package_metadata_ambiguous`; status may
not select whichever provider metadata APIs return first.

Version mismatch takes precedence over import errors. Missing distribution and
unreadable metadata retain distinct machine reasons. A later artifact-manager
or EverOS status failure affects only `memory-runtime`; it cannot reclassify an
importable Python distribution. A required non-ready package remains eligible
for the centrally admitted repair action.

Source/unpublished builds never advertise package mutation. They still use the
loader when Memory is enabled, but package status must not imply that a source
tree can be repaired through a package manager.

`MEMORY-INDEP-021` reserves the executable invariant: a disabled or otherwise
not-required packaged installation, including a safely degraded malformed
Memory section, imports zero `avibe_memory` implementation modules, including
both `runtime` and `artifact`, while metadata-only inspection remains allowed.

## Invariant 2: Resolver-Satisfiable Rollback

The transaction captures the complete pre-mutation package shape before it
builds or executes a forward plan:

- running core version, distribution name, and service launcher;
- bundled/pre-split versus split release family;
- every visible distribution metadata provider whose canonicalized name is
  `avibe-memory`; and
- the exact normalized Memory version when exactly one such provider is
  installed.

Memory provider cardinality is part of the captured shape, not an implementation
detail. Only cardinality zero or one is valid. Two or more canonical
`avibe-memory` dist-info providers are ambiguous even when their recorded
versions match: capture fails closed before plan construction or mutation. No
readiness, admission, or rollback path may select one provider from that set.

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
missing or invalid, canonical Memory provider cardinality exceeds one, a
hard-dependency transition target is mismatched, or a pre-split bundled target
also claims a separate Memory distribution. A passable target can never contain
`memory_package=True, memory_version=None`.

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
failed generation, explicitly uninstalls the failed replacement `avibe-os`
distribution and every split `avibe-memory` provider, and installs from the
staged set. Success requires a post-install provider scan proving that the
replacement core metadata/provider and every split Memory metadata provider are
absent. A split-era target is restored with the independent pins above; its
cleanup also re-enumerates canonical Memory providers and requires the resolved
cardinality and exact version. Cleanup or cardinality verification failure is a
rollback failure. Older planners that cannot serialize Memory shape are covered
by the first non-bundled release dependency described under Migration.

Failure semantics are closed:

- capture or resolution failure rejects the request with no mutation;
- install failure after mutation begins runs the already-resolved exact rollback;
- activation or health failure restores the exact package shape, database
  recovery point, and captured launcher through the supervisor's existing path;
- a killed supervisor leaves its last state and resolved target in the dedicated
  package transaction record and existing audit log; a same-intent recovery
  owner reconciles that record and current installed metadata before any later
  forward mutation can be admitted; and
- no recovery step substitutes an unpinned install or recomputes captured facts
  from the replacement release.

`MEMORY-INDEP-019` reserves the shape and failure matrix, including core-only,
matching split packages, recoverable optional-era mismatch, rejected transition
mismatch, unreadable metadata, duplicate canonical Memory providers, pre-split
rollback with replacement-core uninstall verification, resolver failure,
partial mutation, and activation failure.

## Invariant 3: One Server Admission Owner

`PackageLifecycleTransaction` is the only cross-process coordination primitive.
It is implemented at the existing detached restart-supervisor boundary, which
already survives replacement of the service/UI processes and owns activation
and rollback.

Controller automatic/IM upgrade, Web upgrade, CLI upgrade, and Settings Memory
repair generate `intent_id` before their first request and submit it with an
intent such as `upgrade` or `repair_memory_package`. They do not build or execute
an install plan. The supervisor process starts from the pre-mutation launcher,
acquires the single package-lifecycle OS reservation, and records the admitted
identity in `package_lifecycle_transaction.json` under the Avibe runtime
directory before returning its one-shot admission receipt. That file is
exclusive to package transactions and has one writer, the lifecycle owner;
ordinary `schedule_restart` continues to own `restart_status.json` and cannot
overwrite the package projection. The supervisor, not the caller, then captures
shape, resolves both directions, mutates packages, stops and starts the runtime
when required, verifies the result, and releases the reservation. Concurrent
supervisors can be spawned, but exactly one can be admitted; losers return a
stable busy result without touching packages.

### Transaction Record And Bounded Idempotency

The canonical package transaction record starts at `schema_version: 1`. Its
loader obeys these compatibility rules before interpreting any transition:

- a released `restart_status.json` or package record with no schema version, or
  a supported older schema, loads as read-only legacy recovery input; missing
  `intent_id`, captured-shape, or resolved-plan fields never make the record
  unloadable and the loader never rewrites that legacy source in place;
- recognized legacy facts are reconciled with current installed metadata and
  reservation liveness before a version-1 record may be created for a later new
  intent; and
- a record with an unknown higher schema version loads only as fail-closed
  recovery input with reason `transaction_record_version_unsupported`. It is
  never overwritten, and every new forward admission is rejected until code
  that understands the schema can reconcile it.

`MEMORY-INDEP-020` includes released `restart_status.json` fixtures with no
`schema_version`, `intent_id`, captured shape, or resolved rollback identity, and
proves they load without rejection while remaining read-only recovery input.

`intent_id` is an opaque, collision-resistant transaction identity and
idempotency key. The guarantee is deliberately finite: an identity is resolvable
only while it is present in the dedicated current transaction record or one of
the existing ten retained transaction audit logs. Those audit entries retain
the schema version, identity, canonical-intent digest, and terminal projection
needed for lookup; there is no separate retained index. Re-submitting a
resolvable identity and the same canonical intent returns its active or terminal
projection and cannot repeat forward mutation. Reusing it with different intent
content is rejected as an identity conflict.

An identity outside that retention window returns stable `intent_unknown`; it is
never interpreted as a new request and cannot mutate packages. The caller first
reads current dependency and owner state, then mints a new identity for an
explicit retry only when no active transaction remains. There is no new
database, job store, pending file, acknowledgement record, or caller-owned
coordination state.

### Same-Intent Recovery Election

A same-intent resubmission is also the only recovery-election request. When its
record is nonterminal and the reservation is live, it returns the current
projection. When the recorded supervisor and reservation are no longer live,
resubmission of that same identity makes one nonblocking attempt on the existing
package reservation. Exactly one contender can become recovery owner; losers
return the current nonterminal/busy projection. This is the same reservation,
not a lease, token, or second coordination primitive.

The elected owner must not execute forward mutation. It first reconciles current
installed metadata against the recorded `CapturedPackageShape` and
`ResolvedRollbackPlan`. If the resolved bundle and staged artifacts remain
complete, it executes only the recorded exact rollback and ends `restored`. If
required staging is missing or invalid, it fails closed as terminal `failed`
with reason `recovery_staging_unavailable`; it never resolves an unpinned
replacement from the network. This preserves at-most-once forward mutation. A
new forward request is admissible only after the recovered record is terminal,
and it must carry a newly minted identity.

The reservation is held by the supervisor from `admitted` through a terminal
state and is inherited by its package-manager child so a supervisor crash cannot
make a still-running mutation appear unlocked. The dedicated package record and
existing audit log carry `schema_version`, `intent_id`, captured shape, resolved
rollback identity, and last transition for crash recovery. They are written only
by the lifecycle owner. This is not a UI job store or a caller-visible pending
protocol.

### Pre-Mutation Execution Bundle

At `resolved`, the supervisor has created one immutable execution bundle in its
own process. The bundle contains the fully resolved forward and rollback package
commands, staged artifact paths and hashes, uninstall and post-cleanup provider
checks, stop/start commands and environments, captured launcher, health targets,
timeouts, and all data needed to project a terminal result. The supervisor also
imports every internal helper and standard-library module that bundle execution
can call before entering `mutating`.

Every external command in the resolved bundle carries an immutable
`execution_timeout_seconds` enforced by its child-side deadline runner, not only
by the supervisor waiting for it. The staged runner owns the package-manager
process group on Unix or Job Object on Windows, inherits the one reservation
descriptor/handle, terminates its command tree at the recorded deadline, and
then exits so the operating system releases the reservation even when the
supervisor has died. It cannot renew the deadline, start recovery, or write the
transaction record. The deadline runner is only a command executor already
captured in the bundle, not an external lifecycle bootstrap or owner. There is
no external reaper, heartbeat, lease, or second coordination primitive; after
release, the next same-intent submission follows the recovery election above.

From `mutating` onward, the process may execute only bundle-held commands and
data plus standard-library code imported before mutation. A fail-loud guard
rejects any later Python import or package/resource read from the environment
being replaced. Rollback uses the same pre-resolved bundle; it never imports or
reads code from the failed or restored generation. Post-mutation activation and
health are observed only through external child processes and bounded HTTP
probes whose results return as data. Tests must inject attempted imports and
resource reads into every post-mutation phase and prove the guard fails rather
than mixing release generations.

An external bootstrap is rejected: it would introduce a second executable
owner, packaging/versioning contract, and recovery handoff. The frozen
same-process bundle keeps the supervisor, reservation, state machine, and
rollback authority as the single `PackageLifecycleTransaction` primitive.

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
stale nonterminal + same-intent election -> recovering -> restored | failed
```

`rejected`, `busy`, `succeeded`, `restored`, and `failed` are terminal. Failures
before `mutating` leave the installation untouched. Failures from `mutating`
onward cannot release the reservation until exact rollback reaches a terminal
state. Stale status with no live reservation is recovery input, never proof of
success and never permission to overwrite it with a new forward request. It can
be advanced only by same-intent recovery election.

Admission is also the server-side policy boundary:

- source/unpublished deployments reject every package-mutation intent before
  capture, resolution, or subprocess execution;
- a safely degraded malformed optional Memory section is readable disabled state
  and does not block an unrelated core mutation, while a configuration that
  cannot be loaded safely as a whole rejects every package-mutation intent with
  `memory_requirement_unreadable` before optional imports or subprocess
  execution;
- repair of a running Memory installation is accepted only as a transaction
  whose supervisor owns quiescence, mutation, and restart, eliminating the
  frontend status-check TOCTOU; and
- automatic, Web, CLI, and Settings requests receive the same busy, unsupported,
  failure, and terminal result shapes.

This replaces `MEMORY-INDEP-018-KBD-10`. Operators do not overlap package
mutations during rollout; after an active transaction settles they may retry the
same intent. The implementation must not reproduce PR #1710's spread: no lock
acquisition in callers, no pending marker, no UI-reload handoff, and no QR or
Doctor coordination.

Ordinary restarts have one bounded safety check at their existing single
`schedule_restart` server entrypoint. Before writing `restart_status.json` or
touching a process, that entrypoint performs one nonblocking, read-only
reservation-active probe. Contention returns structured `busy` immediately. A
free probe is released immediately and ordinary restart behavior proceeds; the
restart path never becomes package owner, holds no reservation, waits for none,
and never reads or writes the dedicated package transaction record. Package
transactions likewise never write `restart_status.json`. This record separation
and one-shot probe are the complete exception; ordinary lifecycle commands do
not join the package state machine.

`MEMORY-INDEP-020` reserves multiprocess admission evidence: simultaneous
requests from all four entrypoint classes admit exactly one owner through
restart, same-`intent_id` resubmissions are idempotent while conflicting reuse is
rejected within the finite retention window, expired identities return
`intent_unknown`, source deployments reject server-side, the post-mutation
import and resource-read guard covers forward/activation/rollback execution,
and owner death either recovers the recorded transaction or fails closed before
another mutation.

## Invariant 4: UI Recovery Is State-Derived

The caller generates `intent_id` before the initial install `POST`, so it can
retry that request with the same identity when Web restarts before flushing the
admission response. The initial request and every poll request are inside the
same retry boundary; there is no naked awaited request. A network disconnect,
UI restart, transient 404, or temporary 5xx records the last error, sleeps, and
retries until the existing dependency-install deadline instead of rejecting the
whole Settings action. No restart acknowledgement or grace delay is required.

When the replacement UI is reachable, the dependency-specific adapter first
returns a live in-memory job unchanged. Otherwise it uses `intent_id` to read the
lifecycle owner's projection from the dedicated package transaction record and
retained audit window. An active projection returns nonterminal
`status=in_progress` with the same dependency and intent identity; a terminal
projection returns the owner's exact result. Only if neither exists does
`memory-package` re-run the offline/read-only dependency status. It returns
synthetic terminal success with the same identity only when the complete
required readiness invariant above is currently true; otherwise it preserves
`job_not_found` and the current non-ready reason. Other dependencies do not gain
readiness-based recovery. The adapter is read-only and does not acquire the
reservation, acknowledge a restart, or write transaction state.

If lookup returns `intent_unknown`, the adapter preserves that machine reason
and performs the same read-only owner/dependency refresh. It never silently
reuses the expired identity as a new install. The caller may mint a new identity
only after that refresh proves there is no active transaction; the subsequent
submission is an explicit new attempt.

The deadline bounds one polling session, not the lifecycle transaction. At the
deadline the adapter performs one final owner-projection read and one final
read-only dependency refresh, then returns exactly one of three machine
semantics:

- terminal success or failure from the owner, or recovered success when the
  complete Memory readiness invariant is true;
- nonterminal `status=in_progress`, reason `dependency_transaction_active`, and
  `retryable=true` when the current owner projection is active; if the final read
  fails in transport after an earlier active projection for the same identity,
  the conservative result remains retryable `in_progress`; or
- terminal `status=failed`, reason `dependency_poll_transport_exhausted`, with
  the sanitized `last_transport_error` only when transport failures exhausted
  the session without any structured terminal or active projection.

A structured terminal job failure is returned unchanged. A reachable
`job_not_found` plus non-ready state remains a structured failure rather than
transport exhaustion. An active transaction is never reclassified as failed
because it exceeds the polling deadline; the caller resumes by polling the same
`intent_id`. The UI consumes only the dependency adapter and does not read raw
lifecycle state, acknowledge a restart, or require a new durable job, pending
flag, or delayed restart.

This continues `MEMORY-INDEP-018`: packaged Settings repair remains observable
through the all-process restart, poll transport failures are contained, and the
result converges on installed state rather than process-local job memory.

## Scenario And Evidence Matrix

| Scenario | Contract | Required automated evidence | Packaged/operational evidence |
| --- | --- | --- | --- |
| `MEMORY-INDEP-018` | Upgrade/repair preserves package shape; caller-known identity and state-derived UI recovery survive restart and transactions longer than one polling session | Initial-POST transport loss retries the same `intent_id`; poll truth table distinguishes terminal, transport-exhausted, and active-in-progress results; active transactions resume after deadline; dependency-specific recovery route | Real core and Memory wheels: Settings repair, enabled upgrade, all-process restart before POST response, over-deadline active recovery, terminal convergence, and rollback |
| `MEMORY-INDEP-019` | Every rollback plan is exact and resolver-satisfiable | Shape property table; duplicate canonical Memory providers fail closed; core-only and legacy replacement-core uninstall plus post-cleanup absence/cardinality verification; private resolved-plan construction; failure injection | Wheelhouse matrix for core-only, matching, optional-era mismatch, rejected transition mismatch, duplicate providers, pre-split replacement cleanup, resolver failure, and activation rollback |
| `MEMORY-INDEP-020` | One supervisor-owned admission/reservation and frozen execution bundle span mutation through restart | Multiprocess contention across all entrypoint classes; same-identity idempotency and conflicting reuse; source rejection; inherited-lock/no-owner probes; fail-loud post-mutation import/resource guards across forward, activation, health, and rollback; crash recovery | Packaged concurrent and repeated requests admit one transaction; external-process/HTTP observation, service health, and exact package shape verified after success/rollback |
| `MEMORY-INDEP-021` | Not-required status imports no optional implementation | Subprocess import guard for explicit disabled, safely degraded malformed Memory, and whole-config-unreadable status; loader probe contract and non-construction tests | Packaged disabled/core-only and malformed-Memory-config smoke with blocked `avibe_memory` imports |

Scenario IDs must be visible in their executable test names and in the Memory
independence catalog when implementation begins. Release/migration guards must
scan the shipped tree and published artifact set rather than a curated file list.

`MEMORY-INDEP-020` additionally requires these four executable cases:

1. A released legacy `restart_status.json` fixture with no version-1 fields
   loads as read-only recovery input and does not reject-load.
2. An identity pruned from both the current record and ten-log audit window
   returns `intent_unknown`; status refresh precedes a newly minted retry.
3. Killing the owner while a package child hangs proves the child-side deadline
   terminates the command tree and releases the reservation, after which one
   same-intent contender reconciles to `restored` or fail-closed `failed` without
   repeating forward mutation.
4. An ordinary restart concurrent with a live package reservation returns
   structured `busy` before writing `restart_status.json` or touching a process,
   while the package projection remains unchanged.

The release guard also verifies the complete staged Draft asset set, hashes,
distribution metadata, and local resolver closure before finalization, then
checks public Memory and core availability at their ordered publication gates.

## Migration And Release Ordering

The first release whose core wheel no longer bundles Memory is a transition
release. Its base `avibe-os` metadata has an exact hard dependency on the matched
`avibe-memory` release, regardless of whether Memory is currently enabled. This
is required because a pre-Wave-3c upgrader cannot submit the new transaction or
preserve optional package shape. No startup path installs a package.

Publication uses the existing release identity and one asset-complete finalizer;
it does not introduce a Memory-only tag, release, or second finalizer. The order
is fixed:

1. Build the `avibe-memory` wheel/sdist and its Memory-owned EverOS
   manifest/assets, then stage them in the official release Draft and workflow
   artifacts without making a public availability claim.
2. Verify staged Memory hashes and distribution metadata, and prove the exact
   transition core requirement resolves locally from the staged wheelhouse.
3. Build and stage the transition `avibe-os` artifacts with the exact hard
   dependency, no Memory implementation, and no EverOS manifest in the core
   wheel. Verify hashes, metadata, local resolver closure, and the complete
   asset set while the GitHub Release remains Draft.
4. The single finalizer publishes the asset-complete GitHub Release, uploads the
   Memory distributions to their package index before any core distribution,
   and verifies the exact Memory release is publicly resolvable and its public
   manifest URLs and hashes match the staged bytes.
5. Only after that public Memory check passes may the same finalizer upload core.
   It then resolves and downloads both public distributions together and repeats
   the manifest/hash availability checks before declaring success or allowing
   the transition release gate to be removed.
6. Keep the transition gate until packaged upgrade and rollback evidence covers
   every supported pre-split and split origin. In a later release, remove the
   base hard dependency and retain the matched `avibe-os[memory]` extra only
   after owner approval of the post-publication evidence.

This retires `KBD-1` through the hard transition dependency, `KBD-5` through
fully staged legacy rollback, and `KBD-6` through transition availability plus
explicit split-era shape capture. `KBD-10` is replaced by the single admission
owner. Release manifests move with the distribution that owns their artifacts;
the core release consumes verified published availability and never publishes a
manifest that points at a draft/private or differently versioned asset.

Compatibility is one-way during the transition: old releases can upgrade
because the hard dependency is ordinary package metadata; the Wave 3c
transaction can roll back because it captures the old distribution name,
launcher, release family, and exact Memory provider cardinality/version before
mutation. Memory data and V2 config formats do not change. Wave 3c introduces
one schema-versioned, package-specific transaction record so ordinary restart
status cannot overwrite package recovery state; the record is not a UI job
protocol and gains no retained identity index. Released `restart_status.json`
shapes remain accepted as read-only legacy recovery input under the rules above.

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
   `PackageLifecycleTransaction`, the dedicated versioned record, bounded
   intent projection and recovery election, and the frozen self-bounded
   execution bundle in one vertical delivery with `MEMORY-INDEP-020`.
4. **Recovery:** land the dependency-specific UI/API convergence and packaged
   `MEMORY-INDEP-018` closed loop.
5. **Release transition:** move manifest ownership, stage and verify the complete
   release, publish Memory before core inside the single finalizer, then remove
   the release gate only after public availability, all packaged matrices, and
   migration guards pass.

Each implementation PR must use exact packaged artifacts where package shape is
the behavior, keep the local Avibe service untouched, and stop if ownership
spreads beyond the named loader, lifecycle transaction, or dependency recovery
adapter.
