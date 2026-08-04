# Add a forced sidecar restart action to Memory settings (rev15)

> Rev15 keeps one public recovery action and a focused set of internal fixes:
> replayable configuration, config-wide write serialization, restart-specific
> and lifecycle-intent admission, generation-fenced ready activation, complete
> supervisor quiescing, bounded orphan recovery, disk/live replay convergence,
> marker-free replay promotion, store-thread settlement, worker lease rotation,
> locked clear-marker recovery, bounded readiness, supervisor callback rebinding,
> authoritative controller/UI settings refresh, queued-operation exclusion, and
> disabled/fail-closed replay handling, explicit worker activation, and retained
> processing-probe/module-store ownership with cross-lifecycle admission.
> Timed-out work remains owned until it is either joined or proven unable to
> mutate state. It does not add a lifecycle coordinator, explicit state machine,
> provider/store port, or frontend DOM test framework.

## Background and goals

When Memory reports `down` or `memory_sidecar_unavailable`, the Web UI has no
general recovery action. The current restart button appears only in the
`processing_fault_kind === 'engine'` banner. `EverOSProcess` also restarts only
after a child exits or fails to start; it does not replace a process that is
still alive while its UDS or health endpoint is unreachable.

This change has two goals:

1. When Memory is enabled, always show a "Restart engine" action outside the
   setup-stage branch. A missing runtime dependency, an unloaded status, or a
   failed status read must not hide it.
2. Make the controller stop the old sidecar and wait for a replacement to reach
   `ready`. Do not restart Avibe, reinstall the runtime, reload a restart target
   from disk, or run a processing preflight probe. If the replay snapshot still
   carries the durable embedding-change marker, run the existing root safety
   check and allow settlement to verify and clear that marker before launch.

Non-goals: automatic health-based restart, a complete Memory lifecycle rewrite,
provider/store protocol changes, retries for historical `unknown` flushes, and
a full four-platform regression.

## Why reconcile is not sufficient

- Settings PATCH and the old restart route load configuration separately. A
  C0/C1 interleaving can split persisted and live configuration.
- Reconcile can wait up to 30 seconds for worker drain and then run a processing
  probe. A long flush can take up to 300 seconds. That does not provide the
  requested "force after five seconds" behavior, and an unrelated probe failure
  can reject the recovery.
- `self._config` is not the last known-good configuration. Enabled reconcile
  assigns a candidate before root validation and replacement startup complete.
  If both the candidate and UI rollback fail, it can still point at failed C1.

Keep the existing public route, but make it call a dedicated runtime operation:

```text
UI
  -> POST /api/memory/runtime/restart
  -> internal_client.memory_restart()
  -> POST /internal/memory/restart
  -> MemoryRuntime.restart()
```

## Minimal backend design

### 1. Replayable configuration snapshot

Add private `_restart_config` and `_persisted_memory_snapshot` values to
`MemoryRuntime`:

- Initialize `_restart_config` as a deep copy of the startup `MemoryConfig`, so
  a first-start failure can still be retried with the same configuration.
- Update `_restart_config` with another deep copy only when enabled or disabled
  reconcile has completed successfully, while `_reconcile_lock` is held.
- Initialize `_persisted_memory_snapshot` from the startup config. Add a narrow
  `MemoryRuntime.reconcile_persisted()` entry point used only by
  `Controller.reconcile_memory()`: it hot-applies a freshly loaded, successfully
  saved V2 config and returns both the transport result and a deep copy of the
  exact applied `MemoryConfig` on success. The ordinary `reconcile()` keeps its
  current dict return for runtime-internal callers. After acquiring
  `_reconcile_lock`, a persisted reconcile replaces the snapshot with a deep
  copy of its candidate before live application, so the complete expected disk
  block is retained even if reconciliation later fails. Runtime-internal calls
  such as post-Clear reconcile use the default `persisted=False`; artifact
  activation uses `_reconcile_locked()`. Neither may claim a new disk snapshot.
- Never commit a failed candidate. A deep copy is required because
  `MemoryConfig`, its nested settings, and `embedding_change_pending` are
  mutable.
- `restart()` reads a deep copy while holding `_reconcile_lock`. After all
  safety checks pass and before process replacement, it restores `self._config`
  from that copy so worker callbacks and child settings cannot keep using C1.
- Before replacing the process, restart always runs one config transaction that
  freshly loads V2, requires its complete Memory block to equal
  `_persisted_memory_snapshot`, and conditionally replaces only that Memory
  block with `_restart_config`. Thus a failed persisted C1 converges to replayed
  C0 while unrelated platform/runtime fields from later generic saves remain
  intact. If disk already equals C0, the operation is an idempotent no-op. Any
  other Memory block is an unknown/newer writer; fail closed without launching a
  child rather than overwrite it. After commit, update
  `_persisted_memory_snapshot` to the exact replay block.
- A startup snapshot may have `embedding_change_pending=true`. Restart reuses
  the reconcile embedding/root guard and proceeds only after both the guard and
  the same disk-convergence transaction succeed. Existing vectors or an
  indeterminate root return `memory_clear_failed` without launching a child.
  Successful convergence clears the marker in persisted config,
  `_restart_config`, `_persisted_memory_snapshot`, and the later restored
  `self._config`, without changing any other Memory field. It does not run the
  processing probe.
- Every successful runtime-owned V2 Memory mutation returns the exact committed
  `MemoryConfig` from `mutate_v2_config()`. While `_reconcile_lock` is still
  owned, install that returned value into `_persisted_memory_snapshot`
  immediately. In particular, ordinary persisted reconcile captures the
  marker-bearing candidate first, then marker settlement returns the
  marker-free disk block and refreshes the snapshot before any later reconcile
  phase can return or fail. A failed/aborted transaction leaves the previous
  snapshot unchanged.
- Marker settlement also replaces the in-flight reconcile candidate with a deep
  copy of that exact committed marker-free block before live application
  continues. If the remaining reconcile succeeds, commit that marker-free
  candidate to both `self._config` and `_restart_config`; never reconstruct it
  from the original marker-bearing request object. If a later reconcile phase
  fails, keep the previous last-good `_restart_config` while retaining the exact
  marker-free `_persisted_memory_snapshot` for conditional disk convergence on a
  later restart.
- `Controller.reconcile_memory()` installs only the successful applied config
  returned by `reconcile_persisted()` into `Controller.config.memory`; it never
  reuses its original marker-bearing argument. The returned candidate is already
  the exact settled block, so controller shared state, runtime live/replay state,
  and disk converge without mutating an object from the settlement thread. A
  failed reconcile returns no applied candidate and leaves controller config at
  its previous value.
- Explicit restart keeps the exact config returned by its convergence
  transaction as an applied candidate even if old-process stop, replacement
  start, worker activation, or their cleanup later fails. Its internal result
  includes that candidate plus `config_committed=true`; pre-convergence failures
  and busy results return no candidate and `config_committed=false`.
  Runtime converts every ordinary post-convergence orchestration exception into
  this structured failure pair rather than letting internal-server exception
  mapping discard the candidate. `CancelledError` still propagates after
  ownership cleanup because there is no response consumer to refresh.
  `Controller.restart_memory()` installs a deep copy of every non-null applied
  candidate into `Controller.config.memory` before exposing only the transport
  result. Thus disk, Runtime, and Controller remain authoritative independently
  of the later lifecycle outcome.

