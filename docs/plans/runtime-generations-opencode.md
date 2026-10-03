# OpenCode on Runtime Generations: Adapter Design

Status: implemented on the core contract at 32f655d2e. The sections below
are the Phase 1 design. Their line references are to
`refactor/runtime-generations` at 1eec27fab.

## How the implementation differs from the Phase 1 design

- The adapter's generation set is `OpenCodeRuntime` in `client_manager.py`.
- The core has no idle hook. `stop(generation, force)` declines a graceful
  stop while the process still has requests in flight or native run markers.
  The set then retries it on the next release or sweep.
- Leases are core bindings plus an adapter timer. Their expiry is persisted
  in the generation record. A release is routed by lease id to the runtime
  holding the lease, even after a disabled backend's agent was unregistered
  while that lease drains. Only a lease that no runtime has adopted yet goes
  to the registered agent, which adopts it first. Each lease's TTL covers the
  longest its holder may legitimately need. For the provider probe, that is
  the directory-bootstrap ceiling plus the probe's own timeout.
- Adoption keeps the newest adopted generation whose recorded digest equals
  the current spec as current. Every other adopted generation retires and
  stops once its restored work drains.
- `renew_runtime(config, config_save=True)` renews nothing. Every
  `agents.opencode` field is read per turn except the CLI path, and the CLI
  path's binary identity is already a spec input. Credential flows, manual
  Restart, and installs bump the renewal epoch.
- A forced refresh cancels running work and retires every generation. This is
  the coordinator's interrupt path, kept until it is deleted.
- Adoption skips generations that a runtime of this controller process is
  starting or has attached. Those belong to the runtime of an OpenCode backend
  that was disabled and enabled again, and that runtime stops them itself. The
  process keeps this ownership in memory, marking an adopted generation only
  once a runtime attaches it, so an adoption cut short leaves the rest to its
  retry. The record has no `owner_pid`, and adoption writes nothing except the
  conversion of a legacy record. Adoption applies resource governance to each
  adopted generation, as a start does.
- Disabling OpenCode is the user's own interruption (owner decision,
  2026-10-03). The core interrupts the backend's turns and Activities with the
  disabled notice, unregisters the agent, and awaits
  `shutdown_runtime(settle_reason="backend_disabled")`. That call stops every
  generation at once with a forced `stop_all`, and each forced stop settles
  the work bound to its process with that reason. The core's backend-wide
  interrupt is only the fast path: it may fail, and a retry after OpenCode is
  enabled again skips it, so the adapter's own settlement is the guaranteed
  one. A turn that raced the disable fails with the localized
  `error.agentRuntimeRetired`. Re-enabling builds a new agent. Every other
  configuration change still renews in place without interrupting anything.
- A controller that starts with OpenCode disabled stops the servers a crashed
  controller recorded, through `stop_recorded_servers_sync()`, the same
  function as `vibe stop`. It honors ownership here, process identity, and the
  desktop foreign-Runtime refusal. Their durable polls stay durable until
  OpenCode is enabled again.
- A UI lease release reaches the runtime holding the lease, even after its
  agent was unregistered, or else the registered agent, which adopts first.
- Explicit shutdown stops only the generations a runtime of this controller
  started or adopted (`stop_owned_generations_sync()`). Another desktop
  Runtime's generations, sharing this state directory, keep running.
- Bindings, and so leases, count as active runtime work, so maintenance such as
  a CLI replacement waits for a leased sequence between its requests.
- Work outside a turn (`current_server()` and UI leases) is admitted the way a
  turn is. It waits while a native migration drains the backend, and counts as
  active until it is bound.
- A restored poll bound to a generation other than the one it names is
  rewritten to name the generation it is bound to.
- See section 12 for the lifecycle invariants and the audit.
- A record that cannot be parsed is kept and never counted as dead. Its
  process may still run on a port that nothing else records.
- A failed record write never leaves a record protecting less than its process
  runs. A run marker or lease is persisted before its work starts. A turn
  clears its run marker together with its durable poll, so when the write
  fails both stay and a later restore retries. Any other change that only
  releases protection takes effect at once. If its write fails, the record
  stays stale until the next sweep rewrites it. A crash before that sweep only
  keeps the process until the stale lease expires, or until adoption drops a
  marker that no durable poll backs.
- `vibe stop` forgets every record whose process already ended, with its
  overlay copy, as shutdown and adoption do.
- The renewal epoch is persisted under `runtime/opencode/renew_epoch`. A
  generation retired by a renewal therefore never serves again after a crash.
- Readiness and adoption accept a listener that is the spawned process or one
  of its descendants, as with npm shims and Windows `.cmd` wrappers.
- The two-version gate in section 11 passed (RUNTIME-GEN-OBS-020), so a
  version change can coexist.
- Status, abort, and prompts are confirmed to be per process
  (RUNTIME-GEN-OBS-021).

## Worked example

A user replaces the `openrouter` API key in Settings while session `S1` runs a
long OpenCode turn on generation `G1`.

1. The save writes `~/.config/opencode/opencode.json` and signals the
   controller, which advances the snapshot. Nothing stops.
2. `S2` sends a message. Its turn computes the launch spec. The user-config
   digest differs from `G1`'s, and `S1` is bound to `G1`. The core therefore
   starts `G2` on a new port (serialized), marks `G1` retiring, and binds `S2`
   to `G2`.
3. Every call for `S2`'s run goes to `G2`'s port: prompt, status polls,
   steering, and abort. `S1`'s calls keep going to `G1`'s port. Both processes
   write the one SQLite database.
4. `S1`'s turn ends. Its run marker is cleared in `G1`'s record and its binding
   is released. `G1` is now idle and retiring, so it is stopped: the process
   tree is killed and the record deleted.
5. `S1`'s next turn binds to `G2` and continues the same native session from
   the shared database.

## Facts this design relies on

- Verified by the orchestrator: two servers can share one OpenCode home. This
  covers the WAL database, cross-server continuation, and concurrent prompts.
  Simultaneous fresh starts fail with `database is locked`.
- Verified here (hermetic probe, OpenCode 1.18.33, temporary `HOME` and XDG
  directories): `opencode serve --port=0` listens on the default 4096, not on
  an ephemeral port. Avibe must choose each port itself.
- Verified here: a second `opencode serve` on a port that is already taken
  exits at once with `ServeError`, and the first stays healthy. A health probe
  on that port right after a spawn can therefore be answered by the other
  process.
- Verified here: on macOS, psutil 7.2.2 `Process.net_connections()` proves
  that a same-user process is listening on a port.
- From the code: the adapter has no SSE event stream. Live turns and restored
  polls poll over HTTP through the server object they are handed
  (`agent.py:1869-1881`, `agent.py:3027`, `poll_loop.py:922`). "Per-generation
  event streams" therefore means handing each poll its own generation.
- Assumed, not probed: `/session/status`, abort, and `prompt_async` act on
  each process's in-memory run state, while sessions and messages live in the
  shared database. The design does not depend on this, because every call for
  a live run goes to that run's generation.

## 1. Objects

### `OpenCodeServerClient` (`server.py`)

The HTTP surface for one base URL. It holds everything at
`server.py:2206-2873`:

- directory readiness and sessions;
- prompts, messages, status, and abort;
- catalogs, auth, and OAuth;
- diagnostics and the config readers.

It also holds the loop-aware HTTP session (`server.py:516-548`). It is built
from `base_url`, `request_timeout_seconds`, and `model_hub_provider_ids`
(which feed `_public_opencode_catalog`). It owns no process and has no
`ensure_running`. The UI process uses it directly with a leased URL.

### `OpenCodeGeneration(OpenCodeServerClient)` (`server.py`)

One `opencode serve` process. Its state:

- identity: `generation_id`, `pid`, `process_created_at`, `port`;
- launch: `spec` (digest and a non-secret summary), `record_path`, and
  `caller_context_path`;
