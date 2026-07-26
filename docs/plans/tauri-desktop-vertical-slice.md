# Tauri Desktop Vertical Slice

## Status

- Owner: Avibe core
- Target platforms: macOS and native Windows
- Mobile: deferred until the desktop shell and shared WebView constraints are proven
- Delivery strategy: a retained product scaffold, not a disposable prototype
- Integration branch: every desktop PR targets `desktop`; only an
  owner-authorized final integration PR may target `master`

The first credible native Windows closure is intentionally narrow:

> Windows x64 + uv-managed system Python + Tauri WebView2 + Codex only + a
> native `C:\...` workspace + authenticated loopback Controller IPC + Windows
> ProcessHost + pywinpty/ConPTY.

Vault, Show Runtime, askill, OpenCode, Claude, ARM64, private Python, NSIS, and
signing remain product requirements, but none blocks the first D07/D12 proof.

## Background

Avibe already has the right high-level split for a desktop application:

- the React Workbench is the primary user interface;
- the Python service owns agents, sessions, local state, Show Pages, Vault
  orchestration, terminal transport, and IM integrations;
- the browser talks to the service over loopback HTTP, SSE, and WebSocket.

The first desktop release should preserve this split. Tauri should own native
application lifecycle and distribution, while the existing Avibe service
remains the single business-logic and data authority.

Windows is a first-class native target. WSL remains a compatibility option, not
the architecture or the default path.

## Goals

1. Prove that the complete Workbench runs reliably inside Tauri on macOS
   WKWebView and Windows WebView2.
2. Start or adopt one local Avibe Runtime without creating duplicate daemons.
3. Keep long-running Avibe work independent from the desktop window lifecycle.
4. Make native Windows capable of running a real agent session without
   invoking WSL.
5. Preserve Show Pages, Vault, Monaco, xterm, files, clipboard, drag and drop,
   HTTP streaming, and WebSocket behavior.
6. Keep the shell replaceable: if Tauri fails the gate, Electron can replace
   the native shell without rewriting Workbench or Runtime.

## Non-Goals

- Rewriting Workbench in Rust, Swift, Kotlin, or native widgets.
- Moving business logic or persistent data into Tauri commands.
- Bundling a private Python Runtime in the first host-capability milestone.
- Implementing auto-update before startup, lifecycle, and packaging are stable.
- Removing or weakening Show Pages to reduce WebView surface area.
- Delivering mobile applications in this milestone.
- Reproducing every tmux persistence feature on Windows before ConPTY is proven.
- Making Vault, Show Runtime, askill, OpenCode, Claude, ARM64, private Python,
  NSIS, or signing prerequisites for the first native Windows Agent/terminal
  closure.

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│ Tauri Desktop Shell                                         │
│                                                             │
│  bootstrap/error UI   window/tray   single instance         │
│  Runtime discovery    process start native integration      │
└──────────────────────────────┬──────────────────────────────┘
                               │ loopback URL
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Existing React Workbench                                    │
│                                                             │
│ Chat  Sessions  Show Pages  Vault  Monaco  xterm  Settings  │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP + SSE + WebSocket
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Avibe Python Runtime                                        │
│                                                             │
│ Agents  Harness  state  files  terminal  IM  Show Runtime   │
└─────────────────────────────────────────────────────────────┘
```

### Ownership Rules

| Concern | Owner | Contract |
| --- | --- | --- |
| Native window, tray, single instance | Tauri shell | No business state |
| Runtime discovery and launch | Tauri shell | Probe first, start second, adopt after health |
| Workbench UI and navigation | React Workbench | Browser-compatible HTTP origin |
| Sessions, agents, tasks, watches | Python Runtime | Existing APIs and durable state |
| Show Pages | Python and Show Runtime | Full capability preserved |
| Terminal bytes and resize events | Runtime platform adapter | Existing WebSocket protocol |
| Credentials and custody | Vault / platform adapter | Tauri never receives secret values |
| Long-running background work | Python Runtime / Harness | Survives window close and shell crash |

### Security Boundary

The Workbench loads from the configured loopback Avibe origin. Tauri commands
are available only to the bootstrap shell code that needs native lifecycle
operations. Normal Workbench routes and Show Page content must not inherit
filesystem, shell, process, or unrestricted Tauri capabilities.

This boundary preserves Show Pages as a core feature without turning page
content into native application code.

M1 uses one WebView in two phases:

1. the bundled Tauri-origin bootstrap page may call the fixed, argument-free
   Runtime bootstrap command;
2. after readiness, Rust navigates that WebView to the loopback Workbench.

The loopback origin is never listed in a Tauri capability `remote.urls` rule.
Every application command also verifies that its caller is still on the bundled
bootstrap origin. Single-instance, focus, window, and process lifecycle remain
Rust-owned after navigation; Workbench does not need Tauri IPC.

The Python server's existing Host-header validation is a frozen security
invariant: requests that do not name a syntactic loopback host must be rejected.
The desktop shell navigates only to the exact literal IP address whose health
probe succeeded, never the hostname `localhost`.

Show Pages keep their existing product capability. D04 must include a negative
assertion that Show Page JavaScript cannot invoke a Tauri command. Existing
browser-side SSE and public Show HMR origin hardening are tracked as server
security follow-ups; they must not be "solved" by disabling Show Pages.

### Runtime Discovery

M1 assumes an installed `vibe` executable and resolves it in this order:

1. an explicit development/test executable override;
2. the process `PATH`;
3. the standard uv tool bin directory under the user's home;
4. a platform-appropriate Python scripts directory where applicable.

Failure produces a retryable bootstrap error with an install action; it never
falls back to a shell command. A macOS application bundle cannot assume the
interactive shell's rc files were loaded.

`vibe start --no-open-browser` is a short-lived launcher that starts or adopts
the background service and UI processes, then exits. Tauri does not retain or
kill that launcher or the daemons it creates.

## Frozen Desktop Bootstrap Contract

The first implementation uses a small state machine:

```text
probing -> ready
       -> starting -> ready
                   -> failed
