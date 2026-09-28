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

- Three slots are inspected once, before anything is signalled: the service
  lock owner, the process the UI pidfile names, and the OpenCode server its
  pidfile names. Service processes beyond the lock owner are not touched; they
  belong to a full stop.
- Each target's provenance is read once through
  `vibe.desktop_runtime.open_desktop_runtime_provenance`, currently the
  `AVIBE_DESKTOP_RUNTIME_ID` the process inherited. This is the single place to
  swap if another provenance source is needed.
- A target is signalled only through the handle its provenance was read from,
  and only after re-checking that the pid is still that process (birth time and
  id). A pid that has been reused, or can no longer be shown to be the target, is
  never signalled.

### Slot model

Every slot is classified into one `runtime.DesktopSlotState`, and what the stop
did to it is one `runtime.DesktopSlotOutcome`. The desktop host (PR2) mirrors
the states:

| Python `DesktopSlotState` | Desktop host | Meaning |
| --- | --- | --- |
| `MATCH` | Mine | live, and its provenance names the expected id |
| `MISMATCH` | Foreign / Unmanaged | live, and its provenance names another id / no id |
| `ABSENT` | Absent | positive evidence that nothing is there |
| `UNKNOWN` | Unknown | live, or possibly live, and not shown to be any of the above |

- `ABSENT` needs positive evidence: no pidfile, a pidfile naming no live
  process, a pidfile process whose command is readable and is not the expected
  program (a stale pidfile), or, for the service, an available service lock.
- Everything else that cannot be classified is `UNKNOWN`: a held service lock
  whose owner record cannot be read, a pidfile that cannot be read, provenance
  that cannot be read (`AccessDenied`, a zombie), or a matching process whose
  command cannot be read.
- `UNKNOWN` in any slot refuses the whole stop before the first signal. It
  never produces a partial stop, or success after touching the other slots.
- Outcomes are `NOT_OURS` (never signalled), `STOPPED` and `FAILED`. A `MATCH`
  that does not stop is `FAILED` and the stop exits 2.

| Situation | Exit | stderr (last line) | Effects |
| --- | --- | --- | --- |
| Invalid id | 3 | `{"reason":"invalid_runtime_id"}` | none |
| Service has another id or none | 3 | `{"reason":"service_runtime_id_mismatch"}` | none |
| Service provenance unreadable, or the lock is held and its owner unreadable | 3 | `{"reason":"service_identity_unavailable"}` | none |
| UI unknown | 3 | `{"reason":"ui_identity_unavailable"}` | none |
| OpenCode unknown | 3 | `{"reason":"opencode_identity_unavailable"}` | none |
| Service and UI match | 0 | — | service, UI, remote access, OpenCode of this id, abandoned installs of this id |
| Service matches, no UI | 0 | — | service, remote access, OpenCode of this id, abandoned installs of this id |
| Nothing running | 0 | — | OpenCode of this id, abandoned installs of this id |
| Service matches, UI has another id or none | 0 | `{"skipped":"ui","reason":"ui_runtime_id_mismatch"}` | service and OpenCode of this id |
| Another UI took the pidfile while the service stopped | 0 | `{"skipped":"ui","reason":"ui_changed"}` | service, the verified UI if still alive, OpenCode of this id |
| A verified target, including OpenCode, or an installer tree did not stop | 2 | localized error | status `error` |

Refusal (exit 3) happens before anything is signalled; remote access, OpenCode,
installers and the status file are untouched. The JSON lines are the machine
contract and are never localized; the exit-2 diagnostics go through
`vibe/i18n/` (`runtime.stop.*`) and are shared with the full stop.

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
- Abandoned installs are reaped only for this id, and only when the UI side
  was not left to another UI.

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
  qualifies. A record naming anything else is discarded as unreadable and its
  path is never removed.

## Known limits

- The service lock guards only this process. Between the last re-check and a
  signal or the tunnel stop, a successor can still appear; closing that window
  needs a cross-process lifecycle lock.
- A tunnel left when nothing of this Runtime is running, and an OpenCode
  server adopted from another Runtime whose Controller was killed before
  cleanup, are left for adoption or a full stop.
- A held service lock whose owner record is unreadable refuses the stop. A live
  service writes that record right after it takes the lock and rewrites it in
  place on each phase change, so this is a short window; a retry resolves it.
- A zombie is `UNKNOWN` at inspection. After a signal, a verified target that is
  now a zombie counts as gone: it has exited and runs nothing.
- An owner killed between creating its staging directory and writing its record
  leaves an almost empty staging directory that no record names.

## Known-by-design ledger

- Full `vibe stop` keeps OpenCode non-fatal. The desktop host consumes the
  scoped result to replace or remove the bundle OpenCode runs from, so there a
  surviving verified OpenCode fails the stop (exit 2). Plain `vibe stop` is also
  called by people and by the upgrade and restart flows; changing its exit code
  would widen this change beyond the desktop. A test pins both.
- `--expect-runtime-id` help stays English, like every other argparse help
  string. The diagnostics a stop prints are localized; the JSON reason lines are
  not.
- The expected id comes only from `--expect-runtime-id`, never from the CLI's
  own environment.

## Consumer notes for the desktop host (PR2)

- Pass `--expect-runtime-id` for handover, Quit and uninstall. Exit 3 means
  nothing was touched; parse the JSON reason. `*_runtime_id_mismatch` is
  Foreign or Unmanaged; `*_identity_unavailable` is Unknown, to be retried and
  never treated as Absent.
- Exit 2 means a verified process or installer tree of this Runtime may still
  be running: do not replace or remove the bundle.
- Uninstall should take the backend's `.install.lock` before removing its root.
