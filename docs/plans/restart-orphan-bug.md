# Restart Orphan Bug — Vibe Service Survives `vibe restart`

> Status: **Reviewed and implemented in this branch.** The design shifted from
> orphan scanning to a data-dir scoped single-service lock plus a restart
> supervisor job.
> Owner: TBD. Triaged on 2026-05-24.

## 1. Background

The `vibe restart` family of commands (CLI direct, CLI delayed, Web UI
`/control` action, and `do_upgrade` auto-restart) **can leave the previous
`service_main.py` process alive while spawning a new one**. From that point
on, the install is in a stable "two services for the same data dir" state
that is invisible to the user:

- Web UI reports "Service running" — but the PID it reports is the *new*
  service.
- `vibe.pid` only records the new service. Subsequent `vibe stop` /
  `vibe restart` therefore only target the new service; the orphan never
  gets killed.
- Both services race to handle inbound IM events from the same Slack /
  Discord / Lark workspace. Whichever wins a given event is non-deterministic.

The user-visible symptom that surfaced this bug:

- The system-prompt injection added in `feat(show): list show pages` (PR
  #331, merged 2026-05-24) shipped a new `## Show Pages` section.
- The user's running service (PID 60609, started 2026-05-20 12:21) was
  spawned **before** that code reached disk and was therefore serving the
  *old* prompt template (numbered sections, `vibe hook send`).
- The user upgraded vibe and ran `vibe restart --delay-seconds 60` via an
  Agent. PID 70784 was spawned at 2026-05-24 04:12:05 — but PID 60609 was
  never killed. Both processes have been alive since.
- Slack messages in the same Vibe Remote session were sometimes routed
  through PID 60609 (Agent saw the old prompt, no Show Pages) and sometimes
  through PID 70784 (Agent saw the new prompt). The Agent looked like it
  was randomly ignoring a system-level instruction.

This plan captures the full bug surface and proposes a layered fix.

## 2. Evidence

Captured directly from the user's machine on 2026-05-24 18:30~18:50 (UTC+08).

### 2.1 Process state

```
ps -eo pid,ppid,lstart,command | grep service_main
60609     1 Wed May 20 12:21:01 2026     Python .../site-packages/vibe/service_main.py
70784     1 Sun May 24 04:12:05 2026     Python .../site-packages/vibe/service_main.py
70785     1 Sun May 24 04:12:05 2026     Python -c from vibe.ui_server import run_ui_server; run_ui_server('0.0.0.0', 5100)
```

Both `service_main.py` processes are alive, both have `PPID=1` (launchd
adopted after the spawning parent exited).

### 2.2 Pid files

```
~/.vibe_remote/runtime/vibe.pid       → 70784
~/.vibe_remote/runtime/vibe-ui.pid    → 70785
```

The pidfile only references the new service. PID 60609 is no longer
tracked by the runtime even though it is alive.

### 2.3 System-prompt injection mismatch

The user's previous Claude session (PID 36112, parent PID 60609) was
spawned with this `--append-system-prompt`:

```
## 1. Send files                                          ← old numbered style
## 2. Quick-reply buttons
## 3. Scheduled tasks, watches, and hooks
   Use `vibe hook send --session-id ... --prompt ...`     ← removed CLI verb
   ...
   Use `--prompt-file <path>`                              ← renamed param
## 4. User Context and Preferences

(no `## Show Pages` section at all)
```

The current Claude session (PID 48024, parent PID 70784) was spawned with
the **new** injection (unnumbered sections, `vibe agent run --async`,
`--message-file`, and the `## Show Pages` block including
`vibe show path --session-id sesszcbv24wp8`).

Both prompts originated from the same `core/system_prompt_injection.py`
on disk — the difference is *which long-running Python process built the
prompt*. PID 60609 still holds the pre-upgrade module in memory.

### 2.4 Disk vs. memory drift

```
core/system_prompt_injection.py     mtime  2026-05-24 03:28:09  (current, has Show Pages)
core/__pycache__/system_prompt_injection.cpython-313.pyc
                                    mtime  2026-05-24 04:12:05  (rebuilt by PID 70784)
PID 60609                           start  2026-05-20 12:21:01  (in-memory: pre-upgrade)
```

