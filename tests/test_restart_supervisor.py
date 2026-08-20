from __future__ import annotations

import ast
import inspect
import io
import os
import sqlite3
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

from config import paths
from storage.backups import create_sqlite_migration_backup
from vibe import restart_supervisor
from vibe import runtime
from vibe.upgrade import RollbackTarget


#: The install the machine was on before the upgrade replaced it. Deliberately
#: not this process's own interpreter: the case the launcher exists for is the
#: `vibe-remote` -> `avibe-os` rename, where the two generations live in
#: different directories, and a fixture reusing `sys.executable` would pass
#: whether or not the target's install is carried anywhere at all.
_REPLACED_INSTALL = runtime.ServiceLauncher(
    python="/uv/tools/vibe-remote/bin/python",
    main="/uv/tools/vibe-remote/lib/python3.13/site-packages/vibe/service_main.py",
)


def _rollback_target(version: str = "3.0.10") -> RollbackTarget:
    """The install to go back to, as the upgrade captured it before installing."""

    return RollbackTarget(version=version, package="vibe-remote", launcher=_REPLACED_INSTALL)


def _fake_start_runtime(calls, service_pid: int = 222, ui_pid: int = 333):
    calls.append("start_runtime")
    runtime.write_status("running", f"pid={service_pid}", service_pid, ui_pid)
    return restart_supervisor.StartedRuntime(service_pid, ui_pid, ("127.0.0.1", 5123))


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


def _fake_pinned_install(calls, installs, *, pin: str = "vibe-remote==3.0.10"):
    """Record only the rollback's pinned install, never host process-table probes.

    Patching ``restart_supervisor.subprocess.run`` replaces the stdlib function
    for every importer. Rollback then inspects leftover pids through
    ``get_process_command``, which shells out to ``ps -p <pid>``. Capturing that
    probe as ``installs[0]`` makes the pin assertion fail on whatever pid the
    host happens to have live.
    """

    def run(command, **_kwargs):
        argv = list(command)
        if any(pin in str(part) for part in argv):
            calls.append("install")
            installs.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


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


