# Runtime Generations

Status: approved by the owner on 2026-10-01 (decisions recorded below). Supersedes the per-backend catalog
compatibility approach in #2328 (draft). Extends `backend-rolling-restart.md`
by implementing its deferred "prepare a new generation during DRAINING".

## Background

### One case today

A user adds model `X` to the Codex catalog. Session `S1` is running a two-hour
task in `/repo`, and session `S2` in the same directory sends a message.

1. The catalog callback calls `BackendRestartCoordinator.request_restart`.
2. Admission closes for every Codex directory. `S2`'s message, and any message
   to any other Codex session, waits.
3. After 300 s `S1` is force-interrupted, every app-server is stopped, and
   admission reopens.

The same drain-then-interrupt runs for any `agents.*` save, credential change,
OpenCode provider edit, manual restart, or CLI install. It runs for Claude
too, although every Claude session owns its own process.

### What exists, and what does not

- Shared runtime units: a Claude client serves one session; a Codex app-server
  serves one working directory; one `opencode serve` serves the whole instance.
- `backend-rolling-restart.md` defines the barrier
  `READY -> DRAINING -> SWITCHING -> READY`. Turns admitted before the barrier
  finish on the old process, while new turns wait. The document defers
  "prepare a new generation during DRAINING", so no backend runs an old and a
  new process side by side.
- `RuntimeActivationRegistry` keeps exactly one live generation per
  `(backend, resource)`. It is a fence against late commits from a retired
  process, not a router.

### How changes reach runtimes today

| Trigger | Policy | Interrupts running work |
| --- | --- | --- |
| `agents.*` save, credential save or removal, OpenCode provider edits, manual restart, CLI install job | Whole-backend drain, 300 s, then interrupt | Yes |
| Hub/Direct mode switch, native credential migration | Immediate interrupt, user-confirmed | Yes, by design |
| Unattended CLI auto-update | Only when idle, otherwise skipped | No |
| Codex gateway channel or token change | Per directory: wait 30 s, then refuse the new turn | Only the same session's own stale turn |
| OpenCode overlay change | The next turn waits for every run on the shared server, with no bound | No, but new turns can wait indefinitely |
| Claude per-session launch input change | Some inputs wait for the session to idle (no bound); others rebuild immediately | Possibly, for immediate rebuilds |

Defects found while mapping these paths:

- Fields that are read live still drain and interrupt, for example
  `idle_timeout_seconds`, and Codex's unused `auth_mode`.
- Direct-mode Claude credentials and the CLI path are never compared on
  client reuse. Their correctness depends on the global teardown.
- An in-place OpenCode CLI upgrade keeps the old server, because an unchanged
  binary path selects the `PATCH /global/config` reload.
- Codex interrupts the session's own turn before it knows the replacement can
  proceed. If the directory stays busy, both turns are lost.
- No turn is bound to one configuration snapshot from model resolution through
  process launch. Review of #2328 kept finding races where a turn resolved its
  model from one catalog and ran on a runtime holding another.

## Goal

- **G1.** A new turn or session is never blocked or refused because of a
  configuration change. No routine change interrupts running work, except to
  reclaim a generation at the cap below.
- **G2.** Every turn runs against one configuration snapshot end to end. Its
  model resolution and its runtime's launch inputs come from the same snapshot.
- **G3.** One mechanism serves all backends. Backends differ only in how they
  declare their runtime unit, their launch spec, and whether generations can
  run side by side.

Exclusive, user-confirmed operations keep their explicit cutover: Hub/Direct
mode switch, native credential migration, and an explicit "stop running work".

## Model

**Configuration snapshot.** An immutable view of every input that affects a
runtime: Model Hub configuration and catalogs, `agents.*` runtime config, and
credential identity. A turn captures one snapshot when it is admitted.

**Runtime unit.** The scope one runtime process serves:

- Claude: one session client (composite session key);
- Codex: one working directory;
- OpenCode: the Avibe instance.

**Launch spec.** A comparable digest, computed by the backend, of every
process-level input for a unit under a snapshot. It covers binary identity
(resolved path and version), credential identity, Hub channel, gateway URL and
token, catalog or overlay digest, managed policy, and launch environment.
Per-turn inputs (model, effort, prompt text, per-turn routing metadata) are not
part of it.

**Generation.** One running process for a unit, with one launch spec. It is in
one of three states:

