# Agent Guidelines for Avibe

This document is the operating manual for coding agents working in this repository.

## 1. Project Overview

Avibe is the local-first Agent OS: one install command turns a machine into the
runtime an agent lives in, and the user operates that runtime through Web or IM
surfaces such as Slack, Discord, Telegram, Feishu/Lark, and WeChat.

Current product shape:

- V2 config-driven service with a Web UI setup wizard and settings pages
- multi-platform message transport with shared core orchestration
- multi-backend agent routing across OpenCode, Claude Code, and Codex
- local Incus-based unified regression environment for real cross-platform verification

Default mindset:

- treat the system as **multi-platform, multi-backend** first
- prefer root-cause fixes over narrow patches
- preserve user-visible behavior unless the task explicitly changes product behavior
- make the next agent/platform inherit correct behavior automatically

## 2. Design Philosophy and Architecture

### Core Rule: Fix at the Highest Appropriate Layer

- If a bug appears on one platform, check whether the same logic exists for the others before patching a platform adapter.
- If a behavior should be shared by multiple backends, prefer the shared core or backend abstraction over a single backend implementation.
- Keep transport/platform details out of core business logic whenever possible.

Decision checklist before writing code:

1. **Scope**: is this platform-specific/backend-specific, or common?
2. **Abstraction**: can the shared base or core layer own this behavior?
3. **Call path**: is the code called from controller/handlers/common flow?
4. **Future-proofing**: would a new platform/backend inherit the correct behavior automatically?

### Codebase Map

- `main.py` - entry point wiring `config.V2Config` into `core/controller.py`
- `core/controller.py` - orchestration and dependency wiring
- `core/handlers/` - platform/backend-agnostic business workflows
- `core/message_dispatcher.py` - outbound message routing and reply enhancement flow
- `core/reply_enhancer.py` - file-link and quick-reply prompt injection helpers
- `modules/im/` - IM platform adapters (`slack.py`, `discord.py`, `telegram.py`, `feishu.py`, `wechat.py`) plus shared base classes
- `modules/agents/` - agent backend adapters (`opencode/`, `codex/`, Claude-related modules) plus shared abstractions
- `modules/im/formatters/` - platform-specific formatting built on shared formatter concepts
- `config/` - V2 config, settings, sessions, paths, and compatibility conversion
- `ui/` - React + Vite + TypeScript Web UI
- `scripts/` - operational helpers, including regression testing workflows
- `tests/` - pytest-style unit/integration/regression coverage

### Runtime Data and Important Paths

- default home: `~/.avibe/`
- legacy home: `~/.vibe_remote/` remains a compatibility path and may be a back-symlink to `~/.avibe/`
- logs: `~/.avibe/logs/vibe_remote.log`
- persisted state: `~/.avibe/state/`
- default agent working directory: `_tmp/`
- generated regression metadata: `.runtime/incus-regression/` in the primary checkout

## 3. Runtime Environments

### Local `vibe` Service

Common commands:

- install: `uv tool install avibe-os`
- run: `vibe`
- inspect: `vibe status`
- stop: `vibe stop`

Use local `vibe` for:

- local packaging checks
- local CLI behavior checks
- editable-install UI preview when explicitly needed

Hard rule:

- **Never restart the local `vibe` service for routine verification.**
- The local `vibe` process may be the coding agent runtime itself; restarting it can interrupt the session.
- **Tests and probes must never mutate the current local environment or live user state.**
  Do not run commands, setup flows, migrations, installers, config writes, or
  agent detection/install tests against `~/.avibe`, legacy `~/.vibe_remote`, the user's shell
  environment, or the running local service unless the user explicitly asks for
  that exact local operation. Use an isolated `VIBE_REMOTE_HOME`, a temporary
  fixture directory, the Incus regression environment, or the existing regression
  environment instead.
- Unless the user explicitly asks otherwise, use the Incus regression environment for user-facing verification.

### Regression Testing (Incus)

When the user says `回归测试`, treat it as:

