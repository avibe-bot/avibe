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

## 2. Design Philosophy and Architecture

### Product and Technical Decision-Making

- Start from one coherent user mental model. Avibe absorbs backend and
  implementation differences; users and agents see only what they need to act.
- Establish hard constraints before choosing scope. Build the smallest complete
  solution that works within them, and leave concepts undefined until they have
  a clear responsibility.
- Prefer compatibility, permissive adoption, and subtraction. Add validation,
  metadata, state, or isolation only when it protects a demonstrated outcome.
- Reuse existing ownership and lifecycle before adding mechanisms. Keep the
  path simple and stateless, then optimize from measured evidence.

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
- `vibe/ui_server.py` and `vibe/api.py` run in a separate process from the
  controller and reach controller-owned state over the internal socket;
  `vibe/api.py` builds its own `AgentAuthService`. In-memory registries therefore
  cannot span IM and Web surfaces: cross-surface exclusion needs an explicit IPC
  hop or a documented per-instance scope. See PR #1417's Known-by-design ledger.
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

Use local `vibe` for local packaging or CLI behavior checks.

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
- `worktree` targets are temporary isolated environments; delete each one
  explicitly with
  `python3 scripts/incus_regression.py delete --target worktree --slug <slug> --yes`
  when merged, abandoned, or stale
- `reconcile` lists every worktree environment Incus holds — including ones
  created outside the runner, which no metadata-driven command can see — and
  forgets metadata rows whose environment is already gone. It never deletes an
  environment: no recorded field can prove one is unwanted, so that call stays
  with the operator. It reports the names the daemon gave, never re-derived from
  the slug, because a discovered name is bounded by what Incus accepts and
  `--slug` is stricter; one it would reject gets its objects named for a manual
  reclamation instead of a command that would exit on its own argument
- a metadata row is dropped only when the daemon that owns it completed a
  listing whose every entry was readable, that listing held neither its project
  nor its instance, and the row is not a reservation whose `up` may still be
  running. A reservation lives exactly as long as its run: an `up` that fails
  gives its row back while the daemon reports no project for that slug and the
  row is still the one that run wrote, both read at that moment rather than
  remembered from an earlier one — so a project that may bind the port, a
  listing that cannot answer, and a concurrent `up` that took the slug over all
  keep the row. `worktrees.json` is reached only through an accessor bound to the
  daemon it describes — it reserves host ports on this machine and records what
  this machine's daemon holds — so a `--remote` command cannot name it and
  neither reads nor writes it: `reconcile --remote` reports the remote inventory
  with no local provenance, `delete --remote` keeps the local row, and
  `up --remote` requires `--host-port`
- never use `--reset-config` / `--reset-all`, wipe regression state, or overwrite
  Avibe Cloud pairing / `remote_access` just to make probes pass unless asked
- after any regression update, verify service health before reporting success

State and lookup notes:

- regression product state lives under `/home/avibe/.avibe`; `/home/avibe/.vibe_remote` is only the compatibility symlink
- metadata lives under the primary checkout's `.runtime/incus-regression/`, even
  when the runner is invoked from a task worktree
- `.env.regression` is read from the current worktree first, then the primary checkout
- branch/master regression defaults to a locally built Show Runtime archive; packaged release installs use the packaged manifest path

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

Persisted-shape rule:

- on-disk artifacts written by any released version are a shipped surface, even
  behind a feature flag
- a schema change must load older releases' files via migration or safe
  degradation (a broken optional-feature section disables that feature and
  warns; startup never fails), with load fixtures covering the released shapes
- exception (owner ruling 2026-09-05): state written only by a feature the owner has
  declared pre-release — today the Model Hub behind `VIBE_MODEL_HUB_ENABLED` — is not
  a shipped surface; its older shapes may be dropped on load without migration,
  degradation, or compatibility fixtures, and specs for it describe the final state only
- Memory package upgrades are forward-only: success follows the ordinary restart
  path, while install, upgrade, or restart failures are structured terminal
  results and do not prevent a later explicit attempt. Do not add automatic
  package rollback, rollback plans, lifecycle reservations, quarantine, Gate 5
  verification, or recovery bootstrap.
- Memory Runtime manifest/hash/fetch/verify and backup safeguards protect
  published artifact availability. They are not installed-package rollback
  machinery; existing backup creation and manual restore primitives remain
  manual recovery tools.

Source-of-truth rule:

- when changing persistent product behavior, align with V2 config and current Web UI flows rather than legacy assumptions
- a successful `agents.*` runtime config save must also reconcile any live
  controller through the backend rolling-refresh path; persisted config and
  built-in Agent rows alone do not update the in-memory backend registry, and
  callers must not issue a second restart after the config API accepts that
  reconciliation

