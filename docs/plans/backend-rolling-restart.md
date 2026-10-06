# Backend Rolling Restart

Status: superseded by `runtime-generations.md`.

Configuration changes, credential flows, manual Restart, CLI installs, and
enabling a backend no longer drain or interrupt anything. Each runtime unit
moves to the new configuration at its next turn, and running work finishes on
the process it started on. Disabling a backend stops its work at once, because
the user asked for that. The 300 s drain, its timeout override, and the
drain-then-interrupt cutover are deleted.

## What remains of the barrier

The backend admission barrier (`begin_backend_drain` / `end_backend_drain`)
remains for two exclusive operations only:

- **Native credential cutover** (`BackendRestartCoordinator.migration_guard`).
  An explicit user decision moves a native credential, so the guard interrupts
  the backend's running work and retires its runtime strictly before the move.
  An old process could otherwise rewrite a credential file while it is moved.
- **Unattended CLI auto-update** (`run_when_idle`). Admission is closed only
  while the CLI is replaced, so no turn launches a half-installed binary. The
  runtime then renews in place.

These invariants still hold for both:

- At most one active turn per Session and runtime key.
- The barrier never stops the Avibe service, UI, or tunnel.
- A failed operation reopens the barrier and preserves queued input.

## Service restart boundary

A SIGTERM of the Avibe process cannot preserve an in-memory receiver. A true
service rolling restart would need an external supervisor and a cross-process
ownership handoff. Until that exists, a service restart keeps crash-recovery
semantics and must not pretend to be seamless.
