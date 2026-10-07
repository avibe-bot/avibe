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
so it does not move the user's pointer and normally leaves focus alone (see
Focus boundary). Its embedded mode exists
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

Avibe Runtime (adopted or shell-started; reads D, never spawns the daemon)
  └─ agent backend process
       └─ Avibe computer MCP server (stdio; stable for the backend's lifetime)
            └─ cua-driver mcp --embedded --socket <S>   ← per daemon generation
```

Only the shell process may spawn the daemon. The Runtime runs detached, may
be adopted from a terminal-started `vibe`, and keeps running after the window
closes. A daemon it spawned would take the TCC identity of whatever started the
Runtime, not Avibe.app. Cua's docs name this exact gateway wiring as wrong. The
MCP proxy never executes tools and needs no grant, so any process may spawn it.

Two things change at different rates, and the design keeps them apart:

- **Configuration:** whether sessions carry the `avibe_computer` server at all.
  It follows the user's toggle and changes only when the user flips it.
- **Availability:** whether a daemon answers right now. It changes with daemon
  restarts, crashes, grants, shell restarts, and the lock screen.

Availability never changes backend configuration. A stable Avibe-owned server
absorbs it at call time.

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
  menu, following the Start at Login pattern. It adds no webview command. Its
  only durable record is `D.enabled`. The shell keeps no second copy in its
  native preferences: it reads `D` at launch and writes `D` on every flip, so
  the next launch and the live Runtime can never disagree.
- **Runtime compatibility.** The shell adopts a running Runtime as-is, so after
  a desktop update that Runtime can be older than the shell.
  - `GET /ready` stays byte-for-byte unchanged, because released shells parse it
    with an exact key set (`runtime-host/src/health.rs`). A new `GET
    /api/desktop/capabilities` (under `/api/`, where an unknown route is a real
    404; elsewhere the UI's SPA fallback answers 200 with `index.html`) returns
    `computer_use_schema`, the newest `D` `schema_version` the Runtime can read.
    The UI process obtains it from the Controller over the existing internal IPC
    socket and does not derive it from its own package version. The Controller
    is what reads `D`, and `/ready` may pair a newer UI with an older
    Controller. Only a definitive answer means unsupported: a 404, or a schema
    below the one the shell writes. A Controller too old to know the capability
    operation answers the IPC call with its unknown-operation error. The UI maps
    that to `computer_use_schema: 0`, which is definitive. The answer also
    carries the Controller's `controller_id`, which is random per Controller
    process. The cached answer is keyed by that id, and it is cleared whenever
    the `/ready` monitor re-runs adoption. Only a cached answer from the same
    Controller is ever reused. A transport error, such as an IPC timeout or a
    reset, keeps that same-Controller answer. A transient failure therefore
    never stops a healthy daemon. Losing the Runtime itself is already handled
    by the `/ready` monitor. Any other non-definitive answer, such as a 500 or a
    malformed body, also keeps the last known answer. Before the first
    definitive answer, the shell spawns nothing and retries on the next `/ready`
    tick.
  - Turning the toggle on requires that value to cover the schema the shell
    writes. Otherwise the shell refuses to turn it on, and the menu text says
    the Avibe service must restart to support computer use.
  - Turning it off always works. A toggle that was already on stays on as
    `needs_runtime`, with no daemon, until a capabilities probe sees
    support. The shell re-probes on each successful `/ready` check. The
    checked item stays interactive in every state, so turning it off is
    always possible.
  - The shell never restarts an adopted Runtime itself. A pre-feature
    Runtime lacks the field and therefore reads as unsupported.
- **Permission requests (macOS).** The shell requests Accessibility with
  `AXIsProcessTrustedWithOptions` and a prompt. It requests Screen Recording
  with `CGRequestScreenCaptureAccess`, falling back to opening the Settings
  pane.
  - It prompts only on the user's toggle-on action. Every other check is
    silent, because each prompting call while a grant is missing queues
    another system dialog.
  - On macOS 26 an app appears in the Screen Recording pane only after a real
    capture attempt. So the request also makes one attempt from the shell
    process itself: a one-pixel ScreenCaptureKit capture whose result is
    discarded. TCC attributes it to Avibe.app, and it needs no daemon, which
    matters because the daemon starts only after both grants exist. In the
    spike the row appeared after the host's daemon attempted a capture, so
    verify the shell-side probe on macOS 26 in Phase 1.
- **Lifecycle.** The shell runs one state machine whose states are the `D`
  states. Every transition writes `D` first, except that `ready` is written
  only after the health check passes.
  - **Failure budget.** One rule, shared by every failure row. Each failed
    start and each failure from `ready` counts once. When the count reaches
    3 within 5 minutes, the state becomes `error` with the reason. Otherwise
    it becomes `starting` and respawns after backoff. A daemon that passes
    startup and then crashes therefore cannot restart forever.
  - **Failed writes.** A failed `D` write never blocks a stop. Toggle-off,
    quit, and revocation always stop the daemon. If their write failed, the
    menu says the setting was not saved, and the shell retries the write every
    5 s while it runs. A failed write never starts anything: a transition into
    `starting` that cannot write `D` goes to `error` with reason
    `state_unwritable`. A failed post-health `ready` write does the same: it
    stops and reaps the healthy daemon, then holds `error` /
    `state_unwritable` in memory and shows it in the menu.

  The first matching row wins:

  | From | Event | To | Action |
  | --- | --- | --- | --- |
  | any | toggle off | `off` | stop the daemon if running |
  | `off` | toggle on, Runtime lacks a covering `computer_use_schema` | `off` | refuse; the menu says the Avibe service must restart |
  | `off` | toggle on, both grants held | `starting` | spawn |
  | `off` | toggle on, a grant missing | `needs_permission` | prompt and run the capture probe (the only prompting path) |
  | shell launch | `D` unreadable, malformed, or of a newer `schema_version` | unchanged | spawn nothing and do not rewrite `D`; the menu warns. Only an explicit toggle action overwrites it |
  | shell launch | `enabled` and recorded `state` is `error` | `error` | none; no spawn |
  | shell launch | `D` missing or not `enabled` | `off` | if `D` exists, write `off` with this shell's `instance_id` |
  | shell launch | `enabled`, Runtime lacks a covering `computer_use_schema` | `needs_runtime` | none; no spawn |
  | shell launch | `enabled`, both grants held | `starting` | spawn |
  | shell launch | `enabled`, a grant missing | `needs_permission` | none; silent |
  | `needs_runtime` | capabilities now cover the schema, both grants held | `starting` | spawn |
  | `needs_runtime` | capabilities now cover the schema, a grant missing | `needs_permission` | none; silent |
    | any `enabled` state except `error` | a successful `/ready` is followed by an unsupported capabilities answer | `needs_runtime` | stop the daemon if running |
  | any `enabled` state except `error` | adoption found a new `controller_id` with no definitive supported answer yet | `needs_runtime` | stop the daemon if running |
  | `needs_permission` | grant check passes | `starting` | spawn |
  | `needs_permission` | app activation, grant check still fails, no fallback attempt yet in this activation | `starting` | spawn once (stale-preflight fallback) |
  | `starting` | socket accepts and the health check returns `pass` | `ready` | none |
  | `starting` | the health check returns `missing_grant` | `needs_permission` | stop the daemon; no prompt |
  | `starting` | spawn or socket fails, or the health check returns `unhealthy` | per the failure budget | stop the daemon; apply the failure budget |
  | `ready` | the health check returns `missing_grant` | `needs_permission` | stop the daemon |
  | `ready` | daemon exits unexpectedly, or its socket refuses or the health check returns `unhealthy` on 2 consecutive ticks | per the failure budget | stop the daemon if alive; apply the failure budget |
  | any `enabled` state except `error` | quit | `stopped` | stop the daemon |
  | `error` | quit | `error` | none; `error` is kept across relaunch |

  - **Grant check.** Silent, and it runs only while `enabled`. It fires on app
    activation, and every 5 s. In `needs_permission`, it uses the shell's own
    `AXIsProcessTrusted` and `CGPreflightScreenCaptureAccess`. In `ready`, it
    runs the daemon health check below instead. That process reads its grants
    fresh, so a stale shell preflight can never stop a daemon that the fallback
    just brought up. The same 5 s tick also probes the daemon's socket. A wedged
    daemon that is alive but not accepting is treated like one that exited.
  - **Stale preflight.** macOS caches TCC answers per process, so the
    shell's own preflight can stay false after the user grants access in
    System Settings. The fallback row covers that case.
    - A user who grants access returns to Avibe, which is an app activation.
      At most one attempt runs per activation.
    - A fresh daemon reads the grants anew. Its health check then reaches
      `ready`, or returns to `needs_permission` with no prompt.
    - A grant that is truly missing therefore costs one short-lived daemon
      per activation, never a loop.
  - **Support invariant.** Every row that can spawn or prompt sits behind a
    supported capabilities answer. So any enabled state other than
    `needs_runtime` implies that the latest probe said supported. An
    unsupported answer always leads to `needs_runtime` with no daemon.
  - **No indirection.** Every row names its target state and action. `error`
    leaves only through toggle off (the first row), and a later toggle on
    starts from `off`.
  - **Health check.** One composite operation with one 5 s deadline in total.
    It runs in two modes. The full mode runs on every start (`starting`) and
    includes step 4. The light mode is the `ready` heartbeat every 5 s and
    skips step 4, so an idle `ready` daemon never captures the desktop. A
    capture failure later surfaces in the agent's own tool results.
    It returns exactly one of `pass`, `missing_grant`, or `unhealthy`, and
    the lifecycle rows refer only to those three outcomes. A check added
    later therefore needs no new row. On macOS, the check runs these steps:
    1. Call `health_report` and read its `checks` array.
    2. If `tcc_accessibility` or `tcc_screen_recording` fails, return
       `missing_grant`.
    3. Require `bundle_identity` to pass with `identity_source:
       parent_application`, which is the host-attribution evidence of Phase
       0 Q1. Also require `ax_capability` to pass.
    4. (Full mode only.) Call `get_desktop_state` with `max_image_dimension:
       64`, and require an image. `health_report` is read-only and skips
       `screen_capture_capability` (Phase 0 output), so capture is proven
       this way.

    Any other failure, an error, or the deadline expiring returns
    `unhealthy`, with the failing step as the reason. `source.attribution`
    belongs to `check_permissions`, which the policy excludes, so it is
    never read. Windows emits different checks (`ax_capability`,
    `screen_capture_capability`), so the Windows Q6 run sets the steps there.
    The menu shows the localized reason for `needs_permission` and
    `error`.
  - **Orderly quit.** Write `stopped`, then stop the daemon. `D` keeps
    `enabled`, and the effective-status table reads `stopped` as
    `shell_not_running`, the same as a released shell lock after a crash.
- **Daemon.** Spawn directly with `Command`, never through
  `open`/LaunchServices. Mirror upstream `EmbeddedCuaDriverHost` (in
  `cua-driver-sdk/src/embedded.rs`) at the pinned tag.
  - Arguments: `serve --embedded --parent-liveness-stdio
    --no-permissions-gate --socket <S> --host-bundle-id <bundle id>
    --permission-mode standard`.
  - Environment: start from an empty environment, then set
    `CUA_DRIVER_EMBEDDED=1`, `CUA_DRIVER_EMBEDDED_HOST_PID=<shell pid>`,
    `CUA_DRIVER_MANAGED_POLICY_FILE=<bundled tool policy>`,
    `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, and `CUA_DRIVER_RS_UPDATE_CHECK=0`.
    `CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS` stays unset, so the driver keeps
    its 1000 ms post-action watch and the focus-steal lease that comes with
    it (see Latency).
  - Avibe does not depend on that crate. It is unpublished and pulls in the
    whole platform stack, while the protocol needs only these few lines.
  - **Liveness.** The shell keeps the child's stdin pipe open for the daemon's
    lifetime and never writes to it. When the shell dies, the OS closes the pipe
    and the daemon exits, so a crash leaves no daemon holding Avibe's grants. An
    orderly stop closes the pipe and waits up to 3 s for exit. It then
    terminates the process (`SIGKILL`, or `TerminateProcess` on Windows) and
    reaps it. So a daemon that ignores EOF can never hold a stop open or block
    the next spawn. The spike host did not pass `--parent-liveness-stdio`; Phase
    1 tests this with a killed shell.
  - **Endpoint.** The socket lives in the shell's app data directory (see
    `D`). On Windows it is a private pipe name. A dead daemon leaves its socket
    file behind, and a new daemon refuses an existing endpoint. So before each
    spawn the shell probes the socket. If it still accepts, the shell waits up
    to 2 s for an orphan to see its closed pipe. It then removes the file, or
    goes to `error` with reason `endpoint_busy` if the socket still accepts.