- `current`: admits new turns;
- `retiring`: serves only work already bound to it;
- `stopped`.

A unit has at most one `current` generation and a bounded number of
`retiring` ones.

**Binding.** A turn binds to exactly one generation at admission, together
with any Activity, delivery, or scheduled run it owns. Only bound work keeps a
retiring generation alive.

### Turn admission

1. Capture the snapshot and resolve the turn's launch (model, route, efforts)
   from it.
2. Compute the unit's desired launch spec from the same snapshot.
3. Let `G` be the unit's current generation:
   - if `G.spec == desired`, bind to `G`;
   - otherwise, if nothing is bound to `G`, replace `G` in place and bind;
   - otherwise, if the backend runs generations side by side, start `G'` with
     the desired spec, mark `G` retiring, and bind to `G'`;
   - otherwise, when the new generation cannot coexist with `G` (the cap is
     reached, or the backend cannot run the two side by side), force-stop the
     oldest conflicting generation, interrupt its work with the standard
     runtime-update notice, and start `G'`. The new turn never waits for the
     old work and is never refused.
4. When a retiring generation's bound work reaches zero, stop it.
5. When a stale current generation goes idle, replace it eagerly, so the next
   turn does not pay the startup cost.

The same example under this model: `S2` resolves with the snapshot that
contains `X`. `/repo`'s app-server is busy with `S1`, so Avibe starts a second
app-server for `/repo` and `S2` resumes its thread there. `S1` finishes on the
first app-server, which then stops. `S1`'s next turn runs on the second one.

### Invariants

- A session is active on at most one generation at a time. The session turn FSM
  already guarantees one active turn per session.
- A generation stops only when no turn, Activity, delivery, or durable owner is
  bound to it. Durable ownership snapshots already enumerate these.
- Starting a generation is serialized per backend.
- A unit runs at most three generations. When another one is needed at the
  cap, the new one starts first and the oldest retiring generation is then
  force-stopped, so a failed start costs no running work. Its sessions get the
  runtime-update interruption notice and continue on the current generation.
- Teardown never runs under the generation lock, because a forced stop settles
  work that releases its bindings. A teardown that fails or is cancelled keeps
  the generation tracked, closed to admission, so the next sweep retries it.

## Per-backend design

### Claude Code

- **Unit:** one session client.
- **Side by side:** not within one session. Two processes must never resume
  the same session transcript concurrently.
- **Launch spec:** compared on every reuse:
  - the Model Hub launch fingerprint (channel, gateway, token, process
    settings);
  - reasoning effort;
  - system prompt;
  - caller identity, Skill bindings, git PATH;
  - the renewal epoch. Direct-mode credentials and the CLI path change only
    through flows that renew, so the epoch covers them.
- **Spec change while the session's own background Activity runs:** the turn
  stays on the session's current client, keeping that client's process inputs,
  and the session switches at its next idle point. Only when that client
  cannot serve the turn is it force-stopped and replaced. That happens when the
  turn's Hub launch needs a different gateway route, token, or process settings,
  and when the turn comes from a different caller, whose resource authority
  the old process must never carry.
  A model added after the client started does not count: a live client accepts
  `set_model` for it, which a hermetic probe confirmed on Claude CLI 2.1.286.
  Nothing waits.
- **Removed:** the backend-wide barrier and the `refresh_auth_state` teardown
  for routine changes. A configuration or credential save only advances the
  snapshot. Each session rebuilds at its next turn when its spec differs.

### Codex

- **Unit:** one working directory.
- **Side by side:** yes, behind the verification gate below.
- **Launch spec:** binary identity, credential identity (`CODEX_HOME` auth
  digest), Hub channel, gateway URL and token, catalog digest, managed launch
  environment.
- **Routing:** `_transports[cwd]` becomes the directory's generation set. A
  session's live turn is routed to the generation it is bound to. A new turn
  goes to the current generation and resumes its thread there, which replacement
  already does today.
- **Removed:**
  - the 30 s runtime-change wait and `CodexRuntimeChangeBlockedError` for
    routine changes;
  - `_interrupt_active_turn_before_runtime_change`;
  - the catalog compatibility checks from #2328;
  - `refresh_auth_state` as the apply path for configuration and credential
    changes.

### OpenCode

