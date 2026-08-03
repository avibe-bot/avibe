# Harness Run Reliability

Status (2026-08-03): **PR1, PR2, PR5, PR6, and #1139's Activity-output
settlement closure are complete. PR3 and PR4's shared-drain liveness work
remain. PR4's transport-attempt delta and PR7 must be re-baselined against the
durable Delivery/Turn/Activity model before implementation.**

This is the execution plan, not the investigation log. The original detailed
diagnosis and its review history remain available in Git before `fe821905`.
Resolve code references by symbol against current `master`; do not port old line
numbers or old ownership assumptions.

## 1. Current baseline

| Capability | Result |
|---|---|
| Result text for delivered Harness runs | **#1063 merged** |
| Pinned-session reclaim and reservation hardening | **#1064 merged** |
| Durable, user-visible failure notices | **#1072 merged** |
| Delivery / Turn / Message ownership model | **#1134 merged** |
| Teardown-interrupted Run settlement | **#1140 merged**; supersedes closed #1131 |
| Activity output batch receipt and local settlement | **#1139 merged**; supersedes #1121 |
| Idle-eviction interlock for queued work | **Open — PR3** |
| Bounded and supervised shared drains | **Open — PR4**; attempt-state delta requires a current-master reproducer |
| Scheduled/watch terminal-time truth and cron liveness | **Re-baseline — PR7** |

The post-plan architecture is load-bearing:

- `message_deliveries` owns submitted input, FIFO position, acceptance,
  attempts, receipts, and retirement.
- `session_turns` owns native execution and terminal Turn evidence.
- `messages` is accepted communication history, not a queue.
- durable Activity snapshots own pending output payload, ordered batch
  membership, the linked Run union, and the stable output receipt identity.
  Accepted Message evidence or a persisted Activity batch marked for local
  settlement only prevents transport replay; a delivered output that still owes
  local settlement retries only that settlement.
- `agent_runs` is the Harness projection and callback/notice anchor.
- #1140 distinguishes an unstarted claim from an execution that crossed the
  running boundary. Unstarted work returns to queued; infrastructure-interrupted
  running work fails and is not replayed; explicit user Stop remains canceled.

Consequently, the old PR2 resolver design, PR7's old “claimed message row”
decision, and any pre-#1139 single-Activity output assumptions are obsolete. New
work must use the current durable owners instead of recreating side maps or a
parallel output ledger.

## 2. Invariants

Every remaining change must preserve these rules:

1. **One durable owner per phase.** Delivery owns input before native
   acceptance; Turn owns accepted execution; the Activity batch owns pending
   terminal output; Run reflects the outcome.
2. **No silent replay after execution starts.** Infrastructure interruption is
   `failed` with a structured cause and an actionable notice. User Stop is
   `canceled` and does not generate an infrastructure-failure notice.
3. **Unstarted work remains retryable.** A bare claim or queued Delivery must not
   be mislabeled as an interrupted execution.
4. **Terminal writes are guarded.** A natural terminal result, user Stop, and
   infrastructure teardown may race; the exact compare-and-set winner controls
   projections and notices.
5. **Absence of a receipt is not proof of non-delivery.** Any timeout around an
   outbound send must use durable delivery evidence before retrying.
6. **Waiting is not activity.** A real inbound message or exact Turn start may
   establish a session baseline; after that only observable assistant/tool
   progress refreshes it. Run inactivity is stricter and re-arms only from its
   exact owning Turn. A claim, queue wait, gate wait, or unrelated Run in the
   same session must not keep stuck work alive.
7. **No turn-duration timeout.** A healthy turn may run for hours. Bounds may
   apply to inactivity and post-turn delivery, never to productive execution.
8. **Failures remain visible.** Reconcile paths must use #1072's durable notice
   path; a terminal row alone is not the exit criterion.
9. **One output batch has one receipt.** Preserve #1139's stable receipt,
   persisted ordered Activity membership, complete linked Run union, and
   transport-free local-settlement retry. Incomplete or conflicting recovered
   membership fails closed; it never emits a partial batch or invents a second
   receipt.

## 3. Runtime supervision design

Consider one concrete Session. It has an old P3 Delivery `Q1`, queue hold is
`held`, and no Turn is live. The backend process may be reclaimed because `Q1`
is durable and policy deliberately forbids autonomous drain. Later the user
issues send-now: the existing synchronous writer validates the observed head,
releases the hold, and claims exactly `Q1` in one transaction before returning.
The backend runtime is recreated only when that Turn starts. A subsequent
`queue.updated` hint is for projection and passive recovery, never the authority
for send-now's stale-head/refusal result. At no point is `last_activity`
falsified, and neither the hint nor the reclaimer owns `Q1`.

Now suppose recovered Activity output delivery is hung at the same time. Its
`activity_outputs` lane remains single-flight and overdue, but the independent
`session_deliveries` lane still starts `Q1`. This is the user-visible outcome
the design must produce.

### 3.1 Separate facts, hints, and resources

The implementation has three layers with deliberately different failure
semantics:

1. **Durable facts** are the existing Delivery, Turn, Activity, and Run rows.
   They remain correct across process death and are the only source of truth for
   work ownership and settlement.
2. **Wake hints** say only "durable state may now be actionable; read it again".
   They carry no payload or lifecycle authority, may be coalesced, and may be
   lost without losing work.
3. **Runtime resources** are backend clients, transports, asyncio tasks, and
   worker threads. They are disposable projections that may be reclaimed only
   after the durable facts prove that no exact owner still needs them.

This separates two existing loops that must not become one larger loop:

