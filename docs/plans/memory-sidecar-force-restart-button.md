# Add a forced sidecar restart action to Memory settings (rev8)

> Rev8 keeps one public recovery action and a focused set of internal fixes:
> replayable configuration, config-wide write serialization, restart-specific
> lifecycle admission, generation-fenced ready activation, complete supervisor
> quiescing, bounded orphan recovery, worker lease rotation, and locked
> clear-marker recovery. Timed-out work remains owned until it is either joined
> or proven unable to mutate state. It does not add a lifecycle coordinator,
> explicit state machine, provider/store port, or frontend DOM test framework.

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

Add a private `_restart_config` to `MemoryRuntime`:

- Initialize it as a deep copy of the startup `MemoryConfig`, so a first-start
  failure can still be retried with the same configuration.
- Update it with another deep copy only when enabled or disabled reconcile has
  completed successfully, while `_reconcile_lock` is held.
- Never commit a failed candidate. A deep copy is required because
  `MemoryConfig`, its nested settings, and `embedding_change_pending` are
  mutable.
- `restart()` reads a deep copy while holding `_reconcile_lock`. After all
  safety checks pass and before process replacement, it restores `self._config`
  from that copy so worker callbacks and child settings cannot keep using C1.
- A startup snapshot may have `embedding_change_pending=true`. Restart reuses
  the reconcile embedding/root guard and proceeds only after both the guard and
  marker settlement succeed. Existing vectors or an indeterminate root return
  `memory_clear_failed` without launching a child. Successful settlement clears
  the marker in persisted config, `_restart_config`, and the later restored
  `self._config`, without changing any other Memory field. It does not run the
  processing probe.

This field answers only which configuration an explicit restart replays. It
does not redefine `_config` or introduce configuration versioning.

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

- Before calling `internal_client.memory_restart()`, the restart route checks
  `_memory_settings_write_lock()`. If it is already held, return
  `memory_restart_busy` immediately rather than joining its waiter queue.
- If it is free, acquire it immediately and hold it across the complete internal
  restart request and response. Every Memory PATCH already holds this same lock
  across load, save, controller reconcile, and rollback, so a PATCH cannot write
  C1 between the controller's C0 comparison and marker-clear save.
- Add one synchronous `mutate_v2_config(mutator, *, initializer=None)` write API
  in `config/v2_config.py`. Its outermost call acquires `CONFIG_LOCK` and then a
  config-specific cross-process file lock, using the existing
  `storage.lock.MigrationFileLock` primitive with a dedicated lock path and
  bounded acquisition. Only after both locks are owned does it load the current
  document, invoke a synchronous mutator on that fresh working object, validate
  it, and atomically save it before releasing either lock. It never accepts a
  caller-supplied or preloaded `V2Config` instance. First-install/default paths
  may supply an initializer factory, which is also invoked only under both locks
  when the document is absent.
- Make the raw save primitive private to that API. Bind its opaque write token
  to both the transaction session and the exact config object loaded inside it;
  a missing token, a token from another session, or a config object loaded
  before lock acquisition is rejected. Reentrant same-thread mutation calls
  reuse the outer session and its one working object, and only the outermost call
  publishes one final document, rather than reloading or saving a nested stale
  baseline.
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
- Controller settlement uses that mutation API, compares the complete expected
  Memory block on the fresh working object, mutates only
  `embedding_change_pending`, and atomically saves. A mismatch or bounded lock
  timeout fails closed with `memory_clear_failed`. Unrelated config writers wait
  before their load, so none can publish a stale whole-file snapshot afterward.
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

Add route concurrency tests that pause restart settlement and start both a
Memory PATCH and an unrelated `/api/config` save. The Memory PATCH must wait on
the UI lock; the generic save must wait on the cross-process config transaction
before loading its baseline. Cover ordinary completion and an expired settlement
deadline. In the expired case, prove the request and transaction locks remain
owned until the retained thread finishes. Both writers must then preserve the
settled marker and their own C1 fields without either side restoring stale C0.
Also load a stale C0 before another process publishes C1 and prove it cannot be
passed into the mutation API or saved with a later transaction token; the
accepted mutator must observe C1. Hold the file lock from a second process and
prove representative async UI and controller writers keep their event loops
responsive while their complete mutation waits in a worker thread.