## 5. Development Workflow

### Planning and Documentation

- if the task is complex or ambiguous, create a short plan before large changes
- capture background, goal, solution, and todo items in `docs/plans/`
- implementations should follow the plan and update it when scope changes materially
- if requirements are unclear, ask early before committing to a large direction
- update user documentation alongside user-visible features or changed workflows
- keep project-specific plans, investigations, and summaries under `docs/`, never in the repo root

### PR Delivery

- load and follow the `pr-delivery-loop` skill for every implementation task;
  it owns the detailed procedure, but cannot weaken the baseline below
- regardless of Skill resolution, use a task branch/worktree, record the change
  contract, require an exact-head Codex review, zero unresolved review threads,
  and passing CI before close-out, apply the review-loop circuit breaker, and
  never merge without explicit owner instruction
- the fallback change contract names the intended behavior, affected boundaries,
  and validation evidence; pause patching when one root-cause class appears on
  two reviewed heads, or after three findings-bearing heads following an
  architecture or data-model rewrite, then diagnose the whole class before
  continuing
- use the `background-watch-hook` skill for managed review and CI waits
- keep one durable `--forever` combined PR/CI Watch and disable the Watch's per-cycle timeout
- only an explicit owner decision may make Codex findings advisory for an
  architecture/spec-only PR; ordinary documentation and every product or test
  code PR retain the Skill's normal gates

### Pre-Push Requirements

- run the smallest relevant validation first, then broader checks as needed
- before `git push`, run `ruff check` on changed Python files at minimum
- for UI changes, run `npm run build` in `ui/` before pushing
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
- follow the reuse ladder for UI and shared backend logic: inventory existing patterns -> reuse (`ui/src/components/ui/` primitives such as `Button`, `Badge`, `Card`, `Input`, `Popover`, `Dialog` first) -> extend via variants/sizes/props -> promote near-duplicates -> create a reusable unit only when needed; extract on the third repeat
- `../avibe-docs/design.pen` is the visual source of truth; map spacing, type, radius, color, and shadow to exact tokens/classes, add missing tokens instead of hardcoding, and verify against the exported frame when visual fidelity matters
- installed `vibe` uses packaged UI assets, not raw repo `ui/dist/`; for packaged CLI/UI preview, build UI and reinstall from a normal wheel — never `uv tool install --force --editable .`, and never an editable install against system Python

## 7. Testing and Validation

- add tests when an existing test pattern already exists
- do not introduce a brand-new test framework unless requested
- use pytest-style tests (`test_<feature>.py`) colocated or under `tests/`
- for IM integrations, stub/mock platform clients and validate outbound payload/schema behavior
- for reusable capability-first testing guidance, use `standards/scenario-testing/AGENTS.md` as the entrypoint; project-specific scenario metadata lives under `tests/scenarios/`
- when a scenario catalog exists, make the scenario ID visible in the automated test and in the PR description
- treat CLI examples in injected system prompts as live callers: update them with CLI flag changes and keep parser-backed contract coverage so unsupported examples cannot ship
- for multi-step auth/setup flows, update `tests/scenarios/auth_setup/catalog.yaml` and add or update a closed-loop scenario harness case under `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`; keep provider-specific parsing and heuristics in focused unit tests
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
- before publishing a release, explicitly decide whether the version should notify users; put `<!-- avibe:update-notification=none -->` in the annotated tag message when update and post-update notifications should be suppressed while automatic update behavior remains enabled. The workflow emits both the current and legacy `vibe-remote` markers into the GitHub Release body for installed-client compatibility.
- for an official `v*` release, the annotated tag is the operator's only release-state input: put silent-update intent in the tag annotation, push the tag, and do not pre-create or manually edit a GitHub Release
- the official workflows stage assets and generated notes in a Draft; `Release (AI Notes)` may update notes but never publishes. The `Publish to PyPI` workflow is the single finalizer: it verifies the exact notes run, publishes the asset-complete GitHub Release, and only then allows PyPI publication so package manifests never point at private Draft assets
- GitHub-only pre-releases should use the `gh-vX.Y.ZrcN` format (for example `gh-v2.2.8rc2`) so they stay distinct from PyPI-triggering `v*` tags
- GitHub-only pre-releases must include installable artifacts in the GitHub release assets: a wheel built with `ui/dist` and bundled `vibe/show_runtime/*.tgz`, plus the sdist
- releases are published automatically by workflow after tagging/push
- Published managed-runtime manifests are availability contracts. Keep their release URLs under a scheduled manifest-verified backup/recovery guard, and publish the new assets before changing a pinned manifest so the guard never restores bytes from a different release.
