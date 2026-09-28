# Desktop Runtime scoped stop and installer ownership

Refs #2135 (Python half), #2131.

## Outcome and boundaries

A desktop app must update smoothly and never stop a Runtime it did not start.
Authority to stop follows provenance: the desktop host knows which Runtime id its
bundle started, and asks Python to stop only processes that carry that id. No
pidfile or lock record decides what is signalled. `/ready` and `/internal/health` are unchanged, and so are plain
`vibe stop` and `vibe stop --receipt`.

## `vibe stop --expect-runtime-id <RUNTIME_ID>`

`RUNTIME_ID` is the 64-character lowercase hex `AVIBE_DESKTOP_RUNTIME_ID` the
desktop host exports to the Runtime it launches. Every process that Runtime
starts inherits it. The flag is mutually exclusive with `--receipt`.

### Discovery is one scan

`runtime.stop_desktop_runtime` scans this user's processes once, through
`core.process_isolation.processes_carrying_marker`, for those whose inherited
`AVIBE_DESKTOP_RUNTIME_ID` hashes to the expected id. Each one is classified by
its command line:

| Role | Command line |
| --- | --- |
| `service` | runs `vibe/service_main.py` (argv only, so a bundle an update replaced or moved is still recognized), or the development `main.py` |
| `ui` | `vibe.ui_server` and `run_ui_server` |
| `opencode` | `opencode` and `serve` |
| `unknown` | carries the id, but its command line cannot be read |
| none | any other program: agent CLIs, the tunnel connector |

- The stop process and its ancestors carry the id when the desktop host
  started the stop, and are never signalled.
- A signal goes only to a handle the scan returned, and only while
  `is_running()` holds for it. psutil compares the birth time it read at the
  scan, and `send_signal`/`terminate`/`kill` refuse a reused pid themselves.
- Each scan clears psutil's `process_iter` cache: the cached iteration skips,
  once, a pid it has flagged as reused, which would hide that pid's new holder
  from the rescan.

### Order and postcondition

1. Service processes, then UI processes: SIGTERM, then SIGKILL after 5 s.
2. Abandoned backend installs of this id, once no `ui` or `unknown` process of
   this Runtime is alive (the UI drains its own installs as it exits). This is
   the one file the scoped stop reads: the installer records, whose trees are
   signalled by their own markers.
3. The OpenCode server.
4. A rescan decides the outcome. Any role process still carrying the id,
   including one started after the scan or one in the stop's own lineage,
   fails the stop.

`unknown` processes and programs without a role are never signalled.

### Presence and outcome

`runtime.DesktopRuntimePresence` is `MATCH` when the scan finds a role process.
Otherwise the service lock decides without reading its record: free is
`ABSENT`, held is `MISMATCH`, and a lock that cannot be probed is `UNKNOWN`.
The desktop host mirrors these as Mine, Absent, Foreign and Unknown.
`runtime.DesktopRuntimeStopOutcome` is `NOT_OURS` (nothing signalled),
`STOPPED` or `FAILED`.

| Situation | Exit | stderr | Effects |
| --- | --- | --- | --- |
| Invalid id | 3 | `{"reason":"invalid_runtime_id"}` | none |
| No role process carries the id, and the service lock is held | 3 | `{"reason":"service_runtime_id_mismatch"}` | none |
| No role process carries the id, and the lock cannot be probed | 3 | `{"reason":"service_identity_unavailable"}` | none |
| Nothing carries the id, and the lock is free | 0 | none | abandoned installs of this id |
| Every role process carrying the id stopped | 0 | `{"left_running":[{"pid":…,"name":…}]}` when other programs carry the id | status `stopped` |
| A role process is left, or an installer tree did not stop | 2 | localized error, then `{"failed":"<part>","remaining":[{"pid":…,"role":…}]}` | status `error` |

`<part>` is the first of `service`, `ui`, `installs`, `opencode` and `unknown`
that did not stop. Refusal happens before anything is signalled; installers
and the status file are untouched. The JSON lines are the machine contract and
are never localized; the exit-2 diagnostics go through `vibe/i18n/`
(`runtime.stop.*`) and are shared with the full stop.

## Handover

When a desktop `vibe start` finds a controller whose health names another
desktop Runtime id, it calls the same `runtime.stop_desktop_runtime` with that
observed id and fails the start if the stop is refused or fails. It also fails
the start if the UI pidfile still names a running UI afterwards: that UI is not
the superseded Runtime's, and the start would otherwise reuse it or replace it
with an unscoped stop. The tunnel connector is left for the successor to adopt.

## Backend installer ownership (#2131)

