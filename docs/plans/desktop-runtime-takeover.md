# Desktop local Runtime takeover (#2264)

## Outcome

After an explicit first takeover, Desktop runs its bundled Avibe against the
existing data home. Native bootstrap must not silently adopt an independent
Controller and report an application upgrade as a Runtime upgrade.

## Contract

The bundled Python helper discovers a data home and returns a read-only snapshot
of its lock-owning Controller and UI process identities. Native bootstrap owns
the confirmation. The helper revalidates the exact snapshot before stopping
anything, refuses active work or an external supervisor, and stops only the
captured processes. Starting and database migration remain in the existing
`vibe start` path. Readiness must prove the bundled Controller and UI identity.
The stop shares the PID-safe signal primitive introduced in #2242, with forced
termination disabled on POSIX. It does not use the id-scoped scan because an
independent predecessor has no Desktop Runtime id. A predecessor that fails to
exit blocks the transition and leaves its UI running.

The selected home and independent-connection choice live in Desktop's existing
application data directory. An explicit AVIBE_HOME takes precedence. Automatic
home discovery accepts one verified same-user service; ambiguous discovery
requires selection of an existing home. A selected custom home must contain an
Avibe config. Product data is never copied into the application installation.

There is no CLI-owned launchd/systemd registration in the source baseline.
Desktop's existing login item continues to launch Desktop. A service controlled
by an external supervisor requires the owner to disable that registration before
handoff. Global uv/pip installations and shell profiles remain independently
managed. Desktop-launched agents already receive the bundled vibe CLI first on
PATH (#2250); the documentation explains using that entry from a terminal.

## Validation

Primary regression: a bundled bootstrap encountering an external service waits
for a management decision instead of navigating. Python consuming tests must
exercise data-home discovery and exact process identity guards with test-owned
state and processes. Cover active work, ownership changes, external supervisors,
non-ASCII custom homes, failed stop, independent choice, and strict successor
readiness. Run full changed test files, localization/build, Rust workspace tests,
formatting/Clippy, and changed Python Ruff before PR delivery.

Native packaged takeover and OS-login acceptance are residual manual checks;
this implementation must not modify the developer's running service or data.

Local validation after integrating #2242: 128 Python tests passed across the
takeover, Desktop Runtime, and scoped-stop files; 264 Rust tests/doc-tests
passed across the workspace with all features; all four VersionBadge tests,
both frontend builds, desktop localization, changed Python Ruff, Rust formatting,
and Clippy passed. The old process and IPC fixtures use only test-owned state.
