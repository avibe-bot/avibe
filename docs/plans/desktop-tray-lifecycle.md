# Desktop tray lifecycle and login item

## Contract

The native shell remains available after its main window closes. Its tray and
first application menu expose the same Runtime status and explicit lifecycle
actions. Closing a window hides it; it neither exits nor stops a Runtime.
Status is derived from the existing bootstrap state and `/ready` monitor, not
another endpoint or Webview-provided signal. Serving includes the proved
listener; transient probe failures show unreachable while the existing
three-failure recovery threshold remains unchanged.

Stop is offered only for this app's own Runtime. Confirmation stops it without
exiting the shell. The bootstrap then shows a localized stopped notice and an
explicit Retry action; automatic recovery must not undo a user's Stop.
Quit while this app's Runtime serves offers stop-and-quit, keep-running-and-quit,
or cancel. Quit otherwise leaves the adopted Runtime alone. Startup and
uninstall serialize against lifecycle actions; no stop may race a launch.

Start at Login is a native checkable menu item backed by
`tauri-plugin-autostart`. Registration is opt-in, never enabled during setup.
The OS registration is authoritative and is read back after every toggle.
There are no new webview commands or permissions. Workbench and Show Pages
remain unprivileged.

## Stop semantics

PR 2b of #2135 retired the startup receipt this section used to freeze.
`desktop-runtime-scoped-stop.md` owns the stop rules; the sections below that
mention receipts record the receipt-era lane.

- `avibe-runtime-host` owns the Tauri-free operation.
- A retained launch attempt only deduplicates starts; it is never stop authority.
  Authority is the last presence: Stop is offered only while the Controller
  serving the origin carries this bundle's Runtime id (`Mine`). Readiness alone
  never grants authority.
- The host stops through the Runtime's own `vibe stop --expect-runtime-id <id>`,
  run by the exact resolved launcher. Stopping does not resolve an executable,
  search PATH, or fall back to an unscoped stop.
- Exit 0 releases authority. Exit 3 revokes it and shows the retryable
  `runtime_ownership_lost`. Exit 2 or a CLI that cannot run keeps it and shows
  `runtime_stop_failed`. No stop runs while a launch or another stop is pending.

## Scope and dependencies

Based on `origin/desktop` at `b2ed55f11`. The authoritative gap document is
the orchestrator's desktop worktree copy, not a file copied into this branch.
The orchestrator approved generated `desktop/Cargo.lock` changes, new
`desktopBootstrap` locale keys in the central English/Chinese catalogs, and
the ownership-gated runtime-host stop API. Tauri core gains the `tray-icon`
feature; the only new plugin is `tauri-plugin-autostart = "2"`, matching the
existing plugin version style. Signing/packaging files are untouched.
The additive stopped and ownership-lost notices also update the desktop i18n
verifier's frozen notice list, with the orchestrator's explicit approval.
This consumer requires #1977 merged first (the startup-receipt producer);
it does not copy or stack on that lane's Python implementation.

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
- A Runtime deliberately kept running is adopted as this app's own on the next
  shell launch, and Stop is offered for it again.
- A Runtime without this bundle's id, including one without any desktop
  identity, can be adopted but is never offered Stop.
- Runtime version reporting is not added to `/ready`; the tray shows the
  already-proved listener and current readiness without creating a status API.
- Native GUI/login testing remains an integration acceptance step, not a claim
  made from a source-boundary test or a successful compile.
- While bootstrap, uninstall, or stop owns the lifecycle activity, a competing
  Stop or Quit reports busy instead of interrupting a launch/removal command.
  The user retries when that bounded startup or explicit operation finishes.

## Pre-review local evidence (2026-09-10, macOS, head 8856b1648)

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

## P1 review correction

Review comment 3975484212 identified invocation-based ownership as invalid:
an initial readiness miss can lead an idempotent start helper to reuse an
external service. That review concerns head 8856b1648; this is the first
findings-bearing head and the root-cause class is launch provenance.
The consuming correction separates launch deduplication from receipt-proven
stop authority and passes the exact receipt to Python's service-owner gate.
Corrected local validation passes after integrating baseline #1972 with a
history-preserving merge: bootstrap `npm ci`, i18n parity and build; Cargo format
check; workspace/all-target/all-feature Clippy with warnings denied; all-feature
tests (15 shell + 21 boundary + 78 host + 21 bootstrap = 135, plus one doctest);
default-feature tests (132 plus one doctest); central UI `npm ci` and build.
One initial full run hit two existing health fixture read timeouts; all eight
health tests passed the focused rerun and the unchanged full suites then passed.
No health fixture or unrelated code was modified. Existing npm audit/build
warnings and a non-fatal Cargo global-cache cleanup permission warning remain
outside this lane; no user cache or dependency repair was attempted.

## Progress

- [x] Inspect ownership, bootstrap, monitoring, localization, and capability gates.
- [x] Implement native lifecycle and opt-in login control.
- [x] Verify the frozen contract with hermetic tests and builds.
- [ ] Deliver a desktop-base PR with exact-head review and passing CI.