- **Unit:** the instance.
- **Side by side:** yes. See the verification below.
- **Launch spec:**
  - binary identity;
  - Hub overlay digest;
  - managed policy revision and caller-context plugin digest;
  - digest of the user `opencode.json` and `auth.json` that the server loads.
- **Required changes:**
  - one port per generation (the pinned 4096 goes away);
  - one PID record per generation, and adoption of every recorded generation
    after a controller restart;
  - per-generation event streams, poll loops, and active-run tracking;
  - serialized generation starts.
- **Removed:**
  - the overlay transition and its reservations;
  - the deferred auth refresh flags;
  - the `PATCH /global/config` reload path;
  - the policy-pending and plugin-pending refusals;
  - the compatibility checks from #2328.

## Existing mechanisms and their fate

Rule: a lifecycle that can run through generations does. Only an operation that
must be exclusive for safety keeps a dedicated path. A legacy convention is not
a reason to keep a path.

| Mechanism today | Used by | Fate |
| --- | --- | --- |
| `BackendRestartCoordinator.request_restart`: whole-backend drain, 300 s, then interrupt | `agents.*` save; credential save or removal; Web OAuth; IM `/setup`; OpenCode provider edits; manual Restart; CLI install job; catalog changes before #2328 | Replaced. Every caller advances the snapshot. The drain path is deleted. |
| `migration_guard`: immediate interrupt, then exclusive cutover | Native credential migration, startup migration recovery, Hub re-authentication | Kept. Custody needs exclusivity: an old process can rewrite a credential file that is being moved. |
| `migration_guard` for the Hub/Direct mode switch | Mode toggle | Replaced. Mode becomes a launch-spec input. The gateway keeps admitting turns bound to a Hub-mode generation until they end, so a switch interrupts nothing. |
| `run_when_idle` | Unattended CLI auto-update | Kept for the install step only. The refresh that follows is replaced by the binary identity in the spec. |
| Backend drain admission (`begin/end_backend_drain`, `wait_backend_ready`) | All coordinator paths | Kept only inside `migration_guard` and the install window. |
| `RuntimeActivationRegistry` | Turn, Activity, delivery, and scheduled-task commits | Kept as the fence. Each generation gets its own resource key, so one live identity per key still holds. |
| Durable runtime ownership snapshots | Replacement safety | Kept as the evidence that a generation is drained. |
| `restart_backend` marker file and `RuntimeCommandWatcher` | UI-process requests to the controller | Kept as the signal. Its action becomes "advance the snapshot", or "renew generations" for manual Restart. |
| CLI install job fingerprint (path, realpath, version) | Decides whether to restart after an install | Replaced. Binary identity is part of every launch spec. |
| Claude `refresh_auth_state` global teardown | Every Claude refresh | Kept for shutdown and migration only. |
| Claude lazy rebuild on fingerprint or effort change: waits for idle with no bound | Hub route, process settings, effort | Unified under the Claude rule. |
| Claude immediate rebuild on prompt, caller env, Skills, or git PATH change | Reuse check | Unified under the Claude rule; these become spec inputs. |
| Codex `refresh_auth_state` stop-all | Every Codex refresh | Kept for shutdown and migration only. |
| Codex fingerprint replacement: 30 s wait, then `CodexRuntimeChangeBlockedError` | Channel or token change | Replaced by directory generation sets. |
| Codex `_interrupt_active_turn_before_runtime_change` | Channel change | Deleted. A new message to a busy session keeps Codex's normal semantics. |
| Codex catalog invalidation and the #2328 compatibility checks | Catalog change | Replaced. The catalog digest is a spec input. |
| Codex replaced-cwd, dead-transport, and idle-eviction replacement | Health | Kept. Idle eviction becomes the generation reaper. |
| OpenCode `PATCH /global/config` reload | Settings and credential refreshes | Deleted. User config and credential digests are spec inputs. |
| OpenCode deferred refresh (`_auth_refresh_pending`, `_pending_runtime_config`) | Busy refreshes | Deleted. |
| OpenCode overlay transition and reservations | Hub overlay changes | Deleted. The overlay digest is a spec input. |
| OpenCode policy-pending and plugin-pending refusals | Policy or plugin changes | Deleted. Both are spec inputs, so new turns are never refused. |
| OpenCode single-port singleton manager, one PID file, adoption | Every OpenCode turn | Replaced by generation servers on dynamic ports, one PID record per generation, and adoption of every record. |
| UI-process OpenCode manager calling `ensure_running` | Web OAuth flows | Replaced. The UI process asks the controller and never launches a server. |

