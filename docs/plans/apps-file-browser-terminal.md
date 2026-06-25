# Workbench Apps: File Browser + Terminal — Implementation Plan

Status: design locked (brainstorm complete), not yet implemented · Owner: TBD ·
Design: `design.pen` (frames TBD) · Show Page: this feature's brainstorm page

## 1. Background

Avibe positions as a local-first Agent OS, but the Workbench has no first-class
"OS utility" surface: you can't browse/edit arbitrary files on the machine, and
you can't get a real shell. The agent already has full machine access; these apps
expose that capability, to the authenticated owner, as direct human tools.

Framing: not two one-off features, but the first two entries of an **"Apps layer"**
in the Workbench. Show Pages (already per-session mini React/Vite apps at
`/show/<id>`) become a third, *pinnable* app over time. This framing is what makes
the sidebar entry an extensible launcher rather than two ad-hoc nav items.

## 2. Goal

- A Finder-like **File Browser** app: browse the whole machine, view files (reusing
  the existing preview stack), edit + save text/code files, and do basic file ops.
- A **Terminal** app: a real shell in the browser with parity to the local terminal,
  persistent across disconnects.
- An **Apps** launcher in the sidebar that can host these and future apps.
- Ship in two phases: **Phase 1** = Apps shell + File Browser; **Phase 2** = Terminal.

## 3. Locked decisions

1. **Entry**: a separate **"Apps" group** at the sidebar bottom-left, Windows-Start-menu
   hover-expand list (reuse `InboxHoverPopover` hover-card + the `⌘K` `Command` palette);
   on mobile it lives in the "更多" sheet. The bottom status pill **collapses to just the
   green dot + a hover tooltip** for run state (drop the "运行中"/"服务状态" text lines);
   version badge may tuck into the menu. Do **not** put Files/Terminal in the Capabilities
   group (Agents/Skills/Harness/Vaults = "configure the agent"; Files/Terminal = "OS tools").
2. **File Browser scope**: whole-machine Finder, **read + write, no project scoping**
   (project dirs are themselves human-added, so scoping is an artificial boundary; the
   agent can already write anywhere). UX guard only: confirm on delete/overwrite — not a
   scope limit.
3. **Favorites rail**: a **"项目"** group (the user's pinned sidebar projects → their
   workdir roots, reuse existing project data) + a **"系统"** group (OS defaults: Home,
   Desktop, Documents, Downloads, …).
4. **Editor**: **CodeMirror 6** (over Monaco — mobile support + bundle size + fits the
   existing Shiki read-only path).
5. **Terminal stack**: `xterm.js` (+ fit/webgl/attach addons) + FastAPI **WebSocket** +
   stdlib `os.openpty` (reuse the PTY pattern in `core/agent_auth_service.py`); no new
   process/dep for the WS itself. **tmux-backed persistent sessions** running tmux's OWN
   server + OWN socket (e.g. `avibe`), isolated from the user's hand-run tmux. **Graceful
   fallback** to ephemeral PTY if tmux is absent.
6. **tmux as a managed dependency**: self-built **static binary** in CI for 4 targets
   (linux x64/arm64, mac x64/arm64), Avibe-hosted, version-managed via the show-runtime
   model. `required: false` (optional enhancement). See §7.
7. **Mobile terminal**: option **B** — an **accessory key bar** (Esc/Tab/Ctrl/arrows/
   Ctrl-C/pipe; Ctrl as a sticky modifier) + reuse our iOS `visualViewport` keyboard
   handling + lean on tmux for reconnect. File Browser on mobile is largely free (reuse
   responsive components + CM6 is mobile-good).
8. **MVP order**: **File Browser first** (Phase 1, with the Apps shell); Terminal second
   (Phase 2, with the tmux dependency pipeline).

## 4. Verified current state (file:line — from this session's exploration; re-verify at impl)

File handling:
- `GET /api/media/{token}` serve, hardened (symlink TOCTOU re-resolve, safe MIME, nosniff,
  `private` cache) — `vibe/ui_server.py` ~4762; `/meta` ~4820; upload
  `POST /api/sessions/{id}/attachments` (base64 JSON, 25MB cap) ~4845.
