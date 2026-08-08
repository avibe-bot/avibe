# Harness Run Reliability

Status (2026-08-06): **Every implementation unit through PR4 is merged. PR1,
PR2, PR5, PR6, #1139's Activity-output settlement closure, PR3 (#1155), and
PR4 (#1173) are complete. Only PR7R remains as the next unit, and it is
evidence-only. PR4's conditional transport-attempt delta (PR4B) opens only if a
current-master reproducer proves a missing durable fact.**

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
| Scheduled/watch terminal-time truth and cron liveness | **Open — PR7R**, evidence-only; `HFR-180…219` reserved and unoccupied as of 2026-08-06 |

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
- PR7: `HFR-180…219` — reserved and still unoccupied; usable by PR7R

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
