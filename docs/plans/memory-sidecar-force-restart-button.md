# Add a forced sidecar restart action to Memory settings (rev5)

> Rev5 keeps one public recovery action and a small set of internal fixes:
> replayable configuration, settings transaction serialization, fail-fast
> lifecycle admission, worker lease rotation, and locked clear-marker recovery.
> It does not add a lifecycle coordinator, explicit state machine, provider/store
> port, or frontend DOM test framework.

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

### 3. Serialize settlement with Memory settings writes

`CONFIG_LOCK` is process-local: the controller's compare-and-save cannot, by
itself, exclude a Memory PATCH running in the UI process. The public restart
route must therefore participate in the UI process's existing settings
transaction.

- Before calling `internal_client.memory_restart()`, the restart route checks
  `_memory_settings_write_lock()`. If it is already held, return
  `memory_restart_busy` immediately rather than joining its waiter queue.
- If it is free, acquire it immediately and hold it across the complete internal
  restart request and response. Every Memory PATCH already holds this same lock
  across load, save, controller reconcile, and rollback, so a PATCH cannot write
  C1 between the controller's C0 comparison and marker-clear save.
- Controller settlement still performs load, full Memory configuration compare,
  marker-only mutation, and atomic file replacement while its local
  `CONFIG_LOCK` is held. A mismatch fails closed with `memory_clear_failed`.
- The internal UDS endpoint remains controller-private. The supported
  user-facing writer is the UI route that owns the cross-process transaction
  boundary.

Add a route concurrency test that pauses restart settlement, starts a PATCH from
a second task, and verifies the PATCH cannot save until restart releases the UI
lock. It must then read the settled configuration as its baseline and persist
C1 without being overwritten by a stale C0 save.

### 4. Nonqueued admission and complete deadlines

An explicit restart must not wait silently behind another lifecycle operation.
Otherwise the transport can time out first while the controller later performs
an abandoned restart, and a user retry can enqueue a second one.

- Before any await, `restart()` checks `_reconcile_lock` and
  `module._lifecycle_lock`. If either is held, return
  `{ok: false, error: 'memory_restart_busy'}` without changing the process,
  claims, or worker.
- When both are free, acquire them in the established order in the same event
  loop turn, with no intervening await. Concurrent restart, Settings PATCH,
  artifact activation, and clear therefore have one owner. Existing reconcile
  and clear behavior is unchanged; only explicit restart is fail-fast.
- Bound interrupted-clear recovery and embedding guard/settlement separately by
  `CLEAR_CLEANUP_TIMEOUT_SECONDS`, because both phases can run in one request.
- Give graceful worker drain five seconds. Bound forced worker-task cancellation
  separately so cancellation cleanup cannot make the lifecycle unbounded.
- Expose pure budget helpers from `core/memory/process.py` for the production
  defaults. The stop budget includes both TERM and KILL wait rounds. The start
  budget includes recorded-orphan reap, readiness wait, and another full stop
  budget for cleanup after a spawned replacement fails to become ready.
- Set `MEMORY_RESTART_TIMEOUT_SECONDS` strictly above the sum of two clear
  cleanup bounds, worker grace, worker cancellation cleanup, old-process stop,
  and the all-inclusive replacement-start budget. The contract test imports the
  source constants/helpers instead of copying numbers from comments.
- A busy result is a completed, retryable business response. It is not
  `memory_restart_failed`, starts no background task, ends the UI spinner, and
  displays a localized reason.

No additional restart lock or queue is needed. The existing UI settings lock
and controller lifecycle locks define single-flight ownership.

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
2. Pause claims and allow at most five seconds for the current drain. Timeout or
   an ordinary drain error enters the forced phase; task cancellation enters
   the cancellation cleanup path.
3. Cancel and await the old worker task within its explicit cancellation bound.
4. Rotate the lease owner after the old task ends and before any new activation.
5. Only when the snapshot has `embedding_change_pending`, run the root guard and
   marker settlement while claims are fenced and the old worker is stopped.
   Restore `self._config` only after they pass.
6. Stop the old process. Do not discard its supervisor or start another child
   until its process tree is confirmed reaped.
7. Create a process from `_restart_config` and call `start()`. Restore claims and
   the worker only after `start()` and `on_ready` succeed. Return
   `{ok: true, state: 'ready'}` synchronously.

`restart()` does not call `_probe_processing`. The conditional embedding/root
guard protects persistent vector-space compatibility; it is not a processing
health preflight. Other processing failures continue through the existing
worker classification path.

### 6. Failure and cancellation postconditions

Every exit after claims are fenced must end in one of these states:

- **Failure before `old_process.stop()` is invoked:** the old supervisor still
  has its original desired-running state. Reactivate with the new lease owner,
  resume claims and the worker, and return the specific failure. The runtime is
  genuinely restored to its pre-restart state.
- **`old_process.stop()` was invoked:** `EverOSProcess.stop()` sets
  `_desired_running=false` before termination and cancels scheduled restart.
  If stop raises, retain the supervisor reference but keep claims and the worker
  fenced, set the visible runtime error, and return `memory_restart_failed`.
  Do not claim that the old runtime was restored and do not start a second child.
  The user can retry the explicit restart, which first retries owned-tree stop.
- **The old child stopped but replacement startup failed:** retain the new
  supervisor, if one was created, and keep claims and the worker fenced. If its
  bounded supervisor later reaches `on_ready`, the existing callback resumes
  them. With no supervisor, the user can click restart again.

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

1. `core/memory/runtime.py`: add `_restart_config` and fail-fast `restart()`;
   commit snapshots after successful reconcile; fix caller cancellation handling
   in `_stop_worker()`.
2. `core/memory/module.py`: extract locked clear recovery while retaining the
   wrapper for read paths.
3. `core/memory/worker.py`: rotate the lease owner for replacement activation.
4. `core/memory/process.py`: expose complete stop/start budget helpers, including
   startup-failure cleanup.
5. `core/internal_server.py`: add `POST /internal/memory/restart`. Missing runtime
   returns `memory_runtime_missing`; unhandled exceptions map to
   `memory_restart_failed`, not `memory_reconcile_failed`.
6. `vibe/internal_client.py`: add `memory_restart()` with a deadline above the
   complete restart lifecycle budget.
7. `vibe/ui_memory_routes.py`: keep `/api/memory/runtime/restart`, acquire the
   Memory settings write lock across the internal call, and preserve same-origin
   validation.
8. `core/memory/types.py` and frontend `errors` translations: add transport-only
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
  - Hung add and flush cases use shortened bounds; add can be reclaimed by the
    new owner, while flush becomes `unknown` and opens the fault.
  - Clear-marker recovery and replacement share one lifecycle critical section;
    recovery failure launches no child.
  - A startup snapshot with the embedding marker rejects existing vectors and
    settles an empty root before launch.
  - Disabled, `start() is False`, factory exception, failed-start cleanup, and
    stop exception cases assert error codes, supervisor ownership, claims/worker
    fencing, and no double child. A stop exception followed by child exit must
    not auto-restart or report restored claims.
  - Cancellation while stopping the worker and after a new child is spawned
    reaches the documented cleanup state and re-raises `CancelledError`.
- `tests/test_internal_server.py`: success, missing runtime, and exception mapping.
- `tests/test_internal_client.py` / `tests/test_internal_client_timeouts.py`:
  POST and busy response passthrough; deadline computed from both clear phases,
  worker bounds, old stop, readiness, and failed-start cleanup.
- `tests/test_ui_memory_routes.py`: new client path, internal unavailable,
  cross-origin rejection, restart/settings serialization, and final C1 retention.

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
2. `pytest tests/test_internal_server.py tests/test_internal_client.py tests/test_internal_client_timeouts.py tests/test_ui_memory_routes.py -k 'memory and restart'`
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

The necessary complexity comes from six existing safety and concurrency
contracts: failed candidates cannot replace last-good config; marker settlement
cannot overwrite a concurrent settings save; explicit restart cannot queue
behind the transport; pending embeddings cannot bypass the root guard; claimed
rows require a new lease; and clear markers must serialize with root/child
lifecycle. The implementation adds one private snapshot, narrow helpers, and
two precise transport-only error codes while reusing existing locks, guards,
recovery SQL, supervision, and UI primitives.

Do not introduce a general coordinator, explicit restart state machine, new
port, database field, automatic restart policy, or frontend test framework. If
implementation appears to require one, revise this plan before expanding scope.

## Todo

- [ ] Runtime snapshot, pending-embedding guard, fail-fast restart, focused tests
- [ ] Settings/restart serialization and stale-C0 overwrite regression
- [ ] Worker lease rotation and add/flush recovery tests
- [ ] Locked clear recovery and race regression
- [ ] Complete process budgets and failed-start cleanup timeout contract
- [ ] Internal server/client, closed/busy errors, bounded timeout tests
- [ ] UI route and response normalizer
- [ ] Page-level action, deduplicated banner, runtime-missing render, toast/i18n
- [ ] Focused Python/TypeScript validation and Incus `SIGSTOP` scenario