### 4. Nonqueued admission and complete deadlines

An explicit restart must not wait silently behind another lifecycle operation.
Otherwise the transport can time out first while the controller later performs
an abandoned restart, and a user retry can enqueue a second one.

- Before any await, `restart()` checks `_reconcile_lock` and
  `module._lifecycle_lock`. If either is held, return
  `{ok: false, error: 'memory_restart_busy'}` without changing the process,
  claims, or worker.
- When both are free, acquire them in the established order in the same event
  loop turn, with no intervening await. After acquiring `_reconcile_lock`, check
  `_artifact_installing` while that lock is owned. If installation is active,
  release the lock and return `memory_restart_busy` before resolving an artifact
  or touching worker/process state. This check covers `install_artifact()`'s
  deliberate unlocked `ensure(force=True)` interval.
- Track a narrow `_explicit_restart_active` boolean in `MemoryRuntime`. Set it
  after restart has acquired both lifecycle locks, before the first lifecycle
  await, and clear it only in the final ownership cleanup. `MemoryRuntime.clear()`
  checks this flag before awaiting `module.clear()` and returns
  `memory_clear_failed` without starting or queueing `begin_clear()` only when an
  explicit restart owns the lifecycle. `MemoryModule.clear()` retains its
  existing queued lock behavior, so search/profile/read contention still
  serializes instead of becoming a spurious Clear failure. If Clear reaches the
  module lock first, restart observes that lock and returns `memory_restart_busy`.
- Bound interrupted-clear recovery and embedding guard/settlement separately by
  `CLEAR_CLEANUP_TIMEOUT_SECONDS`, because both phases can run in one request.
  Settlement timeout is a reporting threshold, not permission to abandon its
  thread; the mandatory join above retains transaction ownership.
- Give graceful worker drain five seconds. Bound forced worker-task cancellation
  separately so cancellation cleanup cannot make the lifecycle unbounded.
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
- Expose a process-owner settlement budget that includes watcher/monitor
  cancellation joins and one complete TERM/KILL cleanup round. Set
  `MEMORY_RESTART_TIMEOUT_SECONDS` strictly above the sum of two clear cleanup
  bounds, automatic-supervisor start settlement, process-owner settlement,
  worker grace, worker cancellation cleanup, a separate old-process stop retry,
  and the all-inclusive replacement-start budget. The contract test imports the
  source constants/helpers instead of copying numbers from comments. A deadline
  cannot release either transaction while a non-cancellable settlement write is
  still live; its mandatory join is an ownership cleanup tail rather than
  detached restart work.
- A busy result is a completed, retryable business response. It is not
  `memory_restart_failed`, starts no background task, ends the UI spinner, and
  displays a localized reason.

No additional restart lock or queue is needed. The existing UI settings lock,
config-wide write transaction, installer flag, restart-ownership boolean, and
controller/module lifecycle locks define single-flight ownership.

### 5. Forced replacement and lease handoff

`MemoryWorker._boot_id` currently remains stable for the object's lifetime,
while `recover_after_boot()` recovers only `processing` rows owned by another
lease. If restart cancels an already claimed add and reactivates the same worker
owner, that row remains `processing` forever.

Add a private semantic helper such as `begin_replacement_activation()` that
creates a new UUID lease owner and then reuses `begin_activation()`. Do not
change MemoryStore or its recovery SQL.

The locked sequence is fixed:

1. Validate store, artifact, and enabled state; recover an interrupted clear.
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
   cannot resume claims and step 8 will stop it. If either retained start or
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
6. Rotate the lease owner only after both retained tasks are confirmed done and
   before any new activation.
7. Only when the snapshot has `embedding_change_pending`, run the root guard and
   marker settlement while claims are fenced and the old worker is stopped.
   Restore `self._config` only after they pass.
8. Stop the old process. Do not discard its supervisor or start another child
   until its process tree is confirmed reaped.
9. Create a process from `_restart_config` bound to the lifecycle generation
   captured when restart entered its critical section. Its explicit initial
   `start()` suppresses the automatic ready callback: while restart still owns
   `_reconcile_lock -> module._lifecycle_lock`, runtime itself resumes claims,
   starts the worker, and verifies `_worker_task` exists and is not done. An
   activation exception runs bounded replacement cleanup, keeps claims fenced,
   and cannot report ready. Return `{ok: true, state: 'ready'}` only after this
   explicit activation succeeds.

