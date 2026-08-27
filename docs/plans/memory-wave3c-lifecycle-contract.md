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
path queues or waits. This is one shared primitive, not a second lifecycle lock.

If a mutation cannot restore its captured shape, the record enters persistent
nonterminal `quarantined`. Quarantine rejects every forward mutation and cannot
be overwritten or aged out. It clears only after exact recovery or an explicit
repair proves a valid new baseline.

This document owns `MEMORY-INDEP-018` through `MEMORY-INDEP-021`. It has no
dependency on the release/migration contract. That later contract may add
release-family rules, but it cannot weaken these lifecycle invariants.

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
  job, pending-restart marker, acknowledgement record, or retained identity
  index;
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
distribution. A required non-ready package remains eligible for centrally
admitted repair.

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

At `resolved`, the owner holds one immutable execution bundle containing:

- fully resolved forward and rollback package commands;
- staged artifact paths and hashes;
- uninstall and provider-verification commands;
- captured launcher, stop/start commands and environments, health targets, and
  bounded timeouts; and
- all data needed to project a terminal or quarantined result.

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

- capture or resolution failure rejects with no mutation;
- failure after mutation begins executes the staged exact rollback;
- successful rollback proves package shape, provider cardinality, launcher,
  database recovery point, activation, and readiness before `restored`;
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
- absence on a new-attempt request may proceed to admission; and
- absence on the separate recovery-by-nonce read returns `nonce_unknown` and
  can never execute mutation.

After an initial POST loses transport, the caller uses only recovery-by-nonce;
it never repeats the new-attempt POST. Nonce idempotency is bounded to the
current record plus retained terminal audit. Once a terminal audit entry is
pruned, recovery returns `nonce_unknown`; a later explicit attempt uses a new
nonce.

For an eligible new attempt, the adapter spawns the existing detached
supervisor with the immutable nonce, type, and payload. The supervisor:

1. acquires `package-lifecycle.lock` nonblockingly;
2. rechecks the current record under that reservation;
3. rejects source builds, unreadable global configuration, active transactions,
   and quarantine before package capture or subprocess work;
4. mints a collision-resistant `intent_id`;
5. atomically and durably writes `schema_version`, `intent_id`, `client_nonce`,
   type, payload digest, and `state=admitted` to the dedicated transaction
   record by writing and syncing a temporary file, replacing the record, and
   syncing its parent directory (or the Windows durability equivalent); and
6. only after that write is durable, returns the receipt through a one-shot
   inherited pipe to the adapter.

The pipe is response transport, not a coordination primitive or durable
acknowledgement. A lost receipt is reconstructed by nonce lookup. Concurrent
same-nonce requests converge on the recorded identity: a contender that loses
the reservation rereads the current record and returns its identity/projection
when the nonce and canonical intent digest match; otherwise it returns
structured `busy`. A different nonce also sees `busy`. The client never
invents, retries as new, or persists an `intent_id` before the server returns
it.

Every API that accepts an `intent_id` is lookup or recovery only. An ID absent
from the current record and retained terminal audit returns stable
`intent_unknown`; it never mints a replacement, acquires for forward mutation,
or executes a package command.

### Transaction Record

`package_lifecycle_transaction.json` is separate from ordinary
`restart_status.json`. The lifecycle owner is its sole writer. The canonical
record starts at `schema_version: 1` and contains identity, canonical intent
digest, captured shape, resolved bundle identity, current transition, recovery
facts, and the exact projection needed by read-only adapters.

Nonterminal records are never rotated, pruned, overwritten, or replaced by a
new intent. Only terminal `succeeded`, `restored`, `reconciled`, or admitted
pre-mutation `failed` records move into the existing ten-entry audit rotation
when a later intent is durably admitted. Audit rotates terminal records only.
`rejected` and `busy` are unadmitted responses, so neither creates or rotates a
transaction record.

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

The only liveness test is a nonblocking acquisition attempt on that lock.
Status, PID, timestamp, and audit fields are recovery evidence, not ownership.
No second lock, heartbeat, token, or lease is allowed.

The package supervisor holds the reservation from `admitted` through active
forward, activation, verification, and rollback work. If it enters quarantine
with no command running, it records `quarantined` durably and releases the live
reservation; the nonterminal record, not a stale lock, continues to block
forward admission.

Ordinary `schedule_restart` uses the same reservation at its single server
entrypoint. It attempts acquisition once and returns structured `busy` on
contention. On success, the entrypoint passes that same descriptor/handle to the
existing detached ordinary-restart supervisor, closes only its parent duplicate
after a successful spawn, and the child holds it continuously from before
writing restart status through its complete stop, start, and readiness work.
The child exits and thereby releases the reservation after that bounded work.
It does not write the package transaction record or become a package
transaction. Package admission while it holds the reservation returns `busy`.
There is no check-release-proceed window, reacquisition, queue, or wait.

QR, Doctor, UI reload, and direct start/stop flows do not join this contract.
Adding them requires a separate owner decision rather than spreading lock calls
through callers.

### Crash Recovery And Quarantine