- update the latest code into the existing **local** Incus-based regression environment
- let the user verify behavior on Slack, Discord, Feishu/Lark, and WeChat
- preserve previously accumulated regression config/state unless the user explicitly asks for a reset

The regression environment is local developer infrastructure only. It must be
created, inspected, updated, and destroyed through the local Incus runner in this
repo. Remote Incus hosts, remote tenant instances, demo instances, and
customer/user environments are not regression environments and must not be used
as fallbacks for development testing.

The local master regression environment runs a single unified Incus system
container with all four IM platforms enabled simultaneously.

Standard path:

- default command: `./scripts/run_regression.sh`
- direct runner: `python3 scripts/incus_regression.py up --target master`

Connection standard (the one supported way to connect — all platforms, all agents):

- The runner reaches Incus through the `INCUS_CMD` knob only: it builds every
  command as `${INCUS_CMD:-incus}`. This is the single supported connection
  mechanism for development regression on every platform.
  - Linux (native daemon): leave `INCUS_CMD` unset (defaults to `incus`), or set
    `INCUS_CMD="sudo incus"` when the user is not in the `incus-admin` group.
  - macOS (Lima): `INCUS_CMD="limactl shell avibe-incus-regression -- sudo incus"`
    (see the macOS note below).
- `--remote` is a *different axis* — it selects **which** Incus daemon to target,
  not **how** to connect — and is **not** a development-regression connection
  method. It is only a rare escape hatch for operating on a genuinely remote
  Incus host; never use it to reach the local regression environment. Do not
  enable a TLS listener / client cert / named remote on the regression VM to
  "connect that way": the daemon is unix-socket-only by design and `INCUS_CMD`
  is the standard.

Connecting to Incus on macOS (Lima):

- macOS has no native Incus daemon. The regression environment runs inside the
  Lima VM `avibe-incus-regression`, and `incusd` there listens **only on its unix
  socket** (no TLS `core.https_address`, no guest→host socket forward). So a plain
  host `incus` (default `local` remote) has nothing to dial and the runner aborts
  with "you must connect to a remote server".
- Drive the runner through the VM with the `INCUS_CMD` knob (the runner splices it
  in place of `incus`):

  ```
  INCUS_CMD="limactl shell avibe-incus-regression -- sudo incus" ./scripts/run_regression.sh
  ```

  Optionally add the Lima user to the `incus-admin` group inside the VM to drop the
  `sudo`. This is the supported way to run the regression CLI on macOS — not a hack.
- Symptom when this is missing: `incus info` fails, so `up` wrongly concludes the
  instance does not exist, takes the create path, and trips the host-port preflight
  on the UI port the already-running env is forwarding (e.g. 15130). The fix is the
  connection above — not removing the preflight, which is correctly skipped once the
  client can see the existing instance.

Rules:

- use local Incus only; do not pass `--remote`, switch Incus remotes, SSH to a
  remote host, or inspect remote tenant projects for development regression
  unless the user explicitly asks for remote operations outside the development
  workflow
- `master` is the long-running local regression environment; keep it online and
  preserve its product state across code updates
- a normal master update should sync source and restart the Avibe service inside
  the existing local Incus instance, not recreate the environment or reseed data
