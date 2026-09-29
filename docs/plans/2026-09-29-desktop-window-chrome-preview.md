# Desktop chat chrome preview

## Contract

Reuse the complete ChatHeaderBar in a macOS window-level slot: title editing,
Back, agent/model/effort selection, Visualize and its menus keep their existing
state and handlers. The web toolbar remains inline. The window row occupies the
existing chat-header height; there is no additional empty title strip above it.
Only direct hits on blank top-row regions drag or double-click maximize/restore.
Controls, popovers and the transcript retain ordinary input. Settings hides the
retained chat's portal along with its background surface.

The native window uses Overlay with a hidden title and a transparent title bar.
Window-background movement is disabled. Tauri owns drag/double-click behavior;
only start-dragging and internal-toggle-maximize are granted to the loopback
Workbench. Same-origin pages share this accepted window-control boundary;
Runtime lifecycle management stays local-only.

## Scope and delivery

The owner accepted the installed preview, including the complete chat toolbar,
sidebar consistency and equal logo insets, and requested a PR and review on
2026-09-30. Continue the existing isolated branch, integrate current master,
and require an exact-head Codex pass, zero unresolved review threads and all
expected CI checks. Merging, releasing and restarting the Runtime remain outside
this delivery authorization.

The preview began at f1e20d49f. Both the native shell and Runtime-served UI were
installed for acceptance; previous app and package artifacts remain available.
Native physical drag and double-click acceptance stays distinct from DOM/IPC
and build evidence: the owner accepted the preview's appearance, while the
recorded automated gesture evidence is limited to Tauri's browser IPC handler.

## Validation

- Render the real chat in a hermetic browser with web and native-shell markers.
- Verify one complete toolbar, control interaction, geometry and Settings retention.
- Exercise the locked Tauri drag script against the rendered page locally.
- Run affected complete UI tests, UI/bootstrap builds, Rust shell tests and lint.
- Read back installed app bytes and HTTP-served UI assets; preserve service PIDs.

## Local preview evidence

- UI production build, bootstrap build and macOS app bundle passed.
- 100 tests across the complete AppShell, desktopShell, archived-chat and
  ShowPageLaunchControl files passed; all 35 Rust shell boundary tests passed.
- UI lint baseline, browser-test TypeScript, Cargo formatting and Clippy passed.
- Both web/native-marked browser cases passed: toolbar controls, one header,
  48px chrome bounds, title editing, model popup and Settings hide/return.
- A temporary hermetic probe executed locked Tauri 2.11.5 drag.js against the
  actual chat: four top blanks dispatched start_dragging, double-click dispatched
  internal_toggle_maximize, and transcript/title input/model popup dispatched
  neither. The temporary registry-dependent probe was removed after it passed.
- Repacked the installed 3.1.2rc3 wheel with only the built Workbench assets and
  wheel metadata changed. All 390 original Python files matched; old hashed UI
  assets remain available to already-open clients. Installed with uv pip through
  the existing tool interpreter, without dependencies or an editable checkout.
- Replaced /Applications/Avibe.app and verified its executable/Info.plist against
  the bundle. HTTP index and entry assets on port 5123 match this UI build.
- Runtime PIDs 61799 and 61802 stayed running; /health remained ok.
- Reopened the app and the existing chat. Native accessibility and screenshot
  show Back, the editable title, model/effort picker and Visualize in the top row.
  Computer-use coordinate gestures did not establish reliable physical-drag or
  double-click evidence; leave that acceptance to the owner, rather than count
  the browser IPC probe or the native Zoom button as a passing double-click test.

Rollback artifacts: /tmp/Avibe.app.before-window-chrome-20260929 and the original
/tmp/avibe_os-3.1.2rc3-py3-none-any.whl. The preview wheel is retained under
.runtime/desktop-chrome-preview/ with build tag 1desktopchrome.

The evidence above records local preview validation before PR review and CI.

## Alignment follow-up

