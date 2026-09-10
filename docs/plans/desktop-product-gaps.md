# Desktop Product Gaps

## Status

- Owner: Avibe core
- Date: 2026-09-10
- Baseline: `desktop` branch @ `b2ed55f11` (post master-sync 2026-08-11)
- Scope: what the desktop product still lacks to read as a first-class desktop
  application, not a windowed browser. The shell engineering base (loopback
  adoption, capability boundary, private Runtime, lazy backends) is sound and
  is **not** re-litigated here.
- Companion docs: `tauri-desktop-vertical-slice.md` (original slice),
  `desktop-backend-lazy-install.md` (frozen backend-install contract),
  `desktop-notifications-sse.md` (G4), `desktop-deep-links.md` (G5),
  `desktop-window-state.md` (G6)

## Baseline: what already exists

A Tauri v2 thin shell (window + bootstrap screen) that adopts or starts one
local Avibe Runtime, hands the window to the Workbench, and verifies integrity
of the installed private Runtime on every launch. Self-contained packages
embed CPython, the Avibe wheel, locked Python dependencies, Node, and npm.
CI produces unsigned acceptance artifacts for macOS arm/x64 and Windows x64.

What the baseline deliberately does **not** have: any distribution signing,
auto-update, tray, notifications, deep links, multi-window, or persisted
window state. That is the gap list below.

## P0 — distribution and trust (the release gate)

### G1. Code signing and notarization

- Problem: unsigned artifacts are stopped by Gatekeeper (macOS) and flagged by
  SmartScreen (Windows). There is currently no way to ship to a real user.
- Work: introduce a `desktop-package-sign.yml`-style manual workflow or extend
  `desktop-package.yml`: macOS Developer ID signing + notarytool notarization +
  stapling; Windows EV/OV certificate signing of the NSIS installer. Secrets
  via GitHub Environments; artifacts published as Draft releases only.
- Acceptance: a DMG and an NSIS installer, both signed, open on a clean
  machine with no security warning and no right-click override.
- Estimate: M. The Rust/Tauri side is configuration; the cost is certificate
  procurement and Apple/Windows account plumbing.
- Risk: certificate processes (Apple Developer ID, EV certs) have lead time;
  start procurement first.

### G2. Auto-update

- Problem: today an update means "download and replace the app". No security
  fix can reach an installed base.
- Work: integrate `tauri-plugin-updater` against a JSON endpoint (GitHub
  Releases draft flow or a dedicated channel). Decide the signing story for
  update artifacts (updater signature key, independent of G1 codesigning).
  Respect the version identity already stamped by `desktop-package.yml` input.
  The private Runtime install path is already content-addressed and versioned,
  so app replacement composes with it by design.
- Acceptance: an installed app detects a newer published artifact, downloads,
  verifies, and applies it; user state (`~/.avibe`) and private Runtime slots
  survive; rollback path is the previous app bundle reinstalling its payload.
- Estimate: M. Endpoint + key management + first signed release is the bulk.
- Depends: G1 (signed artifacts worth updating to).

### G3. Tray / Runtime lifecycle visibility

- Problem: the Runtime outlives the window by design, but the user has no
  visible handle on that. A closed window with a running process reads as a
  bug or a privacy concern; Activity Monitor is the only exit.
- Work: menu-bar tray (macOS) / notification-area icon (Windows) showing
  Runtime state (adopted/private, version, port), a "quit Runtime" action with
  confirmation, and an explicit quit semantics decision: quit-app vs
  quit-runtime, surfaced both in the tray and the first application menu.
  Align with the existing Uninstall flow, which already stops a private
  Runtime it owns.
- Acceptance: with the window closed, the tray shows live Runtime state; the
  user can stop the Runtime from the tray without opening a terminal; quitting
  the app never silently kills an adopted Runtime.
- Estimate: M. New tray plugin + state bridge; the Runtime already exposes
  `/ready` and status commands to poll.
- Note: this is the visible half of a decision the architecture already made;
  it converts a silent behavior into a product statement.

## P1 — system integration (the native feel)

### G4. Desktop notifications

- Problem: task completion, approval requests, and errors are the core
  feedback loop of an agent OS and are invisible when the window is not
  focused (which is most of the time for long-running work).
- Work: frozen in `desktop-notifications-sse.md`. The shell owns an SSE
  connection to the existing `GET /api/events` (not a new route, not polling
  `/ready`); filters an allow-list (`vaults.updated` pending →
  `approval.requested`; terminal `runs.updated` that ran ≥30s or has
  `run_type` in `{task, watch}` → `run.terminal`); fires
  `tauri-plugin-notification`. Connection survives window-close-to-tray.
  Default on, tray toggle, OS DND untouched. Click focuses the window (deep
  links are G5).
- Acceptance: with the window unfocused or hidden to tray, a pending Vault
  request and a long/background run completion each produce one native
  notification; duplicates of the same `request_id`/`run_id` do not; a
  focused visible window produces none; toggling the tray item off produces
  none. No new IPC / no remote capability.