- Directory listing `POST /api/browse` → `vibe/api.py:browse_directory` ~542 returns
  **dirs only** (`follow_symlinks=False`); `GET /api/browse/favorites` (api.py ~574,
  OS-specific quick-access); `POST /api/browse/mkdir` ~4151.
- `media_objects` (`storage/models.py` ~403): token, canonical `local_path`,
  file_name/content_type/file_ext, size_bytes/mtime_ns, width_px/height_px; dedup index on
  (local_path,size,mtime). `storage/media_service.py` registration + `imagesize` probe.
- Frontend: `DirectoryBrowser`, `FileViewer`/`FileViewerModal`, `ImageViewer`, `FileCard`
  in `ui/src/components/ui/file-*.tsx`; preview decision `ui/src/lib/filePreview.ts:previewKind`
  (markdown/json/csv/code/source/text via Shiki + json-view + papaparse); proxy guard
  `isProxyMediaUrl`; dims `ui/src/lib/mediaProxy.ts`. Upload flow in `Composer.tsx`.

Terminal / server:
- FastAPI app `vibe/ui_server.py`. WebSocket precedent: `/ws/echo` ~1838 + Show HMR proxies
  ~1863/1888 with `_show_runtime_websocket_authorized` ~1914. Chat streaming is via an
  internal unix-socket dispatch server (not SSE in ui_server).
- PTY precedent: `core/agent_auth_service.py` `os.openpty()` ~1155 + `asyncio.create_subprocess_exec`
  ~1166; non-blocking read helper `_read_pty_output` (ui_server ~1226) with ANSI/control sanitize.
- Auth: before-request `enforce_remote_access_cookie` ~1588; local check
  `_is_local_request` / `_websocket_is_local_request` (rejects `X-Forwarded-*`); remote =
  `__Host-vibe_remote_session` OIDC cookie (`vibe/remote_access.py:parse_session_cookie` ~783);
  CSRF `protect_mutating_ui_requests` ~1630. UI server is a SEPARATE uvicorn process; shares
  SQLite/config; logs to `runtime/ui_stderr.log`. New WS/HTTP routes inherit this auth.

Dependencies system (NOT pluggable — hardcoded, but askill is the exact precedent):
- `_ALLOWED_DEPENDENCIES` (`vibe/ui_server.py:3478`), `_ALLOWED_DEP_INSTALLS` (`vibe/api.py:3374`).
- `dependencies_status()` (api.py ~3380) → items `{id, kind: tool|runtime|node, required,
  installed, version, status}`; askill detect via `resolve_cli_path` + `--version` (~3259),
  install via `askill.sh` (~3199); node = detect-only.
- `start_dependency_install_job()` (~3573, if/else per dep) + endpoints
  `GET /api/dependencies`, `POST /api/dependencies/<dep>/install`,
  `GET /api/dependencies/<dep>/install/<job_id>` (ui_server ~3481+).
- UI: `ui/src/components/settings/SettingsDependenciesPage.tsx` (`DEP_META` ~24, install
  buttons ~99); type `ui/src/context/ApiContext.tsx:DependencyItem` ~854; i18n `dependencies.items`
  in `ui/src/i18n/{en,zh}.json`.
- Platform tag `core/show_runtime.py:_runtime_platform_tag` ~1170 (`darwin-arm64`/`linux-x64`…);
  show-runtime download+checksum+versioned-dir model is the template for the tmux vendor.
- Base images: `Dockerfile` ~55 apt-get; `scripts/incus_regression.py` ~1260 apt-get (bake tmux here).

## 5. Design — File Browser

Layout: left = directory tree (extend `DirectoryBrowser` to also list files); right = file
content (read-only → `FileViewer`; editable → CodeMirror 6). Save via a new write API.

New backend APIs (native async FastAPI in `ui_server.py`; path-safety in a shared helper):
- `GET /api/files/list?path=` → entries with `{name, kind: dir|file|symlink, size, mtime, ext}`
  (or extend `/api/browse` to include files; prefer a dedicated `fs` surface to keep `browse`
  picker semantics intact). `show_hidden` flag.
- `GET /api/files/read?path=` → bytes/text for an arbitrary path (today only token-based media
  read exists). Reuse the hardened serve logic (canonicalize, regular-file check, size cap,
  safe content-type, `nosniff`). Cap inline preview like the media route.
