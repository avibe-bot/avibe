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
- **Tests and probes must be hermetic by default.** Treat `$HOME`, XDG dirs,
  keychains, CLI config/token stores, running services, browser profiles, and
  cloud accounts as production data unless the user explicitly asks otherwise.
- Any test that reaches write-capable production paths must redirect the whole
  call path to test-owned state and prove a representative write cannot touch
  real local or external user state; `uses_real_paths` tests must remain read-only.
- Unless the user explicitly asks otherwise, use the Incus regression environment for user-facing verification.

### Regression Testing (Incus)

When the user says `回归测试`, update the latest code into the existing **local**
Incus regression environment, preserve accumulated product state unless reset is
explicitly requested, then let the user verify Slack, Discord, Feishu/Lark, and
WeChat behavior.

Entry points:

- default: `./scripts/run_regression.sh`
- direct: `python3 scripts/incus_regression.py up --target master`
- macOS/Lima: `INCUS_CMD="limactl shell avibe-incus-regression -- sudo incus" ./scripts/run_regression.sh`

Hard rules:

- local Incus only for development regression; never use `--remote`, SSH, remote
  tenant projects, demos, or customer/user environments unless explicitly asked
  for remote ops
- use the runner, not raw Incus commands; it owns naming, source sync, state
  preparation, readiness checks, Show Runtime setup, metadata, and cleanup
- `master` is the long-running unified four-platform environment; keep it online,
  preserve product state, sync source, and restart the service in place
- `worktree` targets are temporary isolated environments; delete with
  `python3 scripts/incus_regression.py delete --target worktree --yes` or
  `cleanup-stale --yes` when merged, abandoned, or stale
- never use `--reset-config` / `--reset-all`, wipe regression state, or overwrite
  Avibe Cloud pairing / `remote_access` just to make probes pass unless asked
- after any regression update, verify service health before reporting success

State and lookup notes:

- regression product state lives under `/home/avibe/.avibe`; `/home/avibe/.vibe_remote` is only the compatibility symlink
- metadata lives under the primary checkout's `.runtime/incus-regression/`, even
  when the runner is invoked from a task worktree
- `.env.regression` is read from the current worktree first, then the primary checkout
- branch/master source checkouts default `REGRESSION_SHOW_RUNTIME_SOURCE=github-source`; packaged release installs should use the packaged manifest path

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
- use the skill's bundled `wait_pr.py` / `wait_action.py` for PR review and CI watches; do not hand-roll PR waiters
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

- source lives in `ui/`; build with `cd ui && npm run build`; `ui/dist/` is served by `vibe/ui_server.py`
- reuse `ui/src/components/ui/` primitives first (`Button`, `Badge`, `Card`, `Input`, `Popover`, `Dialog`, etc.); extend via variants/sizes/props before creating new primitives
- follow the reuse ladder for UI and shared backend logic: inventory existing patterns -> reuse -> extend -> promote near-duplicates -> create a reusable unit only when needed; extract on the third repeat
- `design.pen` is the visual source of truth; map spacing, type, radius, color, and shadow to exact tokens/classes, add missing tokens instead of hardcoding, and verify against the exported frame when visual fidelity matters
- installed `vibe` uses packaged UI assets, not raw repo `ui/dist/`; for packaged CLI/UI preview, build UI and reinstall from a normal wheel, not `uv tool install --force --editable .`
- do not run editable installs against system Python, and do not restart local `vibe` for UI checks unless the user explicitly requests that local-service workflow

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
