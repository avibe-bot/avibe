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
- Rust injects the top-level identity-only `__AVIBE_NATIVE_APP_WINDOW__` marker into every child document. Terminal reuses the internal window's slot-backed lifecycle, including best-effort session deletion on unload/close. Ordinary browser terminal tabs keep persistent reattachment. Identity is not carried in a query parameter.
- Dock running indicators remain owned by existing internal windows. Parameterized launches (including chat visualization and editor paths) remain internal.

## Evidence

The UI production build, focused app-launch/bridge unit tests, Rust target tests, shell boundary tests, UI test typechecking, and the repository lint baseline gate passed locally. Browser interaction coverage exercises Dock, Library, and search against native success, old shells, Web, rejected bridge calls, and Alt/Shift Show Page launches. A shared-storage terminal regression checks distinct native session IDs, disposal, and ordinary browser reattachment.

Lifecycle selection has a pure-function test proving that cleanup selects every App window while excluding main and pet. Actual destruction on origin changes, clearing, and exit is part of isolated native acceptance; source-text matching is not used to claim that behavior.

Native acceptance is pending an unlocked console. An isolated preview uses bundle identifier `bot.avibe.desktop.nativewindows.acceptance` and port `15177`. On November 4, 2026, the preview reached the local Workbench, but macOS reported `CGSSessionScreenIsLocked=Yes`; blank captures and unverified focus are not acceptance evidence. A durable unlock Watch resumes the remaining native checks and cleanup.

Native marker acceptance must show that the main Workbench successfully opens a native App window, then assert `__AVIBE_NATIVE_APP_WINDOW__ === true` in the child while `__AVIBE_DESKTOP_SHELL__` and `__AVIBE_DESKTOP_APP_WINDOWS__` are both `undefined`. In a narrowed Files window, click Open Terminal Here and verify cwd and an independent session, then reload and verify isolation again while an ordinary browser terminal retains its persistent session.

## Review diagnosis and scope decision

Two reviewed heads exposed the same test-fidelity defect: newly added source assertions depended on Rust implementation identifiers. The first repair removed lifecycle matching but missed the marker assertions. The orchestrator independently verified the complete three-thread inventory and consumer tests on November 4, 2026, then authorized a test-only correction: remove identifier-based builder/constant/negative scans, retain the shared JS-key literal contract, and resolve every capability through Tauri's ACL machinery to verify that no App-window or webview label receives IPC on macOS or Windows. Actual injection and destruction remain native acceptance checks. Production behavior is unchanged; no JS engine or builder abstraction is added for tests.

### Window identity diagnosis and entry inventory

The next review exposed session ownership again: Files-to-Terminal navigation lost the query parameter used by the first repair. Across three findings-bearing heads there were four threads; session ownership occurred on `6f61576` and `5e16a118`. The orchestrator independently verified the inventory and navigation consumer on November 4, 2026 and approved replacing the URL protocol with a webview identity marker. If the next review repeats window identity or session ownership, stop and return to the orchestrator before another patch.

Inventory completed before the repair:

| Entry | Session ownership and cwd |
| --- | --- |
| Direct native Terminal | The child marker selects a fresh slot-backed session; default cwd. |
| Alt/Shift additional Terminal | Each document has its own terminal token and slot; no shared shell. |
| Narrow Files: Open Terminal Here | Router state carries cwd; native route passes it as `params.cwd` to the existing windowed lifecycle. No filesystem path enters the page URL. |
| Wide Files: Open Terminal Here | Existing parameterized internal AppWindow with `windowed`, window id, and cwd params; unchanged. |
| Reload | Tauri reinjects identity before page scripts; a fresh independent shell replaces the old disposable session. Router state, where retained by browser history, supplies cwd. |
| Back/forward | The document keeps its identity; re-entering Terminal mounts a disposable session with that history entry's cwd. |
| Show Page same-origin navigation | A newly loaded document receives the same child marker, even without a standalone query. AppShell freezes standalone mode from that marker too, keeping main Workbench layout reads/writes disabled. |

Storage audit: Files owns its cwd, selection, clipboard, and listing in component memory. Editor route buffers/tabs are component state and have no window-id persistence; `editorRecents` intentionally shares recent files/folders, not live buffers or shell identity. Show Page content owns its application storage; the host adds no shared session identity. Theme, language, recent-file history, and account cookies remain shared by design. Only TerminalTabs uses localStorage as a live shell identity. Workbench layout persistence is already excluded by the frozen standalone state; using the same child marker also covers hard navigation from Show Pages without the query parameter. No extra storage namespace or per-App mechanism is needed.

The browser regression extends the existing terminal ownership test at its WebSocket boundary: actual Files context-menu navigation must attach exactly one independent session with the selected cwd, retain isolation after reload/history traversal, and leave the browser's persistent session untouched. The prior direct-launch-only regression missed navigation. Native marker injection itself remains an isolated OS acceptance check.

Local repair evidence: all 21 native-launch browser cases passed, including the new narrow/wide Files flows and history/reload checks. The narrow test failed on pre-fix source by attaching the shared first session without cwd. All 5 Rust target tests, 48 shell boundary tests, and CI-equivalent all-target/all-feature Clippy passed. The unrelated Workbench failure was checked on the CI merge's exact master parent `de57d80f`: all 17 tests passed in an isolated baseline worktree; no Workbench code or test was edited.