Defects that the refactor fixes as it touches these paths:

- Claude `idle_timeout_seconds` is read once at startup. It becomes live.
- An in-place OpenCode CLI upgrade is ignored. Binary identity includes the
  version.
- Codex direct launches with Model Hub enabled lose the desktop-managed
  environment.
- Codex `direct` and `native_cli` launch identical processes under different
  fingerprints. Comparing specs removes the needless replacement.

## Triggers after the refactor

| Trigger | Action |
| --- | --- |
| `agents.*` save, credential save or removal, provider settings, CLI install or upgrade, catalog edit, built-in snapshot refresh, credential-address repair, Hub/Direct mode switch | Advance the snapshot. Each unit switches generation at its next turn. |
| Manual "Restart backend" | Renew generations: new turns start a new generation, and old ones retire when their work is done. Nothing is interrupted. |
| Explicit "stop running work" | The existing interruption path. |
| Native credential migration, migration recovery, Hub re-authentication | `migration_guard`, unchanged. |
| Unattended CLI auto-update | `run_when_idle` for the install step only. |
| Backend disabled | Its work stops at once with the interruption notice, and its agent is destroyed. New messages get the disabled reply at once. |
| Backend enabled | A new agent registers at once. |

The `AGENTS.md` rule that an `agents.*` save reconciles "through the backend
rolling-refresh path" becomes: the save advances the snapshot, and live units
move to it through generations.

## Core contract

Implemented in the first PR; the Codex and OpenCode adapters build on it.

- **Renewal instead of restart.**
  - `BackendRestartCoordinator.request_restart(backend)` first asks
    `AgentAuthService.renew_backend_runtime(backend)`. When the backend's agent
    implements `renew_runtime(runtime_config)`, the coordinator adopts the
    config and returns `"restarted"` without a drain.
  - Every routine trigger reaches the coordinator, so implementing the hook
    moves all of them at once: `agents.*` saves, credential flows, provider
    edits, manual Restart, and the install job.
  - Enabling and disabling a backend follow the same rule; see "One
    lifecycle: deleting the drain path" below.
- **Renewal epoch.**
  - `renew_runtime(runtime_config, *, config_save)` bumps the backend's renewal
    epoch, which is part of every launch spec. A config save
    (`config_save=True`) whose only changes are fields read live renews
    nothing. Each unit therefore moves at its next turn even when the
    change is invisible to the spec, such as a credential written to the
    CLI's own store.
  - `idle_timeout_seconds` is read on every idle sweep, so it needs no
    renewal at all.
- **Catalog changes.** The Model Hub catalog callback calls the agent's
  `adopt_model_hub_catalog()` when present. Claude's is a no-op because each
  turn resolves its launch. Without the hook, the legacy path runs.
- **`modules/agents/runtime_generations.py`.** `RuntimeGenerationSet` holds
  one unit's generations.
  - `acquire(spec)` runs admission step 3: reuse, replace in place when idle,
    or start a new current generation while the busy one retires. At the cap
    of three, the oldest retiring generation is force-stopped. A failed start
    leaves the current generation serving.
  - `reap()` stops drained retiring generations, and `stop_all(force)` serves
    shutdown.
  - Adapters provide `start(spec)` and `stop(generation, force) -> bool`, and
    own the routing of bound work. The core decides "drained" from its own
    binding count alone. A graceful stop may decline with `False` while the
    adapter's own evidence still shows work. That evidence always includes
    an Activity the process started (`AgentService.activation_has_activities`):
    an Activity can outlive its turn and its Session's move to a newer
    process, so no Session binding names it any longer.
  - Admission is pure bookkeeping: every state change happens in one
    synchronous step, and no admission path ever awaits a teardown. One
    reconciler task per set owns every stop. It runs stops one at a time and,
    after each, re-evaluates the cap and which generations are drained.
  - The cap counts every attached generation, including ones whose graceful
    stop declined. When a unit is over the cap, the oldest generation is
    force-stopped. Admission never waits, so while a forced stop is still
    running, a unit can briefly exceed the cap by one generation for each new
    spec that arrives. The adapter's forced stop has a bounded kill timeout,
    and once it returns, the reconciler stops the unit back down to the cap.
  - A declined stop is retried on the next release or sweep. A failed or
    cancelled stop is retried only by the next sweep; the generation is
    `closed`, so it never serves a turn again. A sweep or stop-all that
    arrives while a stop runs entitles every generation, including that one,
    to one more attempt.
  - `stop_all` closes admission first. A graceful `stop_all` lets bound
    generations finish their work. `settled()` waits for the reconciler.