- the **session runtime reclaimer** releases idle backend resources;
- the **Harness work supervisor** activates and reconciles durable work.

The reclaimer never executes queued work and the work supervisor never decides
that a durable Session row should be deleted. They share one read-only runtime
ownership provider and one wake interface, not one coroutine.

### 3.2 Derived session disposition

The runtime ownership provider reads Delivery, Turn, Session hold, active
Activity, and fallback Run facts from one SQLite read transaction and returns
one immutable snapshot. It does not persist a new state or maintain an
in-memory shadow ledger. It is strictly side-effect free: ownership reads must
not release locks, repair projections, settle Runs, or call helpers such as
`owned_agent_run_ids()` that mutate while answering.

```python
class SessionRuntimeDisposition(str, Enum):
    ACTIVE = "active"          # an exact live owner needs the runtime
    TRANSITIONING = "transitioning"  # an unresolved handoff forbids reclamation
    RUNNABLE = "runnable"      # durable work should be activated now
    WAITING = "waiting"        # durable policy intentionally blocks activation
    RECLAIMABLE = "reclaimable"  # no durable or in-process owner remains
    UNKNOWN = "unknown"        # the snapshot could not prove safety

@dataclass(frozen=True)
class SessionRuntimeOwnershipSnapshot:
    session_id: str
    disposition: SessionRuntimeDisposition
    delivery_ids: tuple[str, ...]
    turn_ids: tuple[str, ...]
    active_activity_ids: tuple[str, ...]
    fallback_run_ids: tuple[str, ...]
    queue_hold_state: str
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class RuntimeTargetOwnershipSnapshot:
    backend: str
    runtime_key: str
    sessions: tuple[SessionRuntimeOwnershipSnapshot, ...]
    sessionless_active_activity_ids: tuple[str, ...]
    disposition: SessionRuntimeDisposition
```

Reclamation operates on a runtime target, not blindly on one Session. Claude's
runtime key is a session-base/workdir composite, while one Codex transport is
keyed by cwd and can serve several Sessions. Each backend exposes only its
current runtime key plus candidate session bases; the shared provider resolves
those bases to durable `agent_sessions` rows and then computes each Session
snapshot in the same SQLite transaction. The same query also collects active
Activity rows by exact backend/runtime key even when `session_id` is NULL; those
rows are target-level owners and force `active`. The target disposition is the
safest member disposition, using `unknown > active > transitioning > runnable >
waiting > reclaimable` for the aggregate. Before the cleanup decision, every
runnable member emits its wake even when another member already blocks the
shared target. Any target-level active Activity or `active`, `transitioning`, or
`unknown` Session blocks target cleanup.
`runnable` wakes activation but, like `waiting`, does not itself own the current
backend process; an all-`runnable|waiting|reclaimable` target may release idle
resources after the final locked recheck. A positively orphaned runtime with no
durable binding may be reclaimed, but a mapping error is `unknown`.

The exact classification is:

| Durable fact | Derived disposition | Runtime effect |
|---|---|---|
| `starting|active` Turn, Turn-owned Delivery, persisted active Activity, or exact in-process backend operation | `active` | forbid ordinary idle reclamation |
| unresolved Delivery fence, waiting successor, or an ownership handoff that cannot yet be classified | `transitioning` | fail closed; reconcile the exact owner |
| open-hold ownerless FIFO head or queued execution-bearing Run not yet represented by a Delivery | `runnable` | wake its delivery/request lane; it does not itself pin backend resources |
| held FIFO backlog with no live Turn/fence | `waiting` | preserve durable work but allow disposable backend runtime reclamation |
| terminal Delivery/Turn history and terminal Runs only | `reclaimable` | no pin |
| read failure, unknown Delivery state, or unknown execution-bearing Run type | `unknown` | fail closed for this cycle and log the reason |

`queued` therefore does not mean "keep a backend process alive forever". An
open queue head means "activate it now"; a held queue means "keep it durable but
do not execute it". Both survive runtime reclamation. This resolves the apparent
contradiction between an idle runtime and durable queued input without inventing
fake activity.

The provider consumes `DELIVERY_STATE_MATRIX` and the current Turn-state
contract. It may add a derived policy helper beside that matrix, but it may not
retype Delivery state names in cleanup callers. `watch_runtime` is supervisor
bookkeeping and never an execution owner. An Activity persisted in `active`
phase pins its exact runtime; `awaiting_output` and terminal Activity snapshots
are settlement-only and do not pin backend resources. Unknown Run or Activity
types fail closed until the shared classification deliberately names them.

### 3.3 Activation before reclamation

Every reclaimer pass follows one order:

1. Read the exact ownership snapshot.
2. For a runnable Delivery, emit a coalescing `session_deliveries` wake. A
   fallback queued Run emits `requests` instead. The row is durable work, not a
   runtime pin.
3. For `active`, `transitioning`, or `unknown`, skip reclamation.
4. For `runnable`, `waiting`, or `reclaimable`, apply the existing idle grace
   rule without mutating Session, hold, queue, Delivery, or Run.
5. Acquire the same backend runtime-generation mutation lock used by Turn start,
   then read the snapshot again immediately before cleanup. A new exact owner or
   replaced generation wins; a still-runnable row may outlive the reclaimed
   runtime and later recreate it safely.

The reclaimer does not wait for the request to finish. The wake is a hint; the
`session_deliveries` lane later claims the exact FIFO head under the existing guarded
Delivery/Turn transaction. If the hint is lost, startup and periodic
reconciliation rediscover the same row. A losing claim re-reads; invalid input
is retired by the existing exact-Delivery validation; temporary unavailability
leaves the row durable and uses bounded lane backoff. None of these outcomes
refreshes `session_last_activity` or pins the old runtime.