These snapshots answer which configuration an explicit restart replays and
which exact Memory block it may conditionally replace on disk. They do not
redefine `_config` or introduce general configuration versioning.

### 2. Clear-marker and replacement atomicity

`_recover_interrupted_clear()` currently checks `_clear_active` before taking
the module lifecycle lock. A concurrent clear can fail in that gap, leave a
durable marker, and let restart or reconcile launch a child without rechecking.

Make the following narrow change:

- Extract `_recover_interrupted_clear_locked()`. Its caller must already hold
  `module._lifecycle_lock`; it then takes the root lifecycle lock in the existing
  order and rereads the durable marker.
- Keep `_recover_interrupted_clear()` as the wrapper used by search, profile,
  and status. It retains the active-clear fast return and the existing immediate
  failure or `clearing` behavior on read paths.
- Reconcile, artifact activation, and restart use the existing order
  `_reconcile_lock -> module._lifecycle_lock -> root lifecycle lock` and perform
  marker recovery and child replacement in one lifecycle critical section.
- The locked helper has no `_clear_active` fast return. Once the lifecycle lock
  is held, it cannot overlap an active clear and must reread the marker.

This closes the existing check/use window without creating a general lifecycle
coordinator.

### 3. Serialize settlement with every V2 config writer

`CONFIG_LOCK` is process-local: the controller's compare-and-save cannot, by
itself, exclude a Memory PATCH or an unrelated `/api/config` save running in the
UI process. Because every `V2Config.save()` replaces the complete JSON document,
Memory-only serialization is insufficient.

- Before calling `internal_client.memory_restart()`, the restart route checks a
  synchronous `_memory_settings_write_pending_count` and
  `_memory_settings_write_lock()`. Every Memory PATCH increments the counter
  before its first await and decrements it in `finally` only after its complete
  lock/save/reconcile/rollback path exits, so it covers the current owner and
  every queued writer. If the count is nonzero or the lock is held, return
  `memory_restart_busy` immediately rather than joining its waiter queue.
- If it is free, acquire it immediately and hold it across the complete internal
  restart request and response. Every Memory PATCH already holds this same lock
  across load, save, controller reconcile, and rollback, so a PATCH cannot write
  C1 between the controller's C0 comparison and marker-clear save.
- Add one synchronous
  `mutate_v2_config(mutator, *, config_path=None, initializer=None)` write API in
  `config/v2_config.py`. Normalize the selected default or custom target to one
  absolute path without resolving away its existing symlink/replace semantics.
  Preserve `V2Config.save(config_path)`'s parent-creation behavior before lock
  acquisition, then derive a deterministic per-target sibling lock path from
  that normalized config path. Its outermost call first acquires this
  path-specific cross-process file lock, using the existing
  `storage.lock.MigrationFileLock` primitive with a dedicated lock path and
  bounded acquisition, without owning `CONFIG_LOCK`. Only after the file lock is
  held does it acquire `CONFIG_LOCK`, load the current document, invoke a
  synchronous mutator on that fresh working object, validate it, and atomically
  save it before releasing both locks. This fixed file-lock-then-`CONFIG_LOCK`
  order applies to every writer; migrate any caller that currently enters
  `CONFIG_LOCK` before invoking the helper. Inline readers can therefore take
  the process-local lock briefly while another process owns the file lock,
  without freezing their async event loop behind that writer's bounded wait.
  The API never accepts a caller-supplied or preloaded `V2Config` instance.
  First-install/default paths may supply an initializer factory, which is also
  invoked only under both locks when the selected document is absent. Loading,
  initializing, token validation, and the final atomic replace all use that same
  normalized target; custom-path callers never fall back to the default config.
- Make the raw save primitive private to that API. Bind its opaque write token
  to the normalized target path, transaction session, and exact config object
  loaded inside it; a missing token, a token for another path/session, or a
  config object loaded before lock acquisition is rejected. Reentrant
  same-thread mutation calls for the same target reuse the outer session and its
  one working object, and only the outermost call publishes one final document.
  Reject a nested request for a different target before acquiring another lock;
  callers split it into a separate outer operation so lock ordering remains
  explicit.
- Migrate every whole-file V2 writer to the mutation API: the generic and Memory
  paths in `vibe/api.py`, direct writers in `vibe/ui_server.py` and
  `vibe/remote_access.py`, `SettingsHandler`, `ModelHubService`, controller and
  agent-auth flows, startup/default writers, runtime settlement, and every other
  audited former `V2Config.save()` call path. Each mutator merges only its owned
  fields into the transaction's freshly loaded working object.
- Every asynchronous caller, including controller-side IM settings, model-hub,
  agent-auth, startup/runtime, and UI/server paths, offloads the complete
  `mutate_v2_config()` call to one `asyncio.to_thread()` invocation. No async
  caller may acquire the synchronous file lock or run load/merge/save inline on
  its event loop. Awaited external I/O happens before the transaction; the
  synchronous mutator then revalidates against the freshly loaded state.
- Controller convergence uses that mutation API, compares the fresh working
  object's complete Memory block with `_persisted_memory_snapshot`, then replaces
  only that block with `_restart_config` and clears its marker when the root
  guard permits. A mismatch or bounded lock timeout fails closed with
  `memory_clear_failed`. Unrelated config writers wait before their load, so
  none can publish a stale whole-file snapshot afterward, and their non-Memory
  fields are preserved.
- Settlement runs in one retained `asyncio.to_thread()` task. A deadline uses
  `wait_for(shield(task))`, never cancellation of the thread as proof that its
  work stopped. If the deadline or caller cancellation fires, cleanup continues
  to await that exact task under `shield()` while the controller lifecycle locks
  and the UI settings transaction remain owned. Only after the task reaches a
  terminal result may either process release its transaction boundary or return
  a response. The thread works on a private config copy and performs no mutation
  outside its final compare-and-save operation.
- `internal_client.memory_restart()` owns one request task with no shorter HTTP
  read timeout. Its reporting deadline and caller-cancellation path both use
  `shield()`; after either one fires, cleanup joins the retained request before
  returning its terminal result or re-raising `CancelledError`. This prevents a
  disconnected UDS request from continuing settlement after the UI route has
  released `_memory_settings_write_lock()`.
- The internal UDS endpoint remains controller-private. The supported
  user-facing writer is the UI route that owns the cross-process transaction
  boundary.

Add route concurrency tests that pause restart convergence and start both a
Memory PATCH and an unrelated `/api/config` save. The Memory PATCH must wait on
the UI lock; the generic save must wait on the cross-process config transaction
before loading its baseline. Cover ordinary completion and an expired settlement
deadline. In the expired case, prove the request and transaction locks remain
owned until the retained thread finishes. Both writers must then preserve the
settled marker and the generic writer's unrelated C1 fields without either side
publishing a stale whole-document snapshot.
For a failed Memory C1 plus failed UI rollback, prove restart conditionally
replaces only the expected C1 Memory block with replay C0 before reporting ready;
settings reads and a fresh `V2Config.load()` must match the live runtime. An
unexpected C2 Memory block must reject restart without any disk or process
mutation.
Also load a stale C0 before another process publishes C1 and prove it cannot be
passed into the mutation API or saved with a later transaction token; the
accepted mutator must observe C1. Hold the file lock from a second process and
prove `CONFIG_LOCK` remains available to representative inline async UI and
controller readers while off-thread writers wait on that file lock; after file
lock acquisition, complete mutations still serialize and preserve both changes.

