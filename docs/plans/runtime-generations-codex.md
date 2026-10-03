# Runtime Generations: Codex Adapter

Status: implemented on the Codex lane of `runtime-generations.md`, on the
shared core of `modules/agents/runtime_generations.py`. The owner decisions
recorded in that document are binding here.

## Verification gate (done)

Hermetic probe environment:

- `codex-cli 0.159.3`, running under a `sandbox-exec` wrapper that allows
  loopback egress only, using the repository `CodexTransport`;
- temporary `HOME`, `CODEX_HOME`, and `TMPDIR`;
- `tests/e2e/drivers/mock_llm_upstream.py` serving the Responses API, configured
  as a `model_providers` entry in a temporary `config.toml`.

All scratch was deleted afterwards.

| Check | Result |
| --- | --- |
| (a) Two app-servers in one cwd | Pass. Both initialize. Concurrent turns on different threads, one per server, both complete. |
| (b) Resume on B while A is alive | Fails as written. Codex holds a cross-process writer lock: an OS file lock on `$CODEX_HOME/thread-writer-locks/<thread>.lock`, implemented in `codex-rs/rollout/src/writer_lock.rs`. While A has the thread loaded, even idle, B's `thread/resume` returns `-32600 "thread … already has an active writer"`. |
| (b') Release lever | `thread/unsubscribe` on A returns `unsubscribed`. Once a thread has no subscriber and is idle, A unloads it after `thread_unload_delay_secs` (default 60; see `app-server/src/request_processors/thread_lifecycle.rs`), emits `thread/closed`, and drops the lock. Measured: 60.02 s at the default, 0.05 s with `-c thread_unload_delay_secs=0`. B then resumes while A stays alive, and B's next upstream request carries A's turn history. A later stray `turn/start` on A fails with `thread not found`. |
| (b'') Release by exit | EOF, SIGTERM, or SIGKILL of A's process tree releases the lock. After SIGKILL a stale `.lock` file remains, but the OS lock is gone and B resumes. |
| (c) A's shutdown appends nothing | Pass in every variant: unsubscribe with delay 0 then EOF or SIGTERM; unsubscribe at the default delay then EOF; no unsubscribe then EOF, SIGTERM, or SIGKILL. The rollout's size and sha256 are identical before and after both A's unload and A's exit. A's exit changes only its own lock and tmp files in `CODEX_HOME`; the SQLite WAL files are unchanged. A fresh server later reads every turn as completed, with all user texts in order. |
| (d) `thread/read` and `thread/fork` on B while A holds the source | Both work, whether the source is idle or has an active turn. Neither touches the source rollout. |
| (e) Cross-process read of a live turn | B reports A's in-progress turn as `interrupted`, never as `inProgress`. |
| (f) Unsubscribe during an active turn | Codex defers the unload until the turn ends. Our connection stops receiving that turn's notifications, so `turn/completed` never arrives. When the turn ends, the thread unloads and `thread/closed` is delivered. |

Consequences:

1. **A thread moves only after its old generation releases it.** The release
   is `thread/unsubscribe` followed by `thread/closed`, or the old process
   having exited. Every Codex app-server launches with
   `thread_unload_delay_secs=0`. Avibe never unsubscribes a thread it still
   routes to, so the setting changes nothing else.
2. **Never unsubscribe a thread whose turn Avibe still needs to observe** (f).
   When a move follows an interrupt, Avibe settles the interrupted request
   itself.
3. **Live-turn reads must go to the generation running the turn** (e). This
   matters for the fork boundary read.
4. Codex's own lock makes a misroute fail loudly (`active writer` or
   `thread not found`) rather than corrupt a rollout.

## Model

`modules/agents/codex/agent.py` keeps one `RuntimeGenerationSet` per working
directory (`_units[cwd]`). Each generation wraps one `_CodexRuntime`:

| Field | Meaning |
| --- | --- |
| `transport` | the app-server process |
| `hub` | it holds the directory's Model Hub gateway credential |
| `activation` | its own `RuntimeActivationIdentity`, key `"{cwd}#{serial}"` |
| `threads` | base session id → the Codex thread loaded in this process |
| `released_threads` | thread id → set on `thread/closed` |
| `last_activity` | idle-eviction clock |
| `ended` | the process is gone (the core detaches a generation before its teardown, and a graceful teardown may still decline) |

`_session_generations[base_session_id]` names the generation holding each
Session's thread. `_session_mgr.get_thread_id(bid)` is set only while that
thread is loaded there.