Activation never depends on a runtime target already existing or reaching the
cleanup loop. The `session_deliveries` lane queries open-hold Session ids with a
queued head and no live Turn in bounded pages, then invokes a narrow
`SessionTurnManager` recovery/claim entry for each exact Session. Queue events,
startup, and periodic reconciliation are the primary triggers; the reclaimer's
wake closes only the admission-versus-cleanup race.

The stuck-active backstop is not an ordinary idle timeout. It applies only when
the exact owner has stopped producing observable progress past the existing
threshold, and it routes through #1140's guarded teardown settlement. A long
tool call without progress may still hit this explicit infrastructure-failure
policy; this work does not add another recovery layer or silently replay it.

### 3.4 Work lanes and wake protocol

Add one controller-owned `RuntimeWorkSupervisor` and replace `_watch_store`'s
serial executor role with independently registered single-flight lanes:

```python
class RuntimeWorkLane(str, Enum):
    SESSION_DELIVERIES = "session_deliveries"
    DEFINITIONS = "definitions"
    REQUESTS = "requests"
    RUN_CALLBACKS = "run_callbacks"
    VAULT_CALLBACKS = "vault_callbacks"
    ACTIVITY_OUTPUTS = "activity_outputs"
    FAILURE_NOTICES = "failure_notices"
    STALE_RUNS = "stale_runs"

def notify_runtime_work(*lanes: RuntimeWorkLane) -> None:
    """Coalesce wake hints; never carry durable payload or imply acceptance."""
```

The supervisor owns scheduling only. `SessionTurnManager` registers the
`session_deliveries` handler; `ScheduledTaskService` registers the Harness
handlers only while its service lease is valid. Registration returns a
generation token; duplicate live registration is rejected, and unregistering a
token suppresses re-arm from that generation before awaiting its exact worker.
An event for an unregistered lane is discarded because startup reconciliation
is mandatory when a new handler registers. Each lane owns exactly one event,
one current async task, and, when
synchronous work is delegated, one underlying executor future. A wake while the
lane is running sets the event for another bounded pass; it never starts a
second worker. A bounded page that leaves eligible work re-arms itself. A
transient failure records bounded backoff for that lane only. One lane cannot
await or cancel another lane.

The supervisor belongs to one controller process generation and is gated by the
same global service-instance ownership check as dispatch. Controller shutdown or
global lease loss stops and joins **all** lanes, including
`session_deliveries`, before that process releases shared state. A routine
`ScheduledTaskService.stop()` unregisters and joins only its Harness lane tokens;
it does not stop Session delivery recovery while the controller still owns the
instance. Every lane rechecks generation and global ownership before querying or
claiming, so a superseded controller cannot continue passive recovery.

Official producers use **commit then notify**:

1. Commit the authoritative Delivery, Run, Vault, Activity, or definition
   change.
2. Notify the affected lane(s).
3. Treat notification failure as a logged liveness hint failure, not as rollback
   of the committed work.

Transport readiness is the one non-commit capability edge. After an IM
transport positively becomes ready, the controller directly wakes `requests` so
Runs previously skipped for transport unavailability do not wait for periodic
reconciliation. The lane still re-reads durable eligibility and the readiness
hint never selects or claims a Run.

Reuse `core.inbox_events.bus` as the in-process hint source and the existing
permission-restricted `POST /internal/events` Unix-socket bridge for
cross-process hints. Do not add a second endpoint or socket. The supervisor
subscribes through `subscribe_callback` and maps event *types* to lanes; event
payloads remain UI metadata and are never trusted as work state. `runs.updated`,
`queue.updated`, and `vaults.updated` already cover most producers; add one
allowlisted `definitions.updated` event for definition mutations. Activity
recovery can notify its lane directly because its producer is controller-owned.

The external-process bridge must not depend on that process having zero local
browser/event subscribers. Browser presence is unrelated to whether the
controller received the hint. A non-controller publisher bridges first after
commit; on success the controller bus supplies the UI fanout and lane wake, and
on socket failure it falls back to local UI fanout. This avoids duplicate browser
events while keeping the UI responsive when the controller is unavailable.
Socket absence or timeout leaves the command successful because the durable row
is still authoritative.

The producer map is load-bearing:

| Observed edge | Wake lane(s) |
|---|---|
| Delivery becomes queued with open hold or terminal Turn exposes an open successor/head | `session_deliveries` |
| committed `definitions.updated` after add/update/pause/resume/remove | `definitions` |
| queued Harness Run or callback-created Run | `requests` |
| terminal Run that owes a Session callback | `run_callbacks` |
| resolved Vault request that owes auto-resume | `vault_callbacks` |
| persisted recoverable Activity output or local-settlement debt | `activity_outputs` |
| terminal failure that owes a durable notice | `failure_notices` |
| IM transport becomes ready | direct `requests` wake after readiness |
| no event: vanished owner or time eligibility | `stale_runs` periodic eligibility |

The bus-to-lane map ignores event payloads:

| Event | Coalesced lanes |
|---|---|
| `queue.updated` | `session_deliveries` |
| `runs.updated` | `requests`, `run_callbacks`, `failure_notices` |
| `vaults.updated` | `vault_callbacks` |
| `definitions.updated` | `definitions` |
| controller-local Activity recovery hint | `activity_outputs` |
| startup / 30-second reconcile | every registered lane, including `stale_runs` |

