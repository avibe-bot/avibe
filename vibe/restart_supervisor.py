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
from vibe import runtime
from vibe.upgrade import get_restart_command, get_restart_environment, get_restart_invocation_command, get_safe_cwd


logger = logging.getLogger(__name__)
_RESTART_LOG_RETENTION = 10
_SERVICE_LOCK_RELEASE_TIMEOUT_SECONDS = 30.0


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
    from core.services import settings as settings_service

    paths.ensure_data_dirs()
    config = settings_service.load_config(default_factory=settings_service.default_config)

    # Service-only restart: the UI process was never stopped, so carry its
    # existing pid through EVERY status write — including the early
    # starting/setup writes and any failure path — so a crash mid-start can't
    # leave the status reporting ui_pid=None while the UI is still serving.
    preserved_ui_pid = None if start_ui else _read_recorded_ui_pid()

    if _runtime_ready_for_config(config):
        runtime.write_status("starting", None, None, preserved_ui_pid)
    else:
        runtime.write_status("setup", "missing platform credentials", None, preserved_ui_pid)

    service_pid = runtime.start_service(wait_for_ready=False, initial_ready_timeout=0)
    if start_ui:
        bind_host = runtime.effective_ui_bind_host(config)
        ui_pid = runtime.start_ui(bind_host, config.ui.setup_port, wait_for_ready=False)
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
            return _fail(payload, f"stop runtime failed: {exc}", log, 2, started_at=restart_started_at)
        stage_durations.update(ui_timings)
        record_duration("stop_ui_total_seconds", stop_ui_seconds)
        record_duration("stop_service_seconds", stop_service_seconds)
        mark_duration("stop_runtime_seconds", stop_runtime_started_at)
        if restart_ui and ui_pid and ui_stopped is False and runtime.pid_alive(ui_pid):
            return _fail(payload, f"UI pid {ui_pid} did not stop", log, 2, started_at=restart_started_at)
        if stopped is False:
            remaining_service_pids = _remaining_service_pids_after_stop()
            if remaining_service_pids:
                payload["remaining_service_pids"] = remaining_service_pids
                pid_list = ",".join(str(pid) for pid in remaining_service_pids)
                return _fail(
                    payload,
                    f"service stop failed; remaining service pid(s): {pid_list}",
                    log,
                    2,
                    started_at=restart_started_at,
                )

        wait_lock_release_started_at = time.monotonic()
        if not _wait_for_service_lock_release():
            mark_duration("wait_service_lock_release_seconds", wait_lock_release_started_at)
            return _fail(payload, "service lock did not release after stopping runtime", log, 2, started_at=restart_started_at)
        mark_duration("wait_service_lock_release_seconds", wait_lock_release_started_at)

        write("starting service")
        start_runtime_started_at = time.monotonic()
        try:
            new_pid, ui_pid = _start_runtime_processes(start_ui=restart_ui)
        except Exception as exc:
            return _fail(payload, f"start runtime failed: {exc}", log, 1, started_at=restart_started_at)
        mark_duration("start_runtime_seconds", start_runtime_started_at)

        service_status = runtime.read_status()
        if not new_pid:
            new_pid = _service_pid_from_status(_read_starting_service_status())
            service_status = runtime.read_status()
        if not new_pid or not runtime.pid_alive(new_pid):
            return _fail(payload, "start runtime completed but service pid is not alive", log, 3, started_at=restart_started_at)
        if not runtime.service_pid_recorded(new_pid):
            write(f"start runtime returned while service pid={new_pid} is still acquiring its lock")
            wait_lock_started_at = time.monotonic()
            if not runtime.wait_for_service_pid(new_pid, timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS):
                mark_duration("wait_service_lock_seconds", wait_lock_started_at)
                return _fail(
                    payload,
                    f"service pid {new_pid} did not acquire the service lock",
                    log,
                    3,
                    started_at=restart_started_at,
                )
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
) -> dict:
    from storage.migrations import guard_source_checkout_default_state_bootstrap

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
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=get_safe_cwd(),
                env=env,
            )
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--trigger", default="cli")
    parser.add_argument("--scope", default="all", choices=("all", "service"))
    parser.add_argument("--vibe-path")
    parser.add_argument("--prepare-show-runtime", action="store_true")
    args = parser.parse_args(argv)
    return _run_restart_job(
        job_id=args.job_id,
        delay_seconds=max(0.0, args.delay_seconds),
        vibe_path=args.vibe_path,
        trigger=args.trigger,
        scope=args.scope,
        prepare_show_runtime=args.prepare_show_runtime,
    )


if __name__ == "__main__":
    raise SystemExit(main())