- do **not** use `--reset-config` or `--reset-all` unless the user explicitly requests reset behavior
- do **not** disable or overwrite preserved `remote_access` / Avibe Cloud pairing state just to make local probes pass; the regression environment is also used to test remote access, so preserve and fix the host/binding path instead
- when Avibe Cloud remote access is enabled in regression, prefer binding the Incus UI proxy to loopback for local maintenance access (`REGRESSION_PORT_BIND_HOST=127.0.0.1`) while keeping the remote public URL active for product testing
- use `--target master` for the long-running master regression environment
- use `--target worktree` for isolated temporary worktree regression environments
- after running the script, verify the service is healthy before handing back to the user
- prefer Incus regression over local `vibe` whenever validating cross-platform behavior, setup wizard behavior, or user-facing IM flows
- always use `./scripts/run_regression.sh` or `python3 scripts/incus_regression.py`; do not run raw Incus commands directly because the runner owns naming, state preparation, source sync, runtime readiness checks, and worktree cleanup metadata
- the script stores worktree regression metadata under the primary checkout's `.runtime/incus-regression/` by default, even when invoked from a task worktree
- the script reads `.env.regression` from the current worktree first, then falls back to the primary checkout
- temporary worktree environments should be deleted with `python3 scripts/incus_regression.py delete --target worktree --yes` or cleaned with `cleanup-stale --yes`
- the regression container uses `/home/avibe` as a persistent real home; product state should live under `/home/avibe/.avibe`, with `/home/avibe/.vibe_remote` as the compatibility symlink
- the script must prepare and verify Show Runtime before reporting success; if Show Runtime cannot be installed or executed, treat the regression update as failed
- for branch/master regression, `REGRESSION_SHOW_RUNTIME_SOURCE` defaults to `github-source` because source checkouts do not necessarily include a packaged release manifest; release/pre-release installs should use the packaged manifest path

Worktree behavior:

- code is synced from the worktree where the script is invoked
- `master` uses the long-running Incus project/instance and preserves product state
- non-master worktrees use temporary isolated local Incus project/instances
- after a worktree is merged, abandoned, or no longer needed, delete its
  regression environment with `python3 scripts/incus_regression.py delete
  --target worktree --yes`; use `cleanup-stale --yes` to remove environments for
  worktree paths that no longer exist
- worktree mappings live in `.runtime/incus-regression/worktrees.json`

## 4. Configuration and Routing Model

Persistent configuration is centered on `config/v2_config.py` and the Web UI.

High-level V2 config areas:

- platform config: Slack / Discord / Telegram / Feishu / WeChat credentials and switches
- runtime config: default cwd, log level, and related runtime behavior
- agent config: per-backend enablement and CLI paths
- UI config: setup host/port and Web UI behavior

Agent routing model:

- global default: the enabled Vibe Agent recorded in SQLite `state_meta.default_agent_name`
- backend availability and CLI path: `agents.<backend>.enabled` and `agents.<backend>.cli_path`
- per-channel overrides: configured via the Web UI Agent Settings / channel settings
- deprecated fields: `agents.default_backend` and scope-level `routing.agent_backend` /
  `scope_settings.agent_backend` are not route selectors; new routing must follow
  the selected Vibe Agent and its backend

Source-of-truth rule:

- when changing persistent product behavior, align with V2 config and current Web UI flows rather than legacy assumptions

## 5. Development Workflow

### Branching and Scope

- when starting a new feature or bug fix yourself, branch from the latest `master`
- if the user already put you on an existing branch/worktree, continue there unless asked to move
- keep commits small and focused; avoid mixing unrelated changes

### Planning for Non-Trivial Work

- if the task is complex or ambiguous, create a short plan before large changes
- capture background, goal, solution, and todo items in `docs/plans/`
- implementations should follow the plan and update it when scope changes materially
- if requirements are unclear, ask early before committing to a large direction

### Documentation Expectations

- update user documentation alongside user-visible features or changed workflows
- store project-specific plans, investigations, and summaries under `docs/`
- do not put ad-hoc project documentation in the repo root

### Worktrees

- use git worktree for long-running, parallel, or workspace-blocking efforts
- if detailed worktree workflow is needed, load the dedicated worktree skill

### Review Loop for PRs

- before opening a PR, run the reviewer subagent and fix significant issues first
- PR descriptions must name the changed capability and list the affected scenario IDs when a scenario catalog exists
- PR descriptions must state which evidence layers were updated: unit, contract, scenario, and residual manual checks
- after opening a PR, use the `background-watch-hook` skill to keep a review-fix loop running until Codex review passes
- by default, create the review watch immediately after the PR is opened; do not wait for the user to remind you unless they explicitly say not to keep a watch
- keep expensive full-suite gates on GitHub CI by default, then require those CI checks to pass before merge

