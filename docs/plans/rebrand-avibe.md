# Rebrand Plan: Vibe Remote → avibe ("the Agent OS")

Status: draft · branch `rebrand-avibe` (synced to origin/master #481) · 2026-06-04

## 1. Background

Vibe Remote is repositioning from "middleware that bridges AI agents to IM
platforms" to an **Agent OS**: one install command turns a machine into an
environment an agent lives in, and the user operates the whole system by
talking to that agent (Web or IM). The Web app is already feature-complete
(chat, configure agent / model / Skills / Harness / Webhook). `pyproject.toml`
already describes the product as a "Local-first agent runtime".

Brand: **avibe** (domain `avibe.bot`). GitHub org: **avibe-bot** (already live —
`avibe-bot/vibe-show-runtime` is referenced by `core/show_runtime.py`).

## 2. Decisions

Locked:
- **CLI command stays `vibe`.** No rename, no alias.
- **Repo name = `avibe`** under the `avibe-bot` org.
- **Backward compatibility for existing users is required** (config/state, install entry, update check).

Layered naming (resolves "avibe vs avibe-os" — different layers, so we keep both
the OS flag and the escape hatch):

| Layer | Value | Notes |
|---|---|---|
| Brand / domain | `avibe` / `avibe.bot` | trend-neutral, permanent escape hatch |
| GitHub repo | `avibe` | under `avibe-bot` |
| PyPI distribution | `avibe-os` | `avibe` is taken on PyPI; dist name ≠ import name ≠ command |
| CLI command | `vibe` | unchanged |
| Python import packages | `vibe`, `config`, `core`, `modules`, `storage` | unchanged (renaming = pure churn) |
| Category / tagline | "the Agent OS" | revisable copy, not identity |

Runtime home dir (Alex's call): **physically rename `~/.vibe_remote` → `~/.avibe`
on upgrade, then create a back-symlink `~/.vibe_remote` → `~/.avibe`.** Refined
robustness rules in §5a.

## 3. Verified current-state inventory (source of truth for the sweep)

From `pyproject.toml`, `AGENTS.md`, live CLI, and a repo-wide grep:
- PyPI dist name: **`vibe-remote`** (`[project].name`).
- Command: **`vibe`** → `vibe.cli:main` (`[project.scripts]`).
- Wheel packages: **`vibe`, `config`, `core`, `modules`, `storage`** — import surface is multi-package, NOT a single `vibe_remote`.
- Runtime home: `~/.avibe/` (`state/`, `logs/`), env `AVIBE_HOME`, log file `vibe_remote.log`; pytest autouse `AVIBE_HOME` isolation + marker `uses_real_paths`. Old `~/.vibe_remote/` remains a directory migration path.
- i18n: backend `vibe/i18n/`; frontend `ui/src/i18n/{en,zh}.json`. No hardcoded user-facing strings (AGENTS.md §6).
- Other repos: `avibe-bot-backend` (keep), `avibe-docs` (keep; domain on-brand, body copy not).

### Machine-critical endpoint inventory (the parts that can strand old users)
| Endpoint | Location | After-transfer risk |
|---|---|---|
| **Self update-check** | `core/update_checker.py` checks versions from PyPI (`https://pypi.org/pypi/vibe-remote/json`) and reads GitHub release bodies from `api.github.com/repos/cyhhao/vibe-remote/releases/tags` for update-notification policy before the transfer. | **HIGH** — the version source must move to `avibe-os` when the new package ships; the GitHub release-body lookup must move to `avibe-bot/avibe` at transfer and/or rely on GitHub's redirect only for old clients. |
| Install one-liner | Public entry is already `avibe.bot`; before transfer the hosted backend redirected to `raw.githubusercontent.com/cyhhao/vibe-remote/master/install.sh` / `.ps1`, and the scripts used `REPO="cyhhao/vibe-remote"`. | MED — `raw.githubusercontent.com` does NOT reliably redirect after rename. Fix: update the avibe.bot backend redirect target to the new repo path at transfer time, and update script fallback repo metadata in this branch. |
| npm entry | `npm/avibe/bin/avibe.js` hardcodes the two raw install URLs | MED — update raw URLs. (We already hold the `avibe` npm name via `npm/avibe`.) |
| Agent system-prompt link | `core/system_prompt_injection.py` → `github.com/avibe-bot/avibe/raw/master/skills/use-avibe/SKILL.md` | LOW-MED — `github.com/.../raw/` web path redirects better than raw.githubusercontent; update anyway. |
| Package URLs | `pyproject.toml` `[project.urls]` ×4 | LOW — update on transfer. |
| Show Runtime archive | `core/show_runtime.py` → `avibe-bot/vibe-show-runtime` | NONE — already on-brand. |
| Docs / README / VISION / skill examples / tests | many `cyhhao/vibe-remote` strings | LOW — bulk sweep. |

### Key insight (de-risks the whole project)
The command and Python import packages are already stable (`vibe`, `config`,
...), NOT `vibe_remote`. This rebrand is **not a code-identifier churn**. The
real surface is: (a) PyPI distribution name, (b) GitHub repo + the endpoint
table above, (c) brand/display strings, (d) the runtime-home-dir migration.

## 4. Workstreams (commits on `rebrand-avibe`, one PR at the checkpoint)

- **W1 — Repoint machine-critical endpoints, THEN transfer GitHub** (sequencing matters; see §5c).
- **W2 — Runtime home + env compat** (§5a).
- **W3 — Distribution: `avibe-os` + `vibe-remote` shim** (§5b).
- **W4 — Brand/display copy** via `vibe/i18n/` + `ui/src/i18n/{en,zh}.json`, README, package description. Never hardcode. EN/ZH lockstep.
- **W5 — Docs** (`avibe-docs`), EN/ZH 1:1; commit + push to `main` (no PR) per that repo's convention.
- **W6 — Skill + integrations**: `use-avibe` skill name + SKILL.md raw URL.
- **W7 — IM bot re-registration** (external; Slack/Discord/Telegram/Lark display names, OAuth redirects, app-directory review — start early).

## 5. Backward-compat designs (the careful part)

### 5a. Home dir + env (rename + back-symlink, hardened)
Adopt Alex's model: on upgrade, `rename(~/.vibe_remote → ~/.avibe)`, then create
symlink `~/.vibe_remote → ~/.avibe`. Hardening so it never strands anyone:
- **The in-code resolver is the real guarantee, not the symlink.** Resolution
  order in `config/paths`: `AVIBE_HOME` → `~/.avibe` if exists →
  `~/.vibe_remote` if exists (adopt) → default `~/.avibe`.
  The symlink is a convenience for stale absolute references, not the mechanism.
- **Atomic + idempotent + run-before-live.** Do the migration at CLI startup
  BEFORE the service binds or caches any path (the live `vibe` process may be the
  agent runtime — never migrate under a running service). If rename succeeds but
  symlink creation fails, a later startup re-creates the missing symlink.
- **Conflict rule.** If both `~/.avibe` (real) and `~/.vibe_remote` (real, not a
  symlink) exist: prefer `~/.avibe`, do NOT clobber, emit a one-time warning.
- **Windows fallback.** Symlinks need admin/Dev Mode; make the symlink
  best-effort and rely on the resolver (or a directory junction) there.
- Covers: `state/`, `logs/`, sessions, scheduled tasks, watches, Show Page
  workspaces, `remote_access` pairing, agent CLI homes. Container/regression
  homes (e.g. `/data/vibe_remote`) are separate paths — handle independently.
- Tests: every resolver branch; old-user simulation (only `~/.vibe_remote/`) →
  no data loss, notice shown once; both env vars honored.

### 5b. PyPI (you cannot rename a PyPI project)
`avibe-os` is a NEW project; PyPI has no rename. Migration without dual-publishing forever:
1. Going forward, **`avibe-os` is the real package** — single publish per release (update the release workflow, AGENTS.md §9). The first avibe release is **3.0.0**.
2. **One-time `vibe-remote` shim release**: a thin dist that declares
   `[project.scripts] vibe = "vibe.cli:main"` and `dependencies = ["avibe-os>=3.0.0"]`.
   Then `pip/uv install -U vibe-remote` keeps pulling the latest real code (via
   the dep) and still exposes `vibe`. Published once, not every release.
   The shim itself should publish as `vibe-remote==3.0.0`, otherwise existing
   clients that still check `https://pypi.org/pypi/vibe-remote/json` will not
   discover the avibe migration release.
3. **Seamless onto the new name** = via the app's own updater / install script:
   detect an old `vibe-remote` tool install and re-install as `avibe-os`
   (`uv tool uninstall vibe-remote && uv tool install avibe-os`, or pip
   equivalent). This is the only lever that actually moves users to the new
   name; PyPI can't do it.
4. Users who never re-install stay on the shim and keep working indefinitely.

### 5c. GitHub transfer safety (keep the redirect alive)
- **Sequencing: repoint first, transfer second.** Ship a release that updates the
  endpoint table (§3) — especially PyPI package metadata and GitHub release-body
  lookups in `update_checker.py` — to the new package/repo targets, let it roll
  out, THEN transfer. New clients stop depending on the redirect; only the tail
  of old clients relies on it.
- **The redirect dies only if the old name is recreated.** Hard rule: never
  create a repo at `cyhhao/vibe-remote` (or `avibe-bot/vibe-remote`) again. This
  is pure discipline — there is no "occupy + redirect" both-ways option.
- **Public install URL stays decoupled through `avibe.bot`.** The user-facing
  install URL already lives on `avibe.bot`; after transfer, update the hosted
  backend's GitHub raw target rather than changing the public command.
- **Update source remains PyPI-first.** Version availability is checked through
  PyPI, consistent with release publishing. GitHub is still used for release
  URLs and release-body notification policy.
- **Test before committing**: confirm the updater's HTTP client follows the
  GitHub API 301 from a renamed repo for the release-body lookup; update the
  hosted install backend's raw GitHub target at transfer; update `npm/avibe/bin`
  raw URLs; update SKILL.md link + `pyproject.toml` URLs.

## 6. Codex collaboration (division of labor)
- **Claude (lead execution)**: edits across repos, build the resolver/shims/migration, run ruff + focused pytest + local Incus regression + `npm run build`, open the PR, verify (incl. old-user simulation + the API-redirect test).
- **Codex (thoroughness / adversarial)**:
  - C1 — independent exhaustive reference sweep across all three repos; reconcile against §3.
  - C2 — adversarial review of §5a resolver/migration (state paths, first-run races, run-before-live, Windows, regression shared-state) and §5c sequencing.
  - C3 — mandated pre-merge Codex review (AGENTS.md §5).
- **Invocation** (both verified):
  - Native/dogfood: `vibe agent run --agent codex --message "<task>" [--session-id <id>]` (an enabled `codex` Vibe Agent already exists: backend codex, gpt-5.5, effort low; runs are async by default and post back; add `--sync` only when the terminal must wait).
  - Direct CLI: `codex exec --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --cd <dir> "<prompt>"` (codex-cli 0.130.0). Synchronous JSON, full control of reasoning effort — preferred for tight-loop sub-tasks and meticulous audits (the existing agent runs effort "low").

## 7. Testing / validation
- ruff on changed Python before push (AGENTS.md §5).
- focused pytest on the home/env resolver (`test_upgrade_flow.py` already exists — extend it).
- local Incus regression for user-facing flows; do NOT reset state; confirm old-home users load cleanly.
- `npm run build` for UI copy changes.
- live test: updater follows GitHub API 301 after a simulated rename.

## 8. Sequencing
P0 decisions (this doc) → **P1 repoint endpoints (esp. update_checker) + roll out**
→ P2 GitHub transfer (never recreate old name) → P3 home/env migration (early,
hard testing) → P4 distribution (`avibe-os` + shim) → P5 brand/UI/docs → P6 IM
re-registration (parallel, external). One branch, many commits, one PR at the checkpoint.

## 9. Execution checkpoint: latest master sync

Before implementation, the task worktree was fast-forwarded from
`origin/master` to `f8cc2453` (`ci: run npm wrapper on avibe runner (#481)`).
The only remaining local change at this point is this untracked plan document.

## 10. Reference sweep classification rubric

The initial sweep must classify each `Vibe Remote` / `vibe-remote` /
`vibe_remote` / `VIBE_REMOTE` occurrence by product layer, not by blind string
replacement.

### Keep

Keep references that are part of a stable compatibility surface or historical
record:
- CLI command and user shell command examples using `vibe`.
- Python import packages and code identifiers: `vibe`, `config`, `core`,
  `modules`, `storage`.
- Deprecated compatibility inputs that must remain accepted, especially old
  `~/.vibe_remote` path handling.
- Tests and fixtures that intentionally simulate old users, old package names,
  old paths, or old GitHub URLs.
- Migration IDs, historical release notes, and compatibility prose where the old
  name is necessary to explain what is being migrated.

### Change

Change references that define the current/future product identity or future
machine-readable endpoints:
- User-facing brand copy: UI strings, backend i18n messages, README, package
  description, docs, skill prose, install pages.
- Future canonical repo/package metadata: GitHub URLs, `[project.urls]`,
  release workflow references, install docs, package manager examples.
- Machine-critical endpoints in new releases: update checker PyPI package name,
  GitHub release-body repo path, npm installer raw URLs, skill raw URL, and
  install script fallback repo/package metadata.
- Runtime defaults: introduce `AVIBE_HOME`, make `~/.avibe` the default, and
  present old paths only as deprecated compatibility.
- Docker image/package names that represent current public distribution rather
  than legacy fixture state.

### External

Track references that cannot be fully changed by a repo commit:
- Owned short install URL target and any CDN/redirect rule behind it.
- GitHub transfer from `cyhhao/vibe-remote` to `avibe-bot/avibe`.
- PyPI publishing of `avibe-os` and the one-time `vibe-remote` shim.
- npm package publication and validation after installer URL changes.
- Slack/Discord/Telegram/Lark/Feishu/WeChat bot display names, OAuth redirect
  URLs, app-directory review, and any platform-side branding.
- Hosted `avibe.bot` install backend redirect target after the GitHub transfer.

### Needs Alex decision

Resolved:
- The public install URL already is an `avibe.bot` URL. Keep that public entry;
  after transfer, update the hosted backend's GitHub raw target to the new
  `avibe-bot/avibe` install script path.
- Update version checks should remain PyPI-first because releases are published
  on PyPI. Move the package source from `vibe-remote` to `avibe-os` for the
  avibe line; keep GitHub only for release URLs and release-body notification
  policy.
- The first avibe release will be `3.0.0`; the one-time `vibe-remote` shim
  should depend on `avibe-os>=3.0.0`.
- The physical home-dir migration belongs in the first rebrand PR; do it
  end-to-end rather than deferring the move after resolver work.
- Public copy may temporarily say `formerly Vibe Remote` where that helps
  existing users understand the transition.

Still do not guess:
- Timing of the GitHub transfer relative to the endpoint-repoint release.

### Review carefully, do not bulk-replace

Some references are likely mixed-purpose and need per-occurrence judgment:
- `vibe_remote.log` and other filenames: decide whether they are user-visible
  current defaults, legacy compatibility, or migration targets.
- `/data/vibe_remote` and regression paths: preserve existing state unless an
  isolated migration path is explicitly tested.
- `use-avibe` skill naming: update public/raw links and prose, but avoid
  breaking existing skill lookup until a compatibility alias is defined.
- `vibe-remote` in dependency metadata: current real package should become
  `avibe-os`, while old-package references remain only for shim/migration.

## 11. Reference sweep results

Scope: `vibe-remote` task worktree, `avibe-bot-backend`, and `avibe-docs`.
Generated/build folders were excluded (`.git`, `node_modules`, `.next`, `dist`,
`build`, `.venv`, `__pycache__`). The project-level `design.pen` and unrelated
parallel worktrees were not counted in this text sweep.

Token totals:

| Repo | Matching files | Matching tokens |
|---|---:|---:|
| `vibe-remote` | 262 | 1912 |
| `avibe-bot-backend` | 22 | 106 |
| `avibe-docs` | 40 | 179 |

`vibe-remote` token mix: `Vibe Remote` 736, `VIBE_REMOTE` 438,
`vibe_remote` 378, `vibe-remote` 360. The biggest file groups are `docs/`
(80 files), `tests/` (71), `ui/` (20), `vibe/` (15), `modules/` (14), and
`core/` (12).

### Change checklist

These are current/future product identity or machine-readable targets and should
change in implementation.

- `pyproject.toml`, `uv.lock`, `.github/workflows/publish.yml`,
  `.github/workflows/release_ai.yml`, `.github/workflows/lint.yml`: move the
  real distribution from `vibe-remote` to `avibe-os`, keep `vibe` script, update
  project URLs to `avibe-bot/avibe`, and update CI/release checks that assert
  `vibe-remote` output.
- `vibe/upgrade.py` and `core/update_checker.py`: update PyPI metadata source
  to `avibe-os` for the avibe line, update release tag/API bases to
  `avibe-bot/avibe`, and keep GitHub release body marker parsing compatible.
- `install.sh`, `install.ps1`, `npm/avibe/bin/avibe.js`,
  `npm/avibe/package.json`, `npm/avibe/README.md`: install the real package as
  `avibe-os`, use GitHub fallback URLs under `avibe-bot/avibe`, keep command
  name `vibe`, and adjust help text from "underlying vibe-remote Python CLI" to
  the new package layer.
- `README.md`, `README_ZH.md`, `VISION.md`, `VISION_ZH.md`, `SECURITY.md`, and
  user-facing docs under `docs/`: update current brand/product copy to
  `avibe` / Agent OS framing, but preserve old name only in migration/history
  context.
- Backend i18n and frontend i18n: update `vibe/i18n/en.json`,
  `vibe/i18n/zh.json`, `ui/src/i18n/en.json`, and `ui/src/i18n/zh.json` in
  lockstep; do not hardcode replacement text in React or handlers.
- UI metadata and assets: `ui/index.html`, `ui/public/manifest.webmanifest`,
  `ui/src/components/AppShell.tsx`, `ui/src/components/RemoteAccess.tsx`,
  `ui/src/components/settings/SettingsServicePage.tsx`, and related UI
  components still expose `Vibe Remote`, `~/.vibe_remote`, or `vibe_remote.log`.
- Bot/platform setup surfaces: `vibe/templates/slack_manifest.json`, README
  examples such as `@Vibe Remote /start`, and platform setup docs should become
  on-brand. Actual platform-side app renames remain external.
- Sentry and HTTP user agents: `vibe/sentry_integration.py`,
  `vibe/api.py`, and `vibe/remote_access.py` still use `vibe-remote` /
  `Vibe Remote` release or user-agent labels. Decide whether to move these to
  `avibe-os@<version>` / `avibe` while preserving search compatibility.
- `avibe-bot-backend/next.config.ts`: public `/install.sh` remains
  `https://avibe.bot/install.sh`, but the backend redirect target must be
  changed from `cyhhao/vibe-remote` raw to `avibe-bot/avibe` raw at transfer.
- `avibe-bot-backend/lib/links.ts`, `lib/i18n/messages.ts`, email templates,
  `app/page.tsx`, `app/layout.tsx`, login/console logo alt text, and
  `public/vibe-remote-logo.png`: update GitHub URL, public copy, logo naming,
  metadata, and EN/ZH copy together.
- `avibe-docs`: update `index`, `quickstart`, `get-started/install`,
  `ai/install-for-ai`, `reference/commands`, concept pages, platform pages,
  and all `zh/` mirrors. Install command stays `curl -fsSL
  https://avibe.bot/install.sh | bash`; GitHub audit links and PowerShell raw
  URL move to the new repo path after transfer.

### Keep / compatibility checklist

These references should stay, or be retained as aliases, because they are
runtime compatibility surfaces.

- CLI command `vibe` and Python package imports `vibe`, `config`, `core`,
  `modules`, `storage` stay unchanged.
- `AVIBE_HOME` is the only runtime-home env var.
- Old `~/.vibe_remote` remains a migration/adoption path. Tests should include
  an old-user-only home and a removed-legacy-env case.
- The old PyPI project name `vibe-remote` remains in the one-time
  `vibe-remote==3.0.0` shim and migration tests.
- Existing release marker `<!-- vibe-remote:update-notification=none -->`
  should continue to parse for old release bodies; an `avibe` marker may be
  added, but the old marker should not stop working during the transition.
- Tests that intentionally simulate old paths, old package names, old GitHub
  redirect behavior, or old install outputs should be kept and renamed only
  where the assertion is no longer testing legacy compatibility.
- `__Host-vibe_remote_session` and `__Host-vibe_remote_oauth` are cookie names;
  changing them logs users out. Treat as a separate compatibility decision, not
  a brand replacement.
- OpenCode config metadata key `vibe_remote` marks Vibe-managed providers and
  models. Keep it or add an alias path; do not bulk-rename and orphan existing
  OpenCode configs.
- Regression/container data paths such as `/data/vibe_remote` and existing
  regression state roots must be preserved unless an isolated migration is
  explicitly tested. Do not reset regression state.
- `use-avibe` skill name, raw URL, and tests need compatibility handling.
  Update public prose/URLs, but keep an alias or old lookup path until callers
  are migrated.

### External checklist

These cannot be completed by the `vibe-remote` repo PR alone.

- GitHub transfer: `cyhhao/vibe-remote` to `avibe-bot/avibe`, and never
  recreate the old repo name.
- Hosted install backend: update `avibe.bot/install.sh` redirect target in
  `avibe-bot-backend` at the transfer point.
- PyPI: publish `avibe-os==3.0.0` as the real package and one-time
  `vibe-remote==3.0.0` shim with `avibe-os>=3.0.0`.
- npm: publish updated `@avibe/cli` after installer raw URLs and package copy
  move.
- IM platform app registration: Slack, Discord, Telegram, Lark/Feishu, and
  WeChat display names, icons, app-directory/review surfaces, and OAuth
  redirects.
- Product Hunt badge and other third-party badges/links in README may need
  external updates or replacement if they still identify the old product.

### Repository-specific notes

`vibe-remote`:
- High-priority implementation files are `config/paths.py`, `vibe/upgrade.py`,
  `core/update_checker.py`, `install.sh`, `install.ps1`, `pyproject.toml`,
  release workflows, `npm/avibe/*`, i18n JSON files, README/vision docs, and
  focused tests (`tests/test_upgrade_flow.py`, `tests/test_update_checker_platforms.py`,
  `tests/test_install_script.py`, `tests/test_v2_paths.py`).
- `docs/plans/` contains many historical occurrences. Do not spend first-pass
  effort rewriting old plans unless they are part of the current public surface;
  keep them as historical records or update only if they would actively mislead
  future release work.
- `standards/scenario-testing/` intentionally discusses Vibe Remote as an
  incubating adopter; mostly keep historical wording unless the standard is
  being made public under the new brand.

`avibe-bot-backend`:
- Current public copy is centralized mostly in `lib/i18n/messages.ts`, but
  static email templates under `supabase/templates/` and
  `lib/email/templates.ts` duplicate footer/GitHub wording and need lockstep
  updates.
- `lib/cloudflare.ts` names tunnels `vibe-remote-<slug>`. This is operationally
  visible in Cloudflare and should be treated as a migration/compatibility
  choice, not a cosmetic rename.
- `docs/plans/backend-architecture.md` contains historical backend naming and
  `__Host_vibe_remote` cookie references; update only if it is still used as
  active architecture guidance.

`avibe-docs`:
- This is the broadest user-facing copy sweep. EN/ZH pages are paired and must
  stay 1:1.
- Install pages already use `https://avibe.bot/install.sh`; only GitHub audit
  links, Windows raw PowerShell URL, uninstall package name, and old home-dir
  deletion command need layer-aware updates.
- Comparison and concept pages should shift from "Vibe Remote" to "avibe" /
  "the Agent OS" without returning to thin-middleware or coding-only framing.

## 12. Execution focus after sweep

Alex clarified after the sweep that the implementation priority is primarily
`vibe-remote`. Treat the other repos as secondary unless a machine-critical
entrypoint requires them.

### Primary: `vibe-remote`

Do first:
- P1 distribution/update/install surface: `pyproject.toml`, release workflows,
  `vibe/upgrade.py`, `core/update_checker.py`, `install.sh`, `install.ps1`,
  `npm/avibe/*`, and focused tests.
- P2 home-dir migration: `config/paths.py`, `AVIBE_HOME`, `~/.avibe` default,
  old `~/.vibe_remote` adoption,
  back-symlink, conflict rules, and old-user simulation tests.
- P3 main-repo product copy: README/README_ZH, VISION/VISION_ZH, backend i18n,
  frontend i18n, UI metadata, Slack manifest, Sentry/user-agent labels where
  appropriate.

Do not spend first-pass effort rewriting historical `docs/plans/` files,
incubating standards docs, or old compatibility tests unless they block the
release path or actively mislead current implementation.

### Secondary: `avibe-bot-backend`

Only prioritize:
- `/install.sh` redirect target in `next.config.ts` at the GitHub transfer
  point.
- Canonical GitHub URL constants if they are surfaced from the console or email
  templates during the rebrand window.

Defer broader landing-page/email copy polish until the `vibe-remote` migration
path is stable.

### Secondary: `avibe-docs`

Defer the broad public docs rewrite until the main repo has a stable 3.0.0
implementation and package/install commands are final. If touched earlier,
limit it to pages that would otherwise publish wrong install, uninstall, or
GitHub repo instructions.

## 13. Execution checkpoint: PyPI prerelease reservation

2026-06-07: published `avibe-os==3.0.0a0` to PyPI as a real prerelease to
reserve the project name before the final 3.0.0 migration release.

Verification:
- PyPI project page: `https://pypi.org/project/avibe-os/3.0.0a0/`
- PyPI JSON reports package name `avibe-os`, version `3.0.0a0`, and release
  entry `3.0.0a0`.

Operational notes:
- The API token used for the local publish was pasted into the agent
  conversation and must be revoked/rotated.
- This prerelease is a reservation checkpoint, not the 3.0.0 migration release.
- Formal `avibe-os==3.0.0` should be published through the release workflow /
  Trusted Publisher after the transfer sequencing decision.
- The one-time `vibe-remote==3.0.0` shim has not been created or published yet.

## 14. Execution checkpoint: P3 main-repo brand surfaces

2026-06-07: completed the first `vibe-remote` P3 pass for active product and
runtime display surfaces.

Updated surfaces:
- README / README_ZH, VISION / VISION_ZH, SECURITY, and repo operating notes
  now describe `avibe` as the local-first Agent OS while preserving compatibility
  notes for legacy installs.
- Install scripts and the npm wrapper present the new package/install story
  (`avibe-os`, command `vibe`) while still documenting legacy uninstall paths.
- Backend and frontend i18n now use `avibe` for setup, dashboard, remote access,
  settings, Slack install, update notifications, and related user-visible
  strings. EN/ZH keys remain in lockstep.
- UI metadata, PWA manifest, web-push defaults, Slack manifest, remote-access
  pairing defaults, system prompt injection, Sentry release labels, and
  non-protocol user agents now use `avibe`.

Intentionally retained:
- `~/.vibe_remote`, `vibe_remote.log`, cookies, OpenCode
  metadata keys, release-marker compatibility, legacy `vibe-remote` package
  uninstall/shim references, and test fixtures that simulate old installs.
- Product Hunt URLs/badge identifiers until the external listing is replaced or
  updated.
- Historical docs/plans and standards references unless they are active release
  instructions.

Verification:
- `uv run ruff check` on changed Python and focused tests.
- `uv run pytest tests/test_update_checker_platforms.py tests/test_install_script.py tests/test_v2_paths.py tests/test_claude_cli_path.py tests/test_codex_agent.py tests/test_internal_server.py`
  passed: 138 tests, 1 existing pytest config warning.
- `npm test` in `npm/avibe` passed: 8 pass, 3 skipped Windows cases.
- `npm run build` in `ui/` passed with the existing large-chunk Vite warning.
- `git diff --check` passed.

## 15. Execution checkpoint: one-time PyPI shim material

2026-06-07: added the build material for the one-time `vibe-remote==3.0.0`
migration shim.

Behavior:
- The real project remains `avibe-os`.
- `packaging/vibe-remote-shim` builds a minimal `vibe-remote==3.0.0` package
  with dependency `avibe-os>=3.0.0` and the existing `vibe` console script.
- The publish workflow builds that shim only for the `v3.0.0` tag and uploads
  it alongside the real `avibe-os` artifacts. Later releases should publish only
  `avibe-os`.

Verification:
- Shim metadata is covered by `tests/test_pypi_shim.py`.
- The shim wheel build was verified locally with `python3 -m build --wheel`.

## 16. Execution checkpoint: prompt-skill copy sweep

2026-06-07: re-ran a focused sweep for old `Vibe Remote`, `vibe-remote`,
`vibe_remote`, `VIBE_REMOTE`, `cyhhao/vibe-remote`, and `~/.vibe_remote`
references on latest `master`.

Updated:
- `skills/background-watch-hook/SKILL.md` now says Avibe in Agent-facing watch
  guidance and uses `avibe-bot/avibe` in GitHub waiter examples.

Still intentionally retained:
- Main `core/system_prompt_injection.py` is already Avibe-branded and still
  points the `use-avibe` skill URL at `avibe-bot/avibe`; do not change it
  to an `avibe.bot` redirect before the 3.0.0 release.
- `skills/use-avibe` is the current skill slug/path; stale skill references
  should not remain in new prompt injection.
- `~/.vibe_remote`, `vibe_remote.log`, cookie names,
  OpenCode metadata keys, legacy release markers, and `vibe-remote` shim /
  uninstall references remain compatibility surfaces.
- Product Hunt badge URLs and historical docs/plans remain old-name references
  until the external listing or archival docs are explicitly updated.