### 4. Nonqueued admission and complete deadlines

An explicit restart must not wait silently behind another lifecycle operation.
Otherwise the transport can time out first while the controller later performs
an abandoned restart, and a user retry can enqueue a second one.

- Add a synchronous, read-only `MemoryModule.lifecycle_busy` admission property
  backed by a private `_lifecycle_pending_count`. Search, profile, Clear, and the
  interrupted-clear wrapper enter a synchronous intent scope after input
  validation but before their first lifecycle await, and leave it in `finally`
  only after their complete lifecycle-lock path exits. The scope covers both the
  current owner and every queued operation, including the interval between clear
  recovery and a search/profile provider call. Nested locked helpers reuse the
  outer intent; no code reads private `asyncio.Lock` waiter state.
- Add a synchronous `_reconcile_pending_count` to `MemoryRuntime`. Every
  non-restart operation that can acquire `_reconcile_lock`, including persisted
  and runtime-internal reconcile and artifact activation, increments it before
  its first await and decrements it in `finally` only after its entire public
  operation exits, including artifact installation's unlocked `ensure()`
  interval. Nested locked helpers reuse the outer intent.
- Before any await, `restart()` checks `_clear_pending_count`,
  `_reconcile_pending_count`, `module.lifecycle_busy`, `_reconcile_lock`, and
  `module._lifecycle_lock`. If a complete Runtime Clear, reconcile, artifact
  activation, or module lifecycle operation is active/queued, or either lock is
  held, return
  `{ok: false, error: 'memory_restart_busy'}` without changing the process,
  claims, or worker.
- Add a narrow `_retained_ownership_active` gate to `MemoryRuntime`. Any worker,
  worker/module store task, processing probe, supervisor start, watcher/monitor,
  or process cleanup owner that outlives its bound sets this gate synchronously
  before lifecycle locks or `_explicit_restart_active` can be released. Clear,
  reconcile, artifact activation, search/profile and interrupted-clear recovery,
  and automatic ready activation check it before their first await and reject
  without entering lifecycle queues or mutating state. Pure status projection
  may report the retained error but cannot run recovery. `close()` joins retained
  ownership; only an explicit restart retry may enter the locked settlement path
  while the gate is set. That retry clears the sticky gate atomically only after
  every retained registry is empty and before normal replacement continues.
- When both are free, acquire them in the established order in the same event
  loop turn, with no intervening await. After acquiring `_reconcile_lock`, check
  `_artifact_installing` while that lock is owned. If installation is active,
  release the lock and return `memory_restart_busy` before resolving an artifact
  or touching worker/process state. This check covers `install_artifact()`'s
  deliberate unlocked `ensure(force=True)` interval.
- Track a narrow `_explicit_restart_active` boolean and
  `_clear_pending_count` in `MemoryRuntime`. Restart sets its boolean after
  acquiring both lifecycle locks, before the first lifecycle await, and clears
  it only in final ownership cleanup. `MemoryRuntime.clear()` checks the restart
  boolean and, if false, increments the Clear counter in the same event-loop turn
  before its first await. Keep that counter owned across `module.clear()` and the
  subsequent reconcile, then decrement it in `finally` on every completion or
  cancellation path. `MemoryModule.clear()` also owns its module lifecycle
  intent only for its direct lifecycle-lock path. Multiple queued Runtime Clears
  and module operations retain independent counts.
- `MemoryRuntime.clear()` still returns `memory_clear_failed` without starting
  or queueing `begin_clear()` when an explicit restart already owns admission.
  `MemoryModule.clear()` retains its existing queued lifecycle-lock behavior, so
  search/profile/read contention still serializes. While a Clear waits behind
  such a reader, the nonzero lifecycle counter makes restart return
  `memory_restart_busy`; restart cannot exploit the brief unlocked handoff while
  `asyncio.Lock` wakes any queued Clear, search, or profile request. Conversely,
  once restart admission has started, the active boolean rejects Clear before it
  enters module lifecycle intent; reads keep their existing queued behavior
  behind restart. Intent updates and admission checks contain no intervening
  await, so one side wins deterministically without inspecting private lock
  waiters or adding an unbudgeted predecessor to restart.
- Bound interrupted-clear recovery and the embedding guard/config convergence
  separately by `CLEAR_CLEANUP_TIMEOUT_SECONDS`, because both phases can run in
  one request. Transaction timeout is a reporting threshold, not permission to
  abandon its thread; the mandatory join above retains transaction ownership.
- Give graceful worker drain five seconds. Bound forced worker-task cancellation
  separately so cancellation cleanup cannot make the lifecycle unbounded.
- Bound worker store-task settlement separately. Cancelling an asyncio drain
  owner does not stop a write-capable `asyncio.to_thread()` call, so worker-task
  completion alone is not proof that lease ownership is quiescent.
- Give `MemoryModule._store_call()` the same explicit shielded-task registry and
  bounded `settle_store_calls()` contract as the worker. Clear, interrupted-clear
  recovery, and failure recording retain SQLite-writing threads after coroutine
  timeout/cancellation. Their reporting timeout enters bounded settlement; if an
  exact task still outlives that bound, retain it and set the cross-lifecycle gate
  before releasing locks. Include module-store settlement as its own restart
  deadline component.
- Bound settlement of processing-probe trees separately. Cancelling the drain
  task can make it terminal while probe termination is still incomplete; every
  retained probe owner must prove its process tree reaped before lease rotation
  or sidecar replacement.
- Bound settlement of any automatic supervisor restart already in progress by
  the same all-inclusive process-start budget. The client deadline includes this
  predecessor explicitly. A generation fence, described below, prevents its
  delayed ready activation from crossing a later Clear or reconcile boundary.
- Add an async process handoff operation that settles every active lifecycle
  owner, not only `_restart_task`. It first applies the non-awaiting supervisor
  fence. A watcher or monitor that is still only waiting/polling is cancelled
  and joined before `stop()`; one that has already entered child-tree cleanup is
  retained and awaited under a process-owner settlement cap. Cleanup tasks are
  never cancelled in the middle of termination. `_watch_cleanup_active` and
  `_monitor_cleanup_active` flip synchronously before their tasks await the
  process lifecycle lock or termination; handoff checks the phase and issues any
  idle-task cancellation in the same event-loop turn, with no intervening await.
  After this operation returns, no predecessor watcher, monitor, or restart task
  can still own or be queued on the process lifecycle lock.
- Expose pure budget helpers from `core/memory/process.py` for the production
  defaults. The stop budget includes both TERM and KILL wait rounds. Put one
  outer `asyncio.timeout()` around the complete `SidecarOwnership.reap()` call,
  including leader termination, the late group sweep, and every unusable-record
  anchor. Timeout retains the ownership record, launches no child, and returns a
  start failure. The start budget uses that single outer reap cap, readiness
  wait, and another full stop budget for cleanup after a spawned replacement
  fails to become ready; it never assumes only one internal reap round.
