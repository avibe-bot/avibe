# Memory Wave 3c Lifecycle Contract

> Status: proposed; implementation and merge require owner/orchestrator approval
>
> Baseline: `origin/dev` at `d87a3941522e21dd1fe2f5973fac55e1b3b4c79a`
>
> Scope: Wave 3c Phase 0, Doc A

## Decision

Wave 3c treats packaged Memory readiness, package mutation, rollback, restart,
and recovery as one lifecycle contract. `PackageLifecycleTransaction`, owned by
the existing detached restart supervisor, is the only package-lifecycle
coordination primitive. It owns a dedicated schema-versioned transaction record
and one OS reservation from admission through active process work.

Callers submit an immutable intent type and payload with a caller-generated
`client_nonce`. The server mints `intent_id`; before returning it, the admitted
supervisor durably writes `intent_id`, `client_nonce`, intent type, and the
canonical payload digest to the transaction record. A lost admission response is
recovered by read-only nonce lookup. An unknown `intent_id` can never create or
execute a transaction.

The same package-lifecycle reservation also excludes an ordinary
`schedule_restart`: that entrypoint acquires it nonblockingly and holds it through
its complete stop/start process work. Contention returns structured `busy`; no
package-lifecycle path queues or waits. This is one shared primitive, not a
second lifecycle lock.

If a mutation cannot restore its captured shape, the record enters persistent
nonterminal `quarantined`. Quarantine rejects every forward mutation and cannot
be overwritten or aged out. It clears only after exact recovery or an explicit
repair proves a valid new baseline.

This document owns `MEMORY-INDEP-018` through `MEMORY-INDEP-021`. Its lifecycle
interfaces are normative for the release/migration contract, while the initial
release-family policy is supplied by the companion
[`memory-wave3c-release-migration-contract.md`](memory-wave3c-release-migration-contract.md).
That later contract may add family cases, but neither document may weaken the
other contract's fail-closed invariants.

## Background And Lineage

PR #1736 is the current Wave 3b base. PR #1739 closed unmerged after five Codex
reviews across four heads and 15 threads showed that Settings-only Memory repair
cannot be correct outside the package lifecycle.

PR #1741 then combined lifecycle and release migration in one specification. It
closed unmerged at `7516adc3a483d7eb54e60fda5d15149cb89f2a19` after three
reviewed heads and 17 threads. Its reviewed readiness, rollback-safety,
execution-bundle, and UI-recovery material is reorganized here. Its final five
threads are split as follows:

- server identity, ordinary restart exclusion, and quarantine are resolved by
  this document;
- legacy first-hop and transition-family rollback rules belong to the separate
  release/migration document.

The earlier plan's Wave 3 `MEMORY-INDEP-018` package-shape assignment is
superseded by this split. Doc A owns `MEMORY-INDEP-018` UI/Settings recovery,
`019` rollback, `020` admission, and `021` import fencing; the companion Doc B
owns `022` and `023`. The main plan carries the same supersession note and links
both documents so implementation evidence has one unambiguous ownership
matrix.

The retained #1739 and #1741 branches are evidence, not implementation bases. No
code is cherry-picked from either one.

## Scope

This contract owns:

- packaged Memory readiness and optional-import fencing;
- exact capture, pre-resolved forward/rollback execution, and fail-closed shape
  handling;
- one server-owned transaction across controller, Web, CLI, and Settings
  package-mutation entrypoints;
- server-issued transaction identity and state-derived recovery across UI
  restart or transport loss;
- exclusion between package transactions and ordinary `schedule_restart`
  process work;
- crash recovery and persistent quarantine; and
- scenario requirements `MEMORY-INDEP-018` through `MEMORY-INDEP-021`.

Non-goals:

- no `PluginHost`, plugin discovery, UDS/RPC service, or second Memory process;
- no second lock, heartbeat, lease, ownership token, external reaper, durable UI
  job, new package-lifecycle pending-restart marker, acknowledgement record, or
  retained identity index; the existing ordinary restart/reload marker remains
  outside this transaction as described below;