def test_legacy_upgrade_target_reads_the_running_release_and_launcher(monkeypatch, tmp_path):
    """A pre-rollback release can still hand the new supervisor its target."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    paths.ensure_data_dirs()
    tool_root = tmp_path / "uv" / "tools" / "avibe-os"
    python_path = tool_root / "bin" / "python"
    vibe_path = tool_root / "bin" / "vibe"
    service_main = tool_root / "lib" / "python3.12" / "site-packages" / "vibe" / "service_main.py"
    python_path.parent.mkdir(parents=True)
    service_main.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    vibe_path.write_text(f"#!{python_path}\n", encoding="utf-8")
    service_main.write_text("# old release\n", encoding="utf-8")
    metadata_dir = service_main.parent.parent / "avibe_os-3.0.12.dist-info"
    metadata_dir.mkdir()
    (metadata_dir / "METADATA").write_text("Name: avibe-os\nVersion: 3.0.12\n", encoding="utf-8")

    monkeypatch.setattr(restart_supervisor, "_read_recorded_pid", lambda: 123)
    monkeypatch.setattr(restart_supervisor, "_running_ui_version", lambda: None)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: f"{python_path} {service_main}",
    )

    target = restart_supervisor._discover_legacy_upgrade_target(
        trigger="upgrade", vibe_path=str(tmp_path / "retargeted-vibe")
    )

    assert target == RollbackTarget(
        version="3.0.12",
        package="avibe-os",
        launcher=runtime.ServiceLauncher(python=str(python_path), main=str(service_main)),
    )


def test_legacy_upgrade_target_prefers_running_version_over_stale_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    paths.ensure_data_dirs()
    tool_root = tmp_path / "venv"
    python_path = tool_root / "bin" / "python"
    service_main = tool_root / "lib" / "python3.12" / "site-packages" / "vibe" / "service_main.py"
    python_path.parent.mkdir(parents=True)
    service_main.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    service_main.write_text("# replaced release\n", encoding="utf-8")
    metadata_root = service_main.parent.parent
    for name, version in (("avibe_os", "3.0.13"), ("vibe_remote", "2.9.4")):
        metadata_dir = metadata_root / f"{name}-{version}.dist-info"
        metadata_dir.mkdir()
        package_name = "avibe-os" if name == "avibe_os" else "vibe-remote"
        (metadata_dir / "METADATA").write_text(
            f"Name: {package_name}\nVersion: {version}\n", encoding="utf-8"
        )

    monkeypatch.setattr(restart_supervisor, "_read_recorded_pid", lambda: 123)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: f"{python_path} {service_main}",
    )
    monkeypatch.setattr(restart_supervisor, "_running_ui_version", lambda: "3.0.12")

    target = restart_supervisor._discover_legacy_upgrade_target(
        trigger="upgrade", vibe_path=str(tmp_path / "retargeted-vibe")
    )

    assert target == RollbackTarget(
        version="3.0.12",
        package="avibe-os",
        launcher=runtime.ServiceLauncher(python=str(python_path), main=str(service_main)),
    )


def test_legacy_upgrade_target_recovers_from_ui_only_process(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    paths.ensure_data_dirs()
    tool_root = tmp_path / "uv" / "tools" / "vibe-remote"
    python_path = tool_root / "bin" / "python"
    service_main = tool_root / "lib" / "python3.12" / "site-packages" / "vibe" / "service_main.py"
    python_path.parent.mkdir(parents=True)
    service_main.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    service_main.write_text("# old release\n", encoding="utf-8")
    metadata_dir = service_main.parent.parent / "vibe_remote-2.9.4.dist-info"
    metadata_dir.mkdir()
    (metadata_dir / "METADATA").write_text("Name: vibe-remote\nVersion: 2.9.4\n", encoding="utf-8")

    monkeypatch.setattr(restart_supervisor, "_read_recorded_pid", lambda: 999)
    monkeypatch.setattr(restart_supervisor, "_read_recorded_ui_pid", lambda: 456)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: None if pid == 999 else f'{python_path} -c "from vibe.ui_server import run_ui_server; run_ui_server()"',
    )
    monkeypatch.setattr(restart_supervisor, "_running_ui_version", lambda: "2.9.4")

    target = restart_supervisor._discover_legacy_upgrade_target(
        trigger="upgrade", vibe_path=str(tmp_path / "retargeted-vibe")
    )

    assert target == RollbackTarget(
        version="2.9.4",
        package="vibe-remote",
        launcher=runtime.ServiceLauncher(python=str(python_path), main=str(service_main)),
    )


def test_service_launcher_from_process_strips_windows_quotes(monkeypatch, tmp_path):
    python_path = tmp_path / "Program Files" / "uv" / "tools" / "avibe-os" / "Scripts" / "python.exe"
    service_main = tmp_path / "Program Files" / "uv" / "tools" / "avibe-os" / "Lib" / "site-packages" / "vibe" / "service_main.py"
    service_main.parent.mkdir(parents=True)
    service_main.write_text("# old release\n", encoding="utf-8")
    command = f'"{python_path}" "{service_main}"'
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: command)

    target = restart_supervisor._service_launcher_from_process(123)

    assert target == runtime.ServiceLauncher(python=str(python_path), main=str(service_main))


def test_legacy_upgrade_without_target_leaves_packaged_runtime_running(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    monkeypatch.setattr(restart_supervisor, "get_build_identity", lambda: SimpleNamespace(kind="package"))
    monkeypatch.setattr(restart_supervisor, "_discover_legacy_upgrade_target", lambda **kwargs: None)
    monkeypatch.setattr(
        restart_supervisor,
        "_stop_runtime_for_restart",
        lambda **kwargs: pytest.fail("a legacy upgrade without a target must not stop the runtime"),
    )

    rc = restart_supervisor._run_restart_job(
        job_id="job-legacy-no-target",
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="upgrade",
    )

    assert rc == 2
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["state"] == "failed"
    assert "existing runtime was left running" in status["error"]


def test_failed_legacy_upgrade_uses_discovered_target_for_rollback(monkeypatch, tmp_path):
    """The v3.0.12 path is recoverable even though it passed no rollback argv."""

    armed = _upgrade_restart_that_dies_after_migrating(monkeypatch, tmp_path, service_running=False)
    monkeypatch.setattr(
        restart_supervisor,
        "_discover_legacy_upgrade_target",
        lambda **kwargs: _rollback_target(),
    )

    rc = restart_supervisor._run_restart_job(
        job_id="joblegacy",
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="upgrade",
        rollback_to=None,
    )

    assert rc == 1
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["rollback_to"] == "3.0.10"
    assert status["rollback_target_source"] == "running_service"
    assert status["rollback"]["state"] == "succeeded"
    assert armed.observed_by_the_rolled_back_version == [["before the upgrade"]]


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


def test_schedule_restart_passes_memory_ui_secret_only_through_stdin(monkeypatch, tmp_path):
    from core.memory.ui_access import MEMORY_UI_SECRET_STDIN_ENV

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    calls = {}
    secret = "test-memory-ui-secret"

    monkeypatch.setattr(
        restart_supervisor,
        "get_restart_invocation_command",
        lambda vibe_path=None: ["/bin/vibe", "restart"],
    )
    monkeypatch.setattr(
        restart_supervisor,
        "get_restart_environment",
        lambda vibe_path=None: {"PATH": "/bin"},
    )
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)

    def fake_popen(command, **kwargs):
        calls["kwargs"] = kwargs
        return SimpleNamespace(pid=45678, stdin=io.BytesIO())

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", fake_popen)

    restart_supervisor.schedule_restart(
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="web-ui",
        scope="service",
        memory_ui_secret=secret,
    )

    assert calls["kwargs"]["stdin"] is restart_supervisor.subprocess.PIPE
    assert calls["kwargs"]["env"] == {
        "PATH": "/bin",
        MEMORY_UI_SECRET_STDIN_ENV: "1",
    }
    assert secret not in calls["kwargs"]["env"].values()


def test_the_argv_the_job_builds_is_the_argv_the_entry_point_accepts(monkeypatch, tmp_path):
    """One command, one parser, proved by running the built argv through the CLI.

    `schedule_restart` builds a command line and a detached `vibe` process parses
    it back. For a while both ends were owned by different parsers -- this module
    built the `--rollback-*` flags, `vibe/cli.py` declared the ones it knew about,
    and the top-level parser ran first. So the flags were not merely unsupported,
    they were rejected: the child exited on `unrecognized arguments`, the seeded
    "scheduled" record was never overwritten by anyone, and the machine stayed on
    the release that could not start. Every test in this file called
    `restart_supervisor.main([...])` directly, so nothing crossed that seam.

    Asserted as a round trip rather than as a list of flags: the spawned argv is
    taken exactly as `Popen` received it and handed to the real entry point, so a
    flag added to the builder later is covered without touching this test.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    spawned = {}

    monkeypatch.setattr(restart_supervisor, "get_restart_invocation_command", lambda vibe_path=None: ["/bin/vibe", "restart"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda vibe_path=None: None)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor, "_prune_restart_logs", lambda: None)

    def fake_popen(command, **kwargs):
        spawned["command"] = command
        return SimpleNamespace(pid=45678)

    monkeypatch.setattr(restart_supervisor.subprocess, "Popen", fake_popen)

    rollback_to = _rollback_target("3.0.10")
    restart_supervisor.schedule_restart(
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="upgrade",
        scope="service",
        prepare_show_runtime=True,
        rollback_to=rollback_to,
    )

    from vibe import cli

    ran = {}

    def fake_run_restart_job(**kwargs):
        ran.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: None)
    monkeypatch.setattr(restart_supervisor, "_run_restart_job", fake_run_restart_job)
    monkeypatch.setattr(sys, "argv", list(spawned["command"]))

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0
    # Not just "it parsed": the rollback target has to arrive whole, because a
    # partially-carried one is the failure this whole path exists to prevent.
    assert ran["rollback_to"] == rollback_to
    assert ran["trigger"] == "upgrade"
    assert ran["scope"] == "service"
    assert ran["prepare_show_runtime"] is True


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


