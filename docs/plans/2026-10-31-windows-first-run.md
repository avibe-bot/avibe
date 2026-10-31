# Windows first-run feedback and background processes

## Change contract

Before Runtime resolution (including bundled archive extraction and verification),
bootstrap publishes the localized `preparing` notice. Its phase remains `probing`,
its origin is empty, its attempt is zero, and it is not retryable. The bootstrap
page initially renders the same notice. Only completed resolution permits the
`probing` notice and endpoint probes. Progress remains indeterminate.

On Windows, either truthy desktop environment marker (`AVIBE_DESKTOP_SHELL` or
`AVIBE_DESKTOP_MANAGED_RUNTIME`) makes isolated children windowless. Without
either marker, callers retain the existing console default. Explicit background
spawns (owned Runtime children, their log sinks, and desktop backend installers)
request no console even when desktop environment markers are absent. POSIX
session isolation and the Rust launcher's detached lifecycle remain unchanged.
Persisted Windows worker recovery still verifies the worker marker before
touching its process tree. An unavailable or failed console signal must not
prevent bounded forced cleanup of that verified tree, including descendants
captured before the leader exited.

Windows NSIS installs eagerly prepare the adjacent bundled Runtime after copying
the app. The executable handles the exact `--prepare-runtime` command before
Tauri initialization and rejects extra helper arguments. It obtains LocalAppData
through the same `dirs` Known Folder lookup as Tauri, uses the identifier from
`tauri.conf.json`, and delegates installation to `PrivateRuntimeBundle::prepare`.
`dirs` is already in Tauri's dependency graph and is explicitly declared as a
Windows dependency for this GUI-free lookup.

The post-install hook waits for preparation and fails installation on a launch
error or nonzero exit. Silent, passive, and `/UPDATE` modes never show the hook's
failure dialog. Preparation does not prune old slots or start the shell. The
normal lazy path remains the integrity/repair fallback; macOS DMG and non-NSIS
paths remain lazy.

## Validation

- Hold launcher resolution at a test-controlled barrier: preparation must
  already be visible, and no endpoint probe may occur until release.
- Check the serialized status and both bundled locale catalogs.
- Exercise the Windows creation-flag policy with isolated environment fixtures,
  plus the existing Windows `GetConsoleWindow` probe.
- Exercise the owned-process spawn boundary without creating a real service;
  every child it creates must request windowless isolation.
- Exercise persisted worker cleanup without a shared console: verified trees
  are reaped, while a changed identity blocks signaling and forced termination.
- Run focused Rust/Python tests, Ruff, desktop i18n/build and UI build gates.
- Verify installer argument dispatch and root derivation, installation/reuse
  through the existing bundle implementation, failure propagation, and retention
  of a predecessor slot in hermetic directories. Verify NSIS hook wiring and
  unattended failure branches with the existing shell-boundary tests.
- On the next Windows rc, verify the preparation sentence during first-run
  repair, eager extraction in the NSIS installer, and zero extra Python console
  windows after the workbench appears, including while a backend CLI installs.
  Exercise a failed silent `/UPDATE` to confirm the existing updater handles
  the nonzero installer exit.

Wizard/onboarding behavior, extraction percentages, signing,
and release publication are outside this change.