- work: `active_run_sessions`, the in-flight request count, and persisted
  leases;
- the activation `identity`;
- `_process` when this controller started it;
- the observed-exit state for resource diagnostics, moved from
  `server.py:306-380`.

Its methods:

- `mark_run_active` / `mark_run_inactive`: the record read-modify-write from
  `server.py:1006-1049` and `1125-1165`, without the overlay-reservation
  parameter;
- `request_scope`: the counter from `server.py:1051-1061`, without the
  pending-restart branch.

It subclasses the client, so every consumer that takes a "server" keeps
working unchanged:

- `_SteeringAwareOpenCodeServer` (`agent.py:189-707`);
- the poll loop;
- `OpenCodeSessionManager` (`session.py:212`, `338`, `407`, `448`).

### `OpenCodeGenerations` (`client_manager.py`)

This replaces `OpenCodeClientManager` (`client_manager.py:1-55`). It is the
adapter half of the core generation set for the single unit
`opencode:instance`:

- `launch_spec(snapshot) -> OpenCodeLaunchSpec` (section 5);
- `start_generation(spec) -> OpenCodeGeneration` (section 2.3);
- `stop_generation(generation, force)` and `generation_idle(generation)`
  (section 4);
- `can_coexist = True`;
- `adopt_recorded()` (section 2.2), `generation(id)`, and `generations()`.

## 2. Processes and records

### 2.1 Generation record

Each record lives at
`paths.get_runtime_dir() / "opencode" / "generations" / f"{generation_id}.json"`
and is written with `write_atomic` (mode 0600).

```json
{
  "schema": 1,
  "generation_id": "ocg_<16 hex>",
  "pid": 123,
  "process_created_at": 1790000000.12,
  "host": "127.0.0.1",
  "port": 52123,
  "started_at": 1790000000.0,
  "spec_digest": "<sha256>",
  "binary": {"path": "/abs/opencode", "version": "1.18.33"},
  "caller_context_path": "/abs/runtime/opencode_caller_context.json",
  "model_hub_overlay_hash": "<sha256> | null",
  "model_hub_overlay_provider_ids": ["..."],
  "active_run_sessions": ["ses_..."],
  "leases": {"lease_id": 1790000960.0}
}
```

- The record is written right after spawn, as `_write_pid_file` is today
  (`server.py:2052-2056`). A crash during start therefore leaves a record that
  adoption cleans up.
- Run-marker and lease changes update it under the generation's own lock.
- It is deleted only after the process is proven gone.

### 2.2 Adoption after a controller restart

`adopt_recorded()` runs once, lazily, before whichever comes first of the
first acquire, `restore_active_polls`, `prepare_runtime_restart`, or an
ownership snapshot. For each record:

1. **Prove ownership.** The pid is alive, and `runtime.process_create_time(pid)`
   equals `process_created_at` (today's `_pid_file_proves_owned_server`,
   `server.py:382-393`). The command line is `opencode serve ... --port=<port>`
   (`server.py:1566-1569`).
2. **Check health and the port.** `/global/health` answers on the recorded
   port, and psutil shows this pid listening on it. This replaces the
   Linux-only `/proc` walk at `server.py:1572-1616`.
3. **Proven and healthy:** adopt it with its recorded spec and attach an
   activation identity. Reconcile `active_run_sessions` against the durable
   active polls that name this generation. This generalizes
   `server.py:735-780`.
4. **Proven but unhealthy:** terminate its tree (`server.py:1701-1793`) and
   delete the record, as `_cleanup_orphaned_managed_server` does today
   (`server.py:1833-1846`).
5. **Not proven** (dead or reused pid): delete the record and never send a
   signal.

An adopted generation becomes current only when an acquire's desired spec
equals its spec. The newest match wins, and every other adopted generation
retires.

The released shape is `logs/opencode_server.json`, which every released
version writes (`server.py:1080-1117`). Adoption converts a proven, live
legacy server into a record whose `spec_digest` is `"legacy"`, so it never
becomes current. The record keeps that file's `active_run_sessions`, and the
legacy file is then deleted.

- Trust follows today's rules for that file: the birth identity when it is
  present, otherwise the command line and port, as in
  `terminate_instance_sync` (`server.py:2129-2179`).
- A legacy file with a dead pid is deleted.
- Load fixtures cover each released shape: with and without
  `process_created_at`, `caller_context_path`, and the overlay fields.

Unrecorded `opencode serve` processes are never adopted. Today's port scan
(`_find_opencode_serve_pids`, `server.py:1628-1684`) exists only because the
port is fixed.

### 2.3 Starting a generation

`start_generation(spec)` runs under the core's per-backend start serialization.
The start includes the wait for health, so one start's migration never
overlaps another's.

1. `ensure_plugin_installed()` (`caller_context.py:128-134`).
2. **Port.** Bind `127.0.0.1:0` in Python, read the port, close the socket,
   and pass `--port=<port>`. Never pass `--port=0`.
3. **Environment.** Use today's launch environment (`server.py:2011-2038`):
   - the desktop role, EXA, and external Skills off;
   - `server_environment()` and `AVIBE_OPENCODE_MODEL_HUB`;
   - in Hub mode, `OPENCODE_CONFIG_CONTENT` from the spec's overlay, and
     `OPENCODE_CONFIG` pointing at a per-generation copy
     `<generation_id>.overlay.json` (mode 0600).

   The copy replaces the shared overlay path, because `prepare_opencode_overlay`
   rewrites the shared file for every new overlay (`model_hub.py:1281-1291`)
   while older generations still run with that path.
4. Spawn the process, write the record, and apply resource governance
   (`server.py:1119-1123`).
5. **Readiness.** The generation is ready when `/global/health` is healthy
   and psutil shows this pid listening on the port. If the process exits
   first, another process took the port, so pick a new port and retry, up to
   three attempts. A timeout reaps the process, as at `server.py:2074-2090`.
6. Attach the activation identity with
   `registry.attach("opencode", f"opencode:{generation_id}")`. Each generation
   has its own resource key, so a reused port can never alias an older
   identity.

When a start fails, `OpenCodeGenerations` keeps the failed pid.
`_resource_failure_for_server` (`agent.py:987-1037`) uses it to attribute the
failure to cgroup pressure, as it does today.

## 3. Routing

### Binding map

`OpenCodeAgent._session_generations: dict[str, OpenCodeGeneration]` is keyed
by `base_session_id`.

- **Set:** when a turn's acquire returns, or when a restored poll resolves its
  generation.
- **Popped:** wherever `_active_requests` and the request session are popped
  (`agent.py:1301-1304`, `3123-3125`).

Every session-scoped lookup that today calls `_get_server()` or
`_client_manager._server_manager` reads this map instead:

| Call site | Today | New |
| --- | --- | --- |
| Turn start | `_get_server()`, then `configure_model_hub_overlay`, then `ensure_running` (`agent.py:1356-1365`) | `spec = launch_spec(snapshot)`, then `binding = await generations.acquire(spec)`, then `server = binding.generation` |
| Caller-context path | `caller_context_binding_path()` from the singleton PID file (`agent.py:1366`, `server.py:405-410`) | `server.caller_context_path` from the record |
| Activation identity | `_attach_server_activation` (`agent.py:887-908`) | `server.identity`, attached at start or adoption |
| Run marker | Singleton, with an overlay reservation (`agent.py:1748-1755`) | `server.mark_run_active(session_id)` |
| Durable active poll | `add_active_poll` (`agent.py:1760-1779`) | The same call, plus `processing_indicator["opencode_generation_id"]`. That bag already carries other OpenCode keys, so storage does not change. |
| Prompt and poll | `_SteeringAwareOpenCodeServer(server, state)` (`agent.py:1869`) | Unchanged; `server` is now the generation |
| Turn end | `_retire_active_poll(server, ...)` (`agent.py:2037-2044`, `2086-2100`) | Unchanged, then `binding.release()` |
| Superseding a running request | `_get_server()` abort and `wait_for_session_idle` (`agent.py:1266-1274`) | The old task's generation |
| Steering | `_client_manager._server_manager` (`agent.py:2176`) | `state.generation`, a new `_OpenCodeSteerState` field set at `agent.py:1827` and `2991` |
| Steer reconciliation | Singleton (`agent.py:2369`) | The bound generation. Finding a part is a database read, so an unbound session falls back to the current generation. |
| `/stop` and `clear_sessions` | `_get_server()` (`agent.py:2478`, `2517`) | The generation captured from the map before awaiting the task |
| `_abort_active_request` | `_get_server()` (`agent.py:2538`, `2546`) | The bound generation. With no binding there is no native run, so skip. |
| Restore | `_get_server()` (`agent.py:2635`, `2951`; `poll_loop.py:922`) | The resolved generation, passed explicitly as `run_restored_poll_loop(poll_info, server)` |
| Activation resolvers | Singleton identity (`agent.py:942-964`) | The bound generation's identity, else the current generation's, else `None` |