@pytest.mark.parametrize(
    "outcome",
    [
        {"ok": False, "state": "failed", "error": "boom"},
        {"ok": None, "state": "scheduled", "error": None},
        {"ok": True, "state": "succeeded", "error": None},
    ],
    ids=["failed", "in-flight", "succeeded"],
)
def test_a_recorded_outcome_reports_the_job_and_nothing_about_liveness(monkeypatch, tmp_path, outcome):
    """The record says what the job did. It makes no claim about the machine.

    A fence, not a repair. An earlier revision of this fix had the writer stamp
    what was alive at write time, and every later reader of that stamp was a way
    to get a present-tense question wrong from a past-tense answer; doctor now
    measures liveness when it reports, so the record must stay a statement about
    the job alone. The liveness probes are stubbed to raise rather than to answer,
    so a writer that starts consulting them fails here instead of in review --
    and it names the second cost of consulting them, since a probe can fail on
    its own (an unopenable lock file is enough) and a diagnostic detail is never
    worth losing the restart result over, least of all on the spawn-error path
    where losing it also strands the `ok: null` marker that makes status report a
    restart still in flight.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    def fail_probe(*_args, **_kwargs):
        raise OSError("service.lock cannot be opened")

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", fail_probe)
    monkeypatch.setattr(runtime, "extra_service_process_pids", fail_probe)

    restart_supervisor._write_status({"job_id": "job-1", **outcome})

    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is outcome["ok"]
    assert status["state"] == outcome["state"]
    assert "service_alive" not in status


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
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)

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
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)

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
    monkeypatch.setattr(restart_supervisor, "_discover_legacy_upgrade_target", lambda **kwargs: None)
    monkeypatch.setattr(restart_supervisor, "get_build_identity", lambda: SimpleNamespace(kind="source"))

    def fake_run(command, **kwargs):
        calls.append(("run", command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(restart_supervisor.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)

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
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)

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
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)

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
        return restart_supervisor.StartedRuntime(222, 333, ("127.0.0.1", 5123))

    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", slow_start_runtime)
    # Both halves of the generation this start launched are alive: the status
    # carries a UI pid only for a UI that is still there, so a stub that answered
    # for the service alone would be describing a different machine.
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in {222, 333})
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: False)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout: 222 if pid == 222 else None)

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
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)
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
        lambda wait_for_ready=True, initial_ready_timeout=5.0, **kwargs: calls.append(
            ("start_service", wait_for_ready, initial_ready_timeout, kwargs)
        )
        or 222,
    )
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda cfg: calls.append(("bind_host", cfg)) or "0.0.0.0")
    monkeypatch.setattr(
        runtime,
        "start_ui",
        lambda host, port, wait_for_ready=True, **kwargs: calls.append(
            ("start_ui", host, port, wait_for_ready, kwargs)
        )
        or 333,
    )
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 222)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 222)

    started = restart_supervisor._start_runtime_processes()

    assert started.service_pid == 222
    assert started.ui_pid == 333
    # The health target is resolved here or nowhere: this is the only place that
    # loads the config to decide the bind host and port, so it is the only answer
    # to "where is the UI" that cannot disagree with where the UI was started.
    assert started.ui_health_target == ("0.0.0.0", 5123)
    assert calls[:2] == ["ensure_data_dirs", "load_config"]
    assert calls[2][:3] == ("start_service", False, 0)
    assert calls[3] == ("bind_host", config)
    assert calls[4][:4] == ("start_ui", "0.0.0.0", 5123, False)
    assert calls[2][3]["memory_ui_secret"] == calls[4][4]["memory_ui_secret"]

    # Every process this helper starts comes from the one install it was given,
    # asserted over whatever it started rather than over two named spawns: the
    # service and the UI are two halves of one generation, and a rollback that
    # started one from the restored install and the other from the replaced one
    # is a state neither release was ever run in. `None` is "this process",
    # which is the right answer for every restart except a rollback.
    def started_from() -> list:
        return [call[-1].get("launcher") for call in calls if call[0] in ("start_service", "start_ui")]

    assert started_from() == [None, None]

    calls.clear()
    restart_supervisor._start_runtime_processes(launcher=_REPLACED_INSTALL)
    assert started_from() == [_REPLACED_INSTALL, _REPLACED_INSTALL]

    status = runtime.read_status()
    # "starting", not "running", and the stubbed lock says the pid is recorded.
    # This helper spawns; it does not observe. Its callers wait for the service's
    # own report and promote the status themselves, so a claim of "running" from
    # here is a claim about a process that has not migrated the database yet.
    assert status["state"] == "starting"
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
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 222)

    rc = restart_supervisor._run_restart_job(
        job_id="jobsvc", delay_seconds=0, vibe_path="/bin/vibe", trigger="web-ui", scope="service"
    )

    assert rc == 0
    # The UI was deliberately left running on both the stop and start sides.
    assert captured == {"stop_ui": False, "start_ui": False}
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is True
    assert status["scope"] == "service"


def _seed_state_database(db_path):
    """The database as the machine had it before the upgrade touched anything."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table if not exists alembic_version (version_num varchar(32) not null)")
        conn.execute("delete from alembic_version")
        conn.execute("insert into alembic_version (version_num) values ('20260806_0047')")
        conn.execute("create table payload (value text)")
        conn.execute("insert into payload (value) values ('before the upgrade')")