A live binding has exactly two owners, because Codex lets one process at a
time hold a thread:

- the process ending (`_forget_runtime_sessions`);
- the thread's release (`_release_session_thread`).

Every other path uses `_forget_stale_session`, which clears only thread ids
that no live process holds:

- the bulk refresh, migration, and resume paths;
- `/new`, through `clear_sessions`, which first releases each live thread.

A Session whose process survived a failed stop therefore stays bound to it. Every
per-Session path routes through this binding: `handle_stop`,
`steer_active_turn`, `reconcile_steer_attempt`, liveness capture, activation
lookup, and notification filtering. `_runtimes[cwd]` tracks every process not
yet ended, attached to its unit or not, for Hub scope decisions and shutdown.

### Launch spec

`CodexLaunchSpec.digest` hashes the exact process inputs, so two generations
with equal digests are interchangeable:

```text
sha256({
  "epoch": renewal_epoch,
  "binary": {configured, realpath, [st_dev, st_ino, st_size, st_mtime_ns]},
  "argv": hub_overrides + extra_args,      # gateway URL
  "catalog": content-addressed catalog path,
  "env": sha256(sorted launch env),        # managed PATH, AVIBE_MODEL_HUB_TOKEN, CODEX_HOME
  "credential": codex_credential_identity(CODEX_HOME),
  "cwd": [cwd, inode(cwd)],
})
```

- **Binary identity.** This is the resolved path and its stat signature; no
  version probe runs. Every install path replaces the file: npm wrote new
  inodes and install-time mtimes for `codex.js` and `package.json`. So the
  signature changes on every upgrade or reinstall, including one made outside
  Avibe, and no turn pays for a `--version` subprocess.
- **Credential identity** (`vibe/codex_config.py`). This is the effective
  credential store, the auth mode, the API key digest, and the ChatGPT
  `account_id`. Tokens are excluded: a running app-server reloads its
  credential store itself on a 401 for the same account
  (`UnauthorizedRecovery` in `codex-rs/login`). So only an account, key, mode,
  or sign-in change needs a new process. Only `auth.json` is read. With the
  `keyring` or `auto` store, Codex may keep the credential in the OS keyring
  (`Direct`, the default off Windows) or in `secrets/codex_auth.age` with its
  key in the keyring (`Secrets`, the Windows default). Avibe never reads
  either, so a change made there outside Avibe is adopted at the next renewal.
  Avibe's own sign-in renews, and an API key Avibe writes pins the file store.
- `config.toml` is not a process input: app-server reloads it for every
  `thread/start` and `thread/resume`.
- **Excluded inputs.** `auth_mode` (unused), `idle_timeout_seconds` (read
  live), and every per-turn input.
- `direct` and `native_cli` launches have identical inputs, so they share a
  generation.
- The spec carries the binary, argv, environment, and a catalog pin. `start`
  uses only these values, so nothing re-reads the configuration between the
  resolution and the launch.

The Hub catalog comes from the turn's `ModelHubRuntimeRouter.snapshot()`, the
same load the launch resolution used. It is cached by `(binary identity,
models digest)`, with the two most recent entries kept. Each Hub generation
holds its own pin while it runs.

A turn's whole configuration is one load, taken in one synchronous step at
admission, before anything awaits (`_launch_inputs`): the Model Hub snapshot,
the Codex config's binary and extra arguments, the runtime environment, the
renewal epoch, and the binary, credential, and cwd identities. The Hub launch
resolution, catalog preparation, the spec digest, acquisition, and a failure
retry all use only that load, so a save or renewal that lands while any of them
is awaited changes nothing about the turn; the directory's next turn moves
(RUNTIME-GEN-015). The turn's shell environment builds on the environment its
app-server was launched with, never on the current config. A probe takes its
own load the same way, at its own start.

## Lifecycle invariants

Every transition below was audited against each invariant, by the adapter
author and two independent reviewers.