The implementation must inventory all official producer entry points and add a
table-driven contract test that invokes each one against a real SQLite file. It
must observe no hint before commit or after rollback/losing CAS, and the mapped
hint after a successful commit. Storage functions themselves do not perform
socket I/O and no notification is sent from inside a transaction.

### 3.5 Reconciliation is a safety net

The controller constructs the supervisor paused, registers available handlers,
restores backend identities and durable Delivery/Turn owners, then activates the
supervisor and wakes every registered lane before normal dispatch exposure.
Hints received while paused may coalesce, but startup reconciliation is the
load-bearing recovery. During steady state, one slow reconciliation
timer wakes all lanes and lets each lane re-read its own indexed eligibility.
After producer coverage is proven, the final default is 30 seconds, expressed as
an internal constant with an injectable test clock rather than a public product
setting. Normal latency comes from commit-triggered wakes, not this timer. The
timer never executes storage or delivery work inline.

The injected legacy file stores are the only compatibility exception. They have
no transactional post-commit publisher, so their existing two-second file or
directory signature probes remain as temporary hint sources. A changed
definition signature wakes `definitions`; a changed request-directory signature
wakes both `requests` and `run_callbacks`, because that directory also stores
terminal Runs awaiting callback delivery. Each signature check is separately
tracked off the event loop; it performs no reload, claim, callback, or settlement
inline and cannot block another probe or lane. The compatibility probes are not
enabled for the production SQLite stores. This preserves legacy test/tool
responsiveness without making two-second polling the production executor.
Removing those file stores removes the probes with them.

`PRAGMA data_version` may avoid an unnecessary reload but cannot be the only
edge that keeps work alive: another consumer can observe the edge first, a
process can die between commit and notification, and time-based eligibility has
no write edge. Every lane must therefore be restart-recoverable from its durable
query alone.

Global lease loss follows the same stop path as controller cleanup. A later
controller generation performs its own startup reconciliation; no lane or event
subscription is transferred between generations.

### 3.6 Blocking work, supervision, and shutdown

No synchronous SQLite or filesystem call runs on the controller event loop
unless a contention test proves it cannot block. Synchronous work runs through
a tracked executor future. A wrapper timeout does not cancel that underlying
operation or release the lane's single-flight ownership:

- the future remains the lane owner until it actually exits;
- the lane is marked overdue and does not launch a replacement;
- an independent supervisor logs the exact overdue lane;
- completion re-arms the lane if durable work remains;
- shutdown stops new wakes, cancels async work, and joins each exact owner before
  disposing shared stores.

SQLite operations must retain the repository's bounded busy timeout. A rare
compound failure that outlives the controller's shutdown grace is logged as
critical, retains its store/executor dependencies, and refuses same-process
service restart until the exact future exits. Process shutdown may then own
termination; the service must not dispose shared state underneath the thread or
launch a new generation beside it. This design does not add worker processes,
killable thread pools, or a second recovery ledger solely to mask that case.

The current SQLite busy timeout is five seconds, equal to the controller's
generic five-second coroutine-stop wait. PR4 must not reuse that equal deadline
for a supervised lane: the lane gets a separate shutdown grace longer than the
database busy bound, and a grace expiry marks the controller generation tainted.
`cleanup_sync()` returns whether the service-instance lock is safe to release.
On a tainted process shutdown it skips disposal of the exact
supervisor/store/executor dependency set, refuses any in-process restart, and
does **not** explicitly release the service-instance lock. The operating system
releases that lock only when the process, including the old worker, actually
exits; a replacement process therefore cannot overlap it. This result must be
wired through both signal cleanup and the normal `controller.run()` finally
path, outside the generic five-second wrapper. This is bounded failure
containment, not a retry or permission to overlap work.

### 3.7 No new durable owner

The wake/supervision layer adds no table and no item lifecycle. It must not:

- copy Delivery content or FIFO order into a work lane;
- copy Activity output payload, membership, receipt, or settlement into a lane;
- let a wake imply native acceptance;
- let a watchdog write Turn terminal state;
- infer retry safety from a missing in-memory task.

The lane always re-reads and invokes the existing guarded owner API. #1139's
Activity batch remains the only owner for recovered output and #1140's terminal
CAS remains the only owner for teardown settlement.

## 4. Required pre-code baseline

Before each remaining PR, write or run a current-master reproducer. Classify the
old defect as `reproduces`, `fixed by #1134/#1139/#1140`, or `superseded by a
new owner`. Do not preserve an old prescription merely because the symptom still
has the same name.

Minimum baseline cases:

- open and held FIFO heads, every Delivery fence/Turn-owned role, every live Turn
  state, terminal history, and fallback Run ownership each reach both passes of
  runtime reclamation and produce the §3.2 disposition;
- each backend-specific idle eviction path is classified as requiring the shared
  owner interlock or as restart-safe for queued work;
- a complete #1139 Activity output batch with multiple Activities and linked
  Runs is restored in its persisted order; accepted-Message evidence and the
  persisted local-settlement-only marker each prevent transport replay while
  local settlement is retried;
- recovered Activity output delivery hangs while a new Harness request becomes
  ready;
- scheduled and watch Runs transfer to a durable Delivery/Turn and later produce
  success, failure, and result-less terminal outcomes;
- definition health is observed before and after the owning Run becomes
  terminal;
- scheduler shutdown/restart distinguishes unstarted, running, naturally
  terminal, and user-stopped work.

## 5. PR3 — Session runtime ownership and activation

### Goal

