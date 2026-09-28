# Desktop Runtime scoped stop and installer ownership

Refs #2135 (Python half), #2131.

## Outcome and boundaries

A desktop app must update smoothly and never stop a Runtime it did not start.
Authority to stop follows provenance: the desktop host knows which Runtime id its
bundle started, and asks Python to stop only processes that carry that id. The
process check exists only to make sure the signal hits the process that was
verified. `/ready` and `/internal/health` are unchanged, and so are plain
`vibe stop` and `vibe stop --receipt`.

## `vibe stop --expect-runtime-id <RUNTIME_ID>`

`RUNTIME_ID` is the 64-character lowercase hex `AVIBE_DESKTOP_RUNTIME_ID` the
desktop host exports to the Runtime it launches. The flag is mutually exclusive
with `--receipt`.

- Targets are resolved once: the service lock owner and the process the UI
  pidfile names (when it is a live UI server). Service processes beyond the lock
  owner are not touched; they belong to a full stop.
- Each target's provenance is read once through
  `vibe.desktop_runtime.open_desktop_runtime_provenance`, currently the
  `AVIBE_DESKTOP_RUNTIME_ID` the process inherited. This is the single place to
  swap if another provenance source is needed.
- A target is signalled only through the handle its provenance was read from,
  and only after re-checking that the pid is still that process (birth time and
  id). A pid that has been reused, or can no longer be shown to be the target, is
  never signalled.

| Situation | Exit | stderr (last line) | Effects |
| --- | --- | --- | --- |
| Invalid id | 3 | `{"reason":"invalid_runtime_id"}` | none |
| Service has another id or none | 3 | `{"reason":"service_runtime_id_mismatch"}` | none |
| Service provenance unreadable | 3 | `{"reason":"service_identity_unavailable"}` | none |
| Service and UI match, or are not running | 0 | — | service, UI, OpenCode, remote access, abandoned installs of this id |
| Service matches, UI has another id, none, or is unreadable | 0 | `{"skipped":"ui","reason":"ui_runtime_id_mismatch"}` or `ui_identity_unavailable` | service and OpenCode only |
| A verified target or installer tree did not stop | 2 | human-readable error | status `error` |

Refusal (exit 3) happens before anything is signalled; remote access, OpenCode
and the status file are untouched.

Mixed case: the service is the Runtime the caller asked about, so it is stopped
and the command succeeds. A UI that does not belong to that Runtime is never
signalled, and what it owns stays with it: remote access (its tunnel) and its
backend installs. The skipped line tells the caller a foreign UI remains.

## Handover

When a desktop `vibe start` finds a controller whose health names another
desktop Runtime id, it calls the same `runtime.stop_desktop_runtime` with that
observed id. It keeps the tunnel for the successor, as before, and fails the
start if the stop is refused or incomplete.

## Backend installer ownership (#2131)

- The UI process that starts an installer owns its tree. Every installer runs
  in its own process group, carries a fresh `AVIBE_PROCESS_IDENTITY` marker, and
  has a durable record under `<runtime dir>/desktop-backend-installs/`.
- After npm exits, times out, or the owner shuts down, the owner stops the
  leader, waits for its group, and reaps any member by group and by marker.
  `.install.lock` and the staging directory are released only after the tree is
  shown gone. If it cannot be shown gone, the install fails with
  `install_drain_failed`, and the lock and the record stay.
- The owner drains on its stop signal (`handle_exit`) and on lifespan shutdown,
  then refuses new installs (`install_shutting_down`).
- An owner killed before its drain finished leaves its record behind. The
  process that stopped the UI reaps it: `vibe stop`, `vibe stop
  --expect-runtime-id` (only records of that id), and the restart supervisor. A
  record that cannot be reaped fails the stop (exit 2) or the restart.

## Consumer notes for the desktop host (PR2)

- Pass `--expect-runtime-id` for handover, Quit and uninstall; treat exit 3 as
  "not ours, nothing was touched" and parse the JSON reason.
- Uninstall should take the backend's `.install.lock` before removing its root.