- `PUT /api/files/write` → `{path, content}` save. Atomic write (temp + rename), size cap,
  optional mtime precondition for conflict detection.
- `POST /api/files/mkdir|rename|move|delete` → file ops; delete/overwrite require explicit
  confirm on the client and are audit-logged.
- Path-safety helper: canonicalize (`Path.resolve`), reject when the resolved target is not a
  regular file/dir as expected (TOCTOU), normalize `~`. **No allow-root restriction** (scope is
  whole-machine by decision), but all writes/deletes go through one choke-point that logs.

Frontend:
- Extend `DirectoryBrowser` to render files alongside dirs (icon by ext, size/mtime).
- Reuse `FileViewer`/`FileViewerModal` for read-only preview; add an **edit mode** that mounts
  CodeMirror 6 for editable text/code (lazy-loaded like the preview stack). Save button →
  `/api/files/write`; dirty-state + conflict handling.
- Favorites rail: "项目" group from pinned sidebar projects (their workdir roots) + "系统"
  defaults (reuse `browse_favorites`). Reuse existing project data; no new persistence.
- New page `ui/src/components/workbench/FileBrowserApp.tsx`, route `/apps/files` (see §10 shell).

## 6. Design — Terminal

- WS endpoint `GET /api/terminal/<session>` (native FastAPI websocket; inherits
  `enforce_remote_access_cookie`). Protocol: text/binary frames for I/O; a small JSON control
  channel for resize `{type:"resize", cols, rows}` → `TIOCSWINSZ` on the master fd.
- Backend: allocate `os.openpty()`, `asyncio.create_subprocess_exec` the launch command, pump
  master↔websocket (reuse the `agent_auth_service` pattern + `_read_pty_output` style reader on
  the event loop / threadpool — no per-request `asyncio.run`).
- **tmux persistence**: launch `tmux -L avibe new-session -A -s <id>` (own socket `-L avibe`,
  attach-or-create) so the shell lives in tmux and survives WS drops; reconnect re-attaches.
  Run with a minimal tmux config (status bar off, sane keys) so it reads as a clean terminal,
  not "a tmux". If tmux missing → spawn `$SHELL -l` directly (ephemeral). tmux is `required:false`.
- Spawn env: set an explicit UTF-8 locale (`LANG`/`LC_CTYPE`) — do NOT inherit the daemon's
  likely C/POSIX locale; set `TERM=screen-256color` (or ship terminfo) for inner programs.
- Lifecycle/reaper: idle timeout, max sessions per user, cleanup of dead servers/sockets.
- Frontend `TerminalApp.tsx` + `xterm.js` (`@xterm/xterm`, addons fit/webgl/attach), route
  `/apps/terminal`.

## 7. tmux as a managed dependency

