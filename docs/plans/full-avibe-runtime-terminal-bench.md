# Full-runtime Terminal-Bench vertical slice

## Scope and ownership

Worktree: `feature/vibey-terminal-bench-full-runtime`, base `9a89da41b`.
Implementation owner: `sesxksgh2gjus`; original orchestrator: `sesu69aqh5sc7`.
All earlier fork-parent claims are superseded; this Session is the only writer.
The paused standalone worktree and its partial changes are out of scope.
No Docker, paid upstream requests, deployment, push, or PR is authorized here.

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
Consolidate the sibling's untracked bridge/profile here; delete only the two
unused package initializers and the superseded untracked plan after consolidation. The wrapper invokes the normal
service entrypoint in an owned subprocess. All product imports and CLI operations
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
   A result is not a verifier reward. Harbor execution and ATIF export are later
   gates, not claims made by this slice.
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
  single source-only wrapper. The sibling bridge/profile were consolidated;
  unused benchmark package initializers and the superseded plan were removed.
- Product `pyproject.toml`, `uv.lock`, service, Agent and storage files are unchanged.
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

Remaining gates: parent-run **wrapper** acceptance with an installed wheel and
real CPA engine; Harbor wiring/ATIF; exact-source/benchmark/resource pinning;
paired scored tasks; current-head review/CI. The original orchestrator separately
reported successful `avibe-os 3.2.0rc5` + real-engine + loopback upstream acceptance
(source `98cc70a1c`), which is not evidence for this wrapper or this checkout's
base `9a89da41b` and is not a benchmark score.

Run only focused tests through `~/.local/bin/hd1-test`; retain product dependency
files unchanged. Any unproven lifecycle/provider capability remains a named gate.
