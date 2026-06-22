from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import paths
from vibe import runtime

# The supervisor lives in scripts/ (not an installed package); load it the same
# way test_incus_regression.py loads its sibling script.
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "incus_regression_supervisor.py"
SPEC = importlib.util.spec_from_file_location("incus_regression_supervisor", SCRIPT_PATH)
supervisor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


def _write_restart_status(status: dict) -> None:
    path = runtime.get_restart_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json(path, status)


def test_restart_in_progress_true_while_job_pid_alive(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running", "supervisor_pid": 4242, "supervisor_started_at": 1000.0})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(runtime, "process_create_time", lambda pid: 1000.0)

    assert supervisor._restart_in_progress() is True


def test_restart_in_progress_false_when_pid_reused(monkeypatch, tmp_path):
    # Pid is alive but its start time no longer matches what the job recorded —
    # the pid was reused (e.g. after a reboot) by an unrelated process, so the
    # restart is not actually in progress and recovery must proceed.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running", "supervisor_pid": 4242, "supervisor_started_at": 1000.0})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(runtime, "process_create_time", lambda pid: 9999.0)

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_when_job_pid_dead(monkeypatch, tmp_path):
    # The P2: a killed restart job or a reboot leaves ok=None + state=running with
    # a now-dead pid. The supervisor must treat it as stale, not in progress, so
    # it can exit nonzero and let systemd recover instead of looping "restarting".
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running", "supervisor_pid": 4242})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_without_recorded_pid(monkeypatch, tmp_path):
    # An older "running" status with no job pid can't be confirmed alive → stale.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running"})

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_for_scheduled_restart(monkeypatch, tmp_path):
    # A delayed restart is only sleeping ("scheduled") and hasn't stopped the
    # service yet, so a crash during the delay must still be recovered — even
    # though the job process is alive.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "scheduled", "supervisor_pid": 4242})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_when_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": True, "state": "succeeded", "supervisor_pid": 4242})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)

    assert supervisor._restart_in_progress() is False


def test_main_recovers_when_restart_leaves_unready_service(monkeypatch, tmp_path):
    # Codex P2: after a restart writes a new service pid that hangs (alive but
    # never acquires the lock) and the restart job then fails, the supervisor must
    # not adopt the unready pid and loop forever — it must exit nonzero so systemd
    # recovers the service. Old ready pid 100 is dead; the file now points at the
    # hung pid 200 (alive, not recorded); the UI (333) is alive; no restart active.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("200", encoding="utf-8")
    paths.get_runtime_ui_pid_path().write_text("333", encoding="utf-8")

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(supervisor, "_reap_child", lambda pid: None)
    monkeypatch.setattr(supervisor, "_restart_in_progress", lambda: False)
    monkeypatch.setattr(runtime, "start_service", lambda wait_for_ready=True: 100)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", lambda host, port: 333)
    # 100 was ready at startup; the hung 200 never records (no lock).
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 100)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in {200, 333})
    monkeypatch.setattr(runtime, "stop_ui", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "stop_service", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

    rc = supervisor.main()

    assert rc == 1
    assert runtime.read_status()["state"] == "error"


def test_main_backs_off_during_active_restart(monkeypatch, tmp_path):
    # Service is gone but a managed restart is in progress → the supervisor must
    # write "restarting" and keep waiting, never exit for systemd. Guards the
    # TOCTOU where the restart begins after the loop-top: the recovery branch
    # re-reads _restart_in_progress() before exiting.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_ui_pid_path().write_text("333", encoding="utf-8")

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(supervisor, "_reap_child", lambda pid: None)
    monkeypatch.setattr(supervisor, "_restart_in_progress", lambda: True)
    monkeypatch.setattr(runtime, "start_service", lambda wait_for_ready=True: 100)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", lambda host, port: 333)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 100)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 333)  # service 100 dead, ui alive

    statuses: list[str] = []
    monkeypatch.setattr(runtime, "write_status", lambda state, *a, **k: statuses.append(state))

    class _Stop(Exception):
        pass

    def stop_on_sleep(_seconds):
        raise _Stop()

    monkeypatch.setattr(supervisor.time, "sleep", stop_on_sleep)

    with pytest.raises(_Stop):
        supervisor.main()

    assert "restarting" in statuses
    assert "error" not in statuses
