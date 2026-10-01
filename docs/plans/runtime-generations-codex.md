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
thread is loaded there; `_unbind_session` clears both together. Every
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
  `account_id`. Tokens are excluded: a running app-server reloads `auth.json`
  itself on a 401 for the same account (`UnauthorizedRecovery` in
  `codex-rs/login`). So only an account, key, mode, or sign-in change needs a
  new process.
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

## Turn admission

Under the Session lock, `handle_message`:

1. takes the router snapshot and resolves the launch from it;
2. builds the spec;
3. calls `unit.acquire(spec)`;
4. keeps that binding until the turn is registered or has failed;
5. if the Session's thread is loaded in another generation, moves it there
   (`_move_session_to`).

The move:

- If the Session's own turn is still running there, interrupt it and settle
  the interrupted request locally. Unsubscribing ends this connection's view
  of that turn.
- Send `thread/unsubscribe`, then wait for `thread/closed` from that process,
  bounded at 15 s.
- On a timeout, `thread/loaded/list` decides. A thread still loaded raises
  `CodexThreadReleaseUnavailableError`: a visible failure with the input held
  for an explicit retry.
- A process that has exited counts as released.

The turn then resumes, starts, or forks its thread through
`_open_session_thread`, which binds whatever thread ended up loaded.

Notifications arrive per generation. `thread/closed` resolves a pending
release. A thread-level event without a turn id is dropped when its Session is
no longer bound to the emitting generation. A turn that completes on a
non-current generation schedules a reap.

The fork boundary is read through the generation that holds the source thread.
Fork metadata names the durable source Session, not its base session, and any
other process reports a live turn as `interrupted`.

## Stop and reap

`stop(generation, force) -> bool` is the only teardown hook, and the core's
reconciler is its only caller:

- A **graceful** stop declines while `_generation_drained` is false. That is
  the case while a bound Session has a live turn on a live process, or while
  the ownership snapshot blocks replacement:
  - `blocks_transport_replacement` for a live process;
  - `blocks_dead_transport_replacement` for an exited one.

  A turn on an exited process does not block.
- A **forced** stop at the cap settles the generation's running turns through
  `AgentService.force_end_runtime_work` with the runtime-update notice. It also
  settles the Activities of every Session bound to the generation, since an
  Activity can outlive its turn. Then it ends the process. Shutdown ends the
  process without that notice.
- Either way, `_stop_runtime` reserves the activation identity's retirement
  before the process stops, so late owner commits are fenced. Only after a
  successful stop are the generation's Sessions unbound and its Hub scope
  revisited.

Ownership targets: the current generation answers for every durable Session in
its directory, as before. A detached or retiring generation answers only for
the Sessions it still holds.

Reap triggers:

- the controller sweep (`reap_runtime_generations`, every 60 s);
- a turn completing on a non-current generation;
- a Session moving off a generation;
- the release of the last binding.

Idle eviction of the current generation keeps the idle timeout, the two
ownership snapshots, and the stuck-active backstop. It then calls
`unit.retire` and waits for `unit.settled()`.

Native-credential migration, End from Running Agents, and the exclusive
refresh need processes gone outside the core's own decisions. They use
`_stop_generations_now`:

- After the final synchronous check, it detaches every selected generation
  before the first stop awaits. A turn arriving meanwhile starts its own
  generation and cannot bind to one about to be killed.
- If a stop fails, the still-running process is adopted back as retiring, with
  its Sessions still bound, so a later call or sweep can retry.

Failure replacement instead releases its own binding and retires the broken
generation, which is atomic with admission. The process then stops only
through the drained check, so a neighbour's running turn is never killed.

End on an app-server that other Sessions still use releases only the ending
Session's thread (`release_session_runtime`). Otherwise the thread would keep
Codex's writer lock in that process.

An unusable current generation is retired before a new turn acquires:

- one that exited;
- one that is alive but uninitialized after a request timeout.

New turns get a fresh process. A replaced cwd inode changes the spec.

## Model Hub gateway scope

All Hub generations of a directory share its request-scoped gateway
credential. Per-turn routes ride on `responsesapiClientMetadata`. The scope is
retired only when the directory's last Hub process ends.

## Triggers

| Hook | Behavior |
| --- | --- |
| `renew_runtime(config, config_save=False)` | Adopts the config. A plain `agents.*` save (`config_save=True`) needs nothing more, because the binary and extra arguments are already spec inputs. Every other caller bumps the renewal epoch. Nothing stops or waits. |
| `adopt_model_hub_catalog()` | Drops the prepared catalogs; the next Hub turn prepares from its own snapshot. |
| `refresh_runtime_config` / `refresh_auth_state` | Kept only for the exclusive `migration_guard` cutover: stop every generation. |
| `retire_for_native_migration` | Refuses while any generation is bound or not drained; otherwise ends every process and requires it to exit. |
| `prepare_resume_binding` | Releases only the resumed Session's thread; the process keeps serving others. |
| `retire_unowned_session_transport` (End) | Ends the directory's processes once the ending Session was their last user. |
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
