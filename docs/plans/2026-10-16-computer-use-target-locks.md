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
- Remove the desktop lease record, lock file, heartbeat, lease-loss handling,
  and lease epoch coupling. Remove legacy lease files on startup when possible.

A future foreground operation takes a global exclusive lock only for its own duration.

Actions by another Session, like actions by the owner, can make an observation
stale. The driver's token and capture validation and the effect result report
it. Agents re-observe after an unexpected result.

## Validation

- Hermetic fake-upstream tests cover concurrent observation, independent
  windows, same-target contention, force-quit/process conflicts, clipboard
  serialization, legacy-file cleanup, token-only input, pid-less zoom, and
  driver-reported stale tokens after a cross-Session action.
- Run the focused Python tests and Ruff checks, then the desktop-shell check
  when desktop files are changed.