probing -> failed
```

The shell produces these states:

| Field | Producer | Consumer | Meaning |
| --- | --- | --- | --- |
| `phase` | Rust Runtime host | Bootstrap UI | `probing`, `starting`, `ready`, or `failed` |
| `origin` | Rust Runtime host | WebView navigation | Validated loopback HTTP origin |
| `attempt` | Rust Runtime host | Bootstrap UI | Current readiness probe attempt |
| `message` | Rust Runtime host | Bootstrap UI | Non-secret diagnostic summary |
| `retryable` | Rust Runtime host | Bootstrap UI | Whether the user may retry safely |

Behavior:

1. Resolve the origin from an explicit desktop override or the Avibe default.
2. Accept only literal `http://127.0.0.1` or `http://[::1]` loopback origins
   during this milestone.
3. Probe `GET /health`.
4. If healthy, adopt the existing Runtime and navigate to Workbench.
5. If absent, launch `vibe start --no-open-browser` without a shell.
6. Poll health with a bounded timeout.
7. Navigate only after a successful health response.
8. Never kill an adopted Runtime when a window closes or the shell exits.
9. Never expose command strings, environment variables, or secret-bearing
   process output to the WebView.

The initial shell assumes `vibe` is installed. Private Runtime bundling belongs
to the distribution milestone.

## Native Windows Runtime Contract

Windows support must be expressed behind platform interfaces rather than
scattered `os.name` branches. `core/platforms/__init__.py` selects an
implementation once; callers depend only on the narrow protocols:

```text
ControlIpcHost
├── PosixUnixSocketHost
└── WindowsLoopbackHost

ProcessHost
├── PosixProcessHost
└── WindowsProcessHost

TerminalHost
├── PosixPtyTmuxHost
└── WindowsConPtyHost

CredentialIpcHost
├── PosixSocketHost
└── WindowsNamedPipeHost

DependencyHost
├── PosixDependencyHost
└── WindowsDependencyHost
```

The interfaces preserve current product behavior while allowing different
platform mechanisms:

- Controller IPC keeps its current HTTP/SSE behavior. POSIX retains the Unix
  domain socket; Windows uses an ephemeral loopback listener with an atomically
  written endpoint descriptor, instance nonce, and bearer credential. It does
  not invent a custom named-pipe HTTP stack.
- `ProcessHost` models three explicit ownership classes:
  `DAEMON`, `OWNED_TREE`, and `ADOPTABLE_DAEMON`. The Runtime service is
  independent from Tauri; service-owned workers enter a Windows Job Object only
  after the service owns their lifecycle. Graceful control shutdown precedes
  forced whole-tree termination.
- daemon launch never passes POSIX-only `start_new_session` semantics on
  Windows; Controller internal-server startup failure must make the service
  unhealthy, and UI restart treats `WSAEADDRINUSE` (`10048`) as the Windows
  equivalent of the existing POSIX address-in-use errors.
