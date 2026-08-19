from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import paths

# Imported here, at the top, rather than where the rollback path uses it. This
# process runs the version being rolled back FROM, and a pinned reinstall
# replaces the source tree underneath it, so a first import taken after that
# install reads the OTHER release's source into this process. Importing at module
# load makes every later use a `sys.modules` hit that touches no file. The module
# is pure stdlib, so paying for it on every restart costs nothing.
from storage.backups import find_restorable_backup, next_backup_sequence, restore_sqlite_backup
from vibe import runtime
from vibe.upgrade import (
    build_upgrade_plan,
    get_restart_command,
    get_restart_environment,
    get_restart_invocation_command,
    get_safe_cwd,
)


logger = logging.getLogger(__name__)
_RESTART_LOG_RETENTION = 10
_SERVICE_LOCK_RELEASE_TIMEOUT_SECONDS = 30.0
# Long enough for a package index to be slow, short enough that a hung download
# does not leave the machine sitting with no service forever. A rollback that
# times out is recorded as failed, which is a diagnosable state; a rollback that
# waits without a bound is not.
_ROLLBACK_INSTALL_TIMEOUT_SECONDS = 600.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _restart_log_path(job_id: str) -> Path:
    paths.get_logs_dir().mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return paths.get_logs_dir() / f"restart-{timestamp}-{job_id}.log"


def _pending_restart_path() -> Path:
    return paths.get_runtime_dir() / "pending_restart.json"


def mark_pending_restart(
    *,
    trigger: str,
    scope: str = "service",
    reason: str = "restart_in_progress",
    restart_job_id: str | None = None,
) -> dict:
    payload = {
        "trigger": trigger,
        "scope": scope,
        "reason": reason,
        "restart_job_id": restart_job_id,
        "created_at": _now_iso(),
        "created_at_epoch": time.time(),
    }
    path = _pending_restart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json(path, payload)
    return payload


def _consume_pending_restart_for_job(job_id: str) -> dict | None:
    path = _pending_restart_path()
    payload = runtime.read_json(path)
    if not isinstance(payload, dict):
        return None
    restart_job_id = payload.get("restart_job_id")
    if restart_job_id and restart_job_id != job_id:
        return None
    try:
        path.unlink()
    except OSError:
        logger.debug("Failed to remove pending restart marker", exc_info=True)
    return payload


