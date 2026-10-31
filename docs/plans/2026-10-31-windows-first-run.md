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
- On the next Windows rc, verify the preparation sentence during first-run
  extraction and zero extra Python console windows after the workbench appears,
  including while a backend CLI installs.

Wizard/onboarding behavior, extraction percentages, NSIS extraction, signing,
and release publication are outside this change.
