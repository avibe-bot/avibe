# Process Identity Across Clock Changes

Status: proposed, low priority. The owner asked for this plan on 2026-10-04,
after #2356.

## Background

### One case today

1. At 10:00:00 Avibe starts an OpenCode server, pid 4321. Its record says
   "pid 4321, created at 10:00:00".
2. At 10:20 the Avibe process is killed by the kernel's out-of-memory killer.
   No shutdown runs, so the OpenCode server keeps running its turn.
3. Shortly after, the system steps its clock back by two seconds.
4. At 10:21 Avibe is started again and finds the record. psutil now reports
   pid 4321 as created at 09:59:58. The comparison is exact, so Avibe decides
   the pid belongs to another process and drops the record without stopping
   anything.
5. The old server keeps running with no owner. Its turn is never recovered,
   and `vibe stop` cannot find it.

### Why

- **psutil's create time moves with the clock on Linux.** It returns the
  process's start in clock ticks since boot plus the boot time. That boot time
  is the kernel's `btime`: the current wall clock minus the uptime, in whole
  seconds. Every clock step or NTP adjustment therefore moves the create time
  of every process. The Incus regression VM stepped by about 2 s every few
  minutes, roughly 96 s in one hour.
- **macOS can shift it too.** The repository already records this in
  `core/process_isolation.py::process_identity_matches` and
  `vibe/desktop_backends.py::_installer_owner_gone`.
- **Avibe uses the value for two different questions:**
  - *Is the process at this pid still the one I recorded?* This is identity,
    answered by an exact comparison.
  - *How long ago did it start?* This is age, compared with `time.time()`.

  The first question breaks when the clock moves. The second does not, because
  it is measured on the same clock.

### Scope of the exposure

- A normal stop or restart does not depend on this. `Controller.cleanup_sync`
  shuts the backends down and stops every OpenCode server this controller
  owns. A service restart keeps crash-recovery semantics; see
  `backend-rolling-restart.md`, "Service restart boundary".
- The servers a running controller started itself are judged by their process
  handle since #2356, both each turn and at shutdown.
- What remains is bookkeeping across processes: recovery after an Avibe
  process died without cleanup, and the few records one Avibe process checks
  while another runs, such as a restart in progress.
- A mismatch always errs toward "the recorded process is gone". The cost is a
  leaked process or a duplicate start, never a signal to a stranger, because
  every stop path refuses to signal a pid it cannot prove.

### An approach that was rejected

#2356 first replaced the shared reader `vibe.runtime.process_create_time` with
a clock-stable value: a per-boot anchor plus the tick count from `/proc`.
Review found a new consumer it broke on every round, among them wall-clock age
checks, records written by released builds, and the watch reuse heuristic.
Changing the meaning of a value that many consumers and persisted records
share is the wrong unit of change. The PR was narrowed to process handles.

## Goal

Every check of "is the process at this pid still the one recorded" is immune
to clock changes on every platform, and it is changed one consumer at a time
without altering any value other consumers or released records rely on.

## Design

The repository already has the model:
`core/process_isolation.py`'s inherited identity marker. It is a 256-bit token
in the spawned process's environment (`AVIBE_PROCESS_IDENTITY`), inherited by
its whole process tree. The record keeps only its fingerprint. The marker is
the authority and the birth time only a hint:
- `process_identity_matches` proves the recorded process by its marker.
- `process_identity_recycled` proves a reused pid only when the process holding
  it is readable and lacks the marker.

Command workers (`core/command_runner.py`), the Model Hub engine supervisor,
and the desktop backends use it today.

One rule decides identity, in this order:

1. **A process handle**, when this process holds one: it is exact until the
   handle reaps the child. #2356 uses this. On Windows, an open handle to any
   process also keeps its pid from being reused, and becomes signaled when the
   process exits.
2. **The inherited marker**, when the process was spawned with one.
3. **A structural relation**, where one exists. A child waiting for its parent
   compares `os.getppid()` with the recorded parent pid on POSIX. Windows never
   re-parents, so `os.getppid()` keeps the old pid after the parent exits; the
   child opens a handle to the parent at start (rule 1) and waits on it.
4. **The birth time, only for records with none of 1–3**, such as those written
   by released builds. It gives three answers, never two:
   - *the same process*: the birth time matches;
   - *another process*: the birth time differs and a second, readable proof
     agrees, such as a command line, port, or role that no longer matches;
   - *unknown*: anything else. The record is kept, nothing adopts or signals
     its process, and a later pass asks again.

   A boolean check cannot express the third answer, so each consumer still
   using rule 4 needs one. This is the existing `process_identity_recycled`
   and `_installer_owner_gone` stance.

Ages keep the wall-clock birth time and are out of scope, for example
`vibe.runtime._pid_reservation_is_fresh`. Ordering a process's birth against a
file's mtime to decide whether it is still the same process is identity, not
age.

### Invariants

- Neither a clock step nor a create-time shift makes a live recorded process
  look gone or foreign.