- Put one outer `asyncio.timeout(_STARTUP_TIMEOUT_SECONDS)` around the complete
  `_wait_for_ready()` operation, not only a deadline check at the top of its
  polling loop. The cap includes socket security, the final `/health` request,
  its response body, and polling sleeps. A readiness timeout enters the existing
  separately budgeted failed-start cleanup; no individual health request may
  extend the readiness phase beyond the exported start budget.
- Expose a process-owner settlement budget that includes watcher/monitor
  cancellation joins and one complete TERM/KILL cleanup round. Set
  `MEMORY_RESTART_TIMEOUT_SECONDS` strictly above the sum of two clear cleanup
  bounds, automatic-supervisor start settlement, process-owner settlement,
  worker grace, worker cancellation cleanup, worker and module store-task
  settlement, processing-probe settlement, a separate old-process stop retry,
  and the all-inclusive replacement-start budget. Add a worker-activation bound
  after replacement health plus a distinct replacement-activation cleanup
  allowance containing a non-awaiting supervisor fence, captured automatic-start
  and watcher/monitor settlement, another worker cancellation, worker/module
  store-task settlement, probe settlement, and complete TERM/KILL stop round.
  The contract test imports the source constants/helpers instead of copying
  numbers from comments. A deadline cannot release either transaction while a
  non-cancellable config/store write or owned probe/process tree is still live;
  its mandatory join is an ownership cleanup tail rather than detached restart
  work.
- A busy result is a completed, retryable business response. It is not
  `memory_restart_failed`, starts no background task, ends the UI spinner, and
  displays a localized reason.

No additional restart lock or queue is needed. The UI settings-writer intent
counter and lock, config-wide write transaction, installer flag,
restart-ownership boolean, Clear/reconcile/module lifecycle-intent counters, and
controller/module lifecycle locks define single-flight ownership.

### 5. Forced replacement and lease handoff

`MemoryWorker._boot_id` currently remains stable for the object's lifetime,
while `recover_after_boot()` recovers only `processing` rows owned by another
lease. If restart cancels an already claimed add and reactivates the same worker
owner, that row remains `processing` forever.

Add a private semantic helper such as `begin_replacement_activation()` that
creates a new UUID lease owner and then reuses `begin_activation()`. Do not
change MemoryStore or its recovery SQL.

Make `begin_activation()` create and return a generation-specific completion
future. `_recover_activation()` resolves that exact future only after
`recover_after_boot()`, interrupted-flush handling, metadata reads, and fault
classification all finish. Before the drain loop retries a failed activation,
it rejects the failed generation's future with the original exception and then
creates the next generation; cancellation rejects it as well. A done callback
consumes every terminal exception so an ordinary retry generation with no waiter
cannot emit an unobserved-future warning. `_ensure_worker()` accepts and reuses a
prepared activation future, creating one only for ordinary callers, and returns
the exact future associated with the worker task instead of using task existence
or liveness as success evidence. Explicit restart and automatic ready activation
keep claims paused and use
`wait_for(shield(activation_future), WORKER_ACTIVATION_TIMEOUT_SECONDS)` so the
reporting timeout cannot cancel the shared handshake. Only explicit cancellation
and joining of the worker may reject that generation. Resume claims only after
the exact future resolves successfully.

Make `_store_call()` create an explicit task for `asyncio.to_thread()` and await
it under `shield()`. Keep every unfinished task in a private worker registry;
task cancellation must leave the underlying store task live and discoverable.
A done callback consumes its terminal result and removes it only after the
thread has actually returned. Add `settle_store_calls()` to snapshot and await
all registered tasks without cancelling them. Once the drain task is terminal,
no new store calls can enter the registry, so an empty registry proves every old
lease `claim_due`, `settle`, flush transition, recovery, and metadata write is
finished.

The locked sequence is fixed:

1. Snapshot the last-good `_restart_config` and recover an interrupted clear.
   Validate store and artifact availability only when that replay is enabled.
   Do not reject solely because the persisted candidate is enabled while the
   replay is disabled; the replay state is authoritative after step 9
   convergence and needs no launch prerequisites.
2. Before touching the worker, call a non-awaiting, idempotent supervisor handoff
   fence on the old `EverOSProcess`. It sets `_desired_running=false`, marks ready
   callbacks quiesced, and captures the exact restart, watcher, and monitor task
   references. It cancels a restart only when `_starting` proves it has not
   entered `_start_locked()`; an active start is retained rather than unsafely
   cancelled. A child exit cannot schedule another start after this fence.
3. Immediately pause new claims. Await an active restart under the complete
   process-start bound, then run the process handoff operation under the
   process-owner settlement bound: cancel and join idle watcher/monitor tasks,
   or retain and join the one already performing child cleanup. An active start
   may finish launching a transient child, but generation-fenced activation
   cannot resume claims and step 10 will stop it. If either retained start or
   lifecycle owner outlives its bound, keep its exact references and claims
   fenced, then return fail-closed without rotating the lease or invoking
   `stop()` beside it. A retry rejoins those same tasks first.
4. Allow at most five seconds for the current worker drain. Timeout or
   an ordinary drain error enters the forced phase; task cancellation enters
   the cancellation cleanup path.
5. Cancel and await the old worker task within its explicit cancellation bound.
   Do not clear `_worker_task` until the exact task is done. If it outlives the
   bound, retain the task reference, keep claims fenced, and enter the
   fail-closed state below; do not rotate the lease or touch the process.
6. After the exact drain task is terminal, call `settle_store_calls()` on both
   `MemoryWorker` and `MemoryModule` under their distinct bounds. If any shielded
   store thread remains live, retain its registry entry, the old lease, and every
   existing process reference; set the retained-ownership gate, keep claims
   fenced, and return fail-closed. A retry rejoins the same store task before it
   can proceed.
7. Ask the old supervisor to settle every retained processing-probe tree under
   the probe-settlement bound. `processing_healthy()` registers the exact probe,
   process group, and owned-process snapshot immediately after spawn returns and
   before awaiting probe completion; any cleanup task is recorded before its
   first cleanup await. It removes that record only after complete reap.
   Cancellation or failed cleanup keeps the record on the supervisor. If
   settlement cannot prove every tree reaped, retain those records and the old
   supervisor, keep claims fenced, and return fail-closed. A retry settles the
   same owners first.
8. Rotate the lease owner only after the supervisor, process owners, drain task,
   store-task registry, and probe registry are all confirmed quiescent, and
   before any new activation.
9. Run config convergence while claims are fenced and the old worker is stopped.
   When `_restart_config` has `embedding_change_pending`, run the root guard
   first and clear the marker in the replay block. The transaction must replace
   only the expected persisted Memory block with replay C0, preserving every
   unrelated field, and return the exact committed block. Refresh
   `_persisted_memory_snapshot` from that return value, use the returned block as
   the marker-free replay object, and restore `self._config` only after
   convergence succeeds. Ordinary persisted reconcile follows the same rule:
   settlement rebases its private in-flight candidate from the committed block,
   so successful live and `_restart_config` state cannot retain the old marker.
   If the exact converged replay block is disabled, install that disabled block
   into runtime state, proceed through bounded old-process stop, and record the
   successful terminal state `{ok: true, state: 'disabled'}`. The controller
   wrapper installs the same exact committed block into
   `Controller.config.memory`, and the UI's mandatory success reload switches to
   the authoritative disabled settings. A stop error remains fail-closed and
   cannot report disabled success.