Automatic retries use a separate nonblocking ready notification. The callback
captures process identity and lifecycle generation, schedules one retained
runtime activation task, and returns without taking runtime locks, so it cannot
deadlock `_start_locked()` with a caller already holding them. The activation
task acquires `_reconcile_lock -> module._lifecycle_lock`, then verifies that the
runtime is enabled, the process is still current and ready, the generation is
unchanged, and no explicit restart or artifact installation owns admission.
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
- **Failure before `old_process.stop()` is invoked:** the old supervisor still
  owns its child but is quiesced. Re-arm supervision and ready callbacks. If its
  child is still running, reactivate with the new lease owner, resume claims and
  the worker, and return the specific failure. If the child exited during the
  handoff, keep claims fenced and let the re-armed supervisor start a child;
  only a current-generation activation task may resume the worker after taking
  both lifecycle locks. This branch applies only after the previous worker and
  process-owner tasks are confirmed done; it never attempts reactivation beside
  a retained task.
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
  as startup failure. Restart directly observes the exception, runs the same
  bounded child cleanup, keeps claims fenced, and cannot report `ready`. A later
  automatic ready activation that fails records the visible runtime error and
  likewise leaves claims fenced.

`CancelledError` is re-raised after ownership and claims cleanup; it is never
reported as a business failure. Fix `_stop_worker()` so it swallows only the
cancelled drain task's `CancelledError`, while cancellation of the lifecycle
task itself propagates. Restart uses shielded cleanup to reach one of the states
above. If cancellation lands after a new child is spawned but before its watcher
or monitor exists, shield `new_process.stop()`: clear `_process` only after
successful reap; otherwise retain the supervisor reference and keep claims
fenced.

`start()` returning `False` keeps the specific
`memory_sidecar_unavailable`. Stop, factory, and other restart orchestration
exceptions use the transport-only `memory_restart_failed`.

### 7. Queue semantics

- If cancellation occurs after an add is claimed, activation under the new lease
  returns the old owner's row to `pending`, preserving at-least-once behavior.
  The provider may have accepted the request before local acknowledgement, so
  forced restart cannot promise exactly-once side effects.
- If cancellation occurs while a flush is `in_flight`, activation permanently
  marks it `unknown` and opens a processing fault. Historical `unknown` entries
  do not retry or disappear after five minutes. A later successful flush can
  close the fault but does not rewrite the historical record.

## Interface and UI changes

### Backend and transport

1. `config/v2_config.py`, `vibe/api.py`, and every audited whole-file writer:
   add the cross-process `mutate_v2_config()` API, make raw save private, bind
   the working object to an opaque session token, and offload the complete
   mutation from every asynchronous caller.
2. `core/memory/runtime.py`: add `_restart_config`,
   `_explicit_restart_active`, lifecycle generation and a retained ready
   activation task, and fail-fast `restart()`; commit snapshots after successful
   reconcile; retain worker tasks that outlive cancellation; reject restart
   while artifact installation is active; make only restart-owned Clear
   contention fail fast.
3. `core/memory/module.py`: extract locked clear recovery while retaining the
   wrapper and existing queued lifecycle-lock behavior for read/Clear paths.
4. `core/memory/worker.py`: rotate the lease owner for replacement activation.
5. `core/memory/process.py`: expose complete stop/start and process-owner
   settlement budget helpers, put one cap around all orphan-reap rounds, add the
   explicit-handoff supervisor fence, distinguish idle watcher/monitor tasks
   from active cleanup owners with narrow phase flags, and support
   callback-suppressed explicit start.
6. `core/internal_server.py`: add `POST /internal/memory/restart`. Missing runtime
   returns `memory_runtime_missing`; unhandled exceptions map to
   `memory_restart_failed`, not `memory_reconcile_failed`.
7. `vibe/internal_client.py`: add `memory_restart()` with a deadline above the
   complete restart lifecycle budget; retain and join the shielded request task
   after a reporting timeout.
8. `vibe/ui_memory_routes.py`: keep `/api/memory/runtime/restart`, acquire the
   Memory settings write lock across the internal call, and preserve same-origin
   validation.
9. `core/memory/types.py` and frontend `errors` translations: add transport-only
   `memory_restart_failed` and `memory_restart_busy`; add no SQLite schema field.

