# Computer Use Target Locks

## Background

The released Computer Use server keeps a desktop-wide lease for the lifetime of
an Avibe Session. A Session that only observed one window can therefore block
unrelated Sessions and read-only observations.

## Goal

Allow independent Sessions to observe concurrently and mutate independent
targets concurrently, while preserving per-Session driver sessions,
observation snapshots, element-token validation, Turn binding, and the SDK's
dispatched/ambiguous failure rules.

## Solution

- Keep target locks in the Controller process.
- Do not lock observations.
- Lock one window identity for window mutations, one application identity for
  launch, one process identity for force-quit, and the clipboard for clipboard
  writes.
- Wait for a bounded interval, then return `target_busy` with
  `dispatched: false`.
- Advance a window's in-process mutation version after a successful action so
  another Session's older observation is rejected before dispatch.
- Remove the desktop lease record, lock file, heartbeat, lease-loss handling,
  and lease epoch coupling. Remove legacy lease files on startup when possible.

A future foreground operation takes a global exclusive lock only for its own duration.

## Validation

- Hermetic fake-upstream tests cover concurrent observation, independent
  windows, same-target contention, force-quit/process conflicts, clipboard
  serialization, legacy-file cleanup, and stale cross-Session observations.
- Run the focused Python tests and Ruff checks, then the desktop-shell check
  when desktop files are changed.