The upgrade reached disk; only the live process did not.

## 3. Root cause — eight defects in the restart path

Severities: **CRIT** breaks the invariant "one service per data dir";
**HIGH** causes silent or incorrect status reports; **MED** is observability.

### B1 — `stderr=DEVNULL` swallows every failure in delayed restart [CRIT]

`vibe/api.py:118-125` (`_spawn_delayed_restart`):

```python
subprocess.Popen(
    helper_cmd,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,        # ← every failure is invisible
    start_new_session=True,
    close_fds=True,
    cwd=cwd,
)
```

The helper subprocess and the eventual `vibe restart` it spawns both
discard their stderr. `ImportError`, `FileNotFoundError`, permission
errors, uncaught exceptions in `cmd_stop` / `cmd_start` — all silent.
The caller receives "Restart scheduled in 1 minute" and has no way to
know whether the delayed work succeeded.

### B2 — `stop_process` unlinks pidfile even when kill fails [CRIT]

`core/runtime.py:390-399`:

```python
def stop_process(pid_path, timeout=5):
    if not pid_path.exists():
        return False
    pid = int(pid_path.read_text(encoding="utf-8").strip())
    if not pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        return False
    stopped = stop_pid(pid, timeout=timeout)
    pid_path.unlink(missing_ok=True)       # ← unconditional even when stopped=False
    return stopped
```

If `stop_pid` returns `False` (PermissionError, ProcessLookupError, or
signal-handler hang past timeout that still ate SIGKILL), the pidfile is
removed regardless. The next `start_service` then sees "no pidfile, no
service" and unconditionally spawns a fresh one — leaving the original
process alive and untracked.

**This is the direct mechanism by which PID 60609 and PID 70784 ended up
coexisting.**

### B3 — `stop_pid` can report success after SIGKILL without re-checking liveness [CRIT]

`core/runtime.py:329-364`:

```python
try:
    os.kill(pid, signal.SIGKILL)
except ProcessLookupError:
    return True
except OSError:
    pass
return True
```

After the SIGKILL send succeeds, the function returned `True` without
confirming that the process actually exited. This means `stop_process`
could receive a false success and remove the pidfile even when the target
was still alive. The P1 implementation now polls after SIGKILL and returns
`False` if the process remains live.

### B4 — `start_service` never scans for orphan vibe processes [CRIT]

`core/runtime.py:474-502`:

```python
def start_service():
    with _SERVICE_LOCK:
        pid_path = paths.get_runtime_pid_path()
        if pid_path.exists():
            try:
                existing_pid = int(pid_path.read_text(...).strip())
            except Exception:
                existing_pid = 0
            if existing_pid and pid_alive(existing_pid):
                if not _pid_mismatches_service(existing_pid):
                    return existing_pid
                logger.warning("Ignoring stale service pid file ...")
            pid_path.unlink(missing_ok=True)
        main_path = get_service_main_path()
        return spawn_background([sys.executable, str(main_path)], pid_path, ...)
```

The pidfile is the sole source of truth. If it lies (B2) or is missing,
`start_service` happily spawns a duplicate. No `pgrep -f service_main.py`,
no `psutil`-based scan, no port-in-use probe. Combined with B2, this
makes "two services for one data dir" a stable state instead of an
immediate failure.

### B5 — `cmd_stop` ignores return value; `_cmd_restart_with_delay` proceeds anyway [HIGH]

`vibe/cli.py:3407-3416` and `vibe/cli.py:4109-4117`:

```python
def cmd_stop():
    runtime.stop_service()          # ← return value discarded
    runtime.stop_ui()
    ...

def _cmd_restart_with_delay(delay_seconds: float) -> int:
    if delay_seconds > 0:
        return _schedule_delayed_restart(delay_seconds)
    print("Restarting vibe services...")
    cmd_stop()                       # ← failure not surfaced
    print("Waiting 3 seconds...")
    time.sleep(3)
    return cmd_start()               # ← runs even if stop failed
```

Restart proceeds even if the prior stop did not actually kill anything.

### B6 — `do_upgrade` returns "Restarting..." before restart attempts to run [HIGH]

`vibe/api.py:1591-1648`:

```python
if auto_restart:
    _spawn_delayed_restart(            # fire-and-forget, default delay=2s
        get_restart_invocation_command(vibe_path=current_vibe_path),
        safe_cwd,
        env=get_restart_environment(vibe_path=current_vibe_path),
    )
    restarting = True
return {
    "ok": True,
    "message": "Upgrade successful." + (" Restarting..." if restarting else " Please restart vibe."),
    "restarting": restarting,           # ← reported as success ~2s before the restart even fires
}
```

The HTTP response is constructed and returned before the restart attempt
runs. Web UI shows green "Restarted" purely on the HTTP 200, independent
of the actual outcome.

### B7 — Web UI `/control` runs stop+start inside the UI server process [HIGH]

`vibe/ui_server.py:1436-1445`:

```python
elif action == "restart":
    runtime.write_status("restarting", ...)
    runtime.stop_service()                  # same buggy stop as B2
    _stop_opencode_server()
    time.sleep(3)
    runtime.ensure_config()
    service_pid = runtime.start_service()   # same blind start as B3
    runtime.write_status("running", "restarted", service_pid, ...)
```

The UI server is a peer of the vibe service (both orphans with `PPID=1`);
killing the vibe service does not kill the UI server. The UI server then
writes `state="running"` and `detail="restarted"` to status, irrespective
of whether the kill or the start actually succeeded. The Web UI surfaces
this as a green status.

### B8 — Delayed helper exits immediately; no audit of outcome [MED]

`vibe/api.py:111-117` builds the helper code:

```python
helper_code = (
    "import subprocess, time\n"
    f"time.sleep({delay_seconds!r})\n"
    f"subprocess.Popen({command!r}, cwd={cwd!r}, env={env!r}, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)\n"
)
```

After the helper's `Popen` returns, the helper exits. Nothing waits for
or inspects the spawned `vibe restart`. Combined with B1, the entire
delayed restart path is fire-and-forget with no audit trail.

## 4. Goal

After this fix, the following invariants must hold:

1. At most one `service_main.py` process per `AVIBE_HOME` data dir
   at any time. A second invocation either reuses the existing one or
   fails loudly.
2. Every `vibe restart` (CLI direct, CLI delayed, Web UI, `do_upgrade`
   auto-restart) leaves a durable audit record: timestamp, old PID, new
   PID, stop result, start result, health-check result.
3. The HTTP response from `do_upgrade` and `/control` `action=restart`
   only reports success after the new service has been verified healthy
   (or returns a "pending" state if verification is still in flight).
4. An Agent can request a restart through the documented path and
   receive a verifiable success/failure result in the *next* turn,
   without crash-killing itself mid-restart.

## 5. Solution

The first-principles boundary is:

> Same `AVIBE_HOME` means same service identity. Exactly one process may
> own that service identity at a time.

Pidfiles are now treated as auxiliary status, not the source of running
authority. The service process itself owns the runtime lock for its entire
life. Restart is delegated to an external supervisor job, so a caller inside
an Agent process does not half-stop its own parent service.

### Implemented architecture

Implemented in this branch:

- `service_main` acquires `runtime/service.lock` before starting the
  controller and holds it until process exit.
- `start_service` refuses to spawn a second service when the lock is held
  without a matching live pidfile.
- the service writes `vibe.pid` only after it owns the lock; parent
  processes no longer predeclare pidfile ownership.
- `stop_pid` verifies liveness after SIGKILL and returns failure if the
  process survives.
- `stop_process` preserves pidfiles when a live process fails to stop.
- `vibe restart`, `vibe restart --delay-seconds`, Web UI `/control`
  restart, and upgrade auto-restart all schedule the same restart supervisor
  job.
- the supervisor writes `runtime/restart_status.json` and
  `logs/restart-*.log`, then performs stop/start outside the Agent or UI
  request path.
- `vibe status` includes the latest restart job status so an Agent can check
  the result in the next turn.

Follow-up, if desired:

- richer UI display for restart pending/succeeded/failed.
- optional launchd/systemd integration as a separate product-level service
  manager migration.

### Why not process scanning as the main fix?

