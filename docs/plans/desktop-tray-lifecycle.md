# Desktop tray lifecycle and login item

## Contract

The native shell remains available after its main window closes. Its tray and
first application menu expose the same Runtime status and explicit lifecycle
actions. Closing a window hides it; it neither exits nor stops a Runtime.
Status is derived from the existing bootstrap state and `/ready` monitor, not
another endpoint or Webview-provided signal. Serving includes the proved
listener; transient probe failures show unreachable while the existing
three-failure recovery threshold remains unchanged.

Stop is offered only for a shell-started Runtime. Confirmation stops it without
exiting the shell. The bootstrap then shows a localized stopped notice and an
explicit Retry action; automatic recovery must not undo a user's Stop.
Quit with launch ownership offers stop-and-quit, keep-running-and-quit, or
cancel. Quit without ownership leaves the adopted Runtime alone. Startup and
uninstall serialize against lifecycle actions; no stop may race a launch.

Start at Login is a native checkable menu item backed by
`tauri-plugin-autostart`. Registration is opt-in, never enabled during setup.
The OS registration is authoritative and is read back after every toggle.
There are no new webview commands or permissions. Workbench and Show Pages
remain unprivileged.

## Frozen stop semantics

- `avibe-runtime-host` owns the Tauri-free operation.
- Missing launch ownership is an error, never a best-effort stop.
- The host retains the exact resolved launcher/interpreter that launched the
  Runtime. Stopping does not resolve an executable, search PATH, or invoke a
  shell interpreter.
- The Runtime's own `stop` command owns shutdown. The helper inherits the same
  null standard streams and process isolation as startup.
- Successful stop releases ownership. A failed stop preserves it for an
  explicit retry. Confirmed Runtime loss releases ownership as before.
- Tests prove adopted refusal, launch/stop executable identity, argument shape,
  failure retention, and stop/monitor/bootstrap exclusion.

## Scope and dependencies

Based on `origin/desktop` at `b2ed55f11`. The authoritative gap document is
the orchestrator's desktop worktree copy, not a file copied into this branch.
The orchestrator approved generated `desktop/Cargo.lock` changes, new
`desktopBootstrap` locale keys in the central English/Chinese catalogs, and
the ownership-gated runtime-host stop API. Tauri core gains the `tray-icon`
feature; the only new plugin is `tauri-plugin-autostart = "2"`, matching the
existing plugin version style. Signing/packaging files are untouched.
The additive stopped notice also updates the desktop i18n verifier's frozen
notice list, with the orchestrator's explicit approval.

## Verification

- Hermetic Rust unit and source-boundary tests; no local Runtime or OS login
  registration is changed by tests.
- Bootstrap i18n parity and frontend build before Cargo compilation.
- `cargo fmt --all -- --check`, workspace/all-feature Clippy with warnings
  denied, and workspace/all-feature tests.
- CI exercises macOS and Windows. Manual integration must verify tray rendering,
  close/reopen, all quit choices, stop confirmation/cancellation, status recovery,
  and opt-in login registration across a real OS login on both platforms.

## Known by design

- Workbench Settings integration is deferred; the native toggle is the only
  login-item control in this slice.
- A Runtime deliberately kept running is adopted on the next shell launch and
  cannot then be stopped via this session's tray ownership path.
- Runtime version reporting is not added to `/ready`; the tray shows the
  already-proved listener and current readiness without creating a status API.
- Native GUI/login testing remains an integration acceptance step, not a claim
  made from a source-boundary test or a successful compile.
- While bootstrap, uninstall, or stop owns the lifecycle activity, a competing
  Stop or Quit reports busy instead of interrupting a launch/removal command.
  The user retries when that bounded startup or explicit operation finishes.

## Local evidence (2026-09-10, macOS)

- Bootstrap dependency install, i18n parity, and production frontend build pass.
- Central Workbench build passes with existing third-party annotation/chunk-size
  warnings; no unrelated dependency or bundle changes were made.
- Rust format check and all-target/all-feature Clippy pass with warnings denied.
- Workspace all-feature tests pass: 15 shell unit, 20 shell boundary, 69 host
  unit, 21 bootstrap integration, and one host documentation test.
- Default-feature workspace tests also pass, exercising the installed-Runtime
  build alongside the private-Runtime feature build.
- All stop tests use fakes or test-owned executables; no running local service,
  login item, real user home, or private Runtime install was modified.

## Progress

- [x] Inspect ownership, bootstrap, monitoring, localization, and capability gates.
- [x] Implement native lifecycle and opt-in login control.
- [x] Verify the frozen contract with hermetic tests and builds.
- [ ] Deliver a desktop-base PR with exact-head review and passing CI.