- no coordination added to QR, Doctor, UI reload, or direct global lifecycle
  commands outside the existing `schedule_restart` entrypoint;
- no caller-local lock, install plan, restart handoff, or recovery owner;
- no package installation during Avibe startup;
- no release identity, publication ordering, manifest ownership, or migration
  family decision, which belongs to the separate release/migration contract;
  and
- no product, test, scenario-catalog, workflow, release, manifest, or config
  change in this Phase 0 PR.

If implementation introduces another coordination primitive or makes a caller
coordinate install and restart, it has escaped this contract.

## Invariant 1: Readiness And Probe Ownership

`core.memory_loader` is the sole owner of the runtime entrypoint contract. It
exports a non-constructing probe that imports fixed `avibe_memory.runtime`,
compares its protocol with the host protocol, and verifies that
`create_memory_runtime` is callable. `load_memory_runtime` and the probe share
one private resolver. The probe never calls the factory or constructs a runtime.
No API, CLI, Doctor, or dependency-status module copies the entrypoint name,
protocol constant, or factory validation.

Every status path follows this order:

1. Load persisted configuration through `V2Config` and read its
   `memory_required` decision.
2. If only the optional Memory section is malformed, preserve `V2Config` safe
   degradation: disable Memory, retain its warning, and treat the recovered
   decision as readable `not_required`. Import zero `avibe_memory`
   implementation modules and do not block unrelated core mutation.
3. If configuration as a whole cannot be loaded safely, return fail-closed
   `memory_requirement_unreadable`. It is neither `not_required` nor `ready`,
   imports zero optional modules, and blocks every package-mutation intent.
4. If Memory is not required, return `not_required` without importing an
   `avibe_memory` implementation module. Metadata-only distribution inspection
   is allowed but makes no runtime-readiness claim.
5. If Memory is required on a packaged build, enumerate canonical distribution
   providers, inspect the unique readable version, call the loader-owned runtime
   probe, and import the separate `avibe_memory.artifact` contract.
6. Only after artifact import succeeds may the artifact manager be asked for
   EverOS status.

For a required packaged installation, `memory-package` is `ready` if and only
if:

- exactly one canonical `avibe-memory` metadata provider is installed;
- its readable normalized version equals the running Avibe release;
- the loader-owned runtime probe succeeds; and
- the artifact contract imports successfully.

Zero providers means missing. More than one is non-ready
`memory_package_metadata_ambiguous`; no path selects the first provider.
Version mismatch takes precedence over import failure. Missing distribution and
unreadable metadata retain distinct machine reasons. A later artifact-manager
or EverOS status failure affects only `memory-runtime`, not the importable Python
distribution. Required non-ready states have an explicit action class:

- `missing`, version mismatch, and runtime/artifact probe failure are
  `repairable`; status exposes the existing centrally admitted repair action;
- duplicate canonical providers, unreadable metadata, and source or unpublished
  builds are `operator_only`; status exposes no repair action and a direct repair
  intent is rejected before mutation with its structured reason. Quarantine is
  also `operator_only` for ordinary forward repair: that action is hidden and
  rejected, while the separately named `repair_quarantine` recovery action
  remains explicitly exposed as described in Invariant 3.

The repairable action is an admission path, not a promise that every repair can
be planned: a later owner preflight may still return the same structured
pre-mutation failure without mutating packages.

Source/unpublished builds never advertise package mutation. They may use the
loader when Memory is enabled, but status cannot imply that a source tree is
package-manager repairable.

`MEMORY-INDEP-021` reserves the executable invariant: disabled or otherwise
not-required packaged Memory, including a safely degraded malformed Memory
section, imports zero `avibe_memory` implementation modules while metadata-only
inspection remains allowed. Whole-config failure also imports zero optional
modules and blocks mutation.

## Invariant 2: Capture And Rollback Are Executable

The lifecycle owner captures the complete pre-mutation package shape before it
builds or executes a forward plan:

- running core version, canonical distribution provider, service launcher, and
  release-family identity;
- every visible provider whose canonicalized name is `avibe-memory`; and
- the exact normalized Memory version when provider cardinality is one.

Memory provider cardinality is part of the shape. Only zero or one is
representable. Duplicate canonical providers fail closed before plan
construction, readiness selection, or mutation, even when their versions match.

There are two types, not one partially valid plan:

- `CapturedPackageShape` is the immutable pre-mutation observation.
- `ResolvedRollbackPlan` exists only after a release-family policy validates the
  shape and every exact requirement, uninstall, provider check, launcher, and
  artifact resolves into transaction-owned staging.

The plan constructor is private to the lifecycle owner. It cannot contain a
required distribution with an absent version or an unverified provider. Every
constructed rollback plan is resolver-satisfiable and executable without a new
package-index dependency. Release-family policy is pure, fixed in the running
release, and evaluated before mutation; unknown or inconsistent families reject
the request. A later release/migration contract can add family cases only by
preserving this fail-closed interface.

Core-only cleanup is active: if the captured valid family has no Memory
provider, rollback removes Memory introduced by the forward mutation and proves
provider absence. A valid optional split shape restores base core and Memory as
independent exact pins so a captured mismatch remains expressible. Cleanup
re-enumerates canonical providers and requires the resolved cardinality and
version. A family whose dependency metadata makes the captured pins
inconsistent has no plan and rejects before mutation.

`restored` is a shape result, independent of readiness. It is true only when
installed distribution presence, exact versions, and canonical Memory provider
cardinality are exactly equivalent to the `ResolvedRollbackPlan` target. A
valid optional-era captured mismatch may therefore finish `restored` while the
current readiness projection remains non-ready and repairable; that family is
not rejected before mutation solely because its captured versions differ.

At `resolved`, the owner holds one immutable execution bundle containing:

- fully resolved forward and rollback package commands;
- staged artifact paths and hashes;
- uninstall and provider-verification commands;
- captured launcher, stop/start commands and environments, health targets, and
  bounded timeouts; and
- all data needed to project a terminal or quarantined result.

Immediately before the first package-mutating child command, the owner performs
a write-ahead transition to `state=mutating` in the same transaction record and
durably syncs it (temporary file, atomic replace, and parent-directory sync, or
the Windows equivalent). No package command may start before that marker is
durable. The marker is the recovery boundary: `admitted`, `captured`, and
`resolved` prove that no package mutation was started.

The supervisor imports every internal helper and standard-library module the
bundle can call before `mutating`. From `mutating` onward it executes only
bundle-held commands/data and already imported standard-library code. A
fail-loud guard rejects any later Python import or package/resource read from the
environment being replaced. Rollback uses the same bundle. Activation and
health are observed only through external child processes and bounded HTTP
probes.

Every external command has immutable `execution_timeout_seconds`, enforced by a
captured child-side deadline runner. The runner owns the Unix process group or
Windows Job Object, inherits the one reservation descriptor/handle, terminates
its command tree at the deadline, and exits even if the supervisor died. It is
only a bundle executor: it cannot own lifecycle state, renew a deadline, start
recovery, or write the record. No external bootstrap or reaper is introduced.

Failure semantics are closed:

- capture or resolution failure before admission rejects with no record or
  mutation; after durable `admitted` but before `mutating`, it records terminal
  `failed` with a machine reason, leaves the installation untouched, appends
  the terminal audit entry, releases the reservation, and never enters
  quarantine;
- failure after mutation begins executes the staged exact rollback;
- successful rollback proves the exact target package shape, provider
  cardinality, launcher, database recovery point, and activation before
  `restored`; readiness is a separate projection and is not a prerequisite;
- a dead owner leaves its record and bundle identity for same-intent recovery;
  and
- if exact rollback cannot run or cannot prove restoration, the record becomes
  `quarantined`, never an ordinary terminal failure.

`MEMORY-INDEP-019` reserves shape properties, private plan construction,
provider ambiguity, cleanup verification, resolution failure, partial mutation,
activation failure, exact rollback, and quarantine entry.