| Invariant | Rule | Owner |
| --- | --- | --- |
| Binding | A live Session binding is dropped only by its process ending or by a proven release. Every other path forgets only bindings whose process is gone. | `_forget_runtime_sessions`, `_release_session_thread`, `_forget_stale_session` |
| Release proof | A thread loads elsewhere only after `thread/closed`, `notLoaded`, absence from `thread/loaded/list`, or the old process's exit by return code. Anything less raises `CodexThreadReleaseUnavailableError` and changes nothing; every caller propagates it. | `_release_session_thread` |
| Settlement | Every adapter-initiated kill of a process with bound work settles that work (turns and every bound Session's Activities) with its reason (the runtime-update notice, or a disable's), and only that work: turns running when settlement begins that still own their gate, and Activities started by this agent's own generations. | `_end_bound_work` via `_stop_runtime(settle_reason=)`, `force_end_runtime_work` |
| Fence | Once a teardown begins, no durable owner commits to its generation: the retirement is reserved before the final drained check, settlement, and stop, and aborted when any of them declines or fails. | `_stop_runtime` |
| Hub scope | The gateway credential is revoked exactly when this agent's last Hub process in the directory is gone, a starting or failed-start one included; another agent instance's processes never share it. | `_runtimes`, `_retire_hub_scope_after` |
| Per generation | A decision about a generation reads only its own Sessions, turns, bindings, and process. | admission, drained check, eviction, End |
| Session serialization | Turn admission, End, `/new`, and resume preparation run under the Session's lifecycle lock. | `session_lifecycle` |
| Spec identity | Equal digests are interchangeable processes; the credential identity covers the file store only, and names an account by its ChatGPT account id or, in an older bag without one, by its id_token subject or email, never by a token. | `_launch_spec_digest`, `codex_credential_identity` |
| Eventual teardown | Every started process is eventually stopped: drained retiring ones, the cap's victim, failed starts, and survivors of a failed or cancelled stop. | core reconciler, sweep, `_readopt` |

Two documented exceptions: shutdown ends processes without the
runtime-update notice, and a process ending unbinds its Sessions without their
locks. That unbinding is synchronous and checks generation identity, so it
never drops a binding that already moved; taking Session locks in the
reconciler would deadlock against failure replacement, which waits for the
reconciler while holding one.

## Turn admission

Under the Session lock, `handle_message`:

1. takes the router snapshot and resolves the launch from it;
2. builds the spec;
3. retires every unusable attached generation, then calls `unit.acquire(spec)`,
   and checks the acquired process again, since it may have died while the
   spec was prepared;
4. keeps that binding until the turn is registered or has failed;
5. if the Session's thread is loaded in another generation, moves it there
   (`_move_session_to`).

The move, `_release_session_thread`, is the one release primitive. It returns
only on proof of release; anything less raises
`CodexThreadReleaseUnavailableError`, a visible failure with the input held for
an explicit retry, and leaves the binding in place:

- If that process is being torn down, wait for the teardown: its exit releases
  the thread, and the Session's next turn cannot be among the work the
  teardown settles.
- If the Session's own turn is still running there, interrupt it. Only an
  accepted interrupt, or a turn or process that already ended, lets the move
  go on; the interrupted request is then settled locally, because
  unsubscribing ends this connection's view of that turn. A refused interrupt
  changes nothing.
- Send `thread/unsubscribe`, then wait for `thread/closed` from that process,
  bounded at 15 s.
- On a timeout, `thread/loaded/list` decides. A thread still loaded fails the
  move.
- The process's exit, by its return code, counts as released. A closed stdout
  reader does not: the child can outlive it and keep the writer lock, so the
  move waits for the exit within the same bound.

The turn then resumes, starts, or forks its thread through
`_open_session_thread`, which binds whatever thread ended up loaded. A resume
claims its thread in the target process before the RPC, so one whose answer is
lost is still released there before any other process resumes it. A new or
forked thread is persisted before the Session uses it, so a failed write never
leaves a conversation out of resume history.

A cancelled admission clears its pending turn start, so it never pins its
generation.

Notifications arrive per generation. `thread/closed` resolves a pending
release. A thread-level event without a turn id is dropped when its Session is
no longer bound to the emitting generation. A turn that completes on a
non-current generation schedules a reap.

The fork boundary is read through the generation that holds the source thread
while that process runs, even if it stopped answering: reading it then fails
closed, while any other process reports a live turn as `interrupted`. Fork
metadata names the durable source Session, not its base session.

## Stop and reap

`stop(generation, force) -> bool` is the only teardown hook, and the core's
reconciler is its only caller:

- A **graceful** stop declines while `_generation_drained` is false. That is
  the case while a bound Session has a live turn on a running process, or while
  the ownership snapshot blocks replacement:
  - `blocks_transport_replacement` for a running process;
  - `blocks_dead_transport_replacement` for one that exited.

  Drained is decided twice: once outside the fence, so a busy generation never
  blocks admissions, and again inside it before the stop.