### Durable active-poll recovery

`restore_active_polls` (`agent.py:2598-2911`) resolves each poll's generation
in this order:

1. If `processing_indicator["opencode_generation_id"]` names an adopted
   generation, use it.
2. If the key is missing (the released shape), use the adopted generation
   whose `active_run_sessions` lists the session. In practice this is the
   converted legacy record.
3. Otherwise no live process owns the run. Verification reads use a lease on
   the current generation, as a dead server is handled today. The existing
   `session_still_active` logic then settles the run.

The restored task binds `_session_generations`, re-marks the run on that
generation (`agent.py:2951-2953`), and gets
`_SteeringAwareOpenCodeServer(generation, state)` (`agent.py:3027`).

### Caller context

The binding file stays one file keyed by native session id
(`caller_context.py:114`, `202-337`). That is safe across generations: a
session is active on at most one generation, and unbinding checks a token
(`caller_context.py:282-306`).

- Each generation launches with `server_environment()` and records that path.
- A turn uses its own generation's recorded path (`agent.py:1669`, `2758`).
- The plugin source digest is a spec input. A plugin change starts a new
  generation instead of refusing turns.

## 4. Retire when drained, the cap, and serialized starts

`generation_idle(G)` uses adapter evidence only. `G` is idle when all of these
hold:

- it has no live binding (turns, restored polls, or controller-side leases);
- its in-flight request count is zero;
- its `active_run_sessions` is empty after reconciliation;
- its record holds no unexpired lease (UI leases, section 6).

A per-generation ownership snapshot would list only the sessions bound to that
generation. That list is empty exactly when the conditions above hold, and
unbound sessions acquire the current generation, so the snapshot cannot veto
retiring a generation.

Exclusive operations still use ownership snapshots.
`runtime_ownership_snapshots()` (`agent.py:1097-1159`) returns one target per
generation with that generation's bound sessions. The current generation's
target keeps `include_all_backend_sessions=True`, so durable work that has not
acquired a generation still blocks `migration_guard`, as it does today.

`stop_generation(G, force)`:

- **Not forced:** call `registry.retire_if_current(G.identity, lambda:
  generation_idle(G))`, which replaces the predicate at `agent.py:910-940`. If
  it refuses, keep `G` running. Otherwise close the HTTP session, terminate
  the tree, and delete the record and the overlay copy.
- **Forced** (the cap, migration, or an explicit stop):
  1. Mark the sessions bound to `G` as interrupted by a runtime update.
  2. Cancel their request tasks with `_cancel_active_requests`
     (`agent.py:1225-1253`), filtered to `G`. The cancellation handler then
     emits the standard runtime-update notice instead of removing the ack
     silently. This mirrors `_user_stopped_sessions` (`agent.py:765`,
     `2118-2123`).
  3. Retire the durable polls, terminate the tree, and delete the record.

The core owns the cap of three and the serialized starts. The adapter declares
`can_coexist = True`.

Other lifecycle cases:

- **Backend disabled.** The core interrupts the backend's work, unregisters
  the agent, and awaits `OpenCodeAgent.shutdown_runtime(settle_reason=...)`,
  which stops every generation at once and settles the work bound to each with
  that reason.
- **Native migration.** `retire_for_native_migration` (`agent.py:1086-1089`,
  `server.py:550-593`) strictly stops every generation. It refuses while any
  generation has requests or runs, and keeps today's ownership proofs.
- **Controller shutdown.** `terminate_all_generations_sync()` covers tracked
  generations and records. It replaces `terminate_instance_sync` at
  `controller.py:2386-2393`.

## 5. Launch spec

`OpenCodeLaunchSpec` holds `digest`, `binary_path`, `overlay`, and a summary.
Two specs are equal when their digests are equal. The digest is the sha256 of
canonical JSON over these inputs:

| Input | Source | Notes |
| --- | --- | --- |
| Binary identity | `resolve_cli_path` on the snapshot's `agents.opencode.cli_path` | Covers the realpath and `(st_dev, st_ino, st_size, st_mtime_ns)`. Every install writes the file anew, so the stat signature changes on any upgrade or reinstall, including an in-place one. The version (`<binary> --version`, cached per stat signature) goes into the generation's record only, so the digest stays a pure function of the snapshot. |
| Hub overlay | `overlay.content_hash` and `provider_ids`; `None` in Direct mode | The same overlay object the turn resolves its model with. A Hub/Direct switch therefore changes the spec. |
| Managed policy | `_MANAGED_RUNTIME_POLICY_REVISION` (`server.py:72`) | A bump now starts a new generation instead of refusing turns. |
| Caller-context plugin | sha256 of `PLUGIN_SOURCE` | |
| User config | The global files OpenCode loads: `get_opencode_config_paths()` plus XDG `{config.json, opencode.json, opencode.jsonc}` | Each file contributes its canonical parsed JSON with `$schema` removed, its raw bytes when it cannot be parsed, or an absence marker. OpenCode may insert `$schema` on load, and that must not start new generations. |
| Credentials | Normalized `auth.json` | Each provider maps to its type, plus its key for `api` and `wellknown` entries, plus stable non-secret OAuth identity fields. OAuth `access`, `refresh`, and `expires` are excluded. OpenCode rewrites them whenever it refreshes a token, so a raw digest would start a new generation after every refresh. |
| Renew epoch | The core | Set by manual Restart, and by an Avibe-initiated OAuth success (section 6). |

**Snapshot discipline.** A turn should load the Model Hub configuration once.
Today there are three loads:

- `turn_mode` (`agent.py:1348-1355`, `model_hub.py:931-932`);
- `prepare_opencode_overlay` (`model_hub.py:1177-1178`);
- `resolve_opencode_overlay_launch`, for unlisted models
  (`model_hub.py:1085`).

Proposal: pass the core snapshot's Model Hub configuration into
`prepare_opencode_overlay(config)` and
`resolve_opencode_overlay_launch(overlay, model, config)`, and derive the turn
mode as `direct` when the overlay is `None`, `hub` otherwise. The overlay
digest and the model resolution then come from one load. `model_hub.py` is
shared, so this needs core agreement.

**One snapshot per launch.** `_prepare_launch()` reads every mutable launch
input in one synchronous step, before its first await. That covers the Model Hub
snapshot, the CLI path, the renewal epoch, the binary identity, and the user
config and credential digests (`OpenCodeRuntime.launch_inputs()`). The policy
revision and plugin source are constants of the Avibe build. The overlay is
derived from that Hub snapshot, and the spec is a pure function of the
snapshot and the overlay (`compute_launch_spec(inputs, overlay)`). Turns,
restored polls, `current_server()`, leases, and adoption all use this one
path. A save or renewal that lands while the overlay is prepared therefore
changes nothing about that launch: it stays on the generation its snapshot
names, starts no spare generation, and the next launch moves. After the
snapshot, only the generation's HTTP request timeout is read live; it is a
client setting, not a process input.

