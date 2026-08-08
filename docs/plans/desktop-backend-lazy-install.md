# Desktop Backend Lazy Installation

## Status

Frozen implementation contract for the first self-contained desktop release.

## Product Outcome

The desktop package remains usable on a clean macOS or Windows machine without
system Python, Node.js, npm, or `uv`. It embeds the Avibe Runtime, Python,
Node.js, and npm, but does not embed Codex, Claude Code, or OpenCode. Agent
backends are installed only after the user requests one.

## Resolution Contract

1. An explicitly configured executable path is authoritative while it remains
   valid. Avibe never silently replaces it.
2. With no configured path, existing external installations are discovered
   first through the current platform resolver.
3. Only when external discovery fails may Avibe resolve an app-private backend
   recorded under the desktop backend root.
4. A desktop Runtime projects every configured backend selector through the
   same resolver used by Settings before constructing Claude, Codex, or
   OpenCode runtime configuration. GUI-process `PATH` inheritance is never a
   second source of truth for whether an installed backend can launch.
5. Installing a missing backend in a desktop-managed Runtime uses the bundled
   Node.js and npm. It never requires or mutates system Node.js, npm, or global
   package state.
6. Non-desktop installations retain the existing global one-click installer
   behavior.

## Private Install Contract

- The package allowlist is fixed in code:
  - Codex: `@openai/codex`
  - Claude Code: `@anthropic-ai/claude-code`
  - OpenCode: `opencode-ai`
- npm is invoked directly as `node <npm-cli.js>` without a shell.
- Lifecycle scripts are disabled. Avibe selects the target-specific optional
  package and verifies its native executable instead of delegating publication
  to an upstream postinstall script.
- npm installs into a fresh staging prefix below the app-private backend root.
- Avibe validates the installed package version and a target-native executable
  before publishing the release.
- Publication is an atomic, bounded `current.json` descriptor. Every descriptor
  path is relative and must remain below the backend root.
- The exact published executable path is persisted in backend configuration.
- Updates use the same staged install and atomic publication path. A running
  backend may continue using its previous release; old releases are not deleted
  during installation.
- Failed staging directories are removed. Previously published releases and
  configuration remain unchanged.

## Runtime Bundle Contract

Runtime manifest schema 2 contains Python, Node.js, npm, and Avibe metadata.
It contains no agent backend version, executable, helper, or license. The
desktop launcher supplies these immutable paths to Python:

- `AVIBE_DESKTOP_RUNTIME_ROOT`
- `VIBE_SHOW_RUNTIME_NODE_BIN`
- `AVIBE_DESKTOP_NPM_CLI`
- `AVIBE_DESKTOP_BACKENDS_ROOT`

The backend root is mutable app data and is separate from the content-addressed
Runtime root. The Runtime integrity verifier therefore never treats lazy-loaded
backends as bundled files.

The desktop builder installs `claude-agent-sdk` from its pure-Python source
distribution. Its platform wheels embed Claude Code, so accepting those wheels
would violate the same boundary even though the executable lives inside a
Python dependency rather than under `tools/`. The builder verifies that the
installed SDK contains no bundled Claude executable before packaging.

## Lifecycle And Updates

- A private backend reports `managed_by: "desktop"`, but remains independently
  updateable from the Settings UI.
- A successful install path is already persisted and hot-reconciled. Settings
  adopts it as both the displayed and saved value, so it never offers a second
  Save action for the same activation.
- Install completion and failure messages use the configured Avibe language.
- Backend update checks continue to compare the installed version with the
  package registry version.
- Desktop application updates replace the signed Runtime bundle but do not
  silently replace private backends.
- Explicit desktop uninstall removes both the app-private Runtime and the
  app-private backend root after the managed Runtime has stopped. Avibe user
  state under `AVIBE_HOME` remains untouched.

## Acceptance

- A clean desktop install can install and launch each backend without system
  Node.js or npm.
- An existing external backend is selected before an app-private one.
- An explicit configured path remains selected across desktop and backend
  updates.
- Private install and update publish only verified direct native executables;
  command shims such as `.cmd` are never persisted.
- A failed install cannot replace the active descriptor or configured path.
- The product Runtime archive contains no Codex, Claude Code, OpenCode, or
  backend-specific ripgrep payload.
- macOS and Windows package CI verify Runtime schema 2 and the bundled npm CLI.

## Residual Follow-up

Release pruning needs a backend-process drain boundary. Until that exists,
superseded private backend releases are retained to avoid deleting files used by
active sessions.