- **Forced stops.** `AgentService.force_end_runtime_activities(backend,
  runtime_key)` settles one runtime's Activities as interrupted by a runtime
  update, using the existing backend-refresh notice.
- **Snapshot discipline.** A turn loads its configuration once. Its launch
  resolution and its launch spec both derive from that load. Each adapter
  reads every mutable launch input (config, renewal epoch, binary and
  credential identity, Hub snapshot) in one synchronous step before its
  first await, and nothing after that step reads adapter state.

## Verification

### OpenCode, two servers on one home (done)

Hermetic probe, OpenCode 1.18.33, temporary `HOME` and XDG directories, fake
OpenAI-compatible upstream. Two `opencode serve` processes ran on different
ports over one data directory:

| Check | Result |
| --- | --- |
| Storage | One SQLite database in WAL mode (`opencode.db`, `-wal`, `-shm`) |
| Session created and prompted on A | Visible on B with its messages |
| Same session continued on B | Both servers then list four messages; no stale cache on A |
| Concurrent prompts on different sessions, one per server | Both completed in parallel (1.6 s against a 1.5 s upstream delay) |
| Both servers started at the same moment on a fresh home | Both failed with `database is locked` while migrating. Starting them in sequence works, so generation starts must be serialized. |

### Codex, two app-servers in one directory (gate for the Codex PR)

Hermetic probe with a fake Responses upstream:

- two app-servers run in one directory at the same time;
- a thread started on A resumes on B while A is still alive;
- A's later shutdown appends nothing to that thread's rollout.

If the last check fails, A must unload a thread before the thread moves.

## Delivery

Three PRs; the second and third run in parallel lanes:

1. **Core and Claude** (orchestrator):
   - the core contract above;
   - Claude on the complete launch spec and the Claude rule;
   - every routine trigger renewed in place through the coordinator. Codex
     and OpenCode keep their current path until their own PR lands;
   - the Model Hub catalog callback;
   - this document and `AGENTS.md`.
2. **Codex** (lane):
   - the verification gate;
   - directory generation sets;
   - Codex triggers on the new entry;
   - the Codex defects above.
3. **OpenCode** (lane):
   - generation servers;
   - OpenCode triggers on the new entry;
   - removal of the reload, deferral, transition, and refusal paths;
   - the UI-process launch path.

The Hub/Direct mode switch is a snapshot change in the first PR. It never enters
`migration_guard`. The gateway keeps resolving a Hub turn that was admitted
before a switch to Direct, until that turn ends.

The Codex adapter is integrated into the first PR's branch before that PR
merges, so the shared core ships with a production caller. The OpenCode
adapter follows as a stacked PR. The drain-then-interrupt path is deleted
once the last backend renews in place.

Each PR ships scenario IDs and regression tests that fail on the code before
it.

## One lifecycle: deleting the drain path

Once all three adapters renew in place, no routine trigger needs the drain.
Enabling a backend registers it at once. Disabling a backend is the one
change that stops work, because the user asked for exactly that.

### Example

Codex is running a long turn in `~/app` when the user turns Codex off in
Settings. Before this step, new Codex messages were held for up to 300 s, then
the running turn was interrupted, and only then did the held messages get the
"Codex is disabled" reply. After this step:

1. The save reaches `request_restart("codex", config_save=True)`. The Codex
   agent leaves the registry at once, so a new message gets the disabled
   reply without waiting.
2. The turn in `~/app` and its Activities settle as `backend_disabled`,
   through the coordinator's `interrupt_backend`, which the native credential
   cutover also uses with `backend_refresh`. The conversation is told "This
   turn was stopped because Codex was turned off", and a Harness run ends
   `canceled` with the same explanation, because the user chose this.
3. `shutdown_runtime()` stops every app-server of the agent, and the agent is
   gone. A turn that raced the disable fails visibly with
   `error.agentRuntimeRetired` and starts nothing.