## Invariant 3: One Admission And Recovery Owner

### Admission Protocol

All package-mutation entrypoints call one server adapter. A new-attempt request
contains `client_nonce`, intent type, and canonical payload, but no `intent_id`.
The nonce is collision-resistant, minted once per explicit caller attempt, and
is not transaction authority.

The adapter first performs read-only nonce lookup:

- a match in the nonterminal current record returns its projection;
- a match in retained terminal audit returns its terminal projection;
- a miss is eligible for a new admission; and
- a recovery-by-nonce miss returns `nonce_unknown` and cannot execute
  mutation unless the caller has first confirmed that no current/audit
  projection and no publication holder exist, in which case one replay of the
  same nonce is allowed.

After an initial POST loses transport, the caller first uses recovery-by-nonce.
If that read confirms absence of a current/audit projection and no lock-holder
publication, it may replay the same nonce once within the existing boundary;
once any server identity or projection is observed, it never submits a new
attempt. Nonce idempotency is bounded to the current record plus retained
terminal audit. Once a terminal audit entry is pruned, recovery returns
`nonce_unknown`; a later explicit attempt uses a new nonce. No unadmitted
outcome is persisted or projected.

For an eligible new attempt, the adapter spawns the existing detached
supervisor with the immutable nonce, type, and payload. The supervisor:

1. acquires `package-lifecycle.lock` nonblockingly;
2. immediately writes holder type, PID, and acquisition timestamp into the lock
   file itself;
3. rechecks the current record under that reservation;
4. rejects source builds, unreadable global configuration, active transactions,
   quarantine, and operator-only package states before package capture or
   subprocess work;
5. mints a collision-resistant `intent_id`;
6. atomically and durably writes `schema_version`, `intent_id`, `client_nonce`,
   type, payload digest, and `state=admitted` to the dedicated transaction
   record by writing and syncing a temporary file, replacing the record, and
   syncing its parent directory (or the Windows durability equivalent); and
7. only after that write is durable, returns the receipt through a one-shot
   inherited pipe to the adapter.

The pipe is response transport, not a coordination primitive or durable
acknowledgement. A lost receipt is reconstructed by nonce lookup. Concurrent
same-nonce requests converge on the recorded identity: a contender that loses
the reservation rereads the current record and returns its identity/projection
when the nonce and canonical intent digest match. Reservation contention has
three explicit machine outcomes with distinct retry rules:

- `busy_package_transaction` means another package transaction owns the
  reservation or has a nonterminal record. The caller does not submit a new
  intent while that transaction is active; it may read the known projection and
  retry admission after the transaction is terminal.
- `busy_restart` means an ordinary restart holds the reservation. The caller
  retries the same client nonce after a short backoff, without expecting a
  package publication or minting a new nonce.
- `busy_pending_publication` means a package supervisor owns the reservation but
  is between lock acquisition and its durable `admitted` write. The caller
  briefly retries read-only nonce projection until the record appears or the
  existing request deadline expires, never repeating the new-attempt POST.

The server returns a matching projection before any busy outcome. A different
nonce never receives `busy_pending_publication`; it receives
`busy_package_transaction` or `busy_restart` according to the owner. The client
never invents or persists an `intent_id` before the server returns it.

Every API that accepts an `intent_id` is lookup or recovery only. An ID absent
from the current record and retained terminal audit returns stable
`intent_unknown`; it never mints a replacement, acquires for forward mutation,
or executes a package command.

### Transaction Record

`package_lifecycle_transaction.json` is separate from ordinary
`restart_status.json`. The lifecycle owner is its sole writer. The canonical
record starts at `schema_version: 1` and contains the current transaction
(identity, canonical intent digest, captured shape, resolved bundle identity,
current transition, recovery facts, mutation-boundary marker, and the exact
projection needed by read-only adapters) plus an atomically updated
`terminal_audit` array containing at most ten structured terminal projections.
This is the fixed `N=10` retention window.
Each update writes and syncs a temporary file, atomically replaces the record,
and syncs its parent directory (or the Windows durability equivalent) before a
receipt or next admission is returned.

