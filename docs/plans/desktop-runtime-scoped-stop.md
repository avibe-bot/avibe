# Desktop Runtime scoped stop and installer ownership

Refs #2135, #2131. PR 2a is the Python half; PR 2b is the desktop host and
removes the startup receipt from both sides.

## Outcome and boundaries

A desktop app must update smoothly and never stop a Runtime it did not start.
Authority to stop follows provenance: the desktop host knows which Runtime id its
bundle started, and asks Python to stop only processes that carry that id. The
scoped stop reads no file: no pidfile, lock record or installer record decides
what is signalled. `/ready` and `/internal/health` are unchanged, and so is
plain `vibe stop`. The startup receipt (`@avibe-start-receipt:` and
`vibe stop --receipt`) is gone: provenance is the Runtime id, not a receipt the
host has to keep.

## `vibe stop --expect-runtime-id <RUNTIME_ID>`

`RUNTIME_ID` is the 64-character lowercase hex `AVIBE_DESKTOP_RUNTIME_ID` the
desktop host exports to the Runtime it launches. Every process that Runtime
starts inherits it. The stop also stops the home's tunnel connector, under the
rule in the ledger below.

### Discovery is one scan

`runtime.stop_desktop_runtime` scans this user's processes once, through
`core.process_isolation.processes_carrying_marker`, for those whose inherited
`AVIBE_DESKTOP_RUNTIME_ID` hashes to the expected id. Each one is classified by
its environment, then by the exact argv shape Avibe launches that role with.
Only the arguments Avibe writes decide; the program in front of them is whatever
interpreter, configured executable or shim runs them. An agent's command line is
arbitrary text, so words it merely contains, such as
`rg vibe.ui_server run_ui_server` or `rg opencode serve`, name no role:

| Role | Recognized by |
| --- | --- |
| `installer` | `AVIBE_DESKTOP_ROLE=installer` in its environment, whatever it runs |
| `service` | runs `vibe/service_main.py` (argv only, so a bundle an update replaced or moved is still recognized), or the development `main.py` |
| `ui` | `<interpreter> -c "from vibe.ui_server import run_ui_server; …"`, the launch `start_ui` and the UI restart have always used |
| `opencode` | `AVIBE_DESKTOP_ROLE=opencode`, which the server manager stamps on the server, and argv ending with `serve --hostname=… --port=…`, the arguments it appends to `agents.opencode.cli_path`, whether that runs natively, through an npm install's `node` shim or as a wrapper. The agent work the server runs inherits the stamp, and those arguments are common to servers, so neither decides alone |
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
- The scan asks psutil only for what the platform has: uids on POSIX, the
  username on Windows, where psutil refuses to collect uids at all.

### Order and postcondition

1. Service processes, then UI processes: SIGTERM, then SIGKILL after 5 s.
2. Backend installer trees of this Runtime whose owning UI has exited, by the
   owner each tree names (see below). A tree whose owner is still alive stays,
   and the rescan reports it. Their staging and records stay for the next
   claim of their backend root.
3. The OpenCode server.
4. A rescan decides the outcome. Any role process still carrying the id,
   including one started after the scan or one in the stop's own lineage,
   fails the stop.

`unknown` processes and programs without a role are never signalled.

### Presence and outcome

`runtime.DesktopRuntimePresence` is `MATCH` when the scan finds a role process.
Otherwise the service lock decides without reading its record: free is
`ABSENT`, held by another handle is `MISMATCH`, and a lock that cannot be
probed, including a lock call that fails for any other reason, is `UNKNOWN`.
The desktop host mirrors these as Mine, Absent, Foreign and Unknown.
`runtime.DesktopRuntimeStopOutcome` is `NOT_OURS` (nothing signalled),
`STOPPED` or `FAILED`.

| Situation | Exit | stderr | Effects |
| --- | --- | --- | --- |
| Invalid id | 3 | `{"reason":"invalid_runtime_id"}` | none |
| No role process carries the id, and the service lock is held | 3 | `{"reason":"service_runtime_id_mismatch"}` | none |
| No role process carries the id, and the lock cannot be probed | 3 | `{"reason":"service_identity_unavailable"}` | none |
| Nothing carries the id, and the lock is free | 0 | none | status `stopped` |
| Every role process carrying the id stopped | 0 | `{"left_running":[{"pid":…,"name":…}]}` when other programs carry the id | status `stopped` while the lock is free |
| A role process carrying the id is left | 2 | localized error, then `{"failed":"<part>","remaining":[{"pid":…,"role":…}]}` | status `error` while the lock is free |

