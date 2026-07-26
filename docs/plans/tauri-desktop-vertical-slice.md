# Tauri Desktop Vertical Slice

## Status

- Owner: Avibe core
- Target platforms: macOS and native Windows
- Mobile: deferred until the desktop shell and shared WebView constraints are proven
- Delivery strategy: a retained product scaffold, not a disposable prototype

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
2. Accept only `http://127.0.0.1`, `http://localhost`, or `[::1]` loopback
   origins during this milestone.
3. Probe `GET /health`.
4. If healthy, adopt the existing Runtime and navigate to Workbench.
5. If absent, launch the installed `vibe` executable without a shell.
6. Poll health with a bounded timeout.
7. Navigate only after a successful health response.
8. Never kill an adopted Runtime when a window closes or the shell exits.
9. Never expose command strings, environment variables, or secret-bearing
   process output to the WebView.

The initial shell assumes `vibe` is installed. Private Runtime bundling belongs
to the distribution milestone.

## Native Windows Runtime Contract

Windows support must be expressed behind platform interfaces rather than
scattered `os.name` branches:

```text
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

- Windows terminal sessions use ConPTY through a maintained binding and retain
  the existing xterm WebSocket protocol.
- Process stop/restart uses native process-tree semantics and never calls
  `wsl.exe`.
- credential IPC uses a Windows-appropriate authenticated local transport.
- dependency installation resolves native PowerShell and Windows assets.
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

Deliver one complete native Windows workflow:

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

1. Commit this plan before implementation.
2. Add a minimal `desktop/` Tauri workspace and frozen bootstrap types.
3. Implement loopback origin validation, health probing, installed Runtime
   launch, bounded readiness, and navigation.
4. Add unit tests and macOS/Windows shell build CI.
5. Exercise D01-D05 and D08-D10 locally where the host permits.
6. Open the first non-draft PR and complete review/CI gates.
7. Land Windows platform interfaces and ConPTY support as a separate focused
   PR, using D07 and D12 as its product gate.
8. Add private Runtime packaging and signing only after M1 and M2 pass.

## First PR Scope

Included:

- this plan and bootstrap contract;
- `desktop/` Tauri v2 scaffold;
- safe loopback Runtime discovery and launch;
- bootstrap status/error/retry UI;
- unit tests for the Rust host logic;
- desktop build workflow for macOS and Windows;
- developer commands needed to run the shell.

Excluded:

- ConPTY and Windows credential IPC;
- bundled Python and installer artifacts;
- tray and updater polish beyond what D01/D02/D09 require;
- any reduction of Show Page functionality;
- mobile projects.

