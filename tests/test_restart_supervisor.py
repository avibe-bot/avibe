from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from config import paths
from vibe import restart_supervisor
from vibe import runtime


def _fake_start_runtime(calls, service_pid: int = 222, ui_pid: int = 333):
    calls.append("start_runtime")
    runtime.write_status("running", f"pid={service_pid}", service_pid, ui_pid)
    return service_pid, ui_pid


def _fake_stop_runtime(calls, *, ui_stopped=True, ui_pid=None, service_stopped=True):
    calls.append("stop_runtime")
    return (
        ui_stopped,
        {"stop_remote_access_seconds": 0.01, "stop_remote_access_skipped": True},
        0.02,
        ui_pid,
        service_stopped,
        0.03,
    )


def test_schedule_restart_spawns_supervisor_and_records_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("12345", encoding="utf-8")
    calls = {}

    monkeypatch.setattr(restart_supervisor, "get_restart_invocation_command", lambda vibe_path=None: ["/bin/vibe", "restart"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: {"PATH": "/bin"})
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs

        class Proc:
            pid = 45678

        return Proc()

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", fake_popen)

    result = restart_supervisor.schedule_restart(delay_seconds=60, vibe_path="/bin/vibe", trigger="agent")

    assert result["state"] == "scheduled"
    assert result["supervisor_pid"] == 45678
    assert result["old_pid"] == 12345
    assert calls["command"][:2] == ["/bin/vibe", "__restart-supervisor"]
    assert calls["command"][calls["command"].index("--delay-seconds") + 1] == "60"
    assert "--prepare-show-runtime" not in calls["command"]
    assert calls["kwargs"]["start_new_session"] is True
    assert calls["kwargs"]["env"] == {"PATH": "/bin"}
    assert runtime.read_json(runtime.get_restart_status_path())["job_id"] == result["job_id"]


def test_schedule_restart_can_prepare_show_runtime_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    calls = {}

    monkeypatch.setattr(restart_supervisor, "get_restart_invocation_command", lambda vibe_path=None: ["/bin/vibe", "restart"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: None)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)

    def fake_popen(command, **kwargs):
        calls["command"] = command

        class Proc:
            pid = 45678

        return Proc()

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", fake_popen)

    restart_supervisor.schedule_restart(delay_seconds=2, vibe_path="/bin/vibe", trigger="upgrade", prepare_show_runtime=True)

    assert "--prepare-show-runtime" in calls["command"]


def test_schedule_restart_marks_status_failed_when_spawn_fails(monkeypatch, tmp_path):
    # The "scheduled" status is seeded before spawning; if the spawn fails, no
    # child will overwrite it, so schedule_restart must mark it failed (otherwise
    # `vibe status` shows a permanently pending restart that never ran).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    monkeypatch.setattr(restart_supervisor, "get_restart_invocation_command", lambda vibe_path=None: ["/bin/vibe", "restart"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: None)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)

    def boom(*args, **kwargs):
        raise OSError("no such executable")

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", boom)

    with pytest.raises(OSError):
        restart_supervisor.schedule_restart(delay_seconds=0, vibe_path="/bin/vibe", trigger="agent")

    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is False
    assert status["state"] == "failed"
    assert "failed to spawn" in status["error"]


def test_restart_job_stops_and_starts_service(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)

    rc = restart_supervisor._run_restart_job(job_id="jobabc", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 0
    assert calls == ["stop_runtime", "start_runtime"]
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is True
    assert status["state"] == "succeeded"
    assert status["old_pid"] == 111
    assert status["new_pid"] == 222
    # The job records its own pid + start time so a watcher can validate the
    # restart is live and not a reused pid.
    assert status["supervisor_pid"] == os.getpid()
    assert isinstance(status["supervisor_started_at"], (int, float))
    assert status["stage_durations"]["stop_remote_access_seconds"] == 0.01
    assert status["stage_durations"]["stop_remote_access_skipped"] is True
    assert "stop_ui_total_seconds" in status["stage_durations"]
    assert "stop_service_seconds" in status["stage_durations"]
    assert "stop_runtime_seconds" in status["stage_durations"]
    assert "wait_service_lock_release_seconds" in status["stage_durations"]
    assert "start_runtime_seconds" in status["stage_durations"]
    assert "restart_total_seconds" in status["stage_durations"]


def test_restart_job_uses_lock_holder_when_pidfile_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    calls = []

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda: 111)
    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)

    rc = restart_supervisor._run_restart_job(job_id="joblockowner", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 0
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["old_pid"] == 111
    assert status["new_pid"] == 222


def test_restart_job_prepares_show_runtime_after_service_start(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "get_restart_command", lambda vibe_path=None: ["/bin/vibe"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: None)

    def fake_run(command, **kwargs):
        calls.append(("run", command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(restart_supervisor.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)

    rc = restart_supervisor._run_restart_job(
        job_id="jobruntime",
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="upgrade",
        prepare_show_runtime=True,
    )

    assert rc == 0
    assert calls == [
        "stop_runtime",
        "start_runtime",
        ("run", ["/bin/vibe", "runtime", "prepare", "--strict"]),
    ]
    assert runtime.read_json(runtime.get_restart_status_path())["state"] == "succeeded"


def test_restart_job_schedules_pending_followup_after_success(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []
    scheduled: list[dict] = []

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)

    original_schedule_restart = restart_supervisor.schedule_restart

    def _schedule_restart(**kwargs):
        scheduled.append(kwargs)
        return {"job_id": "followup"}

    restart_supervisor.mark_pending_restart(
        trigger="web-ui-config-pending",
        scope="service",
        reason="restart_in_progress",
        restart_job_id="jobpending",
    )
    monkeypatch.setattr(restart_supervisor, "schedule_restart", _schedule_restart)

    try:
        rc = restart_supervisor._run_restart_job(
            job_id="jobpending",
            delay_seconds=0,
            vibe_path="/bin/vibe",
            trigger="web-ui",
            scope="service",
        )
    finally:
        monkeypatch.setattr(restart_supervisor, "schedule_restart", original_schedule_restart)

    assert rc == 0
    assert scheduled == [
        {
            "delay_seconds": 0.0,
            "vibe_path": "/bin/vibe",
            "trigger": "web-ui-config-pending",
            "scope": "service",
        }
    ]
    assert runtime.read_json(restart_supervisor._pending_restart_path()) is None


def test_restart_job_aborts_when_stop_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        restart_supervisor,
        "_stop_runtime_for_restart",
        lambda stop_ui=True: _fake_stop_runtime(calls, service_stopped=False),
    )
    monkeypatch.setattr(restart_supervisor, "_remaining_service_pids_after_stop", lambda: [111])
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(restart_supervisor.subprocess, "run", lambda *args, **kwargs: calls.append("run"))

    rc = restart_supervisor._run_restart_job(job_id="jobdef", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 2
    assert calls == ["stop_runtime"]
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is False
    assert status["state"] == "failed"
    assert "remaining service pid(s): 111" in status["error"]
    assert status["remaining_service_pids"] == [111]


def test_restart_job_continues_when_old_pid_already_exited(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        restart_supervisor,
        "_stop_runtime_for_restart",
        lambda stop_ui=True: _fake_stop_runtime(calls, service_stopped=False),
    )
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(restart_supervisor, "_remaining_service_pids_after_stop", lambda: [])
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)

    rc = restart_supervisor._run_restart_job(job_id="joboldgone", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 0
    assert calls == ["stop_runtime", "start_runtime"]
    assert runtime.read_json(runtime.get_restart_status_path())["state"] == "succeeded"


def test_restart_job_aborts_when_extra_service_survives_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    calls = []

    monkeypatch.setattr(
        restart_supervisor,
        "_stop_runtime_for_restart",
        lambda stop_ui=True: _fake_stop_runtime(calls, service_stopped=False),
    )
    monkeypatch.setattr(restart_supervisor, "_remaining_service_pids_after_stop", lambda: [333])
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)

    rc = restart_supervisor._run_restart_job(
        job_id="jobextra",
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="test",
    )

    assert rc == 2
    assert calls == ["stop_runtime"]
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is False
    assert status["state"] == "failed"
    assert status["remaining_service_pids"] == [333]
    assert "remaining service pid(s): 333" in status["error"]


def test_restart_job_adopts_slow_starting_service_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    def slow_start_runtime(start_ui=True):
        calls.append("start_runtime")
        runtime.write_status("starting", "service process is still starting", 222, 333)
        try:
            paths.get_runtime_pid_path().unlink()
        except FileNotFoundError:
            pass
        return 222, 333

    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", slow_start_runtime)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: False)
    monkeypatch.setattr(runtime, "wait_for_service_pid", lambda pid, timeout: pid == 222)

    rc = restart_supervisor._run_restart_job(job_id="jobslow", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 0
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is True
    assert status["state"] == "succeeded"
    assert status["new_pid"] == 222
    service_status = runtime.read_status()
    assert service_status["state"] == "running"
    assert service_status["service_pid"] == 222
    assert service_status["ui_pid"] == 333


def test_restart_job_marks_start_runtime_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime([]))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(
        restart_supervisor,
        "_start_runtime_processes",
        lambda start_ui=True: (_ for _ in ()).throw(RuntimeError("service refused to start")),
    )

    rc = restart_supervisor._run_restart_job(job_id="jobtimeout", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 1
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is False
    assert status["state"] == "failed"
    assert "start runtime failed: service refused to start" in status["error"]
    assert "restart_total_seconds" in status["stage_durations"]


def test_restart_job_waits_for_service_lock_release_before_start(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda start_ui=True: _fake_start_runtime(calls))

    lock_checks = iter([(False, 111), (True, None)])

    def service_instance_lock_available():
        calls.append("lock_available")
        return next(lock_checks)

    monkeypatch.setattr(runtime, "service_instance_lock_available", service_instance_lock_available)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)
    monkeypatch.setattr(restart_supervisor.time, "sleep", lambda _seconds: None)

    rc = restart_supervisor._run_restart_job(job_id="joblock", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 0
    assert calls == ["stop_runtime", "lock_available", "lock_available", "start_runtime"]
    assert runtime.read_json(runtime.get_restart_status_path())["state"] == "succeeded"


def test_restart_job_fails_when_service_lock_does_not_release(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: False)
    monkeypatch.setattr(
        restart_supervisor,
        "_start_runtime_processes",
        lambda start_ui=True: (_ for _ in ()).throw(AssertionError("start should wait for lock release")),
    )
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)

    rc = restart_supervisor._run_restart_job(job_id="joblockfail", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert rc == 2
    assert calls == ["stop_runtime"]
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["state"] == "failed"
    assert "service lock did not release" in status["error"]


def test_start_runtime_processes_starts_service_and_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    calls = []
    config = SimpleNamespace(
        ui=SimpleNamespace(setup_port=5123),
        has_configured_platform_credentials=lambda: True,
    )

    from core.services import settings as settings_service

    def fake_ensure_data_dirs():
        calls.append("ensure_data_dirs")
        paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "ensure_data_dirs", fake_ensure_data_dirs)
    monkeypatch.setattr(settings_service, "load_config", lambda default_factory=None: calls.append("load_config") or config)
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda wait_for_ready=True, initial_ready_timeout=5.0: calls.append(
            ("start_service", wait_for_ready, initial_ready_timeout)
        )
        or 222,
    )
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda cfg: calls.append(("bind_host", cfg)) or "0.0.0.0")
    monkeypatch.setattr(
        runtime,
        "start_ui",
        lambda host, port, wait_for_ready=True: calls.append(("start_ui", host, port, wait_for_ready)) or 333,
    )
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)

    service_pid, ui_pid = restart_supervisor._start_runtime_processes()

    assert service_pid == 222
    assert ui_pid == 333
    assert calls == [
        "ensure_data_dirs",
        "load_config",
        ("start_service", False, 0),
        ("bind_host", config),
        ("start_ui", "0.0.0.0", 5123, False),
    ]
    status = runtime.read_status()
    assert status["state"] == "running"
    assert status["service_pid"] == 222
    assert status["ui_pid"] == 333