10. Stop the old process. Do not discard its supervisor or start another child
   until its process tree is confirmed reaped.
11. If the converged replay is disabled, return the recorded disabled state
   without creating a replacement child, activation task, or worker. Otherwise,
   create a process from `_restart_config` bound to the lifecycle generation
   captured when restart entered its critical section. Its explicit initial
   `start()` suppresses the automatic ready callback. While restart still owns
   `_reconcile_lock -> module._lifecycle_lock`, keep claims paused, call
   `begin_replacement_activation()`, start the worker, and await that exact
   activation-generation future with the shielded worker-activation wait. Only
   after it resolves may runtime resume claims. On rejection or timeout, apply
   the replacement supervisor's non-awaiting handoff fence before the first
   cleanup await, capture its restart/watcher/monitor owners, then cancel and
   join the new worker, settle module/worker store calls and processing probes,
   settle captured process owners, and stop the replacement under the distinct
   replacement-activation cleanup allowance.
   Any cleanup owner that outlives its bound is retained fail-closed. Return
   `{ok: true, state: 'ready'}` only after real activation succeeds.

Automatic retries use a separate nonblocking ready notification. The callback
captures process identity and lifecycle generation, schedules one retained
runtime activation task, and returns without taking runtime locks, so it cannot
deadlock `_start_locked()` with a caller already holding them. The activation
task acquires `_reconcile_lock -> module._lifecycle_lock`, then verifies that the
runtime is enabled, the process is still current and ready, the generation is
unchanged, and no explicit restart or artifact installation owns admission. It
then keeps claims paused and awaits the worker's exact activation-generation
future under the same worker-activation bound before resuming them.
Every Clear, reconcile, artifact activation, restart, and close operation bumps
the generation at its serialized lifecycle entry, before its first lifecycle
await, and uses that value for any process it creates. Consequently an
activation delayed behind later lifecycle work observes a stale token and exits
without resuming claims or creating a worker. Current-generation activation
failures set the visible runtime error and leave claims fenced; task completion
is consumed and the task is retained/cancelled during later lifecycle cleanup
rather than becoming an unobserved exception.

`restart()` does not call `_probe_processing`. The conditional embedding/root
guard protects persistent vector-space compatibility; it is not a processing
health preflight. Other processing failures continue through the existing
worker classification path.

### 6. Failure and cancellation postconditions

Every exit after claims are fenced must end in one of these states:

- **An automatic supervisor task outlives handoff settlement:** retain that
  exact task, keep the old supervisor quiesced and claims fenced, retain the old
  worker and lease, and return `memory_restart_failed` without touching process
  ownership. A retry first rejoins the same task; it cannot continue while that
  task is live.
- **A watcher or monitor owns cleanup beyond process handoff settlement:** retain
  every captured task and the old supervisor, keep claims fenced, and return
  `memory_restart_failed` without calling `stop()` concurrently or starting a
  replacement. A retry rejoins the same owners under another bounded interval;
  only after they finish may the distinct old-process stop retry begin.
- **The old worker task outlives forced cancellation:** retain that exact task
  in `_worker_task`, retain the original lease, keep claims fenced, leave the
  old process untouched and its supervisor quiesced, set the visible runtime
  error, and return `memory_restart_failed`. A retry reuses that quiesced
  supervisor, cancels and waits on the retained task for another bounded
  interval. It can continue only after the task is done and its outcome has been
  consumed; while it remains live the retry returns the same fail-closed
  response and creates no replacement worker or child.
- **A shielded worker store task outlives settlement:** retain its exact registry
  entry and the original lease after the drain task terminates, keep claims
  fenced, leave the old process untouched, and return `memory_restart_failed`.
  A retry awaits the same task under another bounded interval. It cannot run
  recovery, rotate the lease, or touch process ownership until the thread has
  returned and its terminal result has been consumed.
- **A processing-probe tree outlives cancellation or cleanup:** retain its exact
  probe/process-group/owned-process record and cleanup owner on the supervisor,
  retain the current lease, and keep claims fenced. Restart returns
  `memory_restart_failed` without stopping or replacing the supervised child
  beside that credential-bearing tree. A retry reapplies the bounded settlement
  to the same record and cannot proceed until the registry is empty.
- **Failure before `old_process.stop()` is invoked:** the old supervisor still
  owns its child but is quiesced. A live PID is not evidence that the UDS health
  endpoint is ready, so never resume claims or the worker from `running` alone.
  If it remains running, retain the quiesced supervisor, keep claims and the
  worker fenced, and return the specific failure; a retry rejoins ownership and
  attempts replacement again. Never reuse the supervisor's
  construction-generation callback. If the child exited, it may be re-armed for
  automatic start with a callback rebound to the current supervisor object and
  lifecycle generation, but claims stay fenced until that newly bound activation
  task takes both lifecycle locks and proves the replacement ready. This branch
  applies only after all previous worker, store, and process-owner tasks are
  confirmed done; it never attempts reactivation beside a retained task.
- **`old_process.stop()` was invoked:** `EverOSProcess.stop()` sets
  `_desired_running=false` before termination and cancels scheduled restart.
  If stop raises, retain the supervisor reference but keep claims and the worker
  fenced, set the visible runtime error, and return `memory_restart_failed`.
  Do not claim that the old runtime was restored and do not start a second child.
  The user can retry the explicit restart, which first retries owned-tree stop.
- **The old child stopped but replacement startup failed:** retain the new
  supervisor, if one was created, and keep claims and the worker fenced. If its
  bounded supervisor later reaches ready, its generation-fenced activation task
  may resume them only after coordinating with both lifecycle locks and proving
  it is still current. With no supervisor, the user can click restart again.
- **The replacement reaches health but explicit activation fails:** treat this
  as startup failure. Restart directly observes rejection of the exact
  activation-generation future, keeps claims fenced, cancels and joins the new
  worker, settles its store calls and probe trees, then stops the replacement
  under the separate replacement-activation cleanup allowance. Retain any owner
  that outlives its bound and never report `ready`. A later automatic ready
  activation that fails records the visible runtime error and likewise leaves
  claims fenced.

`CancelledError` is re-raised after ownership and claims cleanup; it is never
reported as a business failure. Fix `_stop_worker()` so it swallows only the
cancelled drain task's `CancelledError`, while cancellation of the lifecycle
task itself propagates. Restart uses shielded cleanup to settle the worker store
registry and reach one of the states above. If cancellation lands after a new
child is spawned but before its watcher or monitor exists, shield
`new_process.stop()`: clear `_process` only after successful reap; otherwise
retain the supervisor reference and keep claims fenced. The same shielded cleanup
retains incomplete processing-probe owners rather than dropping their local
references when cancellation is re-raised.

`start()` returning `False` keeps the specific
`memory_sidecar_unavailable`. Stop, factory, and other restart orchestration
exceptions use the transport-only `memory_restart_failed`.

### 7. Queue semantics

- If cancellation occurs after an add is claimed, activation under the new lease
  returns the old owner's row to `pending`, preserving at-least-once behavior.
  The provider may have accepted the request before local acknowledgement, so
  forced restart cannot promise exactly-once side effects. Store-thread
  settlement guarantees no old-lease claim or acknowledgement can commit after
  that recovery begins.