- A reused pid is never taken for the recorded process, and a process that
  cannot be proven is never signalled.
- Every record written by a released build is still read. New fields are
  optional and additive, and `process_created_at` keeps its meaning.

## Consumers

| Consumer | Where | Decides | Today | Plan |
| --- | --- | --- | --- | --- |
| OpenCode adopted servers | `modules/agents/opencode/server.py`: `_record_proves_process`, `OpenCodeGeneration.process_alive` with no handle, `_owned_here` pid fallback, `_stop_unrecorded_process_sync` for a non-child | Adopt, stop, `vibe stop`, and liveness of a server from a previous controller | Exact birth time, then command and port; a boolean that deletes the record when false | Spawn with a marker and store its fingerprint in the generation record, then prove by marker. A legacy record gets rule 4's three answers: `adopt_recorded_generations` and `forget_dead_records` delete only *another process* and keep an *unknown* record without adopting or stopping it |
| Claude CLI registry | `modules/agents/claude_process_reaper.py`: `register_claude_owned_process`, `_process_identity_matches`; `core/services/running_agents.py`: `_collect_orphans` (the Running Agents list) and `_end_orphan_pid` (End, right before it signals) | Which leftover CLI processes are shown, reaped, or ended | `ps -o lstart` within 1 s, repeated in each consumer | Add a marker to the CLI environment beside `AVIBE_CLAUDE_PROCESS_OWNER` and store its fingerprint in the registry. All four consumers prove by it through one shared check |
| Restart supervisor | `vibe/restart_supervisor.py` writes `supervisor_started_at`; `vibe/ui_server.py` (restart in flight), `vibe/upgrade.py` (seed retention), and `scripts/incus_regression_supervisor.py::_restart_in_progress` compare it | Whether a restart job is still running | Exact birth time, or within 1 s | Spawn the supervisor with a marker, have the status carry its fingerprint, and migrate all three readers |
| Deferred upgrade activation (Windows) | `vibe/upgrade.py` passes `--parent-started-at`; `vibe/cli.py` waits on it | When the launching parent has exited | Pid alive and birth time unchanged | Rule 3: the helper opens a handle to the parent at start and waits on it. The candidate keeps accepting and ignoring `--parent-started-at` indefinitely: an older installed release runs the newer candidate's parser, so a user who skips releases would otherwise fail to upgrade |
| Install generations | `vibe/install_generations.py::_installer_is_live` | Whether the installer that wrote a generation's marker still runs, before collection may retire unselected generations | Installer birth time no later than the marker's mtime | Rule 4 with the installer's command line as the second proof. An *unknown* installer counts as live, so collection never deletes a candidate during a handoff |
| Desktop installer owner | `vibe/desktop_backends.py::_installer_owner_gone` | Whether a Runtime's installer owner exited | Birth time, then a readable second proof | No change: it already follows rule 4 |
| Legacy watch entries | `core/watches.py::_legacy_pid_was_reused`, with blocked entries kept by `_unreaped_runtime_entries` | Recovery of watch workers recorded before `process_identity` existed; a `--forever` worker can outlive an upgrade | Birth time later than `updated_at` | Rule 4: *another process* only when the process holding the pid is readable and is not that watch's worker by its command line; *the same process* when it is; otherwise *unknown*, kept blocked and never signalled. Current entries already use the marker |

## Rollout

- One PR per consumer, in this order: OpenCode adopted servers, the Claude
  registry and Running Agents, the restart supervisor (all three readers),
  install generations with the deferred upgrade activation, and legacy watch
  entries.
- Each PR adds its marker or relation and keeps reading the legacy shape under
  rule 4. It changes no shared helper's meaning; at most it extends
  `core/process_isolation.py` with what the consumer needs.
- Each PR names, for every check it touches, whether that check is identity or
  age.

## Validation

- **Unit tests** for each consumer, covering:
  - a create-time shift with the marker present, which must still match;
  - a reused pid holding a readable environment without the marker, which
    must be taken for another process;
  - an unreadable environment, which proves nothing either way;
  - each released record shape, read without a marker, including the *unknown*
    answer that keeps a record without acting on it.
- **Windows tests** for the deferred activation wait: the helper proceeds once
  its parent exits, although `os.getppid()` still returns the old pid.
- **Linux tests with real child processes** for marker inheritance through a
  launcher, such as an npm shim or a shell wrapper.
- **End to end on the Incus regression VM**, whose clock drifts on its own:
  1. Kill the controller with `kill -9` while an OpenCode turn runs.
  2. Wait for a clock adjustment.
  3. Start the service again. The server must be adopted, its turn recovered,
     and a later stop must stop it.

## Open questions

- Whether a process's initial environment is readable under the service user
  for every backend binary on macOS and Windows: a hardened or signed binary
  might refuse it. Measure before each consumer relies on it. An unreadable
  environment falls back to rule 4 and never signals.
- Whether the OpenCode launcher or the Claude CLI clears the environment for
  any descendant that holds the listening socket or does the work. Measure
  with a real server and CLI.
