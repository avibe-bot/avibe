# Avibe desktop shell

A Tauri v2 shell that opens one window, makes sure an Avibe Runtime is serving
the local Workbench, and then hands the window to it. It is deliberately thin:
it hosts no product UI of its own beyond a bootstrap screen, and it owns no
product logic.

## What it does

1. Resolves an installed `vibe` executable and reads
   `vibe desktop endpoint --json`. Python derives that versioned descriptor from
   the effective V2 UI binding, so Rust never duplicates config semantics.
2. Validates the target origin. Only the literal addresses `127.0.0.1` and
   `[::1]` over `http` are accepted, so the shell can never be pointed at a
   remote host. The hostname `localhost` is refused too: it names both addresses,
   and the shell navigates to exactly the one whose readiness probe answered. A
   refused origin is reported without being quoted back — it is unvalidated
   process output on its way to a WebView.
3. Probes `GET /ready`. Only the exact versioned `product: "avibe"` affirmative
   response means that the UI, service-lock owner, and Controller IPC are all
   ready; that Runtime is **adopted as-is**.
4. Otherwise runs `vibe start --no-open-browser` once, detached, with no shell
   interpreter involved. The shell already owns a window onto the Workbench;
   plain `vibe start` honours `config.ui.open_browser` and would leave the user
   with a second, browser-hosted view of the same Runtime.
5. Polls until the Runtime is ready or the bound expires, then navigates. The
   window never leaves the bootstrap page before combined readiness succeeds.
   If the launcher it started exits non-zero, it gives up immediately instead of
   waiting out the timeout: nothing is coming, and the user is asked to update
   Avibe rather than watching a spinner for two minutes.
6. After navigation, probes the same exact origin every two seconds. Three
   consecutive failures return the WebView to the bundled bootstrap and rerun
   adoption/startup; a transient miss does not replace the Workbench.

The Runtime outlives the shell. Closing the window never stops a Runtime,
whether the shell adopted it or started it.

### Why the Workbench stays unprivileged

The three bootstrap commands are declared in `src-tauri/build.rs`, which makes
`tauri-build` generate `allow-bootstrap-status` / `allow-bootstrap-retry`
permissions. Without that declaration a Tauri v2 app command is callable from
any page in any window — including the Workbench and every Show Page inside it.

`src-tauri/capabilities/bootstrap.json` then grants those two permissions to
`local: true` pages in the `main` window and names **no** `remote` URL, so the
grant stops matching the moment the window navigates to the Runtime's http
origin. `ensure_shell_ui` in `src-tauri/src/lib.rs` rejects such a call a second
time regardless.

Tauri decides *local* versus *remote* with `is_local_url`, which counts any URL
relative to the configured `devUrl` as local. Port `1420` is therefore refused
as a Runtime origin outright — in release builds too, where the dev server does
not exist — so no `AVIBE_DESKTOP_ORIGIN` value can produce a Workbench page that
Tauri would classify as the shell's own.

All three properties are asserted, in `src-tauri/tests/shell_boundaries.rs` and
`runtime-host/src/origin.rs`. Widening any of them fails a test.

## Prerequisites

- Rust stable (`rustup toolchain install stable`), with `rustfmt` and `clippy`
- Node.js `^20.19.0 || >=22.12.0`
- macOS: Xcode Command Line Tools. Windows: MSVC build tools + WebView2
  (preinstalled on Windows 11). Both provide the system WebView; there is
  nothing to install for the WebView itself.
- An installed Avibe Runtime (`uv tool install avibe-os`) if you want the shell
  to actually reach a Workbench. It must implement
  `vibe desktop endpoint --json`, `GET /ready`, and
  `vibe start --no-open-browser`; an older one produces a localized retryable
  update failure.

## Commands

Run everything from `desktop/`.

| Command | What it does |
| --- | --- |
| `npm ci` | Install the bootstrap UI dependencies |
| `npm run tauri dev` | Run the shell against the Vite dev server on port 1420 |
| `npm run build` | Type-check and build the bootstrap UI into `dist/` |
| `npm run test:i18n` | Verify English/Chinese bootstrap key and placeholder parity |
| `npm run dev` | Serve the bootstrap UI alone in a browser (no shell, no IPC) |
| `cargo test --workspace` | Run every Rust test |
| `cargo test -p avibe-runtime-host` | Run the runtime logic tests without compiling Tauri |
| `cargo fmt --all` | Format |
| `cargo clippy --workspace --all-targets -- -D warnings` | Lint |
| `cargo build --workspace` | Compile the shell binary |
| `npm run tauri build` | Produce a bundled application |

