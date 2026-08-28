# Memory Wave 3c Lifecycle Contract

> Status: proposed; implementation and merge require owner/orchestrator approval
>
> Baseline: `origin/dev` at `d87a3941522e21dd1fe2f5973fac55e1b3b4c79a`
>
> Scope: Wave 3c Phase 0, Doc A

## Decision

Wave 3c treats packaged Memory readiness, package mutation, rollback, restart,
and recovery as one lifecycle contract. `PackageLifecycleTransaction`, owned by
the detached package-lifecycle supervisor, is the only package-lifecycle
coordination primitive. It owns a dedicated schema-versioned transaction record
and one OS reservation from admission through active process work.

Callers submit an immutable intent type and payload with a caller-generated
`client_nonce`. The server mints `intent_id`; before returning it, the admitted
supervisor durably writes `intent_id`, `client_nonce`, intent type, and the
canonical payload digest to the transaction record. A lost admission response is
recovered by read-only nonce lookup. An unknown `intent_id` can never create or
execute a transaction.

The same package-lifecycle reservation also excludes an ordinary
`schedule_restart`: its detached supervisor acquires it nonblockingly and holds
it through its complete stop/start process work. The entrypoint receives the
admission result over a one-shot receipt channel. Contention returns structured
`busy`; no package-lifecycle path queues or waits. This is one shared primitive,
not a second lifecycle lock.

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
- a transaction-owned immutable recovery bootstrap containing the decoder,
  runner, validated DTO schema, and every rollback execution dependency; and
- all data needed to project a terminal or quarantined result.

The owner stages and hash-verifies that bootstrap before it writes `resolved`.
The transaction record names its immutable locator, schema version, and content
hash. The bootstrap is part of transaction staging, not an installed package or
a second lifecycle owner. Its complete recovery contract is Invariant 3 I3.

Immediately before the first package-mutating child command, the owner performs
a write-ahead transition to `state=mutating` in the same transaction record and
durably syncs it (temporary file, atomic replace, and parent-directory sync, or
the Windows equivalent). No package command may start before that marker is
durable. The marker is the recovery boundary: `admitted`, `captured`, and
`resolved` prove that no package mutation was started.

Before `mutating`, the supervisor may use the live implementation only to
capture, resolve, build, and validate the staged bundle and bootstrap. From
`mutating` onward the original supervisor and every elected recovery owner use
only the record-referenced bootstrap, bundle-held commands/data, and the
bootstrap's dependencies. A fail-loud guard rejects any Python import or
package/resource read from the environment being replaced. Rollback uses the
same bundle. Activation and health are observed only through contained external
child processes and bounded HTTP probes.

Every external command has immutable `execution_timeout_seconds`, enforced by
the staged runner. Command containment and reservation lifetime obey Invariant 3
I1 for both package transactions and ordinary restarts. The runner is only a
bundle executor: it cannot own lifecycle state, renew a deadline, start
recovery, or write the record. No second reservation, external reaper, or
independent coordination process is introduced. G3-2 owns bootstrap staging,
the package-supervisor hold, and process containment.

Failure semantics are closed:

- capture or resolution failure before admission rejects with no record or
  mutation; after durable `admitted` but before `mutating`, an ordinary forward
  lineage records terminal `failed` with a machine reason, leaves the
  installation untouched, appends the terminal audit entry, releases the
  reservation, and never enters quarantine, except that a record which already
  claims `resolved` but names an invalid I3 bootstrap is internally inconsistent
  and enters fail-closed quarantine;
- every pre-mutation loss in a repair lineage returns the current record to its
  retained quarantine state under the rule below; it never becomes terminal
  `failed` or permits a new forward intent;
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

### Quantified Reservation And Recovery Invariants

The following three invariants are exhaustive. They apply to every current and
future lifecycle stage; a stage is not exempt merely because it does not invoke
the package manager.

