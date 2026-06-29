# Apps Layer v2 — Windowing Redesign

## Background

The Apps layer v1 (File Browser + Terminal, PR #659) shipped as **full-page** routes
with an invented sidebar treatment. On review the owner asked for a major UX/UI rework:
apps should behave like a real OS — open in **draggable, resizable, multi-instance
windows** managed by a **Dock**. Design was done and **LOCKED** in `design.pen`
(canonical frame: `Apps · Dock on REAL Workbench`, id `NbPMq`).

Hard rule learned during design: **base everything on the REAL current layout**
(`ui/src/components/AppShell.tsx`, `workbench/WorkbenchSidebar.tsx`, `workbench/ChatPage.tsx`,
`workbench/Composer.tsx`) — not an imagined one.

## Goal

Turn Files / Terminal / Editor into windowed OS-style apps with a unified Dock, and
upgrade the editor to the Monaco (VS Code) kernel, without regressing the existing
backend (`/api/files/*`, terminal WS) shipped in #659.

## Locked design

1. **Windowing.** A reusable `<AppWindow>` (Mac chrome: traffic lights close/min/max,
   centered title, rounded + drop-shadow) that is **draggable (titlebar), resizable
   (edges/corner), maximizable, focus-to-raise (z-order), minimizable**. A
   `WindowManager` context owns the open-window list, z-order, focus, min/restore,
   and per-app multi-instance. Windows render in a portal layer above the workbench
   main area (not over the sidebar). Bounds clamp to the viewport.
2. **Dock = unified launcher + window manager.** One surface = pinned app list +
   running indicators (dot under icon) + minimized-window thumbnails (right of a
   divider, click to restore). Anchored **bottom-LEFT**, **rises ABOVE the bottom-left
   "Apps" button** (clear of the centered Chat composer). Interaction:
   **hover Apps = transient preview float; click Apps = pin/unpin toggle.** Reuse the
   existing hover-popover pattern (`InboxHoverPopover`) for the hover/close-delay.
3. **Sidebar bottom** (in `AppShell.tsx`, real layout): replace the single
   `[AppsLauncher][dot][VersionBadge]` row with **row 1 = [Apps (LEFT) | 设置 (right)]**
   two buttons, **row 2 = [VersionBadge … green run-dot]**. Apps stays left (Dock anchor);
   the old `AppsLauncher` popover is superseded by the Dock. Keep the "Open Control Panel"
   mode-switch link below. 设置 button = the control-panel / settings entry.
4. **Terminal app** (port `AppsTerminalPage`/`TerminalView` into a window body): full-width
   xterm (v1 squished-strip bug is gone once it fills the window), session tabs,
   `tmux·persistent` badge, accessory key bar. Backend unchanged.
5. **File Browser app** (port `AppsFileBrowserPage`): Finder-like list (type icon + size +
   modified + selection), favorites/projects rail, toolbar **New File + New Folder +
   breadcrumbs + search + status bar**. (New File already added backend-side? verify;
   v1 had New Folder only.)
6. **Editor = Monaco (VS Code kernel).** Replaces the CodeMirror look in `FileEditorPane`.
   - **Lazy-load**: `React.lazy(() => import(...Monaco...))` so Monaco is NOT in the main
     bundle — it loads only when an Editor window opens. Use `vite-plugin-monaco-editor`
     to trim language workers to what we support. Self-host (Avibe serves UI from the
     user's machine — local transfer, no CDN).
   - **Caching**: content-hash immutable chunks (Vite default) + a **Service Worker
     precache** (Workbox `injectManifest`, `revision:null`, cache-first) that warms
     Monaco in the background after the workbench loads → instant + offline subsequent
     opens. Optional: prefetch on hover of the Dock's Editor icon. `cleanupOutdatedCaches`
     on version bump.
   - **Mobile**: Monaco has no official touch support. Add a mobile UI layer = **reuse the
     Terminal accessory key bar** (symbol keys `(){}[];` Tab Esc) + explicit Copy-all /
     Select-all buttons + a touch-selection shim. Gate the heavy editor / show a lighter
     path on very small screens if needed.

## Implementation phases

- **P1 — Windowing foundation.** `WindowManager` context + `<AppWindow>` (drag/resize/
  maximize/focus/minimize) + window portal layer. Headless of any specific app (a demo
  body). Unit/interaction sanity.
- **P2 — Dock.** Dock component (launcher + running + minimized thumbnails), anchored
  bottom-left, hover-preview + click-pin, wired to WindowManager. Replace the sidebar
  `AppsLauncher` trigger; keep `AppsLauncher` removal behind the Dock.
- **P3 — Port apps into windows.** Files + Terminal bodies moved into `<AppWindow>`
  (route → window, or keep routes as deep-links that open a window). Multi-instance.
- **P4 — Monaco editor.** Lazy-loaded Monaco in the editor window + VS Code theme; SW
  precache; mobile accessory layer. Swap `FileEditorPane`'s CodeMirror.
- **P5 — Sidebar bottom + mobile.** `[Apps|设置]` + `[ver·dot]` rows in `AppShell`; mobile
  Dock-as-bottom-bar + full-screen single app.
- **P6 — Polish.** Minimize→Dock animation, multi-window expose, keyboard (⌘`/⌘W),
  empty/restore states, i18n (en/zh), `npm run build` green.

## Evidence layers

UI build (`npm run build`) per phase; reuse #659 backend tests (file browser / terminal)
unchanged; manual workbench sanity in local Incus regression; visual diff vs design.pen
`NbPMq` / app-window frames.

## Notes

- ~95% frontend (`ui/src/**`) → lead-owned per the standing split; backend reuses #659.
- design.pen frames: `X7d3Ev` AppWindow, `iwYIX` Terminal, `nknn2` Files, `dnYPx` Editor,
  `RFMRw` Dock states, `NbPMq` Dock-on-real-workbench (canonical). Exports in
  `/tmp/avibe-apps-design/`.
