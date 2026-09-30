# Workbench Dependencies — Settings tab + askill auto-install

Status: in progress (branch `feature/dependencies-settings`)

## Background

The Skills page wraps the open-source **askill** CLI. Today askill is *not*
installed for the user: if it is missing, `SkillsPage` shows a dead-end
"not installed" screen (`ui/src/components/workbench/SkillsPage.tsx:291`) and
the user must install it by hand.

By contrast, the **Show Page runtime** is a required local runtime that Vibe
Remote installs and keeps current automatically: `core/show_runtime.py`
(`ShowRuntimeManager`) plus `vibe runtime prepare --strict`, which is invoked
from every install/upgrade path:

- CLI upgrade → `_prepare_show_runtime_after_install` (`vibe/cli.py:4605/4686`)
- UI upgrade (no auto-restart) → `_prepare_show_runtime_after_upgrade` (`vibe/api.py:1647/1680`)
- UI upgrade (auto-restart) → `schedule_restart(prepare_show_runtime=True)` → `restart_supervisor`

All three funnel through **`vibe runtime prepare`** (`cmd_runtime`, `vibe/cli.py:4661`).

## Decision (user, 2026-05-31)

askill is a **required** local dependency and must be **auto-installed at the
same lifecycle point as the Show Page runtime** — not left as a manual
dead-end. Add a Settings → **Dependencies** tab to surface status and offer
manual re-check / repair / update.

## Goals

1. Auto-install + keep-updated askill wherever the Show Page runtime is prepared.
2. New Settings → **Dependencies** tab: askill, Show Page runtime, Node.js
   (+ a link to Backends), each with status + manual actions.
3. Replace the Skills "not installed" dead-end with a one-click install.
4. **Reuse** existing infra — no re-rolling (reuse ladder).

Non-goal (Phase 2): unify backends + askill + show runtime into one declarative
"managed local tool" registry. This phase keeps askill + show-runtime explicit.

## Install mechanism (verified)

> Superseded: Avibe now downloads a pinned askill binary mirror-first and
> verifies its sha256; see `release-download-mirror.md`. The original mechanism
> follows.

- Official one-liner: `curl -fsSL https://askill.sh | sh` (same shape as the
  OpenCode installer already in `install_agent`).
- No npm fallback: askill ships via the askill.sh installer, not a public npm
  package, so a curl/bash-less host (e.g. Windows) is told to install manually
  rather than hit a guaranteed-failing `npm i -g`.
- Target version: askill v0.1.13+.

## Backend (`vibe/api.py`, `vibe/cli.py`, `vibe/ui_server.py`)

- `install_askill()` — build the curl installer command (npm fallback) and run
  it through the existing `_run_install_command(...)`. That helper already
  skips the `agents.<name>.cli_path` write when there is no `agents.askill`
  attribute (verified `api.py:2327`), so it is safe for a non-agent tool.
- `ensure_askill_installed(force=False)` — `resolve_cli_path("askill")`; if
  present and not forced, no-op; else `install_askill()`. Idempotent.
- Hook askill into **`vibe runtime prepare`** (`cmd_runtime`): after the show
  runtime is prepared, call `ensure_askill_installed()` and include askill in
  the status/output. Respect a `VIBE_INSTALL_SKIP_ASKILL` escape hatch (mirrors
  `VIBE_INSTALL_SKIP_SHOW_RUNTIME`). This is the single chokepoint, so askill
  auto-installs in the same place as the show runtime — no new call sites.
- `dependencies_status()` — aggregate: askill (`resolve_cli_path` + `askill
  --version`), show runtime (`ShowRuntimeManager.status()`), Node.js
  (`resolve_cli_path("node")` + version), backends summary (count installed via
  the existing `get_backend_runtime`). Returns `{ok, deps:[{id, label, kind,
  required, installed, version, status, detail, action}]}`.
- Manual actions reuse the background install-job machinery
  (`start_agent_install_job` / `get_agent_install_job`), generalized to accept
  `askill` (relax the `is_agent_backend` gate; the worker dispatches askill →
  `install_askill()`, no `restart_backend`). Show-runtime repair → run `vibe
  runtime prepare --force`.
- Routes (thin, `@app.route`): `GET /api/dependencies`, and a unified
  `POST /api/dependencies/<dep>/install` + `GET .../install/<job_id>` for both
  askill (install/reinstall) and show-runtime (repair, via prepare --force).
- Tests (`tests/test_local_deps.py`, hermetic — monkeypatch the subprocess
  boundary): askill install command construction, `ensure` idempotency, the
  status aggregation shape, and the job-runner generalization.

## Frontend (`ui/`)

- Nav: add `dependencies` to `SettingsPageShell.tsx` `TABS` (after Backends,
  icon `package`) + the `SettingsTab` union; route `/admin/settings/dependencies`
  in `App.tsx` (+ legacy `/settings/dependencies` redirect).
- `SettingsDependenciesPage.tsx` — reuse `SettingsPanel` / `SettingsRow` +
  `Badge` status pills + `Button` actions + the install-job polling already in
  `ApiContext` (generalize `installAgent` → `installDependency`). Rows: askill
  (REQUIRED), Show Page runtime (REQUIRED), Node.js, Agent backends (dim, links
  to Backends). Auto-managed banner up top. Matches the design.pen frame
  `vibe-remote — Settings · Dependencies (Dark)`.
- i18n: `settings.tabs.dependencies` + `settings.dependencies.*` in
  `en.json` + `zh.json` (1:1).

## Skills dead-end

- `SkillsPage` `notInstalled` block → add a primary **Install askill** button
  (reuse the install-job + poll); on success, re-run `refresh()`.

## Evidence layers

- Unit: `tests/test_local_deps.py`.
- Build/type/lint: `tsc --noEmit`, `vite build`, `ruff`.
- Residual manual (NOT runnable in the sandbox — AGENTS forbids mutating local
  `~/.vibe_remote`): live install against real `askill.sh`, and the
  auto-install hook firing during a real `vibe upgrade` / `runtime prepare`.

## Todo

D1 inventory ✓ · D2 plan ✓ · D3 backend · D4 frontend · D5 skills dead-end ·
D6 validate + reviewer + PR + Codex watch.