Nonterminal current transactions are never rotated, pruned, overwritten, or
replaced by a new intent. When a later intent is durably admitted, only
terminal `succeeded`, `restored`, `reconciled`, or admitted pre-mutation
`failed` current records are appended to `terminal_audit`; the oldest terminal
entry is evicted beyond ten. `rejected`, `busy_package_transaction`,
`busy_restart`, and `busy_pending_publication` are unadmitted responses, so none
creates or rotates a transaction record. `restart-*.log` files remain human
audit logs only; they
have no nonce/identity semantics and are not migration input.

Compatibility rules are:

- released `restart_status.json`, a record with no schema version, or a
  supported older schema loads as read-only legacy recovery input and is never
  rejected or rewritten in place;
- recognized legacy facts are reconciled with current metadata and reservation
  liveness before a version-1 record may admit a new intent; and
- an unknown newer schema loads fail-closed as recovery-only
  `transaction_record_version_unsupported` and blocks forward admission until
  understood.

### Reservation And Ordinary Restart

There is exactly one lock file, `package-lifecycle.lock`, under the Avibe runtime
directory. On Unix its advisory lock is held on one open file descriptor; on
Windows the equivalent exclusive file lock is held on one inheritable handle.
Package-manager children inherit that exact descriptor/handle. Unrelated
children do not.

The only liveness test is a nonblocking acquisition attempt on that lock. The
first action by a holder after acquisition is a write-ahead publication in the
lock file itself containing holder type (`package` or `ordinary_restart`), PID,
and acquisition timestamp. Contenders read that publication: an empty or
unreadable lock file is `busy_pending_publication`, while a readable holder
drives the appropriate structured busy result. Ordinary restart publishes the
same fields before spawning its child. No second lock, heartbeat, token, lease,
or durable ownership channel is allowed.

The package supervisor holds the reservation from `admitted` through active
forward, activation, verification, and rollback work. If it enters quarantine
with no command running, it records `quarantined` durably and releases the live
reservation; the nonterminal record, not a stale lock, continues to block
forward admission.

Ordinary `schedule_restart` uses the same reservation at its single server
entrypoint. It attempts acquisition once and returns structured `busy_restart`
on contention. Immediately after acquisition, it publishes the holder metadata
described above, then reads the package transaction record once before spawning
the existing detached ordinary-restart supervisor. If it finds nonterminal
`inactive`, `interrupted`, `mutating` residue, or `quarantined` without a live
package recovery owner, it releases the reservation and returns structured
`blocked_interrupted_transaction` pointing to recovery; it never starts a
partial ordinary stop/start. A restart explicitly owned by the elected
recovery transaction is exempt and uses that transaction's reservation. The
entrypoint then passes that same descriptor/handle to the child, closes only
its parent duplicate after a successful spawn, and the child holds it
continuously from before writing restart status through its complete stop,
start, and readiness work. The child exits and thereby releases the
reservation after that bounded work. It does not write the package transaction
record or become a package transaction. Package admission while it holds the
reservation returns `busy_restart`.
There is no check-release-proceed window, reacquisition, queue, or wait.

QR, Doctor, UI reload, and direct start/stop flows do not independently join
this contract. Any existing caller that invokes the shared `schedule_restart`
entrypoint, including WeChat QR login, automatically inherits its short
reservation hold and structured `busy` response; that is shared-entrypoint
behavior, not caller-local coordination. New callers remain excluded and
require a separate owner decision rather than spreading lock calls through
callers.

The existing `pending_restart.json` path remains owned by ordinary restart and
reload flows. Package lifecycle never reads or writes it, and a package-driven
restart never uses it. If an ordinary pending follow-up reaches the shared
entrypoint while a package reservation is active, it receives structured
`busy` and retries through the existing ordinary path; preserving that retry is
a gate-3 implementation note, not a Phase 0 product change.