- A **forced** stop at the cap settles the generation's work through
  `_end_bound_work`, then ends the process. `force_end_runtime_work` captures
  the exact turns before its first await, so a Session's next turn, admitted
  while the settlement runs and waiting for the teardown, is never cancelled
  with them. A registered turn whose gate a later turn already took, such as
  one the liveness monitor settled after the reader closed, is not settled
  again. A process whose reader closed while the child runs can never
  report its work again, so it is stopped the same way. Shutdown ends
  processes without the notice.
- `_stop_runtime` owns the fence. Only after a successful stop are the
  generation's Sessions unbound and its Hub scope revisited.

Ownership targets: the current generation answers for every durable Session in
its directory, as before. A detached or retiring generation answers only for
the Sessions it still holds.

Reap triggers:

- the controller sweep (`reap_runtime_generations`, every 60 s), which also
  retires processes that exited or stopped answering and retries the stop of a
  child that outlived its own failed start;
- a turn completing on a non-current generation;
- a Session moving off a generation;
- the release of the last binding.

Idle eviction judges each generation only by the Sessions whose threads it
holds. The current generation keeps the idle timeout and the two ownership
snapshots, then calls `unit.retire` and waits for `unit.settled()`. The
stuck-active backstop applies to every generation: it settles only the exact
stuck turn, under the Session's lifecycle and after the terminal emission, and
then retires that generation, so a native turn that may still run ends with
its process once its neighbours finish.

Native-credential migration, End, and the exclusive refresh need processes gone
outside the core's own decisions. They use `_stop_generations_now`:

- After the final synchronous check, it detaches every selected generation
  before the first stop awaits. A turn arriving meanwhile starts its own
  generation and cannot bind to one about to be killed.
- End settles through `_end_bound_work`. Migration and the exclusive refresh
  pass `settle=False`, because `migration_guard` and the coordinator already
  settled the backend.
- A generation whose stop fails, or that a cancellation left unstopped, is
  adopted back as retiring with its Sessions still bound, so a later call or
  sweep can retry.

Failure replacement instead releases its own binding and retires the broken
generation, which is atomic with admission. The process then stops only
through the drained check, so a neighbour's running turn is never killed.

## End, `/new`, and resume

`end_session` owns End from Running Agents, under the Session's lifecycle:

- It acts on the directory whose app-server holds the Session's thread, not on
  the Session's configured working directory.
- If no other Session is bound to that directory's processes, they stop, and
  the ending Session's own turn and Activities settle first.
- Otherwise only its thread is released, which interrupts its own turn first
  and fails rather than forget a turn Codex did not stop. That includes a
  process the reconciler already detached: its stop may still decline, so End
  waits out that teardown and proves the release instead of killing it.
- Its state is cleared only after that succeeded, so a failed End stays
  retryable; Running Agents reports the failure even after a successful
  canonical stop.

`clear_sessions` (`/new`) holds every cleared Session's lifecycle, releases
each thread first, and clears the durable and in-memory mappings only after
every release succeeded. Those mappings are cleared per session key, so a
Session of that key that started meanwhile is locked and released too before
anything is cleared. `prepare_resume_binding` releases the thread under the
lifecycle; the core rewrites the mapping right after, with no await between.

## Model Hub gateway scope

All Hub generations of a directory share its request-scoped gateway
credential. Per-turn routes ride on `responsesapiClientMetadata`. The scope is
retired only when the directory's last Hub process ends.

The scope belongs to one agent instance (`<cwd>#<instance>`), and so does each
generation's activation and ownership key (`<cwd>#<instance>.<serial>`): a
re-enabled agent numbers its generations from 1 again while a disabled agent's
retried teardown may still reserve its own. Any failure after a spawn, in the
start itself or in the setup that follows it, leaves the process marked for
the sweep. A disable whose
teardown failed keeps the old agent's processes until the idle sweep's retry
stops them, while a re-enabled agent already runs its own in the same
directory. The old agent revoking its scope with its last Hub process therefore
never revokes the credential the new agent's processes hold (RUNTIME-GEN-016).

## Triggers

