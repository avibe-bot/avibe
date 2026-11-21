# Computer Use: Settings navigation without a permission request

## Background and baseline

Owner decision, 2026-11-18: **Open Settings** in the Computer Use guide should
navigate to the selected privacy pane, not request permission and leave an
additional macOS confirmation above it. This is distinct from #3933, which
removed Avibe's own Screen Recording explanation alert.

Implementation base: `c7953082e28b1fa6a838c461d20a6b066bcd14f7`.
Installed acceptance baseline supplied by the orchestrator:
`3.2.7-rc.14.w1.1`, source `9fa2b748c3a38c0826f71f4e078717dad8dcb987`.
Both have the same prompting `RuntimeState::open_permission` path:
Accessibility calls `request_accessibility_grant`; Screen Recording calls
`request_permissions`, which also requests Accessibility and performs a
one-pixel capture. No peer fix is present at the implementation base.

## Change contract

- `computer_use_open_permission` queues the selected pane unchanged. Handling
  that event opens only that pane and performs the existing silent refresh.
  Navigating must never initiate an Accessibility/Screen Recording permission
  request or a registration capture, regardless of current permission state.
- Preserve the Screen Recording setup-resume marker, its settlement rules,
  `activated` refresh, and the existing driver start after both grants arrive.
  Opening Settings does not grant permission, enable Computer Use, or convert
  unknown state into a grant.
- Keep **Turn on** / the application-menu and tray switches unchanged. Their
  intentional permission request, shell one-pixel registration attempt and
  lifecycle-managed permission child remain in place.
- No automatic handling of existing macOS alerts; no TCC writes or resets.
- No UI layout/copy change is necessary: both localized guide steps already
  explain adding Avibe with `+` if it is missing (Screen Recording also offers
  dragging the app). Removing the navigation request may leave a never-used
  app unlisted until the user adds it; do not imply automatic registration.

## Platform evidence

Apple's `AXIsProcessTrustedWithOptions` reference describes
`kAXTrustedCheckOptionPrompt` as an optional asynchronous notification which
does not change the permission-check result. Apple's Mac User Guide,
“Allow accessibility apps to access your Mac”, explicitly supports adding a
missing app with the `+` button, selecting the app, and clicking Open.
These support separating navigation from prompting; they do not prove a
particular ad-hoc installed app identity will retain a grant.

## Validation plan

Primary regression boundary: execute the source controller event dispatch and
permission-navigation method with recording platform adapters and the real
`ComputerUseLifecycle`. Existing UI tests stop at native `invoke`, and existing
native tests check pane names/queue admission rather than the handler effects.
The new test must fail on the pre-fix handler because it actually calls a
prompt/capture adapter, not because a source string disappears.

Cover both pane selections, repeated navigation with absent/present grants,
unknown Runtime support, silent activation after a user grants access, and the
unchanged enable/menu request path. Run the existing UI caller/status tests and
native lifecycle tests; compile the desktop without launching it. Run tests
serially with test-owned files. No installed app, OS permissions, driver, or
window is used.

## Delivery boundary and real-package check

Freeze the diff and local evidence for the orchestrator. Do not push, open a
PR, replace the installed app, or touch the frozen W1 acceptance tree.
The orchestrator schedules a later package check: both **Open Settings**
buttons land on the matching pane without a new app-requested confirmation;
if Avibe is absent, `+` can add the exact installed app; after a manual grant,
returning to Avibe updates the guide and resumes Computer Use if enabled.
Screen Recording Quit & Reopen must still return to the guide without stopping
the Runtime. Initial enable is a separate positive control and may still
prompt. Do not reset the owner's permissions to manufacture this test.

The currently installed package is ad-hoc. Final permission-identity acceptance
needs the signed RC; local source tests and a compile do not establish that.

## Todo

- [x] Same-source regression fails on the unchanged baseline.
- [x] Remove navigation-only requests; preserve resume/activation behavior.
- [x] Focused consumers, lifecycle checks, lint and desktop compile pass.
- [x] Freeze local commit/diff for the orchestrator; real-package check is deferred.

## Local evidence (2026-11-18)

- Same-source controller fixture: baseline **3 failed / 1 passed**. Two
  Accessibility clicks recorded `(2, 0, 0)` request/capture effects; two Screen
  Recording clicks recorded `(2, 2, 2)`. The enable/menu positive control passed.
  Fixed source: **4 passed**, including 48 navigation combinations, activation
  recovery and marker settlement. Adapter functions record calls; no OS calls
  or windows execute. The simulated driver acknowledges real lifecycle start
  directives; this proves dispatch/state behavior, not real driver health.
- Existing consumers: **87 UI tests**, **39 macOS Computer Use tests**,
  **21 runtime-host lifecycle tests**, **52 shell-boundary tests** passed.
- `cargo build --locked --offline -p avibe-desktop` passed, without launching
  the binary. Focused Clippy with `-D warnings`, Cargo fmt, Ruff and diff checks
  passed. No UI product code changed, so a UI bundle rebuild is not part of
  this change's evidence.
- `tests/permission_navigation/run.py` is an explicit same-source harness,
  **not automatically discovered by ordinary Cargo tests**; its README gives
  the invocation. CI wiring and signed-package acceptance remain later gates,
  not claimed here. No PR, push, package replacement or permission reset.
- The substantive production diff is confined to one macOS adapter:
  `open_permission` becomes navigation + existing resume/activation handling,
  and its now-unused Accessibility request wrapper is removed. The full
  initial-enable request, one-pixel capture and lifecycle files are unchanged.