### Crash Recovery And Quarantine

A same-`intent_id` request is the only recovery-election request. If the
reservation is live, it returns the current projection. If the owner and
reservation are not live, contenders make one nonblocking acquisition attempt
on the same lock. One becomes recovery owner; losers return current
nonterminal/busy state. Recovery never repeats forward mutation.

The elected owner reconciles installed metadata with `CapturedPackageShape` and
the recorded `ResolvedRollbackPlan`:

- if the current state is `admitted`, `captured`, or `resolved`, the durable
  write-ahead marker proves no package command started; owner loss or a
  non-crash capture/resolution failure records terminal pre-mutation `failed`
  with its exact reason, appends terminal audit, releases the reservation, and
  immediately permits a new intent;
- complete valid staging executes only recorded exact rollback;
- proven exact restoration ends `restored`; and
- once `state=mutating` (or any later state) is durable, missing/invalid
  staging, rollback failure, or unverifiable restored shape enters persistent
  `quarantined` with an exact reason.

`quarantined` is nonterminal. It remains the current record, survives restart,
is never audit-pruned, and rejects every forward intent even when installed
metadata appears internally readable. Readiness may describe observed state but
cannot bless it as a baseline.

Quarantine clears through exactly one of two owner actions:

1. Same-intent exact recovery restores the original captured shape and proves
   provider cardinality, launcher, and activation, ending `restored` under the
   shape rule above; readiness is projected separately.
2. An explicit `repair_quarantine` request reuses the ordinary transaction
   admission path in the current record: the caller supplies a nonce and exact
   target payload, the server mints a new `intent_id`, and the record
   transitions `quarantined -> admitted (repair)` while retaining the
   `quarantined_baseline` and without a nested repair slot or second record.
   It acquires the same reservation and names the expected supported release
   family and exact core/Memory target. That exact target shape replaces the
   captured baseline for this repair attempt, while the quarantined baseline
   remains available for recovery. The resolved repair bundle, target digest,
   and rollback data are durably recorded before `repairing` or any package
   mutation. The owner must capture the observed state, prove family match,
   resolve and execute the exact repair, prove canonical Memory provider
   cardinality zero or one as required by that family, and record the confirmed
   new baseline before ending `reconciled`. Any failure to resolve, mutate, or
   prove the new baseline transitions the same record back to `quarantined`.

There is no automatic clear, metadata-only acceptance, caller acknowledgement,
or overwrite by an unrelated newly minted forward intent; the server-issued
`repair_quarantine` intent is the sole bound recovery exception.

State transitions are:

```text
new attempt
  -> rejected | busy_package_transaction | busy_restart | busy_pending_publication
  -> admitted -> captured -> resolved -> failed (pre-mutation owner loss or capture/resolution failure) | mutating
  -> activating -> verifying -> succeeded
  -> rolling_back -> restored | quarantined
stale nonterminal + same-intent election
  -> recovering -> restored | quarantined
quarantined + explicit repair
  -> repairing -> reconciled | quarantined
```

`succeeded`, `restored`, `reconciled`, and an admitted pre-mutation `failed` are
terminal transaction states. `rejected`, `busy_package_transaction`,
`busy_restart`, and `busy_pending_publication` are unadmitted responses; all
three busy outcomes are retryable according to their rules above and never
terminal. `quarantined` is persistent nonterminal and mutation-blocking.

`MEMORY-INDEP-020` reserves multiprocess admission and recovery evidence across
controller, Web, CLI, and Settings, including:

- nonce recovery after a lost admission response and `intent_unknown` for an
  invented or expired server ID;
- same-nonce convergence, publication-window `busy_pending_publication`
  retry, and different-nonce contention;
- a nonterminal record surviving the embedded ten-entry terminal audit and a
  released legacy record
  `restart_status.json` fixture loading read-only;
- an ordinary restart's short, bounded hold of the one reservation through
  stop/start while a concurrent package request returns `busy`;