### Frontend

1. `memoryRead.ts`: define and export `MemoryRuntimeRestartResult` and a pure
   normalizer near the current Memory response classifier. Accept `{ok:true}`;
   normalize `{ok:false,error}` and `{status:'failed',error}` as failures; fail
   closed on malformed bodies.
2. `ApiContext.tsx`: import that type and normalizer. `restartMemoryRuntime()`
   returns only the normalized result and does not create a reverse import.
3. `SettingsMemoryPage.tsx`: when `settings?.enabled === true` and the page is not
   cross-origin forbidden, render a page-level Status action row before the
   `remoteUnavailable` / `memorySetupStage()` branch. Use the existing secondary
   `xs` button with `RotateCw` / `Loader2`; disable it while restarting. Runtime
   required, setup loading, and status loading/error states share this action.
   Await the result. Show completion copy on success and a localized reason plus
   literal error code on failure, with a generic fallback when no code exists.
4. `MemoryStatusPanel.tsx`: remove the old engine-fault restart action and its
   props so the normal Status body cannot render a duplicate. Keep the credential
   fault's settings action.
5. `en.json` / `zh.json`: add button, completion, failure,
   `memory_restart_failed`, and `memory_restart_busy` copy in both locales;
   remove the unused engine-banner action key.

## Minimum sufficient tests

### Python

- `tests/test_memory_runtime.py`
  - Successful restart confirms old stop, new ready child, and worker recovery.
  - After failed C1 reconcile and failed rollback, restart replays last-good C0;
    the uncontended operation remains inside both lifecycle locks.
  - Pre-held reconcile/module locks and concurrent restart calls immediately
    return `memory_restart_busy`, create no waiters, and change no ownership.
    An installer paused inside unlocked `ensure(force=True)` also returns busy
    before artifact resolution, worker fencing, or process replacement.
  - Clear started while `_explicit_restart_active` is true returns before
    `begin_clear()` and leaves no waiter or durable marker; the inverse ordering
    makes restart return busy. A search/profile call holding the same module lock
    still makes Clear wait and then complete, preserving existing behavior.
  - Hung add and flush cases use shortened bounds; add can be reclaimed by the
    new owner, while flush becomes `unknown` and opens the fault.
  - A worker task that ignores its first cancellation is retained with the old
    lease and fenced claims. No child/process action occurs; retry stays closed
    until the exact task terminates, then completes replacement normally.
  - If the old child exits during the five-second drain grace, its supervisor is
    already quiesced: no automatic restart can schedule ready activation, resume
    claims, or replace `_worker_task`. Pre-stop failure re-arms that supervisor
    and resumes work only after a live/ready child is established.
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
    settles an empty root before launch.
  - Disabled, `start() is False`, factory exception, explicit activation
    exception, automatic activation exception, failed-start cleanup, and stop
    exception cases assert error codes, supervisor ownership, claims/worker
    fencing, and no double child. An activation exception cannot report ready; a
    stop exception followed by child exit must not auto-restart or report
    restored claims.
  - Cancellation while stopping the worker and after a new child is spawned
    reaches the documented cleanup state and re-raises `CancelledError`.
- `tests/test_internal_server.py`: success, missing runtime, and exception mapping.
- `tests/test_internal_client.py` / `tests/test_internal_client_timeouts.py`:
  POST and busy response passthrough; deadline computed from both clear phases,
  automatic-supervisor start settlement, watcher/monitor owner settlement,
  worker bounds, a distinct old stop retry, the outer all-round orphan-reap cap,
  readiness, and failed-start cleanup. On reporting timeout or caller
  cancellation, the retained request is joined before its caller can release
  transaction ownership.
- `tests/test_ui_memory_routes.py`: new client path, internal unavailable,
  cross-origin rejection, restart/settings serialization, timed-out settlement
  ownership, and final C1 retention.
- `tests/test_v2_config.py` / config route tests: a generic non-Memory save in a
  second process waits before loading while settlement owns the config
  transaction, then preserves both its C1 change and the cleared marker. Saving
  without a valid session/object token is rejected; a stale C0 loaded before a
  later transaction cannot be saved, and an accepted mutator observes the
  intervening C1. Nested same-thread mutations share one fresh working object
  and publish once without deadlock. While a second process holds the file lock,
  representative async UI and controller writers wait off-thread and event-loop
  probes continue to run.