- **State file `D`.** `computer-use.json` in the shell's per-user app data
  directory is the only shell-to-Runtime contract. On macOS that directory is
  `~/Library/Application Support/bot.avibe.desktop/`, where `bootstrap.log`
  already lives. On Windows it is `%APPDATA%\bot.avibe.desktop\`. The shell
  writes it atomically on every change. It is also the only cross-process
  record: the Runtime, the computer MCP server, and the separately running
  Workbench API process all read the same file.
  - Every write carries `schema_version`, `enabled` (the toggle's only durable
    record and the configuration input), `state`, `reason` (a stable code, or
    null), `shell_pid`, `instance_id` (random per shell process),
    `generation`, `driver_version`, and `tool_snapshot` (the absolute path
    and SHA-256 of the tool snapshot bundled beside the driver).
    `state` is one of these:
    - `off`: the toggle is off.
    - `needs_permission`: the toggle is on and a grant is missing.
    - `starting`: the toggle is on, both grants are held, and the daemon is
      not yet accepting connections.
    - `ready`: the socket accepts connections.
    - `error`: start or health failed for a reason other than a missing
      grant.
    - `stopped`: the toggle is on and the shell is quitting or has quit.
    - `needs_runtime`: the toggle is on, but the adopted Runtime cannot read
      this schema. No daemon runs.
  - `ready` also carries `socket_path`, `proxy_executable` (absolute path to
    the bundled binary) and `host_bundle_id`.
  - `generation` increases with each daemon spawn within one `instance_id`.
    The pair identifies a daemon across shell restarts.
  - The shell writes a non-ready state before stopping the daemon (`off` for
    toggle-off, `stopped` for quit). It never deletes the file, so `enabled`
    survives quits and restarts. A missing file means the user never enabled
    the feature.
  - **Shell liveness** is a lock, not a PID. Each shell holds an exclusive
    lock on `computer-use.lock`, next to `D`, for its whole lifetime: `flock`
    on macOS, `LockFileEx` on Windows. The OS releases it when the shell
    dies, even on power loss. A reader that can take the lock knows no shell
    is running, so a reused PID cannot fake liveness. `shell_pid` stays in
    `D` for diagnostics only.
  - The location belongs to the desktop shell, of which each OS user has
    one. It does not belong to the Avibe home. So every Runtime finds `D`
    without seeing the shell's launch environment, whatever home it
    resolves: `AVIBE_HOME`, the default `~/.avibe`, or a legacy-only
    `~/.vibe_remote`. `core/computer_use.py` resolves the same per-platform
    path, and tests redirect it.
- **Tool policy.** Ship a YAML allow-list as the driver's managed policy
  (Phase 0 finding). It is pinned with the driver version, because the list
  must be reviewed against each new tool surface. The v1 list has exactly 28
  tools, and the bundled tool snapshot must equal it:
  - Observation: `list_apps`, `list_windows`, `get_window_state`,
    `verify_state`, `get_accessibility_tree`, `get_screen_size`,
    `get_desktop_state`, `get_cursor_position`, `zoom`.
  - Apps and windows: `launch_app`, `kill_app`, `set_window_frame`.
  - Input: `click`, `double_click`, `right_click`, `drag`, `scroll`,
    `type_text`, `press_key`, `hotkey`, `set_value`.
  - Clipboard: `clipboard_read`, `clipboard_write`.
  - Sessions: `start_session`, `end_session`, `get_session`,
    `list_sessions`.
      - Diagnostics: `health_report`.
  - `check_permissions` is excluded. Its `prompt: true` raises system
    dialogs, and only the native toggle-on action may prompt. The policy
    applies to every caller, the shell included, so the shell's health check
    uses `health_report` instead.
  - `bring_to_front`, `move_cursor`, and `invoke_menu` are excluded. The first
    persistently takes the foreground, and the second can move the user's real
    pointer. `invoke_menu` temporarily activates the target and restores the
    previous app only on a best-effort basis. Activation is the purpose of all
    three, so they are removed.
  - **Focus boundary (macOS).** Avibe cannot promise that no background
    input ever takes focus. An app can activate itself in response to any
    event, and the driver's guide (`MACOS.md` at the pinned tag, "The
    no-foreground contract") names shortcuts that do. So the boundary is
    what Avibe enforces:
    - No tool exists whose purpose is activation.
    - Foreground delivery is rejected, and input must address one window
      (below).
    - The driver keeps its 1000 ms focus-steal lease, which reverts an
      activation seen during the post-action watch.
    - As defense in depth, the server rejects the focus-intent shortcuts
      that guide names: `⌘L`, `⌘⇧G`, and the browser tab shortcuts `⌘1`
      to `⌘9`, `⌘[`, `⌘]`, `⌘⇧[`, and `⌘⇧]`. The prompt says to open a URL
      with `launch_app` and `urls` instead.

    What remains is that an app may still raise itself after background
    input. The idle-desktop validation measures this, and the user docs
    state it. The release check re-reads that guide section for every new
    driver version.
  - **Window-only input (macOS).** The computer MCP server admits an input
    call only when it addresses one window: a `pid` with a `window_id`, or an
    element token. It rejects any other targeting, including a desktop target
    (`target.kind: "desktop"` or `scope: "desktop"`), `delivery_mode:
    "foreground"`, and anything it does not recognise. This is an allow rule, so
    a targeting option added in a later driver is refused until it is reviewed.
    The Windows Q6 run decides the Windows policy, where some surfaces work only
    in the foreground.

  It omits config, update,
  extension, recording/replay, cursor-theme, legacy `page`, the typed browser
  tools, and visual parsing. Typed browser tools are deferred, not rejected:
  they need their own runtime and origin scope.

### Runtime (`core/`, `modules/agents/`)

- **One owner.** A new `core/computer_use.py` owns both reads of `D`.
  - **Configuration.** It returns the single managed stdio MCP spec (name
    `avibe_computer`, launching Avibe's computer MCP server with Avibe's Python)
    only when `enabled` is true and `D.tool_snapshot` exists with a matching
    SHA-256. Otherwise it returns nothing. A failed snapshot check logs one
    warning per distinct hash, and the status reads `unavailable` /
    `snapshot_invalid`. An optional feature with a broken bundle is never
    injected, and the rest of the agent keeps working. Backends only
    translate this spec.
  - **Effective status.** One total derivation, where the first matching row
    wins. The server and the Workbench both use this one reader.

    | `D` | Status | Reason |
    | --- | --- | --- |
    | missing | `off` | `never_enabled` |
    | unreadable, malformed, or unknown `schema_version` | `unavailable` | `invalid_state_file` |
    | `enabled` is false | `off` | `toggle_off` |
    | `tool_snapshot` missing or hash mismatch | `unavailable` | `snapshot_invalid` |
    | shell lock not held, or `state` is `stopped` | `unavailable` | `shell_not_running` |
    | `state` other than `ready` | that state | its recorded `reason` |
    | `ready`, socket refuses a connection | `unavailable` | `daemon_unreachable` |
    | `ready`, socket accepts | `ready` | null |

    Configuration reads only `enabled` and `tool_snapshot`. A missing or invalid
    file counts as not enabled. Atomic writes keep a half-written file from
    appearing.
- **Computer MCP server.** A small Avibe-owned stdio server is the one stable
  endpoint for every backend.
  - It starts without a daemon. It serves the tool list from the snapshot
    that `D.tool_snapshot` names, and checks the hash. The shell bundles that
    snapshot with the driver and the policy, so the tools always match the
    driver the shell runs, whatever version the Runtime is.
  - On each call it reads the effective status. If it is `ready`, the server
    forwards the call through an upstream `cua-driver mcp --embedded --socket
    <S>` child. It respawns that child when the (`instance_id`, `generation`)
    pair changes or the child has exited. The child gets
    `CUA_DRIVER_EMBEDDED=1`, `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`, and
    `CUA_DRIVER_RS_UPDATE_CHECK=0`; without the last, the proxy runs its own
    update check and prints a banner.
  - Otherwise it returns an error result that names the state and reason
    (off, needs permission, starting, error, unavailable). The agent can then
    tell the user what to do.
  - After a respawn, element tokens from the old daemon are invalid. Its
    error tells the agent to observe again.
  - It moves `structuredContent` into a trailing text block and drops
    `outputSchema` for every backend. Codex needs this (Phase 0, Q4), and it
    costs the other backends nothing.
  - **Concurrent sessions.** Claude spawns one server per session. Codex
    and OpenCode share one server across their conversations, so a caller
    cannot be identified by process. Isolation uses the Avibe session id
    that every session prompt already carries
    (`core/prompts/session-start.md`), and the server enforces it:
    - The server adds a required `session` string to every advertised
      tool schema, so schema-driven clients always send it. The injected
      prompt says to fill it with the session id.
    - The server calls `start_session` for a session's name before forwarding in
      four cases: its first call, its first call after an `end_session`, its
      first call after the daemon key (`instance_id`, `generation`) changes, and
      its first call after the server replaced its upstream proxy child. Closing
      that transport ends its sessions in Cua. That call creates or revives the
      session. Agents never manage the Cua session lifecycle.
    - Calls from one session run one at a time, and the server queues the
      rest. So a session never has two calls in flight. An `end_session`
      releases the lease only after the calls queued before it have
      finished.
    - The server forwards the id as the named Cua session. Element tokens
      and session state are therefore per Avibe session, even on one shared
      upstream connection.
  - **Desktop lease.** There is one desktop, so only one session acts at a
    time.
    - Acquisition, refresh, and release run under an exclusive OS lock on
      `computer-lease.lock`, next to `D` (`flock` on macOS, `LockFileEx` on
      Windows). Inside that lock the server reads and writes the lease
      record: holder session id, last refresh time, a lease `epoch` that
      increases with each new holder, and the daemon key (`instance_id`,
      `generation`) it was taken under.
    - Two first calls from different processes therefore serialize. One
      wins, and the other gets `desktop_busy` naming the holder.
    - The holder refreshes the lease when each call starts, and every 10 s
      while a call is still running. A long call, such as a 120 s
      `get_accessibility_tree`, therefore keeps it.
    - The lease lapses 60 s after the last refresh. A holder that crashed,
      even mid-call, stops refreshing and frees the desktop within a minute,
      without any PID check. Calling `end_session` releases it at once.
    - A lease whose daemon key differs from `D`'s current one is void. A
      native stop, a toggle off and on, or a respawn therefore clears every
      lease without waiting out the 60 s.
    - Each session remembers the epoch it last held. Another session may have
      held the desktop in between, so a session can reacquire the lease under a
      newer epoch. Before forwarding that call, the server runs `end_session`
      and then `start_session` for its name. Cua then invalidates every element
      token the session held, so a stale token fails with Cua's own error.
      Coordinates from an old screenshot are not covered by that, so the server
      tracks `observe_first` per target window. After an epoch change, the
      server refuses input to a window (`pid`, `window_id`) with `observe_first`
      until that session has called `get_window_state` or `zoom` on that same
      window. Observing window A therefore never clears input to window B, and
      calls such as `get_desktop_state` or `get_cursor_position` clear nothing.
      A server process that has no record of a session, for example after the
      backend restarted it, starts that session with every window requiring
      observation. Nothing has to persist across server processes.
  - This isolation is cooperative. Every caller is the same user's agent;
    it is not a security boundary.
  - Tool restriction stays in the driver's managed policy, not in this server.
  - The proxy exits with its daemon and never reconnects (Phase 0, Q3), so
    this server is what keeps one endpoint alive per backend lifetime.
- **Claude Code.** Pass `mcp_servers={"avibe_computer": ...}` in the session
  options built in `core/handlers/session_handler.py`. SDK 0.2.93 supports
  `McpStdioServerConfig`. Keep `strict_mcp_config` unset so user MCP config is
  preserved. An agent file that sets `tools` must list `mcp__avibe_computer__*`
  to keep access.
- **Codex.** Append `-c mcp_servers.avibe_computer.*` overrides to the
  app-server launch, after Avibe's fixed overrides.
  `features.computer_use=false` stays: Avibe supplies one cross-backend tool
  instead of Codex's own. Also set
  `mcp_servers.avibe_computer.default_tools_approval_mode="approve"` (Phase 0,
  Q4). Avibe runs Codex with `approvalPolicy: never`, and Codex cancels every
  non-read-only MCP call under that policy.
- **OpenCode.** Add `mcp.avibe_computer` (`type: "local"`) through the existing
  config overlay / `PATCH /global/config` path.
- **Why the server follows the toggle.** The allowed tool schemas measured
  about 70 KB, roughly 17.6k tokens per session (on the 32-tool draft, which
  also included `check_permissions`). Every session of a
  user who never enabled the feature would pay that, so the server is not
  injected unconditionally. Only availability is absorbed at call time.
- **Configuration reconciliation.** `core/computer_use.py` owns the one change
  that alters backend configuration. A controller background task reads
  `enabled`, the `tool_snapshot` hash, and whether the snapshot file currently
  verifies, every 2 s. This is the same polling model as
  `RuntimeCommandWatcher`. The hash is recomputed only when the file's size or
  mtime changes. The task compares this triple with the triple each live
  consumer was built from. A deleted, corrupted, or restored snapshot therefore
  reconciles even when the expected hash in `D` is unchanged. A desktop update
  that ships a new driver therefore reaches running sessions the same way a
  toggle flip does. On a mismatch it reconciles every consumer that caches MCP
  configuration:
  - Codex and OpenCode: the same `AgentAuthService._refresh_backend_runtime`
    handler that restart markers call.
  - Cached Claude clients: marked stale, then retired and recreated at their
    next turn boundary, never mid-turn.

  The comparison is against the final value, so a flip and flip-back between
  polls correctly needs nothing. Availability changes never reach this path.
  The stop guarantee does not depend on it either: turning the toggle off
  stops the daemon at once, so calls fail with `off` before reconciliation
  removes the tools.
- **Prompt.** Add a short section in `core/system_prompt_injection.py`, only
  when the server is configured. It says the tools can report a state such as
  `off` or `needs_permission`, and then the agent tells the user instead of
  retrying. It says to fill the required `session` field with the session id. It
  says `desktop_busy` means another session holds the desktop, so the agent
  waits or tells the user, and to call `end_session` when a GUI task is done. It
  also says to prefer CLI/API routes, use GUI tools for GUI-only steps, prefer
  accessibility element-token actions over pixel input, treat screen content as
  untrusted, and observe state before retrying any action, since an error result
  does not prove the action failed. If every window comes back AX-unresolved and
  the desktop shows no apps, the screen is likely locked: stop and tell the
  user. On macOS, it never uses focus-intent shortcuts (open URLs with
  `launch_app` and `urls`), and no tool may deliberately take focus or move the
  pointer; if
  only a foreground action would work, the agent tells the user. On Windows, the
  Q6 run decides foreground use.

### Workbench

A read-only status line, derived from the effective status: ready, off, needs
permission, needs a newer Runtime (with "restart the Avibe service"),
starting, error with its reason, or unavailable with its reason. Validation
covers one rendering per status. The Workbench API reads it through the same
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
  fresh proxy works against the restarted daemon. So no backend may hold the
  raw proxy as its MCP server. The Avibe computer MCP server holds it and
  respawns it per daemon generation.
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
  the default permissions. So only the approval override is Codex-specific.
  Folding `structuredContent` is harmless here, so the shared server does it
  for every backend.

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
  config, install extensions (`cua-perception` is AGPL), or update the driver. A
  capability manifest does not fit general computer use: it is deny by default
  for resources too, so every app would have to be listed by bundle id. The
  driver's YAML tool policy only names tools. Set as
  `CUA_DRIVER_MANAGED_POLICY_FILE` on the embedded daemon, a 30-tool draft
  allow-list shrank `tools/list` to 30 through the proxy. The final v1 list (see
  Tool policy) adds `health_report` and removes `bring_to_front`, `move_cursor`,
  and `invoke_menu`, giving 28. A 32-tool draft that also allowed
  `check_permissions` listed exactly its 32 tools under the policy. Omitted
  tools returned `permission_denied`, and allowed ones worked. Agent-side
  environment on the proxy could not widen it: a widening user policy, a
  widening managed policy, and `unrestricted` mode variables each left the
  daemon's surface unchanged. `kill_app` stays allowed, because standard mode
  only terminates processes this runtime launched.
- **End to end.** `launch_app` started Calculator in the background
  (`self_activation_suppressed: true`). `get_window_state` returned a PNG plus a
  154-element AX tree in about 1.9 s. Five AX `click`s by `element_token` gave
  `6×7 = 42`, read back from the AX tree.
- **Latency.** After each action the driver watches for window changes for up to
  1000 ms by default, and the daemon reads that bound from
  `CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS` (0 to 10000). An A/B on embedded
  background Calculator AX clicks ran 2 rounds of six clicks per setting. 1000
  ms gave about 2.3 s per click, 300 ms about 1.6 s, and 0 ms about 1.35 s.
  Every run computed `12×3 = 36` correctly with no errors. A no-op call through
  the proxy takes 1 to 2 ms, so the remaining 1.3 s floor sits inside
  Calculator's AXPress (the slow `-25204` path, trycua/cua#3836). The watch also
  holds the driver's focus-steal lease, which reverts another app's activation
  while it runs. Upstream warns that shortening it weakens that protection. So
  v1 keeps the 1000 ms default and gives up the measured 0.7 s per action. Focus
  protection outweighs latency for unattended use, and latency tuning waits for
  a measured need. Even at 1000 ms, an app that activates itself later than the
  watch can stay frontmost. The idle-desktop validation measures this, and the
  user docs state it. Upstream measured 78 ms at `0`, so the floor depends on
  how fast the target app answers. Click results report `effect: unverifiable`
  either way, so agents observe after acting.
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
- [ ] Shell: sidecar packaging, pinned manifest, tool policy and tool snapshot,
  signing order, notices
- [ ] Shell: toggle, permission flow, daemon lifecycle, state file `D`, health
- [ ] Core: `core/computer_use.py`, the computer MCP server, configuration
  reconciliation, Claude/Codex/OpenCode translation, prompt
- [ ] Workbench status line + i18n
- [ ] User docs: enabling, permissions, `require_bind` guidance, stop
- [ ] Signed build, idle desktop: the agent cursor overlay stays visible, and
  representative AX and pixel actions neither take focus nor move the user's
  pointer (redoes the confounded Phase 0 check)
- [ ] Windows parity

## Validation

Each case lives in the suite of the component that owns the behavior.

- **Rust, shell lifecycle (`desktop/`)**, with grant checks, spawn, socket,
  health, `/ready`, and capabilities faked. These cases cover:
  - One case per lifecycle-table row. Each asserts the resulting `D` and
    that no prompt is raised outside the toggle-on row.
  - Stale preflight: a grant the shell's preflight misses reaches `ready`
    after one activation. A truly missing grant spawns once per activation.
    A stale shell preflight does not stop a `ready` daemon.
  - Health check: the full mode runs at start and includes the capture
    probe. The `ready` heartbeat never captures. A step that hangs or fails
    returns `unhealthy` in both modes.
  - Start failures, on a first start or a respawn, retry with backoff.
    They reach `error` only on the third failure within 5 minutes. A daemon
    that passes startup health and then crashes from `ready` each time also
    reaches `error` on its third failure.
  - Capabilities: a transport error or a 500 keeps the last answer, but
    only for the same `controller_id`. Only a 404 or a lower schema means
    unsupported. A new `controller_id` with no definitive answer stops a
    running daemon. A Runtime without a covering `computer_use_schema`
    never gets a daemon. Turning the toggle on is refused there, while
    turning it off still works.
  - Process: a daemon that ignores EOF is killed after 3 s. A killed shell
    takes its daemon down and releases its lock, and the next launch
    reclaims the endpoint. `generation` bumps on each spawn, and nothing is
    spawned through LaunchServices.
  - State file: a failed `D` write still stops the daemon, and the toggle
    persists only in `D`.
- **Release check (shell build)**, covering three points:
  - The daemon environment names the bundled managed policy.
  - The pinned driver's `tools/list`, under that policy, equals the bundled
    tool snapshot.
  - The driver guide's no-foreground section is re-read for the new driver
    version.
- **Python, UI process (`vibe/`)**, covering these cases:
  - `/ready` is byte-for-byte unchanged for released shells.
  - `/api/desktop/capabilities` answers from the Controller over IPC.
  - An old Controller's unknown-operation error reads as schema `0`.
- **Python, Runtime configuration (`core/computer_use.py`)**, covering these
  cases:
  - One case per row of the effective-status table, asserting both status
    and reason.
  - A Runtime with `AVIBE_HOME` set reads the same `D`.
  - The spec and the prompt section exist exactly when `enabled` is true
    and the snapshot verifies. A missing or mismatched snapshot yields
    neither, plus `snapshot_invalid`.
  - Each backend translation is checked; Codex also carries the approval
    override.
  - Reconciliation brings every live consumer (Codex, OpenCode, cached
    Claude clients) to the final (`enabled`, snapshot hash, snapshot valid)
    triple. Claude clients are recreated only between turns.
  - Availability changes, and a triple that returns to its old value
    between polls, cause no reconciliation. Deleting and then restoring the
    snapshot file reconciles both ways.
- **Python, computer MCP server**, with the upstream command faked. These
  cases cover:
  - Upstream: a call while not ready returns the named state and never
    spawns a child. An (`instance_id`, `generation`) change or a child exit
    respawns exactly once. Folding keeps image blocks.
  - Sessions: every advertised schema requires `session`. Two sessions get
    separate Cua sessions on one upstream connection. A session is revived
    before its next call after `end_session`, after a daemon respawn, and
    after a child replacement. A session's calls run one at a time, and
    `end_session` waits for the calls queued before it.
  - Lease: simultaneous first calls from two processes yield exactly one
    holder, and the other gets `desktop_busy` until `end_session` or 60 s
    idle. A 120 s call keeps its lease. A stop and re-enable voids every
    lease.
  - Stale input: after an epoch change, old element tokens are rejected,
    and input to a window gets `observe_first` until that same window is
    observed. Observing window A does not clear window B. A new server
    process requires an observation before a session's first input.
  - macOS input: a desktop-targeted, foreground, or unrecognised input
    call is rejected, and so are the guide's focus-intent shortcuts.
- **Manual, on a signed build.**
  - Slack → agent → a background GUI task completes while the user keeps
    working, and the tray toggle stops an in-flight session's access.
  - On an idle desktop, sample the frontmost app and the pointer during AX
    and pixel actions, and confirm that the agent cursor overlay stays
    visible.

All automated cases are hermetic: the `D` path and the upstream command are
redirected to test-owned fakes.