- If cancellation occurs while a flush is `in_flight`, activation permanently
  marks it `unknown` and opens a processing fault. Historical `unknown` entries
  do not retry or disappear after five minutes. A later successful flush can
  close the fault but does not rewrite the historical record.

## Interface and UI changes

### Backend and transport

1. `config/v2_config.py`, `vibe/api.py`, and every audited whole-file writer:
   add the cross-process `mutate_v2_config()` API, make raw save private, bind
   the target path and working object to an opaque session token, derive one
   lock per normalized default/custom path, always acquire that file lock before
   `CONFIG_LOCK`, and offload the complete mutation from every asynchronous
   caller.
2. `core/memory/runtime.py`: add `_restart_config`,
   `_persisted_memory_snapshot`, `_explicit_restart_active`,
   `_clear_pending_count`, `_reconcile_pending_count`,
   `_retained_ownership_active`, lifecycle generation and a retained ready
   activation task, and fail-fast `restart()`; refresh the
   persisted snapshot after every successful runtime-owned V2 mutation and
   rebase a successfully reconciled
   live/replay candidate from marker settlement's exact return; conditionally
   converge disk to replay before launch; await the exact worker-activation
   generation and retain worker/store/probe owners that outlive cancellation;
   reject restart while artifact installation is active; make only explicit
   restart admission fail fast. Make `restart()` return its transport
   result plus the exact applied replay config whenever convergence committed,
   including later lifecycle failure, and no config before convergence or on
   busy. Add `reconcile_persisted()` to return the exact
   successful candidate alongside the transport result while ordinary
   `reconcile()` preserves its dict contract.
   `Controller.reconcile_memory()` installs that returned candidate into
   `Controller.config.memory`; post-Clear and artifact-internal reconciles cannot
   claim a persisted snapshot or change controller shared config.
3. `core/memory/module.py`: extract locked clear recovery while retaining the
   wrapper and existing queued lifecycle-lock behavior for read/Clear paths. Add
   the synchronous lifecycle-intent scope and `lifecycle_busy` property covering
   active and queued search, profile, Clear, and recovery operations. Shield and
   retain explicit module store tasks and expose their bounded settlement. A
   narrow Runtime-supplied admission predicate prevents search/profile recovery
   and Clear from entering while retained ownership is fenced.
4. `core/memory/worker.py`: retain and shield explicit store-thread tasks, expose
   bounded settlement, make activation return a generation-specific completion
   future, and rotate the lease owner only after prior ownership registries are
   empty.
5. `core/memory/process.py`: expose complete stop/start and process-owner
   settlement budget helpers, put one cap around all orphan-reap rounds, add the
   explicit-handoff supervisor fence, distinguish idle watcher/monitor tasks
   from active cleanup owners with narrow phase flags, hard-cap the complete
   readiness operation, retain processing-probe trees and cleanup owners until
   reaped, expose their settlement and the separate replacement-activation
   cleanup budget, rebind re-armed ready callbacks to the current generation,
   and support callback-suppressed explicit start.
6. `core/controller.py` / `core/internal_server.py`: add a controller-owned
   restart wrapper and `POST /internal/memory/restart`. Whenever Runtime returns
   an applied config, including a post-convergence failure, install a deep copy
   into `Controller.config.memory`, then expose only the transport response with
   its `config_committed` flag. Missing runtime returns
   `memory_runtime_missing`; unhandled exceptions map to `memory_restart_failed`,
   not `memory_reconcile_failed`.
7. `vibe/internal_client.py`: add `memory_restart()` with a deadline above the
   complete restart lifecycle budget; retain and join the shielded request task
   after a reporting timeout.
8. `vibe/ui_memory_routes.py`: keep `/api/memory/runtime/restart`, acquire the
   Memory settings write lock across the internal call, use a synchronous
   pending-writer count to reject restart behind active or queued settings
   saves, and preserve same-origin validation.
9. `core/memory/types.py` and frontend `errors` translations: add transport-only
   `memory_restart_failed` and `memory_restart_busy`; add no SQLite schema field.

### Frontend

1. `memoryRead.ts`: define and export `MemoryRuntimeRestartResult` and a pure
   normalizer near the current Memory response classifier. Accept `{ok:true}`;
   normalize `{ok:false,error}` and `{status:'failed',error}` as failures; fail
   closed on malformed bodies, and preserve a strict boolean `config_committed`.
2. `ApiContext.tsx`: import that type and normalizer. `restartMemoryRuntime()`
   returns only the normalized result and does not create a reverse import.
3. `useMemoryResource.ts`: expose a narrow `invalidate()` transition that drops
   the last accepted payload without changing sticky forbidden state. Any restart
   response with `config_committed=true` uses it before the authoritative
   settings reload; existing resources otherwise keep their current retry policy.
4. `SettingsMemoryPage.tsx`: when `settings?.enabled === true` and the page is not
   cross-origin forbidden, render a page-level Status action row before the
   `remoteUnavailable` / `memorySetupStage()` branch. Use the existing secondary
   `xs` button with `RotateCw` / `Loader2`; disable it while restarting. Runtime
   required, setup loading, and status loading/error states share this action.
   Await the result. When `config_committed=true`, including a later lifecycle
   failure, invalidate the pre-restart Memory settings payload, keep its form
   unavailable, and await an authoritative `getMemorySettings()` reload before
   ending the restart state. A failed reload exposes the settings read error
   without restoring an editable stale C1 payload. Refresh dependency/status
   state as well. Show completion copy only after a successful operation and
   settings reload. After a failed operation, show its localized reason plus
   literal error code only after the required reload settles; when no config was
   committed, keep the existing settings payload and use the generic fallback
   when no code exists.
5. `MemoryStatusPanel.tsx`: remove the old engine-fault restart action and its
   props so the normal Status body cannot render a duplicate. Keep the credential
   fault's settings action.
6. `en.json` / `zh.json`: add button, completion, failure,
   `memory_restart_failed`, and `memory_restart_busy` copy in both locales;
   remove the unused engine-banner action key.

## Minimum sufficient tests

### Python