A same-`intent_id` request is the only recovery-election request. If the
reservation is live, it returns the current projection. If the owner and
reservation are not live, contenders make one nonblocking acquisition attempt
on the same lock. One becomes recovery owner; losers return current
nonterminal/busy state. Recovery never repeats forward mutation.

The elected owner reconciles installed metadata with `CapturedPackageShape` and
the recorded `ResolvedRollbackPlan`:

- complete valid staging executes only recorded exact rollback;
- proven exact restoration ends `restored`; and
- missing/invalid staging, rollback failure, or unverifiable restored shape
  enters persistent `quarantined` with an exact reason.

`quarantined` is nonterminal. It remains the current record, survives restart,
is never audit-pruned, and rejects every forward intent even when installed
metadata appears internally readable. Readiness may describe observed state but
cannot bless it as a baseline.

Quarantine clears through exactly one of two owner actions:

1. Same-intent exact recovery restores the original captured shape and proves
   provider cardinality, launcher, activation, and full required readiness,
   ending `restored`.
2. An explicit `repair_quarantine` action references the quarantined
   `intent_id`, acquires the same reservation, and names the expected supported
   release family and exact core/Memory target. It is recovery under the same
   record, not a new forward intent. It must capture the observed state, prove
   family match, resolve and execute an exact repair, prove full readiness and
   canonical Memory provider cardinality zero or one as required by that
   family, and record the confirmed new baseline before ending `reconciled`.

There is no automatic clear, metadata-only acceptance, caller acknowledgement,
or overwrite by a newly minted intent.

State transitions are:

```text
new attempt
  -> rejected | busy
  -> admitted -> captured -> resolved -> mutating
  -> activating -> verifying -> succeeded
  -> rolling_back -> restored | quarantined
stale nonterminal + same-intent election
  -> recovering -> restored | quarantined
quarantined + explicit repair
  -> repairing -> reconciled | quarantined
```

`succeeded`, `restored`, `reconciled`, and an admitted pre-mutation `failed` are
terminal transaction states. `rejected` and `busy` are unadmitted responses.
`quarantined` is persistent nonterminal and mutation-blocking.

`MEMORY-INDEP-020` reserves multiprocess admission and recovery evidence across
controller, Web, CLI, and Settings, including:

- nonce recovery after a lost admission response and `intent_unknown` for an
  invented or expired server ID;
- same-nonce convergence and different-nonce contention;
- a nonterminal record surviving audit rotation and a released legacy record
  `restart_status.json` fixture loading read-only;
- an ordinary restart's short, bounded hold of the one reservation through
  stop/start while a concurrent package request returns `busy`;
- source and unreadable-config rejection before mutation;
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

Projection rules are:

- active or recovering state returns nonterminal `status=in_progress` with the
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
- structured `quarantined`; or
- terminal `dependency_poll_transport_exhausted` with sanitized
  `last_transport_error` only when transport exhausted without any structured
  terminal, active, or quarantined projection.

A reachable structured failure is returned unchanged. Active work is never
reclassified as failed because one polling session expired. A caller resumes by
nonce until identity is known, then by the same `intent_id`. No restart
acknowledgement, delayed restart, durable UI job, or pending flag is added.

`MEMORY-INDEP-018` reserves packaged Settings repair through initial-response
loss, all-process restart, transport retry, over-deadline active work,
state-derived recovery, quarantine projection, and terminal convergence.

## Scenario And Evidence Matrix

| Scenario | Contract | Automated evidence | Packaged evidence |
| --- | --- | --- | --- |
| `MEMORY-INDEP-018` | UI recovery follows nonce, server identity, owner state, and final readiness | POST-loss retry boundary; nonce/ID projection truth table; transport, active, quarantined, and terminal deadline results | Real wheels: Settings repair, response loss, all-process restart, over-deadline recovery, quarantine projection, convergence |
| `MEMORY-INDEP-019` | Capture is exact and every constructed rollback is staged and executable | Provider-cardinality property; private plan construction; cleanup verification; resolution and rollback failure injection | Wheelhouse matrix for core-only and optional split shapes, duplicate providers, partial mutation, activation failure, exact restore, quarantine |
| `MEMORY-INDEP-020` | One server owner and reservation cover admission through recovery and ordinary restart exclusion | Multiprocess nonce/identity contention; released `restart_status.json` fixture; nonterminal retention; ordinary restart short-hold busy; child timeout and no-live-reservation acquisition; quarantine enter/exit | Concurrent controller/Web/CLI/Settings requests, restart contention, killed-owner recovery, exact package and service health |
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
2. **Contract owners:** implement loader readiness and captured/resolved rollback
   types with `MEMORY-INDEP-019` and `021`.
3. **Lifecycle owner:** implement server admission, one reservation, versioned
   record, frozen bundle, crash recovery, quarantine, and
   `MEMORY-INDEP-020`.
4. **Recovery adapter:** implement nonce/identity UI convergence and packaged
   `MEMORY-INDEP-018`.

Merging Doc A partially unlocks implementation gates 2 through 4 only after
separate owner approval. It does not unlock release/migration gate 5. Each
implementation PR must use exact packaged artifacts where package shape matters,
must not restart the local Avibe service for verification, and must stop if
ownership spreads beyond the loader, lifecycle transaction, or dependency
adapter.