def _payload_rows(db_path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute("select value from payload order by rowid")]


def _upgrade_restart_that_dies_after_migrating(
    monkeypatch,
    tmp_path,
    *,
    service_running: bool,
    ui_serving: bool = True,
):
    """Arm the incident this whole path exists for, and hand back what it did.

    The new version stops the old one, starts, takes its pre-migration backup,
    commits a row, and then dies without ever holding the service lock. Only the
    two processes and the package index are stubbed: the database, the backup
    window, and the watermark the job reads out of it are the real ones, because
    they are the things the rollback has to get right.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    db_path = paths.get_sqlite_state_path()
    _seed_state_database(db_path)

    calls: list[str] = []
    installs: list[list[str]] = []
    launchers: list = []
    observed_by_the_rolled_back_version: list[list[str]] = []

    def start(start_ui=True, launcher=None):
        calls.append("start_runtime")
        launchers.append(launcher)
        if calls.count("start_runtime") == 1:
            # The new version gets far enough to take its rollback point and
            # commit, then dies without ever holding the lock.
            create_sqlite_migration_backup(db_path, backups_dir=paths.get_state_backups_dir())
            with sqlite3.connect(db_path) as conn:
                conn.execute("insert into payload (value) values ('committed by the new version')")
            raise RuntimeError("service refused to start")
        observed_by_the_rolled_back_version.append(_payload_rows(db_path))
        return _fake_start_runtime([], service_pid=222, ui_pid=333)

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", start)
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor.subprocess, "run", _fake_pinned_install(calls, installs))
    monkeypatch.setattr(runtime, "verified_service_running", lambda: service_running)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout=None: pid)
    # The machine as this failure actually leaves it: the version that died in
    # its migration is gone, and so nothing of it is still holding the database
    # open. Stated here rather than left to the host's real process table, which
    # a test may not read and must never depend on.
    monkeypatch.setattr(restart_supervisor, "_remaining_service_pids_after_stop", list)
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda *args, **kwargs: False)
    # The UI the rollback restarts, and whether it answers. Both are stubbed
    # rather than left to the host: `_ui_is_serving` opens a real socket, and a
    # test that let it would wait out the timeout against whatever happens to be
    # listening on the developer's machine.
    ui_probes: list[tuple[str, int]] = []
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 333)
    monkeypatch.setattr(
        runtime,
        "wait_for_ui_server",
        lambda host, port, timeout=None: ui_probes.append((host, port)) or ui_serving,
    )

    return SimpleNamespace(
        db_path=db_path,
        calls=calls,
        installs=installs,
        launchers=launchers,
        ui_probes=ui_probes,
        observed_by_the_rolled_back_version=observed_by_the_rolled_back_version,
    )


def test_a_rollback_whose_ui_never_serves_is_not_reported_as_recovered(monkeypatch, tmp_path):
    """A live pid is not a serving UI, and this job is the one that cannot guess.

    The service came back and holds the lock; the UI process was started and is
    alive, and never answers. Everything a pid-based check can see says the
    machine is up, which is exactly the evidence this record must not accept:
    nobody is watching an unattended rollback, so the half that is dark has to be
    the half that is reported. The service pid is still recorded either way --
    the run failed, it did not vanish.
    """

    armed = _upgrade_restart_that_dies_after_migrating(
        monkeypatch, tmp_path, service_running=False, ui_serving=False
    )

    restart_supervisor._run_restart_job(
        job_id="jobdark", delay_seconds=0, vibe_path="/bin/vibe", trigger="upgrade", rollback_to=_rollback_target()
    )

    rollback = runtime.read_json(runtime.get_restart_status_path())["rollback"]
    assert rollback["state"] == "failed"
    assert rollback["ui"] == {"pid": 333, "serving": False}
    assert rollback["service_pid"] == 222
    assert "Web UI" in rollback["error"]
    # It reached the UI at all, rather than failing for want of a probe.
    assert armed.ui_probes


def test_a_restart_that_leaves_nothing_running_puts_the_old_version_back(monkeypatch, tmp_path):
    """The property the change exists for: an upgrade cannot end with a dark instance.

    Install, then restore, then start -- and the started version has to find the
    database the version it is reads. That last part is why the order is asserted
    through what the started process sees rather than through a call log: a
    restore that lands after the start is a restore that lands after the service
    has already opened a schema it cannot read.
    """

    armed = _upgrade_restart_that_dies_after_migrating(monkeypatch, tmp_path, service_running=False)

    rc = restart_supervisor._run_restart_job(
        job_id="jobroll", delay_seconds=0, vibe_path="/bin/vibe", trigger="upgrade", rollback_to=_rollback_target()
    )

    # The restart still failed, and still says so: a rollback recovers the
    # machine, it does not turn a failed upgrade into a successful one.
    assert rc == 1
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is False
    assert "start runtime failed: service refused to start" in status["error"]

    # Nothing of the failed generation is left alive across the install and the
    # restore: the second stop is the rollback's own, and without it a process
    # that never reached the lock is still holding the database file open when
    # the restore rewrites it -- and is what the final start would adopt.
    assert armed.calls == ["stop_runtime", "start_runtime", "stop_runtime", "install", "start_runtime"]
    # The distribution the OLD release was published as, not the one this build
    # is: a machine that predates the `vibe-remote` -> `avibe-os` rename has no
    # `avibe-os==3.0.10` to go back to, and pinning one is an index round-trip
    # that fails and leaves the instance dark.
    assert "vibe-remote==3.0.10" in armed.installs[0]
    assert armed.observed_by_the_rolled_back_version == [["before the upgrade"]]
    assert _payload_rows(armed.db_path) == ["before the upgrade"]

    # The rollback starts the install it just reinstalled, not the one this job
    # is running out of. An upgrade spawns the job through the `vibe` on PATH,
    # which the install has already replaced, so by now `sys.executable` names
    # the failed generation -- and across the `vibe-remote` -> `avibe-os` rename
    # the two are different directories, both present, both startable. `None`
    # for the first start is the upgrade's own: it is what should run next.
    assert armed.launchers == [None, _REPLACED_INSTALL]

    rollback = status["rollback"]
    assert rollback["target_version"] == "3.0.10"
    assert rollback["state"] == "succeeded"
    assert rollback["install"]["ok"] is True
    assert rollback["database"]["restored"] is True
    assert runtime.read_status()["service_pid"] == 222


def test_a_service_still_holding_the_lock_is_never_rolled_back_underneath(monkeypatch, tmp_path):
    """The same failure, the opposite answer, decided by one fact: the lock.

    A restart that failed while a service is still serving has not produced the
    state this recovery exists for, and reinstalling and restoring underneath
    that live process is the damage rather than the repair. Nothing about the
    failure itself distinguishes the two cases -- only what is holding the lock
    when it is over does.
    """

    armed = _upgrade_restart_that_dies_after_migrating(monkeypatch, tmp_path, service_running=True)

    rc = restart_supervisor._run_restart_job(
        job_id="jobheld", delay_seconds=0, vibe_path="/bin/vibe", trigger="upgrade", rollback_to=_rollback_target()
    )

    assert rc == 1
    assert armed.calls == ["stop_runtime", "start_runtime"]
    assert armed.installs == []
    assert _payload_rows(armed.db_path) == ["before the upgrade", "committed by the new version"]
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["rollback"] == {"target_version": "3.0.10", "state": "skipped", "reason": "service_running"}


def test_a_service_that_took_the_lock_and_then_died_is_rolled_back(monkeypatch, tmp_path):
    """The incident this path exists for, in the shape it actually has.

    The lock is taken BEFORE the database is migrated -- it has to be, since the
    migration is what it excludes. So a release that fails on its migration does
    hold the lock, for the seconds it takes to get there and die, and a job that
    stopped at "the pid is recorded" spends those seconds writing
    `state: succeeded` about an instance that ends them with nothing running.

    Which makes the recorded pid unusable as the finish line, in the one case
    that matters. What ends the wait is the service saying it is up, which it
    says after the migration.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("111", encoding="utf-8")
    db_path = paths.get_sqlite_state_path()
    _seed_state_database(db_path)
    calls: list[str] = []
    installs: list[list[str]] = []
    launchers: list = []
    observed_by_the_rolled_back_version: list[list[str]] = []
    alive = {222: True, 333: True, 444: True}

    def start(start_ui=True, launcher=None):
        calls.append("start_runtime")
        launchers.append(launcher)
        if calls.count("start_runtime") == 1:
            # The new version takes its rollback point, commits, and is now in
            # the migration that will kill it -- all of it under the lock.
            create_sqlite_migration_backup(db_path, backups_dir=paths.get_state_backups_dir())
            with sqlite3.connect(db_path) as conn:
                conn.execute("insert into payload (value) values ('committed by the new version')")
            runtime.write_status("running", "pid=222", 222, 333)
            return restart_supervisor.StartedRuntime(222, 333, ("127.0.0.1", 5123))
        observed_by_the_rolled_back_version.append(_payload_rows(db_path))
        return _fake_start_runtime([], service_pid=444, ui_pid=333)

    def started(pid: int) -> bool:
        # Asking is what finds out: the migration failed, so by the time anyone
        # looks a second time the holder is gone and the lock with it.
        alive[pid] = False
        return pid == 444

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda stop_ui=True: _fake_stop_runtime(calls))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", start)
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor.subprocess, "run", _fake_pinned_install(calls, installs))
    # The failed generation is gone: nothing of it is still holding the database
    # open. Stated here rather than left to the host's real process table, which
    # a test may not read and must never depend on -- the sibling helper already
    # isolates this, and without it ``ps -p 444`` (a pid this test invented)
    # lands in the shared ``subprocess.run`` mock as a fake install.
    monkeypatch.setattr(restart_supervisor, "_remaining_service_pids_after_stop", list)
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda *args, **kwargs: False)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: True)
    monkeypatch.setattr(runtime, "service_instance_started", started)
    monkeypatch.setattr(runtime, "verified_service_running", lambda: False)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda host, port, timeout=None: True)

    rc = restart_supervisor._run_restart_job(
        job_id="jobdark", delay_seconds=0, vibe_path="/bin/vibe", trigger="upgrade", rollback_to=_rollback_target()
    )

    assert rc == 3
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["ok"] is False
    assert "did not finish starting" in status["error"]
    assert status["rollback"]["state"] == "succeeded"
    assert "vibe-remote==3.0.10" in installs[0]
    assert observed_by_the_rolled_back_version == [["before the upgrade"]]
    assert _payload_rows(db_path) == ["before the upgrade"]