- `tests/test_memory_runtime.py`
  - Successful restart confirms old stop, new ready child, and worker recovery.
    Pause real `recover_after_boot()` after worker task creation and prove restart
    retains both lifecycle locks, keeps claims paused, and cannot report ready
    until the exact activation-generation future resolves.
  - After failed C1 reconcile and failed rollback, restart replays last-good C0;
    the uncontended operation remains inside both lifecycle locks and
    conditionally replaces the expected persisted C1 Memory block with C0 while
    preserving unrelated fields. A fresh disk load must equal the live runtime;
    an unexpected Memory C2 fails before process mutation. If C0 is disabled,
    convergence stops any retained old child without invoking the process
    factory, start, activation, or worker; Runtime, Controller, disk, and the UI
    reload all observe disabled. A post-Clear internal reconcile cannot
    overwrite the persisted snapshot contract.
  - Pre-held reconcile/module locks and concurrent restart calls immediately
    return `memory_restart_busy`, create no waiters, and change no ownership.
    An installer paused inside unlocked `ensure(force=True)` also returns busy
    before artifact resolution, worker fencing, or process replacement.
    Hold one search/profile request in the module lock and queue a second; across
    the owner's release/waiter-wakeup gap, `lifecycle_busy` remains true and
    restart returns busy without joining the read queue. After both reads finish,
    restart can acquire admission normally.
    Hold one reconcile owner and queue a second non-restart reconcile; across
    the owner's release/waiter-wakeup gap, `_reconcile_pending_count` remains
    nonzero and restart returns busy without joining the queue. Each intent is
    released on success, failure, and cancellation.
  - Clear started while `_explicit_restart_active` is true returns before
    `begin_clear()` and leaves no waiter or durable marker; the inverse ordering
    makes restart return busy. A search/profile call holding the same module lock
    still makes Clear wait and then complete, preserving existing behavior.
    While that Clear is queued, `_clear_pending_count` also makes restart return
    busy through the lock-release/waiter-wakeup gap. Concurrent Clear requests
    retain independent counts until their complete Clear/reconcile paths exit.
  - Force each retained-owner class past its settlement bound and prove the
    retained-ownership gate remains set after `_explicit_restart_active` clears.
    Clear, search/profile recovery, persisted/internal reconcile, artifact
    activation, and automatic ready activation must reject before their first
    lifecycle await. An explicit restart retry may enter, joins the exact owners,
    and clears the gate only after every registry is empty.
  - Hung add and flush cases use shortened bounds; add can be reclaimed by the
    new owner, while flush becomes `unknown` and opens the fault.
  - A worker task that ignores its first cancellation is retained with the old
    lease and fenced claims. No child/process action occurs; retry stays closed
    until the exact task terminates, then completes replacement normally.
  - A drain task cancelled while awaiting a blocked store `claim_due`, `settle`,
    or `mark_flush_in_flight` can become done while its shielded thread remains
    registered. Restart retains the old lease and process without running
    recovery; after the thread is released, retry joins it and only then rotates
    and recovers.
  - Cancel a drain task inside a real supervised processing probe and make its
    first termination round fail. The terminal worker task is insufficient for
    restart admission: the exact probe-tree record remains on the supervisor,
    claims and lease stay fenced, and no process replacement occurs. A retry
    reaps that same record before continuing.
  - Timeout interrupted-clear recovery while `MemoryModule._store_call()` owns a
    blocked `get_meta`, `begin_clear`, `finish_clear`, or failure-recording
    thread. Cancellation leaves the exact task shielded and registered, keeps
    lifecycle/retained admission owned until it returns, and prevents a late
    marker or epoch write from crossing a later operation.
  - If the old child exits during the five-second drain grace, its supervisor is
    already quiesced: no automatic restart can schedule ready activation, resume
    claims, or replace `_worker_task`. Pre-stop failure beside an alive but
    unhealthy child retains the quiesced supervisor and keeps claims and the
    worker fenced; `.running` alone never reactivates it. A child that exited may
    re-arm with a callback bound to the current lifecycle generation and resume
    work only after a replacement proves ready; the callback from the
    supervisor's construction generation is never reused.
  - An automatic restart already inside `_start_locked()` is retained while
    claims are paused. It either settles within the complete start bound and its
    transient child is stopped, or restart returns fail-closed without lease or
    process mutation; retry rejoins the same task.
  - An automatic retry reaches ready while Clear or reconcile owns the module
    lifecycle lock. Its activation task waits, then observes the bumped
    lifecycle generation and exits without resuming claims or replacing the
    worker. The equivalent no-intervening-lifecycle case activates only after
    taking both locks.
  - The old watcher or safety monitor enters child-tree cleanup immediately
    before restart. Handoff joins that exact owner under the process-owner bound
    before calling `stop()`; timeout stays fail-closed, and the successful case
    still has a separately budgeted complete stop retry.
  - Clear-marker recovery and replacement share one lifecycle critical section;
    recovery failure launches no child.
  - A startup snapshot with the embedding marker rejects existing vectors and
    settles an empty root before launch. A successful ordinary persisted
    reconcile that clears the marker refreshes `_persisted_memory_snapshot`, the
    successful live config, and `_restart_config` to the exact marker-free disk
    block. After vectors are created, a later restart must replay that block
    without rerunning the stale-marker root guard or returning
    `memory_clear_failed`.
  - Disabled, `start() is False`, factory exception, explicit activation
    exception, automatic activation exception, failed-start cleanup, and stop
    exception cases assert error codes, supervisor ownership, claims/worker
    fencing, and no double child. Drive explicit activation failure through real
    worker recovery after task creation; its rejected future cannot report ready,
    and replacement worker/store/probe settlement plus a complete process stop
    fit the distinct cleanup allowance. A stop exception followed by child exit
    must not auto-restart or report restored claims.
  - Let the healthy replacement exit immediately after activation failure. The
    non-awaiting replacement fence runs before worker/store/probe cleanup, so no
    watcher can schedule a new start; if a start/process owner already entered,
    cleanup captures and settles it inside the advertised allowance.
  - Delay the real activation generation beyond its reporting bound and prove
    `wait_for(shield(future))` leaves the handshake pending. Only subsequent
    worker cancellation rejects it, without `InvalidStateError` or loss of the
    real activation outcome.
  - After convergence commits C0, fail old stop, replacement start, and explicit
    activation in separate cases. Each failure returns the exact applied C0 and
    `config_committed=true`; `Controller.config.memory`, Runtime, and disk all
    remain C0. Pre-convergence failure returns no candidate and false.
  - Cancellation while stopping the worker and after a new child is spawned
    reaches the documented cleanup state and re-raises `CancelledError`.
- `tests/test_memory_slice3.py`: a successful persisted reconcile that settles
  the embedding marker returns the exact marker-free applied config to
  `Controller.reconcile_memory()`, which installs a deep copy into
  `Controller.config.memory`. Disk, controller, runtime live state, and replay
  state must match; the original marker-bearing request object is not installed.
  Failure returns no candidate and preserves the prior controller config.
- `tests/test_internal_server.py`: success, missing runtime, exception mapping,
  and post-convergence failure propagation without serializing the config object.
- `tests/test_internal_client.py` / `tests/test_internal_client_timeouts.py`:
  POST and busy response passthrough; deadline computed from both clear phases,
  automatic-supervisor start settlement, watcher/monitor owner settlement,
  worker, store-task, and processing-probe bounds, a distinct old stop retry, the
  outer all-round orphan-reap cap, readiness, failed-start cleanup, explicit
  worker activation, and a separate complete replacement-activation cleanup
  allowance. On reporting timeout or caller cancellation, the retained request
  is joined before its caller can release transaction ownership.
- `tests/test_ui_memory_routes.py`: new client path, internal unavailable,
  cross-origin rejection, restart/settings serialization, timed-out settlement
  ownership, unrelated-field retention, and failed-Memory-C1 convergence to C0.
  Hold one settings writer and queue a second; through the owner's
  release/waiter-wakeup gap, the synchronous pending count makes restart return
  busy without acquiring the lock. After every writer exits, restart may acquire
  admission normally. A `config_committed=true` failure invalidates C1 and reloads
  C0 before showing the lifecycle error; a pre-convergence failure keeps C1.