Mechanism (mirrors show-runtime):
- **Self-build** a static tmux in CI for 4 targets (linux x64/arm64, mac x64/arm64), built
  **with `utf8proc`** (mac CJK double-width correctness — upstream prebuilts don't guarantee it)
  and terminfo handled. Host as Avibe release assets + a manifest pinning version + per-platform
  asset + sha256. (May start by mirroring upstream `tmux/tmux-builds` static binaries to validate
  fast, but the target is self-build for control over utf8proc/terminfo/signing/version.)
- **Install**: download the pinned asset for `_runtime_platform_tag`, verify sha256, unpack into
  a versioned immutable dir under `~/.avibe/runtime/tmux/versions/<ver>/`.
- **macOS signing (verified this session on arm64)**: an unsigned binary is SIGKILLed (exit 137,
  "code object is not signed at all"); `codesign -s -` (ad-hoc, free, no Apple cert, no
  notarization) makes it run. Programmatic `curl`/urllib download does NOT set the
  `com.apple.quarantine` xattr, so Gatekeeper does not prompt for CLI exec. → On download:
  verify signature; if not validly signed, ad-hoc sign (`codesign -f -s -`); strip quarantine if
  present.
- **Always use the vendored binary** (ignore system tmux): deterministic version (test once),
  sidesteps tmux client/server protocol skew, we control utf8proc/terminfo/signing. Binary is
  tiny (~0.6–0.9 MB).
- **Hosted/Docker/Incus**: also bake tmux into base images (one apt line) — simplest there.
- **Registration** (low-friction, follows askill): add `"tmux"` to `_ALLOWED_DEPENDENCIES` /
  `_ALLOWED_DEP_INSTALLS`; add `tmux_status()` (detect `tmux -V`, min-version check, NOT exact pin)
  + `ensure_tmux_installed(force)` (download/verify/sign); append to `dependencies_status()`
  (`kind="tool"`, `required=false`); route in `start_dependency_install_job()`; add `DEP_META`
  + i18n + Settings·Dependencies card.

## 8. Mobile

- Terminal = option B: accessory key bar above the keyboard (Esc/Tab/Ctrl/arrows/Ctrl-C/`|`;
  Ctrl/Alt as sticky modifiers); reuse the iOS `visualViewport` keyboard handling (see
  `mobile-responsive-webui.md` / iOS keyboard fix) so the terminal sizes above the keyboard;
  tmux covers reconnect on network/background drops. C (gestures, customizable keys) is post-v1.
- File Browser = largely free: responsive layout + CodeMirror 6 is mobile-good. Apps launcher on
  mobile lives in the "更多" sheet.

## 9. Security

- Does NOT expand the trust boundary: the agent already has machine access, and the Workbench is
  OIDC + Cloudflare-tunnel gated (`enforce_remote_access_cookie`). These expose an existing
  capability to the authenticated owner.
- But a terminal raises the blast radius of any auth bug to "remote shell". Hardening:
  - opt-in / feature flag;
  - **never reachable via public `/p/` share links** — Workbench + private `/show` only;
  - WebSocket `Origin` check (codebase already rejects `X-Forwarded-*`);
  - audit log of file writes/deletes and terminal commands;
  - idle timeout on terminal sessions.

## 10. Implementation plan

**Phase 1 — Apps shell + File Browser**
- Apps shell: sidebar "Apps" group + hover/`⌘K` launcher (reuse `InboxHoverPopover` + `Command`);
  collapse the status pill to the green dot + tooltip; routes `/apps/*`; mobile entry in "更多".
- Backend: `fs` APIs (list with files, read-by-path, write, mkdir/rename/move/delete) + shared
  path-safety choke-point + audit; confirm-on-destructive on the client.
- Frontend: extend `DirectoryBrowser` (files); `FileBrowserApp`; CodeMirror 6 edit mode; favorites
  "项目" + "系统". i18n keys; reuse `ui/src/components/ui/` primitives.
- Tests: path-safety unit tests (canonicalize, regular-file, atomic write); UI `npm run build`.

**Phase 2 — Terminal**
- tmux dependency pipeline (§7): CI static builds (4 targets) + manifest + version mgmt +
  download/verify/sign; register in the Dependencies system + Settings card; bake into base images.
- Terminal backend: `/api/terminal/<session>` WS + PTY + tmux own-socket persistence + resize +
  lifecycle/reaper + UTF-8 locale + `TERM`.
- Frontend: `TerminalApp` + `xterm.js` + addons; mobile accessory key bar + iOS viewport reuse.
- Security: flag, never-on-`/p/`, Origin check, audit, idle timeout.
- Regression: verify terminal across platforms in the Incus environment.

## 11. Testing & validation

- Backend: focused pytest for path-safety + fs APIs (hermetic, redirect to test-owned dirs;
  never touch real `$HOME`/state); min-version + sign/locale handling for the tmux vendor.
- UI: `cd ui && npm run build`. Native async FastAPI for new routes (no `asyncio.run` bridges,
  not `ui_compat`). `ruff check` on changed Python before push.
- Cross-platform/user-facing: Incus regression for the terminal especially.

## 12. Related plans

- `dependencies-settings.md` — the Dependencies tab + askill precedent (extend for tmux).
- show-runtime vendor/version model — template for the tmux managed binary.
- `file-preview-component.md` — the `FileViewer` stack being reused/extended.
- `mobile-responsive-webui.md` + iOS keyboard fix — reused for the mobile terminal.
- `agent-run-terminal-lifecycle.md` — DISTINCT: that is CLI `vibe agent run` session lifecycle,
  not this Web terminal app.