### Pre-Push Requirements

- run the smallest relevant validation first, then broader checks as needed
- before `git push`, run `ruff check` on changed Python files at minimum
- fix lint errors before pushing; CI runs `pre-commit run --all-files` with Ruff
- do not require a full local CI run before opening or updating a PR; prefer focused local validation and let GitHub CI run the slow gates asynchronously

## 6. Coding Standards

### Language and i18n

- default to English for comments, docs, logs, and user-facing copy
- use non-English text only when required for localization/i18n
- backend user-facing strings must go through `vibe/i18n/`
- frontend user-facing strings must go through `ui/src/i18n/en.json` and `ui/src/i18n/zh.json`
- never hardcode user-visible display text in handlers, platform adapters, or React components

### Python and Module Conventions

- follow PEP 8 and 4-space indentation
- use `snake_case` for functions and `PascalCase` for classes/dataclasses
- add type hints for public functions where practical
- keep modules cohesive
- add new business logic under `core/handlers/` when it is platform-agnostic
- add new IM integrations under `modules/im/` and new agent backends under `modules/agents/`
- no repo-wide formatter is enforced; keep diffs focused if you use Black/Ruff

### Web UI Server

- `vibe/ui_server.py` is served by FastAPI/uvicorn; new UI routes should use native async FastAPI patterns where practical.
- `vibe/ui_compat.py` exists only as a migration scaffold for the old Flask-style route surface. Do not expand it into a general framework unless a migration regression requires it.
- Do not introduce per-request `asyncio.run()` bridges in UI request paths. Async helpers reached from UI handlers should be awaited directly on the ASGI event loop; blocking work should stay sync or move through a threadpool.

### Frontend (UI)

- source lives in `ui/`
- build command: `npm run build` from `ui/`
- built assets land in `ui/dist/` and are served by `vibe/ui_server.py`

Reuse design-system primitives — do not re-roll:

- buttons must use `Button` from `ui/src/components/ui/button.tsx`; pick a `variant` + `size` rather than hand-rolling `<button>` with custom Tailwind classes. Icon-only triggers use `variant="ghost" size="icon"` (with a `className` size override when the surrounding row is tight).
- status pills must use `Badge` (or `badgeVariants()` applied to a `<button>` when the pill needs to be clickable) from `ui/src/components/ui/badge.tsx`; pick a semantic variant (`success` / `warning` / `info` / `destructive` / `secondary`) instead of redefining border/bg/text colors.
- the same rule applies to every other primitive under `ui/src/components/ui/` (`Card`, `Input`, `Label`, `Popover`, `Dialog`, `Combobox`, `Separator`, ...): if a primitive already exists, extend it via `variant`, `size`, or `className` overrides — do not duplicate it inline.
- if no existing primitive fits, add a new variant (or a new primitive in `ui/src/components/ui/`) so the next caller can reuse it. Prefer adjusting the design-system layer over re-implementing the visual locally in a feature component.
- the source of truth for visual tokens (colors, radii, spacing, variant names) is `design.pen` — extend primitives to match its variant names so design ↔ code stay aligned.

**Reuse-first methodology (the reuse ladder).** This generalizes the rules above; it applies to shared backend logic too, not just UI:

- **Inventory before you build.** Survey what already exists — primitives, tokens, services, and how sibling features solved the same problem — and build from that inventory, not from scratch.
- **Walk the ladder in order:** reuse as-is → extend it (new `variant` / `size` / prop / arg) → promote a near-duplicate that lives in a feature folder into the shared layer so every caller gets it → only then build new, as a real reusable unit in its proper home, never an inline one-off.
- **Extract on the third repeat.** When the same markup / logic / constant recurs in ~3 places, lift it into one shared component or util and retrofit the existing callers. Touching N call sites with the same pattern means extracting the pattern, not pasting it N times — one concept, one home.
- Prefer extending the shared / design-system layer over patching the symptom in a feature file; the next caller should inherit the fix for free. Don't rush past reuse for speed — a clean, reusable change beats a fast local hack.

