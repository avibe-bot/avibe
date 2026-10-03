# Desktop computer use via embedded Cua Driver

## Status

- Owner: Avibe core
- Date: 2026-10-01 (Phase 0 findings through 2026-10-03)
- Stage: plan, Phase 0 spike done on macOS; the Windows run of Q6 is open
- Upstream: [trycua/cua](https://github.com/trycua/cua) `libs/cua-driver`,
  release `cua-driver-rs-v0.31.0` (2026-09-30), MIT
- Companion docs: `desktop-tray-lifecycle.md` (native toggle pattern),
  `desktop-auto-update.md` (signing), `desktop-product-gaps.md`

## Background

Avibe agents can run shell commands and take a desktop screenshot
(`vibe screenshot`), but they cannot operate GUI apps. Codex's own
`features.computer_use` is deliberately disabled
(`modules/agents/codex/transport.py`), and Claude Code and OpenCode have no
equivalent tool. The result is that one GUI-only step blocks a whole task.

Cua Driver is a Rust runtime that observes and operates native apps on macOS,
Windows, and Linux. It exposes an MCP stdio server: screenshots, window and
accessibility state, click, type, hotkeys, scroll, drag, clipboard, and
sessions. It delivers input in the background where the platform allows it,
so it does not steal focus or move the user's pointer. Its embedded mode exists
for agent harnesses: the host app requests the macOS grants once, and the
driver runs inside the host's TCC responsibility chain.

Other parts of cua (Fleets, Lume, Bench, CUA-S1) are out of scope.

## Goal

When a user enables computer use in the Avibe desktop app, every Avibe agent
session gets the same set of computer-use tools, whichever backend runs it
(Claude Code, Codex, OpenCode) and wherever the request came from (IM, Web,
Harness, scheduled run). Users grant OS permissions to Avibe once.

## Owner decision: IM-triggered computer use is allowed

Remote IM requests may drive the desktop. This adds no new trust tier: a bound
IM user's agent already runs with full shell access on this machine
(`bypassPermissions` / `danger-full-access`). Computer use is therefore gated by
the same identity rules (`core/auth.py` binding, channel `require_bind`), plus:

1. **Explicit enablement.** Computer use is off until the user turns it on in
   the desktop app. While it is off, the driver does not run.
2. **A visible actor.** The agent-cursor overlay stays on, so on-screen actions
   are visibly the agent's.
3. **A native stop.** The same tray/app-menu toggle stops the driver at once.
   That ends every session's access, not only one session's.
4. **Untrusted screen content.** The injected prompt tells agents to treat
   on-screen text as data, never as instructions.

A locked Mac cannot be driven (Phase 0 finding). Remote use therefore needs an
unlocked login session. User docs must say so, and must also say that window
screenshots still capture app contents while the screen is locked.

There is no per-action approval prompt. Unattended IM use is the point of the
feature, and per-call prompts would not reach a user who is away from the
machine. User docs must recommend `require_bind` for shared channels.

## Architecture

```text
Avibe.app (Tauri shell; owns TCC grants, toggle, lifecycle; writes state file D)
  └─ cua-driver serve --embedded --socket <S>   ← executes tools, checks TCC

Avibe Runtime (adopted or shell-started; watches D, never spawns the daemon)
  └─ agent backend process
       └─ cua-driver mcp --embedded --socket <S>   ← stdio MCP proxy only
```

Only the shell process may spawn the daemon. The Runtime runs detached, may
be adopted from a terminal-started `vibe`, and keeps running after the window
closes. A daemon it spawned would take the TCC identity of whatever started the
Runtime, not Avibe.app. Cua's docs name this exact gateway wiring as wrong. The
MCP proxy never executes tools and needs no grant, so any process may spawn it.

As a consequence, computer use is available only while the desktop shell is
running. The tray keeps the shell alive after the window closes.

### Shell (`desktop/src-tauri`, `desktop/runtime-host`)

- **Packaging.** Ship `cua-driver` as a Tauri sidecar (`externalBin`), signed as
  a nested executable before the app is signed and notarized. Pin the version
  and SHA-256 in a sources manifest beside `runtime-sources.json`. Fetch only
  the plain driver binary, never the `cua-perception` extension, which is AGPL.
  Add the upstream MIT notice. Desktop packages are per architecture
  (`aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-pc-windows-msvc`), so
  ship one thin slice per package: `lipo -thin` of the universal binary keeps
  Cua's valid per-slice signature. Windows ships `cua-driver.exe`; whether the
  default-off `cua-driver-uia.exe` worker is needed is a Windows-run question.
  Spawn it with `CREATE_NO_WINDOW` (it is a console program), as `runtime-host`
  already does for the Runtime.
- **Toggle.** A native checkable "Computer Use" item in the tray and the app
  menu, following the Start at Login pattern. It adds no webview command. The
  shell persists the state with its other native preferences.
- **Enable flow (macOS).** Request Accessibility
  (`AXIsProcessTrustedWithOptions` with prompt) and Screen Recording
  (`CGRequestScreenCaptureAccess`; fall back to opening the Settings pane).
  Start the daemon only after both report granted. Prompt only from the user's
  toggle action; app launch checks silently, because every prompting call while
  a grant is missing queues another system dialog. On macOS 26 an app appears in
  the Screen Recording pane only after a real capture attempt, so the request
  step also runs one capture probe (the driver's `check_permissions` with
  `prompt: true` does this as the host). If a grant changes later, restart the
  daemon, because macOS caches TCC answers per process.
- **Daemon.** Spawn directly with `posix_spawn`/`Command`, never through
  `open`/LaunchServices. Environment: `CUA_DRIVER_EMBEDDED=1`,
  `CUA_DRIVER_HOST_BUNDLE_ID=<bundle id>`,
  `CUA_DRIVER_PERMISSION_MODE=standard`,
  `CUA_DRIVER_MANAGED_POLICY_FILE=<bundled tool policy>`,
  `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, `CUA_DRIVER_RS_UPDATE_CHECK=0`,
  `CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS=300`. Use a private socket under the
  Avibe run directory. The driver's parent-liveness pipe ends it if the shell
  dies. An orderly quit stops it explicitly. A terminated daemon leaves its
  socket file behind, and a new daemon refuses to start on an existing endpoint,
  so the shell removes the stale socket (after confirming its own daemon has
  exited) before each spawn.
- **State file `D`.** `~/.avibe/run/desktop-computer-use.json` is the only
  shell-to-Runtime contract. The shell writes it atomically on every state
  change. It is also the only cross-process record: the Runtime and the
  separately running Workbench API process read the same file.
  - Every state carries `schema_version`, `state`, `reason` (a stable code,
    or null), `shell_pid`, and `generation`. `state` is one of these:
    - `off`: the toggle is off.
    - `needs_permission`: the toggle is on and a grant is missing.
    - `starting`: the toggle is on, both grants are held, and the daemon is
      not yet accepting connections.
    - `ready`: the socket accepts connections.
    - `error`: start or health failed.
  - `ready` also carries `socket_path`, `proxy_executable` (absolute path to
    the bundled binary), `driver_version`, and `host_bundle_id`.
  - `generation` increases with each daemon spawn.
  - The shell writes a non-ready state before stopping the daemon. It removes
    the file on orderly quit.
  - A fixed path under the Avibe home works for adopted Runtimes, which never
    see the shell's launch environment.
- **Tool policy.** Ship a YAML allow-list as the driver's managed policy
  (Phase 0 finding). It is pinned with the driver version, because the list
  must be reviewed against each new tool surface. It allows observation,
  app launch/front/frame, menu, pointer, keyboard, value, clipboard, session,
  `check_permissions`, and `health_report` tools. It omits config, update,
  extension, recording/replay, cursor-theme, legacy `page`, the typed browser
  tools, and visual parsing. Typed browser tools are deferred, not rejected:
  they need their own runtime and origin scope.
- **Health.** After start, call `check_permissions` and
  `health_report(include=["bundle_identity"])` and require
  `source.attribution == "host"`. On failure, write `D` as `error` with its
  reason, stop the daemon, and show the localized reason in the menu.

### Runtime (`core/`, `modules/agents/`)

- **One owner.** A new `core/computer_use.py` reads `D` and derives one
  effective status. It is `ready` only when the state is `ready`, `shell_pid` is
  alive, the proxy path is absolute, and the socket accepts a connection. A
  missing file or a dead `shell_pid` derives `unavailable`. Otherwise the
  recorded state is used, so a crashed shell's stale file never reads as
  `ready`. Only `ready` yields the single managed stdio MCP spec: name
  `computer`, command `proxy_executable`, args `mcp --embedded --socket <S>`,
  env `CUA_DRIVER_EMBEDDED=1`, `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`,
  `CUA_DRIVER_RS_UPDATE_CHECK=0`. The proxy runs its own update check and prints
  a banner without the last one. Backends only translate this spec. They do not
  decide availability.
- **Claude Code.** Pass `mcp_servers={"computer": ...}` in the session options
  built in `core/handlers/session_handler.py`. SDK 0.2.93 supports
  `McpStdioServerConfig`. Keep `strict_mcp_config` unset so user MCP config is
  preserved. An agent file that sets `tools` must list `mcp__computer__*` to
  keep access.
- **Codex.** Append `-c mcp_servers.computer.*` overrides to the app-server
  launch, after Avibe's fixed overrides. `features.computer_use=false` stays:
  Avibe supplies one cross-backend tool instead of Codex's own. Two Codex
  specifics (Phase 0, Q4): set
  `mcp_servers.computer.default_tools_approval_mode="approve"`, because Avibe
  runs Codex with `approvalPolicy: never` and Codex cancels every non-read-only
  MCP call under that policy; and launch the server through a small
  Avibe-owned stdio filter (Avibe's Python runs it in front of the proxy). The
  filter moves `structuredContent` into a trailing text block and drops
  `outputSchema`, because Codex drops MCP image content when
  `structuredContent` is present. Remove the filter once that Codex bug is
  fixed. Tool restriction stays in the driver's managed policy, not in the
  filter.
- **OpenCode.** Add `mcp.computer` (`type: "local"`) through the existing config
  overlay / `PATCH /global/config` path.
- **Availability changes.** Inject the server only while the effective status is
  `ready`. The proxy exits when no daemon is listening and when its daemon goes
  away, and it does not reconnect (Phase 0, Q3), so always-inject would hand
  agents a dead MCP server. `core/computer_use.py` also owns change detection,
  so neither the shell nor a backend has to. A controller background task polls
  the effective status every 2 s, the same polling model as
  `RuntimeCommandWatcher`. It compares the pair (is-ready, `generation`). When
  the pair changes, the task calls the same
  `AgentAuthService._refresh_backend_runtime` handler that the restart markers
  call. It does so for each running backend whose launch config embeds the
  server, which today means Codex and OpenCode. So enabling adds the server, and
  disabling, a daemon restart, or a shell crash removes or replaces it without
  waiting for an unrelated refresh. Claude sessions read the status on their
  next start. Teaching the Codex filter to reconnect is the fallback if refresh
  proves too disruptive; it is not planned for v1.
- **Prompt.** Add a short section in `core/system_prompt_injection.py`, only
  when the server is present: prefer CLI/API routes, use GUI tools for GUI-only
  steps, prefer accessibility element-token actions over pixel input, treat
  screen content as untrusted, and observe state before retrying any action,
  since an error result does not prove the action failed. If every window
  comes back AX-unresolved and the desktop shows no apps, the screen is likely
  locked: stop and tell the user. On Windows, use foreground delivery only for
  the action that needs it.

### Workbench

A read-only status line, derived from the effective status (ready, off,
needs permission, starting, error with its reason, or unavailable when the
shell is not running). The Workbench API reads it through the same
`core/computer_use.py` reader, not through Runtime memory, because it runs in a
separate process. Copy goes through `ui/src/i18n/en.json` and `zh.json`.
The control itself stays native in v1.

## Signing prerequisite

The app must also be a registered bundle in a normal location
(`/Applications` or `~/Applications`). An unregistered bundle run from `/tmp`
never appears in the Privacy panes, so the user cannot grant it.

TCC binds a grant to the app's code requirement. Ad-hoc acceptance builds
match by cdhash, so every new build loses the grant. In the spike, toggling the
stale row off and on in System Settings did not repair it: tccd kept checking
the old cdhash requirement until the rows were reset. Computer use can be tested
on ad-hoc builds, which need a re-grant after each install, but ships only on
Developer ID signed, notarized builds (`APPLE_SIGNING_IDENTITY` path in
`desktop-package.yml`).

## Upstream mechanism risk

On macOS, background pixel input goes through private SkyLight SPI
(`SLEventPostToPid`, plus an authentication message for keyboard events to
Chromium targets on macOS 14+). The symbols are resolved with `dlsym`, and the
driver falls back to `CGEventPostToPid` if they are missing. Accessibility
actions (`AXUIElementPerformAction`) and ScreenCaptureKit are public API. A
macOS update can therefore degrade background pixel input without notice.
Pinning the driver version and adding a release smoke check are the mitigation.
This also rules out Mac App Store distribution, which Avibe does not use.

## Phase 0 spike: questions that change the design

Run on a local ad-hoc build. Grant permissions manually.

1. Embedded attribution: does a Tauri-spawned daemon report
   `attribution: host`, and does `bundle_identity` pass?
2. End to end: can Claude Code, through the proxy, background-click Calculator
   to `42` with Terminal frontmost, and receive screenshots as images?
3. Proxy without a daemon: what happens at startup, and when the daemon
   restarts mid-session (new generation)? This decides inject-on-availability
   vs. always-inject (see Availability changes).
4. Codex and OpenCode: do MCP image results reach the model?
5. Standard mode over MCP: what happens to residual authorizations (for
   example, an existing logged-in Chromium profile) when no
   `DriverAuthorizationHost` is installed? v1 accepts a refusal.
6. Windows: is a release asset available, and does the same spawn flow work
   (UIPI: elevated windows refuse)?
7. Size: the macOS universal binary tarball is about 44 MB. Confirm the
   installed size delta.

### Phase 0 findings (2026-10-01 to 2026-10-03, macOS 26.2, driver 0.31.0)

Direct-mode runs used `cua-driver mcp --direct` from a scratch directory with
`HOME` redirected. Embedded runs used a minimal ad-hoc signed Swift host
(adapted from upstream `examples/embedded-host-macos`) that spawned
`serve --embedded` directly and talked to it through `mcp --embedded`.

- **Q1 embedded attribution: yes.** `check_permissions` reports
  `attribution: host` and `bundle_identity` passes with
  `identity_source: parent_application`. tccd logs the daemon's Screen
  Recording requests with the host as the responsible process. Granting
  Accessibility and Screen Recording to the host alone was enough for a
  background Finder AX tree plus window screenshot, and the driver raised no
  prompt of its own. Grants are cached per process, so the host restarts the
  daemon after a grant changes, as planned.
- **Q2 end to end with Claude Code (macOS): works, with reliability caveats.**
  A headless Claude Code 2.1 session got the server through `--mcp-config`
  (`mcp --embedded --socket`), saw all 58 tools, and received screenshot image
  blocks (it described window details that were only in the image). Calculator
  was never frontmost in a 0.2 s frontmost-app sample across both runs while
  the user kept working. Run 1 (6×7) used AX element tokens: 9 calls, 54 s.
  Run 2 (8×9) started with background pixel clicks, which were unreliable on
  Calculator: one was refused, one landed late, and later digits registered
  twice. It recovered by switching to AX presses. Those returned
  `-25204` yet took effect (upstream trycua/cua#3836 tracks this). Run 2 took
  34 calls and 7 minutes. Consequence: the injected prompt must say to prefer
  element-token actions, to use pixel input only as a fallback, and to observe
  before retrying any action, because an error result does not prove the
  action failed.
- **Q3 proxy without a daemon: it exits.** With no daemon on the socket, the
  proxy exits before the MCP handshake. When the daemon stops, the connected
  proxy exits too, and it does not come back after the daemon restarts. A
  fresh proxy works against the restarted daemon. This settles Availability
  changes: inject only while the effective status is `ready`.
- **Q4 Codex (codex-cli 0.145.0): images are dropped, and writes are
  cancelled. Both have workarounds.** Every cua image tool
  (`get_window_state`, `get_desktop_state`, `zoom`) returns `structuredContent`
  next to its image. Codex then forwards only the structured part. An A/B
  check with a one-tool server showed this: without `structuredContent` the
  model read `72` from the image; with it, the model saw no image (same result
  on two models). Upstream openai/codex#10334 tracks this and is open. A stdio
  filter that folds `structuredContent` into a text block fixed it end to end:
  Codex read `72` from a cua `get_window_state` screenshot. Separately, under
  `approval_policy=never` Codex cancelled every MCP tool without
  `readOnlyHint: true`, reporting `user cancelled MCP tool call`. cua marks
  `click`, `type_text`, `press_key`, `launch_app`, and `kill_app` that way.
  Setting `default_tools_approval_mode="approve"` on the server let a
  destructive-annotated tool run; `auto` still cancelled it.
- **Q4 OpenCode (1.18.16): images arrive and need no workaround.** A
  sandboxed `opencode run` (all XDG dirs redirected, provider passed through
  `OPENCODE_CONFIG_CONTENT`) returned the image as an attachment, with and
  without `structuredContent`. Through the real cua proxy, the model read `72`
  from a `get_window_state` screenshot. A destructive-annotated tool ran under
  the default permissions. So the stdio filter and the approval override are
  Codex-only translation details. They do not belong in the shared spec.

- **Artifact.** The universal tarball's SHA-256 matches `checksums.txt`. The
  binary is Developer ID signed by Cua AI (`YCK386LBJ7`) but not notarized, and
  is 71 MB unpacked. Avibe re-signs it as a nested executable. From 0.33.0
  each asset also carries a sigstore bundle; pin the hash either way.
- **Telemetry is on by default.** Even `list-tools` creates a telemetry id.
  `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` disables it and nothing is written. The
  shell must set this together with `CUA_DRIVER_RS_UPDATE_CHECK=0`; Avibe owns
  the driver version.
- **Tool surface: restrict with a managed tool policy, not a manifest.** The
  server exposes 58 tools, including `install_extension`, `set_config`,
  `check_for_update`, and `replay_trajectory`. Agents must not change driver
  config, install extensions (`cua-perception` is AGPL), or update the driver.
  A capability manifest does not fit general computer use: it is deny by
  default for resources too, so every app would have to be listed by bundle
  id. The driver's YAML tool policy only names tools. Set as
  `CUA_DRIVER_MANAGED_POLICY_FILE` on the embedded daemon, a 30-tool allow-list
  shrank `tools/list` to 30 through the proxy. Omitted tools returned
  `permission_denied`, and allowed ones worked. Agent-side environment on the
  proxy could not widen it: a widening user policy, a widening managed policy,
  and `unrestricted` mode variables each left the daemon's surface unchanged.
  `kill_app` stays allowed, because standard mode only terminates processes
  this runtime launched.
- **End to end.** `launch_app` started Calculator in the background
  (`self_activation_suppressed: true`). `get_window_state` returned a PNG plus a
  154-element AX tree in about 1.9 s. Five AX `click`s by `element_token` gave
  `6×7 = 42`, read back from the AX tree.
- **Latency.** After each action the driver watches for window changes for up
  to 1000 ms by default, and the daemon reads that bound from
  `CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS` (0 to 10000). An A/B on embedded
  background Calculator AX clicks ran 2 rounds of six clicks per setting.
  1000 ms gave about 2.3 s per click, 300 ms about 1.6 s, and 0 ms about
  1.35 s. Every run computed `12×3 = 36` correctly with no errors. A no-op call
  through the proxy takes 1 to 2 ms, so the remaining 1.3 s floor sits inside
  Calculator's AXPress (the slow `-25204` path, trycua/cua#3836). The shell
  sets 300 ms: it saves about 0.7 s per action and still gives the watch a
  short window to report a new window or sheet. The watch also holds the
  driver's focus-steal lease, which reverts another app's activation, so `0`
  would drop that protection. Upstream measured 78 ms at `0`, so the floor
  depends on how fast the target app answers. Click results report
  `effect: unverifiable` either way, so agents observe after acting.
- **Element tokens are session-scoped** (`s00000001:5`). Observe and act on one
  persistent MCP connection. A one-shot CLI call cannot reuse a token.
- **A locked screen blocks all input; window capture still works.** With the
  session locked (`CGSSessionScreenIsLocked`), every `get_window_state`
  returned zero AX elements. The cause was `ax_window_unresolved` (no AXWindow
  under the pid), and the driver refused every background input route.
  Calculator also hit 7 s AX timeouts. `get_desktop_state` returned only the
  lock-screen wallpaper, but window capture still returned the real Calculator
  contents. `health_report` still said all checks were ok, and no result names
  the lock. Consequence: the injected prompt must say that when every window
  comes back AX-unresolved and the desktop shows no apps, the screen is likely
  locked, so the agent stops and tells the user instead of retrying. Ask
  upstream to report the lock explicitly. Avibe will not try to unlock or keep
  the screen awake.
- **Q5 residual authorization: unreachable under the v1 tool policy.** In
  standard mode the residual boundary that needs a grant or a
  `DriverAuthorizationHost` is attaching to an existing logged-in Chromium
  profile. Only the typed browser tools and `page` reach it, and the managed
  policy hides all of them, so v1 needs no authorization host. Two other
  standard-mode refusals held over MCP: `kill_app` on a process the runtime
  did not launch returned "may terminate only a process proven to have been
  launched by this Cua runtime", and the process survived. `check_permissions`
  with `prompt: true` raised nothing once the host held both grants.
  Attaching to a real Chrome profile without the policy was not exercised, on
  purpose.
- **Q6 Windows: assets exist; the spawn flow is unverified.** Releases ship
  `windows-x86_64` and `windows-arm64` zips. `cua-driver.exe` (33 MB) and
  `cua-driver-uia.exe` (21 MB) are Authenticode signed by Cua AI, Inc., and
  both are console programs. Upstream says embedding works on Windows with no
  permission step, provided the daemon runs in the interactive logon session
  (Session 0 is refused). The daemon is a child of the desktop shell, which
  runs in that session, so it qualifies. Upstream also lists three Windows
  limits that matter for the prompt and docs:
  - Elevated targets need a high-integrity daemon. v1 accepts the refusal.
  - Chromium page content, terminals, and GTK or custom-drawn surfaces
    cannot be driven in the background. They need
    `delivery_mode: "foreground"`, which briefly takes focus.
  - The window-change wait does not exist on Windows.

  Still open, and only answerable on a Windows machine:
  - Does `serve --embedded --socket` work there?
  - Can the agent's proxy reach the daemon?
  - Is the `cua-driver-uia.exe` worker needed?

  Windows stays behind macOS (see Todo).
- **Q7 size.** Today's macOS app is 17 MB installed. The thin driver adds
  33 MB (arm64) or 35 MB (x86_64) installed, and about 12 MB to a
  zlib-compressed DMG. On Windows, `cua-driver.exe` adds 33 MB installed and
  about 8 MB to the LZMA-compressed NSIS installer. With the UIA worker it
  is 54 MB and about 12 MB. This is acceptable for a feature
  that is off by default. A lazy download was considered and rejected, because
  it adds a second supply-chain and update path for about 12 MB.
- **Focus/cursor evidence was confounded.** The user was using the machine
  during the run, so frontmost-app and cursor changes cannot be attributed. Redo
  this on an idle desktop.

## Todo

- [x] Phase 0 spike on macOS; answers recorded above
- [ ] Phase 0 Q6 on a Windows machine: embedded spawn, proxy reach, UIA worker
- [ ] Shell: sidecar packaging, pinned manifest, signing order, notices
- [ ] Shell: toggle, permission flow, daemon lifecycle, state file `D`, health
- [ ] Core: `core/computer_use.py` + Claude/Codex/OpenCode translation + prompt
- [ ] Workbench status line + i18n
- [ ] User docs: enabling, permissions, `require_bind` guidance, stop
- [ ] Windows parity

## Validation

- Rust: every toggle, permission, start, health, and stop path writes the
  matching `D` state, with a non-ready state before the daemon stops and
  `ready` only after its socket accepts; generation bumps on each spawn; no
  spawn through LaunchServices; toggle persistence.
- Python: the effective status for every `D` state, plus missing, malformed,
  dead-`shell_pid`, and unconnectable-socket files (only a live `ready` file
  injects); the watcher refreshes each embedding backend exactly once per
  (is-ready, `generation`) change and never on an unchanged poll; the prompt
  section appears only with the server. The Codex translation adds the stdio
  filter and the approval override, and the filter folds `structuredContent`
  into text while keeping image blocks. Tests stay hermetic: the `D` path is
  redirected to test-owned state.
- Shell: the daemon environment names the bundled managed policy, and a release
  check compares that policy against the pinned driver's `tools/list`.
- Manual on a signed build: Slack → agent → background GUI task completes while
  the user keeps working; the tray toggle stops an in-flight session's access.