**I1: containment covers every reservation-protected command tree.** For every
lifecycle reservation holder in `{package transaction, ordinary restart}` and
every external command tree it launches while holding the reservation, exactly
one platform rule applies:

- on POSIX, the bounded runner owns its process group and inherits a duplicate
  of the detached supervisor's open-file-description lock; or
- on Windows, the detached supervisor assigns the runner and every descendant
  to a supervisor-owned Job Object with kill-on-owner-close before execution,
  while the runner inherits no byte-range lock ownership.

On both platforms the detached supervisor acquires the reservation itself,
keeps its primary descriptor or handle throughout its complete lifecycle, and
uses the one-shot receipt only to return admission. Starting or completing one
runner never releases the primary hold. If the supervisor dies, the POSIX
duplicate extends exclusion until the already-running bounded tree exits, while
the Windows Job Object terminates that tree before the supervisor handle closes.
This covers forward mutation, activation, verification, rollback,
rollback-verification, ordinary stop/start/readiness work, and every future
external stage. A completed handoff to the intended long-running Avibe service
is not protected command work, but the start runner must exit and readiness must
prove that handoff before release. Therefore, for every release of the
reservation, zero protected external work remains. Entrypoint-to-supervisor lock
handoff, parent-waits-only containment, an uncontained descendant, or a
stage-specific exception violates I1.

**I2: persisted transaction states form three disjoint sets.** Every persisted
state belongs to exactly one row:

| Set | Members and authority | Reservation projection |
| --- | --- | --- |
| Active-track nonterminal | `admitted`, `captured`, `resolved`, `mutating`, `activating`, `verifying`, `rolling_back`, `recovering`, and `repairing`; the durable record proves a package transaction exists | Held probe -> `indeterminate-held`; free probe -> `interrupted`. Neither result identifies the holder. Only `interrupted` permits same-intent election. |
| Authoritative parked | `quarantined`; deliberate lock release leaves the record as the authoritative projection and forward-mutation block | Entirely outside the held/free liveness overlay. Project `quarantined` and its exact recovery/repair affordance without relabeling it `interrupted`. |
| Terminal | `succeeded`, `restored`, `reconciled`, and admitted pre-mutation `failed` | No liveness overlay; return the terminal projection and retain it only through the bounded audit rule. |

Unadmitted `rejected`, `busy_package_transaction`,
`busy_pending_publication`, and generic `busy` responses are not transaction
states. G3-1B derives `busy_package_transaction` from an active-track record,
not from OS-lock ownership; an ordinary restart that happens to hold the lock
does not change that projection. A parked quarantine returns its authoritative
quarantine rejection instead. When `repair_quarantine` durably transitions the
same record to `admitted (repair)`, that repair lineage joins the active track
while retaining `quarantined_baseline`; every failure rule below returns it to
the parked set when required.

**I3: post-mutation recovery is independent of installed packages.** Before
`resolved`, G3-2 creates a transaction-owned immutable recovery bootstrap that
contains the decoder, runner, validated versioned DTO schema, rollback command
material, and every execution dependency needed by the recorded rollback. It
stores it in transaction staging outside every package install target, validates
the complete staged artifact, and records its immutable locator, schema version,
and content hash. It cannot write `resolved` until those facts are durable. From
`mutating` onward, both the original supervisor and an elected replacement
validate that hash and execute through the bootstrap. A replacement imports
only bootstrap-owned code; it never imports the installed `package_shape`,
package-lifecycle implementation, or another module/resource from the
environment being repaired. A record-referenced bootstrap that is missing,
corrupt, schema-invalid, or hash-mismatched produces fail-closed `quarantined`
without executing mutation or rollback. The bootstrap owns no record,
reservation, election, or acknowledgement; it is immutable execution staging
under the one lifecycle owner.

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
  projection exists and a nonblocking reservation probe reports availability,
  in which case one replay of the same nonce is allowed.