Make runtime reclamation and durable work activation consume one coherent
session disposition. Active or transitioning work must not lose its runtime,
open runnable work must be woken, and held backlog must remain durable without
keeping a backend process alive forever.

### Required behavior

1. Implement the §3.2 ownership provider as one SQLite read transaction over
   `agent_sessions`, `message_deliveries`, `session_turns`, and fallback
   `agent_runs`, plus Activity rows in the existing `runtime_records` aggregate.
   Reuse the existing engine and storage tables; add no table,
   cache, or background resolver. Resolve a backend runtime target to every
   durable Session it can serve, then classify those Sessions in the same
   transaction. Also classify active Activity rows with a matching
   backend/runtime key and NULL Session as target-level owners. A returned
   snapshot is valid only for that transaction and exact runtime target.
2. Derive `active`, `transitioning`, `runnable`, `waiting`, `reclaimable`, or
   `unknown` exactly as §3.2 specifies. In particular:
   - an open, ownerless FIFO head is `runnable`, not a permanent runtime pin;
   - a held backlog with no live owner is `waiting` and may release backend
     resources without changing durable queue state;
   - a fence or Turn owner forbids reclamation;
   - terminal history and `watch_runtime` do not pin;
   - a queued or running execution-bearing Run is considered only when its exact
     Delivery/Turn representation is absent from the same snapshot.
3. Inventory Claude, Codex, and OpenCode runtime-reclamation paths. Every path
   that can invalidate the exact runtime needed by a live owner consumes the
   shared provider. A backend transport cache that is proven restart-safe may
   reclaim under `waiting`, but requires a consuming test; do not clone provider
   rules into adapters. A shared Codex cwd transport may be reclaimed only when
   every associated Session is `runnable|waiting|reclaimable`, after all
   runnable members have been woken and the locked recheck still finds no live
   owner. Claude composite keys must be mapped by persisted anchor/workdir
   identity, not string-prefix guesses.
4. Add the controller-owned `RuntimeWorkSupervisor` contract and register
   `SessionTurnManager` as the sole `session_deliveries` handler. Consult the
   provider in both passes of `evict_idle_sessions`. On a runnable Delivery,
   notify that lane but do not turn durable unclaimed work into an unbounded
   runtime pin; a fallback queued Run not yet represented by Delivery notifies
   `requests`. Recompute under the exact backend runtime-generation mutation
   lock immediately before cleanup. A newly committed claim/Turn or replaced
   generation wins; a still-runnable row survives runtime reclamation.
   The lane itself uses a bounded indexed query for open-hold queued Sessions
   without live Turns and invokes one exact-Session manager entry; it is not a
   full global `recover_durable_delivery_state()` call hidden inside cleanup.
5. Use two failure modes:
   - a lookup that positively proves one binding is dangling fails open for only
     that binding, so a deleted target cannot pin an unrelated session forever;
   - any exception while resolving a binding, or any provider-wide failure,
     fails closed for the eviction cycle, because missing safety data is not
     evidence that eviction is safe.
6. Bound exact active/transitioning ownership with the existing real-progress
   inactivity clock and
   stuck-active threshold. A newly admitted pin does not restart that clock.
   Beyond the bound, use the #1140 teardown path to settle exact running
   ownership and preserve queued/unstarted work.
7. Inventory every `session_last_activity` writer. Do not touch it on claim,
   enqueue, gate wait, output wait, polling/protocol frames, wake, provider
   lookup, or unrelated Session progress. Real inbound/Turn-start baselines and
   subsequently attributable assistant/tool/active-Activity progress are the
   only liveness signals. Shared Codex transport activity cannot refresh another
   Session's progress clock.
8. Keep activation asynchronous. The reclaimer emits only the coalescing wake;
   `SessionTurnManager` remains the claimant and Turn creator. A wake must never
   select a different queue head, bypass hold, or mutate Delivery/Run state.
   Existing synchronous commands such as send-now keep their exact-head
   validation, hold release, claim, refusal, and response semantics; their
   events only trigger passive recovery after the guarded result is committed.

### Required evidence

- `HFR-130`: one SQLite snapshot classifies every Delivery/Turn/Activity/Run
  ownership combination without a torn handoff;
- `HFR-131`: an open ownerless FIFO head wakes `session_deliveries`, remains
  durable if its idle runtime is reclaimed, and is later claimed through the
  existing exact-head transaction;
- `HFR-132`: a held FIFO backlog survives while its disposable backend runtime
  is reclaimed, then starts after the existing explicit hold release;
- `HFR-133`: unresolved fence and Turn-owned Delivery states forbid cleanup;
- `HFR-134`: active/starting/waiting Turns forbid cleanup while terminal Turn
  and Delivery history alone do not;
- `HFR-135`: active Activity rows pin their exact runtime, while `awaiting_output` and
  terminal Activity snapshots do not;
- `HFR-147`: an active Activity with no Session id pins its exact backend/runtime
  target and cannot be mistaken for an orphan;
- `HFR-136`: a `watch_runtime` heartbeat sharing the same definition/session does not pin,
  while an execution-bearing watch Run does;
- `HFR-137`: a pin admitted between eviction passes wins;
- `HFR-138`: bare-Run to reserved-Delivery and Delivery to Turn ownership handoffs cannot
  disappear across a torn provider read;
- `HFR-139`: unrelated sessions are not pinned;
- `HFR-140`: one positively missing binding fails open, while a per-binding
  lookup exception or provider failure aborts the cycle;
- `HFR-141`: repeated queued followers do not refresh progress or make a held Session
  immortal; teardown settles only exact running ownership;
