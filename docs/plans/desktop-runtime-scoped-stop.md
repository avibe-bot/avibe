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
  pidfile names. Service processes beyond the lock owner are not touched; they
  belong to a full stop.
- A pidfile process whose command is readable and is not the expected program
  is stale and ignored. A live process whose command cannot be read is never
  treated as absent: for the UI it is `ui_identity_unavailable`.
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
| Service and UI match | 0 | — | service, UI, remote access, OpenCode of this id, abandoned installs of this id |
| Service matches, no UI | 0 | — | service, remote access, OpenCode of this id, abandoned installs of this id |
| Nothing running | 0 | — | OpenCode of this id, abandoned installs of this id |
| Service matches, UI has another id, none, or is unreadable | 0 | `{"skipped":"ui","reason":"ui_runtime_id_mismatch"}` or `ui_identity_unavailable` | service and OpenCode of this id |
| Another UI took the pidfile while the service stopped | 0 | `{"skipped":"ui","reason":"ui_changed"}` | service, the verified UI if still alive, OpenCode of this id |
| A verified target or installer tree did not stop | 2 | human-readable error | status `error` |

Refusal (exit 3) happens before anything is signalled; remote access, OpenCode
and the status file are untouched.

Mixed case: the service is the Runtime the caller asked about, so it is stopped
and the command succeeds. A UI that does not belong to that Runtime is never
signalled, and what it owns stays with it: remote access (its tunnel) and its
backend installs. The skipped line tells the caller a foreign UI remains.

### What else a scoped stop touches

Shared resources are stopped only through an owner that was verified, or by
their own provenance:

- Remote access belongs to the UI, and with no UI to the service. It is
  stopped only through a verified UI or, with no UI, a verified service, and
  only while the UI pidfile still names the verified UI (or nothing). Stopping
  the service takes seconds; if another UI took the pidfile meanwhile, the
  tunnel and installs are left to it (`ui_changed`). With nothing of this
  Runtime running the tunnel is not touched: another Runtime may be between
  restarts and about to adopt it.
- The OpenCode server pidfile is shared by every Runtime of this home, so it can
  already name a successor's server. The server is stopped only when its own
  provenance names this Runtime. A server this Runtime adopted from another one
  is terminated by the adopting Controller when it stops.
- Abandoned installer records are reaped only for this id, and only when the
  UI side was not left to another UI.

## Handover

When a desktop `vibe start` finds a controller whose health names another
desktop Runtime id, it calls the same `runtime.stop_desktop_runtime` with that
observed id. It keeps the tunnel for the successor, as before, and fails the
start if the stop is refused or incomplete. An OpenCode server that the old
Runtime started and its Controller did not terminate is stopped as in the verb.

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

## Known limits

- The service lock guards only this process. Between the last re-check and a
  signal or the tunnel stop, a successor can still appear; closing that window
  needs a cross-process lifecycle lock.
- A tunnel left when nothing of this Runtime is running, and an OpenCode
  server adopted from another Runtime whose Controller was killed before
  cleanup, are left for adoption or a full stop.

## Consumer notes for the desktop host (PR2)

- Pass `--expect-runtime-id` for handover, Quit and uninstall; treat exit 3 as
  "not ours, nothing was touched" and parse the JSON reason.
- Uninstall should take the backend's `.install.lock` before removing its root.
