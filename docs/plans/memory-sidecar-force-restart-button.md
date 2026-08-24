# Add a forced Memory sidecar restart action (rev21)

> Historical design. Superseded by
> [`memory-unified-recovery-pr3.md`](./memory-unified-recovery-pr3.md); Wake is
> now the sole non-destructive availability operation.

> Rev21 keeps one public recovery action and one linear Runtime lifecycle while
> closing the replay-marker, delayed-readiness, and UI timeout races. The Runtime,
> Module, Worker, and Process interfaces stay deep: callers see
> `MemoryRuntime.restart()`, while worker lease handoff and process ownership
> remain private implementation details.

## Decision

When Memory is enabled, the settings page will expose one "Restart engine"
action. The action replaces an alive-but-unreachable sidecar with a new child
using the Runtime's replay configuration: the startup configuration until the
first successful reconcile, then the latest configuration applied successfully.
An independently successful `embedding_change_pending` settlement is reflected
in that snapshot without adopting the rest of a subsequently failed candidate.

This plan deliberately does not repair configuration persistence. In
particular, restart does not:

- load its target configuration from disk;
- rewrite a failed persisted candidate back to the last-good configuration;
- migrate or serialize every V2 config writer;
- update `Controller.config` or force the UI to reload settings; or
- settle `embedding_change_pending`.

If the replay snapshot still has `embedding_change_pending=true`, restart fails
closed with `memory_clear_failed` before pausing the worker or touching the
sidecar. The existing Settings/Clear lifecycle remains responsible for checking
the provider root and settling that persisted marker. Once settlement succeeds,
the Runtime mirrors only the cleared marker into its replay snapshot. Keeping
the settlement itself and all other configuration repair out of restart is what
removes the global config transaction and Controller rebasing work from this
feature.

## Goals

1. Whenever saved Memory settings are known to be enabled, show one restart
   action even when status is loading, unavailable, or reports `down`.
2. Stop the old supervised sidecar, start a replacement from the in-memory
   replay configuration, and report success only after the replacement process
   is ready.
3. Preserve the queue's existing at-least-once behavior without leaving rows
   stranded under the old worker lease.
4. Never launch a second child until the old supervisor has confirmed its child
   tree is stopped.

## Non-goals

- automatic health-based restart;
- persisted/live/Controller config repair after a failed settings rollback;
- a general Memory lifecycle coordinator or explicit state machine beyond the
  narrow ready-callback generation described below;
- new provider, store, or process interfaces;
- retrying historical `unknown` flushes;
- disabled/no-store cleanup recovery UX;
- global ownership tracking for every `asyncio.to_thread()` call; and
- a full four-platform regression.

## Why reconcile is not the restart operation

The current UI route calls persisted reconciliation. That is the wrong
interface for a process-only recovery action:

- reconcile loads the persisted settings instead of replaying the last working
  Runtime configuration;
- it may wait 30 seconds for a worker drain and then run a processing preflight;
- an active flush can last up to 300 seconds; and
- `MemoryRuntime._config` is assigned before every later root/start step has
  succeeded, so a failed candidate is not necessarily safe to replay.

Keep the existing public route, but route it to a dedicated operation:

```text
UI
  -> POST /api/memory/runtime/restart
  -> internal_client.memory_restart()
  -> POST /internal/memory/restart
  -> MemoryRuntime.restart()
```

## Observable contract

`MemoryRuntime.restart()` returns one of:

```text
{ok: true, state: "ready"}
{ok: false, error: <Memory error code>}
```

The contract is:

- concurrent callers join the same in-progress restart task; they do not queue
  a second stop/start sequence;
- success means the replacement `EverOSProcess.start()` returned `True`, which
  already includes the sidecar health/readiness check;
- restart does not run the processing preflight probe;
- restart does not modify persisted settings; an earlier reconcile that settled
  the durable embedding marker also clears only that marker in the replay copy;
- disabled replay returns `memory_disabled` without constructing a process;
- unavailable store/module returns `memory_store_unavailable`;
- a pending embedding change or failed clear recovery returns
  `memory_clear_failed` without launching a replacement;
- ordinary orchestration failures return `memory_restart_failed`;
- a replacement that fails readiness returns
  `memory_sidecar_unavailable`; and
- no failure may leave two sidecars owned by the Runtime.