- `HFR-142`: a claimed or gate-waiting request does not refresh activity;
- `HFR-143`: a long turn with observable progress is not evicted;
- `HFR-144`: the stuck threshold settles only the exact active owner through
  #1140 and never replays it;
- `HFR-145`: every enabled backend eviction path either honors the provider or proves queued
  work resumes without loss or replay;
- `HFR-146`: one active Session prevents reclamation of a shared Codex cwd
  transport while an unrelated held Session on that transport does not create
  fake activity; Claude composite-key mapping uses the exact durable binding.

Exit criterion: open work is activated, held work remains durable without
pinning resources, no productive or transitioning owner is reclaimed, and the
provider cannot make a stuck session immortal.

## 6. PR4 — Event-first supervised work lanes

### Goal

One hung or unbounded request, Run-callback, vault-callback, or post-turn-output
pass must not block another Harness tenant. Normal work begins from a
post-commit wake; startup and slow reconciliation repair lost hints. This is the
direct fix for the observed 65-minute serial-loop stall. #1139 remains the sole
Activity-output owner.

### Required behavior

1. Extend the controller-owned §3.4 `RuntimeWorkSupervisor`; do not add a second
   scheduler inside `ScheduledTaskService`. Replace `_watch_store` with Harness
   lane registration plus a slow reconciliation timer; it must not await a
   drain or perform storage I/O.
2. Give `definitions`, `requests`, `run_callbacks`, `vault_callbacks`,
   `activity_outputs`, `failure_notices`, and `stale_runs` independent
   single-flight state. Reuse current drain functions behind the lane boundary
   where their ownership is already correct; split only unbounded passes into
   bounded pages. Do not create seven new service classes merely to rename seven
   methods.
3. Implement one shared coalescing notifier. An event means "query durable
   eligibility again" and carries no item id. A running lane consumes the event
   before its next page so a concurrent commit cannot be lost between the final
   query and task exit.
4. Subscribe the supervisor to the existing inbox event bus and reuse the
   `/internal/events` socket bridge. Add only `definitions.updated` to the
   allowlist and shared event vocabulary. Map events to coalescing lane wakes;
   ignore payload identity and status when deciding durable work. Make external
   publishers bridge-first with local fanout only as the unavailable-controller
   fallback, so local subscribers never suppress the controller bridge or see
   duplicate successful-bridge events.
5. Instrument every producer in the §3.4 map after its authoritative commit.
   Cross-process notification is best effort and short-lived. In-process
   notification is synchronous event setting. A notifier exception is logged
   but never changes the producer's committed result.
   Preserve `notify_transport_ready()` as the direct non-commit `requests` wake
   after an IM transport reconnects.
6. Run every lane as bounded work. Request, Run-callback, and Vault-callback
   queries use explicit page limits. A page that reaches its limit or reports
   another eligible item re-arms the same lane. A temporarily skipped item uses
   existing durable eligibility/backoff when present; do not hot-spin on an
   in-memory event.
7. Move synchronous `maybe_reload`, eligibility, and stale-run storage work off
   the controller event loop. Track the underlying executor future as the lane
   owner through timeout/cancellation as §3.6 requires. Never start a replacement
   until that exact future exits.
8. Keep one independent supervisor task that reports the exact overdue lane and
   service generation. It observes lane timestamps only and cannot query the
   stores or write terminal state. Repeated service start/stop creates one new
   generation and leaves no task from the previous generation able to re-arm
   work.
9. Startup wakes every lane after backend identity restoration and Delivery/Turn
   recovery. The periodic reconcile timer later wakes every lane regardless of
   `data_version`; time eligibility and lost notifications therefore recover.
   Preserve the existing two-second cadence initially as a compatibility safety
   net while the producer-coverage test is red/green. Once every official
   producer is proven, set the final default to 30 seconds in the same PR and
   prove post-commit wake latency separately from fallback latency. Retain the
   two-second signature probe only for explicitly injected legacy file stores;
   a definition signature wakes `definitions`, while a request-directory
   signature wakes `requests` and `run_callbacks`. Neither may execute work
   inline.
10. Stop disables new wakes, cancels and awaits async lanes, and awaits each
    exact synchronous future before tearing down stores, executors, or scheduler
    state. If the outer shutdown grace expires, keep those dependencies alive
    and refuse same-process restart; never convert quarantine into permission to
    dispose or overlap. A completion callback from an old generation may log but
    cannot re-arm the service.
    Global controller lease loss stops every lane; routine scheduled-service
    stop unregisters only the Harness tokens it owns.
11. Bound maintenance work, not Agent execution. No lane timeout is a Turn
    timeout, transport retry verdict, or permission to settle a Run.
12. Preserve #1139's persisted Activity batch as the sole owner of output
    payload, order, Run union, stable receipt, accepted-Message evidence, and
    local-settlement-only debt. PR4 changes how that owner is scheduled, not its
    data model or transition writer.

### Conditional transport-attempt delta

The old PR4 also proposed new durable transport-attempt state. It is not part of
the event-first implementation by default. First run the current-master crash
matrix against #1139. If the accepted Message, stable Activity receipt, and
local-settlement-only marker already close the reproduced window, record that
evidence and add no state.

Only a remaining concrete ambiguity may open a separate PR4B contract review.
That review must name the missing fact, prove the existing Activity owner cannot
express it, and keep any attempt metadata inside the existing Activity aggregate
under `SessionActivityRegistry`. It may not add an output ledger, copy payload or
membership, create a second receipt, or infer resend safety from timeout alone.

### Required evidence

- `HFR-155`: an in-process post-commit wake starts the exact eligible lane
  without waiting for periodic reconciliation;
