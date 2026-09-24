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
- A retained launch attempt only deduplicates starts; it is never stop authority.
  Only a successful launcher exit plus a valid schema-v1 `started` receipt grants
  authority. `reused`, absent/malformed/duplicate receipts, failed helpers, and
  helpers still running grant none. Readiness alone never grants authority.
- The producer contract is `docs/plans/desktop-start-receipt.md` in the separate
  `feat/desktop-start-receipt` lane. Its receipt is sourced from the service
  startup function's actual spawn/reuse branch, never before/after snapshots.
  UI reuse does not change the service's provenance outcome.
- Startup stdout is drained without forwarding it to a webview. At most 64 KiB
  is retained; oversized output is drained and discards authority. Exactly one
  line beginning `@avibe-start-receipt:` is accepted. Schema, positive PIDs, and
  finite positive creation timestamps are validated, including fractional ms.
- Missing proven ownership is an error, never a best-effort stop.
- The host retains the exact resolved launcher/interpreter that launched the
  Runtime. Stopping does not resolve an executable, search PATH, or invoke a
  shell interpreter.
- The Runtime's own `stop --receipt <JSON>` command owns shutdown. The helper
  uses the same process isolation as startup and null standard streams. This
  path never falls back to an unscoped stop or re-resolves an executable.
- Python validates the current service PID and creation time (2 ms tolerance)
  at its service-owner gate. Exit 3, including invalid receipt, replaced PID,
  unavailable identity, and changed creation time, revokes authority and returns
  a localized `runtime_ownership_lost` bootstrap screen with explicit Retry.
  Retry uses the existing endpoint discovery and `/ready` state machine; no
  second status channel or automatic stop/restart is introduced.
- Successful stop releases ownership. Other stop failures preserve it for an
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
- A Runtime deliberately kept running is adopted on the next shell launch and
  cannot then be stopped via this session's tray ownership path.
- Receipt v1 is a service-owner gate, not a per-process capability: a matching
  service identity invokes today's full graceful stop, including UI, service
  sweep, OpenCode, and tunnel. UI reuse does not prevent this full stop. The
  gate does not prevent replacement after validation or independently authorize
  each auxiliary process. The orchestrator explicitly accepted these v1 limits
  on 2026-09-10; no per-process guarantee is claimed.
- Older Runtime packages without receipt support can still serve/adopt, but
  cannot gain tray stop authority. A ready Runtime whose helper has not finished
  is also treated conservatively until a successful receipt becomes available.
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
