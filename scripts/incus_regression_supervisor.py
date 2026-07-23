#!/usr/bin/env python3
"""Run Avibe service + Web UI inside the Incus regression systemd service."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from config import paths
from config.v2_config import V2Config
from vibe import runtime


def _read_pid_file(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _reap_child(pid: int | None) -> None:
    if not isinstance(pid, int) or pid <= 0 or os.name == "nt":
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return
    except OSError:
        return


def _restart_in_progress() -> bool:
    status = runtime.read_json(runtime.get_restart_status_path()) or {}
    # Only the active stop/start phase ("running") should suppress the supervisor's
    # own recovery. A "scheduled" (delayed) restart is just sleeping and hasn't
    # touched the service yet, so a crash during the delay must still be recovered
    # immediately rather than waiting for the job to wake.
    if status.get("ok") is not None or status.get("state") != "running":
        return False
    # And only while the job process is still alive: a stale status left by a
    # killed restart job or a reboot would otherwise keep this true forever, so the
    # supervisor would loop writing "restarting" instead of exiting nonzero to let
    # systemd recover the service.
    restart_pid = status.get("supervisor_pid")
    if not (isinstance(restart_pid, int) and restart_pid > 0 and runtime.pid_alive(restart_pid)):
        return False
    # Defend against pid reuse (notably across a reboot): confirm the live pid is
    # still the same process the restart job recorded, by start time. Only treat a
    # mismatch as stale; if either start time is unavailable, fall back to the
    # liveness check above.
    recorded_started_at = status.get("supervisor_started_at")
    current_started_at = runtime.process_create_time(restart_pid)
    if isinstance(recorded_started_at, (int, float)) and current_started_at is not None:
        return abs(current_started_at - recorded_started_at) < 1.0
    return True


def _config() -> V2Config:
    runtime.ensure_dirs()
    return runtime.ensure_config()


def main() -> int:
    stopping = False

    def request_stop(signum, frame):  # noqa: ANN001
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    config = _config()
    service_pid = runtime.start_service(wait_for_ready=False)
    bind_host = runtime.effective_ui_bind_host(config)
    ui_pid = runtime.start_ui(bind_host, config.ui.setup_port)

    if not runtime.service_pid_recorded(service_pid):
        ready_service_pid = runtime.wait_for_service_ready(
            service_pid,
            timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS,
        )
        if ready_service_pid is None:
            runtime.write_status("error", "service did not become ready", service_pid, ui_pid)
            runtime.stop_ui()
            runtime.stop_service()
            return 1
        service_pid = ready_service_pid
    runtime.write_status("running", "incus regression started", service_pid, ui_pid)

    while not stopping:
        # _restart_in_progress() is read fresh at each decision below rather than
        # snapshotted here: a managed restart can begin mid-iteration, and a stale
        # snapshot would let the supervisor kill a healthy restart.
        current_service_pid = _read_pid_file(paths.get_runtime_pid_path())
        # Only track a *ready* service pid (recorded and holding the service lock).
        # Adopting a pid just because the file changed would let a hung restart
        # that never becomes ready masquerade as healthy and block recovery.
        if (
            current_service_pid
            and current_service_pid != service_pid
            and runtime.service_pid_recorded(current_service_pid)
        ):
            _reap_child(service_pid)
            service_pid = current_service_pid

        current_ui_pid = _read_pid_file(paths.get_runtime_ui_pid_path()) or ui_pid
        if not current_ui_pid or not runtime.pid_alive(current_ui_pid):
            _reap_child(current_ui_pid)
            if not _restart_in_progress():
                config = _config()
                ui_pid = runtime.start_ui(runtime.effective_ui_bind_host(config), config.ui.setup_port)
                runtime.write_status("running", "ui restarted in incus regression", service_pid, ui_pid)
        elif current_ui_pid != ui_pid:
            ui_pid = current_ui_pid

        if not runtime.pid_alive(service_pid):
            _reap_child(service_pid)
            current_service_pid = _read_pid_file(paths.get_runtime_pid_path())
            if (
                current_service_pid
                and current_service_pid != service_pid
                and runtime.service_pid_recorded(current_service_pid)
            ):
                service_pid = current_service_pid
                time.sleep(1)
                continue
            # Re-read right before the recovery exit: a managed restart may have
            # begun after the dead-service check above, and exiting for systemd
            # would interrupt a healthy restart.
            if _restart_in_progress():
                runtime.write_status("restarting", "incus regression restart in progress", service_pid, ui_pid)
                time.sleep(1)
                continue
            runtime.write_status("error", "service exited in incus regression", service_pid, ui_pid)
            runtime.stop_ui()
            return 1
        time.sleep(1)

    runtime.stop_ui()
    runtime.stop_service()
    runtime.write_status("stopped", "incus regression stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