After an initial POST loses transport, the caller first uses recovery-by-nonce.
If that read confirms absence of a current/audit projection and a nonblocking
probe confirms reservation availability, it may replay the same nonce once
within the existing boundary; once any server identity or projection is
observed, it never submits a new attempt. Nonce idempotency is bounded to the
current record plus retained terminal audit. Once a terminal audit entry is
pruned, recovery returns `nonce_unknown`; a later explicit attempt uses a new
nonce. No unadmitted outcome is persisted or projected.

For an eligible new attempt, the adapter spawns the existing detached
supervisor with the immutable nonce, type, and payload. The supervisor:

1. acquires `package-lifecycle.lock` nonblockingly;
2. immediately mints a collision-resistant acquisition ID and overwrites the
   lock-file observability bytes with that ID, holder type, PID, and acquisition
   timestamp;
3. rechecks the current record under that reservation;
4. rejects source builds, unreadable global configuration, active transactions,
   quarantine, and operator-only package states before package capture or
   subprocess work;
5. mints a collision-resistant `intent_id`;
6. atomically and durably writes `schema_version`, `intent_id`, `client_nonce`,
   the non-authoritative reservation acquisition ID for diagnostics, type,
   payload digest, and `state=admitted` to the dedicated transaction record by
   writing and syncing a temporary file, replacing the record, and syncing its
   parent directory (or the Windows durability equivalent); and
7. only after that write is durable, returns the receipt through a one-shot
   inherited pipe to the adapter.

The supervisor retains the descriptor or handle that it acquired until the
transaction is terminal or quarantine is durably recorded; command runners can
receive only the bounded duplicate described in Invariant 2. Every admission or
acquisition outcome, including contention or rejection, returns through the
same one-shot receipt path. The pipe is response transport, not a coordination
primitive or durable acknowledgement. A lost receipt is reconstructed by nonce
lookup. Concurrent
same-nonce requests converge on the recorded identity: a contender that loses
the reservation rereads the current record and returns its identity/projection
when the nonce and canonical intent digest match. The server returns a matching
projection before any contention outcome.

The reservation layer proves liveness only. If its nonblocking acquisition
fails before a matching durable projection is visible, it may return retryable
`busy_pending_publication` while the adapter boundedly rereads the current
record. That result says only that the reservation is held and record
publication may still be settling; it makes no package, ordinary-restart, PID,
or acquisition-ID owner claim. Exhausting the bounded rereads returns generic
retryable `busy`. Lock-file publication bytes may be observed for diagnostics,
but they never authorize an owner-specific classification or correlation.

G3-1B applies I2. It derives `busy_package_transaction` from a durable
active-track record. It means that a package transaction exists; it does not
claim that the current OS reservation holder is that transaction. Held and free
probes project `indeterminate-held` and `interrupted` exactly as I2 defines. A
parked record instead projects authoritative `quarantined`, including its exact
reason and repair affordance. The caller does not submit a new forward intent
while either record class is current. Contention with an ordinary restart
remains generic reservation contention. The client never invents or persists an
`intent_id` before the server returns it.

Every API that accepts an `intent_id` is lookup or recovery only. An ID absent
from the current record and retained terminal audit returns stable
`intent_unknown`; it never mints a replacement, acquires for forward mutation,
or executes a package command.

### Transaction Record

`package_lifecycle_transaction.json` is separate from ordinary
`restart_status.json`. The lifecycle owner is its sole writer. The canonical
record starts at `schema_version: 1` and contains the current transaction
(identity, canonical intent digest, non-authoritative diagnostic reservation
acquisition ID, `repair_lineage`, retained `quarantined_baseline`, captured
shape, resolved bundle identity, recovery-bootstrap locator/schema/hash, current
transition, recovery facts, mutation-boundary marker, and the exact projection
needed by read-only adapters) plus an atomically updated `terminal_audit` array
containing at most ten structured terminal projections.
This is the fixed `N=10` retention window.
Each update writes and syncs a temporary file, atomically replaces the record,
and syncs its parent directory (or the Windows durability equivalent) before a
receipt or next admission is returned.