The status file belongs to the service holding the service lock. The stop
writes it only while it holds the free lock itself, so no service can start,
and publish its own status, before the write lands. When a service holds the
lock, whether a successor started before an earlier stop of this id finished or
this Runtime's own service that did not stop, or the lock cannot be probed, the
stop leaves the status file alone, whether it succeeded or failed.

`<part>` is the first of `service`, `ui`, `installer`, `opencode` and `unknown`
that did not stop. Refusal happens before anything is signalled; installers
and the status file are untouched. The JSON lines are the machine contract and
are never localized; the exit-2 diagnostics go through `vibe/i18n/`
(`runtime.stop.*`) and are shared with the full stop.

## Handover

`vibe start` alone owns the act of replacing a superseded desktop Runtime, and
does it only when asked with `--hand-over`, a hidden flag the desktop host
passes on app launch or a user Retry. When a desktop `vibe start` finds a
controller whose health names another desktop Runtime id:

- without `--hand-over` it exits 3 and leaves that Runtime running;
- with it, it calls the same `runtime.stop_desktop_runtime` with that observed
  id, and exits 3 if the stop is refused and 2 if it fails;
- it also exits 3 if the UI pidfile still names a running UI afterwards: that
  UI is not the superseded Runtime's, and the start would otherwise reuse it or
  replace it with an unscoped stop.

Exit 3 means this start did not take the home over; exit 2 means the superseded
Runtime may be left partly running. `runtime.stop_desktop_runtime` never stops
the tunnel connector, so the successor adopts it. These errors go through
`vibe/i18n/` (`desktopRuntime.handover*`).

## Backend installer ownership (#2131)