def test_stop_runtime_for_restart_stops_ui_and_service(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    calls = []
    ui_entered = threading.Event()
    service_entered = threading.Event()

    def stop_ui(timings=None, *, stop_remote_access=True):
        assert stop_remote_access is False
        calls.append("stop_ui")
        ui_entered.set()
        assert service_entered.wait(timeout=1.0)
        if timings is not None:
            timings["stop_remote_access_seconds"] = 0.01
        return True

    monkeypatch.setattr(runtime, "stop_ui", stop_ui)

    def stop_service():
        calls.append("stop_service")
        service_entered.set()
        assert ui_entered.wait(timeout=1.0)
        return True

    monkeypatch.setattr(runtime, "stop_service", stop_service)

    ui_stopped, timings, stop_ui_seconds, ui_pid, service_stopped, stop_service_seconds = (
        restart_supervisor._stop_runtime_for_restart()
    )

    assert ui_stopped is True
    assert service_stopped is True
    assert timings["stop_remote_access_seconds"] == 0.01
    assert stop_ui_seconds >= 0
    assert stop_service_seconds >= 0
    assert ui_pid is None
    assert sorted(calls) == ["stop_service", "stop_ui"]


def test_schedule_restart_service_scope_adds_flag(monkeypatch, tmp_path):
    """A service-only restart passes ``--scope service`` to the supervisor job;
    the default ``all`` scope adds no flag (back-compat for CLI/upgrade)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    commands: list[list[str]] = []
    monkeypatch.setattr(restart_supervisor, "get_restart_invocation_command", lambda vibe_path=None: ["/bin/vibe", "restart"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: {"PATH": "/bin"})
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)

    def fake_popen(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", fake_popen)

    restart_supervisor.schedule_restart(delay_seconds=0, vibe_path="/bin/vibe", trigger="web-ui", scope="service")
    assert "--scope" in commands[-1] and commands[-1][commands[-1].index("--scope") + 1] == "service"

    restart_supervisor.schedule_restart(delay_seconds=0, vibe_path="/bin/vibe", trigger="web-ui")
    assert "--scope" not in commands[-1]


def test_restart_job_service_scope_keeps_ui(monkeypatch, tmp_path):
    """scope='service' restarts only the service: the UI is neither stopped nor
    started, so its recorded pid is preserved across the restart."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    calls = []
    captured: dict[str, bool] = {}

    def stub_stop(stop_ui=True):
        captured["stop_ui"] = stop_ui
        return _fake_stop_runtime(calls)

    def stub_start(start_ui=True):
        captured["start_ui"] = start_ui
        return _fake_start_runtime(calls)

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", stub_stop)
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", stub_start)
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)

    rc = restart_supervisor._run_restart_job(
        job_id="jobsvc", delay_seconds=0, vibe_path="/bin/vibe", trigger="web-ui", scope="service"
    )

    assert rc == 0
    # The UI was deliberately left running on both the stop and start sides.
    assert captured == {"stop_ui": False, "start_ui": False}
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is True
    assert status["scope"] == "service"