The `package_shape` owner supplies the versioned JSON-safe DTOs and codecs for
captured shape, rollback target, and execution-bundle package data. G3-1B owns
only the outer lifecycle record and persists those encoded values without
duplicating package-shape semantics. Before `resolved`, G3-2 uses that live
owner to validate the DTOs and build the I3 bootstrap. After `resolved`, only
the hash-verified bootstrap may decode those values or execute rollback; neither
the original nor a replacement supervisor rehydrates them through the installed
`package_shape` codec. Serialized DTOs and the bootstrap are not executable
authority and do not create another record owner, persistence surface, or
coordination channel.

Nonterminal current transactions are never rotated, pruned, overwritten, or
replaced by a new intent. When a later intent is durably admitted, only
terminal `succeeded`, `restored`, `reconciled`, or admitted pre-mutation
`failed` current records are appended to `terminal_audit`; the oldest terminal
entry is evicted beyond ten. `rejected`, record-derived
`busy_package_transaction`, `busy_pending_publication`, and generic `busy` are
unadmitted responses, so none creates or rotates a transaction record.
`restart-*.log` files remain human audit logs only; they
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
directory. On POSIX its advisory lock has open-file-description semantics and is
held on one open file descriptor. On Windows an exclusive byte-range lock is
held by the supervisor on a non-inherited handle. The reserved lock byte range
and the publication byte range are fixed and non-overlapping; publication must
never extend into the lock range.

The only authoritative liveness test is a nonblocking acquisition attempt on
that OS lock. After acquisition, a holder mints a collision-resistant
acquisition ID and overwrites, never appends to, the lock-file observability
bytes with that ID, holder type (`package` or `ordinary_restart`), PID, and
acquisition timestamp. Those bytes are diagnostic only: empty, unreadable,
stale, or apparently correlated publication never establishes who owns the OS
reservation. A contender may boundedly reread publication and the current
record while returning retryable `busy_pending_publication`, but that response
carries no owner claim; exhaustion returns generic retryable `busy`. No
acquisition-ID comparison or metadata correlation may produce an owner-specific
result. No second lock, heartbeat, token, lease, or durable ownership channel is
allowed.

For an I2 active-track record, a failed nonblocking probe means only
`indeterminate-held`; it cannot establish whether package or ordinary-restart
work holds the reservation. A successful probe, immediately released without
publication or lifecycle writes, proves that no protected process work remains
and projects `interrupted`. No timestamp, command deadline, PID, acquisition ID,
or diagnostic publication may infer interruption or correlate the current
holder to the transaction. The ambiguity is constructively bounded by I1:
every protected runner and ordinary restart is bounded, and the reservation
cannot become free while protected external work remains.

The package supervisor holds the reservation from `admitted` through every
active-track command and transition. If it enters quarantine with no protected
command running, it records `quarantined` durably and releases the live
reservation. I2 then makes that parked record, not a stale lock or a liveness
overlay, the authoritative forward-admission block.

Ordinary `schedule_restart` serializes its immutable request and spawns the
existing detached ordinary-restart supervisor with a one-shot receipt channel.
On both POSIX and Windows that supervisor, not the entrypoint, attempts one
nonblocking acquisition of the same reservation. After acquisition it publishes
the diagnostic metadata described above and reads the package transaction
record once before any stop/start work. If it finds an active-track record,
acquisition proves that record `interrupted`; it releases the reservation and
returns structured `blocked_interrupted_transaction` pointing to recovery. If
it finds authoritative parked `quarantined`, it releases the reservation and
returns structured `blocked_quarantined_transaction` with that record's repair
affordance, without relabeling it. Neither case starts a partial ordinary
stop/start. Contention returns generic retryable `busy` and never infers the
current holder from publication bytes. A restart explicitly owned by the
elected recovery transaction is exempt and uses that transaction's reservation.

