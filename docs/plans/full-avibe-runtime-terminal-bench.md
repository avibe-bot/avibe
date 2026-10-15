# Full-runtime Terminal-Bench vertical slice

## Scope and ownership

Worktree: `feature/vibey-terminal-bench-full-runtime`, starting base `9a89da41b`.
This change adds source-only evaluation tooling around the installed product.
The earlier standalone-core experiment is out of scope and is not used for
product benchmark results. Deployment, merge, public leaderboard submission,
and remote/GPU provisioning remain separate decisions.

## Source-backed correction to the investigation

A new embedded Controller or a second Agent loop is unnecessary. Existing paths:

- `main.main()` / installed `vibe.service_main.main()` owns service lock, config,
  migrations, Controller construction, signals, readiness and shutdown.
- Empty `platforms.enabled` is already a supported workbench-only installation.
  `MultiIMClient` keeps that runtime alive; `AvibeBot` is the internal transport.
- `AVIBE_WEB_UI_SUPERVISOR` already suppresses the UI watchdog's UI launch. The
  benchmark supervisor intentionally starts no UI; no browser or tunnel is needed.
- `vibe agent run --sync --cwd ...` creates a real durable Run/Session/delivery and
  reaches the real Controller, MessageHandler, AgentService and VibeyAgent.
- `vibe.model_hub_client.ModelHubRemoteService` already creates Sources and routes
  through the controller-owned IPC API. Do not bootstrap raw credentials or
  model routes by writing private storage, and do not bypass Model Hub.
- `AVIBE_HOME` controls product state, including SQLite, native transcript,
  compaction, scheduler, Watches, and job recovery. There is no separate removed
  memory component to resurrect; the shipped transcript/context implementation
  is the measurement subject.

## First implementation contract

Add only source-side tooling under `scripts/terminal_bench/` and focused tests,
not product wheel dependencies, not a Harbor dependency in the product lock.
The wrapper invokes the normal service entrypoint in an owned subprocess.
All product imports and CLI operations
run in child processes with their own environment; the parent environment and
running user service are never modified.

Per trial:

1. Validate an existing task cwd and claim a *new* private trial root outside it.
   Reject overlap in either direction, existing state, and symlink aliases.
2. Prepare an isolated config with no external messaging platforms, remote pairing,
   updates, native backend launches, or UI. Keep scheduler/Watch/Session machinery:
   disabling Harness would not measure the product. Use explicit local catalog
   fixtures and offline managed-runtime settings in the hermetic slice.
3. Start the real service and require controller IPC health, not merely a PID or
   fabricated readiness file. Do not run the service from the task cwd: task
   `config.py`/`core` modules must not shadow product modules.
4. Configure one Source through Model Hub IPC using a key supplied only on a
   private stdin pipe. Never persist the provider key in a command, wrapper config,
   parent artifact, or task shell environment. Runtime engine custody stays owned
   by the product. No Source means a reported `blocked` bootstrap, not a fake pass.
5. Use the real CLI Run path with explicit Agent/model/effort, task cwd, and
   instruction. Collect the durable run outcome and redacted terminal result.
   A result is not a verifier reward. The optional Harbor adapter runs the
   separate verifier; ATIF export and public submission are not pilot gates.
6. Gracefully terminate the exact owned service and collect its descendants while
   alive. Join/terminate/kill by recorded process identity within bounded grace;
   never use a global `vibe stop`, pattern kill, or the user's runtime records.
   Refuse to mark a trial closed if an owned process survives. Export artifacts
   only after shutdown; never copy raw SQLite, runtime logs, jobs or config.

Task environment and runtime state are separate. On a host, the wrapper uses a
throwaway HOME/XDG to avoid user state. In a sandbox, its caller may explicitly
supply the task image environment; preserve toolchain variables and task HOME,
while stripping inherited Avibe caller routing and credential variables. The
wrapper is not a security sandbox: paid runs require an external isolated
container with no host mounts or secret access. Tasks may write scripts,
SQLite databases, logs, and notes in their normal cwd.

## Tests and evidence