The five-second requirement applies only to graceful worker drain. It is not a
five-second HTTP or whole-operation deadline. Process stop and startup keep
their existing bounded behavior. A synchronous SQLite store call that was
already executing must finish before the worker task is considered stopped;
the SQLite connection's existing busy timeout bounds ordinary lock contention,
but the plan does not pretend Python can preempt an arbitrary running thread.

## Backend design

### 1. Replay configuration

Add one private `_restart_config` to `MemoryRuntime`:

- initialize it as a deep copy of the startup `MemoryConfig`;
- update it with a deep copy only after enabled or disabled reconciliation has
  returned success while `_reconcile_lock` is held;
- immediately after `_settle_embedding_change_pending()` succeeds, clear only
  `_restart_config.embedding_change_pending` while `_reconcile_lock` is held,
  even if a later reconciliation step fails;
- never copy any other field from a failed candidate; and
- copy it again at the start of the locked restart sequence.

The deep copies are required because `MemoryConfig`, its nested settings, and
`embedding_change_pending` are mutable.

The startup value is intentionally a retry candidate, not proof that the
configuration has run successfully. A new Runtime has no durable last-good
record. After its first successful reconcile, `_restart_config` becomes the
latest successfully applied configuration for the remainder of that Runtime
instance. This plan does not add persistence for replay history across an Avibe
restart.

Marker settlement is a separate successful fact from candidate activation. The
existing guard has already proved the provider root safe and persisted
`embedding_change_pending=false`, so leaving `true` in the startup or last-good
replay copy would permanently disable restart after a later readiness or root
failure. Mutating only that boolean preserves the last-good settings while
making the replay snapshot agree with the durable marker.

Immediately before replacement, restore `self._config` from the replay copy so
the worker's `enabled` callback and the new child settings use the same value.
Restart does not compare this snapshot with disk and does not
claim to repair a pre-existing disk/live split. A failed C1 settings transaction
therefore remains a Settings concern: restart may run live C0, but the persisted
file is unchanged and the UI must not report that configuration was repaired.

### 2. One retained restart task

Add one private `_restart_task` to `MemoryRuntime`.

`restart()` creates `_restart_once()` as a task when none is active, otherwise
it awaits the existing task. Every caller awaits it through `asyncio.shield()`.
Caller cancellation or an HTTP disconnect therefore detaches that caller but
does not abandon a half-completed process handoff. A later caller joins the same
task and receives the same terminal result.

The task converts ordinary exceptions to the structured failure result and is
cleared by a done callback only after it is terminal. `close()` joins an active
restart task before running its existing worker/process shutdown. Do not add a
restart-specific shutdown budget or a registry of lifecycle owners.

### 3. Existing lock order and Clear serialization

The restart task uses the existing lock order:

```text
MemoryRuntime._reconcile_lock
  -> MemoryModule._lifecycle_lock
     -> provider-root lifecycle lock, only inside Clear recovery
```

No pending counters, waiter inspection, retained-owner gate, or new coordinator
is added. A concurrent reconcile, Clear, artifact activation, or restart finishes
in this lock order; it is acceptable for restart to wait for the current
lifecycle operation rather than implement fail-fast admission. The narrow
generation below only invalidates readiness notifications; it does not grant
ownership or replace these locks.

Make two focused fixes to keep that order real:

1. Extract `MemoryModule._recover_interrupted_clear_locked()` from the current
   wrapper. It requires `_lifecycle_lock`, takes the existing root lock, rereads
   the durable marker, and performs the existing bounded recovery. The public
   wrapper keeps its current read-path behavior. Reconcile, artifact activation,
   and restart call the locked helper only after both Runtime and Module locks
   are held. This removes the current recovery/check gap.
2. Make `MemoryRuntime.clear()` hold `_reconcile_lock` across `module.clear()`
   and its optional post-Clear reconcile. `module.clear()` continues to own its
   own Module/root locks; the post-Clear step reacquires the Module lock and calls
   the existing locked reconcile helper. Do not pre-acquire the Module lock and
   then call `module.clear()`, which would self-deadlock.

Capture and status do not need a new lifecycle gate. Capture only enqueues store
work, and status metadata writes do not carry a worker lease. Search/profile and
Clear already serialize through the Module lifecycle lock.

### 4. Ready-callback serialization

