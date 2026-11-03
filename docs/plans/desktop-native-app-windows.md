# Desktop native app windows

Owner approved a PR on November 4, 2026 to make ordinary launches from the desktop Dock, App Library, and Command-K app search open standalone app pages in real Tauri windows.

## Contract

- Only `files`, `terminal`, `editor`, and Show Pages with a validated session id use the native window bridge for ordinary activation and Alt/Shift new-window launches.
- Existing browser-tab launches, parameterized internal `openApp` calls, persisted AppWindow state, library/preview, web/PWA surfaces, and older shells keep their current AppWindow behavior.
- A failed bridge call falls back to `WindowManager.openApp`.
- Rust accepts structured targets only, builds the URL from the active validated loopback origin, confines navigation to that origin, and grants the command only to the main Workbench window.
- Existing target windows are focused for activation; Alt/Shift creates another native window.
- App windows use standard system decorations and are excluded from window-state persistence. They are closed when the active Runtime origin changes or the shell exits.

## Validation

Run focused Rust and UI tests, the UI build, formatting, and the repository's required checks. Use an isolated preview bundle and port for native evidence; do not touch the installed Avibe app, the production development port, or the local `vibe` service. Record screenshots for Files, a Show Page, activation focus, reopen after close, and Alt/Shift multi-window behavior, then remove preview artifacts.

## Implementation decisions

- Command/Control-click keeps the existing new-context path, which the shell forwards to the system browser.
- Child windows have standard decorations and HTML5 file drops. They receive neither shell capability markers nor overlay insets or drag-region scripts.
- Closing the main macOS window hides it under the existing lifecycle policy; child windows remain open. Explicit shell exit destroys them.
- Main and child webviews use the default persistent webview data store, with no private profile or custom store. Authentication uses the same-origin `/api/session` request and cookies; it does not depend on copying a parent's `sessionStorage`.
- Native launches are serialized to avoid duplicate labels from rapid activation. A completed build rechecks its origin so a concurrent Runtime transition cannot leave a stale child alive.
- Rust adds `ephemeral=1` to native Terminal URLs. The standalone page reuses the internal window's slot-backed terminal lifecycle: each native window has independent shells, with the same best-effort session deletion on unload/close. Ordinary browser terminal tabs keep persistent reattachment.
- Dock running indicators remain owned by existing internal windows. Parameterized launches (including chat visualization and editor paths) remain internal.

## Evidence

The UI production build, focused app-launch/bridge unit tests, Rust target tests, shell boundary tests, UI test typechecking, and the repository lint baseline gate passed locally. Browser interaction coverage exercises Dock, Library, and search against native success, old shells, Web, rejected bridge calls, and Alt/Shift Show Page launches. A shared-storage terminal regression checks distinct native session IDs, disposal, and ordinary browser reattachment.

Lifecycle selection has a pure-function test proving that cleanup selects every App window while excluding main and pet. Actual destruction on origin changes, clearing, and exit is part of isolated native acceptance; source-text matching is not used to claim that behavior.

Native acceptance is pending an unlocked console. An isolated preview uses bundle identifier `bot.avibe.desktop.nativewindows.acceptance` and port `15177`. On November 4, 2026, the preview reached the local Workbench, but macOS reported `CGSSessionScreenIsLocked=Yes`; blank captures and unverified focus are not acceptance evidence. A durable unlock Watch resumes the remaining native checks and cleanup.