Scanning can be useful for diagnostics, but it is the wrong primary
boundary. A process list answers "what looks like a service process right
now?" The product invariant is different: "who owns this data directory?"
The lock answers the invariant directly and avoids false positives across
different worktrees, installs, and `AVIBE_HOME` values.

## 6. Implementation plan

Implemented as one coherent restart rewrite:

1. Add `runtime/service.lock` and `runtime/restart_status.json` paths.
2. Add service lifetime lock acquire/release in `main.py`.
3. Move service pidfile writing into the service process after lock acquire.
4. Make `start_service` wait for pidfile confirmation after spawning.
5. Add `vibe/restart_supervisor.py` as the single restart job runner.
6. Route CLI restart, delayed restart, Web UI restart, and upgrade
   auto-restart through `schedule_restart`.
7. Include latest restart job status in `vibe status`.

## 7. Test plan

### 7.1 Unit / contract

- `tests/test_runtime_service_lock.py`
  - existing live pid is reused.
  - unrelated pidfile is ignored and replacement waits for service lock
    confirmation.
  - lock holder without matching live pidfile raises
    `ServiceAlreadyRunningError`.
  - lock acquisition blocks a second holder.
- `tests/test_restart_supervisor.py`
  - scheduling creates a restart job, log, detached supervisor, and status.
  - restart job stops then starts through the stable CLI invocation.
  - stop failure aborts before start and records a failed job.
- `tests/test_vibe_cli.py`
  - delayed restart schedules supervisor work without touching stop/start
    locally.
  - direct restart also schedules supervisor work.
  - stop failure preserves pidfile.
- `tests/test_ui_server_logs.py`
  - Web UI restart schedules supervisor work and returns job metadata.

### 7.2 Scenario harness

Future scenario harness candidates under `tests/scenarios/restart_orphan/`:

- `restart_orphan/clean_restart` — happy path; one PID before → one PID after,
  pidfile points to new PID, logs show stop=ok start=ok.
- `restart_orphan/double_spawn_recovery` — start a service, simulate the
  bug by manually leaving a fake service lock holder, call `vibe start` →
  must raise structured error and not duplicate.
- `restart_orphan/agent_triggered` — exercise
  `vibe restart --delay-seconds 60` end-to-end through the regression
  harness, assert `vibe status` reports the restart job result.

### 7.3 Manual regression

After this branch lands:

1. If an old service is still alive, stop it manually once; old code did
   not hold `service.lock`.
2. Start the new service and confirm `runtime/service.lock` and
   `runtime/vibe.pid` are written by the service process.
3. From an Agent run, trigger `vibe restart --delay-seconds 60`, then check
   `vibe status` after the delay; it should show a succeeded restart job
   and exactly one service PID.

## 8. Risks and open questions

1. **launchd / systemd dependency.** P3.2 is opt-in or required? If
   required, we break headless installs (Docker, plain Linux). Suggestion:
   keep the spawn_background path as a fallback when no service-manager
   integration is registered.
2. **What counts as "the same install"?** Resolved here: same
   `AVIBE_HOME` means same service identity, regardless of Python
   install path.
4. **Stop-grace window.** Some IM backends (Lark websocket reconnect
   loops, OpenCode server) may need more than 5s to flush. Should
   `stop_pid` default timeout be raised to 15s? Or expose
   `VIBE_STOP_TIMEOUT_SECONDS`?
4. **Old-version orphan cleanup.** A pre-fix orphan cannot hold the new lock.
   Operators may need to kill it once during rollout. After that, the lock
   prevents recurrence.

## 9. References

- This bug report and its visual analysis live at the user's Show Page
  for session `sesszcbv24wp8` (private; URL shared in IM).
- The merged feature whose absence first surfaced the bug:
  PR #331 `feat(show): list show pages` (2026-05-24).
- Root cause file paths (master at time of triage):
  - `core/runtime.py:329-399` — `stop_pid` / `stop_process`
  - `core/runtime.py:474-502` — `start_service`
  - `vibe/restart_supervisor.py` — unified restart job runner
  - `vibe/api.py:1591-1648` — `do_upgrade`
  - `vibe/ui_server.py:1416-1446` — `/control` restart endpoint
  - `vibe/cli.py:3329-3416, 4077-4117` — `cmd_start` / `cmd_stop` / `cmd_restart`