4. Turning Codex back on registers a fresh agent at once.

An earlier draft let a disabled backend's agent keep its running work and
drain, and let a re-enable reopen it. Every lookup that serves running work
then had to reach retired agents, and review kept finding the ones that did
not. The owner decided that a disable, being the user's own action, should
simply stop the work, and that machinery is gone.

### Coordinator

- `request_restart(backend, *, config_save)` never drains or holds a message.
  `AgentAuthService.renew_backend_runtime` owns the cases:
  - **Registered and enabled:** `renew_runtime`, unchanged.
  - **Codex or OpenCode disabled:** unregister, `interrupt_backend`, then
    `shutdown_runtime()`. A cancelled requester never leaves it half done.
  - **Claude disabled:** Claude stays registered with `enabled=False`, as
    before. Its work is interrupted the same way, and the clients live at
    that moment, busy ones included, are captured by identity and closed; a
    retry closes only those, never a client of a re-enabled backend. Saving
    again while it is disabled interrupts nothing more.

  Disable-teardown ownership: the core's backend-wide interrupt is the fast
  path, and it is skipped once the backend is enabled again. The guaranteed
  settlement is scoped to what the disable captured: the disabled agent's own
  generations (adapter forced stop with `settle_reason`), or Claude's captured
  clients. The core matches that work by owner, never by key: every forced
  generation stop, a disable's or the cap's, ends the Activities started
  under that process's activation identity, whatever runtime key they carry
  and whether or not a Session still binds the process, and settles turns
  only where the stopping agent holds the Session's turn gate
  (`force_end_runtime_work(..., activation_identities=, agent=)`). A retry
  after a re-enable therefore still finishes the disabled agent's work and
  touches nothing new, and an Activity that outlived its turn or its
  Session's move still settles with its process.
  - **Enabled but not registered:** a new agent registers at once.
- `run_when_idle` keeps admission closed for the install step only. Afterwards
  it renews instead of refreshing.
- `migration_guard` is unchanged.
- Deleted: the 300 s drain, `AVIBE_BACKEND_RESTART_DRAIN_TIMEOUT_SECONDS`,
  `"draining"` as a restart result, and the legacy catalog restart. The
  refresh that tears a runtime down survives only inside `migration_guard`.

### Adapter hooks

| Hook | Meaning |
| --- | --- |
| `renew_runtime(config, *, config_save)` | Unchanged. |
| `reap_runtime_generations()` | Called by the 60 s sweep on every registered agent. |
| `shutdown_runtime(settle_reason=None)` | Stop every process of this agent now. A disable passes `settle_reason="backend_disabled"`, and every forced stop then settles the turns and Activities bound to that process with it, scoped to this agent instance; service shutdown and probe teardown pass nothing and show no notice. It raises when any process survives, so `AgentService.run_until_done` keeps the teardown and the idle sweep retries it; a retry must be idempotent and never touch another instance's work. |

### Startup

A controller that starts with OpenCode disabled stops the OpenCode servers
recorded by a controller that crashed; no agent would ever adopt them. Their
durable polls stay durable, and the next controller with OpenCode enabled
restores them, as before. Codex app-servers and Claude clients exit with their
parent's stdio, so they leave nothing behind.

### Scenarios

- **RUNTIME-GEN-006.** Disabling a backend stops its work at once: new
  messages see it disabled, running work settles with the interruption
  notice, and every process stops.
- **RUNTIME-GEN-007.** Re-enabling registers a fresh agent at once.
- **RUNTIME-GEN-008.** An unattended CLI update renews after the install
  instead of refreshing.

## Decisions

Recorded from the owner, 2026-10-01:

1. A configuration change never blocks or refuses a new turn or session. The
   user experience takes precedence over legacy conventions.
2. Manual "Restart backend" renews generations without interrupting running
   work.
3. A unit runs at most three generations. At the cap, the oldest retiring
   generation is force-stopped and its work interrupted.
4. When a Claude spec changes while the session's own background Activity
   runs, the turn stays on the current client until the session is idle. The
   client is force-replaced only when its snapshot cannot serve the turn.
5. Lifecycles are unified under generations wherever safety allows. PRs are
   few, and work that can run in parallel does.

Recorded from the owner, 2026-10-03:

6. Disabling a backend is an explicit, user-visible action. It stops that
   backend's work immediately and destroys its agent; nothing drains.