- `HFR-156`: a cross-process commit followed by the Unix-socket hint starts the
  lane, while socket failure leaves the committed work recoverable;
- `HFR-157`: process death or a deliberately dropped hint is recovered by
  startup or slow reconciliation exactly once at the guarded owner boundary;
- `HFR-158`: a hung request-admission store call, Run-callback lookup/enqueue,
  vault-callback storage/dispatch operation, or recovered-output send does not
  delay the other drains or stale-run sweeps;
- `HFR-159`: a contended reload probe or stale-run store operation does not block the event
  loop, suppress independent drain progress, or serialize unrelated tenants;
- `HFR-176`: injected legacy file stores retain separately supervised
  two-second signature hints without running work inline; a request-directory
  change wakes both request and callback lanes, while production SQLite uses
  post-commit wakes plus the slow reconcile timer;
- `HFR-160`: a timed-out synchronous worker remains the sole lane owner until its real
  future exits; no overlapping retry starts, and shutdown cannot dispose its
  store or restart the service before it joins;
- `HFR-161`: large request, Run-callback, and vault-callback backlogs drain in bounded pages
  and reliably re-arm;
- `HFR-162`: a wake during the final empty query is not lost; timeout, cancellation, and
  exception completion each re-arm remaining eligible work with backoff, while
  shutdown cancellation does not re-arm;
- `HFR-163`: only one instance of each drain can run;
- `HFR-164`: routine scheduled-service stop/restart unregisters and joins only
  its Harness lane registrations and their watchdog state; the controller-owned
  supervisor and `session_deliveries` lane remain active;
- `HFR-165`: repeated scheduled-service start/stop cycles leave exactly one
  current Harness registration generation and no stale overdue-drain logs;
- `HFR-166`: a complete multi-Activity batch preserves its persisted order, one stable
  receipt, and the complete Run union while its lane is delayed, restarted, or
  locally retried;
- `HFR-167`: an accepted Message or persisted Activity-batch local-settlement-only marker
  suppresses transport replay, including after restart;
- `HFR-168`: incomplete or conflicting recovered batch membership fails closed before
  transport and remains under the same Activity owner;
- `HFR-169`: work with no Run row still reaches an Activity-owned terminal outcome visible
  from its session;
- `HFR-170`: every skip re-arms with backoff;
- `HFR-171`: the independent supervisor reports which owned lane is overdue;
- `HFR-172`: the table-driven producer contract fails when an official entry
  point lacks the mapped post-commit event; it observes no event before commit,
  after rollback, or after a losing CAS;
- `HFR-173`: unsupported event types remain rejected by the existing socket endpoint;
  browser subscribers cannot suppress a controller wake, and successful bridge
  fanout reaches the browser once.
- `HFR-174`: definition changes wake reconcile without waiting for fallback;
- `HFR-175`: global service-instance lock loss stops every lane registration,
  including `session_deliveries`, before the old process can query or claim, and
  no old-generation completion can re-arm work;
- `HFR-177`: IM transport readiness directly wakes `requests` and a skipped Run
  resumes without waiting for slow reconciliation;
- `HFR-178`: a synchronous lane outliving shutdown grace retains the
  service-instance lock and exact dependencies until its worker or process exits,
  so a replacement process cannot overlap it.

Exit criterion: normal work is event-woken, missed hints are recovered, a hung
lane cannot block another lane or the controller event loop, lifecycle teardown
leaves no old-generation worker, and Activity/Delivery/Turn/Run ownership is
unchanged.

## 7. PR7 — Evidence gate before any new timeout model

Do **not** implement the old PR7 prescription. #1134 added
`complete_on_return`, durable Delivery ownership, immutable Turn terminal
evidence, and restart recovery; #1139 added exact Activity output batch receipts,
Run-union settlement, anti-redelivery evidence, and transport-free local
settlement retry; #1140 closed teardown-interrupted Run settlement. The previous
plan guessed at the remaining timeout model before proving which gaps still
exist. PR7 starts with evidence, not a schema or writer.

### PR7R — Current-master evidence

PR7R changes tests and the plan only. It must publish one executable matrix for:

- Claude, Codex, and OpenCode;
- direct IM and durable Workbench execution lanes;
- scheduler cron fires, one-shot `at` fires, manual CLI/API task runs, and watch
  Runs;
- success, failure, result-less termination, user Stop, terminal persistence
  failure, pending output delivery, and post-delivery local settlement failure.

For every cell, trace the exact durable facts from Run enqueue through request /
Delivery reservation, Turn start, terminal-result latch, Turn terminal evidence,
Activity output batch, accepted Message receipt, Run settlement, and definition
health projection. The matrix must answer these questions with a consuming test,
not prose inference:

1. Does the Run remain nonterminal until its actual terminal Turn/result or
   Activity output batch settles it? If yes, close the old premature-success
   claim with regression evidence instead of adding another writer.
2. Which observable assistant/tool events can be attributed to the exact Turn
   and participating Runs? Prove this separately for every backend and both
   lanes. A backend/lane without an exact signal blocks a generic inactivity
   timeout; session-wide activity is never an acceptable substitute.
3. Can scheduler and manual Runs with different source semantics or effective
   deadlines enter the same Turn? Record the current merge key and every Run in
   the batch. Cancellation is Turn-level, so no per-Run policy may be specified
   until this cardinality is explicit.
4. Which evidence exists before the Turn becomes terminal? A terminal-result
   latch, durable pending-output fact, accepted Message, or Activity
   local-settlement-only marker proves that natural completion has started and
   must outrank a later inactivity decision.