**Match the design pixel-for-pixel.** UI that drifts from the design is a defect, not a detail:

- `design.pen` is the visual source of truth; "looks roughly right" is not done.
- **Map every value to an exact token or class** — each size, weight, spacing, radius, color, and shadow corresponds to a specific token / utility. Look it up; don't eyeball it. If a needed token is missing, add it to the token layer first instead of hardcoding a one-off.
- **Verify by side-by-side comparison, not memory.** Render the built surface at the design's target viewport, place it next to the exported design frame, enumerate the deltas (spacing, type scale, color, radius, shadow, alignment), and fix until they match before calling it done.
- Confirm the utility classes you used actually resolve to the intended values — a class that silently no-ops (missing or aliased token) looks fine in code and wrong on screen.

Important packaging caveat:

- the installed `vibe` command uses packaged UI assets, not raw `ui/dist/` from the repo by default
- for local preview of packaged CLI/UI changes, build the UI and reinstall from a normal wheel; do not use `uv tool install --force --editable .` for the live local CLI
- do not run `python3 -m pip install -e .` against the system Python for validation; editable installs belong in a temporary venv or another explicitly isolated environment
- do not restart local `vibe` just to verify UI changes unless the user explicitly requests a local-service workflow and the session impact is understood

## 7. Testing and Validation

- prefer the smallest relevant checks first: focused pytest, targeted scripts, or narrow manual validation
- keep slow full-suite gates in GitHub CI rather than running them locally for every feature PR
- add tests when an existing test pattern already exists
- do not introduce a brand-new test framework unless requested

Testing guidance:

- use pytest-style tests (`test_<feature>.py`) colocated or under `tests/`
- for IM integrations, stub/mock platform clients and validate outbound payload/schema behavior
- for reusable capability-first testing guidance, use `standards/scenario-testing/AGENTS.md` as the entrypoint; project-specific scenario metadata lives under `tests/scenarios/`
- when a scenario catalog exists, make the scenario ID visible in the automated test and in the PR description
- for multi-step auth/setup flows, update `tests/scenarios/auth_setup/catalog.yaml` and add or update a closed-loop scenario harness case under `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`; keep provider-specific parsing and heuristics in focused unit tests
- for UI changes, run `npm run build` in `ui/`
- for cross-platform or user-facing verification, use the Incus regression workflow
- until CI fully covers a flow, do a manual sanity check for the affected workflow when practical

## 8. Git, Security, and Operational Safety

### Git Hygiene

- commit messages must use `type(scope): summary`
- never commit secrets such as tokens or credentials files
- avoid destructive git operations unless the user explicitly requests them

### Operational Safety

- keep `AGENT_DEFAULT_CWD` scoped to `_tmp/` or another sanitized directory
- logs may contain sensitive context; scrub before sharing them back
- be careful with persisted state under `~/.avibe/`, legacy `~/.vibe_remote/`, and `.runtime/incus-regression/`
- do not reset or wipe regression data unless the user explicitly asks for it

## 9. Release Notes

- tags follow the latest version number +1 (for example `v1.0.1` -> `v1.0.2`)
- before publishing a release, explicitly decide whether the version should notify users; add `<!-- avibe:update-notification=none -->` to the GitHub Release body when update and post-update notifications should be suppressed while automatic update behavior remains enabled. The legacy `vibe-remote` marker is still parsed for compatibility.
- GitHub-only pre-releases should use the `gh-vX.Y.ZrcN` format (for example `gh-v2.2.8rc2`) so they stay distinct from PyPI-triggering `v*` tags
- GitHub-only pre-releases must include installable artifacts in the GitHub release assets: a wheel built with `ui/dist` and bundled `vibe/show_runtime/*.tgz`, plus the sdist
- releases are published automatically by workflow after tagging/push