- `tests/test_v2_config.py` / config route tests: a generic non-Memory save in a
  second process waits before loading while settlement owns the config
  transaction, then preserves both its C1 change and the cleared marker. Saving
  without a valid session/object token is rejected; a stale C0 loaded before a
  later transaction cannot be saved, and an accepted mutator observes the
  intervening C1. Nested same-thread mutations share one fresh working object
  and publish once without deadlock. While a second process holds the file lock,
  writers wait without owning `CONFIG_LOCK`, representative inline async UI and
  controller reads complete, and event-loop probes continue to run.
  A custom-path initializer and mutation update only that selected target and
  its sibling lock, leaving the default config untouched. Tokens cannot cross
  paths, same-target nested mutation reuses its session, and nested
  different-target mutation is rejected before acquiring another lock.
- `tests/test_memory_worker.py`: `_store_call()` shields and retains its explicit
  thread task across drain cancellation. `settle_store_calls()` times out without
  cancelling the thread, retains it for retry, consumes its terminal result, and
  reports quiescence only after the registry is empty. Activation generations
  resolve only after full recovery, reject with the real recovery error before
  retry creates a new generation, and reject on cancellation.
- `tests/test_memory_module.py`: clear/recovery store calls use explicit shielded
  tasks; timeout and caller cancellation retain each SQLite-writing task through
  terminal result, and settlement reports quiescence only when the registry is
  empty.
- `tests/test_memory_process.py`: stale recorded leader plus late helper and
  multiple unusable-record anchors share one outer reap deadline. Timeout keeps
  the record and prevents child launch. Idle watcher/monitor tasks cancel and
  join during handoff; active cleanup owners are retained and settle under the
  exported owner budget. A cancelled processing probe whose termination fails
  retains its exact tree and cleanup owner; settlement retries that record and
  reports quiescence only after reap. A replacement handoff fence applied before
  activation cleanup prevents an exit from scheduling restart, while an already
  active start is captured and settled. Explicit start suppresses automatic
  ready notification.
  A socket that appears just before the readiness deadline with a stalled health
  response cannot extend `_wait_for_ready()` beyond its outer cap, and the
  separately budgeted failed-start cleanup still runs.

### TypeScript

- Table-test the pure normalizer with `ok:true`, `ok:false`, `status:'failed'`,
  strict `config_committed` values, and malformed responses.
- Reuse React SSR / `renderToStaticMarkup`, without a new DOM framework. Inject
  enabled plus runtime-missing state into `SettingsMemoryPage` and assert that
  the setup prompt and exactly one restart action coexist. Also cover status
  loading/error and the disabled restarting state. `MemoryStatusPanel` tests
  assert no engine restart action and retain the credential action.
- Extend the existing Memory resource/state tests with the restart-success
  transition: invalidate displayed C1, settle the authoritative settings reload
  with C0, and expose only C0 to the form. The reload-failure case keeps no
  editable C1 payload. Add the equivalent `config_committed=true` failure
  transition: reload C0 before displaying the lifecycle error. A false flag
  preserves C1. Keep click/toast behavior in the single manual scenario below.

## Validation

1. `pytest tests/test_memory_runtime.py -k restart`
2. `pytest tests/test_memory_worker.py tests/test_memory_module.py -k 'store or cancel or activation or clear'`
3. `pytest tests/test_internal_server.py tests/test_internal_client.py tests/test_internal_client_timeouts.py tests/test_ui_memory_routes.py tests/test_v2_config.py -k 'memory or config or restart'`
4. Run `ruff check` on every changed Python file.
5. In `ui/`, run the relevant normalizer, `SettingsMemoryPage`, and
   `MemoryStatusPanel` Vitest files, then `npm run build`.
6. Update only the local Incus `master` regression environment with
   `./scripts/run_regression.sh`; preserve configuration and verify service health.
7. Through `python3 scripts/incus_regression.py shell --target master`, identify
   the managed sidecar PID and send `SIGSTOP` to create an alive-but-unreachable
   process. In the Web UI, verify the persistent action, spinner, toast, PID
   replacement, and return to `ready`. If replacement does not complete, send
   `SIGCONT` to the original PID during cleanup.
8. Check Avibe service health again through the runner. Do not restart the local
   `vibe` service and do not use `kill -9`; that only covers existing child-exit
   supervision, not this scenario.

## Review conclusion

The necessary complexity comes from existing safety and concurrency contracts:
failed candidates cannot replace last-good config, and successful replay must
conditionally converge the known persisted Memory block to that same config;
config mutation cannot race any whole-file V2 writer, start from an unlocked
baseline, lose a custom target path, hold `CONFIG_LOCK` during a file-lock wait,
block an async event loop, or outlive its transaction; every successful
runtime-owned mutation refreshes the exact persisted snapshot and successful
settlement propagates the same exact block into controller shared config;
explicit restart cannot queue behind or leapfrog any active or queued module
lifecycle operation, while reads and destructive Clear retain their existing
serialization; artifact installation excludes launch; orphan recovery has one
complete cap; every active process lifecycle
owner is settled and budgeted before stop; readiness, including the final health
request, has one hard cap; delayed automatic activation and re-armed supervision
are bound to the current lifecycle generation and both runtime locks; pending
embeddings cannot bypass the root guard or remain in a successful replay
snapshot; claimed rows require a new lease only after the old worker and every
shielded worker/module store thread exit; activation success is observable and
its shared future is shielded from reporting timeout; post-convergence failure
still propagates the committed block to Controller and UI; retained ownership
excludes every other mutating lifecycle; replacement cleanup fences supervision
before awaiting; and clear markers serialize with root/child lifecycle. The
implementation adds two private snapshots, focused intent counters, one narrow
retained-owner gate, focused helpers, and two precise transport-only error codes
while reusing existing locks, guards, recovery SQL, supervision, and UI
primitives.

Do not introduce a general coordinator, explicit restart state machine, new
port, database field, automatic restart policy, or frontend test framework. If
implementation appears to require one, revise this plan before expanding scope.

## Todo

- [ ] Replay/disk snapshots, conditional C1-to-C0 convergence, focused tests
- [ ] Marker-settlement persisted/live/replay refresh and later-restart regression
- [ ] Persisted reconcile propagation into Controller.config
- [ ] Settings/restart serialization and stale-C0 overwrite regression
- [ ] Path-aware token-bound config mutation and custom-target regression
- [ ] File-lock-first order, async writers off-thread, inline-reader responsiveness
- [ ] Timed-out settlement ownership and retained-request regression
- [ ] Worker lease rotation and add/flush recovery tests
- [ ] Retained worker cancellation fail-closed state and retry
- [ ] Shielded store-thread registry, bounded settlement, and retry
- [ ] Module clear/recovery store-task retention and cross-lifecycle owner gate
- [ ] Locked clear recovery and race regression
- [ ] Restart/Clear intent admission, queued-Clear handoff, artifact admission
- [ ] Queued read intent admission and waiter-handoff regression
- [ ] Supervisor handoff/rebind, lifecycle-owner settlement, readiness/deadline contract
- [ ] Lifecycle-generation ready activation and Clear/reconcile race regression
- [ ] Complete orphan/start budgets, explicit activation, failed-start cleanup
- [ ] Shielded activation handshake and fenced replacement-activation cleanup
- [ ] Internal server/client, closed/busy errors, bounded timeout tests
- [ ] UI route and response normalizer
- [ ] Page action, deduplicated banner, success/failure settings reload, toast/i18n
- [ ] Focused Python/TypeScript validation and Incus `SIGSTOP` scenario