- `tests/test_memory_process.py`: stale recorded leader plus late helper and
  multiple unusable-record anchors share one outer reap deadline. Timeout keeps
  the record and prevents child launch. Idle watcher/monitor tasks cancel and
  join during handoff; active cleanup owners are retained and settle under the
  exported owner budget. Explicit start suppresses automatic ready notification.

### TypeScript

- Table-test the pure normalizer with `ok:true`, `ok:false`, `status:'failed'`,
  and malformed responses.
- Reuse React SSR / `renderToStaticMarkup`, without a new DOM framework. Inject
  enabled plus runtime-missing state into `SettingsMemoryPage` and assert that
  the setup prompt and exactly one restart action coexist. Also cover status
  loading/error and the disabled restarting state. `MemoryStatusPanel` tests
  assert no engine restart action and retain the credential action.
- Keep click/toast behavior in the single manual scenario below.

## Validation

1. `pytest tests/test_memory_runtime.py -k restart`
2. `pytest tests/test_internal_server.py tests/test_internal_client.py tests/test_internal_client_timeouts.py tests/test_ui_memory_routes.py tests/test_v2_config.py -k 'memory or config or restart'`
3. Run `ruff check` on every changed Python file.
4. In `ui/`, run the relevant normalizer, `SettingsMemoryPage`, and
   `MemoryStatusPanel` Vitest files, then `npm run build`.
5. Update only the local Incus `master` regression environment with
   `./scripts/run_regression.sh`; preserve configuration and verify service health.
6. Through `python3 scripts/incus_regression.py shell --target master`, identify
   the managed sidecar PID and send `SIGSTOP` to create an alive-but-unreachable
   process. In the Web UI, verify the persistent action, spinner, toast, PID
   replacement, and return to `ready`. If replacement does not complete, send
   `SIGCONT` to the original PID during cleanup.
7. Check Avibe service health again through the runner. Do not restart the local
   `vibe` service and do not use `kill -9`; that only covers existing child-exit
   supervision, not this scenario.

## Review conclusion

The necessary complexity comes from existing safety and concurrency contracts:
failed candidates cannot replace last-good config; marker settlement cannot
race any whole-file V2 writer, start from an unlocked baseline, block an async
event loop, or outlive its transaction; explicit restart and destructive Clear
cannot queue behind each other while ordinary reads retain their existing
serialization; artifact installation excludes launch; orphan recovery has one
complete cap; every active process lifecycle owner is settled and budgeted
before stop; delayed automatic activation is fenced by lifecycle generation and
both runtime locks; pending embeddings cannot bypass the root guard; claimed
rows require a new lease only after the old worker exits; activation success is
observable; and clear markers serialize with root/child lifecycle. The
implementation adds one private snapshot, narrow helpers, and two precise
transport-only error codes while reusing existing locks, guards, recovery SQL,
supervision, and UI primitives.

Do not introduce a general coordinator, explicit restart state machine, new
port, database field, automatic restart policy, or frontend test framework. If
implementation appears to require one, revise this plan before expanding scope.

## Todo

- [ ] Runtime snapshot, pending-embedding guard, fail-fast restart, focused tests
- [ ] Settings/restart serialization and stale-C0 overwrite regression
- [ ] Token-bound config mutation API and concurrent non-Memory writer regression
- [ ] Async UI/controller config writers off-thread and event-loop responsiveness
- [ ] Timed-out settlement ownership and retained-request regression
- [ ] Worker lease rotation and add/flush recovery tests
- [ ] Retained worker cancellation fail-closed state and retry
- [ ] Locked clear recovery and race regression
- [ ] Restart-specific Clear admission and artifact-install admission
- [ ] Supervisor handoff fence, lifecycle-owner settlement, and deadline contract
- [ ] Lifecycle-generation ready activation and Clear/reconcile race regression
- [ ] Complete orphan/start budgets, explicit activation, failed-start cleanup
- [ ] Internal server/client, closed/busy errors, bounded timeout tests
- [ ] UI route and response normalizer
- [ ] Page-level action, deduplicated banner, runtime-missing render, toast/i18n
- [ ] Focused Python/TypeScript validation and Incus `SIGSTOP` scenario