Add one private monotonic `_lifecycle_generation`, one retained
`_ready_activation_task`, and one latest-event slot to `MemoryRuntime`. Each
outer Runtime lifecycle owner advances the generation in `finally`: reconcile,
Clear, restart, and the full artifact-install span. Locked helpers reuse their
owner's generation rather than marking a nested lifecycle. The artifact-install
span continues to use its existing active flag while its blocking thread runs
outside `_reconcile_lock`.

Bind each supervisor's `on_ready` callback to that exact supervisor. The callback
must synchronously schedule or coalesce a Runtime-owned activation task and
return; it must not await Runtime locks while `EverOSProcess` is still holding
its own lifecycle lock. Scheduling overwrites the latest-event slot with the
notifying supervisor and captured generation, then ensures one activation task
is running. That task drains the slot and acquires `_reconcile_lock` followed by
`module._lifecycle_lock` for each event. Its done callback clears only the exact
task that became terminal, so a newer task cannot be lost. Before resuming claims
it revalidates all of the following:

- the captured generation is unchanged;
- the notifying supervisor is still `self._process` and reports `running`;
- replay/live configuration is enabled with no pending embedding marker; and
- artifact installation is not active.

A callback emitted by a lifecycle-owned `start()` is therefore stale after that
lifecycle advances the generation in `finally`; the explicit success path resumes
claims and starts the worker itself. A delayed automatic retry in a quiescent
generation is serialized behind any later lifecycle and activates the worker
only if its process identity and captured generation still match. A callback
during the unlocked artifact-install span fails the active-install revalidation.
`close()` advances the generation, clears the latest-event slot, and joins the
retained ready-activation task before process shutdown. This prevents both lock
inversion with the Process supervisor and unowned callback work.

### 5. Worker stop and lease handoff

Only worker store calls need cancellation settlement because they may write a
row using the old lease owner.

Change `MemoryWorker._store_call()` to create one task for the current
`asyncio.to_thread()` call and await it through `shield()`. If the drain task is
cancelled, wait for that exact store task to finish before propagating
`CancelledError`. The worker drain lock already makes these calls serial, so no
registry or Module-wide store tracking is needed.

Change `MemoryRuntime._stop_worker()` so `_worker_task` is cleared only after
the exact task is terminal. It must distinguish the drain task's expected
`CancelledError` from cancellation of the lifecycle caller.

Add a private Worker helper such as `begin_new_lease_activation()` that generates
a new lease UUID and then reuses `begin_activation()`. Its interface is expressed
only in Worker lease terms; it does not know about process replacement. Call it
only after the old worker task and its active store call have ended, and only
after the old supervisor has stopped successfully. The next drain activation
then uses the existing `recover_after_boot()` path before making a new claim.

Queue semantics remain unchanged:

- an add claimed under the old lease is recovered for the new lease and may be
  delivered again under the existing at-least-once contract; and
- an interrupted in-flight flush becomes `unknown` and opens the existing
  processing fault. Restart does not retry or erase that historical result.

### 6. Keep artifact installation truthful

`install_artifact()` currently clears `_artifact_installing` in `finally` even
though cancellation of `asyncio.to_thread(ensure)` does not stop its thread.
Use one local shielded task and wait for it to finish before clearing the flag
or propagating cancellation. Restart checks the flag while holding
`_reconcile_lock` and returns `memory_restart_failed` before handoff if an
installation is still active.

This is a local ownership fix. Do not add an installer registry or a general
`to_thread()` framework.

### 7. Linear replacement sequence

Inside `_restart_once()`:

1. Acquire `_reconcile_lock`, copy the replay configuration, and validate
   store/module availability, artifact installation state, enabled replay, and
   `embedding_change_pending=false`. Resolve the installed Python runtime here,
   before handoff, but do not run a processing preflight.
2. Acquire `module._lifecycle_lock`.
3. Run locked interrupted-Clear recovery. If it fails, return without creating
   a process. On success, call `pause_claims()` again with no intervening await;
   the existing recovery helper may resume claims before returning, but no worker
   can run between its return and this synchronous fence.
4. Allow the current drain tick five seconds to finish with new claims paused.
   Whether it finishes gracefully or times out, cancel and await the exact old
   worker task. The shielded Worker store call must settle before this step ends.