- source and unreadable-config rejection before mutation;
- operator-only duplicate-provider/unreadable-metadata server rejection before
  mutation;
- existing ordinary `pending_restart.json` follow-up receiving shared-entrypoint
  `busy` while package work is active and retrying later;
- child timeout, inherited-reservation behavior after owner death, and a
  nonblocking acquisition that succeeds once no live reservation remains; and
- quarantine entry, forward rejection, exact recovery, and explicit verified
  reconciliation exit.

## Invariant 4: UI Recovery Is State-Derived

The caller generates `client_nonce` before the initial install POST. The POST,
nonce recovery request, and every later poll are inside one retry boundary; no
request is naked-awaited. Transport loss, UI restart, transient 404, and
temporary 5xx retain the last transport error and retry until the existing
dependency-install deadline.

If the POST response is lost, the caller queries by nonce. Once the server-issued
`intent_id` is known, the dependency adapter returns a live in-memory job when
present; otherwise it reads the dedicated transaction record and terminal audit.
It is read-only and never acquires the reservation or writes lifecycle state.
The read-only probe distinguishes `active` (the reservation is live) from
`interrupted` (the record is nonterminal but no reservation is live). Both
project `status=in_progress`; `interrupted` additionally exposes a recovery
entry in the UI. Only an explicit same-`intent_id` recovery request may trigger
the election described in Invariant 3, so polling never becomes an owner.
During the short admission publication window, `busy_pending_publication` is a
retryable nonterminal response: the caller re-reads by the same nonce and never
repeats the new-attempt POST.

Projection rules are:

- active, interrupted, or recovering state returns nonterminal
  `status=in_progress` with the
  same dependency, nonce, and server identity;
- quarantine returns structured `status=quarantined`, the owner reason, and the
  explicit recovery/repair affordance without pretending the forward attempt is
  terminal;
- terminal state returns the owner's exact result;
- unknown identity returns `intent_unknown` and never triggers mutation; and
- only when no owner projection exists may `memory-package` run offline/read-only
  dependency status and synthesize success if the complete readiness invariant
  is true. Other dependencies do not gain readiness recovery.

The deadline bounds one polling session, not the transaction. After a final
owner read and dependency refresh, it returns exactly one machine shape:

- owner terminal success/failure or readiness-derived success;
- retryable `in_progress` for an active transaction, including when the last
  read failed after an earlier active projection;
- retryable `in_progress` for an interrupted nonterminal transaction with a
  recovery affordance; interrupted is never classified as transport
  exhaustion;
- structured `quarantined`; or
- terminal `dependency_poll_transport_exhausted` with sanitized
  `last_transport_error` only when transport exhausted without any structured
  terminal, active, interrupted, or quarantined projection.

A reachable structured failure is returned unchanged. Active work is never
reclassified as failed because one polling session expired. A caller resumes by
nonce until identity is known, then by the same `intent_id`. No restart
acknowledgement, delayed restart, durable UI job, or new pending flag is added.

`MEMORY-INDEP-018` reserves packaged Settings repair through initial-response
loss, all-process restart, transport retry, over-deadline active work,
state-derived recovery, quarantine projection, and terminal convergence.

## Scenario And Evidence Matrix