- Windows terminal sessions use ConPTY through a maintained binding and retain
  the existing xterm WebSocket protocol. `TerminalService` continues to own
  session state; the host owns only `open`, `read`, `write`, `resize`, `wait`,
  and `close`.
- Process stop/restart uses native process-tree semantics and never calls
  `wsl.exe`.
- Vault credential IPC uses a Windows named-pipe byte stream only after the
  matching avault CLI transport contract exists. Avibe must not invent one
  unilaterally.
- dependency installation resolves native PowerShell and Windows assets;
  POSIX-only dependencies such as tmux return `not_applicable` on Windows
  rather than failing startup.
- paths are native Windows paths end to end; `/mnt/c` translation is forbidden.

## Milestones

### M1: Tauri Host Capability

Deliver a buildable Tauri v2 shell that:

- probes and adopts an existing Runtime;
- launches an installed Runtime when absent;
- shows a compact startup/error state with retry;
- navigates the main WebView to Workbench after readiness;
- prevents duplicate shell instances;
- keeps Runtime lifecycle independent from window lifecycle;
- includes unit tests for origin validation and state transitions;
- builds on macOS and Windows CI.

No updater or bundled Python is required.

### M2: Native Windows Runtime Slice

The first native target is Windows x64 with a uv-managed system Python and the
Codex backend. ARM64, bundled Python, Vault, Show Runtime, askill, OpenCode, and
Claude do not block the first D07/D12 proof. They remain required product
follow-ups; they are not removed or weakened.

Deliver the slice through six ordered contracts. A PR must not combine adjacent
contracts merely because they share the Windows target:

1. replace the Controller's fixed Unix-socket assumption with `ControlIpcHost`
   while preserving the existing HTTP routes, SSE ordering, and authentication;
2. add Windows process ownership, graceful stop, and whole-tree cleanup without
   making Tauri the owner of the Runtime process tree;
3. complete one real Codex session in a native `C:\...` workspace, including
   `.cmd`/`.exe` argument fidelity, streamed output, a file modification, and
   no `wsl.exe` or `/mnt/` path in the process lineage;
4. extract a `TerminalHost` conformance seam around the existing POSIX PTY/tmux
   behavior without changing the WebSocket or `TerminalView` contract;
5. implement the same contract on Windows with
   `pywinpty>=3.0.5,<4; sys_platform == "win32"` and pin the wheel in
   `uv.lock`;
6. follow with Vault named-pipe work across Avibe/avault, Show Runtime and
   dependency proofs, then private Runtime packaging, installers, signing, and
   ARM64.

The resulting product workflow is:

1. install Avibe in Windows without WSL;
2. start the service from PowerShell;
3. open the Tauri Workbench;
4. start one real agent backend;
5. stream its output;
6. read and modify a file in a `C:\...` working directory;
7. open a ConPTY terminal, send Unicode input, resize, and interrupt;
8. stop and restart cleanly without orphaning the process tree.

The first slice may omit detached terminal persistence across a full machine
reboot. That behavior must be designed after ConPTY lifecycle evidence exists.

The Windows terminal implementation uses dedicated reader and writer workers,
not the shared asyncio executor. Its close path must drain or close the output
pipe before `ClosePseudoConsole`, which can otherwise block on older Windows
releases. The supported floor is Windows 10 version 1809; release CI installs
pywinpty wheel-only.

Vault and Show Pages remain in the desktop acceptance catalog. Their native
Windows proof follows the first Agent/terminal slice because Vault requires a
matching avault transport contract and Show Runtime already has Windows bundle
assets but lacks a native lifecycle CI proof.

### M3: Product Distribution

- macOS signed and notarized DMG;
- Windows NSIS x64 installer first, ARM64 after the x64 gate;
- app-private `uv`, CPython, and Avibe wheel;
- versioned Runtime directories with rollback;
- uninstall preserves user data under the Avibe home;
- updater only after cold start, upgrade, rollback, and process lifecycle pass.

### M4: Mobile Reassessment

Re-evaluate Tauri mobile using the proven Workbench compatibility results.
Mobile work must have mobile-specific navigation, input, safe-area, keyboard,
and gesture design; it is not a desktop viewport shrink.

## Acceptance Scenarios