The same snapshot carries the `agents.opencode` settings object the CLI path
was read from (`OpenCodeLaunchInputs.settings`). After admission, the turn
reads no live `agents.opencode` field:

| Read | Disposition |
| --- | --- |
| `default_provider` and `default_reasoning_effort` (model resolution in `_run_turn`) | taken from the admission settings |
| `active_turn_timeout_seconds` and `error_retry_limit` (`run_prompt_poll`) | taken from the admission settings |
| the same two in `run_restored_poll_loop` | read once when the restore starts; a restored poll has no earlier admission |
| `request_timeout_seconds` (a generation's HTTP client) | read when the generation starts; always 60 from `to_app_config`, not a user setting |
| `controller.config.language` | not an `agents.opencode` field |
| channel and scope overrides | out of scope: a recorded follow-up, as for Codex |

**Check-to-spawn window.** Files are digested before the spawn. If a save
lands in between, the generation loads files newer than its label says. The
next turn sees the mismatch and starts one more generation. A generation never
runs files older than its label.

## 6. The UI process stops launching servers

Today these UI-process paths call `ensure_running`:

- `vibe/api.py:5902-5908`: model options;
- `vibe/api.py:11666-11697` (`_opencode_get_server`): providers (`12030`),
  model save (`12591`), and auth deletion (`12934`);
- `core/agent_auth_service.py:3524-3566` (`_opencode_server`): Web OAuth
  (`3602`, `3667`, `3697`) and the provider probe (`3227`).

None of them may launch a server after this change. Instead, they lease a
generation from the controller over control IPC (`core/internal_server.py`,
`vibe/internal_client.py`):

- `POST /internal/opencode/generation-leases` with `{purpose, ttl_seconds}`.
  The controller captures the snapshot, computes the launch spec, and acquires
  with a lease, which is a core binding with a TTL. It returns
  `{lease_id, generation_id, base_url, model_hub_provider_ids, expires_at}`.
  It returns 404 when OpenCode is disabled, and 503 when the Hub engine is
  down or the start fails.
- `POST /internal/opencode/generation-leases/{lease_id}/release`.

The lease expiry is persisted in the generation record. A controller restart
therefore keeps an OAuth flow's generation alive; today the UI's own server
also survives a controller restart.

The UI helper is `async with internal_client.opencode_generation(purpose,
ttl) as server`, which yields an `OpenCodeServerClient`. The controller-side
`AgentAuthService` uses the agent's local lease with the same shape.

| Flow | Change |
| --- | --- |
| Options, providers, model save, auth deletion | One lease per call (a 60 s request window), released in `finally`. Outside the controller a lease lasts `LEASED_REQUEST_TIMEOUT_SECONDS` longer than the window it is asked for, timed from before the controller grants it, and the leased client's request timeout is at most that constant. The client starts no request that its timeout could carry past the lease, so the controller never stops a generation under a running UI request; the TTL only stays behind a UI process that died holding the lease. `_opencode_lease` returns `None` only when OpenCode is disabled; a lease an enabled OpenCode cannot give raises, so no caller takes an unavailable runtime for a disabled one. Options and providers report the error. Model save fails closed, since it needs the live catalog to refuse a built-in duplicate. Auth deletion reports "OpenCode is unavailable" instead of "disabled". |
| Web OAuth | One lease from `_start_opencode_oauth_web` through the waiter. Authorize, the pasted-callback forward, and `wait_provider_oauth` must reach the same process, because the pending OAuth lives in its memory. The TTL is the flow timeout plus 60 s. The lease is released in the waiter's `finally` and in `_terminate_web_flow`. A success renews OpenCode. |
| Provider probe | One lease for the probe. Drop its `mark_run_active` and `mark_run_inactive` calls (`agent_auth_service.py:3332`, `3509`), which today write the controller's PID file from the UI process. |
| Provider save | Still calls `restart_backend("opencode")` (`api.py:12924`, and the other provider writes at `12444-13042`), which the core remaps to a snapshot advance. `_refresh_opencode_provider_catalog_async` then leases the current spec, which is a new generation if the save changed the digest. |
| Status | `_opencode_process_status` (`api.py:9852-9872`) reads the records and reports running when any proven generation is alive. |

## 7. Other consumers

- `vibe/cli.py:13872-13914` (`_stop_opencode_server`): iterate the records
  and the legacy file with the same per-pid checks. Return `True` when any
  server stopped.
- `core/services/running_agents.py:717-764` (`_end_opencode`): abort on the
  bound generation. When the last session ends, call
  `agent.retire_idle_generations()` instead of `retire_for_native_migration`
  on the singleton.
- `core/handlers/settings_handler.py:453-479` and
  `core/handlers/message_handler.py:548-562`: use
  `async with agent.catalog_server()`, a lease on the current generation.
- `core/agent_auth_service.py`:
  - the provider lookup (`796-805`) and the IM API-key install
    (`2112-2130`) use a lease;
  - `_refresh_opencode_server` (`2149-2160`) is deleted;
  - the disable branch (`2272-2280`) becomes `shutdown_runtime`;
  - the OpenCode branches at `2339-2346` and `2359-2364` move to the core
    path.
- `docs/CLI.md:681` and `708`, and `docs/CLI_ZH.md`: point the PID-file row at
  the records directory, and drop the `OPENCODE_PORT` row. That setting does
  not exist; the port is hard-coded at `config/v2_compat.py:182`.

## 8. Deletions

| Mechanism | Code |
| --- | --- |
| `PATCH /global/config` reload | `refresh_global_config` (`server.py:667-706`); `_merge_global_config_snapshot` and `_get_global_config_snapshot` (`server.py:2792-2812`); the reload branch of `refresh_runtime_config` (`agent.py:1171-1223`); `config_reconciler.py`, whose only caller is `server.py:2798`. |
| Deferred refresh | `_auth_refresh_pending`, `_auth_refresh_pending_port`, and `_pending_runtime_config` (`server.py:285-288`); `_restart_for_auth_refresh_locked` (`595-649`); `detach_after_deferred_refresh` (`651-665`); `reload_runtime_config`, `_set_runtime_config`, and `_apply_pending_runtime_config_locked` (`708-730`); `restart_for_auth_refresh` (`2181-2194`); the pending branches at `1054-1055` and `1863-1864`. |
| Overlay transition and reservations | The overlay fields (`server.py:290-304`); `MODEL_HUB_OVERLAY_DRAIN_TIMEOUT_SECONDS` (`66`); `set_model_hub_overlay_preparer` (`428-434`); `_configured_model_hub_overlay` through `_prepare_model_hub_launch_boundary` (`812-873`); `configure_model_hub_overlay` (`875-997`); `release_model_hub_overlay_reservation` (`999-1004`); the reservation parameter of `mark_run_active` (`1006-1037`); `OpenCodeModelHubOverlayRequiredError` (`238-239`); the agent's reservation plumbing (`agent.py:750-756`, `1323`, `1360-1364`, `1748-1755`, `2068-2084`, and the `_release...` calls). |
| Policy- and plugin-pending refusals | `OpenCodeManagedPolicyRefreshPendingError` (`server.py:234-235`); `_caller_context_plugin_refresh_pending` (`287`); the refusal block (`1860-1913`); `_pid_file_has_caller_context_binding`, `_pid_file_has_current_runtime_policy`, `_pid_file_was_started_by_current_process`, and `_pid_file_has_known_no_active_runs` (`1189-1208`); their display mapping (`agent.py:1064-1067`); the i18n keys `error.opencodeModelHubOverlayRequired` and `error.opencodePolicyRefreshPending` (`vibe/i18n/en.json:442-443`, `zh.json:442-443`). |
| #2328 compatibility checks | None on this base; #2328 is still an open draft. Nothing is ported from it. |
| Singleton and fixed port | `DEFAULT_OPENCODE_PORT` (`server.py:51`); `_instance`, `_class_lock`, `get_instance`, and `get_instance_if_managed_server_exists` (`249-250`, `436-501`); `_pid_file` (`281`); `_runtime_generation_token` and the replacement-retire helpers (`324-366`); `_is_port_available` and `_find_opencode_serve_pids` (`1618-1684`); `_cleanup_orphaned_managed_server` (`1795-1846`); `ensure_running` and `_ensure_running_with_current_overlay` (`1848-1947`); `stop_instance_sync` and `terminate_instance_sync` (`2156-2204`); the `port` field of `OpenCodeCompatConfig` (`v2_compat.py:53`, `182`). |
| Adapter refresh entry | `OpenCodeAgent.refresh_runtime_config` (`agent.py:1171-1223`) becomes `apply_runtime_config_change`: reload `opencode_config` and take no server action. A forced refresh, used only by the coordinator's interrupt path until that path is deleted, force-stops every generation. `prepare_runtime_restart` (`agent.py:1082-1084`) becomes the adoption trigger. |

## 9. Tests

The rule: each deleted mechanism loses its tests, each rewritten contract
keeps one test at its owner boundary, and each new behavior gets a test that
fails on the old code.

| File | Change |
| --- | --- |
| `tests/test_opencode_server.py` | **Delete** the tests for removed mechanisms: `refresh_global_config` (`1397-2149`); `restart_for_auth_refresh` (`2172-2277`); `request_scope` pending (`2455`); `reload_runtime_config` (`2469`); pending detach (`2598`); `get_instance` (`2493-2570`, `2652`); Hub launch refusals and rechecks (`506-565`); plugin and policy refreshes, deferrals, and refusals (`483`, `691-903`); stale-pid cleanup (`1021`); the unmanaged healthy server (`1049`); singleton generation replacement (`1069`, `1095`); the Windows port scan (`2151`); `terminate_instance_sync` (`243`, `267`); the overlay transition tests (`3350-3527`). **Rewrite** as generation tests: the start timeout (`198`); record run markers (`933-1002`); record birth identity (`954`); adoption with active runs and reused pids (`2289`, `2301`); `terminate_sync` (`2313-2341`); adoption and stop proofs (`2354-2445`); the observed exit (`2445`); resource governance (`2482`); overlay preparation as a spec input (`600`, `672`); the recorded caller-context path (`903`); the catalog projection with the generation's provider ids (`302`, `342`). **Keep** the HTTP and diagnostic tests (`175-192`, `288`, `1126-1377`, `2629-2675`, `2700-3189`, `3260`, `3317`). |
| New tests: spec | An in-place binary change (stat or version) changes the digest; this fails on the old code, which compared only the path. An OAuth token rewrite keeps the digest. An API-key change changes it. Inserting `$schema` keeps it. |
| New tests: process and routing | The launch never passes `--port=0`. Adoption covers every record plus the legacy file. A start on a taken port retries. A turn's status, abort, and steering go to its own generation while a newer one is current. A retiring generation stops when its last binding is released. |
| `tests/test_opencode_client_manager.py` | Replace both tests (`14`, `47`) with hook tests for `OpenCodeGenerations`. |
| `tests/test_opencode_config_reconciler.py` | Delete, with the module. |
| `tests/test_agent_auth_service.py` | Delete `1580` and `1761-2185` (refresh, global-config reload, and uncached-server attach). Add one test: a config change takes no server action, and the next turn gets a new generation. |
| `tests/test_ui_api.py` | `43-1546`: patch the lease helper instead of `OpenCodeServerManager.get_instance`. The catalog assertions stay. Delete `346` (the UI launch boundary). Rewrite `600` as a lease-error mapping. |
| `tests/test_web_oauth_flow.py` | `856-2172`: patch the lease. Add one test: authorize, callback, and wait share one lease, which is released on success, cancel, and expiry. |
| `tests/test_multi_platform_runtime.py` | Delete `59` and `91` (the localized refusals). Move `127`, `223`, `1179`, `1373`, and `3978` from overlay reservations and `ensure_running` to acquire bindings. |
| `tests/test_opencode_restore_polls.py` | `340-1114`: route through the poll's generation. Add one test: a poll in the released shape (no generation key) routes to the legacy record. |
| `tests/test_agent_steering.py` | `1306`, `1450`, `2706`, and `2920`: steer through the bound generation. `2706` covers "no binding". |
| `tests/test_vibe_cli.py` | `1007` and `1035`: stop every record plus the legacy file. |
| `tests/scenarios/auth_setup/test_auth_setup_scenarios.py` | `3037`, `4083`, and `4273`: use leases and renew-on-OAuth. Update the matching scenario IDs in `catalog.yaml`. |
| `tests/test_runtime_activation.py` and `tests/scenarios/model_hub/catalog.yaml` | Rewrite `242` for per-generation identity. MH-RUNTIME-002, 003, 004, and 007 (`catalog.yaml:503-538`) describe the deleted transition. Retire them and add IDs for "an overlay change starts a new generation; the old one finishes its run". |
| Smaller fake-server updates | `tests/test_opencode_stop_receipt.py:233` (binding release replaces reservation release); `tests/test_opencode_directory_bootstrap.py:188`; `tests/test_runtime_ownership.py:299` (per-generation targets); `tests/test_native_takeover_lifecycle.py:862`, `883`; `tests/test_desktop_runtime_stop.py:241`, `543`, `718` (dynamic-port argv, several servers); `tests/test_agent_backend_alive.py:135-221`; `tests/test_running_agents_service.py:1075`, `1109`; `tests/test_settings_handler.py:520-636`; `tests/test_message_handler_typing.py:361`; `tests/test_save_opencode_provider_auth.py:524`, `666`, `916`; `tests/test_native_writer_custody.py:290`; `tests/test_backend_connection.py:118`; `tests/test_native_login_single_flight.py:79`. |

## 10. What the adapter needs from the core contract

1. The spec object carries the adapter's launch inputs: the overlay content
   and the binary path. Equality is by digest only.
2. `acquire(spec, *, purpose, ttl=None)` returns a binding that pins its
   generation until released or expired. The binding works as an async
   context manager, and another task can release it.
3. `bind(generation)` binds a restored poll to an adopted generation without
   comparing specs. `adopt(generations)` registers adopted generations, with
   "the newest matching generation becomes current".
4. A decision on who emits the runtime-update notice on a forced stop. The
   adapter can emit it from the cancelled task's handler.
5. A renew epoch as a spec input, for manual Restart and Avibe-initiated OAuth
   success.
6. The snapshot carries the Model Hub configuration for
   `prepare_opencode_overlay` (section 5).
7. For backends that can coexist, in-place replacement starts the new
   generation before stopping the old one, so it never waits on a stop.

## 11. Risks and open items

- **Cross-version coexistence is unverified.** An upgrade while an old
  generation is busy runs the new version's database migrations under a
  running older version. Gate for Phase 2: a hermetic two-version probe in
  which the old version is mid-prompt while the new one starts, the old prompt
  completes, and the session continues on the new version. If the probe
  fails, the owner must decide whether a version change interrupts the old
  generation or waits for it to drain.
- **OAuth tokens are assumed to be read live.** The normalized credential
  digest assumes OpenCode reads OAuth tokens from `auth.json` on each request.
  Renew-on-OAuth-success covers Avibe's own logins either way.
- **Memory.** Up to three servers can run at once.

## 12. Lifecycle invariants and audit

This audit covers head `2b35e0739`. Each invariant below is a one-line rule. In
the transition table, every cell names the code path and states whether the
rule holds under normal flow, a failed write (W), cancellation (C), and
process death (D). A gap is a real violation and is fixed in the push that
follows the audit, with a regression that fails on `2b35e0739`. A known item
is a bounded consequence kept by design, and is also listed in the PR's
Known-by-design ledger. Gaps G4 and G5 came from Codex's review of
`2b35e0739`, on transitions the first version of this table missed. Gaps G6–G9
and the corrected cells came from an independent agent that checked every
"holds" claim against the code.

### Invariants

| ID | Rule |
| --- | --- |
| R1 | Every `opencode serve` process Avibe started has a record on disk before it can serve, until it is confirmed gone. |
| R2 | A record is removed only once its process is confirmed gone: stopped by Avibe, dead, or proven not ours. A record that cannot be read is kept. |
| R6 | A record of another desktop Runtime sharing this state directory is never adopted, stopped, forgotten, or overlay-cleaned here. `_record_is_ours()`, applied in `_recorded_processes()`, is the one gate every reader goes through. A durable poll whose run a live process of another Runtime executes is never bound, rewritten, or settled here. |
| R3 | A record never protects less than its process runs. A run marker or lease is written before its work starts. A release may lag, but a sweep rewrites it. |
| R4 | A generation's overlay copy lives and goes with its record. |
| R5 | No write recreates the record of a process that has stopped. |
| O1 | A live process is attached to at most one runtime in this controller. |
| O2 | Every recorded live process is attached to a runtime here, or is being started here, or stays unmarked so that an adoption here or a successor can take it. |
| O3 | The owned-here mark, the process's pid and create time, is set at spawn, before anything that can fail, the record write included. It ends only when a stop proves the process gone or its start proves it exited. Service shutdown stops every owned process, through its record or, without one, by that identity. |
| O4 | Every attached generation whose stop declined or failed is retried, by the controller's sweep or by its own runtime's sweep. |
| L1 | A lease holds one binding on its generation from grant until release or expiry. |
| L2 | Every lease ends by a release, its TTL, or a controller restart, where adoption re-holds what remains or drops what expired. |
| L3 | A release reaches the runtime holding the lease, whether or not its agent is registered. If no runtime holds it yet, the registered agent adopts first. |
| L4 | A lease's TTL covers its holder's longest legitimate wait. |
| L5 | A lease's expiry is in the record before the lease is handed out (R3). |
| B1 | A turn, a restored poll, a `current_server()` use, and a lease each hold one binding for their whole use, and release it exactly once on every exit. |
| B2 | No new work binds to a closed or stopped generation. A turn's prompt, status, abort, steering, and stop reach the generation it is bound to. |
| B3 | A bound generation is stopped only by a forced stop, which first settles the bound work, each session once across retries, with the stop's reason (a disable's, else a runtime update), and never another agent instance's work. |
| M1 | A native run is marked on its generation, and that marker persisted, before the run's first native write. |
| M2 | A turn clears its marker together with its durable poll: both go, or both stay. This is master's contract. |
| M3 | A marker that no live run or durable poll backs never keeps a process: adoption reconciliation and the re-reconcile in `_stop`. |
| S1 | New turns run on the generation whose spec equals the current spec. A renewal is persisted before it takes effect. |
| N1 | A native migration never overlaps the start of an OpenCode process: work outside a turn waits while the backend drains, and admitted work counts as active until it is bound. |
| P1 | A durable poll names the generation that runs its native turn, including after a restore binds it elsewhere. |