5. Are `health`, `consecutive_failures`, `recent_failures`, `last_run_at`, and
   `last_error` already monotonic projections of bounded terminal Run history?
   Dispatch or Delivery acceptance is never task success.

Exit criterion: the checked-in matrix and tests identify each remaining defect
and its current owner. PR7R adds no status, timeout field, terminal writer,
health cursor, or cancellation path.

### After PR7R — close the claim or review the contract

PR7R does not authorize implementation. If its executable evidence disproves an
old claim, close that claim without adding code. For every reproduced defect,
amend this plan in a separate documentation-only review before opening an
implementation PR. Do not reserve PR7A / PR7B as implementation units until that
contract review passes.

The amendment must define one complete model, not another list of local patches:

1. Name the durable owner and guarded terminal transition at every phase,
   including a bare Run before Delivery reservation, each nonterminal Delivery
   role, a starting or active Turn, terminal-result and pending-output evidence,
   Activity receipt, and post-delivery local settlement. It must define
   request/coalescing cleanup and the durable user notice for every terminal
   cause.
2. State whether exact-Turn progress must survive restart. If it must, define the
   persisted owner, timestamp/update CAS, recovery read, and restart-after-progress
   evidence. If no backend-independent durable signal exists for every backend
   and lane, do not implement a generic inactivity timeout.
3. Define one policy owner for coalesced Runs. Either prove all participants have
   the same snapshotted policy or keep incompatible Runs in separate Turns.
   Specify natural completion, user Stop, and timeout precedence before adding a
   timeout writer.
4. If the model includes a user-configurable task inactivity override, specify
   its V2 config key, concrete default, normalization, persistence, CLI/API/Web
   inputs, English and Chinese documentation, and unchanged watch semantics. If
   those supported surfaces are not part of the implementation, remove the
   override from the model rather than supporting it only through fixtures.
5. Reuse the exact Run, Delivery, Turn, Message, and Activity owners already on
   `master`. Any new state, schema, terminal writer, retry, or replay rule needs
   evidence that those owners cannot express the reproduced behavior.

Terminal-truth and scheduler-liveness defects remain separate implementation
reviews even when PR7R reproduces both. Each implementation PR starts with red
tests for the approved contract and must not infer missing policy from this
evidence plan.

## 8. Order and review boundaries

```text
complete: #1063, #1064, #1072, #1134, #1139, #1140
    |
    v
PR3 runtime ownership + `session_deliveries` supervisor foundation
    |
    v
PR4 event-first supervised work lanes
    |
    v
PR7R current-master evidence matrix
    |
    v
contract amendment for each reproduced defect
    |
    v
separate terminal-truth / scheduler-liveness implementation PRs
```

PR7R is a separate test/documentation review unit. It may close either old claim
without an implementation PR. A reproduced defect first receives the complete
contract amendment above; only then may it become a separate implementation
unit. Keep PR3 and PR4 separate: PR3 defines session ownership/reclamation;
PR4 replaces serial polling as the normal executor. A PR4B transport-attempt
change exists only if the current-master reproducer proves a missing durable
fact and its separate contract review passes.

Implementation ownership is intentionally narrow:

- **PR3** owns the derived SQLite snapshot, the controller-owned supervisor
  foundation, `session_deliveries` registration, reclaimer consumers, and the
  exact activation/reclamation tests. It does not restructure Harness drains.
- **PR4** registers Harness lanes, replaces `_watch_store`, extends the existing
  Unix-socket event vocabulary and bridge use, instruments post-commit
  producers, and adds lane lifecycle and isolation tests. It does not change
  eviction disposition or durable owner schemas.
- **PR7R** remains evidence-only.

PR3 and PR4 may be developed in isolated worktrees from this reviewed contract,
but PR4 is rebased onto the accepted PR3 foundation before it opens for review.
No stacked public PR may ask reviewers to infer an unmerged wake interface.
Each lane owns its files and tests; the orchestrator alone resolves shared
controller/supervisor edits and verifies one consuming test per contract before
push.

Scenario ranges reserved by the original plan remain available on current
`master`:

- PR3: `HFR-130…154`
- PR4: `HFR-155…179`
- PR7: `HFR-180…219`

Check the catalog again immediately before coding. If a range has been occupied,
allocate a fresh contiguous block above the highest merged ID; never reuse a
closed branch's overflow table.

## 9. Validation and non-goals

For every PR:

- add unit and contract tests first against the real owner boundary;
- add/update `tests/scenarios/harness_failure_recovery/catalog.yaml`;
- name the assigned HFR scenario ID in every affected automated test and list
  all affected scenario IDs in the implementation PR description;
- assert both the durable state and the user-visible notice where failure is
  expected;
- use guarded writers and test the losing race, not only the happy winner;
- keep tests hermetic; do not write real `~/.avibe` state;
- route new backend and frontend display copy through the matching English and
  Chinese i18n catalogs;
- run Ruff on changed Python files;
- for any UI change, run `cd ui && npm run build` and verify the packaged
  `ui/dist` contains the intended frontend change;
- use the local Incus regression runner for cross-platform verification;
- never restart the local `vibe` service for routine validation.

Non-goals:

- a new Run status value such as `interrupted`;
- a second Activity output ledger, receipt identity, or settlement owner beside
  #1139's persisted Activity batch;
- backend-level exactly-once execution;
- an absolute Agent turn-duration timeout;
- widening this work into unrelated session/process lifecycle cleanup;
- preserving old implementation details that #1134, #1139, or #1140
  superseded.