- Estimate: M. Event source exists; the work is the shell-owned stream,
  filter/dedup, reconnect, and the plugin/permission prompt.
- Depends: tray (#1975, merged). Does not depend on G1/G2.

### G5. Deep links (`avibe://`)

- Problem: no path back into a specific session, Show Page, or approval from
  outside the app (IM messages, emails, notifications). The Workbench is only
  reachable as a single entry URL.
- Work: frozen in `desktop-deep-links.md`. Scheme `avibe`, four kinds
  (`session`, `show`, `settings`, `vaults/request`) mapping onto existing
  Workbench routes. Hot path extends the existing single-instance callback;
  cold path stashes the target until bootstrap navigates. Delivery is
  same-origin in-window navigation — no new IPC, no capability widening.
  Malformed links are dropped and never quoted into the WebView. IM/https
  producers are not switched in v1.
- Acceptance: `avibe://session/<id>` focuses the running app and opens that
  chat; a cold start finishes bootstrap then lands on it; unknown or
  malformed URLs do nothing visible in the Workbench. Click-on-notification
  locating is a G4 follow-up, not this slice.
- Estimate: M. Parser + stash/apply + scheme registration; security review
  of untrusted argv.
- Independent of G1/G6. G8 may later reinterpret `show` as a torn-out
  window; the grammar stays.

### G6. Window state persistence

- Problem: the window always opens 1200×800 centered. Every desktop user
  notices within a week.
- Work: frozen in `desktop-window-state.md`. `tauri-plugin-window-state`
  on the native frame (`x/y/width/height/maximized` per window label).
  Full-screen/Spaces are not restored. Off-screen frames clamp so the
  title bar is visible and size ≥ 880×600. Bootstrap and Runtime
  navigation must not reset the restored frame.
- Acceptance: move and resize, quit, relaunch → same frame; unplug the
  display the window was on → window still visible, not smaller than
  minimum. Hide-to-tray then Open restores the last shown frame.
- Estimate: S.
- No dependencies. Do this before G8 so new windows inherit the store.

### G7. Launch at login (optional, off by default)

- **Shipped** in #1975 (tray checkable "Start at Login", default off).
  Remaining work is real-OS verification on a signed build, not a new
  feature.

## P2 — polish and reach

### G8. Multi-window — deferred

- **Decision (2026-09-10):** do not implement native multi-window. Keep the
  current single-WebView shell wrapping the Workbench SPA. Session switching,
  Show Pages, and Settings stay in-SPA.
- Why: every frozen v1 contract (tray, start-receipt, G4 SSE, G5 deep
  links, G6 window state) already holds at N=1. Native multi-window is not
  "browser tabs"; it requires a singleton-Runtime + multi-WebView
  lifecycle (only the main window bootstraps; extra windows are remote
  from frame one and must never gain bootstrap IPC; closing them must not
  stop the Runtime). That lifecycle is unverifiable without a second
  window, so it ships in the same slice as the first extra window — not
  ahead of it.
- Trigger to reopen: a demonstrated need to pin a Show Page on another
  display. First slice then is "settings window + torn-out Show Page",
  not per-session windows. Until that trigger, Workbench split-pane (web
  + desktop) is the cheaper way to sit two sessions side by side.
- Estimate when reopened: M–L, lifecycle invariants + two window kinds.

### G9. macOS Universal Binary

- One DMG for both architectures instead of two (lipo the app binaries;
  runtime payloads are per-arch and can stay separate resources or be
  universal-merged).
- Estimate: S–M, mostly packaging automation. Low priority while dual-DMG
  works.

### G10. Accessibility and shortcuts

- Keyboard access to the full Workbench under WKWebView/WebView2, global
  hotkey to summon the window, standard cut/copy/paste menu wiring on macOS.
- Estimate: M, spread thin. Audit first, fix by surface.

### G11. Diagnostics surface

- `vibe doctor` and logs exist; the desktop has no "open logs / run
  diagnostics" affordance in-app. Pairs naturally with the tray menu (G3).
- Estimate: S once G3 lands.

## Explicit non-goals for this phase

- Mobile (per the original slice doc).
- Auto-updating agent backends from the app (frozen contract in
  `desktop-backend-lazy-install.md` keeps backends independent).
- Rewriting Workbench behavior for desktop: everything above is shell-layer
  and composes with the replaceable-shell constraint.

## Suggested sequencing

1. G1 signing (start certificate procurement immediately — longest lead time)
2. G3 tray + lifecycle (product meaning of the architecture, no dependencies)
3. G2 auto-update (needs G1; ship together with the first signed release)
4. G6 + G4 + G7 (small system-integration wins, parallelizable)
5. G5 deep links, then P2 items by demand

The deliberate observation from the review: every gap is shell-layer. The
thin-shell bet held — the missing work is breadth on one boundary, not
rework under it.