Owner acceptance requires the web layout inside the relocated toolbar: the same
1080px content maximum and responsive horizontal gutters as the transcript and
composer. The surrounding empty chrome remains a full-width drag region. Native
traffic lights use Tauri's trafficLightPosition configuration (12, 24), centering
the controls within the 48px row without custom AppKit positioning code.
Validate web and native content bounds at 880, 1200 and 2000px viewport widths,
then replace the local shell and UI preview again.

Alignment follow-up installed: both real-browser cases passed at all three widths;
UI/bootstrap/macOS builds and all 35 Rust shell boundary tests passed. The installed
app binary and HTTP-served UI entry assets matched the rebuilt artifacts. Backend
package files were preserved and Runtime PIDs stayed unchanged. The app reopened
and the previous chat was selected; final native screenshot capture was blocked
because the Mac locked. Native traffic-light visual acceptance remains unverified.
Previous preview app: /tmp/Avibe.app.before-chrome-alignment-20260929.
Current UI preview wheel: .runtime/desktop-chrome-alignment/ with build tag
2desktopchrome; original and first-preview wheels remain available for rollback.

## Sidebar consistency follow-up

The native sidebar reserves the same 48px traffic-light row and bottom divider
on the home and chat routes. Its logo and navigation must retain their positions
when switching routes. Scope the clearance to the sidebar so the home page's
content and top controls retain their existing layout. Web sidebar spacing is
unchanged. Validate real home/chat navigation in the existing hermetic browser
cases, rebuild the UI, and replace the local preview UI without restarting the
Runtime or rebuilding the unchanged native shell.

Installed sidebar follow-up evidence:

- The complete web/native browser file passed both cases, including brand-link
  navigation to home and history return to chat. Logo, Inbox and capability-nav
  bounding boxes stay identical; the native sidebar retains its 48px row and
  divider, including the top-right edge. Web retains its original 10px brand inset.
- All 53 AppShell/desktopShell tests, browser-test TypeScript, UI lint baseline,
  production build and diff whitespace checks passed.
- Built and installed the 3desktopchrome preview wheel under
  .runtime/desktop-sidebar-consistency/. All 446 original non-UI package files
  remain byte-identical. All 191 built UI files match the wheel and all 223
  previous hashed assets remain available. The served index and entry assets
  match the new production build.
- Reopened the unchanged installed desktop shell and inspected the real home
  and chat screenshots: the sidebar divider, logo and navigation align. Returned
  to home for owner acceptance. Runtime PIDs 61799 and 61802 stayed unchanged and
  /health remained ok. No Runtime restart or native-shell rebuild was needed.

## Sidebar logo inset follow-up

The native sidebar's brand mark uses the same 16px top and left content inset
after the 48px traffic-light row. The web sidebar keeps its existing spacing.
The hermetic browser geometry assertion covers this invariant. Rebuild and
install a new UI-only preview wheel, then leave the existing desktop shell open
on the home route.

Installed logo inset follow-up evidence:

- Desktop-only sidebar spacing is now 16px after the 48px native row, matching
  the existing 16px horizontal inset. Web remains at its original 10px top inset.
- The complete web/native browser file passed both cases, including the native
  logo geometry assertion; 53 focused UI tests, browser-test TypeScript, UI lint,
  production build and diff whitespace checks passed.
- Repacked and installed the 4desktopchrome UI preview wheel under
  .runtime/desktop-sidebar-logo-inset/. The 446 non-UI package files remain
  byte-identical and the 242 previous UI assets remain available. Served index
  and entry assets match the build. The desktop window was reopened on the home
  route for owner acceptance; Runtime PIDs 61799 and 61802 and /health stayed
  unchanged.

## PR preparation

Integrated master 99db88055 with a normal merge and preserved its Runtime,
updater and Vault changes. The resulting candidate passed both production UI
and bootstrap builds, 102 focused UI tests, all 12 browser cases in the chrome
and sidebar/Dock files, and all 84 desktop Rust tests (including 35 shell boundary
tests). UI lint baseline, browser-test TypeScript, Rust formatting, Clippy and
diff checks passed. The chrome browser file now runs in the existing CI job.
The accepted installed preview remains in place; these master changes have not
been installed into the owner's local Runtime.