After passing the record check, the ordinary-restart supervisor returns its
acquisition result through the one-shot receipt and keeps its primary descriptor
or handle continuously through restart status, stop, start, and readiness work.
Every external command tree in that sequence receives the I1 POSIX duplicate or
Windows Job Object containment before it starts; the ordinary supervisor cannot
release until no protected tree remains.
The receipt carries only the response; it is not a second lock, durable
acknowledgement, or transfer of reservation ownership. Neither platform passes
a reservation from the entrypoint to the detached supervisor. The supervisor
releases only when the sequence is complete. It does not write the package
transaction record or become a package transaction. Package admission while it
holds the reservation returns generic retryable `busy`.
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

An explicit same-`intent_id` request is the only active-track recovery-election
request. It makes one nonblocking acquisition attempt on the same lock. Failure
returns `indeterminate-held` without claiming that the holder owns the
transaction, and starts no recovery. Success proves `interrupted` and elects
that contender as recovery owner; other contenders return the durable
active-track projection or generic busy state. No deadline or diagnostic fact
may bypass this acquisition proof. Recovery never repeats forward mutation.

Parked `quarantined` never participates in that held/free election. Its
authoritative projection remains visible until the original same-intent exact
recovery or explicit `repair_quarantine` action acquires the reservation under
the rules below. Neither action first relabels quarantine as `interrupted`.

For `admitted` or `captured`, the elected owner needs no bootstrap or installed
shape inspection: the write-ahead boundary proves that mutation never began and
the pre-mutation lineage rules apply directly. For `resolved` or any later
active-track state, the elected replacement first validates the record-referenced
I3 bootstrap using its staged schema and hash. It imports and executes only that
bootstrap, never the installed `package_shape` or lifecycle implementation.
Missing, corrupt, or schema-invalid staging returns the record to authoritative
`quarantined` with the exact bootstrap reason. The bootstrap then reconciles
installed metadata with `CapturedPackageShape` and the recorded
`ResolvedRollbackPlan`:

- if the current state is `admitted`, `captured`, or `resolved`, the durable
  write-ahead marker proves no package command started; for an ordinary forward
  lineage, owner loss or a non-crash capture/resolution failure records terminal
  pre-mutation `failed` with its exact reason, appends terminal audit, releases
  the reservation, and immediately permits a new intent;
- if `repair_lineage=true`, every pre-mutation crash, owner loss, capture
  failure, or resolution failure in `admitted (repair)`, `captured`, or
  `resolved` durably restores `state=quarantined` with the retained
  `quarantined_baseline` and exact reason, releases the reservation, creates no
  terminal audit entry, and continues to reject forward intent;
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
   transitions `quarantined -> admitted (repair)`, sets
   `repair_lineage=true`, and retains the `quarantined_baseline` without a
   nested repair slot or second record.
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
  -> rejected | busy_package_transaction
  -> busy_pending_publication | busy
  -> admitted -> captured -> resolved
  -> failed (ordinary pre-resolution loss)
  -> quarantined (invalid resolved bootstrap) | mutating
  -> activating -> verifying -> succeeded
  -> rolling_back -> restored | quarantined
active-track + same-intent election
  -> recovering -> restored | quarantined
quarantined + original same-intent exact recovery
  -> recovering -> restored | quarantined
quarantined + explicit repair
  -> admitted (repair, repair_lineage=true) -> captured -> resolved
  -> repairing -> reconciled | quarantined