Existing tests do not exercise this source-side supervisor boundary. New focused
pytest cases protect fresh-state refusal, environment/cwd separation, no parent
state mutation, actual service/IPC/Run dispatch, task writes (including Unicode
and SQLite), explicit blocked bootstrap, redaction, and exact-owned shutdown.

The integration test may replace only the external Model Hub *engine adapter*
with a scripted test double, while keeping production Controller, Model Hub
service/router/gateway, native providers, AgentService, Vibey and Harness real.
Report that boundary honestly: it proves local product integration, not real CPA
installation, credential custody, provider connectivity, or a benchmark score.

## Implemented evidence

- `scripts/terminal_bench/{profile,control,runner}.py` and `README.md` are the
  single source-only wrapper. Optional `harbor_agent.py`, `launch.py` and
  `credential.py` adapt its lifecycle and private credential transport to
  Harbor; no extracted product packages are uploaded.
- The initial wrapper left product dependency, service, Agent and storage files
  unchanged. The follow-up reliability repair also changes shared final-response
  classification so an explicit output-limit stop cannot be persisted as success;
  dependency files, service ownership and database schema remain unchanged.
- The subprocess test uses normal `main.py`, IPC Source/catalog/route APIs and
  the real CLI Run path. It proves a native Bash tool writes a Unicode note and
  task SQLite, with no framework files in task cwd; task `config.py` cannot
  shadow product imports. Provider keys never enter the service/tool environment.
- A real product Watch creates a delayed note after the initial Run result.
  The wrapper waits for Harness quiescence instead of prematurely tearing down
  background work. Missing credentials are explicitly blocked, not fake passes.
- The deadline case interrupts a continuously writing real tool, closes the
  runtime, then verifies no later writes. A separate process-lifecycle case
  covers a setsid child, a stale unrelated pid identity, and clock-shifted
  identity without signalling the bystander.
- Exports name the Run/Session, root-Run outcome scope, actual installation,
  reported usage (completeness explicitly unknown), cleanup and null verifier
  reward. Raw product state/logs are not copied.

## Installed-runtime acceptance and remaining gates

An independent container check used the official `avibe-os 3.2.0rc5` wheel
(source `98cc70a1c`, SHA256
`5b1b59cd1cf93f36b00f1bef8490612da180366244e2cee163e0d59977fffea0`), its real
managed Model Hub engine, and a local scripted HTTP upstream. It confirmed
ordinary CLI Run dispatch, native file writes/reads, task-owned SQLite, Harness
quiescence and process cleanup. The full Harbor path subsequently passed its
synthetic separate verifier with reward 1 and zero infrastructure exceptions.
These are integration checks, **not model capability scores**. Source-checkout
unit tests use an external-engine fixture and are a separate evidence layer.

The optional adapter requires Harbor 0.24.0, verifies wheel bytes before upload,
and records the actual wrapper source and installation. It declares `atif=False`;
a local verifier score does not depend on ATIF or official leaderboard submission.
Docker Compose can preserve a host UID on uploaded files; credential setup
adopts only a validated single-link regular inode under its private directory,
while normal consumption retains strict ownership and permission checks.

The first remote paired pilot reached a real candidate verifier (reward 0 after
an empty-reply Run failure); its final raw provider stop was not retained. The
reference lost its owner without terminal verification. These do not form a valid
paired capability comparison. Reliability diagnosis and repair are tracked in
[the follow-up contract](2026-10-14-terminal-bench-reliability.md).

Remaining gates: a separately authorized paired run after reliability acceptance,
cost assessment before larger batches, and current-head review/CI before code
delivery. Benchmark results must name the tested wheel and wrapper revisions,
not assume they match the latest product branch. Preserve failed attempts and
resource/protocol differences; do not extrapolate a small pilot to a leaderboard
rank. See `scripts/terminal_bench/HARBOR.md` for supported images and lifecycle
limitations.

Run only focused tests through `~/.local/bin/hd1-test`; retain product dependency
files unchanged. Any unproven lifecycle/provider capability remains a named gate.