5. Call the old supervisor's existing `stop()` interface. Clear `_process` only
   after `stop()` succeeds. If it fails, retain the supervisor reference, keep
   claims paused, and return `memory_restart_failed` without constructing a new
   process.
6. Restore `self._config` from the replay copy, replace the provider, apply the
   existing artifact/root metadata checks, and construct the replacement.
   Bind its ready callback to that exact supervisor and assign it to `_process`
   before awaiting `start()` so ownership is never lost.
7. After old ownership is gone, rotate the Worker lease and then call the
   replacement's `start()`. Rotating before `start()` ensures that both immediate
   success and a later automatic ready callback use the new lease. If `start()`
   returns `False`, keep the replacement supervisor for its existing automatic
   retry, keep claims paused, and return `memory_sidecar_unavailable`.
8. On success, resume claims, start the drain task, clear the visible runtime
   error, and return `{ok: true, state: "ready"}`.
9. Advance `_lifecycle_generation` in `finally`, making any callback emitted
   during the lifecycle stale before releasing `_reconcile_lock`.

Failures before the old process is touched may restore the previous worker and
claims when its supervisor is still running. Failures after old ownership is
released remain fail closed: retain any replacement supervisor, keep claims
paused, and leave the same restart action available. These two phase-based
postconditions replace rev19's per-owner failure matrix.

`EverOSProcessPort` remains unchanged. Runtime calls only `running`, `starting`,
`start()`, `stop()`, and `processing_healthy()`; it never reads watcher,
monitor, restart-task, process-tree, or cleanup-phase internals. The Process
implementation remains responsible for the successful `stop()` postcondition.

## Transport and UI

### Backend transport

1. `core/internal_server.py`: add `POST /internal/memory/restart`, which calls
   `MemoryRuntime.restart()`. Missing Runtime returns `memory_runtime_missing`;
   unhandled exceptions map to `memory_restart_failed`.
2. `vibe/internal_client.py`: add `memory_restart()` without a client-side
   reporting timeout. The Runtime may be waiting for an already-running SQLite
   thread that Python cannot preempt, so only the internal response is proof that
   its retained restart task reached a terminal result.
3. `vibe/ui_memory_routes.py`: keep `/api/memory/runtime/restart`, preserve its
   same-origin/user checks, and call `memory_restart()` instead of reconcile.
   Create or join one loop-scoped, retained UI-process restart request task using
   the same loop-rebinding discipline as `_memory_settings_write_lock()`. That
   task acquires the settings lock and holds it until the no-timeout internal
   request returns; route callers await it through `asyncio.shield()`. A browser
   disconnect or cancelled route waiter therefore does not release the settings
   lock while the controller restart continues, and a normal Memory PATCH cannot
   save/reconcile concurrently. Clear the retained task only after it is terminal.
   Do not add a pending counter or a cross-process config lock.
4. `core/memory/types.py` and frontend translations: add transport-only
   `memory_restart_failed`. Reuse existing error codes for disabled, unavailable,
   clear-marker, and sidecar-readiness failures. Do not add a SQLite schema value.

### Frontend

1. `SettingsMemoryPage.tsx`: render one page-level secondary `xs` action with
   `RotateCw` whenever `settings?.enabled === true` and Memory access is
   authorized. This preserves both trusted loopback and authenticated Avibe
   Cloud access; the frontend does not invent a separate locality test. Put the
   action outside the status/loading/runtime-required branches so a missing
   status or runtime dependency does not hide it.
2. Keep one `restarting` boolean. Disable the action and show `Loader2` while the
   request is pending.
3. Reuse the existing `MemoryRuntimeRestartResult` union. Success shows a
   completed-restart toast and reloads status/failures. Failure uses
   `memoryErrorMessage()` and the returned literal error code. Do not invalidate
   or reload settings because restart did not change them.
4. Remove the engine-fault banner's duplicate restart action. Keep the
   credential-fault "Open settings" action.
5. Update English and Chinese translations together. Do not add
   `restartRetryEligible`, `config_committed`, config-recovery state, or a new
   frontend test framework.

## Focused tests

Test observable behavior through the existing module interfaces. Do not assert
private counters, task registries, generations, or callback phases.

### Python