def test_a_generation_that_was_already_gone_is_quiesced(monkeypatch, tmp_path):
    """The ordinary case, and the one a stop report gets exactly backwards.

    A version that died in its migration has nothing left to kill, so
    `stop_service` reports that it stopped nothing -- the same answer it gives
    for a process that refused to die, because the two states are
    indistinguishable from the report and distinguishable only from the machine.
    A rollback gated on the report therefore refuses to run on the failure it was
    built for, which is the one where the instance is already dark.
    """

    armed = _upgrade_restart_that_dies_after_migrating(monkeypatch, tmp_path, service_running=False)
    monkeypatch.setattr(
        restart_supervisor,
        "_stop_runtime_for_restart",
        lambda stop_ui=True: _fake_stop_runtime(armed.calls, service_stopped=False, ui_stopped=False),
    )

    rc = restart_supervisor._run_restart_job(
        job_id="jobgone", delay_seconds=0, vibe_path="/bin/vibe", trigger="upgrade", rollback_to=_rollback_target()
    )

    assert rc == 1
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["rollback"]["quiesced"] is True
    assert status["rollback"]["state"] == "succeeded"
    assert _payload_rows(armed.db_path) == ["before the upgrade"]


@pytest.mark.parametrize(
    "survivor",
    [
        {"service_pids": [999]},
        {"ui_alive": True},
    ],
    ids=["a-service-process-of-the-failed-generation", "a-ui-process-that-would-not-die"],
)
def test_a_failed_generation_that_will_not_stop_stops_the_rollback_instead(monkeypatch, tmp_path, survivor):
    """Quiescing is a step with an outcome, not a step with a side effect.

    A process that resists termination is the entire reason the stop is there, so
    a rollback that ran it and then carried on regardless would be at its most
    confident in the only case it is about. What follows makes that concrete: the
    restore rewrites a database file that process holds open, and the final start
    adopts its live recorded pid instead of launching what was just reinstalled --
    so the rollback would report success for the version it was rolling back from,
    over a database it had corrupted underneath it.

    So the database is what this asserts on. Untouched is the whole claim; the
    status record only has to be readable enough to send someone to look.
    """

    armed = _upgrade_restart_that_dies_after_migrating(monkeypatch, tmp_path, service_running=False)
    # Whichever process survived, and whatever the stop said about it: what
    # decides is the machine afterwards, so the stop is left reporting success.
    monkeypatch.setattr(
        restart_supervisor, "_remaining_service_pids_after_stop", lambda: list(survivor.get("service_pids", []))
    )
    monkeypatch.setattr(
        runtime, "ui_pid_file_points_to_running_ui", lambda *args, **kwargs: bool(survivor.get("ui_alive"))
    )

    rc = restart_supervisor._run_restart_job(
        job_id="jobstuck", delay_seconds=0, vibe_path="/bin/vibe", trigger="upgrade", rollback_to=_rollback_target()
    )

    assert rc == 1
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["rollback"]["state"] == "failed"
    assert status["rollback"]["quiesced"] is False
    assert "still running" in status["rollback"]["error"]
    # Nothing was installed and nothing was restored: the machine is left exactly
    # as the failed upgrade left it, which is recoverable by hand.
    assert [command for command in armed.installs if any("3.0.10" in argument for argument in command)] == []
    assert _payload_rows(armed.db_path) == ["before the upgrade", "committed by the new version"]


