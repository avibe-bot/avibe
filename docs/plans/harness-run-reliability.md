# Harness Run Reliability

Status (2026-08-06): **Every implementation unit through PR4 is merged. PR1,
PR2, PR5, PR6, #1139's Activity-output settlement closure, PR3 (#1155), and
PR4 (#1173) are complete. Only PR7R remains as the next unit, and it is
evidence-only; its first increment is in flight (`HFR-180…211`, see §7). PR4's
conditional transport-attempt delta (PR4B) opens only if a current-master
reproducer proves a missing durable fact.**

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
| Idle-eviction interlock for queued work | **#1155 merged** (PR3); scenarios `HFR-130…154` |
| Bounded and supervised shared drains | **#1173 merged** (PR4); scenarios `HFR-155…179`; attempt-state delta (PR4B) still requires a current-master reproducer |
| Scheduled/watch terminal-time truth and cron liveness | **Open — PR7R in progress**, evidence-only; first increment occupies `HFR-180…211`, `HFR-212…219` still reserved. All 168 matrix cells are `unproven` with named probes: no test on master traces a Run from a trigger's admission through to that Run's terminal settlement. Q2 open — Claude on both lanes and Codex on the durable lane have covered live attribution; Codex/direct-IM lacks an end-to-end backend emit, and OpenCode's restart path drops the Turn and accepted Runs. Q1/Q3/Q4/Q5 open, Q4 Run-scoped and Claude-only, Q5 on the two stored definition fields only; two End-path defects reproduced |

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
6. **Waiting is not activity.** A real inbound message may establish a session
   baseline, and every exact Turn start establishes a fresh one even when the
   work came from a long-idle scheduled/watch queue; after that only observable
   assistant/tool progress refreshes it. Run inactivity is stricter and re-arms
   only from its exact owning Turn. A claim, queue wait, gate wait, or unrelated
   Run in the same session must not keep stuck work alive.
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

Consider one concrete Session. It has an old P3 Delivery `Q1` and no live Turn.
That durable fact is `runnable`: the delivery lane must claim `Q1`, while the
idle backend process remains reclaimable until the Turn actually starts. If an
empty P1 send-now races the recovery wake, its synchronous writer validates and
claims exactly the observed head in one transaction. A subsequent
`queue.updated` hint is for projection and passive recovery, never the authority
for send-now's stale-head/refusal result. At no point is `last_activity`
falsified, and neither the hint nor the reclaimer owns `Q1`.

Now suppose recovered Activity output delivery is hung at the same time. Its
exact Activity batch worker remains single-flight for its partition and overdue,
but another output partition and the independent `session_deliveries` lane can
still progress. This is the user-visible outcome the design must produce.

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

They also share one **runtime activation boundary** per exact backend resource
target and generation. This is an in-process mutation interlock, not a durable
owner: every transition that can change the target from
`runnable|waiting|reclaimable` to `transitioning|active` enters it before the
authoritative commit, and reclamation enters it before the final snapshot. The
boundary is defined by the disposition change rather than by an enumerated
caller list, so a new admission path cannot bypass it merely because it creates
a Run instead of a Turn or Activity.

### 3.2 Derived session disposition

The runtime ownership provider reads Delivery, Turn, active Activity, and
fallback Run facts from one SQLite read transaction and returns one immutable
snapshot. It does not persist a new state or maintain an
in-memory shadow ledger. It is strictly side-effect free: ownership reads must
not release locks, repair projections, settle Runs, or call helpers such as
`owned_agent_run_ids()` that mutate while answering.

The provider explicitly executes SQLite `BEGIN` before its first SELECT and
keeps that read transaction open through every Delivery, Turn, Activity, Run,
and Session query. SQLAlchemy `engine.begin()` alone is not accepted as evidence
because pysqlite may defer the actual `BEGIN` for read-only statements. An
equivalent single compound SQL statement is also valid. Handoff tests use one
real SQLite file, two engines/connections, and a barrier after the first read to
prove that the result cannot combine two commit generations.

```python
class SessionRuntimeDisposition(str, Enum):
    ACTIVE = "active"          # an exact live owner needs the runtime
    TRANSITIONING = "transitioning"  # an unresolved handoff forbids reclamation
    RUNNABLE = "runnable"      # durable work should be activated now
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
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class RuntimeTargetOwnershipSnapshot:
    backend: str
    resource_key: str
    activity_runtime_keys: tuple[str, ...]
    sessions: tuple[SessionRuntimeOwnershipSnapshot, ...]
    sessionless_active_activity_ids: tuple[str, ...]
    sessionless_fallback_run_ids: tuple[str, ...]
    disposition: SessionRuntimeDisposition
```

Reclamation operates on a runtime resource target, not blindly on one Session.
The disposable resource key and durable Activity runtime key are separate
identities. For example, one Codex transport resource is keyed by cwd, while its
Activity/Turn keys are `base_session_id:cwd`; one cwd can therefore map to many
Activity runtime keys. Each backend exposes its exact current resource key,
candidate Session bindings, and the set of Activity runtime keys belonging to
that resource. Every binding carries the stable `agent_sessions.id` as its
ownership identity; `session_anchor` and workdir remain adapter routing facts,
not ownership keys. Archive may rewrite an anchor to vacate an admission alias,
but it cannot transfer or erase unresolved ownership. The provider never
derives this relation by splitting strings or by scanning same-workdir Sessions.

The shared provider resolves those exact Session IDs to durable
`agent_sessions` rows and
computes each Session snapshot in one SQLite transaction. The same query
collects active Activity rows for the backend and matches them through the
adapter-supplied resource-to-Activity-key mapping even when `session_id` is NULL;
those rows are target-level owners and force `active`. An active Activity whose
runtime key cannot be mapped is `unknown`, not an orphan. The target disposition
is the safest member disposition, using `unknown > active > transitioning >
runnable > waiting > reclaimable` for the aggregate. Before the cleanup
decision, every runnable member emits its wake even when another member already
blocks the shared target. Any target-level active Activity or `active`,
`transitioning`, or `unknown` Session blocks target cleanup.
`runnable` wakes activation but, like `waiting`, does not itself own the current
backend process; an all-`runnable|waiting|reclaimable` target may release idle
resources after the final locked recheck. A positively orphaned runtime with no
durable binding may be reclaimed, but a mapping error is `unknown`.

Fallback Runs receive the same route treatment. An IM/CLI Run may have no
`session_id` while its persisted `legacy_session_key`, backend/agent identity,
and execution metadata still name the target. Each backend maps that exact route
to its resource key without string parsing. A queued Session-less Run is
`runnable`; its pre-execution and PID-started forms are `transitioning` and
`active` on the mapped target. If a nonterminal Session-less Run cannot be
mapped, that backend's target snapshot is `unknown` and reclamation fails closed
for the cycle rather than silently omitting the owner.

The exact classification is:

| Durable fact | Derived disposition | Runtime effect |
|---|---|---|
| `starting|active` Turn with its exact owned Delivery, persisted active Activity, or exact in-process backend operation | `active` | forbid ordinary idle reclamation |
| exact linked `waiting` Turn plus its initial `interrupt_waiting` Delivery | `transitioning` | fail closed for this pass and reconcile that exact predecessor/successor owner |
| unresolved Delivery fence or an ownership handoff that cannot yet be classified | `transitioning` | fail closed; reconcile the exact owner |
| ownerless FIFO head or queued execution-bearing fallback Run not yet represented by a Delivery | `runnable` | wake its delivery/request lane; it does not itself pin backend resources |
| claimed fallback Run in normalized `running` / legacy `processing` with no persisted execution-start marker (`pid IS NULL` or absent) | `transitioning` | preserve the pre-execution handoff until exact recovery requeues or starts it |
| fallback Run in normalized `running` / legacy `processing` with the persisted execution-start marker (`pid IS NOT NULL`) | `active` | preserve its runtime until #1140's exact terminal/teardown settlement |
| terminal Delivery/Turn history and terminal Runs only | `reclaimable` | no pin |
| read failure, unknown Delivery state or execution-bearing Run type, or an unpaired/mismatched `waiting`/`interrupt_waiting` half | `unknown` | fail closed for this cycle and log the reason |

`queued` therefore does not mean "keep a backend process alive forever". An
ownerless queue head means "activate it now", while its durable row survives
runtime reclamation until the delivery lane creates the exact Turn owner. This
resolves the apparent contradiction between an idle runtime and durable queued
input without inventing fake activity or a second policy state.

The provider consumes `DELIVERY_STATE_MATRIX` and the current Turn-state
contract as related facts, not as independent liveness enums. The exact
`waiting` Turn / `interrupt_waiting` initial-Delivery pair is the specific
exception to the matrix's general `turn_owned` ordering role: it has not crossed
native start and is `transitioning`, not `active`. A `starting|active` Turn with
its exact owned Delivery is active; a lone or mismatched half is `unknown`. The
pair's wake is not permission to call `activate_waiting_successor()` directly.
Activation must re-read the exact predecessor Turn, prove its
`control_successor_turn_id`/`control_successor_delivery_id` linkage, and consume
the predecessor's immutable terminal winner through the existing terminal-
resume path. Before that terminal proof, targeted recovery may reconcile only
the predecessor control attempt and must leave the successor waiting.
The provider may add a derived policy helper beside the matrix, but it may not retype
Delivery state names in cleanup callers. `watch_runtime` is supervisor
bookkeeping and never an execution owner. An Activity persisted in `active`
phase pins its exact runtime; `awaiting_output` and terminal Activity snapshots
are settlement-only and do not pin backend resources. Unknown Run or Activity
types fail closed until the shared classification deliberately names them.

### 3.3 Activation before reclamation

Every reclaimer pass follows one order:

1. Read the exact ownership snapshot.
2. Derive wakes from the raw member facts before target aggregation. For a
   runnable Delivery, emit a coalescing `session_deliveries` wake. A fallback
   queued Run emits `requests` instead. A transitioning Delivery/Turn owner,
   including every exact waiting-successor pair, wakes `session_deliveries` even
   when its active predecessor makes the aggregate disposition `active`; a pre-
   execution fallback Run wakes `requests`. The row is durable work, not a
   runtime pin.
3. For `active`, `transitioning`, or `unknown`, skip reclamation.
4. For `runnable`, `waiting`, or `reclaimable`, apply the existing idle grace
   rule without mutating Session, hold, queue, Delivery, or Run.
5. Acquire the exact target/generation runtime activation boundary, then read
   the snapshot again immediately before cleanup. A new exact owner or replaced
   generation wins; a still-runnable row may outlive the reclaimed runtime and
   later recreate it safely.

Every path that crosses the disposition boundary participates. This includes
Turn start, a fallback Run claim from queued to normalized running/legacy
processing, the fallback Run PID execution-start transition, and
`SessionActivityRegistry.start()` or a progress upsert that can recreate an
active row. The adapter maps the durable route or Activity runtime key to the
disposable resource target before acquiring the boundary. The admission path
revalidates the observed generation and holds the boundary through its
authoritative commit. No call may split claim and generation validation across
an unlocked window.

If admission wins first, the reclaimer's locked snapshot observes the new
`transitioning|active` owner. If cleanup wins first, it marks or detaches that
exact generation while holding the boundary. A delayed old-generation Activity
event may not create an active row; it follows the existing exact backend/Run
settlement path when it has an owner, otherwise it is ignored as stale
telemetry. A queued fallback Run remains queued or is recovered through its
existing exact Run CAS, then may activate against the current/new generation;
it is never marked execution-started against the retired generation. This adds
no Run or Activity state, durable fence, or terminal writer.

The reclaimer does not wait for the request to finish. The wake is a hint; the
`session_deliveries` lane later claims the exact FIFO head under the existing guarded
Delivery/Turn transaction. If the hint is lost, startup and periodic
reconciliation rediscover the same row. A losing claim re-reads; invalid input
is retired by the existing exact-Delivery validation; temporary unavailability
leaves the row durable and uses bounded lane backoff. None of these outcomes
refreshes `session_last_activity` or pins the old runtime.

Activation and transition recovery never depend on a runtime target already
existing or reaching the cleanup loop. The `session_deliveries` lane has two
bounded indexed eligibility queries: Session ids with a claimable queued head
and no live Turn, and unresolved Delivery/Turn fences, starting owners, or
waiting successors that require exact reconciliation. It invokes a narrow
`SessionTurnManager` claim/recovery entry for the exact Session plus observed
predecessor Turn/control attempt, successor Turn, Delivery, and native identity.
The manager must re-prove the predecessor's exact successor linkage and terminal
snapshot before its existing terminal-resume writer may activate the successor;
the low-level successor CAS is never a standalone recovery entry. A wake that
arrives while the predecessor is still nonterminal no-ops after any exact control
reconciliation. The `requests` lane likewise includes claimed pre-execution
fallback Runs. Losing or stale identities no-op after their guarded re-read.
Queue events, startup, and periodic reconciliation are the primary triggers; the
reclaimer's wake closes only the admission-versus-cleanup race.
The full global `recover_durable_delivery_state()` remains startup-only after
backend identity restoration.

The stuck-active backstop is not an ordinary idle timeout. It applies only when
the exact owner has stopped producing observable progress past the existing
threshold, and it routes through #1140's guarded teardown settlement. A long
tool call without progress may still hit this explicit infrastructure-failure
policy; this work does not add another recovery layer or silently replay it.

### 3.4 Work lanes and wake protocol

Add one controller-owned `RuntimeWorkSupervisor` and replace the serial executor
roles of both `ScheduledTaskService._watch_store` and
`ManagedWatchService._watch_store` with independently registered, partitioned
work lanes:

```python
class RuntimeWorkLane(str, Enum):
    SESSION_DELIVERIES = "session_deliveries"
    TASK_DEFINITIONS = "task_definitions"
    WATCH_DEFINITIONS = "watch_definitions"
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
`session_deliveries` handler; `ScheduledTaskService` and `ManagedWatchService`
register only the Harness handlers they own while their service generation is
valid. Registration returns a generation token; duplicate live registration is
rejected, and unregistering a token suppresses re-arm from that generation
before awaiting its exact worker. An event for an unregistered lane is
discarded because startup reconciliation is mandatory when a new handler
registers.

PR3 also registers the minimal `requests` reconciliation handler needed for
claimed pre-execution fallback Runs: it can only requeue or preserve the exact
observed Run under the existing PID/owner fence. It does not admit general
queued Runs. PR4 replaces that registration, through the same generation-token
handoff, with the complete bounded `requests` admission handler. PR3 therefore
introduces no interval in which a newly classified `transitioning` Run can pin a
runtime until process restart, and PR4 does not create a second consumer.

Durable Harness Run admission has one consumer. A scheduler fire, manual task
run, callback, or watch completion commits the queued Run and wakes `requests`;
`ScheduledTaskService._run_task` no longer claims, spawns, or awaits the same
Run on a second path. Before enqueue, a scheduler fire computes the existing
one-successor rule in the enqueue transaction from same-definition
scheduler-sourced Runs and their Delivery/Turn facts. An accepted active
execution does not suppress its first follower. Any later Run or Delivery that
has not yet begun its own accepted Turn is the one queued successor and
suppresses further fires. When that successor starts or terminalizes, the next
fire may create one new follower. The existing Run/Delivery/Turn rows are the
evidence; no scheduler fence row or second owner is added.
Callers that require a synchronous result observe the persisted Run outcome
rather than taking execution ownership from the lane.

Likewise, every completed Activity output batch, whether produced during a live
Turn or found after restart, is persisted and wakes `activity_outputs`. A wake
does not make a newly unbound completion immediately claimable. Preserve
Claude's existing `ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS` coalescing window: the
first unbound completion establishes the eligibility deadline from its
persisted `completed_at`, and later matching completions inside that window do
not extend it. A restart recomputes the remaining window from durable
timestamps. An already bound batch or local-settlement-only retry is complete
evidence and is eligible immediately. No new durable deadline or batch owner is
added.

Once due, both the context-sensitive live flush and restart recovery enter the
same supervisor partition keyed by exact `(backend, runtime_key)` before
claiming. The live path may use its existing in-memory Turn/context while it
owns that partition. Its existing expected-Turn selection may scan past
unrelated unbound output to choose the causally matching batch; the supervisor
does not replace that #1139 rule with a global row-order policy. The partition
lease spans claim, emit, and settle/requeue, so no second live or recovery
worker can claim or send another batch for that runtime while the selected
batch owns the boundary. After release, the registry again chooses by its
existing causal/FIFO contract. Neither the wake nor the grace timer carries
payload, membership, or receipt authority.

Each lane owns one coordinator event/task plus a bounded map of item workers by
partition key. The coordinator alone queries eligibility and never awaits item
execution. It starts work only when that lane has capacity and no worker already
owns the same partition. The existing guarded durable claim remains the item
authority. A wake while the coordinator or an item is running sets the event for
another bounded scan; it never starts a second worker for the same partition. A
bounded page that leaves eligible work re-arms itself. A transient failure
records bounded backoff for that item/partition only. One lane cannot await or
cancel another lane.

Eligibility pagination cannot repeatedly stop on rows whose partition is
already owned. A lane query either returns one actionable head per partition or
uses stable keyset advancement while excluding the coordinator's occupied and
backoff keys. One scan continues past full blocked pages until it fills current
capacity or reaches the stable end cursor; only then may a later wake restart
from the beginning. Durable FIFO within each partition is unchanged.

Partition keys are not invented tenant labels; they are the existing FIFO or
serialization owner key. They are Session id for Delivery activation; request
execution-lock key (or standalone Run id) for Run admission; callback target
Session for Run/Vault callbacks; and exact `(backend, runtime_key)` output queue
for recovered Activities. Ordinary suppressible failure notices with a
definition use `("failure_streak", definition_id)` as their conservative
partition because the existing policy derives one canonical consecutive-
failure streak from that definition's Run history. Suppression-bypass notices
(interruptions and binding changes) and definitionless failures retain their
existing durable `failure_id`/Run identity. A destination is routing, never a
notice serialization key. Definition-level serialization may conservatively
span two already-closed streaks; it preserves correctness without adding a
streak field or moving streak derivation out of `failure_streak_decision()`.
One selected Activity batch is indivisible and owns that runtime partition
through settlement or requeue. Selection order remains the existing #1139
registry contract, including expected-Turn causal matching; the supervisor
forbids overlap but does not replace that policy with a second queue.
`task_definitions`, `watch_definitions`, and `stale_runs` each retain one bounded
coordinator because their owner operations mutate shared local store/scheduler
state. In particular, every blocking `ManagedWatchStore` reload, cycle marker,
and runtime-state storage write stays behind the one `watch_definitions`
single-flight store boundary: its mutable full-store mirror is not safe for
concurrent watch-id writers. The store phase returns an immutable snapshot or
guarded write result. `ManagedWatchService.reconcile_watches()` and all
`_active_tasks` creation, cancellation, done-callback, and runtime projection
assembly remain on the controller event loop. Only the assembled projection's
blocking persistence enters the tracked store worker. Long-running waiter
processes/tasks remain outside that maintenance worker. This preserves one
store writer and one loop-owned lifecycle without creating a second mirror
owner.

That prepare/apply split is the general execution-placement rule. Blocking,
side-effect-free reads and serialized store commits run in tracked synchronous
workers. Asyncio task/timer mutation and in-memory lifecycle application run on
their owning event loop after the worker returns. Existing domain claim and
settlement APIs remain the semantic owners under the lane partition. Moving a
blocking call off-loop never authorizes moving its caller's event-loop state
machine into the executor.
Every lane has a small internal maximum greater than one
where item workers are supported; the limit is injectable in tests and is not a
public product setting. One hung tenant therefore consumes one partition/slot
rather than the whole lane, while the global bound prevents unbounded tasks. If
all slots are occupied, remaining durable work waits and is re-read when one
exits.

`InboxEventBus.subscribe_callback` runs on the publishing thread, which may not
be the controller event-loop thread. The supervisor captures its owning loop at
startup; callbacks only call `loop.call_soon_threadsafe(...)`. The scheduled
loop callback is the sole mutator of coordinator events, tasks, partitions, and
generation state. A closed or superseded loop drops the hint and relies on
startup/periodic reconciliation.

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
controller received the hint, and a successful socket POST does not prove the
controller-to-UI event stream was connected at that instant. After commit, a
non-controller UI publisher therefore fans out locally and also posts the hint
to the controller with one ephemeral event id/origin. The UI bridge keeps a
small bounded TTL set of ids it already fanned out locally and drops only the
matching echoed event; other UI processes still receive it once. This id is
transport deduplication metadata, not durable work identity or acceptance
evidence. Socket absence, timeout, or a disconnected return stream leaves the
command successful and the local UI responsive because the durable row remains
authoritative.

The producer map is load-bearing:

| Observed edge | Wake lane(s) |
|---|---|
| Delivery becomes queued with open hold or terminal Turn exposes an open successor/head | `session_deliveries` |
| committed task or watch mutation after add/update/pause/resume/remove | `task_definitions`, `watch_definitions` via the shared event; each lane re-reads only its own store |
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
| `definitions.updated` | `task_definitions`, `watch_definitions` |
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
directory signature probes remain as temporary hint sources. A changed task
definition signature wakes `task_definitions`; a changed managed-watch
definition signature wakes `watch_definitions`; a changed request-directory
signature wakes both `requests` and `run_callbacks`, because that directory also
stores terminal Runs awaiting callback delivery. Each signature check is
separately tracked off the event loop; it performs no reload, claim, callback,
or settlement inline and cannot block another probe or lane. The compatibility
probes are not enabled for the production SQLite stores. This preserves legacy
test/tool responsiveness without making two-second polling the production
executor. Removing those file stores removes the probes with them.

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
operation or release that partition's single-flight ownership:

- the future remains the partition owner until it actually exits;
- the partition is marked overdue and does not launch a replacement;
- an independent supervisor logs the exact overdue lane and partition;
- completion re-arms the lane if durable work remains;
- shutdown stops new wakes, cancels only work that has not crossed an external-
  effect boundary, and joins each exact owner before disposing shared stores.

Every handler declares its cancellation boundary. Eligibility reads and work
before a native/transport write may be canceled and recovered from durable
state. Once an Activity output, callback, notice, or other non-idempotent send
may have reached its destination, that worker is shielded from graceful-shutdown
cancellation and retains its lane partition, receipt owner, dependencies, and
service-instance lock until it persists the exact outcome or exits with a
definitive pre-write result. A shutdown grace expiry taints the process as below;
it is not permission to abandon the send and let startup replay it. Forced
process death remains outside graceful exactly-once guarantees and is the
current-master reproducer gate for any separate PR4B attempt contract.

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

The process signal handler is never the cleanup executor. It only records the
shutdown intent and schedules `controller.request_shutdown(...)` onto the
captured loop with a loop-safe call; it does not call `cleanup_sync()`, wait on a
future, raise `SystemExit`, or release the service-instance lock while that loop
is running on the same thread. The loop-owned async shutdown path closes new
dispatch, stops and joins the supervisor and registered services, and records
whether the lock is safe to release before stopping the loop. The
`Controller.run()` `finally` path performs only any remaining synchronous
cleanup after the loop has stopped. A signal received before loop activation is
latched and consumed as soon as the loop starts, or handled by the same stopped-
loop cleanup path if startup aborts.

On a tainted process shutdown, cleanup skips disposal of the exact
supervisor/store/executor dependency set, refuses any in-process restart, and
does **not** explicitly release the service-instance lock. The operating system
releases that lock only when the process, including the old worker, actually
exits; a replacement process therefore cannot overlap it. `main.py` releases
the lock only after reading the loop-owned safe-to-release result. This is
bounded failure containment, not a retry or permission to overlap work.

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

**Merged as #1155 (2026-08-04).** Retained as the accepted contract: PR7R and
any later implementation unit must consume the runtime ownership snapshot,
activation interlock, and work-supervisor interfaces defined here rather than
reintroducing side maps or adapter-local reclamation rules.

### Goal

Make runtime reclamation and durable work activation consume one coherent
session disposition. Active or transitioning work must not lose its runtime,
and ownerless runnable work must be woken without keeping a backend process
alive forever.

### Required behavior

1. Implement the §3.2 ownership provider as one SQLite read transaction over
   `agent_sessions`, `message_deliveries`, `session_turns`, and fallback
   `agent_runs`, plus Activity rows in the existing `runtime_records` aggregate.
   Explicitly execute SQLite `BEGIN` before the first SELECT and hold that
   transaction through the final ownership query; entering SQLAlchemy's
   `engine.begin()` context without an emitted `BEGIN` is insufficient.
   Reuse the existing engine and storage tables; add no table,
   cache, or background resolver. Resolve a backend runtime target to every
   durable Session it can serve, then classify those Sessions in the same
   transaction. Also classify active Activity rows with a NULL Session as
   target-level owners, using the backend-provided resource-key to Activity-key
   mapping rather than assuming those keys are equal. A returned snapshot is
   valid only for that transaction and exact runtime resource target. Map
   Session-less fallback Runs from their persisted legacy Session key plus
   backend/agent execution identity to the exact resource target; an unresolved
   nonterminal route fails closed for that backend cycle.
2. Derive `active`, `transitioning`, `runnable`, `reclaimable`, or
   `unknown` exactly as §3.2 specifies. In particular:
   - an open, ownerless FIFO head is `runnable`, not a permanent runtime pin;
   - a fence or Turn owner forbids reclamation;
   - terminal history and `watch_runtime` do not pin;
   - an execution-bearing Run is considered only when its exact Delivery/Turn
     representation is absent from the same snapshot: queued is `runnable`,
     normalized running/legacy processing without the persisted PID boundary is
     `transitioning`, and the same state with a persisted PID is `active` until
     exact settlement.
3. Inventory Claude, Codex, and OpenCode runtime-reclamation paths. Every path
   that can invalidate the exact runtime needed by a live owner consumes the
   shared provider. A backend transport cache that is proven restart-safe may
   reclaim under `waiting`, but requires a consuming test; do not clone provider
   rules into adapters. A shared Codex cwd transport may be reclaimed only when
   every associated Session is `runnable|waiting|reclaimable`, after all
   runnable members have been woken and the locked recheck still finds no live
   owner. Claude composite keys must be mapped by the stable Session row ID
   plus the adapter's exact runtime keys, not by a mutable admission anchor or
   string-prefix guesses.
   Inventory every path that can change a target from
   `runnable|waiting|reclaimable` to `transitioning|active`, including backend
   receivers and standalone fallback Run claim/execution start. It must enter
   the exact resource-target/generation activation boundary before the owning
   commit and revalidate the observed generation. Direct registry or Run-store
   transitions that can race cleanup are forbidden.
4. Add the controller-owned `RuntimeWorkSupervisor` contract and register
   `SessionTurnManager` as the sole `session_deliveries` handler. Consult the
   provider in both passes of `evict_idle_sessions`. On a runnable Delivery,
   notify that lane but do not turn durable unclaimed work into an unbounded
   runtime pin; a fallback queued Run not yet represented by Delivery notifies
   `requests`. Recompute under the exact target/generation runtime activation
   boundary immediately before cleanup. A newly committed fallback Run claim or
   execution start, Turn, active Activity, or replaced generation wins; a
   still-runnable row survives runtime reclamation. The reclaimer keeps the
   boundary through generation detach/cleanup initiation so no delayed
   old-generation admission can become a new durable pin after the final
   snapshot.
   The lane itself uses separate bounded indexed queries for claimable queued
   Sessions without live Turns and for unresolved Delivery/Turn fences,
   starting owners, and waiting successors. It invokes a manager entry guarded
   by the observed Session, Delivery, Turn, attempt, and native identity; stale
   observations no-op. The exact linked `waiting` Turn / `interrupt_waiting`
   initial-Delivery pair is `transitioning` even though the Delivery matrix calls
   it `turn_owned`; the pair emits its recovery wake even while its active
   predecessor keeps the aggregate runtime active. A mismatched half fails
   closed. Recovery also revalidates the predecessor Turn's exact
   `control_successor_*` linkage and immutable terminal winner; it may activate
   the successor only through the existing terminal-resume writer. A wake before
   predecessor terminalization cannot call the low-level successor CAS and
   leaves the pair waiting. The `requests` lane applies the equivalent exact
   recovery to claimed pre-execution fallback Runs. Neither lane hides a full global
   `recover_durable_delivery_state()` call inside cleanup; that global pass
   remains startup-only after backend restoration.
   PR3 registers this fallback-only `requests` recovery handler together with
   `session_deliveries`; it neither scans nor admits ordinary queued Runs. PR4
   atomically replaces the registration with the full admission handler.
5. Use two failure modes:
   - a lookup that positively proves one binding is dangling fails open for only
     that binding, so a deleted target cannot pin an unrelated session forever;
   - any exception while resolving a binding, or any provider-wide failure,
     fails closed for the eviction cycle, because missing safety data is not
     evidence that eviction is safe.
6. Bound exact active/transitioning ownership with the existing real-progress
   inactivity clock and stuck-active threshold. Merely observing a newly
   admitted pin does not restart that clock, but the authoritative exact Turn
   start establishes a fresh baseline for every source, including scheduled,
   watch, and agent-initiated work admitted after a long queue wait. Beyond the
   bound, use the #1140 teardown path to settle exact running ownership and
   preserve queued/unstarted work.
7. Inventory every `session_last_activity` writer. Do not touch it on claim,
   enqueue, gate wait, output wait, polling/protocol frames, wake, provider
   lookup, or unrelated Session progress. A real inbound message may establish
   a baseline; the exact Turn-start boundary must establish a fresh baseline
   before the new execution can be judged stuck. Subsequently attributable
   assistant/tool/active-Activity progress is the only refresh signal. Shared
   Codex transport activity cannot refresh another Session's progress clock.
8. Keep activation asynchronous. The reclaimer emits only the coalescing wake;
   `SessionTurnManager` remains the claimant and Turn creator. A wake must never
   select a different queue head, bypass hold, or mutate Delivery/Run state.
   Existing synchronous commands such as send-now keep their exact-head
   validation, hold release, claim, refusal, and response semantics; their
   events only trigger passive recovery after the guarded result is committed.

### Required evidence

- `HFR-130`: one SQLite snapshot classifies every Delivery/Turn/Activity/Run
  ownership combination without a torn handoff; two engines/connections and a
  deterministic barrier prove that an explicit read transaction prevents mixed
  commit generations;
- `HFR-131`: an open ownerless FIFO head wakes `session_deliveries`, remains
  durable if its idle runtime is reclaimed, and is later claimed through the
  existing exact-head transaction;
- `HFR-132`: a held FIFO backlog survives while its disposable backend runtime
  is reclaimed, then starts after the existing explicit hold release;
- `HFR-133`: unresolved fences and Deliveries owned by exact `starting|active`
  Turns forbid cleanup;
- `HFR-134`: an exact linked `waiting` Turn / `interrupt_waiting` initial-
  Delivery pair is `transitioning` and emits one exact recovery wake even while
  its active predecessor makes the aggregate `active`; a mismatched half is
  `unknown`, and terminal Turn/Delivery history alone is reclaimable. Targeted
  recovery re-proves the predecessor's exact `control_successor_*` linkage and
  immutable terminal winner before the existing terminal-resume writer activates
  that successor once. A wake before the predecessor terminal CAS is a no-op,
  while a stale CAS cannot start it twice or claim another head;
- `HFR-135`: active Activity rows pin their exact runtime, while `awaiting_output` and
  terminal Activity snapshots do not;
- `HFR-147`: an active Activity with no Session id maps from its durable Activity
  runtime key to the exact backend resource target and cannot be mistaken for an
  orphan; a missing map fails closed;
- `HFR-136`: a `watch_runtime` heartbeat sharing the same definition/session does not pin,
  while an execution-bearing watch Run does;
- `HFR-137`: every transition across the runtime activation boundary wins or
  loses atomically against reclamation. Deterministic barriers cover both
  interlock orderings for Turn start, Session-bound and Session-less Activity
  admission, `claim_pending_run()`, and `mark_run_execution_started()`.
  Admission-before-cleanup is visible in the locked snapshot. Cleanup-before-
  admission prevents an old-generation Activity or execution-started Run from
  becoming active, while preserving the queued/pre-execution Run for its
  existing exact recovery path;
- `HFR-138`: bare-Run to reserved-Delivery and Delivery to Turn ownership handoffs cannot
  disappear across a torn provider read;
- `HFR-148`: fallback queued, pre-execution claimed, and execution-started Runs
  classify as `runnable`, `transitioning`, and `active` respectively from the
  persisted PID boundary;
- `HFR-149`: periodic reconciliation finds a persisted Delivery/Turn fence,
  starting owner, waiting successor, or claimed pre-execution fallback Run after
  its process disappears and invokes only its exact guarded recovery entry. The
  active-predecessor/waiting-successor case still emits one coalesced recovery
  wake before aggregate disposition is applied. Deterministic barriers prove a
  wake after successor creation but before predecessor terminal proof cannot
  activate it. A stale observation neither pins the runtime forever nor recovers
  another item; the fallback Run recovery is live in PR3 before PR4 broadens the
  lane;
- `HFR-150`: Session-less IM/CLI fallback Runs map from persisted route and
  backend identity to the exact runtime target; archived Session-bound Runs
  remain visible through the resource's durable Session membership even after
  an admission anchor is reused. Queued, pre-execution claimed, and PID-started
  rows derive `runnable`, `transitioning`, and `active`, while an unmappable
  nonterminal row fails closed instead of disappearing;
- `HFR-139`: unrelated sessions are not pinned;
- `HFR-140`: one positively missing binding fails open, while a per-binding
  lookup exception or provider failure aborts the cycle;
- `HFR-141`: repeated queued followers do not refresh progress or make a held Session
  immortal; teardown settles only exact running ownership;
- `HFR-142`: a claimed or gate-waiting request does not refresh activity, while
  the exact Turn start after an arbitrarily long queue or gate wait establishes
  one fresh baseline before reclamation may judge the execution stuck;
- `HFR-143`: scheduled, watch, and agent-initiated Turns admitted into a
  long-idle Session are not immediately evicted after exact Turn start, and a
  long Turn with attributable observable progress remains live while unrelated
  shared-transport activity does not refresh it;
- `HFR-144`: the stuck threshold settles only the exact active owner through
  #1140 and never replays it;
- `HFR-145`: every enabled backend eviction path either honors the provider or proves queued
  work resumes without loss or replay;
- `HFR-146`: one active Session prevents reclamation of a shared Codex cwd
  transport while an unrelated held Session on that transport does not create
  fake activity; Claude composite-key mapping uses the stable Session row ID
  plus its exact adapter runtime keys.

Exit criterion: open work is activated, held work remains durable without
pinning resources, no productive or transitioning owner is reclaimed, and the
provider cannot make a stuck session immortal.

## 6. PR4 — Event-first supervised work lanes

**Merged as #1173 (2026-08-05).** Retained as the accepted contract. The
conditional transport-attempt delta below remains unimplemented and is the only
part of this section still open.

### Goal

One hung or unbounded request, Run-callback, vault-callback, or post-turn-output
pass must not block another Harness tenant. Normal work begins from a
post-commit wake; startup and slow reconciliation repair lost hints. This is the
direct fix for the observed 65-minute serial-loop stall. #1139 remains the sole
Activity-output owner.

### Required behavior

1. Extend the controller-owned §3.4 `RuntimeWorkSupervisor`; do not add a second
   scheduler inside `ScheduledTaskService`. Replace both
   `ScheduledTaskService._watch_store` and `ManagedWatchService._watch_store`
   with lane registration plus the shared slow reconciliation timer; neither
   loop may await a drain or perform storage I/O.
2. Give `task_definitions`, `watch_definitions`, `requests`, `run_callbacks`, `vault_callbacks`,
   `activity_outputs`, `failure_notices`, and `stale_runs` independent
   coordinator state and the §3.4 bounded partition workers where item work can
   block. Reuse current drain functions behind the lane boundary where their
   ownership is already correct; split only unbounded passes into bounded pages
   and item calls. Do not create seven new service classes merely to rename
   seven methods.
3. Implement one shared coalescing notifier. An event means "query durable
   eligibility again" and carries no item id. A running lane consumes the event
   before its next page so a concurrent commit cannot be lost between the final
   query and task exit.
4. Subscribe the supervisor to the existing inbox event bus and reuse the
   `/internal/events` socket bridge. Add only `definitions.updated` to the
   allowlist and shared event vocabulary. Map events to coalescing lane wakes;
   ignore payload identity and status when deciding durable work. External UI
   publishers perform local fanout and bridge the hint with an ephemeral event
   id; the return bridge deduplicates only the origin process's echoed id. Local
   subscribers therefore neither suppress the controller wake nor miss an
   update while the return stream reconnects.
5. Instrument every producer in the §3.4 map after its authoritative commit.
   Cross-process notification is best effort and short-lived. In-process
   notification marshals event setting onto the captured controller loop with
   `call_soon_threadsafe`. A notifier exception is logged but never changes the
   producer's committed result.
   Preserve `notify_transport_ready()` as the direct non-commit `requests` wake
   after an IM transport reconnects. The transport registry remains the sole
   readiness owner and adds one synchronous final-decision guard per configured
   platform. The current global `_ready_lock` continues to protect only brief
   reads/mutations of the `_ready_platforms` set; it is never held across a
   database call. The keyed guards serialize only one platform's readiness
   transitions and stale settlement, so a slow Slack settlement cannot delay
   Discord readiness. They are bounded in-memory capability locks, not work
   owners.

   The same readiness owner records an in-memory generation and injected UTC
   `unready_since` for the current outage. Registering or hot-adding a configured
   platform that is not ready starts a fresh generation before its child thread
   or the stale lane can observe it; a ready transition closes it. Every later
   ready-to-unready edge starts another generation under the exact platform
   guard, whether that edge comes from explicit disconnect, the client runner's
   `finally`, or hot removal. Repeated observations while the platform is
   already unready do not refresh the generation or timestamp. A new process
   conservatively starts a new generation for each configured but not-ready
   platform, granting another bounded reconnect window rather than failing a
   Run from unverifiable pre-restart history. This metadata describes the
   current capability edge only. It is not a durable queue, Run fact, or
   settlement owner, and no transition bulk rewrites queued Runs.

   Candidate discovery happens outside the platform guard. For a queued row
   carrying `transport_unavailable` evidence, the worker first reserves the
   SQLite write transaction with `BEGIN IMMEDIATE` or the repository's
   equivalent, then enters the exact platform guard, briefly reads readiness
   plus the current outage generation under `_ready_lock`, releases that brief
   set lock, and re-reads the Run evidence. If the platform is ready, the worker
   uses the same exact queued/reason/timestamp CAS to clear the ended outage's
   stale evidence and returns without terminalizing. If it is unready, the
   effective outage start is the later of the persisted `last_skip_at` and the
   current generation's `unready_since`; settlement is ineligible until that
   effective start exceeds the reconnect TTL. The worker then commits only an
   exact `status=queued` CAS with the observed reason/timestamp while still
   holding the platform guard. Both ready and ready-to-unready transitions take
   that same platform guard, mutate the readiness set and generation briefly
   under `_ready_lock`, release both, and only then publish any wake; neither
   transition performs database work while holding either lock. A reconnect
   that wins first makes terminal settlement no-op and permits exact evidence
   cleanup; a later disconnect rebases the effective age even when capacity
   kept the old row stamp from being cleared; a settlement that wins first
   commits before readiness publication; a request claim that already changed
   the row to `running` makes the stale CAS lose. This is a final-decision
   interlock, not a durable readiness state or a second Run owner.
   A scheduler fire or manual task run only enqueues and notifies; remove
   `_run_task`'s direct claim/spawn/await path so `requests` is the single durable
   Run admission consumer. Preserve the existing one queued follower behind an
   active scheduled Run with a transactional same-definition query across Run,
   Delivery, and Turn facts: active accepted work allows one successor, while an
   unstarted successor suppresses later fires. Route both live and
   restart-discovered completed Activity
   batch claims through the same `activity_outputs` partition lease; the live
   path keeps #1139's context-sensitive expected-Turn selection inside that
   lease, including scanning past unrelated unbound output. The lease remains
   owned through emit and settle/requeue. No backend-local flush may race the
   recovery lane as an independent claimant. Preserve the current live
   coalescing grace before claiming an unbound batch. Compute its due time from
   the first matching persisted `completed_at`, do not extend it for later
   members, and preserve the remaining delay across restart. Bound batches and
   local-settlement-only retries bypass the grace because their immutable
   membership already exists.
6. Run every lane as bounded work. Request, Run-callback, and Vault-callback
   queries use explicit page limits. A page that reaches its limit or reports
   another eligible item re-arms the same lane. A temporarily skipped item uses
   existing durable eligibility/backoff when present; do not hot-spin on an
   in-memory event.
7. Move synchronous `maybe_reload`, eligibility, stale-run, cycle-marker, and
   runtime-state persistence work off the controller event loop.
   `watch_definitions` is the single store worker for managed-watch stale-worker
   recovery retry, reload, guarded per-watch writes, and persistence of an
   already assembled runtime projection. It returns immutable snapshots/results
   to the controller loop. Keep `reconcile_watches()`, `_active_tasks`, task
   creation/cancellation, done callbacks, timer state, and projection assembly
   on that loop; never call `asyncio.create_task()` or mutate those maps from an
   executor thread. The long-running waiter process/task remains under the
   existing managed-watch lifecycle and is never subjected to a maintenance-
   lane timeout. Track each underlying store future as its lane owner through
   timeout/cancellation as §3.6 requires. Never start a replacement store call
   until that exact future exits.
8. Keep one independent supervisor task that reports the exact overdue lane,
   partition, and service generation. It observes worker timestamps only and
   cannot query the stores or write terminal state. Repeated service start/stop
   creates one new generation and leaves no task from the previous generation
   able to re-arm work.
9. Startup wakes every lane after backend identity restoration and Delivery/Turn
   recovery. The periodic reconcile timer later wakes every lane regardless of
   `data_version`; time eligibility and lost notifications therefore recover.
   Preserve the existing two-second cadence initially as a compatibility safety
   net while the producer-coverage test is red/green. Once every official
   producer is proven, set the final default to 30 seconds in the same PR and
   prove post-commit wake latency separately from fallback latency. Retain the
   two-second signature probes only for explicitly injected legacy file stores;
   task and managed-watch definition signatures wake `task_definitions` and
   `watch_definitions` respectively, while a request-directory signature wakes
   `requests` and `run_callbacks`. None may execute work inline.
10. A process signal only requests loop-owned asynchronous shutdown. That path
    disables new dispatch and wakes, cancels only workers still before their
    declared external-effect boundary, and awaits every worker that may have
    written until it persists accepted/refused/pre-write evidence. It also awaits
    each exact synchronous future before tearing down stores, executors, or
    scheduler state. If the outer shutdown grace expires, keep those workers and
    dependencies alive and refuse same-process restart; never convert quarantine
    into permission to dispose or overlap. A completion callback from an old
    generation may log but cannot re-arm the service.
    Global controller lease loss stops every lane; routine scheduled-service or
    managed-watch-service stop unregisters only the Harness tokens it owns. The
    signal handler never blocks the running loop or releases the process lock;
    `main.py` does so only after loop shutdown reports it safe.
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

- `HFR-155`: an in-process post-commit wake from either the controller loop or a
  worker/receiver thread is marshaled onto that loop and starts the exact
  eligible lane without waiting for periodic reconciliation;
- `HFR-156`: a cross-process commit followed by the Unix-socket hint starts the
  lane, while socket failure leaves the committed work recoverable;
- `HFR-157`: process death or a deliberately dropped hint is recovered by
  startup or slow reconciliation exactly once at the guarded owner boundary;
- `HFR-158`: a hung request admission, callback delivery, vault callback, or
  recovered-output send for one partition does not delay another partition in
  the same lane, the other lanes, or stale-run sweeps; more than one full page of
  followers for the occupied partition cannot hide a later actionable
  partition. A blocked ordinary failure notice for definition A does not delay
  definition B, while two ordinary failures of A targeting different Sessions
  never overlap and the later decision observes the first canonical result;
- `HFR-159`: a contended task/watch reload probe or stale-run store operation
  does not block the event loop, suppress independent drain progress, or
  serialize unrelated partitions;
- `HFR-176`: injected legacy file stores retain separately supervised
  two-second task, watch, and request signature hints without running work
  inline; a request-directory change wakes both request and callback lanes,
  while production SQLite uses post-commit wakes plus the slow reconcile timer;
- `HFR-160`: a timed-out synchronous worker remains the sole partition owner
  until its real future exits; no overlapping retry starts for that partition,
  and shutdown cannot dispose its store or restart the service before it joins;
- `HFR-161`: large request, Run-callback, and vault-callback backlogs drain in
  bounded pages and reliably re-arm; scheduler, manual, callback, and watch
  producers only enqueue and notify, one `requests` claim starts each Run, and
  synchronous callers observe persisted outcomes without becoming a second
  execution owner; one active accepted scheduled Run permits exactly one queued
  same-definition successor, and that successor suppresses later fires until it
  starts or terminalizes;
- `HFR-162`: a wake during the final empty query is not lost; timeout,
  pre-effect cancellation, and exception completion each re-arm remaining
  eligible work with backoff, while shutdown cancellation does not re-arm;
- `HFR-163`: each lane enforces its configured global worker bound and only one
  item worker per partition while allowing a different partition to progress.
  Ordinary notices for one definition share its conservative failure-streak
  partition; interruptions, binding changes, and definitionless failures retain
  their existing per-failure identities and do not collapse into that streak;
- `HFR-164`: routine scheduled-task or managed-watch service stop/restart
  unregisters and joins only its owned Harness lane registrations and watchdog
  state; the controller-owned supervisor and `session_deliveries` lane remain
  active;
- `HFR-165`: repeated scheduled-service start/stop cycles leave exactly one
  current Harness registration generation and no stale overdue-drain logs;
- `HFR-166`: complete multi-Activity batches preserve persisted order, one stable
  receipt per batch, and each complete Run union while delayed, restarted, or
  locally retried. Two same-Turn unbound completions inside the injected live
  grace produce one batch, receipt, and user-visible message; the first wake
  cannot claim early, a second completion does not extend the deadline, and a
  restart midway waits only the durable remaining grace. Bound and local-only
  retry batches are immediately eligible. One selected batch for exact
  `(backend, runtime_key)` owns the partition through emit and settle/requeue so
  another batch cannot overlap it; after release, the registry preserves
  #1139's expected-Turn causal selection, including scanning past unrelated
  unbound output, and loss of live context falls back to persisted
  Activity/Turn/Run settlement without a competing claimant;
- `HFR-167`: an accepted Message or persisted Activity-batch
  local-settlement-only marker suppresses transport replay, including after
  restart; graceful shutdown after remote acceptance but before local receipt
  persistence cannot cancel the owning send and must await its exact settlement;
- `HFR-168`: incomplete or conflicting recovered batch membership fails closed before
  transport and remains under the same Activity owner;
- `HFR-169`: work with no Run row still reaches an Activity-owned terminal outcome visible
  from its session;
- `HFR-170`: every skip re-arms with backoff;
- `HFR-171`: the independent supervisor reports which owned lane and partition
  are overdue;
- `HFR-172`: the table-driven producer contract fails when an official entry
  point lacks the mapped post-commit event; it observes no event before commit,
  after rollback, or after a losing CAS, and a worker-thread publisher touches
  asyncio state only through the controller loop;
- `HFR-173`: unsupported event types remain rejected by the existing socket
  endpoint; browser subscribers cannot suppress a controller wake; and local
  fanout plus bounded event-id deduplication delivers once both while the return
  stream is connected and while it is reconnecting;
- `HFR-174`: task and managed-watch definition changes wake their separate
  reconcile lanes without waiting for fallback; the generic
  `definitions.updated` event safely coalesces both;
- `HFR-175`: global service-instance lock loss stops every lane registration,
  including `session_deliveries`, before the old process can query or claim, and
  no old-generation completion can re-arm work;
- `HFR-177`: IM transport readiness directly wakes `requests` and a skipped Run
  resumes without waiting for slow reconciliation. Deterministic barriers cover
  reconnect winning the final readiness guard (no failure notice; one request
  claim), stale settlement winning and committing before readiness publication
  (one terminal failure; zero claim), and a queued-to-running request claim
  making the stale exact CAS lose. They also keep an expired stamp queued when
  the platform reconnects and drops again before any worker clears that stamp:
  the second outage receives its own full TTL from the injected readiness clock,
  then permits exactly one terminal settlement after that TTL. They prove that
  exact evidence cleanup may win before the second disconnect, that repeated
  disconnect observations do not extend one outage, and that every disconnect
  producer uses the same edge owner. Process startup gives a configured but
  not-ready platform the same fresh bounded window. A blocked Slack
  final-decision guard does not block Discord readiness mutation,
  outage-generation changes, or its `requests` wake;
- `HFR-178`: a real signal while the controller loop is running requests
  nonblocking async shutdown; a synchronous lane outliving shutdown grace
  retains the service-instance lock and exact dependencies until its worker or
  process exits, so a replacement process cannot overlap it; the same is true
  for an async worker past its external-effect boundary, and a signal before loop
  startup follows the same ownership decision;
- `HFR-179`: contended managed-watch store reconciliation and per-watch guarded
  store writes stay single-flight, keep the SQLite row and full-store mirror
  consistent, and cannot wrongly restart a completed one-shot watch. Blocking
  store phases run in the tracked worker, while `reconcile_watches()`,
  `asyncio.create_task()`, `_active_tasks`, cancellation, callbacks, and runtime
  projection assembly are asserted to run on the controller loop. Store
  contention does not block that loop or another lane, while a legitimately
  long-running waiter is not canceled by the maintenance timeout and remains
  owned by the managed-watch lifecycle.

Exit criterion: normal work is event-woken, missed hints are recovered, a hung
lane cannot block another lane or the controller event loop, lifecycle teardown
leaves no old-generation worker, and Activity/Delivery/Turn/Run ownership is
unchanged.

## 7. PR7 — Evidence gate before any new timeout model

**Open. This is the next unit of work.** PR3 and PR4 removed the serial-drain
and reclamation causes; whether any terminal-truth or scheduler-liveness defect
survives them is now an open question that only evidence may answer.

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

#### PR7R status (2026-08-07) — first increment, revised under twenty-five review rounds

The matrix is `tests/run_terminal_truth_evidence.py`, closed by
`tests/test_run_terminal_truth_matrix.py` (`HFR-184…187`, `HFR-189…190`,
`HFR-192…194`, `HFR-196`, `HFR-198`, `HFR-200…204`, `HFR-206…211`) and fed by
`tests/test_run_terminal_truth_evidence_probes.py` (`HFR-180…183`, `HFR-188`,
`HFR-191`, `HFR-195`, `HFR-197`, `HFR-199`, `HFR-205`). It expands
to the full 3 × 2 × 4 × 7 = 168 cells; each cell is written once per
(lane, trigger, outcome) with a `per_backend` override wherever the backend
demonstrably changes the answer, because the durable owner chain is chosen by
the lane, not by the backend. **All 168 cells are `unproven`** and name the
exact probe that would close them; `UNPROVEN_BUDGET` pins that number so a gap
cannot widen silently.

The budget moved 28 → 78 → 117 → 168 across the first three adversarial review
rounds and has held at 168 since; rounds four through twenty-five changed how the
findings are demonstrated, three question verdicts, and twenty degenerate
guards (three of them in round ten, two more in round eleven that were in this
unit's own new code, two in round twelve that were round eleven's fixes, two in
round thirteen that were round twelve's, two in round fourteen that were round
thirteen's, and two in round fifteen that were round fourteen's — the newest
guard is now reliably the likeliest defect). Round 17 moved Q2 from `answered`
back to `open` without touching the budget, and the distinction is worth
keeping straight: the 168 cells are the trigger/outcome matrix, and the Q2
signal table is a separate six-cell table the budget has never counted.
Why it moved is the
most useful thing this increment produced. Cells were marked
`covered` by a test that is real, passing, and about something adjacent to the
cell rather than about the cell. The first round found fifty such cells, in four
substitutions, each of which reads as coverage until the cited test's body is
compared against the cell's subject:

- a **storage-writer property** — the owed-notice stamp, proven by driving the
  five terminal writers directly — standing in for an IM dispatch path that
  nothing shows reaching those writers;
- a **Claude-only fixture** standing in for three backends: every Activity test
  starts with `backend="claude"`, and `modules/agents/claude_agent.py` is the
  only production producer of a `SessionActivity`;
- an **ownership-transfer test that asserts the Run is still `running`**
  standing in for a terminal success;
- a **constant lookup** (`SETTLEMENT_TERMINAL_STATUS[...]`) standing in for a
  settlement that was never driven.

The second round found 39 more, in cells the first round had read and left
alone — which is the real finding, because it means one adversarial pass over an
evidence matrix is not enough:

- a **cancellation test** — it calls `execution.cancel()` and asserts the Run is
  `failed` with `last_error` — standing in for a successful one-shot `at`
  firing (3 cells);
- a **Delivery-FSM prewrite test** that inserts a bare Delivery and never
  creates an `agent_runs` row, standing in for what the Run does when the
  terminal write fails (12 cells);
- an **AST read of each backend's `handle_stop`** plus a Running-tab test whose
  `SessionTurnManager` is a stub returning a prebuilt `{ok: True}` dict, the two
  together standing in for a durable Stop settling a real Run (12 cells);
- a **settlement test admitted through `enqueue_agent_run`** with
  `task_trigger_kind="agent_run"` — not one of the matrix's four triggers —
  standing in for all four of them (12 cells).

That last one generalises: a test can trace a cell's **settlement** half
perfectly and still say nothing about its **admission** half, and admission is
exactly what a trigger owns.

The third round asked that question of the remaining cells **as a class** and
got one answer for all of them: **no test on master traces a Run from a
trigger's admission through to that Run's terminal settlement.** Every surviving
citation proved a segment —

- **storage writers driven in isolation**, with no admission and no Run (12);
- a **projection whose backend never returns**, so nothing settles (9);
- an **Activity registry holding a bare run-id string** against a mock or stub
  run store — there is no `agent_runs` row in any of those tests at all (16);
- a **genuine end-to-end terminal chain** that is nonetheless admitted by
  `enqueue_agent_run`, deliberately not one of the four triggers (10);
- the **two End reproducers**, filed as `defect` for a Run half they explicitly
  disclaim (8) — and because `defect` is excluded from `UNPROVEN_BUDGET`, that
  misclassification was hiding those eight cells behind a vocabulary word. Both
  are now `unproven`, and the finding ↔ matrix tie scans every cell's detail
  text instead of only `defect` cells.

An all-`unproven` matrix is an uncomfortable result and it is the correct one.
The unit's value is the 168 named probes plus the demonstration that the earlier
coverage was an artefact of reading citations charitably.

The matrix guard cannot catch any of these: it verifies that a citation
resolves, not that it is about the right thing. That check is a reader's job,
and it is now a required one whenever a cell is marked covered — every round,
not once. Two second-order lessons from round 3 about the guard itself:

- a guard assertion can encode a **conclusion** rather than a property.
  `UNPROVEN_BUDGET < len(_CELLS)` was written to mean "this unit is not an empty
  shell" and became false the moment the evidence said so. It is now an
  anti-degeneracy check on the number of **distinct** probe texts, which is the
  property that was actually wanted;
- the same **validation asymmetry** appeared twice in one file: test citations
  got full nested-symbol resolution while finding *owners* were checked only for
  file existence, so a renamed owner would stay green. There is now one
  resolver, `_assert_symbol_exists`, used for both. `HFR-105`'s copy in
  `tests/test_scheduled_tasks.py` still has the leaf-only weakness and is
  untouched by this unit.

Rounds 2, 3 and 4 also overturned claims in the other direction, all three times
the same way. Q2's blanket "no backend exposes a per-Turn progress signal" was
false for codex (round 2, `_active_turns[base]` read instead of the `item/*`
notification), then false for Workbench-lane opencode (round 3,
`_active_requests[base]` instead of `run_prompt_poll`'s `request.context`), then
false for Workbench-lane claude (round 4, `session_turn_started[composite_key]`
instead of `_adopt_pending_turn_token`). Loosening a blocker is as much a review
outcome as tightening a claim; the transferable part is that the check has to be
made one level up from where the loss happens. Making the same error three times
after naming it twice is the durable lesson here: a remembered mistake is not a
guard, so the form that survives is a rule — before concluding a backend exposes
no signal, read the **producer**, not the map the producer writes into.

Round 4 changed no cell counts and hardened both reproducers, in opposite
directions. The codex one was **under-staged**: its fixture left `_transports`
empty and `get_thread_id` returning `None`, which is exactly the dead-app-server
case the teardown exists to clean up, so the probe was staging the absence of the
interrupt it then reported missing. Giving it a live transport relocated the
defect (see PR7R-F2). The claude one **over-claimed**: it asserted an ordering and
left reachability to the reader. Review challenged it on cold-start grounds and
was right to — an ordering is not a window until the fixture's combination of
registries is shown to occur. It does occur, warm-idle, and that is now driven
rather than argued. Review's *prescription* for it — block the real
session-creation path and observe the registries — was rejected, because it would
turn an End probe into a session-creation test and would need a live SDK.
Accepting a finding's gap while rejecting its prescription is a third review
outcome, distinct from accepting or rejecting whole.

Round 5 found that round 4's fixture fix stopped one step short, and one more
degenerate guard. The codex probe's live transport was necessary and not
sufficient: the fixture still made the session its cwd's last, so `_end_codex`
stopped the shared app-server and the failed-interrupt run's turn died by process
kill. Two runs that both end the turn — one by interrupt, one by kill — report
`ended` truthfully, so the identical payloads were *accurate* rather than
evidence. Only a **co-tenant** session on the same cwd keeps the transport up and
leaves the un-interrupted turn genuinely executing, and the probe now asserts
that survival instead of inferring it from the fixture's shape. Generalised: when
the claim is "these two worlds are indistinguishable to the caller", the two
worlds have to be shown to actually **differ** first, and a teardown side effect
can quietly collapse them into one.

The degenerate guard is the third of its kind. The finding ↔ matrix tie compared
`referenced` against `set(PR7R_FINDINGS)` — two sets that shrink together, so
deleting a finding from both the dict and every cell detail kept it green, and an
empty matrix passed it. It sat two lines below a block that pins the five
question ids literally. The finding ids are now pinned the same way. The rule
worth carrying: a guard built from a comparison between two **derived**
collections proves consistency, never existence, so anything the plan advertises
has to be named literally somewhere in the guard.

One method note, since an evidence-only unit has no production diff to stash: new
assertions are counter-checked by **mutating the production fact** and confirming
the probe fails. That check needs checking too — one round's mutation appended a
comment that left the asserted substring intact and produced a false pass.

Round 6 closed Q2 and re-opened Q5, and both corrections came from the same
place: a claim of **absence** that no one had driven. Q2 had held claude and
opencode on direct IM open because `turn_token` "is stamped only by
`core/session_turns.py` and the streaming turn dispatch, both Workbench owners".
That was arrived at by grepping the literal string; the write that decides it is
constant-keyed — `platform_specific[AGENT_TURN_TOKEN]` in
`AgentService._stamp_runtime_turn`, which `handle_message` performs for **every**
request on **both** lanes before the backend is invoked. The lane split does not
exist. `HFR-188` now drives the real `handle_message` twice on one runtime key
and asserts the tokens differ (per Turn, not per session) and that a pre-existing
Workbench token is preserved. Six cells went `covered` at that round and the
guard forced Q2 to `answered`; round 17 reopens the two opencode ones, so today
**three cells are `covered`** and **three cells are `open`** and the same guard
forces Q2 back to `open`. For live dispatch the inactivity timeout is no longer
clear on attribution: Codex/direct-IM still lacks an integrated Run emit, and
the activity timestamp remains per **session**.
`HFR-191` narrows the companion over-claim: codex's `should_emit_progress` filter
reads the single `_active_turns[base]` slot, and the runtime gate is held across
the whole backend call, so one runtime key admits one live turn at a time. Round 8
had to reinstate the over-claim — see below — because that says nothing about a
slot keyed differently.

This is the **fourth** correction of the same underlying error (see HFR-OBS-024),
and the first where the previously-stated rule — read the producer, not its
projection — was followed and still did not help, because the failure had moved
from *which* artefact was read to *how* it was searched for. The rule extends: a
"nothing writes X" claim is only as good as the search behind it, so resolve the
**key**, not the string.

Q5 went the other way. Its answer said `last_run_at`/`last_error` "are written in
the same CAS-guarded terminal transition that settles the Run" — they are not.
`store.mark_task_result` commits a definition-level stamp inside `_execute_task`,
and the Run's terminal CAS is a *second* write, `request_store.complete` in
`_execute_claimed_request`'s `finally`. HFR-264 reconciles a refused stamp —
HFR-261 is the definition-write CAS that refuses it, HFR-264 is what turns the
refusal into a failed Run — and nothing reconciles process loss in the gap, so a
definition can advertise a
`last_run_at` for a Run that never settled. The health trio remains answered —
derived per read over a bounded window, so it cannot drift — and Q5's verdict is
now `open` on the two stored fields alone.

The round's second degenerate guard was in the labelling itself: a docstring
claiming `HFR-186` for a test the catalog files as `HFR-187`, plus two guards with
no catalog row at all. Every guard here checks that a **citation** resolves to a
test; none checked the reverse. `HFR-192` ties both directions for the two PR7R
modules.

Round 7 found that round 6's own two guards were each degenerate in the way this
unit keeps rediscovering, and that the plan had been left contradicting itself.
`HFR-191` blocked the second turn *inside* the first backend call, which is what
any mutex does; the Q2 conclusion turns on the gate outliving the call, because a
backend returning from turn submission is not a turn ending. Adding a release
when the backend returns — the exact regression that matters — kept the probe
green. It now lets the first call **return**, asserts the queued turn is still
outside the backend, and only then releases; the mutation fails it. `HFR-192`
walked `tree.body` for `ast.FunctionDef` only, so an `async def test_*` — the
natural shape for the next probe in a unit whose subject is an async admission
path — or a test method in a class was skipped in silence by a guard whose name
promised every test. It recurses into classes and accepts both function kinds
now. The pattern across four instances: **a guard's exemptions are invisible, so
they have to be counter-checked as deliberately as its assertions.**

The plan itself was the third finding, and the most consequential, because it is
the artefact the next unit reads. §7's numbered "Question verdicts" block still
said Q2 blocks the inactivity timeout and Q5 is answered, directly contradicting
the round-6 narration three paragraphs above it and `PR7R_QUESTIONS` — including
the false claim that `last_run_at`/`last_error` share the Run's terminal CAS. The
allocation summary likewise still advertised `HFR-188…219` as reserved after the
same commit filed rows through `HFR-192`, which would have handed a follow-up an
id this unit already owns. Both are now tied to code rather than to diligence:
`HFR-193` compares the verdict word in this document against
`PR7R_QUESTIONS[q]["verdict"]`, and `HFR-194` asserts that no catalog id falls in
the range this document calls reserved. Prose in a plan drifts; the one token an
implementer greps for should not be prose.

Round 8 took back round 6's codex narrowing, and round 9 took round 8's back.
The key-space half survives: the gate keys on `BaseAgent.runtime_turn_key`, the
composite `<base>:<working_path>`, which codex does not override; `_active_turns`
keys on `request.base_session_id` alone, and `CodexAgent.handle_message` moves the
tracked cwd to whatever the latest request carried. So a working-path change does
get two requests past the shared gate at once. What both rounds missed is the code
*between* the gate and the slot. `handle_message` holds
`_session_locks[base_session_id]` — the registry's key space, not the gate's —
across its whole body, and inside it sends `turn/interrupt` for any active turn
before `turn/start`. The two requests are therefore serialized before they reach
the backend, and the turn that goes mute has been interrupted.
`should_emit_progress` is correct.
The real consequence of the split is narrower and still worth having: a cwd change
converts "queue behind the gate and run after" into "interrupt the running turn
and replace it". `HFR-195` now drives that end to end — the two key functions, the
gate admitting both, and the real `CodexAgent.handle_message` recording
`turn/interrupt(turn-1)` before `turn/start(turn-2)`. The probe the codex cell had
carried since round 4 ("can anything put two live turns on one base session") is
closed — with the qualification round 10 adds below: serialized at the lock, but
*not* non-overlapping on the backend.

The error is a new shape, not a fifth repeat of the projection mistake: a correct
fact about one key was carried across to a different key. Worse, the assertion
written to tie the two — `"base_session_id" in getsource(_runtime_turn_key_for_base_session)`
— was a substring read of a function the gate never calls, i.e. the exact
nearby-passing-assertion this unit was created to eliminate, committed by the unit
itself. The rule that follows: **when a conclusion joins two mechanisms, the join
is the claim, and an identifier that appears in both is not the same identifier
until it is computed and compared.** Round 8's second finding was the same
staleness class as round 7's: `HFR-183`'s docstring still described the
4-cells-covered lane split its own assertions had contradicted for two rounds,
invisible to every guard because `HFR-192` reads only the scenario id out of a
docstring. `HFR-196` ties the one sentence shape that went stale — a spelled-out
number of cells said to be covered or open — to the real table, across both PR7R
test modules, the matrix module and this document.

Round 9 then took *that* back, and the rule it leaves is about the retraction
pattern rather than about codex. The `should_emit_progress` claim has now been
argued three times — correct in round 6, defective in round 8, correct again in
round 9 — and each argument reasoned from the two **ends** of a mechanism (the
gate's key at one end, `_active_turns` at the other) without ever running the code
in between. `handle_message` is that code, and it settles it in two lines: a
base-session lock across the whole body, and `turn/interrupt` before `turn/start`.
**A claim that keeps flipping is not an unlucky claim; it is a claim whose subject
was never driven end to end, and the flip count is the signal to go drive it.**
`HFR-195` now drives the real method.

Round 9's second finding is round 8's join rule applied to the *question* rather
than to a conclusion. Q2 asks about the exact Turn **and participating Runs**;
every probe asserted Turn tokens, and the verdict was written as though the
conjunction had been checked. The join turns out to be real and cheap —
`_owned_agent_run_ids` reads `accepted_agent_run_ids` off the emit context and
`_durable_accepted_agent_run_ids` looks Runs up by that context's `turn_token`, so
Run attribution is *derived* from Turn attribution — but cheap to verify is not
verified. `HFR-197` drives it on all three backends, including the case that would
smear Runs across Turns had claude's reused receiver context merged rather than
replaced. Q2 stays `answered`; its residual is now one item, the per-session
activity timestamp.

The other two findings are guards failing their own contract. `HFR-194` reads only
the `- PR7:` allocation line, so this document's *headline* range sat at
`HFR-180…187` while the same commit filed catalog rows through `HFR-196`. And
`_assert_node_exists` reused `_assert_symbol_exists` verbatim, inheriting an
exemption written for a different caller: a class leaf is legitimate for a finding
OWNER and is not a pytest node id, so `tests/foo.py::_helper` resolved green — a
citation naming a real symbol that no test run executes, which is the failure the
resolver exists to prevent, displaced one level. `HFR-198` pins both strictnesses,
and asserts the whole citation corpus against the tightened rule in one place.

Round 10 — seven findings, all seven accepted on the facts, one with its proposed
remedy rejected. The headline is that **the projection error survives being
named.** Round 9 caught itself reading `_active_turns` for a claim about the event
stream, went and drove `handle_message` — and then read the *registry's* one slot
for a claim about what the **backend** is doing. "There is no window in which two
codex turns are live" is false: codex's own protocol note
(`docs/plans/codex-app-server-refactor.md`, insertion step 2) requires waiting for
`turn/completed` with interrupted status before `turn/start`, and production sends
the two calls back to back, so turn-1 is still executing while turn-2 is
registered. The finding asked to reopen Q2; that is rejected, because Q2 asks
whether the exact-Turn signal *exists* and the window does not touch it. What the
window corrects is the **basis** of the answer, and the corrected basis is
stronger: both of the window's arrivals are handled, and `HFR-195` now drives them
through the real `CodexEventHandler` rather than a stub — the interrupted turn's
late `item/completed` is dropped by the named guard in `_on_item_completed` while
the live turn's is kept, and its late `turn/completed` is popped, ack-removed and
stream-released with **nothing** emitted. One byproduct is pinned there too: an
interrupted turn's ack is removed twice, eagerly at `clear_pending(active_turn)`
and again when the completion lands. When the subject of a claim lives on the
other side of a wire, the in-process map is still a projection no matter how
carefully you drove the code that writes it.

The second finding is the same displacement inside this unit's own tests:
`HFR-197` asserted per-Turn Run attribution against a **stubbed** `session_turns`,
i.e. a store that answers whatever the test wants, so a real store that smeared
Runs across Turns would have left it green. It now drives real rows through the
real durable chain, which cost three schema lessons worth recording: a Delivery
may not be inserted directly as `accepted`
(`ck_message_deliveries_materialization`), a Session may hold only one live Turn
(unique `session_turns.session_id`), and `agent_runs.delivery_id` is unique — so a
Turn with plural participating Runs must come from a **merged** Delivery batch,
not from two Runs on one Delivery.

Findings three through five are guards weaker than their own comments, which is
now the fifth instance in this unit. `_assert_node_exists` was tightened at the
**leaf** last round and left the **containers** unexamined, so
`tests/foo.py::Helper::test_x` resolved green while pytest collects nothing in a
class that is neither `Test*`-named nor a `TestCase` subclass — *a rule fixed at
one nesting level does not travel.* `HFR-192` checked docstring ids against the
catalog in one direction only, so a catalog row pointing at a deleted test stayed
green; it is bidirectional now. And `HFR-185`'s anti-degeneracy check counted
distinct probe names *globally*, which one diverse row satisfies on behalf of
every degenerate one; it is per `(lane, outcome)` group now.

The last two narrow claims to what was actually driven. Q1's "reservation does not
settle the Run" rested on a scheduler test that stubs `submit_scheduled` and
merely *reports* `queue_persisted` / `delivery_owner_transferred`. `HFR-199` drives
the real chain and finds the fact holds and is **broader** than claimed:
terminalizing the *Turn* does not settle the Run either — only
`settle_agent_runs_for_turn_in_connection` does, so any path that ends a Turn
without calling it leaves a live Run with no owner. And `HFR-182`'s Q3 claim said
the Turn-level `source_kind` is stamped by the *first* participant, which the
probe preloaded rather than drove; it now attaches a second Run carrying real
provenance and asserts only what that shows — a later participant does not
restamp it.

Round 11 — three findings, all three accepted; the round's substance is that two
of my own fixes were degenerate on the first draft and only the counter-check
found them. The first finding is that round 10 retracted the codex overlap claim
in five artefacts and left it standing in `catalog.yaml`, the file that exists to
**be** the canonical record — a follow-up unit reading only the scenario row would
have taken the retracted contract as the contract. A seventh copy the finding did
not name sat in `HFR-OBS-034`. That is the fourth stale-copy defect in five rounds
(docstring, headline range, document, canonical row), each previously fixed as a
text edit while the class was named in prose only. **Naming a class does not
enforce it; four recurrences are the proof that prose is not a control.** So the
retractions are now data — `RETRACTED_PHRASINGS` in
`tests/run_terminal_truth_evidence.py` — and `HFR-201` enforces them across every
artefact this unit owns.

Writing that guard produced three defects of its own, all caught by insisting the
counter-check restore the **real** prior text rather than a paraphrase of it. The
ledger's first phrase was `"turns never overlap"`, which spells the retraction as
round 10 wrote it and *not* as the catalog carried it ("so the two never overlap",
the wording this round retracted) — a ledger keyed to a paraphrase of the bug
cannot fail on the bug. The proximity rule was
then a 400-character window, and restoring the real stale sentence left it green,
rescued by the word "narrower" describing the consequence of the gate split and
nothing to do with any retraction; widening to "own sentence plus the next" still
passed it, because "narrower" was in exactly that next sentence. The rule is now
the phrase's own sentence and nothing else, and the three strings that decided
that width are asserted in the test. **A proximity rule wide enough to span
unrelated prose does not test proximity, it tests prose density — and a
counter-check written against a remembered version of the defect proves only that
you remember it.**

The second finding is round 10's own lesson at the next joint out. Round 10 taught
`_assert_node_exists` that a container class must be collectible; the
catalog-discovery walker in the same file kept its own inline class walk and never
learned it, so a row citing a test inside an uncollectible class was *discovered*
by one check and *rejected* by the other. One predicate, two readers, one updated
— the walk is now a single module-level `_collected_tests`. The third: `_expand`
read `cell.get("per_backend", {})`, so a misspelled backend key fell back to the
shared proof silently, which is worse than missing evidence because it looks
present. `_validate_matrix` now whitelists both key levels and `HFR-200` pins all
three rejections. The checked-in matrix was audited before the guard landed and
holds no such typo, so the budget stays at 168.

Round 12 — four findings, four factual cores accepted, one proposed remedy
rejected. Three of the four audit round 11's own fixes, which is now the settled
shape of this review: the newest guard is the likeliest defect. The first is that
`_collectible_class`, taught in round 10 that a container class must be
collectible, checked the name and the base list and stopped there — pytest also
*refuses* a `Test*`-named class that defines `__init__` or `__new__`
(`cannot collect test class ... because it has a __init__ constructor`), so a row
could cite a test that never runs and still be discovered. The fix was verified
against this repo's pytest rather than reasoned, including the negative case that
keeps it from becoming a blanket ban: a `unittest.TestCase` subclass with
`__init__` *is* collected, because the unittest plugin claims it. **A
collectibility predicate has to be run against the collector, not derived from
what you remember of it.**

The second is a guard that was never written. Nothing asserted that a catalog id
names exactly one row, and every reader de-duplicates before it looks — `HFR-192`
builds `{id: test}` and compares `{ids}` against the discovered set — so two rows
answering to one id collapse to one element and pass the count, tie and orphan
checks while consumers keep whichever row they read last. `HFR-202` checks the
whole catalog for that collision, and deliberately does not ban the reverse: seven
tests legitimately prove more than one scenario. **A set built from a key silently
accepts a duplicate key.**

The third is the sharpest, because it is round 11's fix reproducing the accident
round 11 named. `HFR-201`'s marker list held the stem `"narrow"` matched with
`in` — and "narrower" is precisely the word that had rescued the stale sentence
the guard was written to catch, so the guard against a false rescue could be
satisfied by the word that caused it. Markers are now whole-word with every
inflection spelled out, and the finding's counterexample is a fixture. **When a
postmortem names the word that fooled you, the fix has to be checked against that
word; naming it is not excluding it.**

The fourth had a right fact and a wrong remedy. Every probe behind Q2's Run clause
built rows with `platform="avibe"`, so the three `direct_im` cells asserted
per-Turn **Run** attribution from durable-lane rows plus a belief that the write
path is platform-blind — the finding asked to reopen the cells. The belief holds:
`_submit_scheduled_turn` gates on session id, `_start_persisted_turn` stamps the
token unconditionally, and `accepted_agent_run_ids_for_turn` filters on state and
turn id; not one reads a platform. So the remedy was rejected and the belief was
driven instead — `HFR-197` now builds a telegram-scoped Scope and Session, takes a
Delivery through claim, native bind and materialized acceptance, binds an
`agent_runs` row and reads back the same attribution. **The belief was cheap to
check, and cheap to check is not checked.** Two schema facts surface only on that
lane and are pinned with it: acceptance *materializes* the snapshot into a
`messages` row, so `messages.scope_id` needs a real Scope and `messages.session_id`
is a **deferred** foreign key that fails at commit rather than at insert; the
Workbench probe passes `scope_id=None` and persists no Message, so it met neither.
The one genuine bypass is sessionless CLI dispatch, which writes no Delivery and no
Turn: empty attribution rather than wrong attribution, outside both lanes — which
are Session-scoped by their own definitions — and asserted as an empty result
rather than left implied. Nothing this round touches a matrix cell, so the budget
stays at 168.

Round 13 — three findings, all three accepted, and all three are the previous two
rounds' own fixes read one step further. No new scenario id and no budget move:
the guards that changed are `HFR-198`, `HFR-193` and `HFR-201`, each of which
already owns a row. The first: round 12 taught `_collectible_class` about
constructors and left its TestCase rule matching a **name** — any base whose
trailing attribute ended in `TestCase` — so `class Helper(FakeTestCase)` passed on
both readers and a row citing a test inside it would have been discovered by the
corpus walk and accepted by the citation check while pytest ran nothing. The base
is now resolved: an exact unittest name reached through the module's own imports,
alias included, or a base defined in the same module followed transitively. Two
positive fixtures keep it off the easy over-correction — `AliasedTests` inherits
through `from unittest import IsolatedAsyncioTestCase as Base`, a name with no
"TestCase" in it, and `Derived` inherits through an intermediate the old docstring
admitted it could not follow. **A rule about what pytest does with a class is
about ancestry, never about spelling; a "conservative approximation" that matches
a suffix is permissive in the direction nobody checked.**

The second is round 12's catalog-duplicate finding one document over, and it
landed on the guard written to stop exactly this drift. `HFR-193` parsed §7's
verdict block with `dict(re.findall(...))`, which keeps the last pair for a
repeated key — a stale `Q2 — open` line left standing above the current
`Q2 — answered` line vanishes into the mapping, both checks pass, and the plan goes
on handing the next unit two contradictory instructions. The parse is now
`_stated_plan_verdicts`, which raises on a repeat, with fixtures on synthetic text
so the regression does not require corrupting the real plan. **A guard whose parse
de-duplicates is not checking the document it reports on.**

The third is the retraction ledger's *enrolment* rather than its rule.
`HFR-OBS-024` still stated as current fact that `should_emit_progress` gates on the
`_active_turns[base]` slot "so the attribution is thrown away downstream", while the
end of the same observation says that claim was narrowed and the filter is correct
— a reader derives the opposite Q2 contract depending on where they stop. Both
remedies applied: the sentence is rewritten as an explicitly historical
round-8-added, round-9-retracted claim, and the phrasing is a new
`RETRACTED_PHRASINGS` row so `HFR-201` enforces it corpus-wide. **Making the
retractions data fixed the enforcement, not the enrolment — a ledger only holds
what someone remembered to put in it.**

Round 14 — two findings, both accepted, and both are round 13's shape again: the
guards under review are `HFR-198` and `HFR-201` for the second round running, and
both failed at their **input** rather than in their logic. No new scenario id and
no budget move. The first: `_collectible_class` decided collectibility from the
class name plus, since round 13, its resolved base — and pytest lets a module, a
class or a function overrule its own name with `__test__`. A future PR7R class
named `Test*` that sets `__test__ = False` was therefore collectible on both
readers, so `HFR-192` could report a covered scenario whose assertions never
execute. Every branch was probed against this repo's pytest rather than reasoned
about, which is why the fix is not a one-liner: the flag is **bidirectional**
(`class FlaggedIn` and `def plain_named` with `__test__ = True` *are* collected,
so an opt-out-only reading would have under-reported), it beats unittest ancestry
in the negative direction, it does **not** excuse the round-12 constructor rule,
at module level it takes the whole file down including a bare `def test_top`, and
the function spelling is written one scope *out* from the function it applies to.
**When a guard predicts another system's behaviour, enumerate that system's
inputs before trusting the one you happened to start from — three rounds running,
this predicate was wrong not because its logic was wrong but because it read too
few things.**

The second is the third narrowing of one rule. `_marker_near` scopes a retraction
to "the phrase's own sentence", and the sentence was computed over a whole
flattened file; a YAML field ends with no terminal punctuation, so a stale `name:`,
the `detail:` below it and the comment after that were one sentence, and a marker
in any of them vouched for a claim in the others — after the 400-character window
(round 11) and the substring markers (round 12). The rule is unchanged; the corpus
is now split into prose **units** before it runs: one per scalar value and one per
contiguous comment block in YAML, the whole file in Python and Markdown, where a
wrapped `#` comment or a pair of adjacent string literals genuinely is one
continuous statement. Comments are units rather than dropped, because
`catalog.yaml` keeps thousands of lines of prose in them that `yaml.safe_load`
would discard. Both over-corrections are pinned by fixture: whole-file flattening
lets the stale field pass, and a per-source-line split chops a folded block scalar
so a wrapped phrase becomes invisible. **A proximity rule is a claim about a unit
of authorship, so it has to be told where the units are; flattening structured
data into prose just moves the blindness somewhere it will not be noticed.**

Round 15 — three findings, all accepted, plus two this unit found itself. One new
scenario (`HFR-203`), no budget move. The first: Q3's answer and the verdict below
said the Turn-level `source_kind` is "stamped by the first participant" — a claim
round 10 already retracted *in the probe*, since `HFR-182` preloads the field and
drives only `_attach_accepted_agent_runs`, establishing that a later participant
does not **restamp** the label, not who set it. The originating write is
`_hydrate_delivery_batch_context` and stays an open probe. The retraction had been
applied where it was found and nowhere else: the edit was made, the ledger row was
not — round 13's lesson recurring one round later.

The second: Q5's answer, its observation copy and two plan lines named `HFR-261`
as the scenario reconciling a **refused** terminal stamp. `HFR-261` is the
producer-side definition-write CAS that makes the stamp refuse; `HFR-264` is the
consumer that turns the refusal into a failed Run. Both are real and adjacent,
which is why the wrong one reads plausibly — and a follow-up unit would have gone
to the guard instead of the reconciliation. The deeper defect is that a scenario
id in prose was never evidence of anything: nothing tied it to the answer's
`evidence` tuple, so it could name any id, or a wrong one, and every guard stayed
green. `HFR-203` now requires that any `HFR-\d+` an answer leans on be carried in
that answer's evidence. The third: `HFR-OBS-024` asserted the Run is `running`
after a Delivery reservation and an ownership transfer; `HFR-199` drives the real
rows and reads `queued` at every nonterminal step, the stale word inherited from
the stubbed scheduler test round 10 superseded.

The fourth is this unit's own, and it is why the round-15 fixes are trustworthy at
all. Counter-check A restored the round-10 claim into Q3's answer and `HFR-201`
stayed **green**: Q3's sentence opens with "NARROWED in round 3", a real marker
about a *different* retraction, two hundred characters up the same sentence. That
is round 11's "narrower" one level up — the row meant to protect the fix did not.
So the rule narrows a fourth time (window, whole words, scope, now **quotation**):
the phrase must be quoted, which is what every genuine retraction here already
does, and is the difference between mentioning a claim and making one. Applying it
surfaced six corpus sites narrating a retraction while restating it unquoted. And
making it computable required finishing round 14's input fix on the other half of
the corpus — a `.py` file was still flattened whole, so a docstring ending without
a full stop merged with the literal after it, and counting quote pairs across a
whole file pairs the close of one literal with the open of the next. Python is now
split per string literal and per contiguous comment block, adjacent literals
merged. The fifth, found while enrolling the rest: "no backend exposes a per-turn
progress signal", retracted in round 3, was standing in two artefacts and in no
ledger row; every cell exposes one on live dispatch. **A citation is not evidence
until something reads it — a scenario id in prose, a retraction applied only where
it was noticed, a marker that merely shares a sentence: each looks like the corpus
checking itself, and none of them was.**

Round 16 — one finding, accepted; no new scenario and no budget move. `HFR-183`'s
docstring said of codex "for codex it drops it", and its closing comment concluded
"the drop above is a discarded live signal, not correct filtering" — round 8's
reading, retracted in round 9 and narrowed again in round 10. The probe Q2 cites as
its evidence therefore contradicted the answer *in its own summary*, while its later
sections drove the correct reading: `CodexAgent.handle_message` holds
`_session_locks[base]` across its whole body and sends `turn/interrupt` before
`turn/start`, so the older turn is already interrupted when the filter drops it,
and its late events are handled rather than lost — `HFR-195` drives both halves.
Both sentences are now historical and both wordings are enrolled.

The ledger is not what failed. Round 9's retraction *was* enrolled — as "attribution
is thrown away", in round 15 — and the same claim went on standing in two other
wordings inside the very file that row was written to police, because a phrase
ledger matches restatements and not paraphrases. Round 15 called its defect "the
edit was made, the row was not"; this is one level down: the row was made, from the
sentence a reviewer happened to quote. **Enrolling a retraction means grepping the
corpus for the claim's subject and enrolling every wording that turns up, not the
one that was quoted at you.** That sweep was run and found exactly these two. The
residual risk is stated rather than guarded: a distant enough paraphrase still
passes, and no mechanical rule distinguishes "restates a retracted claim" from
"discusses the same subject correctly" — making the ledger semantic would be the
degenerate shape it exists to prevent, so the remedy is a habit, not a test.

Round 17 — four findings, all four accepted, none rejected; two new scenarios
(`HFR-205`, `HFR-206`), one question verdict moved, and **no budget move**. The
budget counts the 168-cell trigger/outcome matrix; everything below happens in
the Q2 signal table, the question answers and the corpus, none of which it has
ever counted. Saying so explicitly is part of the round, because "a verdict
moved and the budget did not" is exactly the shape that looks like a missed
update.

1. **Q2 reopens, and it is the fifth instance of this unit's oldest reading
   error.** Every clause of Q2's answer walks a live dispatch path. OpenCode has
   a second emitting entry point, `run_restored_poll_loop`, which is what runs
   after a daemon restart, and it was never walked. It rebuilds its emit context
   through `ProcessingIndicatorHandle.from_snapshot`, whose rebuild is a fixed
   three-key allowlist — `platform`, `is_dm`, `context_token` — so `turn_token`,
   the runtime turn token and `accepted_agent_run_ids` are all dropped, and all
   three of the loop's emits pass that stripped context. The module names
   neither `turn_token` nor `logical_turn_id` anywhere. Both halves of Q2 fail
   there at once, Turn and Runs, on both opencode lanes, because the discard is
   in the shared rebuild and reads no platform. The sharper half, and the reason
   both cells read `defect` rather than `unproven`: the identity is *persisted*.
   `OpenCodeAgent._process_message` writes `logical_turn_id` into the very dict
   the rebuild is handed, under the native steering key, and the restore path
   reads it back to steer; restored `additional_steer_targets` are built with
   `context=None`, which is production admitting the same thing in its own
   words. The Turn survives the restart and is thrown away one call later.
   Round 18 retracted round 17's "one line at the rebuild rather than a
   persistence change" as a size claim standing in for a completeness claim,
   true of the Turn and false of the Runs. `HFR-205` asserts `"run-1" not in repr(snapshot)` — the accepted-run
   list is not in the payload at all, so there is nothing for a rebuild to read
   back, and the remediation splits in two. The Turn half is the rebuild reading
   the steering key it is already handed. The Run half is a *durable read*:
   `SessionTurnManager.accepted_agent_run_ids_for_turn` resolves participants
   from the `message_deliveries` rows accepted against that Turn, so once the
   Turn id is recovered the Runs are one query away — **for Runs that have an
   accepted Delivery row**. A participant with no such row is not recoverable
   that way, and PR7R has not established whether the restored OpenCode loop
   ever has one, so the extent of the Run half is itself open.
   `HFR-205` is a characterization test asserting the current wrong behaviour;
   it is not a `PR7R-F1`/`PR7R-F2` reproducer and adds no writer, status or
   cursor to this unit. The kinds-to-verdict tie then moved Q2 to `open` on its
   own, which is the guard working as intended.

2. **A corpus guard was credited with production behaviour, in five
   artefacts.** Round 16's own fix misfiled its citation: it attributed the
   base-session lock and the interrupt-before-start to the guard that compares
   the plan's verdict words against the matrix, when the scenario that drives
   that production sequence is `HFR-195`. Neither the docstring-id guard nor the
   answer-citation guard could see it — one walks the ids a test claims about
   itself, and four of the five sites are not answers. The new rule is
   mechanical and narrow: a scenario whose test lives in the matrix file is a
   guard over PR7R's own prose, node ids and tables, so it can never be what
   makes production serialize, interrupt or emit, and no sentence may put such
   an id beside a production symbol or say it "drives" anything. Scoped to the
   **sentence**, and that scope was measured rather than assumed — per prose
   unit the rule fires 14 times, of which 9 are legitimate round narrations
   listing which guards changed; per sentence it fires 5 times and all 5 are
   round 16's defect. Crediting one *probe* with another probe's behaviour stays
   unguarded and is named here rather than papered over.

3. **Q3's per-Run detector could only see one shape.** The assertion carrying
   Q3's verdict was a top-level comprehension looking for a dict whose keys
   intersect the accepted run ids, above a comment claiming any future per-Run
   field "would fail here". It would not: a list of `{"run_id": …}` records, a
   map nested one level down, or a scalar naming a single Run all pass it. It
   now walks the projection recursively under two rules — an accepted id
   appearing as a key or a string leaf anywhere outside the flat list, and a key
   whose *shape* names a Run at all — and five positive controls pin each shape,
   because a detector whose emptiness carries a verdict has to be shown firing.

4. **Q4's established facts are Run-scoped, and the answer claimed Turn
   scope.** Q4 asks what a **Turn** carries before it goes terminal. Every test
   it cites registers its activity with a run id, no turn id, and the
   turn-completion flag off, so what is established is that a *Run* carries the
   evidence. The answer had even written the conflation down as though it were
   the reconciliation of an apparent contradiction. Two mechanisms, on purpose:
   the retraction ledger holds the sentence so the wording cannot reappear
   anywhere in the corpus, and `HFR-206` holds the evidence, reading the cited
   tests for what scope they bind so the claim cannot be re-established by
   leaving the prose alone.

**The durable lesson of the round is findings 1 and 4 having the same
skeleton.** Both are a claim about a *level* — a lane, a Turn — resting on
evidence gathered one level down, and in both cases the answer's own prose said
so and was read as a caveat instead of as the defect. Round 16's lesson was that
enrolling a retraction means sweeping for the claim's subject. Round 17's is one
step earlier: **when an answer explains why its evidence does not quite match
its question, that is not a scope note, it is the finding.**

Round 18 — three findings, all three accepted, none rejected; two new scenarios
(`HFR-207`, `HFR-208`), no verdict word moved, and **no budget move**. All three
landed on round 17's own output, which by now is the expected shape of a round
rather than a remark about one.

1. **A size claim was standing in for a completeness claim.** Round 17 closed
   its Q2 finding by saying the remediation was "one line at the rebuild rather
   than a persistence change"; that is retracted. It holds for the Turn, whose
   identity the snapshot already carries under the native steering key, and
   fails for the Runs, because the snapshot carries no run id in any form — the
   round's own characterization test asserts exactly that. The sweep then found
   that the Run half is not a persistence change either, for a different reason:
   `accepted_agent_run_ids_for_turn` resolves participants from the Deliveries
   accepted against a recovered Turn, so once the Turn id is back the Runs are
   one durable read away. The reach of that read is the new open question. A
   participant with no accepted Delivery row is outside the answer, and whether
   the restored OpenCode loop ever has one is not established here. Seven corpus
   sites carried the retracted claim in two different wordings, and both are
   enrolled separately, because a phrase ledger matches text and not meaning.

2. **The plan's canonical Q4 verdict still framed the question so that
   Run-scoped evidence answered it.** Round 17 retracted the conflation in the
   answer and enrolled one wording of it; the plan bullet said the same thing in
   its own words and was left standing — the seventh instance of the class round
   16 named. Two things changed. The bullet now states the scope, and the plan's
   wording is enrolled beside the answer's. Because a ledger can only forbid a
   phrasing and never require the right one to appear, `HFR-208` is its
   complement: when an answer restricts itself with a scope qualifier, the
   plan's verdict bullet for that question must carry the same qualifier. The
   word-level tie that predates it cannot see this class at all, since both
   documents agree on the verdict word and disagree only about what the word is
   a verdict on.

3. **A validator fixed the level it was standing on and left the level above
   it.** The matrix expansion reads its per-trigger overrides with a defaulting
   lookup, so an override filed under a mistyped lane or trigger is dropped
   before the matrix exists: every cell stays well-formed, the budget does not
   move, and the evidence someone wrote under that key is simply gone. Round
   11's validator whitelists cell keys, but it walks the *built* matrix and so
   structurally cannot see this — its own docstring says the hole exists one
   level up. `HFR-207` closes it, and it demonstrates the gap before asserting
   the fix: the first half hands the corrupted input to the old validator and
   shows it accepting, the second half shows the new one raising on the key that
   is actually wrong, naming the lane, the trigger and the outcome separately.

Also fixed, cited by nobody: the summary table near the top of this document
carried a stale scenario range and a stale Q2 verdict. It sits *outside* the
span the prose guards read — that span begins at this status section — so
nothing mechanical reaches it, which is worth knowing before a later round
trusts it.

**The durable lesson is that all three findings are one move.** Each is a
correct fix that stopped at the edge of the artefact in front of it: a
retraction enrolled in the answer but not swept into the plan, a validator
written at the level it happened to be reading, a claim verified for one of its
two halves. The habit that catches this is asking, after every fix, *which other
artefact states the same thing, and which level does this rule really live at.*

Round 19 — one finding, accepted, none rejected; no new scenario, no verdict
move, and **no budget move**.

1. **Q3's per-Run detector was blind to a vector keyed by position, which is
   the cheapest shape anyone would actually add.** Round 17 broadened this
   helper from a top-level intersection to a recursive walk under two rules, a
   mention rule and a key-shape rule, and said so as a closed count. Both rules
   need the data to *carry* a run id. A sibling list —
   `{"accepted_agent_run_ids": ["run-a", "run-b"], "accepted_agent_run_sources":
   ["scheduler", "manual_cli"]}` — carries none: it is aligned to the id list by
   index, and its key is not run-shaped. Per-Run provenance would sit one key
   away from the flat list and the search would report the projection clean,
   which matters more here than it looks, because Q3's verdict rests on that
   search coming back empty. Two rules added. A **stem** rule, which catches any
   key filed under the flat list's own stem regardless of what it holds; and a
   **positional** rule, a scalar sequence at any depth as long as the accepted
   list. The positional rule is deliberately over-inclusive — for a detector
   whose emptiness carries a verdict, a false positive costs one look and a
   false negative costs the verdict — and its limit is stated *and asserted*: it
   is inert for a single accepted Run, since "aligned to a one-element list" is
   not a distinguishable shape, and the stem rule is what covers that case.
   Round 17's count is retracted and enrolled. The verdict itself does not move:
   the projection really does hold one Turn-level label and a flat list.

**The lesson is narrow and worth writing down anyway: do not publish a count.**
This is the third consecutive round in which this one helper was found blind to
a shape, and each of its rules was written from the example in front of it —
which is unavoidable. What was avoidable is the sentence that said "two ways"
and told the next reader the enumeration was closed. A rule plus the shape it
was derived from invites the next shape; a total does not.

Round 20 — five findings, **all five accepted, none rejected**; two new
scenarios (`HFR-209`, `HFR-210`), no verdict move, and **no budget move**.

1. **A probe claimed a suspension and proved a source string.** `HFR-180`'s
   teardown probe justified the window PR7R-F1 needs by asserting that
   `get_or_create_claude_session` contains an `await` and calling the yield
   "unconditional and unbounded". An `await` is not a suspension point, and an
   uncontended `asyncio.Lock` acquires on a fast path that never returns to the
   event loop, so on a quiet runtime the resolver completes in one scheduler
   step and the window is not there. The claim is retracted and enrolled. The
   probe now *drives* the real method under a live loop in both states:
   uncontended, it must finish within one scheduler step; with the generation
   lock held, a second turn must still be parked after eight. Both directions
   are asserted, because either alone is satisfied by a method that never runs.
   Contention, not the `await`, is what opens the window — the finding stands
   for a better reason than the one it was filed with.

2. **`_collectible_class` read `__test__` off the class body; pytest reads an
   attribute.** `getattr(obj, "__test__", True)` walks the MRO, so
   `class TestChild(MutedBase)` is silently collected by nobody while a citation
   naming it resolves green — which is the exact failure `HFR-198` exists to
   prevent, one inheritance hop out. Resolved through locally defined bases in
   declaration order, the same boundary `_unittest_ancestry` already draws. Four
   fixture classes, not one, because the narrow fix is wrong three further ways:
   the base may be several hops up, an inherited opt-**in** admits a class whose
   name says nothing, and a child's own flag outranks what it inherited. All
   four are checked against this repository's actual pytest, not against a
   model of it.

3. **The opening status banner still advertised the old range** — stale in the
   same commit, in the same words, two dozen lines above the summary table that
   round 18 had just corrected. Third round for this shape, so the remedy is not
   a fourth edit: `HFR-209` makes the range a derived fact. Every line claiming
   current occupancy must state exactly the largest id the catalog gives this
   unit, and the reserved tail must start one past it. Equality in both
   directions — a range wider than the evidence hands the next unit a collision,
   a narrower one loses evidence. Historical and hypothetical recitals of the
   span are deliberately excluded; rewriting those to match today would destroy
   the record that motivated `HFR-194`.

4. **The capability index reached neither evidence module.** `INDEX.yaml` is the
   entry point and says so in its first line; thirty-one catalog rows advertised
   coverage a reader following that path could not get to. Both modules
   registered, and `HFR-210` written for the whole capability rather than for
   this unit's two files — a rule that names its own author is the degenerate
   shape this unit keeps catching itself in. Doing that surfaced twenty-two
   older modules in the same condition. That gap is real and is not this
   branch's to close, so it is pinned exactly in `_INDEX_DEBT` rather than
   waived: nothing may join it, and an entry that gets registered must be
   deleted from it in the same commit.

5. **The canonical record still cleared the inactivity timeout.** Round 17 added
   the live-dispatch qualifier to `HFR-OBS-024`'s opening and left its
   concluding sentence saying the all-or-nothing rule was satisfied and the
   timeout no longer blocked — while the final Q2 verdict and `HFR-205` hold
   both opencode cells defective across a restart. All-or-nothing means all. The
   conclusion now says unsatisfied, names the restart as the reason, and counts
   the remaining work as two items rather than one. The released form is
   enrolled by its *consequence* rather than its hedge, because the sentence an
   implementer would act on is the one that has to stay unsayable.

**The durable lesson is that a qualifier does not travel.** Round 17 narrowed a
paragraph's opening and round 18 narrowed a verdict word, and in both cases the
sentence the reader actually acts on — the conclusion, the banner — kept the
unqualified form for three rounds. Findings three, four and five are one shape:
a fact restated in a second artefact, corrected in the first. Two of them became
mechanical rules here rather than edits, which is the only move that has ever
converged on this in twenty rounds.

Round 21 — three findings, **all three accepted, none rejected**; no new
scenario, no verdict move, and **no budget move**.

1. **A hand-held lock is not a reachability proof.** Round 20 replaced a
   source-text claim with a real drive, and then supplied the contention
   itself: the test acquired the generation lock and watched the resolver
   block. That shows the resolver *can* be blocked, not that anything in
   production blocks it — so `HFR-180` could still be advertising an
   unreachable defect. Production has three owners of that key's lock: session
   resolution, `cleanup_session`, and the idle-reclamation sweep. The middle
   one is where End's chain terminates, hop for hop, which this probe already
   asserted independently. So the holder is now **End's own teardown** and the
   parked caller is the real resolver — both sides acquire and release through
   production, and only the two innermost bodies are stubbed. Counter-checked
   against production rather than against the test: remove the lock from
   `cleanup_session` and the probe fails, which is the correct failure, because
   the reachability argument would have gone with it.

2. **The constructor rule was still reading one class body.** Round 20 fixed
   `__test__` to resolve through the MRO and left the `__init__`/`__new__`
   refusal reading `node.body` — in the same function, four lines below the
   line it was editing. pytest refuses a `Test*` class that merely *inherits* a
   constructor, several hops up, and names it in the warning. Resolved through
   the same local ancestry the other two predicates use, with three inherited
   cases and one control class that has a local base and no constructor, so the
   rule cannot degenerate into "any subclass". All four checked against this
   repository's real pytest.

3. **`ours <= occupied` let an interior id vanish in silence.** Delete a guard
   and its catalog row together and the subset holds, the highest id is
   unchanged, the reserved tail is unchanged, and `HFR-192`'s bidirectional tie
   is satisfied because both sides went at once — while the plan keeps claiming
   the whole span occupied with nothing under one of its ids. Now an equality,
   which pins every allocated id individually. A range is a claim about each of
   its members, not about its endpoints.

**The durable lesson is that a fix inherits the blind spot of the thing it
fixes.** All three findings are the previous round's own work, one step short:
round 20 replaced an argument with a drive and then wrote the drive's
precondition by hand; round 20 resolved one attribute through the MRO and left
its neighbour reading the body; rounds 18 and 20 made the id range derived at
its endpoints and left its interior unchecked. The question that catches this
is not *did I fix it* but *what did I have to assume to make the fix pass, and
who is supplying that assumption.*

Round 22 — two findings, **both accepted, none rejected**; one new scenario
(`HFR-211`), no verdict move, and **no budget move**.

1. **A justification copied onto a different shape inverted its own error
   direction.** `_unittest_ancestry` stops at locally-defined bases and says
   why: an unresolved base means *not* collectible, so a false rejection is
   loud and a false acceptance impossible. Rounds 20 and 21 pasted that
   sentence onto `_resolved_test_flag` and `_defines_constructor`, where the
   arrow points the other way — an unresolved base carrying `__test__ = False`
   answers `None` and falls through to the name rule, and one carrying
   `__init__` answers `False`; both **accept**, and pytest collects neither.
   Checked against this repository's pytest rather than reasoned: the opt-out
   case is dropped without even a warning. Bases now resolve across absolute
   imports, including one re-export hop and dotted `module.Class` spellings,
   and a base that still resolves to nothing **raises** — asked only on the
   accepting side, because undecidable is not the same as collectible.

2. **Two fixtures agreeing separately prove no interleaving.** Round 21 made
   the lock holder real and left the two halves of `HFR-180`'s window in
   different fixtures: the parked caller and the End that read it were never
   the same handler in the same loop, and the live set End consulted was an
   empty literal — the answer, typed in. Now one handler carries both, the
   parked caller is real `ClaudeAgent.handle_message` with nothing stubbed
   between it and the generation lock, End runs in the same event loop while
   that turn is suspended, and `claude_active_sessions` **is** the set
   `mark_session_active` writes to, so `idle` is a consequence rather than a
   premise. Counter-checked twice against production: drop the `async with`
   from `cleanup_session`, or stamp the turn active before resolving it, and
   the probe fails with the message that says the window is closed.

**The durable lesson is that a justification does not travel with the shape it
is pasted onto.** Round 21's own lesson — a fix inherits the blind spot of the
thing it fixes — applied to a *comment*: the sentence stayed true of its
original function and became exactly backwards two lines down, which is why the
boundary was producing the silent direction it claimed to rule out. Re-derive
the error direction at every site that quotes a reason; a reason is not a
label.

Round 23 — two findings, **both accepted, none rejected**; **no new scenario
id** (both strengthen rows this unit already owns, `HFR-180` and `HFR-197`), no
verdict move, and **no budget move**.

1. **A retraction overshot, and three rounds chased the wrong thing.**
   `HFR-180`'s window needs an accepted turn suspended inside session
   resolution while End tears the runtime down. Round 4 argued it from source
   and wrote *"the yield is unconditional and unbounded"*, which round 20
   retracted because it states a false **sufficient** condition. Round 23
   retracts the replacement too: *"the window is CONTENTION on the generation
   lock"* is a false **necessary** one, and rounds
   21 and 22 then spent themselves looking for a better contender. There is no
   good one: `_cleanup_session_locked` pops the client out of `claude_sessions`
   **synchronously, before its first `await`**, so in a production run where
   cleanup owns the lock there is no live generation left for `_end_claude` to
   tear down, and the client the probe saw retained was the stub's own. The
   contender is therefore removed rather than replaced. The turn now parks on
   the resolver's *own* suspension — the warm-reuse path awaits
   `_set_claude_model_if_needed` on the cached client, an IPC round trip to a
   CLI that never answers — with the client still registered and the turn
   unstamped, and **no second turn anywhere**. Real
   `get_or_create_claude_session`, real lock, real
   `_get_or_create_claude_session_locked`, real
   `_reuse_cached_claude_session_if_available`; the only double left in the
   resolution path is the SDK client's `set_model`, which is a hung backend —
   the defect class this unit exists for. An AST guard pins the reason cleanup
   was retired: if that `pop` ever moves after an `await`, the probe says so.

2. **The substitution had migrated outward, not disappeared.** `HFR-197`'s part
   (7) built real IM-scoped Delivery and Run rows through the real
   claim/bind/materialize path and then read them by handing a payload *it
   wrote itself* to a private derivation helper. Both ends real, the span
   between them assumed — so a regression that dropped attribution anywhere
   between direct-IM admission and backend emission stayed green. The span is
   now driven: real `AgentService.handle_message` on an IM request carrying the
   durable lane's `turn-im`, real `ClaudeAgent._adopt_pending_turn_token` onto
   a reused emit context that arrives holding the **other** lane's Runs, then
   real `_record_agent_run_terminal_result` against a real
   `SQLiteBackgroundTaskStore` on those rows. After adoption the in-context
   source is empty, so the durable lookup is the only attribution left, and the
   proof is the far end: `run-im1` reaches `succeeded` carrying this emit's text
   and message id while the Workbench lane's four Runs stay `running`.
   Counter-checked three ways against production — stop merging the durable
   ids, let admission clobber a preset `turn_token`, or make adoption merge
   instead of replace.

**The durable lesson is that a stub under pressure migrates outward instead of
disappearing.** Round 9 stubbed the store; round 10 built the rows and stubbed
the lane; round 12 built the lane and stubbed the *caller* — each fix one layer
further from the claim and one layer harder to see, and each round's commit
read as though the claim were now proven. Drive the caller production actually
runs, not the helper it happens to call. And when you retract a claim,
re-derive its condition from production rather than inverting the sentence you
are retracting: round 20 replaced a false sufficient condition with a false
necessary one, and cost three rounds.

Round 24 — one finding, **accepted, none rejected**; **no new scenario id**
(it strengthens HFR-197, which this unit already owns, so the span stays
`HFR-180…211` and `UNPROVEN_BUDGET` stays 168).

1. **Round 23's own rule, applied to round 23.** Its HFR-197 fix drove
   admission, adoption, the recorder and the store — and stopped one call
   short of production's entry point, invoking the *private*
   `_record_agent_run_terminal_result` on an emit context the test had
   fabricated and then adopted onto. No Claude, Codex or OpenCode output ever
   traversed its own emission or the public dispatcher path, so two
   regressions the Q2 cells rest on stayed invisible: a backend that stops
   forwarding the Turn context onto its terminal emit, and a dispatcher that
   stops invoking the recorder on the visible-delivery lane. The probe now
   starts where production starts — real `BaseAgent.emit_result_message` on a
   real `ClaudeAgent`, through `controller.emit_agent_message` (the delegation
   `core/controller.py` performs) into a real
   `ConsolidatedMessageDispatcher`, with the real `SessionTurnManager`, the
   real admitting `AgentService` behind the runtime turn gate, and the real
   `SQLiteBackgroundTaskStore` on the IM-scoped rows. Only the **IM surface**
   is substituted: a client that accepts a send and returns a platform id, and
   the settings lookup that decides visibility. That is the boundary this unit
   is not about, and the next thing further out is the network. Driving the
   public path also buys two properties the recorder could not reach, and both
   are asserted — the settled Run carries the message id the **platform**
   returned rather than a literal the test chose, and the runtime turn gate is
   really consulted and then really released by the terminal emit. The gate
   assertion is preceded by a check that *both* runtime fields are present,
   because `emit_matches_runtime_turn` **falls open** when either is missing
   and a bare "it returned True" would score the same for an adoption that
   dropped the token and skipped the gate. Counter-checked four ways against
   production: stop invoking the recorder on the visible lane, emit a context
   with the Turn token stripped, stop releasing the runtime gate, stop
   carrying the runtime token through adoption.

**The durable lesson is that a rule about substitution applies first to the
round that states it.** Round 23 wrote *"a stub under pressure migrates
outward instead of disappearing"* and its own fix was that migration one step
further — private helper instead of private lane. The lesson was true and the
commit recording it was an instance of what it warned against, because the
rule was aimed backward at rounds 9 through 12 and never turned on the diff in
hand. Before writing a lesson down, apply it once to the fix you just made and
ask whether the layer you stopped at is the entry point production uses or
merely the next one inward.

Round 25 — three findings, **all accepted, none rejected**; **no new scenario
id** (each strengthens a row this unit already owns — `HFR-198`, `HFR-197` and
`HFR-180` — so the span stays `HFR-180…211` and `UNPROVEN_BUDGET` stays 168).

1. **A runtime lookup was mirrored without its order.** `_resolved_test_flag`
   walked the bases depth-first and returned the first `__test__` it reached;
   `getattr` reads the **C3 linearization**. The two diverge the moment two
   bases share an ancestor, and they diverge in *both* directions — checked
   against this repo's pytest rather than reasoned. `TestMroOptOut(MroLeft,
   MroRight)` inherits the flag true through the left base while Python
   resolves `MroRight`'s `False` first, and pytest collects nothing from it
   **without a warning**; `TestMroOptIn` is the mirror, dropped by the walk and
   collected by pytest. Both readers of the predicate — the discovery walk and
   the citation resolver — could therefore advertise a scenario test that never
   executes. The resolver now linearizes. `_defines_constructor` deliberately
   does **not**: it mirrors `hasinit`/`hasnew`, which ask whether *any* class in
   the MRO supplies the attribute, and an existential over a fixed set cannot
   depend on visit order. Saying which of the two a site is doing is half the
   fix — "apply it to the sibling too" is the tempting over-correction, and
   round 21 already recorded how a justification travels onto a shape it does
   not fit.

2. **Round 24's rule, turned on round 24.** `HFR-197`'s part (7b) reached the
   backend's own emitter and still bridged admission to emission by calling
   `_adopt_pending_turn_token` **by hand**, so a receiver that stopped selecting
   and adopting the FIFO-matched pending request before emitting would have left
   the row green. It now drives one terminal frame through a real
   `ClaudeAgent`'s real `_receive_messages` — real `_pop_pending_request`, real
   adoption, real `emit_result_message`, real dispatcher, real store —
   substituting the **SDK stream** and the **IM surface** and nothing else.
   Those two are *processes*, not functions, which is the first time this
   boundary has had an argument behind it rather than a preference.

3. **The doubles nobody was reviewing.** `HFR-180`'s parked caller got more real
   in rounds 21, 22 and 23 while End stayed an `_AsyncFlag`, and each report
   quoted that flag's payload as production's answer. It is not:
   `end_runtime_session` passes no `runtime_lock_held`, so
   `_cleanup_runtime_session` resolves `cleanup_session`, which re-acquires the
   generation lock the parked resolver is still holding. The probe now runs that
   real `cleanup_session` against that real parked resolver and asserts it never
   reaches the locked body, alongside the source fact that makes it the branch
   production takes. The `ended` payload survives as a statement about the
   **route** only — and that much is genuinely production's, because
   `end_running_agent` recomputes the live state and branches on it *before* it
   awaits any End, so the skipped canonical stop cannot have come from the
   double.

Counter-checked five ways against production, each restored after: restore the
depth-first walk (both new cases invert); delete the adopt call in the receiver;
skip the recorder on the visible lane; drop the runtime gate release; take the
generation lock out of `cleanup_session`, and separately have
`end_runtime_session` pass `runtime_lock_held=True`.

**The durable lesson is that a fixture gets more real in the place under review
and stays exactly as fake everywhere else.** Three rounds running, this unit
replaced the one double a reviewer pointed at and then read the *remaining*
doubles as findings. When a probe is made real, enumerate what is still
substituted and check the report against that list — the substitution nobody is
looking at is the one the conclusion ends up quoting.

Round 26 — three findings, **all accepted, none rejected**; **no new scenario
id** and no 168-cell budget change.

1. Computed `__test__` assignments are now undecidable rather than absent: the
   citation/discovery guard fails loudly instead of falling through to a name
   rule that can advertise a node pytest skips.
2. `HFR-197` is narrowed to the integrated Claude receiver path it drives.
   Codex/direct-IM moves `covered` → `unproven` until a real
   `CodexEventHandler` emit carries the accepted Run through the dispatcher.
3. `HFR-180` is narrowed to the production fact its one interleaving reaches:
   the unstamped live turn reads idle, the canonical stop is skipped, and real
   teardown blocks behind the parked resolver. The separate teardown-marker and
   classifier run is removed; their eventual result is not claimed.

The durable lesson is to narrow at the first un-driven boundary. A truthful open
cell is evidence; an adjacent green test is not.

Question verdicts:

1. **Q1 — open, do not close the claim yet.** On the durable Workbench lane,
   driven against real rows in `HFR-199`: enqueue, Delivery reservation, owner
   transfer, batch claim, native bind and materialized start acceptance each
   leave the Run nonterminal — and so does terminalizing the **Turn**. Only
   `settle_agent_runs_for_turn_in_connection` moves it, which is broader than the
   claim round 9 made and has a corollary worth carrying into Q4: a path that ends
   a Turn without calling it leaves a live Run with no owner. A result-less
   failure is not laundered into an interruption. Round 10 corrected the *basis*
   of the first clause rather than the clause itself — it used to rest on a
   scheduler test that stubs `submit_scheduled` and merely reports
   `queue_persisted` / `delivery_owner_transferred`, so a store that settled the
   Run on reserve would have left it green; that test is still cited, for the
   scheduler decision it does prove. The direct-IM lane is still not established:
   the driven chain is durable, so a premature terminal transition in the IM
   reservation or backend-acceptance path would leave it green. The
   premature-success claim may be closed once an IM-lane acceptance-boundary probe
   exists — not before.
2. **Q2 — open. Claude on both lanes and Codex on the durable lane have covered
   live attribution; Codex/direct-IM is unproven, and OpenCode loses attribution
   across restart.** Everything in the rest of this entry describes live
   dispatch; the Codex evidence boundary and restart hole are at the end.
   Codex's `item/*` notifications carry a
   `turnId` and `_find_request_for_notification` resolves the participating
   Run's request from it through `get_request_for_turn`, a per-turn map.
   OpenCode's `run_prompt_poll` receives the exact `AgentRequest` and every
   progress emit passes `request.context`, whose `turn_token` `_process_message`
   has already read as `logical_turn_id`. Claude's `_adopt_pending_turn_token`
   copies the FIFO-head pending request's `turn_token` and attribution keys onto
   the long-lived receiver context before every assistant and tool emit. And the
   direct-IM lane is not the exception four earlier drafts made it: `turn_token`
   is stamped for **every** request on **both** lanes by
   `AgentService._stamp_runtime_turn`, which `handle_message` calls before the
   backend is invoked. The four rounds that said otherwise had grepped the
   literal string and missed the constant-keyed write — see §7's round-6
   paragraph; the durable form is that a "nothing writes X" claim is only as good
   as the search behind it. Three things remain. First, the activity timestamp
   is the one true remediation item: it is stamped per **session** on
   every backend, so an exact attribution is aggregated away at the last step;
   that is what an inactivity timeout has to change. Second, claude resolves by
   **FIFO position** among pending requests rather than by an id the event
   carries — exact only while per-key serialization holds, which
   `HFR-191` drives — so the remediation must build on a weaker mechanism there
   than on codex. Third, the "and participating Runs" half of the
   question, unanswered until round 9 while the verdict was written as though it
   were settled: Run attribution at emit time is **derived** from Turn
   attribution — `_owned_agent_run_ids` reads `accepted_agent_run_ids` off the
   emit context and `_durable_accepted_agent_run_ids` looks Runs up per
   `turn_token` — so it is exactly as exact as the Turn. `HFR-197` drives the
   integrated Claude receiver path; Codex/direct-IM remains open because no probe
   drives `CodexEventHandler` through the dispatcher with the accepted Run.
   Retracted in round 9: codex does **not** discard its Turn signal
   at `should_emit_progress`. That claim was argued three times from the two ends
   of a mechanism without ever running the code between them; `handle_message`
   interrupts the active turn under a base-session lock before starting the next,
   so the mute follows an interrupt (`HFR-195`). Narrowed in round 10: the two
   turns are *serialized at the lock*, not non-overlapping on the backend —
   production skips the interrupted-completion wait its own protocol note
   specifies — and what makes the window harmless is that both of its late
   arrivals are handled, which `HFR-195` now drives through the real event
   handler. Reopened in round 17, and it is the fifth instance of the same
   reading error the paragraph above is a history of: every clause here walks a
   LIVE path, and OpenCode's restart path — `run_restored_poll_loop` — was
   never walked. It rebuilds its emit context through
   `ProcessingIndicatorHandle.from_snapshot`, whose rebuild is a fixed
   three-key allowlist, so `turn_token`, the runtime turn token and
   `accepted_agent_run_ids` are dropped and all three of its emits carry the
   stripped context; the module mentions neither `turn_token` nor
   `logical_turn_id` anywhere. Both halves of the question fail there, Turn and
   Runs, on both lanes — the discard is in the shared rebuild and reads no
   platform. It is a **defect rather than a gap** because the identity is
   persisted: `_process_message` writes `logical_turn_id` into the very dict the
   rebuild is handed, under the native steering key, and the restore path reads
   it back to steer. `additional_steer_targets` building restored targets with
   `context=None` is production saying the same thing. Round 18 retracted the
   sentence that used to close this bullet — "Remediation is one line at the
   rebuild" — because the halves need different fixes: the Turn comes back from
   the steering key the rebuild is already handed, while the Runs are not in the
   snapshot at all and come back only from a durable read
   (`accepted_agent_run_ids_for_turn`, over the Deliveries accepted against that
   Turn), which reaches a participant only if it has such a Delivery row. It is
   a second timeout item on top of the per-session timestamp: `HFR-205` drives
   it.
3. **Q3 — open, split, and narrower than it was.** Established: the *Turn's*
   accepted-run record cannot discriminate between participants — a flat
   `accepted_agent_run_ids` list and one Turn-level `source_kind` that a later
   participant does *not* restamp, with no per-Run source or deadline anywhere
   in the projection. "Anywhere" is only as strong as the search behind it, and
   that search has been widened in each of the last two rounds — round 17 for
   nested and record shapes, round 19 for a sibling vector keyed by position,
   which carries no run id and so was invisible to both of round 17's rules.
   The verdict did not move either time: the projection really does hold one
   Turn-level label and a flat list. Which participant originally stamps that label is not
   reached — `HFR-182` preloads it and drives only the append path — and this
   sentence went on claiming the first participant did until round 15. That is a statement about the projection and must not be read
   as one about the system: each accepted id is the primary key of an
   `agent_runs` row carrying `source_kind`, `source_actor` and `definition_id`,
   and `run_definitions` carries `timeout_seconds` / `lifetime_timeout_seconds`.
   The inputs exist; they are one join away and absent from the projection, so
   the open question is whether the cancellation site performs that join — not,
   as an earlier draft had it, that per-Run timeout policy is unspecifiable.
   Also not established: whether a cron Run and a manual CLI Run actually
   coalesce. `_attach_accepted_agent_runs` is downstream of that decision; the
   owner is `SessionTurnManager._hydrate_delivery_batch_context`, which folds a
   Delivery batch into one context.
4. **Q4 — open, Claude only, RUN-scoped, and two of the four facts are
   unproven.** Every Turn-level pre-terminal fact remains **open**, and that
   is the load-bearing word in this bullet: what is established is established
   about a *Run*. Every activity the answer cites registers with a `run_id`,
   none passes a `turn_id`, and none sets `completes_turn` — `HFR-206` reads
   the citations for exactly that and fails if one starts binding a Turn. Round
   18 retracted this bullet's closing sentence, "Q4 asks whether a pre-terminal
   fact is durably recorded", which framed the question so that Run-level
   evidence answered it. A follow-up implementing this verdict may not treat
   Turn-level pre-terminal evidence as proven. Four
   pre-terminal facts are named and two are established, both for Claude and
   both at Run scope: the
   durable pending-output Activity batch and the `activity_local_settlement_only`
   marker. The terminal-result latch and the accepted Message receipt are not:
   no cited node invokes `SessionTurnManager.on_terminal_result` or reads
   `_avibe_terminal_result_latch`, so removing the latch would leave every test
   named here green. The latch is also in-process context state that
   `on_terminal_delivery_complete` pops, not a durable row, so calling it a
   durable pre-terminal fact was wrong on two counts. Both established facts
   rest on the Activity output batch, so for codex and opencode there is no
   proven pre-terminal evidence at all — the stronger form of Q2's gap, since an
   inactivity decision on those backends would have nothing to outrank it.
   Q4 calling two facts *established* while every matrix cell is `unproven` is
   not a contradiction, and round 18 corrected how this bullet used to explain
   why: Q4 asks whether one named pre-terminal fact is durably recorded **for a
   Run**, a cell asks whether a Run admitted through a given trigger reaches a
   given terminal outcome, and a test may settle one and not the other. Neither
   of them settles the Turn.
5. **Q5 — open, and it splits.** `health`, `consecutive_failures` and
   `recent_failures` are answered: derived per read by
   `SQLiteBackgroundTaskStore._classify_health` over the bounded verdict window
   `_health_rows` collects, rather than stored counters, so a failure ages out of
   the window on its own and a success downgrades `failing` to `degraded`
   instead of erasing history. No health cursor is needed for those three.
   `last_run_at` and `last_error` are **not** written in the Run's terminal CAS,
   as this block said through round 5. They are a definition-level stamp that
   `store.mark_task_result` commits inside `_execute_task`; the Run's terminal
   CAS is `request_store.complete` in `_execute_claimed_request`'s `finally`, a
   second write afterwards. `HFR-264` reconciles a refused stamp — `HFR-261` is
   the definition-write CAS that refuses it, `HFR-264` is what turns the refusal
   into a failed Run rather than a green one — and nothing reconciles process
   loss in the gap, so a definition can advertise a `last_run_at` for a Run that
   never settled. Those two fields are stored, not
   derived, so unlike the trio they can drift: they project an *attempt*, not
   settled Run history.
Reproduced defects, both on the direct-IM / agent-run lane and both owned by
`core/services/running_agents.py`:

- **PR7R-F1** (`HFR-180`) — `_resolve_live_state` reads
  `claude_active_sessions`, which `handle_message` stamps only after
  `get_or_create_claude_session` returns. A Run accepted while that call is in
  flight reads `idle` and End takes the idle branch, so `handle_stop` — the only
  path that emits `stopped` → `canceled`, which invariant 2 requires for a user
  Stop — never runs. The real teardown then blocks behind the parked resolver's
  generation lock. The probe does not release that same End task through
  teardown and classification, so it deliberately makes no claim about the
  eventual teardown marker or classifier result. Which terminal status the IM Run
  ends with is deliberately not claimed — `SETTLED_BY_BACKEND_REFRESH` is
  emitted by `SessionTurnManager.release_for_backend_refresh` on the Workbench
  lane, and nothing connects it to an IM Run. The earlier draft of this finding
  did claim it, on the strength of two constant lookups. Round 3 changed how the
  finding is *demonstrated*, not the finding: the race window used to be
  postulated by the fixture and the intentional-teardown marker stamped by the
  probe itself, so the reproducer would have kept passing — still reporting the
  defect — the day production closed either half. The ordering is now read out
  of `handle_message`, the five-hop chain to the marking code is asserted
  (including the `getattr`-by-string dispatch in
  `_cleanup_runtime_session_state`, which no call-graph check would see), and
  the marker is produced by running the real `_cleanup_session_locked` with its
  own `client is not None or receiver_task is not None` guard applied. Round 4
  added the missing half of that: **reachability**. The staged registry state —
  a client in `claude_sessions`, the key absent from `claude_active_sessions` —
  is not a cold start (the client is registered only at the end of session
  creation) but is the ordinary **warm-idle** state, because `mark_session_idle`
  discards from `active_sessions` and deliberately keeps the client. The probe
  now drives that transition instead of asserting it in prose. The window is not
  a few instructions either: every path through
  `get_or_create_claude_session`, warm reuse included, first acquires the
  unconditional `_claude_runtime_generation_lock`, and the retry path
  additionally awaits `_wait_for_claude_receiver_cleanup`.
- **PR7R-F2** (`HFR-181`) — on the codex active branch a teardown that succeeds
  after a failed `_stop_active_agent` returns `{ok: True, action: "ended"}`.
  Clearing the stale row is deliberate and stays; the payload carrying no signal
  that the turn was never interrupted does not. What the Run receives is
  deliberately not claimed: the reproducer builds only the codex session and
  turn registries, so there is no Run row for it to observe. The earlier draft
  said the Run is never settled — inference from the missing interrupt, not
  evidence. **Round 4 relocated the defect one frame up, and shrank it.** The
  signal is not missing from the system: `_end_codex` already computes and
  returns `interrupted` alongside `process_killed`. It is discarded by
  `end_running_agent`'s failed-stop branch, which throws the teardown result away
  for a fresh `{ok: True, action: "ended", backend: "codex"}` literal and copies
  exactly one field back out. That `process_killed` survives while `interrupted`
  does not is the tell — a field the caller forgot, not information it never had
  — so the remediation is forwarding an existing value, not new plumbing. The
  reproducer runs a **live** transport twice, with `turn/interrupt` raising and
  succeeding, and shows the two payloads byte-identical to the caller. Round 5
  added the condition that makes that pair mean something: the two runs must be
  on a cwd with a **co-tenant** session, or `_end_codex` stops the shared
  app-server and the un-interrupted turn dies by process kill anyway. The
  transport's survival is asserted, and the last-session variant is kept only to
  show that `process_killed` *is* forwarded — the evidence that the caller can
  copy a teardown field and copied one of two.

Both are characterization tests: they assert current behavior, so the
implementation PR that fixes them must flip them. Neither is fixed here. Their
matrix cells are `unproven`, not `defect`: the reproducers characterize End's
behaviour and explicitly disclaim the Run half those cells ask about.

Remaining PR7R work. Round 3 collapsed the old per-area list into one shape,
because every cell now needs the same missing thing — a Run **admitted through
one of the four triggers** and followed **to its own terminal settlement**. The
ordering is by how many cells one harness closes:

1. A **trigger-admission harness**: fire each of `scheduler_cron`,
   `scheduler_at`, `manual_cli` and `watch` for real and hold on to the
   `agent_runs` row it creates. Every cell in the matrix is downstream of this,
   and none of the existing settlement tests have it — they admit through
   `enqueue_agent_run`, which is not a trigger.
2. Drive that Run to each of the seven **outcomes** on the **durable Workbench**
   lane (84 cells), reusing the existing settlement machinery now that the
   admission is real.
3. Repeat on the **direct-IM** lane (84 cells), which additionally needs an
   IM-scoped Harness Run bound to the turn — the thing PR7R-F1/F2 could not
   observe, and what settles Q1's acceptance boundary.
4. **Per-backend** overrides for codex and opencode wherever step 2 or 3 shows
   the answer differs: the pending-output and local-settlement rows are
   Claude-only in production today, which is also what Q4 turns on.
5. Q2 is closed; what it leaves behind is one remediation item. A **per-Turn
   activity timestamp** — every backend has an exact-Turn attribution and every
   backend then stamps activity per session, so the signal is aggregated away at
   the last step. The codex turn slot is **not** a second item: round 9 drove
   `handle_message` and found the interrupt that makes the base-keyed filter
   correct (`HFR-195`). What remains there is a behavioural note rather than a
   defect — a cwd change interrupts and replaces the running turn instead of
   queueing behind it, and `runtime_turn_keys_for_session_key` cannot address the
   replaced turn. Round 10 adds a second note of the same rank: the insertion path
   does not wait for `turn/completed(interrupted)` before `turn/start`, diverging
   from `docs/plans/codex-app-server-refactor.md` step 2, and it removes an
   interrupted turn's ack twice. That one is for the implementation unit, not evidence work. Still open on
   the evidence side: the **Q5 crash-gap probe** (kill between
   `mark_task_result` and `request_store.complete` and check whether anything
   reconciles the definition stamp with the Run's terminal status), the Q3
   admission drive through
   `_hydrate_delivery_batch_context`, whether the cancellation site joins
   `agent_runs`/`run_definitions`, and Q4's terminal-result latch probe.

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
complete: PR3 #1155 runtime ownership + `session_deliveries` supervisor foundation
    |
    v
complete: PR4 #1173 event-first supervised work lanes
    |
    v
NEXT: PR7R current-master evidence matrix          (evidence-only)
    |
    v
contract amendment for each reproduced defect      (documentation-only)
    |
    v
separate terminal-truth / scheduler-liveness implementation PRs
```

PR7R is a separate test/documentation review unit. It may close either old claim
without an implementation PR. A reproduced defect first receives the complete
contract amendment above; only then may it become a separate implementation
unit. PR3 and PR4 stayed separate: PR3 defined session ownership/reclamation;
PR4 replaced serial polling as the normal executor. A PR4B transport-attempt
change exists only if the current-master reproducer proves a missing durable
fact and its separate contract review passes.

Implementation ownership is intentionally narrow:

- **PR3** owned the derived SQLite snapshot, the controller-owned supervisor
  foundation, `session_deliveries` registration, the fallback-only pre-execution
  `requests` recovery registration, reclaimer consumers, and the exact
  activation/reclamation tests. It did not admit general queued Harness Runs
  or restructure the other drains.
- **PR4** registered Harness lanes, replaced the scheduled-task and managed-watch
  `_watch_store` loops, extended the existing Unix-socket event vocabulary and
  bridge use, instrumented post-commit producers, and added lane lifecycle and
  isolation tests. It did not change eviction disposition or durable owner
  schemas.
- **PR7R** remains evidence-only.

Each lane owns its files and tests; the orchestrator alone resolves shared
controller/supervisor edits and verifies one consuming test per contract before
push.

Scenario range status, verified against
`tests/scenarios/harness_failure_recovery/catalog.yaml` on 2026-08-06:

- PR3: `HFR-130…154` — occupied by #1155
- PR4: `HFR-155…179` — occupied by #1173
- PR7: `HFR-180…211` — occupied by PR7R's first increment (`HFR-188…211` were
  taken by its round-6 through round-22 reviews); `HFR-212…219` still reserved for
  the remaining probes listed in §7

Check the catalog again immediately before coding. The highest merged ID is
now `HFR-435`, allocated by unrelated capabilities above this plan's reserved
blocks. If `HFR-180…219` has been taken by then, allocate a fresh contiguous
block above the highest merged ID; never reuse a closed branch's overflow
table.

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