- The UI process that starts an installer owns its tree. Every installer runs
  in its own process group, carries a fresh `AVIBE_PROCESS_IDENTITY` marker, and
  has a durable record under `<runtime dir>/desktop-backend-installs/`.
- The record names the install's staging directory and lives exactly as long
  as the install: the install holds `.install.lock` from before the record is
  written until after it has removed its staging and then the record.
- After npm exits, times out, or the owner shuts down, the owner stops the
  leader, waits for its group, and reaps any member by group and by marker.
  `.install.lock`, the staging directory and the record are released only after
  the tree is shown gone. If it cannot be shown gone, the install fails with
  `install_drain_failed`, and the lock, the staging and the record stay.
- The owner drains on its stop signal (`handle_exit`) and on lifespan shutdown,
  then refuses new installs (`install_shutting_down`).
- Every acquisition of `.install.lock` goes through one function,
  `desktop_backends._claim_backend_root`. A record for that root seen under the
  lock can only belong to an owner that died, so the claim reaps its tree,
  removes its staging, and then the record. When a tree cannot be shown gone the
  claim releases the lock and refuses with `install_locked`. So a UI that
  replaces one killed mid-install reaps first, and never installs over a tree
  that may still be writing.
- The stop paths use the same claim for every backend root a record in scope
  names: `vibe stop`, `vibe stop --expect-runtime-id` (only records of that id,
  once the UI is gone), and the restart supervisor. A root that cannot be
  claimed within 10 s, or a tree that cannot be shown gone, fails the stop
  (exit 2) or the restart, and the record stays for the next pass.
- Removal is confined by the record's shape: only a normalized absolute path
  named `.staging-<32 hex>` directly inside a directory named after a backend
  qualifies. A record naming anything else is discarded as invalid and its
  path is never removed.
- A record that cannot be read is unknown, not invalid, and is kept: the claim
  refuses with `install_locked` and a stop exits 2.
- The OpenCode server and UI pidfiles are written atomically, so no reader
  sees a half-written record.

## Known limits

- Nothing locks the Runtime across processes. A process that starts carrying
  the id after the scan is never signalled; the rescan reports it and the stop
  exits 2.
- A process whose environment cannot be read is not seen by the scan. Processes
  a desktop Runtime starts are same-user and inspectable.
- A service started from a development `main.py` is recognized only while its
  checkout still has that layout on disk. Desktop bundles run
  `vibe/service_main.py`, which is recognized from argv alone.
- `write_shutdown_intent` keeps one record, so with several targets the last
  one written wins.
- An OpenCode server a successor adopted still carries the old id and is
  stopped with it, as before.
- An owner killed between creating its staging directory and writing its record
  leaves an almost empty staging directory that no record names.

## Known-by-design ledger

- Programs without a role that carry the id, such as agent CLIs and the tunnel
  connector, are listed in `left_running` and never signalled. They are either
  children of a stopped role process or, like the connector, kept for adoption.
- The scoped stop does not stop remote access: that path acts on the
  connector's files. The handover leaves the tunnel for the successor.
- A role process in the stop's own lineage is never signalled and fails the
  stop; a program without a role in the lineage is not listed.
- The one file read on the scoped path is the installer records, and their
  trees are signalled by their own markers.
- Full `vibe stop` keeps OpenCode non-fatal. The desktop host consumes the
  scoped result to replace or remove the bundle OpenCode runs from, so there a
  surviving OpenCode of this id fails the stop (exit 2). Plain `vibe stop` is
  also called by people and by the upgrade and restart flows; changing its exit
  code would widen this change beyond the desktop. A test pins both.
- A non-desktop `vibe start` still replaces an unhealthy UI through `stop_ui`,
  unchanged.
- `--expect-runtime-id` help stays English, like every other argparse help
  string. The diagnostics a stop prints are localized; the JSON lines are not.
- The expected id comes only from `--expect-runtime-id`, never from the CLI's
  own environment.

## Consumer notes for the desktop host (PR2)

- Pass `--expect-runtime-id` for handover, Quit and uninstall. Exit 3 means
  nothing was touched; parse the JSON reason. `service_runtime_id_mismatch` is
  Foreign; `service_identity_unavailable` is Unknown, to be retried and never
  treated as Absent.
- Exit 2 means a process or installer tree of this Runtime may still be
  running: do not replace or remove the bundle. `remaining` names each one.
- `left_running` lists the other programs carrying the id. For Quit and
  uninstall the tunnel connector is among them; stop it separately if the
  tunnel should not outlive the app.
- Uninstall should take the backend's `.install.lock` before removing its root.