def test_a_restart_with_no_version_to_go_back_to_changes_nothing(monkeypatch, tmp_path):
    """A plain restart is already running what it would reinstall.

    It has no rollback target, so the failure is left exactly as it happened for
    whoever looks at it -- including the database, which nothing here is entitled
    to move backwards. The absent record is the readable part: a failed restart
    with a `rollback_to` and no `rollback` was armed and killed before it could
    recover, which is a different incident from one that was never recoverable.
    """

    armed = _upgrade_restart_that_dies_after_migrating(monkeypatch, tmp_path, service_running=False)

    rc = restart_supervisor._run_restart_job(job_id="jobplain", delay_seconds=0, vibe_path="/bin/vibe", trigger="cli")

    assert rc == 1
    assert armed.calls == ["stop_runtime", "start_runtime"]
    assert armed.installs == []
    assert _payload_rows(armed.db_path) == ["before the upgrade", "committed by the new version"]
    status = runtime.read_json(runtime.get_restart_status_path())
    assert status["rollback_to"] is None
    assert "rollback" not in status


def test_no_failure_branch_can_end_the_job_without_the_rollback_decision():
    """Whether to roll back is asked once, not per failure branch.

    A list of the branches that deserve a rollback is complete only until the
    next branch is added, and the one nobody remembered is exactly the one that
    leaves an instance dark. So the job has a single failure exit, and this
    asserts that structurally: any new branch reaching the raw `_fail` directly
    fails here, at the moment it is written, rather than in the incident.
    """

    tree = ast.parse(textwrap.dedent(inspect.getsource(restart_supervisor._run_restart_job)))
    wrapper = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "fail"
    )
    inside_the_wrapper = {id(node) for node in ast.walk(wrapper)}
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_fail"
    ]

    assert [node for node in calls if id(node) in inside_the_wrapper], "the wrapper is what ends a failed job"
    assert [node for node in calls if id(node) not in inside_the_wrapper] == []