`cargo` steps depend on `dist/` existing, because `tauri::generate_context!`
embeds the frontend at compile time. Run `npm run build` first in a fresh
checkout — `npm run tauri dev` and `npm run tauri build` handle this themselves.

## Environment overrides

All optional; the defaults are what ships.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AVIBE_DESKTOP_ORIGIN` | Python descriptor | Development/test override for another local Runtime. Still validated as loopback, and port `1420` is reserved for the shell's own UI. The override is adopt-only: the shell re-probes it on retry but never starts the default `vibe` against a different origin. |
| `AVIBE_DESKTOP_VIBE_PATH` | — | Absolute path to a `vibe` executable. Useful because a GUI process launched from Finder or Explorer inherits a minimal `PATH` that usually excludes `~/.local/bin`. |
| `UV_TOOL_BIN_DIR` | uv default | Standard uv override for the directory containing installed tool executables. The shell checks it after inherited `PATH`. |
| `AVIBE_DESKTOP_READY_TIMEOUT_SECONDS` | `120` | How long a starting Runtime has to answer. Accepted range 1–600; anything else is ignored. |

The shell also sets `AVIBE_DESKTOP_SHELL=1` on the Runtime it starts. That is an
output, not an input.

Runtime discovery is ordered and shell-free: the explicit desktop override,
inherited `PATH`, `UV_TOOL_BIN_DIR`, the uv default under `~/.local/bin`,
then platform fallbacks (`~/bin`, `~/.cargo/bin`, `/opt/homebrew/bin`,
`/usr/local/bin`, or `%APPDATA%\Python\Scripts`). The Windows executable name
is `vibe.exe`.
If no executable is found, the bootstrap offers the fixed Avibe installation
guide in the system browser; the WebView cannot provide or alter that URL.

## Layout

```
desktop/
├── index.html, src/           Bootstrap UI (vanilla TS + Vite)
├── runtime-host/              Runtime discovery, launch, readiness — no Tauri dependency
│   ├── src/origin.rs          Loopback origin validation
│   ├── src/health.rs          Exact `/ready` probing
│   ├── src/launcher.rs        Descriptor query plus detached, shell-free startup
│   ├── src/bootstrap.rs       The state machine
│   └── src/status.rs          The status contract shared with the UI
└── src-tauri/                 Window, IPC commands, capability boundary
```

The split is the reason the logic is testable: `avibe-runtime-host` depends on
no Tauri crate, so its tests compile in seconds and run on any platform, and the
`src-tauri` crate stays thin enough to be checked by inspection.

`runtime-host/tests/bootstrap.rs` drives a real `RuntimeHost` through fake
collaborators under `tokio`'s virtual clock, so the 120-second production wait
is exercised in microseconds. Launcher tests execute a fake installed CLI and
assert the descriptor schema, byte bound, direct argv, and literal-loopback
validation.

## Regenerating icons

Icons derive from the product logo. Regenerate from `desktop/`:

```sh
npx tauri icon ../ui/public/logo.png
```

`tauri icon` writes a full set including Android, iOS, and Windows Store assets.
Only the five files listed under `bundle.icon` in `src-tauri/tauri.conf.json`
are used — delete the rest rather than committing them.

The bootstrap screen shows `src-tauri/icons/128x128.png` directly, so the mark in
the window and the mark in the Dock are the same file and cannot drift apart.
Vite fingerprints it into `dist/assets/`; nothing depends on the icon path at
runtime.

## CI

`.github/workflows/desktop-shell.yml` runs on macOS and Windows whenever
`desktop/**` or the two central bootstrap locale sections change: `npm ci`,
`npm run test:i18n`, `npm run build`, `cargo fmt --check`,
`cargo clippy -D warnings`, `cargo test`, `cargo build`. It does not bundle or
sign; those belong to a release workflow.