| Hook | Behavior |
| --- | --- |
| `renew_runtime(config, config_save=False)` | Adopts the config. A plain `agents.*` save (`config_save=True`) needs nothing more, because the binary and extra arguments are already spec inputs. Every other caller bumps the renewal epoch. Nothing stops or waits. |
| `adopt_model_hub_catalog()` | Drops the prepared catalogs; the next Hub turn prepares from its own snapshot. |
| Hub/Direct mode switch | The core commits the mode and calls `adopt_model_hub_catalog()`. The mode decides the turn's launch, so it is a spec change: the next turn starts on a process for the new mode, while a running Hub turn finishes on its own process and keeps the gateway credential until it ends (RUNTIME-GEN-016). |
| `shutdown_runtime(settle_reason=None)` | Service shutdown, probe teardown, and disabling the backend. A disable passes `settle_reason="backend_disabled"`: every forced stop then settles the turns and Activities bound to its generation with that reason before it kills the process, so this agent's work is settled even when the core's own interrupt failed. Activities are ended by the stopping generation's activation identity, never by runtime key or bound Session, because after a re-enable a new agent can run the same Session in the same directory under the same key, and an Activity can outlive its turn or its Session's thread move; turns settle only while they still own their gate. Service shutdown and probe teardown pass no reason and show no notice. A turn that captured the agent before the routing change fails visibly with `error.agentRuntimeRetired` and starts no process (RUNTIME-GEN-006). It raises while any app-server survives; a Session bound to a survivor keeps its state, so the retried teardown settles whatever is still bound and stops it. Re-enabling constructs a new agent. |
| `refresh_runtime_config` / `refresh_auth_state` | Kept only for the exclusive `migration_guard` cutover: stop every generation. |
| `retire_for_native_migration` | Refuses while any generation is bound or not drained; otherwise ends every process and requires it to exit. |
| `prepare_resume_binding` | Releases only the resumed Session's thread; the process keeps serving others. |
| `end_session` (End) | See End above. |
| `probe_connection` | Binds, never acquires, a live generation whose spec equals the current direct spec. The probe never promotes a generation or starts one in a working directory. |

## Deleted

- The 30 s runtime-change wait, `CodexRuntimeChangeBlockedError`, and
  `error.codexRuntimeChangeBlocked`.
- `_interrupt_active_turn_before_runtime_change`. Its local settle step lives
  on in the move.
- `CodexTransport.runtime_fingerprint` and every fingerprint comparison.
- `_get_or_create_transport`, the per-cwd lock, activity, and inode maps, the
  stale-cwd branch, and the probe-cwd counter.
- The single-slot catalog cache and `invalidate_model_hub_runtime`.
- The dead `_is_transport_evictable`.

## Defects fixed

| ID | Defect | Pre-fix failure |
| --- | --- | --- |
| RUNTIME-GEN-010 | A busy directory held a new turn on a new spec | 30 s wait, then `CodexRuntimeChangeBlockedError` |
| RUNTIME-GEN-012 | A busy Session's own turn was interrupted before the replacement was known to proceed, so both turns were lost while a neighbour ran | interrupted, then refused |
| RUNTIME-GEN-014 | A direct launch with Model Hub enabled lost the desktop-managed environment: `build_codex_hub_launch` answers `None` for a non-Hub channel, and the adapter used that as the environment | `env=None` |
| RUNTIME-GEN-015 | Identical `direct` and `native_cli` launches replaced each other | two processes |

These four were run against the base adapter with one harness built through
the real `__init__`. They fail there for the reasons above and pass here.
RUNTIME-GEN-011, 013, and 016–018 cover behaviour that only exists with
generations. RUNTIME-GEN-019 is the opt-in real-binary contract
(`-m e2e_model_hub`).

## Open risks

- **Version window.** The writer lock and `thread/unsubscribe` were verified
  on codex-cli 0.159.3, and RUNTIME-GEN-019 guards upgrades.
  - A Codex without the lock resumes freely.
  - One without `thread/unsubscribe` fails the move with the held-input error
    until its old process ends.
- **No eager pre-start.** A directory has no single desired spec when its
  Sessions use different channels, so a new spec pays about one app-server
  start at its next turn.
- **Pre-existing race.** A turn can resolve its Hub token just before the
  directory's last Hub process retires the scope. It then fails visibly, as it
  did before.
- **Core resume order.** Resume sends its confirmation and commits channel
  routing before the backend prepares, so a thread that stays loaded shows the
  confirmation and then the failure.
- **Survivors of a failed stop.** The exclusive refresh logs a stop failure
  and goes on; the survivor is uninitialized, so it is retired and reaped, and
  migration's exit check backstops it. Shutdown drops the Session bindings of
  a process that survived both stop attempts, then raises, so a disable's
  teardown is kept and retried on every idle sweep until the process is gone
  (RUNTIME-GEN-006).
- **Untracked writers.** A thread still held by a process Avibe does not track,
  such as a wedged child of an earlier controller, fails its resume as
  unavailable rather than as held.