def test_a_recoverable_restart_carries_its_rollback_target_into_the_job(monkeypatch, tmp_path):
    """The target survives the process boundary the restart is built on.

    `schedule_restart` spawns a detached job, so the install to go back to has to
    travel as argv and be read back by the job's own parser. Asserted as a round
    trip rather than as two independent expectations: the two sides are what can
    drift, and a flag renamed on one of them would leave every rollback silently
    disarmed while both halves still look right.

    Asserted as equality against the whole target rather than field by field.
    Argv is the one place the fields are apart, so it is the one place they can
    be separated by accident -- a version arriving without its distribution pins
    a name the old release never had, and one arriving without its install
    reinstalls the right release and then starts the wrong one -- and a field
    added later is carried by this test without it being edited, or fails it.
    """

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

    target = _rollback_target()
    restart_supervisor.schedule_restart(
        delay_seconds=0,
        vibe_path="/bin/vibe",
        trigger="upgrade",
        rollback_to=target,
    )
    upgrade_argv = commands[-1]
    restart_supervisor.schedule_restart(delay_seconds=0, vibe_path="/bin/vibe", trigger="cli")
    plain_argv = commands[-1]
    assert [argument for argument in plain_argv if argument.startswith("--rollback")] == []

    parsed: dict = {}
    monkeypatch.setattr(restart_supervisor, "_run_restart_job", lambda **kwargs: parsed.update(kwargs) or 0)
    assert restart_supervisor.main(upgrade_argv[2:]) == 0
    assert parsed["rollback_to"] == target

    parsed.clear()
    assert restart_supervisor.main(plain_argv[2:]) == 0
    assert parsed["rollback_to"] is None

    # A partial set is refused rather than completed from this process. The job
    # runs the release the rollback is undoing, so "fill in the missing install
    # from here" is the exact wrong answer, and a silent one.
    with pytest.raises(SystemExit):
        restart_supervisor.main(["--job-id", "jobpartial", "--rollback-to", "3.0.10"])