def _prune_restart_logs(limit: int = _RESTART_LOG_RETENTION) -> None:
    try:
        logs = sorted(
            paths.get_logs_dir().glob("restart-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        logger.debug("Failed to list restart audit logs", exc_info=True)
        return
    for path in logs[limit:]:
        try:
            path.unlink()
        except OSError:
            logger.debug("Failed to prune restart audit log %s", path, exc_info=True)


def _write_status(payload: dict) -> None:
    status = {**payload, "updated_at": _now_iso()}
    path = runtime.get_restart_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json(path, status)


def _read_recorded_pid() -> int | None:
    pid_path = paths.get_runtime_pid_path()
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    if pid and pid > 0:
        return pid
    return runtime.resolve_service_owner_pid()


def _read_recorded_ui_pid() -> int | None:
    pid_path = paths.get_runtime_ui_pid_path()
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _remaining_service_pids_after_stop() -> list[int]:
    owner_pid = runtime.resolve_service_owner_pid(include_starting=True)
    pids: list[int] = []
    if owner_pid:
        pids.append(owner_pid)
    pids.extend(runtime.extra_service_process_pids(owner_pid=owner_pid))
    return sorted(set(pids))


def _read_starting_service_status() -> dict | None:
    status = runtime.read_status()
    if status.get("state") != "starting":
        return None
    return status


def _service_pid_from_status(status: dict | None) -> int | None:
    if status is None:
        return None
    pid = status.get("service_pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _rounded_seconds(seconds: float) -> float:
    return round(max(0.0, seconds), 3)


def _fail(payload: dict, error: str, log, return_code: int, *, started_at: float | None = None) -> int:
    if started_at is not None:
        durations = dict(payload.get("stage_durations") or {})
        durations["restart_total_seconds"] = _rounded_seconds(time.monotonic() - started_at)
        payload["stage_durations"] = durations
    payload.update(ok=False, state="failed", error=error)
    _write_status(payload)
    log.write(f"{_now_iso()} {error}\n")
    log.flush()
    return return_code


def _runtime_ready_for_config(config) -> bool:
    has_configured_platform_credentials = getattr(config, "has_configured_platform_credentials", None)
    if callable(has_configured_platform_credentials):
        return bool(has_configured_platform_credentials())
    return bool(getattr(getattr(config, "slack", None), "bot_token", ""))


def _start_runtime_processes(start_ui: bool = True) -> tuple[int, int | None]:
    from core.memory.ui_access import generate_ui_read_secret, process_ui_read_secret
    from core.services import settings as settings_service

    paths.ensure_data_dirs()
    config = settings_service.load_config(default_factory=settings_service.default_config)
    memory_ui_secret = process_ui_read_secret()
    if memory_ui_secret is None and start_ui:
        memory_ui_secret = generate_ui_read_secret()

    # Service-only restart: the UI process was never stopped, so carry its
    # existing pid through EVERY status write — including the early
    # starting/setup writes and any failure path — so a crash mid-start can't
    # leave the status reporting ui_pid=None while the UI is still serving.
    preserved_ui_pid = None if start_ui else _read_recorded_ui_pid()

    if _runtime_ready_for_config(config):
        runtime.write_status("starting", None, None, preserved_ui_pid)
    else:
        runtime.write_status("setup", "missing platform credentials", None, preserved_ui_pid)

    service_pid = runtime.start_service(
        wait_for_ready=False,
        initial_ready_timeout=0,
        memory_ui_secret=memory_ui_secret,
    )
    if start_ui:
        bind_host = runtime.effective_ui_bind_host(config)
        ui_pid = runtime.start_ui(
            bind_host,
            config.ui.setup_port,
            wait_for_ready=False,
            memory_ui_secret=memory_ui_secret,
        )
    else:
        ui_pid = preserved_ui_pid

    if runtime.service_pid_recorded(service_pid):
        runtime.write_status("running", f"pid={service_pid}", service_pid, ui_pid)
    elif runtime.pid_alive(service_pid):
        runtime.write_status("starting", "waiting for service process", service_pid, ui_pid)
    else:
        runtime.write_status("error", "service process exited before startup completed", service_pid, ui_pid)
        raise RuntimeError(f"Vibe service process pid={service_pid} exited before acquiring the service lock")

    return service_pid, ui_pid


def _stop_ui_for_restart() -> tuple[bool, dict[str, float | bool], float, int | None]:
    timings: dict[str, float | bool] = {}
    started_at = time.monotonic()
    stopped = runtime.stop_ui(timings, stop_remote_access=False)
    return bool(stopped), timings, _rounded_seconds(time.monotonic() - started_at), _read_recorded_ui_pid()


def _stop_service_for_restart() -> tuple[bool, float]:
    started_at = time.monotonic()
    stopped = runtime.stop_service()
    return bool(stopped), _rounded_seconds(time.monotonic() - started_at)


def _stop_runtime_for_restart(stop_ui: bool = True) -> tuple[bool, dict[str, float | bool], float, int | None, bool, float]:
    if not stop_ui:
        # Service-only restart: leave the UI process untouched so the open Web
        # UI survives. Report its still-recorded pid; ``ui_stopped`` is True only
        # to satisfy the "did the UI stop" guard (we deliberately did not stop it).
        service_stopped, stop_service_seconds = _stop_service_for_restart()
        return True, {}, 0.0, _read_recorded_ui_pid(), service_stopped, stop_service_seconds
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="avibe-restart-stop") as executor:
        ui_future = executor.submit(_stop_ui_for_restart)
        service_future = executor.submit(_stop_service_for_restart)
        ui_stopped, ui_timings, stop_ui_seconds, ui_pid = ui_future.result()
        service_stopped, stop_service_seconds = service_future.result()
    return ui_stopped, ui_timings, stop_ui_seconds, ui_pid, service_stopped, stop_service_seconds


def _restore_database_for_rollback(backup_watermark: int | None, write) -> dict:
    """Put back the database the version being rolled back from migrated, if it did.

    Whether a migration happened is decided from a number this job read out of
    the backup window itself before the new version was allowed to start. Any
    rollback point recorded at or above it was written during that window, so it
    was written by the version now being rolled back from -- measured by our own
    code, before and after, with nothing in between that the failing version got
    to report about itself.

    Every cheaper test compares labels and every one of them is wrong on a real
    case: the revision stamp and the schema both stay put when a migration
    commits rows and then fails, and the wall clock reverses the order of two
    attempts if it is corrected backwards between them. `storage.backups` refuses
    to answer from those for the same reason.

    No watermark means the job never reached the point of starting the new
    version, so no migration of its doing can exist and there is nothing to put
    back. That is a normal outcome, not a degraded one.
    """

    if backup_watermark is None:
        return {"restored": False, "reason": "no_migration_window"}

    backups_dir = paths.get_state_backups_dir()
    rollback_point = find_restorable_backup(backups_dir, written_at_or_after=backup_watermark)
    if rollback_point is None:
        write(f"no rollback point was written at or after backup sequence {backup_watermark}; database left as it is")
        return {"restored": False, "reason": "no_rollback_point"}

    db_path = paths.get_sqlite_state_path()
    write(f"restoring the database from {rollback_point}")
    replaced = restore_sqlite_backup(rollback_point, db_path)
    write(f"database restored; the one it replaced is at {replaced}" if replaced else "database restored")
    return {
        "restored": True,
        "restored_from": str(rollback_point),
        "replaced_database": str(replaced) if replaced else None,
    }


def _roll_back_failed_upgrade(
    *,
    rollback_to: str,
    vibe_path: str | None,
    start_ui: bool,
    backup_watermark: int | None,
    write,
    record,
) -> dict:
    """Put the machine back on the version it was upgrading from.

    Reached only when a restart has failed AND nothing holds the service lock.
    That second half is the whole condition, and it is deliberately not a list of
    the failure branches that ought to qualify: such a list is complete only
    until the next branch is added, and the branch nobody remembered is exactly
    the one that leaves an instance dark. "No service is running" is the property
    the upgrade must not be able to produce, so it is what gets asked -- and it
    answers correctly for free on the failures that changed nothing, like a stop
    that did not stop, where the old service is still serving and rolling back
    underneath it would be the damage.

    Install, then restore, then start. The install is the step that needs a
    package index and so the step most likely to fail, and it is the only one
    with no effect on the data: failing there leaves the database exactly as the
    upgrade left it, and a rollback that changed nothing is a far better thing to
    find than one that half-changed the schema. Restoring before starting is what
    makes the started version able to read what it finds.

    Each step is recorded before the next begins, because this process can be
    killed mid-rollback and what it has already done to the database has to be
    readable afterwards from the status record rather than inferred from the
    disk.
    """

    rollback: dict = {"target_version": rollback_to, "state": "running", "started_at": _now_iso()}
    record(rollback)
    write(f"rolling back to {rollback_to}: no service is running after the failed restart")

    try:
        plan = build_upgrade_plan(vibe_path=vibe_path, version=rollback_to)
    except Exception as exc:
        rollback.update(state="failed", error=f"cannot build a pinned install for {rollback_to}: {exc}")
        record(rollback)
        return rollback

    rollback["install"] = {"method": plan.method, "ok": None}
    record(rollback)
    try:
        result = subprocess.run(
            plan.command,
            capture_output=True,
            text=True,
            env=plan.env,
            cwd=get_safe_cwd(),
            timeout=_ROLLBACK_INSTALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        rollback["install"] = {"method": plan.method, "ok": False, "error": str(exc)}
        rollback.update(state="failed", error=f"installing {rollback_to} failed: {exc}")
        record(rollback)
        return rollback
    if result.returncode != 0:
        # The installer's own stderr, trimmed: it is the only account of why the
        # rollback could not proceed, and the full text can be megabytes of
        # resolver output.
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        rollback["install"] = {"method": plan.method, "ok": False, "error": detail or f"exit code {result.returncode}"}
        rollback.update(state="failed", error=f"installing {rollback_to} failed with exit code {result.returncode}")
        record(rollback)
        write(f"pinned install of {rollback_to} failed: {detail}")
        return rollback
    rollback["install"] = {"method": plan.method, "ok": True}
    record(rollback)
    write(f"installed {rollback_to} using {plan.method}")

    try:
        rollback["database"] = _restore_database_for_rollback(backup_watermark, write)
    except Exception as exc:
        rollback["database"] = {"restored": False, "error": str(exc)}
        rollback.update(state="failed", error=f"restoring the database failed: {exc}")
        record(rollback)
        return rollback
    record(rollback)

    try:
        service_pid, ui_pid = _start_runtime_processes(start_ui=start_ui)
    except Exception as exc:
        rollback.update(state="failed", error=f"starting {rollback_to} failed: {exc}")
        record(rollback)
        return rollback
    ready_pid = runtime.wait_for_service_ready(service_pid, timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS)
    if ready_pid is None:
        rollback.update(
            state="failed",
            service_pid=service_pid,
            error=f"{rollback_to} started but service pid {service_pid} did not acquire the service lock",
        )
        record(rollback)
        return rollback
    runtime.write_status("running", f"pid={ready_pid}", ready_pid, ui_pid)
    rollback.update(state="succeeded", service_pid=ready_pid, error=None)
    record(rollback)
    write(f"rolled back to {rollback_to}; service pid={ready_pid}")
    return rollback


def _wait_for_service_lock_release(timeout: float = _SERVICE_LOCK_RELEASE_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        available, _holder_pid = runtime.service_instance_lock_available()
        if available:
            return True
        time.sleep(0.2)
    available, _holder_pid = runtime.service_instance_lock_available()
    return available


def _run_restart_job(
    *,
    job_id: str,
    delay_seconds: float,
    vibe_path: str | None,
    trigger: str,
    scope: str = "all",
    prepare_show_runtime: bool = False,
    rollback_to: str | None = None,
) -> int:
    # "service": restart only the service process, leaving the Web UI process
    # running (a config change shouldn't tear down the open Web UI). "all"
    # (default, e.g. CLI `vibe restart` / upgrades) restarts both.
    restart_ui = scope != "service"
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
    log_path = _restart_log_path(job_id)
    safe_cwd = get_safe_cwd()
    _prune_restart_logs()

    with log_path.open("a", encoding="utf-8") as log:
        def write(message: str) -> None:
            log.write(f"{_now_iso()} {message}\n")
            log.flush()

        stage_durations: dict[str, float | bool] = {}

        def record_duration(name: str, duration: float) -> float:
            stage_durations[name] = duration
            payload["stage_durations"] = dict(stage_durations)
            _write_status(payload)
            write(f"{name} completed in {duration:.3f}s")
            return duration

        def mark_duration(name: str, started_at: float) -> float:
            return record_duration(name, _rounded_seconds(time.monotonic() - started_at))

        # The backup sequence read just before the new version was allowed to
        # start, which is what tells a rollback whether that version migrated the
        # database. None until then, and None is the right answer for a failure
        # that never got that far: nothing it could have migrated exists.
        backup_watermark: int | None = None

        def record_rollback(rollback: dict) -> None:
            payload["rollback"] = dict(rollback)
            _write_status(payload)

        def fail(error: str, return_code: int, *, started_at: float | None = None) -> int:
            """End this job as failed, rolling back first when nothing is running.

            Every failure in this job goes through here rather than each branch
            deciding whether it is the kind that warrants a rollback. Which
            branches those are is not knowable as a list -- the next branch added
            would not be on it -- so the decision is made once, from the state the
            upgrade must never be able to produce: no service holding the lock.
            """

            if rollback_to and not runtime.verified_service_running():
                try:
                    _roll_back_failed_upgrade(
                        rollback_to=rollback_to,
                        vibe_path=vibe_path,
                        start_ui=restart_ui,
                        backup_watermark=backup_watermark,
                        write=write,
                        record=record_rollback,
                    )
                except Exception as exc:
                    # A rollback that raises must not swallow the failure that
                    # caused it: the original error is what the operator needs,
                    # and the rollback's own is recorded beside it.
                    record_rollback({"target_version": rollback_to, "state": "failed", "error": str(exc)})
                    write(f"rollback to {rollback_to} raised: {exc}")
            elif rollback_to:
                record_rollback({"target_version": rollback_to, "state": "skipped", "reason": "service_running"})
            return _fail(payload, error, log, return_code, started_at=started_at)

        old_pid = _read_recorded_pid()
        payload = {
            "ok": None,
            "job_id": job_id,
            # Record this restart job's own pid (and start time) so a watcher
            # (e.g. the incus regression supervisor) can tell a live restart from a
            # stale status left by a killed job or a reboot, and from an unrelated
            # process that later reused the pid. Matches the key schedule_restart
            # seeds with the spawned subprocess pid (this process is that pid).
            "supervisor_pid": os.getpid(),
            "supervisor_started_at": runtime.process_create_time(os.getpid()),
            "state": "scheduled" if delay_seconds > 0 else "running",
            "trigger": trigger,
            "delay_seconds": delay_seconds,
            "scope": scope,
            # Recorded whether or not a rollback ends up happening, so a failed
            # restart with no `rollback` record is readable: armed and killed
            # before it could recover, versus never recoverable at all.
            "rollback_to": rollback_to,
            "old_pid": old_pid,
            "new_pid": None,
            "log_path": str(log_path),
            "error": None,
            "created_at": _now_iso(),
            "stage_durations": stage_durations,
        }
        _write_status(payload)
        restart_started_at = time.monotonic()
        write(f"restart job scheduled trigger={trigger!r} delay_seconds={delay_seconds!r} old_pid={old_pid!r}")

        if delay_seconds > 0:
            delay_started_at = time.monotonic()
            time.sleep(delay_seconds)
            mark_duration("delay_seconds_actual", delay_started_at)
            payload["state"] = "running"
            _write_status(payload)
            write("restart job started after delay")
            restart_started_at = time.monotonic()

        write("stopping UI and service" if restart_ui else "stopping service (Web UI kept running)")
        stop_runtime_started_at = time.monotonic()
        try:
            ui_stopped, ui_timings, stop_ui_seconds, ui_pid, stopped, stop_service_seconds = _stop_runtime_for_restart(stop_ui=restart_ui)
        except Exception as exc:
            return fail(f"stop runtime failed: {exc}", 2, started_at=restart_started_at)
        stage_durations.update(ui_timings)
        record_duration("stop_ui_total_seconds", stop_ui_seconds)
        record_duration("stop_service_seconds", stop_service_seconds)
        mark_duration("stop_runtime_seconds", stop_runtime_started_at)
        if restart_ui and ui_pid and ui_stopped is False and runtime.pid_alive(ui_pid):
            return fail(f"UI pid {ui_pid} did not stop", 2, started_at=restart_started_at)
        if stopped is False:
            remaining_service_pids = _remaining_service_pids_after_stop()
            if remaining_service_pids:
                payload["remaining_service_pids"] = remaining_service_pids
                pid_list = ",".join(str(pid) for pid in remaining_service_pids)
                return fail(
                    f"service stop failed; remaining service pid(s): {pid_list}",
                    2,
                    started_at=restart_started_at,
                )

        wait_lock_release_started_at = time.monotonic()
        if not _wait_for_service_lock_release():
            mark_duration("wait_service_lock_release_seconds", wait_lock_release_started_at)
            return fail("service lock did not release after stopping runtime", 2, started_at=restart_started_at)
        mark_duration("wait_service_lock_release_seconds", wait_lock_release_started_at)

        if rollback_to:
            # Read here and nowhere else: the service is stopped, its lock is
            # released, and the new version has not run yet, so this is the last
            # instant at which the window still holds only what earlier versions
            # put there. Every backup numbered at or above it afterwards was
            # written by the version about to start.
            backup_watermark = next_backup_sequence(paths.get_state_backups_dir())
            write(f"pre-start backup sequence is {backup_watermark}; rollback target is {rollback_to}")

        write("starting service")
        start_runtime_started_at = time.monotonic()
        try:
            new_pid, ui_pid = _start_runtime_processes(start_ui=restart_ui)
        except Exception as exc:
            return fail(f"start runtime failed: {exc}", 1, started_at=restart_started_at)
        mark_duration("start_runtime_seconds", start_runtime_started_at)

        service_status = runtime.read_status()
        if not new_pid:
            new_pid = _service_pid_from_status(_read_starting_service_status())
            service_status = runtime.read_status()
        if not new_pid or not runtime.pid_alive(new_pid):
            return fail("start runtime completed but service pid is not alive", 3, started_at=restart_started_at)
        if not runtime.service_pid_recorded(new_pid):
            write(f"start runtime returned while service pid={new_pid} is still acquiring its lock")
            wait_lock_started_at = time.monotonic()
            # Resolve the real lock holder: under a delegated user scope the
            # returned pid may be a launcher that never records itself, so adopt
            # the authoritative owner instead of waiting on a pid that can't win.
            resolved_pid = runtime.wait_for_service_ready(new_pid, timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS)
            if resolved_pid is None:
                mark_duration("wait_service_lock_seconds", wait_lock_started_at)
                return fail(
                    f"service pid {new_pid} did not acquire the service lock",
                    3,
                    started_at=restart_started_at,
                )
            new_pid = resolved_pid
            mark_duration("wait_service_lock_seconds", wait_lock_started_at)
            recorded_ui_pid = service_status.get("ui_pid") if service_status else ui_pid
            runtime.write_status("running", f"pid={new_pid}", new_pid, recorded_ui_pid if isinstance(recorded_ui_pid, int) else None)

        mark_duration("restart_total_seconds", restart_started_at)
        payload.update(ok=True, state="succeeded", new_pid=new_pid, error=None)
        _write_status(payload)
        write(f"restart job succeeded new_pid={new_pid}")

        if prepare_show_runtime:
            env = get_restart_environment(vibe_path=vibe_path)
            prepare_command = [
                *get_restart_command(vibe_path=vibe_path),
                "runtime",
                "prepare",
                "--strict",
            ]
            write("preparing Show Runtime after service restart")
            prepare_started_at = time.monotonic()
            try:
                prepare_result = subprocess.run(
                    prepare_command,
                    cwd=safe_cwd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=300,
                )
                if prepare_result.returncode != 0:
                    write(f"Show Runtime preparation failed with exit code {prepare_result.returncode}")
                else:
                    write("Show Runtime preparation succeeded")
            except subprocess.TimeoutExpired:
                write("Show Runtime preparation timed out after 300 seconds")
            except Exception as exc:
                write(f"Show Runtime preparation skipped: {exc}")
            finally:
                mark_duration("prepare_show_runtime_seconds", prepare_started_at)

        pending_restart = _consume_pending_restart_for_job(job_id)
        if pending_restart is not None:
            write(
                "scheduling pending follow-up restart "
                f"trigger={pending_restart.get('trigger')!r} scope={pending_restart.get('scope')!r}"
            )
            try:
                schedule_restart(
                    delay_seconds=0.0,
                    vibe_path=vibe_path,
                    trigger=str(pending_restart.get("trigger") or "pending-restart"),
                    scope=str(pending_restart.get("scope") or "service"),
                )
            except Exception as exc:
                payload["pending_restart"] = {"scheduled": False, "error": str(exc)}
                _write_status(payload)
                write(f"failed to schedule pending follow-up restart: {exc}")

        return 0


def schedule_restart(
    *,
    delay_seconds: float = 0.0,
    vibe_path: str | None = None,
    trigger: str = "cli",
    scope: str = "all",
    prepare_show_runtime: bool = False,
    memory_ui_secret: str | None = None,
    rollback_to: str | None = None,
) -> dict:
    """Spawn the detached restart job.

    `rollback_to` is the version currently installed, and passing it is what
    makes this restart recoverable: if the restart fails and leaves nothing
    holding the service lock, the job reinstalls exactly that version, puts back
    the database if the new one migrated it, and starts the service again. Only
    an upgrade has an answer for it -- a plain restart is already running the
    version it would roll back to, so there is nothing to reinstall and the
    failure is the operator's to look at.
    """
    from core.memory.ui_access import process_ui_read_secret
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    memory_ui_secret = memory_ui_secret or process_ui_read_secret()
    guard_source_checkout_default_state_bootstrap()
    job_id = uuid.uuid4().hex[:12]
    invocation = get_restart_invocation_command(vibe_path=vibe_path)
    command = [*invocation[:-1], "__restart-supervisor"] if invocation and invocation[-1] == "restart" else [
        *(invocation or ["vibe"]),
        "__restart-supervisor",
    ]
    command.extend(["--job-id", job_id, "--delay-seconds", str(delay_seconds), "--trigger", trigger])
    if scope != "all":
        command.extend(["--scope", scope])
    if vibe_path:
        command.extend(["--vibe-path", vibe_path])
    if prepare_show_runtime:
        command.append("--prepare-show-runtime")
    if rollback_to:
        command.extend(["--rollback-to", rollback_to])
    env = get_restart_environment(vibe_path=vibe_path)
    log_path = _restart_log_path(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Seed the status BEFORE spawning the job so the child's own writes (which set
    # state="running" plus its pid and start time) always land afterwards and are
    # never clobbered. A zero-delay restart could otherwise race the parent's
    # "scheduled" write on top of the child's "running" write, hiding the active
    # restart from the supervisor and making it treat the stopped service as a
    # crash. The job records its real supervisor_pid once it starts.
    payload = {
        "ok": None,
        "job_id": job_id,
        "state": "scheduled",
        "trigger": trigger,
        "scope": scope,
        "delay_seconds": delay_seconds,
        "supervisor_pid": None,
        "old_pid": _read_recorded_pid(),
        "new_pid": None,
        "log_path": str(log_path),
        "error": None,
        "created_at": _now_iso(),
    }
    _write_status(payload)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{_now_iso()} spawning restart supervisor job_id={job_id} delay_seconds={delay_seconds!r}\n")
            log.flush()
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if memory_ui_secret is not None else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=get_safe_cwd(),
                env=runtime._memory_ui_child_env(
                    env,
                    memory_ui_secret=memory_ui_secret,
                ),
            )
            runtime._spawn_stdin(process, memory_ui_secret=memory_ui_secret)
    except OSError as exc:
        # The seed status above is now "scheduled"; if the job can't be spawned
        # (bad cached vibe path, missing executable, permission/log-open error) no
        # child will ever overwrite it, leaving a permanently pending restart in
        # `vibe status`. Mark it failed before propagating.
        payload.update(ok=False, state="failed", error=f"failed to spawn restart supervisor: {exc}")
        _write_status(payload)
        _prune_restart_logs()
        raise
    # Surface the spawned pid to the caller without rewriting the status (that
    # would reintroduce the race); the job writes its own pid on disk when it runs.
    payload["supervisor_pid"] = process.pid
    _prune_restart_logs()
    return payload


def main(argv: list[str] | None = None) -> int:
    from core.memory.ui_access import initialize_process_ui_read_secret

    initialize_process_ui_read_secret()
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--trigger", default="cli")
    parser.add_argument("--scope", default="all", choices=("all", "service"))
    parser.add_argument("--vibe-path")
    parser.add_argument("--prepare-show-runtime", action="store_true")
    parser.add_argument("--rollback-to")
    args = parser.parse_args(argv)
    return _run_restart_job(
        job_id=args.job_id,
        delay_seconds=max(0.0, args.delay_seconds),
        vibe_path=args.vibe_path,
        trigger=args.trigger,
        scope=args.scope,
        prepare_show_runtime=args.prepare_show_runtime,
        rollback_to=args.rollback_to,
    )


if __name__ == "__main__":
    raise SystemExit(main())
