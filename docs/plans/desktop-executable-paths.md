# Desktop executable path handling

Owner: production delivery session `sesmcz3kd4ew2`.
Base: `origin/master` at `3f1c551b4` (2026-10-14).
Classification: small production-safe fix; one serial implementation lane.

## Outcome and contract

A desktop-provided executable path reaches subprocess execution intact, including
spaces and Windows backslashes. Show Pages must resolve the bundled Node executable
under macOS Application Support without a shim. Preserve established command-with-
arguments behavior only where repository callers/documentation establish it.

Audit launcher and runtime-bundle environment producers and Python consumers,
including Node, npm CLI, runtime/backends roots, other executable environment values,
and command parsing helpers. Fix demonstrated defects at their shared owning boundary;
do not rewrite unrelated backend command infrastructure. Invalid configured paths
must not accidentally execute a valid space-delimited prefix. Keep existing fallback
semantics unless the audit establishes a necessary change. A more precise diagnostic
is optional if it stays small and compatible.

## Audit

- `desktop/runtime-host/src/launcher.rs` and
  `desktop/scripts/build-runtime-bundle.py` publish Node, npm, Runtime, and backend
  locations as raw filesystem paths. `vibe/restart_supervisor.py` carries those
  values through a desktop restart without parsing them.
- `vibe/desktop_runtime.py` already consumes all four values as `Path` objects,
  confines immutable files to the Runtime root, verifies Node is executable, and
  keeps the mutable backend root outside the Runtime root. `vibe/desktop_backends.py`
  launches Node and the npm CLI as separate argv entries, so spaces and backslashes
  remain data.
- Runtime and backend roots are directory values and never enter command parsing.
  The npm CLI is a script path passed after the separately validated Node executable.
  These paths need no code change.
- `core/show_runtime.py` is the only defective consumer: it sends the
  launcher-supplied `VIBE_SHOW_RUNTIME_NODE_BIN` path through `_resolve_command`,
  whose `shlex.split` is correct for command strings but corrupts unquoted paths.
- `_resolve_command` must retain command-with-arguments behavior. Explicit Show
  Runtime configuration uses it, runtime repair reconstructs such commands with
  `shlex.join`, and npm discovery uses its PATH lookup. The desktop Node override has
  no command-with-arguments producer or documented caller, so its path-specific
  resolver is the narrow owning boundary for the fix.

## Scope and evidence

Expected edits: `core/show_runtime.py`, relevant existing shared path helper if needed,
`vibe/desktop_runtime.py` only if audit demonstrates a defect, focused existing tests,
and this brief / existing directly relevant documentation. Launcher and bundle sources
are read-only unless the consumer-side solution proves incomplete; report any needed
scope expansion. No schema, dependency, UI, service, packaging or release changes.

Use test-owned filesystem/env state only. Regression coverage must fail on the old
resolver and prove intact argv through a real executable path containing spaces.
Cover native backslash preservation without pretending a Linux/macOS test is Windows
execution. Audit npm/root paths and relevant command compatibility. Run focused tests
and Ruff on all changed Python files. Manager independently inspects the actual diff
and exercises the changed path before reporting completion.

Pre-fix regression evidence on macOS:

- an executable reached through an `Application Support` path resolved as missing;
- a missing full path whose first whitespace-delimited prefix was executable resolved
  to that prefix plus a synthetic argument;
- a Windows path with spaces and backslashes never reached native path validation.

Post-fix focused coverage executes a test-owned interpreter through an
`Application Support` symlink, rejects the valid-prefix case, preserves a Windows
path byte-for-byte up to native path validation, and pins command-with-arguments
behavior for the generic resolver. Existing desktop Runtime confinement,
runtime-bundle probe environment, and all three backend npm argv contracts remain
covered.

Deliver a non-draft PR to `master`, exact-head Codex review, all expected CI green,
and zero unresolved threads. Record audit findings and residual platform acceptance
here and in the PR. The fix needs a new Desktop rc/runtime bundle to reach installed
Desktop users; creating that release is not authorized in this task.

Native installed-Desktop acceptance remains for the next macOS and Windows rc:
macOS must render a Show Page from its normal Application Support Runtime without a
shim, and Windows must launch the bundled Node path from the installed package.

## Safety and close-out

Never restart local vibe/Desktop, touch the owner shim at
`/Users/max/Library/Application`, mutate user state, or use the shim in tests.
Do not merge, deploy, tag, or release without explicit owner instruction.