- `tests/test_memory_runtime.py`
  - alive-but-unreachable process: one old `stop()`, one new `start()`, then
    `ready`; no processing preflight;
  - two concurrent callers share one restart and one result;
  - before any successful reconcile, restart uses the startup retry candidate;
    after successful C0, a failed C1 is not committed and restart uses C0 while
    the persisted config remains byte-for-byte unchanged;
  - successful marker settlement followed by a later reconcile failure clears
    only the marker in the replay snapshot, so restart remains available without
    adopting the failed candidate's settings;
  - pending embedding marker and failed Clear recovery launch no child;
  - restart and Clear/reconcile serialize without a marker/start race;
  - a delayed ready callback racing Clear, reconcile, or artifact activation
    cannot start a worker inside that lifecycle; stale process/generation
    callbacks are ignored and a surviving quiescent retry activates normally;
  - drain completion inside the grace window preserves its result;
  - cancellation during a Worker store call does not start replacement until
    the synchronous write has ended, after which the new lease recovers old
    claimed work;
  - old `stop()` failure retains the old supervisor and creates no replacement;
  - new readiness failure keeps claims paused and allows later automatic ready
    recovery or another explicit restart; and
  - caller cancellation leaves the retained operation owned, and a second
    caller joins it without another replacement.
- `tests/test_memory_worker.py`: new-lease activation rotates the lease and runs
  durable activation recovery before the next claim. Do not duplicate the
  existing Store tests for add recovery and interrupted-flush `unknown` rules.
- Artifact activation coverage: inject a blocking artifact manager, run its
  `ensure()` through the real `asyncio.to_thread` path, and cancel the install
  caller. Through `MemoryRuntime.restart()` results, prove restart remains
  excluded until that exact thread settles and is admitted afterward; do not
  assert the private `_artifact_installing` flag.
- `tests/test_internal_server.py`: success, missing Runtime, and exception
  mapping for the new endpoint.
- `tests/test_internal_client.py` and timeout tests: method/path, response
  passthrough, and the explicit absence of a restart reporting timeout.
- `tests/test_ui_memory_routes.py`: new client call, settings-lock serialization,
  retained-task sharing, caller-cancellation behavior, internal unavailability,
  and existing cross-origin rejection. Prove a PATCH remains blocked after its
  restart route waiter is cancelled and proceeds only after the internal restart
  reaches a terminal response.

### TypeScript

- Extend the existing static-render Memory tests to cover an enabled page-level
  action when status is absent/error, disabled spinner state, and exactly one
  restart action for an engine fault.
- Keep response-shape coverage on the existing
  `MemoryRuntimeRestartResult`; do not add a separate normalizer module unless
  implementation reveals an actual malformed-response bug.
- Cover click/toast behavior with the single manual scenario rather than adding
  a DOM test framework.

## Validation

1. Run focused Memory Runtime, Worker, internal transport, and UI route tests.
2. Run `ruff check` on changed Python files.
3. Run the relevant Vitest files and `cd ui && npm run build`.
4. Update only the local Incus `master` environment with
   `./scripts/run_regression.sh`; preserve its configuration and verify service
   health.
5. In the container, send `SIGSTOP` to the managed sidecar to create an
   alive-but-unreachable process. Verify the page-level action remains visible,
   the old PID is replaced, the action shows progress, and status returns to
   `ready`. Send `SIGCONT` to the original PID only if cleanup needs it.
6. Verify Avibe service health again. Do not restart the local `vibe` service and
   do not use `kill -9` for this scenario.

## Deferred work

Track these as separate proposals only when their own product requirement
exists:

- a cross-process transaction for every V2 config writer;
- failed settings rollback repair across disk, Runtime, Controller, and UI;
- generic async-thread ownership and cancellation infrastructure;
- a general Memory lifecycle coordinator or process generation model beyond the
  narrow ready-callback generation;
- disabled/no-store durable orphan recovery UX; and
- global process-budget or shutdown-lifecycle redesign.

## Todo

- [x] Replay `_restart_config`, single-flight task, and focused Runtime tests
- [x] Locked Clear recovery and serialized Runtime Clear
- [x] Five-second worker drain, store-call settlement, and lease rotation
- [x] Linear old-stop/new-start flow and failure postconditions
- [x] Internal transport and existing UI route switch
- [x] One page-level action, toast/i18n, and engine-banner deduplication
- [x] Focused Python/TypeScript validation
- [ ] Local Incus `SIGSTOP` scenario