| ID | Scenario | Required evidence |
| --- | --- | --- |
| D01 | Cold launch with no Runtime | one Runtime starts and Workbench loads |
| D02 | Launch with existing Runtime | Runtime is adopted; no duplicate daemon |
| D03 | Streaming chat | SSE streams and reconnects after transient loss |
| D04 | Show Page | page renders, hot reloads, and remains interactive |
| D05 | Files | upload, download, drag/drop, and clipboard work |
| D06 | Vault | WebAuthn/PRF flow completes without secrets entering Tauri |
| D07 | Terminal | PTY/tmux on macOS and ConPTY on Windows use the same UI protocol |
| D08 | Sleep and wake | UI reconnects without duplicate Runtime |
| D09 | Window lifecycle | close/reopen and single-instance behavior are deterministic |
| D10 | Runtime crash | shell diagnoses failure and reconnects after restart |
| D11 | Clean install | application starts without system Python |
| D12 | Native Windows | real agent task completes with no WSL process or path |

D03 also verifies that the loopback origin sets its CSRF cookie on first load
and that subsequent mutations still succeed after reload and reconnect in both
WKWebView and WebView2.

### Required Windows Gates

| Gate | Coverage |
| --- | --- |
| `windows-runtime-contract` | isolated `AVIBE_HOME`, wheel install, service and authenticated control IPC, fake Codex, graceful stop/restart, no orphan tree |
| `windows-terminal-e2e` | real ConPTY with PowerShell Unicode, resize, `\x03`, exit, disconnect, and idempotent terminate; no bash or WSL |
| `windows-agent-e2e` | protected/nightly real Codex credential, stream and native file edit, no `wsl.exe`, no `/mnt/` |
| `desktop-windows-build` | required Windows x64 Tauri/WebView2 build |

ARM64 remains off the required matrix until the Claude SDK wheel constraint is
removed. D12 file proof covers drive roots and a native read/edit/rename in a
`C:\...` workspace; no Windows path translation layer is added.

## Go / No-Go Gate

Proceed with Tauri when all are true:

- no core Workbench feature has an unworkable WKWebView or WebView2 blocker;
- browser and desktop behavior remain materially equivalent;
- a native Windows agent session completes end to end;
- background tasks survive window close, shell restart, and WebView crash;
- platform differences remain inside the defined adapters;
- macOS and Windows have automated build and focused lifecycle verification.

Replace only the shell with Electron if any are true:

- Vault, Show Pages, terminal, or streaming requires a long-lived WebView fork;
- reliable lifecycle behavior cannot be tested or reproduced;
- Wry/Tauri must be permanently patched to ship core flows;
- business components accumulate Tauri-specific branches;
- OS WebView differences create recurring release-blocking regressions.

## Implementation Sequence

1. Land this plan and the `desktop` integration-branch policy in a documentation
   PR targeting `desktop`.
2. Land the small `vibe start --no-open-browser` launcher contract in its own
   Python PR.
3. Land a pure desktop-host PR containing only `desktop/**` and its desktop
   workflow: frozen probe/adopt/launch state machine, capability boundary,
   unit tests, and macOS/Windows build jobs.
4. Exercise D01-D05 and D08-D10 locally where the host permits.
5. Land the Windows Controller IPC contract and cross-platform conformance tests.
6. Land `ProcessHost`: graceful control request, daemon ownership, Job Object
   worker cleanup, PID birth identity, restart, and UI port recovery.
7. Prove D12 with Codex on Windows x64 and a real native workspace.
8. Extract the POSIX terminal seam and shared conformance suite without changing
   product behavior.
9. Land pywinpty/ConPTY and prove D07.
10. Prove Vault and Show Runtime on Windows without reducing their capability.
11. Reuse the existing managed-Runtime parsing, validation, caching, and asset
    resolution for private Python/installer work; do not create a parallel
    Windows downloader.
12. Add packaging and signing only after M1 and M2 pass.

Every implementation PR is non-draft, targets `desktop`, and requires GitHub
Codex review on its current head, zero unresolved review threads, and green
required CI before merge.

## Desktop Host PR Scope

Included:

- `desktop/**` Tauri v2 scaffold and bootstrap contract;
- safe loopback Runtime discovery and launch;
- bootstrap status/error/retry UI;
- unit tests for the Rust host logic;
- `.github/workflows/desktop-shell.yml` for macOS and Windows.

Excluded:

- this plan and repository workflow policy, which land separately;
- Python Runtime or CLI changes, including the headless launcher contract;
- ConPTY and Windows credential IPC;
- bundled Python and installer artifacts;
- tray and updater polish beyond what D01/D02/D09 require;
- any reduction of Show Page functionality;
- mobile projects.