| Scenario | Contract | Automated evidence | Packaged evidence |
| --- | --- | --- | --- |
| `MEMORY-INDEP-018` | UI recovery follows nonce, server identity, owner state, and final readiness | POST-loss retry boundary; nonce/ID projection truth table; transport, active/interrupted, quarantined, and terminal deadline results | Real wheels: Settings repair, response loss, all-process restart, over-deadline recovery, quarantine projection, convergence |
| `MEMORY-INDEP-019` | Capture is exact and every constructed rollback is staged and executable | Provider-cardinality property; private plan construction; cleanup verification; capture/resolution failure to terminal `failed`; resolution and rollback failure injection | Wheelhouse matrix for core-only and optional split shapes, duplicate providers, partial mutation, activation failure, exact restore, quarantine |
| `MEMORY-INDEP-020` | One server owner and reservation cover admission through recovery and ordinary restart exclusion | Multiprocess nonce/identity contention; pre-mutation crash to terminal `failed` with immediate retry; explicit `busy_package_transaction`/`busy_restart`/`busy_pending_publication` retry semantics; operator-only server rejection; released `restart_status.json` fixture; nonterminal retention; ordinary restart short-hold busy; child timeout and no-live-reservation acquisition; quarantine enter/exit | Concurrent controller/Web/CLI/Settings requests, restart contention, killed-owner recovery, exact package and service health |
| `MEMORY-INDEP-020` (gate 3 implementation evidence) | Pending ordinary restart follow-up remains retryable when package reservation is busy | Verify `pending_restart.json` is requeued or handed off after structured `busy`; no marker loss and no package-record write | Ordinary config restart completes after package reservation release |
| `MEMORY-INDEP-020` (gate 4 implementation evidence) | QR callers preserve activation when shared restart admission is busy | Verify shared-entrypoint `busy` is surfaced as retryable/presented activation state; no QR-local lock or pending protocol | WeChat QR login during package reservation, followed by eventual restart and active bot |
| `MEMORY-INDEP-021` | Not-required status imports no optional implementation | Subprocess import guard for disabled, safe-degraded optional config, and whole-config failure; non-constructing loader probe | Packaged core-only and malformed-config smoke with blocked optional imports |

Scenario IDs must be visible in executable test names and in the Memory
independence catalog when implementation begins. Guards scan the shipped tree,
provider set, and packaged artifacts rather than a curated caller list.

## Compatibility And Recovery Inventory

Memory data and V2 config formats do not change. The one new persistence surface
is the dedicated package transaction record; it is not a UI job protocol. Legacy
restart status remains accepted as read-only recovery input. Nonterminal state is
never discarded merely because code or UI restarted.

Retained behavior is reconciled, not cherry-picked:

| Retained evidence | Disposition |
| --- | --- |
| #1739 loader-owned non-constructing runtime probe | Preserve one runtime-contract owner and add the not-required import fence |
| #1739 fail-closed rollback metadata handling | Preserve fail-closed capture and structured caller projection behind the transaction owner |
| #1739 Settings packaged repair and state-derived missing-job recovery | Re-derive under nonce/server identity and owner-state polling |
| #1741 pre-mutation frozen bundle and child timeout | Preserve as the transaction execution boundary |
| #1741 caller-issued identity | Replace with caller nonce and server-issued persisted identity |
| #1741 one-shot ordinary restart probe | Replace with nonblocking acquire held through ordinary restart process work |
| #1741 terminal unrecoverable failure | Replace with persistent nonterminal quarantine |

## Phase Gates

1. **Phase 0, Doc A:** approve and merge this lifecycle contract. No product
   implementation occurs in this PR.
2. **Gate 2a, loader readiness:** after Doc A merges and with separate owner
   approval, implement the loader-owned readiness probe, optional-import fence,
   and `MEMORY-INDEP-021`. This is the only implementation gate Doc A alone
   may unlock.
3. **Gate 2b, rollback types:** after both documents merge, implement
   captured/resolved rollback types and `MEMORY-INDEP-019` using the initial
   release-family policy in Doc B.
4. **Gate 3, lifecycle owner:** after both documents merge, implement server
   admission, one reservation, versioned record, embedded terminal audit,
   frozen bundle, crash recovery, quarantine, and `MEMORY-INDEP-020`.
5. **Gate 4, recovery adapter:** after both documents merge, implement
   nonce/identity UI convergence and packaged `MEMORY-INDEP-018`.

Doc A approval alone authorizes only gate 2a. Rollback, lifecycle, and UI gates
require the merged Doc A and Doc B contracts plus separate owner approval;
release/migration gate 5 remains separately gated by Doc B. Each implementation
PR must use exact packaged artifacts where package shape matters, must not
restart the local Avibe service for verification, and must stop if ownership
spreads beyond the loader, lifecycle transaction, or dependency adapter.
