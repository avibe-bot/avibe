# Desktop title bar: Tauri drag regions

## Contract

Keep the macOS overlay title bar and hidden title. The existing top header's
empty space is draggable across the window; no extra horizontal bar or layout
height is added. Setup keeps its language switcher. Interactive controls and
popup surfaces retain their current actions.

Use Tauri's built-in `data-tauri-drag-region` handling and window commands.
Grant only start-dragging and internal-toggle-maximize to the main macOS window,
including its loopback Workbench. Same-origin Show Pages can reach these window
commands through their parent; this is an explicitly accepted window-control
boundary, not isolation. Bootstrap, notifications, updater and Runtime lifecycle
commands remain outside the remote grant.

Remove the custom WebKit message handler, native drag overlay and page mouse
listeners. Mark concrete blank layout containers with the direct-target drag
attribute, rather than inferring controls from global events. Retain the existing
traffic-light clearance without growing the chat header. New shells use the
standard title bar for older pages that do not declare Tauri drag support.

## Review inventory and decision

Before this revision: head 9d3d659c4 had three findings (overlay-state gating,
popup hit testing, double-click tolerance); head 1c4e3d9be had one finding
(same-origin subframes bypassing the claimed main-frame bridge boundary).
The owner now permits Workbench window IPC. Replace the custom interaction
protocol with Tauri, and document the actual same-origin boundary. Double-click
behavior follows the bundled Tauri implementation rather than a local emulation
of macOS preferences. No new dependencies, merge, release or deployment.

The previous lane's last Watch follow-up failed and no run was active. Its Watch
3658eb938494 was paused before taking over the existing clean PR worktree.

## Validation

- Exercise the bundled Tauri drag script against real rendered header/Setup
  controls and popups; ensure blank header targets invoke dragging and controls do not.
- Check the remote capability URL matching and exact permission allowlist.
- Run the relevant complete UI test files, UI and bootstrap builds, Rust tests,
  formatting and Clippy.
- Packaged macOS dragging remains a separate acceptance boundary; do not claim
  browser or unit evidence proves actual native window movement.
- Update PR #2262, read its body back and follow exact-head review/CI gates.

## Local results

- UI build and bootstrap build passed. Six affected UI test files passed
  (199 assertions total).
- `cargo test -p avibe-desktop`: 73 passed, including real Tauri URLPattern
  matching for IPv4/IPv6, alternate ports and rejected non-loopback hosts.
- Clippy with warnings denied passed.
- A temporary local probe loaded the exact locked Tauri 2.11.5 `drag.js`,
  executed it against rendered Setup and chat headers, and verified every marked
  blank invokes start-dragging, buttons and their nested icons invoke nothing,
  a still double-click invokes internal-toggle-maximize, and the real open
  LanguageSwitcher listbox/options invoke nothing. All three probe tests passed;
  the local registry-dependent probe was removed after verification.
- `vibe watch add` cannot bind this Codex desktop chat: `missing_session_policy`.
  The original lane's Watch remains paused to prevent concurrent stale edits.
  Review/CI observation in this chat uses the bundled waiter and a pre-push
  baseline; no unrelated Avibe session is created or messaged.

- Repository UI lint baseline passed with no drift. Direct ESLint reports the
  existing baselined findings; no unrelated lint changes were made.
- Hermetic Chromium probe of the real `/chat/drag-fixture` route passed. With
  the exact Tauri script injected and native invocation captured, real pointer
  clicks at x=100,258,700,1355 along the top all invoked dragging. Title editing
  remained clickable without invoking dragging. Measured header y=0, x=248,
  height under 60px at 1366x800; no extra bar. All application requests were
  intercepted by the existing fixture and the denied-request ledger was empty.
  The temporary test/server were cleaned up. Native OS movement is still deferred.

## Base synchronization

Merged origin/master fb18756aa after GitHub reported a conflict. The sole conflict
was the restored-window grab-area comment on the removed native drag strip.
Preserved master window restoration, update architecture checks, backend install
outcomes and other changes unchanged; the sidebar DOM region still covers the
same grab area the restoration algorithm protects. Revalidate the merged tree.

Merged-tree revalidation: UI/bootstrap builds, 199 UI tests, 75 desktop Rust
tests, all 8 window-frame tests and Clippy passed. The first revised head
55a600141 is under Codex review; wait for its terminal verdict before pushing
the base synchronization, so review evidence cannot be attributed across heads.