- The UI process that starts an installer owns its tree. npm and the installed
  executable's `--version` probe start at one spawn point,
  `desktop_backends._spawn_owned_installer`: in their own process group, with a
  fresh `AVIBE_PROCESS_IDENTITY` marker, `AVIBE_DESKTOP_ROLE=installer`, the
  UI's `AVIBE_DESKTOP_RUNTIME_ID`, `AVIBE_DESKTOP_INSTALLER_OWNER=<pid>:<create
  time>` of the UI, and a durable record under
  `<runtime dir>/desktop-backend-installs/`. Every member inherits the four
  variables, so a process scan finds the whole tree and its owner.
- The role variable exists because nothing else says "installer": watch
  workers, command runners and the Model Hub supervisor carry
  `AVIBE_PROCESS_IDENTITY` too. The spawn point sets the Runtime id itself
  because the installer environment is an allowlist without `AVIBE_*`.
- Ownership travels with the tree. A tree is abandoned when no process has
  its owner's pid. While that pid keeps its create time
  (`runtime.process_create_time`, exact equality) the owner is alive. macOS can
  shift the create time psutil shows for a process that keeps running (#2129),
  so a different one alone proves nothing: the pid was reused only when the
  process holding it is readable and is not a UI of the tree's Runtime, known
  by the launch shape and id the scoped stop's scan uses
  (`runtime.is_desktop_ui`). A missing or malformed owner value, or a different
  create time on a process that cannot be read, fails closed: the reap reports
  failure and stops nothing. One function,
  `desktop_backends.reap_abandoned_desktop_backend_installs`, applies this for
  every caller, so no caller's registry, pidfile or knowledge of the UI decides.
  The owner registry serves only the owner's own drain.
- Records serve only the claim's cleanup of the staging they name.
- The record names the install's staging directory and lives exactly as long
  as the install: the install holds `.install.lock` from before the record is
  written until after it has removed its staging and then the record.
- After npm exits, times out, or the owner shuts down, the owner stops the
  leader, waits for its group, and reaps any member by group and by marker.
  `.install.lock`, the staging directory and the record are released only after
  the tree is shown gone. If it cannot be shown gone, the install fails with
  `install_drain_failed`, and the lock, the staging and the record stay.
- The owner drains on its stop signal (`handle_exit`) and on lifespan shutdown,
  then refuses new installs (`install_shutting_down`). The probe is an owned
  installer, so a drain during it stops it too.
- Every acquisition of `.install.lock` goes through one function,
  `desktop_backends._claim_backend_root`. Under the lock the claim first reaps
  every abandoned installer tree of its Runtime id. Its own trees, and those of
  another live UI of the same id, have a live owner and stay. Then every record
  of the root belongs to an install whose owner died holding the lock: the
  claim reaps its tree by marker, removes its staging, and then the record. So
  a UI that replaces one killed mid-install reaps first, and never installs
  over a tree that may still be writing.
- The claim lists records with `os.scandir`. Any `OSError` on the directory or
  on a record makes the listing unknown: the claim removes nothing and refuses
  with `install_locked`. Only a missing directory reads as "no records", and a
  record gone mid-listing is skipped because its install finished. A tree that
  cannot be shown gone also refuses the claim.
- The stops read no record. `vibe stop --expect-runtime-id` for the id it
  names, and `vibe stop` and the restart supervisor for their own, stop the UI
  and then reap the abandoned installer trees. `vibe stop` reaps before it
  reports a service or UI that did not stop. A tree whose owner is alive,
  such as the install of a UI a missing or corrupt pidfile hid from the full
  stop, stays. A tree that cannot be shown gone, or whose owner cannot be told,
  fails the stop (exit 2) or the restart. Staging and records stay for the next
  claim.
- Removal is confined by the record's shape: only a normalized absolute path
  named `.staging-<32 hex>` directly inside a directory named after a backend
  qualifies. A record naming anything else is discarded as invalid and its
  path is never removed.
- A record that cannot be read is unknown, not invalid, and is kept: the claim
  refuses with `install_locked`.
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
- An OpenCode server started by a release before the stamp is listed in
  `left_running` and not signalled. The handover of those releases never
  stopped OpenCode either.
- Agent work an OpenCode server runs inherits the stamp, so one of its
  programs whose argv ends with `serve --hostname=… --port=…` is taken for an
  OpenCode server.
- Role classification guards against accidental collisions, not mimicry. An
  agent process already runs with the user's full authority and could signal
  the service itself, so a process deliberately shaped like a role is not a
  boundary the scan defends. Every descendant of the service inherits every
  variable it carries, so no inherited marker separates the service from its
  agent tasks, and the service is known by its launch shape and the id alone:
  a process carrying the id that runs a file ending in `vibe/service_main.py`
  is stopped with the Runtime it belongs to.
- An owner killed between creating its staging directory and writing its record
  leaves an almost empty staging directory that no record names.
- `vibe stop` and a claim scan only for installer trees of their own Runtime
  id; from a terminal that means the ones without an id. A tree another
  Runtime id left, for example before an update, is stopped by the desktop
  host's scoped stop of that id, or reaped by the next claim of its backend
  root through the record it left.
- Plain `vibe stop` and the restart supervisor still find the UI by its
  pidfile, as before this change. With that pidfile missing or corrupt the UI
  keeps running; only its installs are no longer at risk.

## Known-by-design ledger

- Programs without a role that carry the id, such as agent CLIs, are listed in
  `left_running` and never signalled; they are children of a stopped role
  process.
- The tunnel connector serves the home, not one Runtime. After a successful
  scoped stop it is stopped through its own files, and only while the stop holds
  the free service lock and no UI of the home is running. A held lock leaves it
  to that service. A handover inside `vibe start` never stops it, so the
  successor adopts the tunnel, and the next UI start's
  `remote_access.reconcile()` brings a stopped tunnel back after Quit.
- argparse usage errors also exit 2, so a `vibe start` too old to know
  `--hand-over` reads as a handover that may have left the predecessor partly
  running. The bundled launcher is always the same version, so this cannot
  happen, and the reading fails closed. No other `vibe start` path exits 2 or 3.
- A `vibe restart` or plain `vibe stop` run by a process a superseded Runtime
  left running still acts on the Controller unscoped, because its desktop
  identity was stripped with its old root. Claiming by the caller's own id
  belongs at the start and restart boundary, tracked in #2135 for PR 3.
- A role process in the stop's own lineage is never signalled and fails the
  stop; a program without a role in the lineage is not listed.
- The scoped stop path reads no file: no pidfile, lock record or installer
  record. Installer trees are found by the scan like every other role; their
  staging and records wait for the next claim of their backend root.
- A claim whose leftover staging cannot be removed still proceeds. The failure
  is logged with the path, the record stays, and the next claim retries.
  Refusing with `install_locked` would let one undeletable directory block that
  backend for good; every install uses a fresh staging directory, so the
  leftover costs only disk.
- Full `vibe stop` keeps OpenCode non-fatal. The desktop host consumes the
  scoped result to replace or remove the bundle OpenCode runs from, so there a
  surviving OpenCode of this id fails the stop (exit 2). Plain `vibe stop` is
  also called by people and by the upgrade and restart flows; changing its exit
  code would widen this change beyond the desktop. A test pins both.
- A non-desktop `vibe start` still replaces an unhealthy UI through `stop_ui`,
  unchanged.
- The start side of the handover claims by pidfile, lock and command line, as
  on master. A service another desktop Runtime starts after the scan can be
  reused under the wrong id, and a live foreign UI whose command line cannot be
  read passes the UI check. A handover check is check-then-act, so the fix
  belongs at the start claim points (`_resolve_service_pid`, `start_ui` reuse
  or replace): with an expected id, claim only processes that carry this
  Runtime id. Tracked in #2135.
- If this stop's ancestors cannot all be read, its lineage falls back to its
  pid and parent pid, with a warning; a role process of this Runtime further up
  the chain could then be signalled.
- `--expect-runtime-id` help stays English, like every other argparse help
  string. The diagnostics a stop prints are localized; the JSON lines are not.
- The expected id comes only from `--expect-runtime-id`, never from the CLI's
  own environment.
- An owner's pid taken over by another UI of the same Runtime, after the owner
  was killed, reads as the owner with a shifted create time, so its tree stays
  until the next scoped stop, which stops that UI first, or the next claim of
  its backend root reaps it through its record. That takes a full pid wrap
  landing on a UI. An owner launched outside the UI's `-c` shape, which Avibe
  never does, reads as reused on a shifted create time, as before this rule.

## Desktop host (PR 2b)

`HealthProbe::presence` classifies the origin: `Mine{ready}` (this bundle's id),
`Foreign{id, ready}`, `Unmanaged` (no id), `Absent` (connection refused) or
`Unknown` (any other failure). Each probe has 5 s, because Windows refuses a
loopback connect only after about 2 s of SYN resends. The host runs one stop verb,
`vibe stop --expect-runtime-id <id>`, mapped as exit 0 → `Completed`, 3 →
`Refused{reason}`, 2 → `Failed{part}`, any other exit → `Failed{unknown}`, and a
spawn error → `Unrunnable`. The host never stops a predecessor itself.

- B1, initial: `Mine{ready}` → adopt; `Mine{!ready}`, `Absent`, `Unknown` and
  `Foreign` → launch; `Unmanaged` → adopt as external. The host owns only the
  handover policy: its launch passes `--hand-over` on app launch or a user
  Retry, never from monitor recovery. Recovery that sees `Foreign` shows
  `runtime_ownership_lost` and launches nothing.
- The start helper's exit decides a handover: 3 → `runtime_ownership_lost`,
  2 after `--hand-over` → `runtime_stop_failed`, any other non-zero exit →
  `launcher_exited`.
- B4, polling: `Mine{ready}` → Ready; `Unmanaged` → Adopted. B5: `Foreign` is
  judged only once this app's helper has exited, because until then it may be
  the predecessor the helper is replacing. One still serving after that was not
  replaced, and shows `runtime_ownership_lost`.
- B6, monitor: the Runtime is serving while presence stays the adopted class.
- B7, uninstall: only `Mine`, or `Absent` with no launch in flight, is stopped
  by the bundle's id, then its backends and bundle are removed. Any other
  presence, a refusal or a failure keeps every file. When the CLI cannot run
  at all and presence is `Absent` with no launch in flight, a native dialog
  offers "Delete Anyway" or "Keep Files".
- B8, Stop and Quit: offered only while the last presence is `Mine`. A refusal
  shows `runtime_ownership_lost`; a failure shows `runtime_stop_failed`.
- `Unknown` → launch relies on the start boundary, tracked in
  https://github.com/avibe-bot/avibe/issues/2135#issuecomment-5877263657.