### Transitions

| Transition | Code path | Invariants and outcome |
| --- | --- | --- |
| Start: success | `start_generation`: overlay copy, spawn, mark owned, `write_record`, governance, `_wait_until_ready`; core `acquire` installs it current | R1, R4, O3, S1 hold. **Known K1:** a controller crash in the microseconds between spawn and the record write leaves an unrecorded process (R1). |
| Start: port taken | outcome `exited`: ownership and record dropped, same id retried on a new port | R1, R2, O3 hold. |
| Start: timeout or cancellation | terminate the spawned process; on exit remove the record, else hand the survivor to the runtime that started it (`on_survivor`), which keeps it owned, record and overlay included, and stops it at every later `reap()` and at `shutdown()` until it is gone | R1, R2, R4 hold. W: a failed record write aborts the start and terminates the process. C: holds for one cancellation. **Known K4:** a second cancellation inside the cleanup can skip the record removal (a stale record, later removed as dead) or, if it lands before the kill, leave the process recorded but unsignalled until `vibe stop`, shutdown, or the next adoption. **Gap G12:** such a survivor was dropped from ownership, so nothing here retried its stop and it ran beside later generations until service shutdown. Every process a runtime spawned is now retried until it is gone, unrecorded ones included (a failed record write), since the runtime holds the process itself. **Gap G14:** ownership began only after the record write, so a start whose write and termination both failed left a process owned by nothing but its runtime's next reap; a service stop before that reap never found it. |
| Start: runtime closed meanwhile | core `acquire` attaches it closed and retiring and kicks the reconciler | O2, O4 hold. |
| Adopt: fresh record | `adopt_recorded_generations` proves, health-checks, and returns it; `ensure_adopted` attaches them oldest first by process start, so the set's serials follow process age, and reconciles markers (W: write-behind), attaches, marks owned, applies governance, attaches activation, re-holds leases | O1–O3, L2 hold. C: cancellation lands only inside `adopt_recorded_generations` (the attach loop has no suspension point), so nothing is marked and the retry adopts everything. D: an unhealthy proven process is stopped; a survivor keeps its record. **Gap G7:** one failed 5 s health probe stops a proven process, which may be busy with runs that durable polls would restore. **Gap G9:** a recorded lease is re-held for `expires_at - now`, unclamped, so a backward clock step across the crash pins the generation for that long. **Gap G15:** generations were attached in record filename order, which is random, so the cap's force-stop of the lowest serial took an arbitrary adopted process instead of the oldest. |
| Adopt: legacy record | converted to a generation with spec `legacy`; the legacy file goes once the new record is written (write-behind) | R1, R3 hold. **Gap G3:** a crash between writing the converted record and removing the legacy file, or a failed removal, makes the next adoption adopt one process twice (O1). **Gap G6:** the same happens inside one controller: a runtime created by a re-enable re-adopts a legacy server that the closed runtime still owns, because ownership is known by generation id and a legacy record has none. A failed unlink also cleared `_supersedes`, so the removal was never retried. |
| Adopt: unreadable record | `_read_json_object` returns `None` and the record is skipped and kept | R2, R4 hold. Its process is excluded from adoption, shutdown, and `vibe stop` alike; only a log line reports it. This is by design: nothing can prove what it names. |
| Adopt: malformed field | a readable record whose field has the wrong type reads as one without that field: `_record_pid`, `_record_port`, `_record_number` (finite numbers only), and `_record_runtime_id` (an id, null, or unrecorded, so judged by its live process); a generation's id is its file name | R2, R6 hold. **Gap G17:** an unhashable `desktop_runtime_id` or `generation_id`, or a number too large for a float, raised in the gate, `_owned_here`, or the proof, so one corrupt record failed every adoption, lease, restore, and shutdown stop. |
| Adopt: dead or not ours | record and overlay removed without a signal | R2, R4 hold. |
| Lease acquire, in controller | `lease_generation` → `OpenCodeRuntime.lease`: core `acquire`, then `set_lease` (persisted first), then `_hold_lease` (timer and holder) | L1, L5 hold. W: the lease fails and its binding is released. C: no binding survives. |
| Lease acquire, IPC | `POST /internal/opencode/generation-leases` → same path | Same as above. A UI caller that gives up after the grant leaves the lease to its TTL (L2). |
| Lease renew | none by design; each holder sizes its TTL | L4 holds for realistic runs: OAuth uses the remaining flow budget + 60 s; the probe uses bootstrap + timeout + 60 s; model options makes at most about 5 × 10 s of requests under 60 s; catalog, model save, and auth removal each make one request bounded by its request timeout. A probe whose every request hits its own timeout can outlast its lease, and then fails anyway. A UI caller that gives up before the grant leaves a lease nobody releases, bounded by its TTL. |
| Lease release | in controller: the captured agent's `release_generation_lease`; IPC: `release_opencode_lease` (holder map, else each runtime agent adopts first, else the recorded lease is cleared, else 404); expiry timer → `release_lease` | L1–L3 hold. W: the drop is write-behind and flushed by the next sweep. After a force-stop, the record write is a no-op (R5). |
| Turn bind and unbind | `_run_turn`: acquire → `_session_generations` → `mark_run_active` (persisted first) → durable poll → prompt; `_process_message` `finally` releases | B1, B2, M1, M2 hold. C: release is synchronous bookkeeping in a `finally`. W: a failed marker write fails the turn before its first native write; a failed clear keeps marker and poll (M2). |
| Restored poll | `_bind_restored_poll` (adopts first; binds by generation id, else legacy marker, else current) → task → `finally` releases | B2 holds. A closed runtime refuses the bind, so the poll stays durable. **Gap G19:** each handed-off poll task waits for the restore to publish it; a restore cancelled, or failing to mark an earlier poll's session running, left the rest waiting forever, holding their binding and request, so later restores skipped them. **Gap G18:** a failed rewrite of the poll's generation was logged and the poll still ran, so a crash before it finished left it naming the old process and the next restore bound it to another server. **Gap G16:** a poll whose run another desktop Runtime's live server executes found no generation here, so restore started its own, rewrote the poll to it, and settled the run from a server that does not run it. **Gap G8:** `restore_active_polls` holds the binding across several awaits before the poll task takes it over. An exception in that stretch leaks the binding (B1), and a forced stop in it leaves the poll registering on a dead process (B3). **Gap G5:** a poll bound to a generation other than the one it names keeps the old name, so after another crash adoption drops the new generation's marker and the rebind goes to the wrong process (P1, M3). |
| Native migration | `BackendRestartCoordinator` guard: `begin_backend_drain` holds turns; it waits on `backend_runtime_active` → `runtime_has_active_turns`; `retire_for_native_migration` → `retire_all_strict`; `verify_idle` | **Gap G4:** `current_server()` and `lease_generation()` bypass turn admission, and an acquisition still starting a process is invisible to `runtime_has_active_turns`, so the guard can yield while a process starts (N1). |
| Retire: renewal | `renew()` persists the epoch first; the next turn's spec installs a new current and the old one retires | S1 holds. W: renewal raises without effect. |
| Retire: End-idle and strict migration | `retire_confirmed()` waits for the reconciler and reports stopped, draining, or failed | B3, O4 hold. |
| Retire: forced refresh | `_cancel_active_requests()` then `retire_all()` | B3 holds; leases keep their generation until released. |
| Disable | the core interrupts the backend's work, unregisters the agent, and runs `shutdown_runtime(settle_reason="backend_disabled")` → `OpenCodeRuntime.shutdown()` inside `AgentService.run_until_done`: a forced `stop_all` of every attached generation, each forced stop settling its bound work with that reason, then `stop_recorded_servers_sync()` for every recorded server no runtime here owns. It raises while any process survives, so the idle sweep retries it; a retry repeats both steps | B3 holds by the user's own interruption. L3 holds: a later UI release reaches the holder. W/D: a survivor fails the teardown, which is retried until it stops. **Gap G2:** a runtime that never adopted stopped without adopting, so a crashed predecessor's generations ran until shutdown (O2). Gap G1, a closed runtime that was never swept, ended with the drain path. **Gap G10:** an incomplete shutdown returned normally, so the teardown's retry was lost. **Gap G13:** the forced stop settled bound work only as a runtime update, so a disable whose core interrupt failed, retried after a re-enable that skips that interrupt, never settled it as disabled; it ended Activities by runtime key, which a re-enabled agent's Activities in the same session share; and a retry settled again a session an earlier attempt had settled, whose next turn may run on the re-enabled agent. |
| Cap force-stop | core `_next_victim` forces the oldest retiring generation; `_on_generation_stopping(force)` → `_interrupt_generation_work` settles the bound sessions' turns and, by the runtime keys the ownership snapshot gives them, their Activities; `stop_generation` | B3 holds; a lease holder sees its process end, by design. **Gap G11:** the forced stop passed no runtime keys, so the interrupted sessions' Activities and Runs stayed active after the process was killed. Every forced stop of the adapter goes through this one hook; `forget()` handles a process that already died, and the record-based stops touch only records no runtime here owns. |
| Controller restart | explicit: shutdown stops every generation this controller owns; next start adopts and restores polls on a new current. After a crash with OpenCode disabled, startup runs `stop_recorded_servers_sync()` | R2, B1 hold. **Gap S-1:** after a crash, a restart with OpenCode disabled never stopped its leftovers, which ran until the next stop or enable (O2). |
| Crash between steps | spawn→record (K1); record→ready (adopted or stopped); lease grant→hand-out (the successor honors it until TTL); marker→durable poll (marker reconciled away); kill→record removal (unproven, removed later); legacy write→legacy removal (G3) | R1–R3 hold except K1 and G3. |
| Process death | `acquire` replaces a dead current (`forget` → `stop_generation` cleans up); a dead retiring generation is cleaned on its last release | R2, B1 hold. |
| CLI stop | `stop_recorded_servers_sync(provenance)`: `forget_dead_records()`, then each proven record, through the one ownership gate, `stop_recorded_server_sync` | R2, R4 hold; a survivor keeps its record; exit stays non-fatal (#2242). |
| Shutdown | `stop_owned_generations_sync()`: stop each recorded generation a runtime of this controller owns, then each owned process with no record by its pid and create time (never one whose create time differs or cannot be read); another desktop Runtime's records are left alone | R2, R4 hold. **Known K5:** the loop keeps running briefly after this call, and a lease expiry or sweep there can rewrite a record of a process just killed (R5). The next adoption or `vibe stop` removes it as dead. |

### Fixes

| Gap | Fix | Regression |
| --- | --- | --- |
| G1 | moot: disabling stops every generation at once, so no closed runtime waits for a sweep. `reap()` still does not wait for a completed adoption | — |
| G2 | `shutdown()` stops a previous controller's servers by their records, after its forced stop of what it attached | `test_disabling_the_backend_stops_a_previous_controllers_generations` |
| G10 | `shutdown()` raises while any attached generation or recorded server survives | `test_a_disable_shutdown_that_leaves_a_process_raises_and_its_retry_finishes` |
| G11 | `_interrupt_generation_work` passes `base_session_id:working_path` runtime keys | `test_a_cap_forced_stop_settles_the_activities_of_the_sessions_it_interrupts` |
| G12 | a failed start's surviving process stays owned by its runtime, whose `reap()` and `shutdown()` retry its stop until it is gone | `test_a_failed_start_whose_process_survives_is_reaped_until_it_is_gone` |
| G17 | every record reader takes fields through the typed readers above; a malformed Runtime id is judged by the live process; `_every_recorded_process` sets each generation's id from its file name | `test_a_malformed_record_never_disables_opencode` |
| G18 | `_bind_restored_poll` releases its binding and raises when the rewrite fails; restore and the poll task leave the poll durable, its Turn live, and add it to `_polls_awaiting_restore`, as every other restore step that leaves a poll for later does; `reap_runtime_generations` restores again through `restore_polls_on_ready_transports` while one remains | `test_a_restore_whose_generation_rewrite_fails_runs_nothing_and_the_sweep_restores_it`, `test_a_poll_task_whose_rebind_cannot_be_recorded_leaves_its_turn_live_for_the_sweep` |
| G19 | `restore_active_polls` tracks every handoff and, in a `finally` around the whole restore, cancels each task it never published, before its poll loop starts: the task releases its binding and request, and the poll stays durable in `_polls_awaiting_restore` for the sweep | `test_a_restore_whose_publication_fails_leaves_no_poll_task_waiting_on_it` |
| G15 | `ensure_adopted` attaches adopted generations sorted by `started_at`; a record without one, such as the legacy record, takes its process's create time | `test_the_cap_stops_the_oldest_adopted_generation_whatever_its_record_order` |
| G16 | restore reads `other_runtimes_live_records()` once and leaves every poll whose run one of them executes: the generation it names, or for a poll naming none, the process whose record marks its session running | `test_restore_leaves_every_poll_whose_run_another_desktop_runtimes_live_server_executes` |
| G14 | ownership starts at spawn and ends only on a proven exit; `stop_owned_generations_sync()` also stops owned processes no record names, proven by pid and create time | `test_service_shutdown_stops_a_started_process_whose_record_was_never_written` |
| G13 | `shutdown_runtime(settle_reason)` makes the reason this agent's forced-stop settlement for good; `_interrupt_generation_work` names sessions only from this agent's bindings, ends the Activities started under the stopping generation's activation identity, bound session or not, and settles each session of a generation once, recorded on the generation, so a retried stop settles only the rest | `test_runtime_gen_006_a_disable_retried_after_a_re_enable_settles_only_the_disabled_agents_work` |
| G3 | adoption skips and removes a legacy record whose process a converted record already named | `test_adoption_takes_a_converted_legacy_server_once` |
| G4 | `_outside_turn_admission()`: work outside a turn waits while the backend drains; it is counted, in one step with the readiness check, until it is bound; `runtime_has_active_turns()` reports it | `test_a_native_migration_never_overlaps_an_opencode_start_outside_a_turn` |
| G5 | `_bind_restored_poll` rewrites the poll's `opencode_generation_id` whenever it binds a different generation | `test_runtime_gen_022_a_restored_poll_resumes_on_the_generation_that_runs_it` |
| G6 | the ownership map records each owned process's identity; adoption skips a legacy record whose process a runtime here owns; `_supersedes` clears only once the legacy file is gone, and every flush retries it | `test_a_reenabled_backend_leaves_a_legacy_server_another_runtime_here_owns` |
| G7 | adoption probes a proven process up to three times, two seconds apart, before stopping it | `test_adoption_gives_a_busy_server_more_than_one_health_probe` |
| G8 | the restore loop holds a poll's binding in one `try` from the bind to the handoff `create_task`, and releases it on any `BaseException`, cancellation included; the task rebinds when a forced stop took the generation it was handed | `test_restore_releases_a_poll_binding_its_task_never_took`, `test_a_cancelled_restore_releases_the_binding_its_poll_task_never_took`, `test_a_restored_poll_whose_generation_was_force_stopped_rebinds_before_registering` |
| G9 | an adopted lease is re-held for at most `MAX_LEASE_SECONDS` | `test_adoption_never_holds_a_recorded_lease_longer_than_any_lease_is_granted` |
| S-1 | controller startup with OpenCode not registered runs `stop_recorded_servers_sync(desktop_caller_provenance())` | `test_a_controller_starting_with_opencode_disabled_stops_a_crashed_controllers_servers` (RUNTIME-GEN-025) |

### Desktop Runtime ownership

Several desktop Runtimes can share one Avibe state directory, so every record
names the Runtime whose controller wrote it (`desktop_runtime_id`, written at
spawn: the one Runtime this process acts for, or null). `_record_is_ours()` is
the one ownership predicate:

- A caller with no desktop provenance acts on any record, as `vibe stop`
  always has.
- A record that names a Runtime is ours only when this caller acts for exactly
  that Runtime. Null counts as a Runtime too, so it is foreign to a desktop
  caller.
- A record from a release or build before the field existed, including the
  legacy single-server record, is judged by its live process. This is the same
  check as `refuse_foreign_desktop_process`: a process carrying another or no
  Runtime id, or one whose environment cannot be read, is foreign; a process
  that is gone belongs to nobody, so its record can be forgotten. Adopting such
  a record keeps it without the field, so the check stays the same on every
  later read.

Every reader goes through `_recorded_processes()`, which applies the gate.

What a shared state directory shares. Two Runtimes share one when they run
with the same `AVIBE_HOME`, by default `~/.avibe`. This is the supported
desktop update path: each private Runtime tree has its own id, and the new
Runtime takes over the home of the one it replaces. It also happens when a
terminal install and the desktop app share a home. Everything under the home
is shared: the V2 config, the SQLite state (sessions, turns, deliveries,
Activities, settings, and OpenCode's active polls), logs, the runtime
directory, and OpenCode's generation records and overlays. The OpenCode
caller-context binding file and plugin are shared too, but they are keyed by
native session and renewed by their writer. The data-directory service lock is
held for a controller's lifetime, so at most one controller runs on a home at
a time. Two Runtimes' poll loops therefore never run at once.

Ownership follows processes, not user data. The SQLite state is the user's,
and any Runtime's controller resumes it, exactly as after a restart; that
boundary is the core's, and it is Runtime-agnostic by design. A Runtime owns
only the processes it started. Durable state stays that Runtime's for as long
as it names a live process of that Runtime:

- A record is the process's identity. Another Runtime's record is never acted
  on, even after its process ends, because a failed proof must never disown a
  live process. Its own Runtime forgets it once its process is gone.
- A poll belongs to the process that executes its run: the generation it
  names, or, for a poll from before generations, the process whose record marks
  its native session running. While another Runtime's record proves that process
  live (`other_runtimes_live_records()`, through the same
  `_record_proves_process` every record reader uses), restore leaves the poll
  untouched: no bind, no rewrite, no settlement. Its Runtime resumes it. Once
  the process is gone, no process runs the poll's run, and this controller
  settles it as after a restart. An app update therefore never strands a turn.

| Path | Disposition |
| --- | --- |
| Adoption, including legacy conversion and dedupe, unproven-record removal, and the unhealthy-server stop | gated |
| Adoption's sweep of overlays without a record | removed: another Runtime writes its overlay before it spawns and records the process |
| `ensure_adopted` lease re-hold, marker reconciliation, `reap()` flush, and `_stop` | act only on generations this runtime attached, so they are ours |
| `stop_owned_generations_sync()` (controller shutdown) | gated for records, and limited to processes owned here; an owned process with no record is ours by spawn |
| `stop_recorded_servers_sync(runtime_ids)` (`vibe stop` and a disabled startup) | gated for the caller's ids; the inline refusal moved into the gate |
| `forget_dead_records(runtime_ids)` and `recorded_servers(runtime_ids)` | gated |
| Status: `vibe/api.py` `_opencode_process_status` → `recorded_servers()` | gated by the UI process's provenance, so another Runtime's servers do not count |
| Lease release | reaches only the holder or the registered agent, so it is ours |
| `start_generation` | writes `desktop_runtime_id` |
| `restore_active_polls` | leaves every poll whose run another Runtime's live process executes; resumes the rest |

### Known by design

- **K1.** A controller crash in the microseconds between spawn and the first
  record write leaves one unrecorded process.
- **M2 pin.** A turn whose marker clear fails keeps its generation, through
  marker and durable poll, until the next restore or cap pressure. This is
  master's contract. In the reverse case, a cleared marker whose poll removal
  failed, the next restore settles the poll.
- **K4.** A second cancellation inside a failed start's cleanup can leave a
  stale record, or a recorded process that is never signalled, until
  `vibe stop`, shutdown, or the next adoption.
- **K6.** A poll left to another Runtime keeps its durable turn live here,
  so the core queues new messages on that session behind it. An idle
  `opencode serve` never exits on its own, so after that Runtime's controller
  crashed, its turn waits until that Runtime runs here again or its server is
  stopped, for example by its scoped `vibe stop --expect-runtime-id`. Taking
  it over instead would settle the run from a server that does not run it.
- **K5.** After shutdown stops the recorded generations, a late write can
  recreate the record of a killed process; it is removed as dead later.
- **Core reconciler latency.** A generation that drains while the reconciler
  pass that declined it is still running waits for the next release or sweep
  (60 s, or the closed runtime's own sweep). This is core behavior.