```

The persisted states and unadmitted responses have exactly the I2 partition.
Every busy outcome is retryable according to its rule above and never terminal.

`MEMORY-INDEP-020` reserves multiprocess admission and recovery evidence across
controller, Web, CLI, and Settings, including:

- nonce recovery after a lost admission response and `intent_unknown` for an
  invented or expired server ID;
- same-nonce convergence, bounded retry-neutral
  `busy_pending_publication` rereads with no owner claim, exhausted generic
  `busy`, and different-nonce contention;
- G3-1B record-derived `busy_package_transaction` for every active-track
  transaction, with held `indeterminate-held` and free `interrupted`
  projections that never infer the reservation holder, plus authoritative
  quarantine entirely outside that overlay;
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
- child timeout, reservation behavior after owner death, and a
  nonblocking acquisition that succeeds once no live reservation remains,
  including each package and ordinary supervisor's continuous primary hold,
  POSIX runner duplicate survival only through bounded exit, Windows Job Object
  kill-on-owner-close containment for every protected external command stage,
  and zero protected work at reservation release;
- staged I3 bootstrap construction and hash recording before `resolved`,
  post-mutation and replacement-owner execution without importing installed
  package-shape/lifecycle code, and missing/corrupt bootstrap quarantine; and
- quarantine entry, forward rejection, exact recovery, and explicit verified
  reconciliation exit.

The three Codex review rounds are closed by invariant rather than by example:

| Review round and comments | Root class | Quantified exclusion and required evidence |
| --- | --- | --- |
| Round 1: `3878813171`, `3878813178` | Supervisor hold across commands; detached cross-platform acquisition | I1 requires supervisor self-acquisition, continuous primary hold, response-only receipt, and runner-only duplicates. |
| Round 2: `3878989468`, `3878989479` | Owner-neutral active/interrupted evidence; all-stage Windows containment | I2 permits only held `indeterminate-held` or free `interrupted` for active-track records; I1 quantifies containment over every protected stage. |
| Round 3: `3879091837`, `3879091850`, `3879091858` | Ordinary-restart containment; quarantine/liveness separation; recovery independence from replaced code | I1 covers every ordinary-restart tree, I2 excludes parked quarantine from the liveness overlay, and I3 requires the hash-bound immutable recovery bootstrap. |

## Invariant 4: UI Recovery Is State-Derived

The caller generates `client_nonce` before the initial install POST. The POST,
nonce recovery request, and every later poll are inside one retry boundary; no
request is naked-awaited. Transport loss, UI restart, transient 404, and
temporary 5xx retain the last transport error and retry until the existing
dependency-install deadline.

If the POST response is lost, the caller queries by nonce. Once the server-issued
`intent_id` is known, the dependency adapter returns a live in-memory job when
present; otherwise it reads the dedicated transaction record and terminal audit.
It is read-only and never writes lifecycle state. A live in-memory job projects
`active`. Without one, G3-1B first applies I2 to the record: parked quarantine
projects its authoritative structured state without probing liveness; only an
active-track record is combined with an owner-neutral nonblocking reservation
probe. Probe failure projects internal `indeterminate-held`, while success is
immediately released and projects `interrupted`. `indeterminate-held` maps to
the existing nonterminal `status=in_progress` without a recovery affordance, so
the UI gains no new status or behavior; `interrupted` maps to the same status
with its existing recovery entry. The external active-track admission result is
`busy_package_transaction` because the durable record exists, never because a
holder was inferred. Only an explicit same-`intent_id` recovery request may
trigger the election described in Invariant 3, so polling never becomes an
owner. During the short admission publication window,
`busy_pending_publication` is a retryable nonterminal response: the caller
boundedly rereads the current record by the same nonce without assuming an owner
type or minting a new nonce. Exhaustion returns generic retryable `busy`.

Projection rules are:

- active-track, indeterminate-held, or interrupted state returns nonterminal
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
- retryable `in_progress` for an active or indeterminate-held transaction,
  including when the last read failed after either earlier projection;
- retryable `in_progress` for an interrupted nonterminal transaction with a
  recovery affordance; interrupted is never classified as transport
  exhaustion;
- structured `quarantined`; or
- terminal `dependency_poll_transport_exhausted` with sanitized
  `last_transport_error` only when transport exhausted without any structured
  terminal, active, indeterminate-held, interrupted, or quarantined projection.

The polling deadline never converts `indeterminate-held` to `interrupted` and
never authorizes recovery. Only a successful owner-neutral reservation probe
does so; bounded command execution and process-close lock release guarantee that
the ambiguous interval itself is bounded without a heartbeat or owner claim.

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
| `MEMORY-INDEP-018` | UI recovery follows nonce, server identity, owner-neutral liveness, and final readiness | POST-loss retry boundary; nonce/ID projection truth table; transport, active/indeterminate-held/interrupted, quarantined, and terminal deadline results; no deadline-based interruption | Real wheels: Settings repair, response loss, all-process restart, over-deadline recovery, quarantine projection, convergence |
| `MEMORY-INDEP-019` | Capture is exact and every constructed rollback is staged and executable | Provider-cardinality property; private plan construction; cleanup verification; capture/resolution failure to terminal `failed`; resolution and rollback failure injection | Wheelhouse matrix for core-only and optional split shapes, duplicate providers, partial mutation, activation failure, exact restore, quarantine |
| `MEMORY-INDEP-020` | One server owner and reservation cover admission through recovery and ordinary restart exclusion | G3-1A liveness-only reservation with diagnostic publication, bounded owner-neutral `busy_pending_publication`, generic contention, and non-overlapping Windows lock/publication bytes; G3-1B multiprocess nonce/identity contention, versioned JSON-safe package-shape DTO storage, I2 active-track/parked/terminal partition, record-derived `busy_package_transaction`, ordinary pre-mutation crash to terminal `failed`, repair-lineage loss back to quarantine, server rejection, legacy fixture, and nonterminal retention; G3-2 package-supervisor acquisition and continuous primary hold, I3 immutable recovery bootstrap, frozen execution, child timeout, I1 all-stage POSIX runner duplicate survival and Windows Job Object kill-on-owner-close, zero protected work at reservation release, no-live-reservation acquisition, and quarantine enter/exit; G3-3 platform-symmetric ordinary-restart supervisor acquisition, record check, one-shot receipt, full hold, and I1 command-tree containment | Concurrent controller/Web/CLI/Settings requests, restart contention, killed-owner recovery without installed package code, exact package and service health |
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
| #1741 pre-mutation frozen bundle and child timeout | Preserve inside the hash-bound I3 recovery bootstrap and I1 containment boundary |
| #1741 caller-issued identity | Replace with caller nonce and server-issued persisted identity |
| #1741 one-shot ordinary restart probe | Replace with detached-supervisor acquire, record check, response-only receipt, and full hold through ordinary restart process work |
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
4. **Gate 3, lifecycle owner:** after both documents merge, implement
   `MEMORY-INDEP-020` in bounded steps: G3-1A owns the liveness-only OS
   reservation primitive, diagnostic publication, generic contention, and
   non-overlapping Windows lock-byte layout; G3-1B owns admission, the outer
   versioned record and audit, nonce/identity, opaque JSON-safe `package_shape`
   DTO storage, the I2 active-track/parked/terminal partition, record-derived
   `busy_package_transaction`, recovery election, and quarantine; G3-2 owns the
   package detached supervisor's acquisition and continuous primary hold, I3
   bootstrap construction/hash validation, frozen execution, all-stage POSIX
   runner duplicates, child deadlines, and Windows Job Object
   kill-on-owner-close containment for every protected external command tree;
   G3-3 owns the platform-symmetric ordinary restart supervisor acquisition,
   lifecycle-record check, response-only receipt, continuous restart hold, and
   I1 containment for every stop/start/readiness command tree. No step may infer
   reservation ownership from publication metadata or import installed
   package-shape/lifecycle code for post-mutation recovery.
5. **Gate 4, recovery adapter:** after both documents merge, implement
   nonce/identity UI convergence and packaged `MEMORY-INDEP-018`.

Doc A approval alone authorizes only gate 2a. Rollback, lifecycle, and UI gates
require the merged Doc A and Doc B contracts plus separate owner approval;
release/migration gate 5 remains separately gated by Doc B. Each implementation
PR must use exact packaged artifacts where package shape matters, must not
restart the local Avibe service for verification, and must stop if ownership
spreads beyond the loader, lifecycle transaction, or dependency adapter.
