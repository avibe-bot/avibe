import json
import os
import pytest
import signal
import shlex
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import paths
from vibe import runtime
from vibe import cli
from vibe import remote_access


def _make_fake_uv_tool(
    tmp_path: Path,
    *,
    editable: bool = False,
    revisions: list[str] | None = None,
    copied_executable: bool = False,
    windows_site_packages: bool = False,
) -> Path:
    tool_root = tmp_path / ".local" / "share" / "uv" / "tools" / "vibe-remote"
    bin_dir = tool_root / "bin"
    bin_dir.mkdir(parents=True)
    vibe_bin = bin_dir / "vibe"
    vibe_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    vibe_bin.chmod(0o755)

    shim_dir = tmp_path / ".local" / "bin"
    shim_dir.mkdir(parents=True)
    if copied_executable:
        (shim_dir / "vibe.exe").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (shim_dir / "vibe.exe").chmod(0o755)
    else:
        (shim_dir / "vibe").symlink_to(vibe_bin)

    if windows_site_packages:
        site_packages = tool_root / "Lib" / "site-packages"
    else:
        site_packages = tool_root / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    if editable:
        (site_packages / "_editable_impl_vibe_remote.pth").write_text("/repo\n", encoding="utf-8")

    if revisions is not None:
        versions_dir = site_packages / "storage" / "alembic" / "versions"
        versions_dir.mkdir(parents=True)
        (versions_dir.parent / "__init__.py").write_text("", encoding="utf-8")
        (versions_dir / "__init__.py").write_text("", encoding="utf-8")
        for revision in revisions:
            (versions_dir / f"{revision}_example.py").write_text(f'revision = "{revision}"\n', encoding="utf-8")

    return site_packages


def _write_alembic_revision(db_path: Path, revision: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table alembic_version (version_num varchar(32) not null)")
        conn.execute("insert into alembic_version values (?)", (revision,))


def test_local_cli_installation_items_pass_for_normal_uv_tool(monkeypatch, tmp_path):
    _make_fake_uv_tool(tmp_path, revisions=["20260606_0018"])
    db_path = tmp_path / "state" / "vibe.sqlite"
    _write_alembic_revision(db_path, "20260606_0018")

    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.os, "get_exec_path", lambda: [str(tmp_path / ".local" / "bin")])
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    items = cli._local_cli_installation_items()

    assert [item["status"] for item in items] == ["pass", "pass", "pass", "pass"]
    assert any("SQLite schema revision is recognized" in item["message"] for item in items)


def test_local_cli_installation_items_pass_for_copied_uv_tool_executable(monkeypatch, tmp_path):
    _make_fake_uv_tool(
        tmp_path,
        revisions=["20260606_0018"],
        copied_executable=True,
        windows_site_packages=True,
    )
    db_path = tmp_path / "state" / "vibe.sqlite"
    _write_alembic_revision(db_path, "20260606_0018")

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.os, "get_exec_path", lambda: [str(tmp_path / ".local" / "bin")])
    monkeypatch.setattr(
        cli,
        "_uv_tool_dir",
        lambda *, bin_dir: tmp_path / ".local" / "bin"
        if bin_dir
        else tmp_path / ".local" / "share" / "uv" / "tools",
    )
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    items = cli._local_cli_installation_items()

    assert [item["status"] for item in items] == ["pass", "pass", "pass", "pass"]
    assert any("SQLite schema revision is recognized" in item["message"] for item in items)


def test_local_cli_installation_items_skips_inactive_stale_uv_tool(monkeypatch, tmp_path):
    _make_fake_uv_tool(tmp_path, editable=True, revisions=None)
    active_bin = tmp_path / "venv" / "bin"
    active_bin.mkdir(parents=True)
    active_vibe = active_bin / "vibe"
    active_vibe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    active_vibe.chmod(0o755)

    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.os, "get_exec_path", lambda: [str(active_bin), str(tmp_path / ".local" / "bin")])
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: tmp_path / "missing.sqlite")

    items = cli._local_cli_installation_items()

    assert not any(item["status"] == "fail" for item in items)
    assert any("Active vibe executable is not the uv tool installation" in item["message"] for item in items)


def test_local_cli_installation_items_fails_for_editable_uv_tool(monkeypatch, tmp_path):
    _make_fake_uv_tool(tmp_path, editable=True, revisions=["20260606_0018"])

    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.os, "get_exec_path", lambda: [str(tmp_path / ".local" / "bin")])
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: tmp_path / "missing.sqlite")

    items = cli._local_cli_installation_items()

    assert any(item["status"] == "fail" and "uv tool installation is editable" in item["message"] for item in items)


def test_local_cli_installation_items_fails_when_alembic_scripts_missing(monkeypatch, tmp_path):
    _make_fake_uv_tool(tmp_path, revisions=None)

    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.os, "get_exec_path", lambda: [str(tmp_path / ".local" / "bin")])
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: tmp_path / "missing.sqlite")

    items = cli._local_cli_installation_items()

    assert any(item["status"] == "fail" and "Packaged Alembic scripts are missing" in item["message"] for item in items)


def test_local_cli_installation_items_fails_for_unknown_sqlite_revision(monkeypatch, tmp_path):
    _make_fake_uv_tool(tmp_path, revisions=["20260604_0017"])
    db_path = tmp_path / "state" / "vibe.sqlite"
    _write_alembic_revision(db_path, "20260606_0018")

    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.os, "get_exec_path", lambda: [str(tmp_path / ".local" / "bin")])
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    items = cli._local_cli_installation_items()

    assert any(
        item["status"] == "fail" and "SQLite schema revision is newer than or unknown to this CLI" in item["message"]
        for item in items
    )


def test_default_config_written(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    config = runtime.ensure_config()
    assert config.mode == "self_host"
    assert (tmp_path / ".vibe_remote" / "config" / "config.json").exists()


def test_status_written(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_status("running", detail="pid=123")
    payload = json.loads(paths.get_runtime_status_path().read_text(encoding="utf-8"))
    assert payload["state"] == "running"
    assert payload["detail"] == "pid=123"


def test_render_status_includes_restart_status(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_status("running", detail="pid=123")
    runtime.write_json(
        runtime.get_restart_status_path(),
        {
            "ok": False,
            "state": "failed",
            "job_id": "job-1",
            "error": "start command timed out after 30 seconds",
        },
    )

    payload = json.loads(cli._render_status())

    assert payload["restart"]["ok"] is False
    assert payload["restart"]["state"] == "failed"
    assert payload["restart"]["job_id"] == "job-1"
    assert payload["restart"]["error"] == "start command timed out after 30 seconds"


def test_stop_process_handles_missing_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    assert runtime.stop_process(paths.get_runtime_pid_path()) is False


def test_pid_alive_returns_true_on_permission_error(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "posix", raising=False)

    def _raise_permission(_pid, _sig):
        raise PermissionError()

    monkeypatch.setattr(runtime.os, "kill", _raise_permission)

    assert runtime.pid_alive(12345) is True


def test_pid_alive_returns_false_for_zombie_process(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "posix", raising=False)
    monkeypatch.setattr(runtime.os, "kill", lambda _pid, _sig: None)

    class ZombieProcess:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return runtime.psutil.STATUS_ZOMBIE

    monkeypatch.setattr(runtime.psutil, "Process", ZombieProcess)

    assert runtime.pid_alive(12345) is False


def test_write_json_is_atomic_and_concurrency_safe(tmp_path):
    # write_json must use a unique temp per call so concurrent in-process writers
    # (e.g. overlapping threadpool-dispatched control requests) never collide on a
    # shared temp file, and must never leave the target half-written or temps behind.
    target = tmp_path / "status.json"
    errors: list[Exception] = []

    def hammer(worker: int) -> None:
        try:
            for i in range(50):
                runtime.write_json(target, {"worker": worker, "i": i})
        except Exception as exc:  # noqa: BLE001 - surface any write race to the assert
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    # Target is always a complete JSON document, never a partial write.
    assert isinstance(json.loads(target.read_text(encoding="utf-8")), dict)
    # No leftover temp files in the directory.
    assert list(tmp_path.glob(".status.json.*.tmp")) == []
    assert [p for p in tmp_path.iterdir() if p.name != "status.json"] == []


def test_pid_alive_delegates_to_windows_probe(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "nt", raising=False)
    monkeypatch.setattr(runtime, "_pid_alive_windows", lambda pid: pid == 4321)

    assert runtime.pid_alive(4321) is True
    assert runtime.pid_alive(1234) is False


def test_cli_pid_alive_reuses_runtime_impl(monkeypatch):
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 99)

    assert cli._pid_alive(99) is True
    assert cli._pid_alive(100) is False


def test_stop_process_delegates_to_windows_terminator(tmp_path, monkeypatch):
    pid_path = tmp_path / "service.pid"
    pid_path.write_text("4321", encoding="utf-8")

    monkeypatch.setattr(runtime.os, "name", "nt", raising=False)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(runtime, "_terminate_process_windows", lambda pid, timeout=5: pid == 4321)

    assert runtime.stop_process(pid_path) is True
    assert not pid_path.exists()


def test_stop_process_preserves_pidfile_when_stop_fails(tmp_path, monkeypatch):
    pid_path = tmp_path / "service.pid"
    pid_path.write_text("12345", encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=5: False)

    assert runtime.stop_process(pid_path) is False
    assert pid_path.exists()
    assert pid_path.read_text(encoding="utf-8") == "12345"


def test_stop_pid_reports_failure_when_sigkill_does_not_terminate(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "posix", raising=False)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "write_shutdown_intent", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    calls = []

    def _kill(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(runtime.os, "kill", _kill)

    assert runtime.stop_pid(12345, timeout=0) is False
    assert calls == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


def test_cli_stop_process_reuses_runtime_impl(tmp_path, monkeypatch):
    pid_path = tmp_path / "service.pid"
    pid_path.write_text("123", encoding="utf-8")
    monkeypatch.setattr(runtime, "stop_process", lambda path: path == pid_path)

    assert cli._stop_process(pid_path) is True


def test_cli_stop_opencode_server_uses_runtime_helpers(tmp_path, monkeypatch):
    pid_file = tmp_path / "opencode_server.json"
    pid_file.write_text('{"pid": 321}', encoding="utf-8")

    monkeypatch.setattr(paths, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 321)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: "C:\\opencode.exe serve --port=4096")
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=5: pid == 321)

    assert cli._stop_opencode_server() is True
    assert not pid_file.exists()


def test_proc_cmdline_decode_preserves_argv_boundaries():
    command = runtime._decode_proc_cmdline(b"/tmp/Vibe Tools/cloudflared\x00tunnel\x00run\x00")

    assert command is not None
    assert shlex.split(command)[0] == "/tmp/Vibe Tools/cloudflared"


def test_cmd_restart_schedules_delayed_restart(monkeypatch, capsys):
    scheduled = {}
    stop_called = []
    start_called = []

    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/usr/local/bin/vibe")
    monkeypatch.setattr(
        cli.api,
        "schedule_restart",
        lambda **kwargs: scheduled.update(kwargs) or {"job_id": "job123"},
        raising=False,
    )
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: scheduled.update(kwargs) or {"job_id": "job123"})
    monkeypatch.setattr(cli, "cmd_stop", lambda: stop_called.append(True))
    monkeypatch.setattr(cli, "cmd_vibe", lambda: start_called.append(True))

    assert cli._cmd_restart_with_delay(60) == 0
    assert scheduled == {
        "delay_seconds": 60,
        "vibe_path": "/usr/local/bin/vibe",
        "trigger": "cli",
    }
    assert stop_called == []
    assert start_called == []

    output = capsys.readouterr().out
    assert "Restart scheduled in 1 minute." in output
    assert "Job ID: job123" in output
    assert "restart supervisor will run in the background" in output


def test_cmd_restart_schedules_delayed_restart_without_cached_vibe(monkeypatch):
    scheduled = {}

    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: None)
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: scheduled.update(kwargs) or {"job_id": "job456"})

    assert cli._cmd_restart_with_delay(5) == 0
    assert scheduled == {
        "delay_seconds": 5,
        "vibe_path": None,
        "trigger": "cli",
    }


def test_cmd_restart_schedules_supervisor_by_default(monkeypatch):
    calls = []

    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/usr/local/bin/vibe")
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: calls.append(kwargs) or {"job_id": "job789"})

    assert cli._cmd_restart_with_delay(0) == 0
    assert calls == [{"delay_seconds": 0.0, "vibe_path": "/usr/local/bin/vibe", "trigger": "cli"}]


def test_cmd_stop_ignores_absent_services(monkeypatch):
    status = []

    monkeypatch.setattr(cli, "_pid_file_points_to_live_process", lambda path: False)
    monkeypatch.setattr(runtime, "stop_service", lambda: False)
    monkeypatch.setattr(runtime, "stop_ui", lambda: False)
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda: False)
    monkeypatch.setattr(cli, "_write_status", lambda state, detail=None: status.append((state, detail)))

    assert cli.cmd_stop() == 0
    assert status == [("stopped", None)]


def test_cmd_stop_fails_when_live_service_survives(monkeypatch, capsys):
    status = []
    service_pid = paths.get_runtime_pid_path()

    monkeypatch.setattr(cli, "_pid_file_points_to_live_process", lambda path: path == service_pid)
    monkeypatch.setattr(runtime, "stop_service", lambda: False)
    monkeypatch.setattr(runtime, "stop_ui", lambda: False)
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda: False)
    monkeypatch.setattr(cli, "_write_status", lambda state, detail=None: status.append((state, detail)))

    assert cli.cmd_stop() == 2
    assert status == [("error", "service stop failed")]
    assert "Avibe service did not stop" in capsys.readouterr().err


def test_cmd_stop_fails_when_lock_owner_survives_without_pidfile(monkeypatch, capsys):
    status = []

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "ui_pid_file_points_to_running_ui", lambda: False)
    monkeypatch.setattr(runtime, "stop_service", lambda: False)
    monkeypatch.setattr(runtime, "stop_ui", lambda: False)
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda: False)
    monkeypatch.setattr(cli, "_write_status", lambda state, detail=None: status.append((state, detail)))

    assert cli.cmd_stop() == 2
    assert status == [("error", "service stop failed")]
    assert "Avibe service did not stop" in capsys.readouterr().err


def test_cmd_vibe_uses_start_compatibility_default(monkeypatch):
    calls = []

    monkeypatch.setattr(cli, "cmd_start", lambda: calls.append("start") or 0)

    assert cli.cmd_vibe() == 0

    assert calls == ["start"]


def test_runtime_architecture_items_warn_for_x86_uv_on_apple_silicon(monkeypatch):
    calls = []

    monkeypatch.setattr(cli, "_is_apple_silicon_host", lambda: True)
    monkeypatch.setattr(cli.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cli.shutil, "which", lambda binary: "/usr/local/bin/uv" if binary == "uv" else None)
    monkeypatch.setattr(
        cli,
        "_binary_architecture",
        lambda path: calls.append(path) or "/usr/local/bin/uv: Mach-O 64-bit executable x86_64",
    )

    items = cli._runtime_architecture_items()

    assert calls == ["/usr/local/bin/uv"]
    assert any(item["status"] == "warn" and "uv architecture: x86_64" in item["message"] for item in items)
    assert any(item["status"] == "pass" and "Python runtime architecture: arm64" in item["message"] for item in items)


def test_runtime_architecture_items_warn_for_x86_python_on_apple_silicon(monkeypatch):
    monkeypatch.setattr(cli, "_is_apple_silicon_host", lambda: True)
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cli.shutil, "which", lambda binary: None)

    items = cli._runtime_architecture_items()

    assert any(
        item["status"] == "warn" and item.get("action") == "Reinstall Avibe with native arm64 uv/Python"
        for item in items
    )


def test_runtime_architecture_items_warn_for_unknown_uv_on_apple_silicon(monkeypatch):
    monkeypatch.setattr(cli, "_is_apple_silicon_host", lambda: True)
    monkeypatch.setattr(cli.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cli.shutil, "which", lambda binary: "/usr/local/bin/uv" if binary == "uv" else None)
    monkeypatch.setattr(cli, "_binary_architecture", lambda path: "/usr/local/bin/uv: POSIX shell script")

    items = cli._runtime_architecture_items()

    assert any(
        item["status"] == "warn"
        and "uv architecture: unknown" in item["message"]
        and item.get("action") == "Check whether this uv wrapper launches native arm64 uv"
        for item in items
    )


def test_runtime_architecture_items_pass_for_arm64e_universal_uv_on_apple_silicon(monkeypatch):
    monkeypatch.setattr(cli, "_is_apple_silicon_host", lambda: True)
    monkeypatch.setattr(cli.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cli.shutil, "which", lambda binary: "/usr/local/bin/uv" if binary == "uv" else None)
    monkeypatch.setattr(
        cli,
        "_binary_architecture",
        lambda path: "Mach-O universal binary with 2 architectures: [x86_64] [arm64e]",
    )

    items = cli._runtime_architecture_items()

    assert any(item["status"] == "pass" and "uv architecture: arm64" in item["message"] for item in items)


def test_binary_architecture_follows_uv_symlink(tmp_path, monkeypatch):
    uv_target = tmp_path / "Cellar" / "uv" / "bin" / "uv"
    uv_target.parent.mkdir(parents=True)
    uv_target.write_text("#!/bin/sh\n", encoding="utf-8")
    uv_link = tmp_path / "bin" / "uv"
    uv_link.parent.mkdir()
    uv_link.symlink_to(uv_target)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=f"{uv_target}: Mach-O 64-bit executable arm64\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    output = cli._binary_architecture(str(uv_link))

    assert output and "arm64" in output
    assert calls == [["file", "-b", str(uv_target)]]


def test_binary_architecture_omits_path_prefix_before_token_parsing(tmp_path, monkeypatch):
    uv_path = tmp_path / "arm64-prefix" / "uv"
    uv_path.parent.mkdir(parents=True)
    uv_path.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        return SimpleNamespace(stdout="Mach-O 64-bit executable x86_64\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    output = cli._binary_architecture(str(uv_path))

    assert cli._architecture_token(output) == "x86_64"


def test_cmd_start_ensures_services_without_stopping(monkeypatch):
    calls = []
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: calls.append(("status", args)))
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(cli.runtime, "start_ui", lambda host, port: calls.append(("start_ui", host, port)) or 5678)
    monkeypatch.setattr(cli.runtime, "service_pid_recorded", lambda pid: True)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: calls.append(("runtime_status", args)))

    assert cli.cmd_start() == 0

    assert ("start_service", {"wait_for_ready": False}) in calls
    assert ("start_ui", "127.0.0.1", 5123) in calls
    assert not any(call == "stop" for call in calls)


def test_cmd_start_keeps_ui_up_while_service_lock_is_slow(monkeypatch):
    calls = []
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: calls.append(("status", args)))
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(cli.runtime, "start_ui", lambda host, port: calls.append(("start_ui", host, port)) or 5678)
    monkeypatch.setattr(cli.runtime, "service_pid_recorded", lambda pid: False)
    monkeypatch.setattr(cli.runtime, "wait_for_service_pid", lambda pid, timeout: False)
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: calls.append(("runtime_status", args)))

    assert cli.cmd_start() == 0

    assert calls.index(("start_service", {"wait_for_ready": False})) < calls.index(("start_ui", "127.0.0.1", 5123))
    assert ("runtime_status", ("starting", "waiting for service process", 1234, 5678)) in calls
    assert ("runtime_status", ("starting", "service process is still starting", 1234, 5678)) in calls


def test_cmd_start_fails_only_when_slow_service_exits(monkeypatch):
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )
    statuses = []

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(cli.runtime, "start_ui", lambda host, port: 5678)
    monkeypatch.setattr(cli.runtime, "service_pid_recorded", lambda pid: False)
    monkeypatch.setattr(cli.runtime, "wait_for_service_pid", lambda pid, timeout: False)
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: False)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: statuses.append(args))

    with pytest.raises(RuntimeError):
        cli.cmd_start()

    assert ("error", "service process exited before startup completed", 1234, 5678) in statuses


def test_service_lifecycle_doctor_warns_when_pidfile_missing_but_lock_owner_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    cli.paths.ensure_data_dirs()
    cli.runtime.write_status("running", "pid missing", None, None)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "service_lock_holder_pid", lambda: 1234)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None, include_unverified=False: [])

    items = cli._service_lifecycle_items()

    messages = [item["message"] for item in items if item["status"] == "warn"]
    assert any("pid file does not match" in message for message in messages)
    assert any("Runtime status service_pid does not match" in message for message in messages)


def test_service_lifecycle_doctor_warns_when_extra_service_process_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    cli.paths.ensure_data_dirs()
    cli.runtime.write_status("running", "healthy", 1234, None)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "service_lock_holder_pid", lambda: 1234)
    monkeypatch.setattr(
        cli.runtime,
        "extra_service_process_pids",
        lambda owner_pid=None, include_unverified=False: [2222] if not include_unverified else [2222],
    )

    items = cli._service_lifecycle_items()

    messages = [item["message"] for item in items if item["status"] == "warn"]
    assert any("Extra Avibe service process detected" in message and "2222" in message for message in messages)
    warning = next(item for item in items if item.get("code") == "runtime.extra_service_process")
    assert warning["repair"]["target"] == "duplicate-service-processes"


def test_home_migration_doctor_warns_when_legacy_home_is_unmigrated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    legacy_home = home / ".vibe_remote"
    legacy_home.mkdir(parents=True)
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))

    items = cli._home_migration_items()

    warning = next(item for item in items if item.get("code") == "runtime.legacy_home_unmigrated")
    assert warning["status"] == "warn"
    assert warning["repair"]["target"] == "home-migration"


def test_service_install_doctor_warns_when_service_uses_legacy_package(monkeypatch):
    monkeypatch.setattr(cli, "_current_cli_install_family", lambda: cli.PACKAGE_NAME)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [])
    monkeypatch.setattr(
        cli.runtime,
        "get_process_command",
        lambda pid: "/home/test/.local/share/uv/tools/vibe-remote/bin/python "
        "/home/test/.local/share/uv/tools/vibe-remote/lib/python3.13/site-packages/vibe/service_main.py",
    )

    items = cli._service_install_family_items()

    warning = next(item for item in items if item.get("code") == "runtime.stale_install_process")
    assert warning["repair"]["target"] == "stale-install-runtime"


def test_tool_family_detection_resolves_uv_tool_launcher_symlink(tmp_path):
    tool_root = tmp_path / ".local" / "share" / "uv" / "tools" / "avibe-os"
    bin_dir = tool_root / "bin"
    bin_dir.mkdir(parents=True)
    vibe_bin = bin_dir / "vibe"
    vibe_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    vibe_bin.chmod(0o755)
    shim = tmp_path / ".local" / "bin" / "vibe"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(vibe_bin)

    assert cli._tool_family_from_text(str(shim)) == cli.PACKAGE_NAME


def test_current_cli_install_family_resolves_cached_launcher_symlink(monkeypatch, tmp_path):
    tool_root = tmp_path / ".local" / "share" / "uv" / "tools" / "avibe-os"
    bin_dir = tool_root / "bin"
    bin_dir.mkdir(parents=True)
    vibe_bin = bin_dir / "vibe"
    vibe_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    vibe_bin.chmod(0o755)
    shim = tmp_path / ".local" / "bin" / "vibe"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(vibe_bin)
    monkeypatch.setattr(cli.sys, "executable", "/usr/bin/python3")
    monkeypatch.setenv(cli.CURRENT_VIBE_EXECUTABLE_ENV, str(shim))
    monkeypatch.setattr(cli, "_path_entries_for_executable", lambda name: [])

    assert cli._current_cli_install_family() == cli.PACKAGE_NAME


def test_repair_duplicate_service_processes_stops_only_extra_process(monkeypatch):
    stopped = []
    monkeypatch.setattr(cli.runtime, "service_instance_lock_attached_to_process", lambda: False)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [2222])
    monkeypatch.setattr(cli.runtime, "stop_pid", lambda pid, timeout=5: stopped.append(pid) or True)
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: None)

    result = cli._repair_duplicate_service_processes()

    assert result["status"] == "repaired"
    assert stopped == [2222]
    assert result["stopped_pids"] == [2222]


def test_repair_stale_install_runtime_stops_only_legacy_extra_process(monkeypatch):
    stopped = []
    refreshed = []
    monkeypatch.setattr(cli.runtime, "service_instance_lock_attached_to_process", lambda: False)
    monkeypatch.setattr(cli, "_current_cli_install_family", lambda: cli.PACKAGE_NAME)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1111)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [2222])
    monkeypatch.setattr(
        cli.runtime,
        "get_process_command",
        lambda pid: {
            1111: "/home/test/.local/share/uv/tools/avibe-os/bin/python service_main.py",
            2222: "/home/test/.local/share/uv/tools/vibe-remote/bin/python service_main.py",
        }[pid],
    )
    monkeypatch.setattr(cli.runtime, "stop_pid", lambda pid, timeout=5: stopped.append(pid) or True)
    monkeypatch.setattr(cli.runtime, "start_service", lambda: (_ for _ in ()).throw(AssertionError("must not restart current owner")))
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: refreshed.append(True))

    result = cli._repair_stale_install_runtime()

    assert result["status"] == "repaired"
    assert stopped == [2222]
    assert refreshed == [True]


def test_repair_stale_install_runtime_restarts_when_legacy_owner_is_stopped(monkeypatch):
    stopped = []
    statuses = []
    monkeypatch.setattr(cli.runtime, "service_instance_lock_attached_to_process", lambda: False)
    monkeypatch.setattr(cli, "_current_cli_install_family", lambda: cli.PACKAGE_NAME)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1111)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [])
    monkeypatch.setattr(
        cli.runtime,
        "get_process_command",
        lambda pid: "/home/test/.local/share/uv/tools/vibe-remote/bin/python service_main.py",
    )
    monkeypatch.setattr(cli.runtime, "stop_pid", lambda pid, timeout=5: stopped.append(pid) or True)
    monkeypatch.setattr(cli.runtime, "start_service", lambda: 3333)
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 4444})
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: statuses.append(args))

    result = cli._repair_stale_install_runtime()

    assert result["status"] == "repaired"
    assert stopped == [1111]
    assert result["service_pid"] == 3333
    assert statuses == [("running", "pid=3333", 3333, 4444)]


def test_repair_stale_install_runtime_restarts_after_lockless_legacy_stopped(monkeypatch):
    stopped = []
    statuses = []
    monkeypatch.setattr(cli.runtime, "service_instance_lock_attached_to_process", lambda: False)
    monkeypatch.setattr(cli, "_current_cli_install_family", lambda: cli.PACKAGE_NAME)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: None)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [2222])
    monkeypatch.setattr(
        cli.runtime,
        "get_process_command",
        lambda pid: "/home/test/.local/share/uv/tools/vibe-remote/bin/python service_main.py",
    )
    monkeypatch.setattr(cli.runtime, "stop_pid", lambda pid, timeout=5: stopped.append(pid) or True)
    monkeypatch.setattr(cli.runtime, "start_service", lambda: 3333)
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 4444})
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: statuses.append(args))

    result = cli._repair_stale_install_runtime()

    assert result["status"] == "repaired"
    assert stopped == [2222]
    assert result["service_pid"] == 3333
    assert statuses == [("running", "pid=3333", 3333, 4444)]


def test_repair_stale_install_runtime_reports_failed_when_restart_fails(monkeypatch):
    stopped = []
    refreshed = []
    monkeypatch.setattr(cli.runtime, "service_instance_lock_attached_to_process", lambda: False)
    monkeypatch.setattr(cli, "_current_cli_install_family", lambda: cli.PACKAGE_NAME)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1111)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [])
    monkeypatch.setattr(
        cli.runtime,
        "get_process_command",
        lambda pid: "/home/test/.local/share/uv/tools/vibe-remote/bin/python service_main.py",
    )
    monkeypatch.setattr(cli.runtime, "stop_pid", lambda pid, timeout=5: stopped.append(pid) or True)
    monkeypatch.setattr(cli.runtime, "start_service", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: refreshed.append(True))

    result = cli._repair_stale_install_runtime()

    assert result["status"] == "failed"
    assert "failed to start" in result["message"]
    assert stopped == [1111]
    assert result["stopped_pids"] == [1111]
    assert refreshed == [True]


def test_repair_home_migration_skips_empty_home_without_initializing(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))

    result = cli._repair_home_migration()

    assert result["status"] == "skipped"
    assert not (home / ".avibe").exists()
    assert not (home / ".vibe_remote").exists()


def test_repair_home_migration_fails_when_compatibility_symlink_is_missing(monkeypatch, tmp_path):
    home = tmp_path / "home"
    avibe_home = home / ".avibe"
    legacy_home = home / ".vibe_remote"
    legacy_home.mkdir(parents=True)
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli.paths, "migrate_default_home", lambda: legacy_home.rename(avibe_home) or avibe_home)
    monkeypatch.setattr(
        cli.paths,
        "ensure_data_dirs",
        lambda: (_ for _ in ()).throw(AssertionError("failed migration must not declare data dirs ready")),
    )

    result = cli._repair_home_migration()

    assert result["status"] == "failed"
    assert "compatibility symlink" in result["message"]
    assert avibe_home.exists()
    assert not legacy_home.exists()


def test_repair_stale_restart_state_removes_marker(monkeypatch):
    paths.ensure_data_dirs()
    restart_path = runtime.get_restart_status_path()
    runtime.write_json(restart_path, {"state": "running", "supervisor_pid": 4242})
    old_timestamp = time.time() - 120
    os.utime(restart_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: False)
    refreshed = []
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: refreshed.append(True))

    result = cli._repair_stale_restart_state()

    assert result["status"] == "repaired"
    assert not restart_path.exists()
    assert refreshed == [True]


def test_repair_stale_restart_state_removes_old_marker_without_start_time(monkeypatch):
    paths.ensure_data_dirs()
    restart_path = runtime.get_restart_status_path()
    runtime.write_json(restart_path, {"state": "running", "supervisor_pid": 4242})
    old_timestamp = time.time() - 120
    os.utime(restart_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(
        cli.runtime,
        "process_create_time",
        lambda pid: (_ for _ in ()).throw(AssertionError("missing start time should use marker age")),
    )
    refreshed = []
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: refreshed.append(True))

    result = cli._repair_stale_restart_state()

    assert result["status"] == "repaired"
    assert not restart_path.exists()
    assert refreshed == [True]


def test_doctor_repair_dry_run_does_not_probe_runtime(monkeypatch):
    def fail_runtime_probe(*args, **kwargs):
        raise AssertionError("dry-run must not touch runtime probes")

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", fail_runtime_probe)

    result = cli._repair_doctor_targets(["duplicate-service-processes"], dry_run=True)

    assert result["ok"] is True
    assert result["results"][0]["status"] == "planned"


def test_restart_parser_accepts_delay_seconds():
    parser = cli.build_parser()
    args = parser.parse_args(["restart", "--delay-seconds", "60"])

    assert args.command == "restart"
    assert args.delay_seconds == 60


def test_doctor_parser_accepts_repair_target_and_dry_run():
    parser = cli.build_parser()
    args = parser.parse_args(["doctor", "repair", "duplicate-service-processes", "--dry-run"])

    assert args.command == "doctor"
    assert args.doctor_action == "repair"
    assert args.doctor_repair_targets == ["duplicate-service-processes"]
    assert args.dry_run is True


def test_doctor_bare_dry_run_does_not_request_repair():
    parser = cli.build_parser()
    args = parser.parse_args(["doctor", "--dry-run"])

    assert args.command == "doctor"
    assert args.doctor_action is None
    assert cli._doctor_repair_requested(args) is False


def test_start_parser_accepts_start_command():
    parser = cli.build_parser()
    args = parser.parse_args(["start"])

    assert args.command == "start"


def test_remote_parser_accepts_pairing_command():
    parser = cli.build_parser()
    args = parser.parse_args(["remote", "pair", "vrp_test", "--device-name", "Mac Studio"])

    assert args.command == "remote"
    assert args.remote_command == "pair"
    assert args.pairing_key == "vrp_test"
    assert args.device_name == "Mac Studio"


def test_remote_parser_allows_guided_setup_without_subcommand():
    parser = cli.build_parser()
    args = parser.parse_args(["remote"])

    assert args.command == "remote"
    assert args.remote_command is None


def test_remote_parser_accepts_status_json():
    parser = cli.build_parser()
    args = parser.parse_args(["remote", "status", "--json"])

    assert args.command == "remote"
    assert args.remote_command == "status"
    assert args.json is True


def test_cmd_remote_pair_prompts_and_reports_success(monkeypatch, capsys):
    captured = {}

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "vrp_prompt")

    def fake_pair(pairing_key: str, backend_url: str, device_name: str):
        captured.update(
            {
                "pairing_key": pairing_key,
                "backend_url": backend_url,
                "device_name": device_name,
            }
        )
        return {
            "ok": True,
            "public_url": "https://alex.avibe.bot",
            "running": True,
            "start": {"ok": True},
        }

    monkeypatch.setattr(remote_access, "pair", fake_pair)

    result = cli.cmd_remote_pair(
        SimpleNamespace(
            pairing_key=None,
            backend_url="https://backend.test",
            device_name="Mac Studio",
            json=False,
        )
    )

    assert result == 0
    assert captured == {
        "pairing_key": "vrp_prompt",
        "backend_url": "https://backend.test",
        "device_name": "Mac Studio",
    }
    output = capsys.readouterr().out
    assert "Remote access is ready" in output
    assert "https://alex.avibe.bot" in output
    assert "vibe remote status" in output


def test_cmd_remote_pair_fails_when_tunnel_does_not_start(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "vrp_prompt")

    def fake_pair(pairing_key: str, backend_url: str, device_name: str):
        return {
            "ok": True,
            "public_url": "https://alex.avibe.bot",
            "running": False,
            "start": {"ok": False, "error": "cloudflared_spawn_failed", "detail": "spawn failed"},
        }

    monkeypatch.setattr(remote_access, "pair", fake_pair)

    result = cli.cmd_remote_pair(
        SimpleNamespace(
            pairing_key=None,
            backend_url="https://backend.test",
            device_name="Mac Studio",
            json=False,
        )
    )

    assert result == 1
    output = capsys.readouterr()
    assert "Step 3: Pairing saved" in output.out
    assert "Remote access is paired, but the tunnel did not start." in output.err
    assert "vibe remote start" in output.err


def test_cmd_remote_pair_json_marks_start_failure_as_not_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "vrp_prompt")

    def fake_pair(pairing_key: str, backend_url: str, device_name: str):
        return {
            "ok": True,
            "pairing": {"ok": True},
            "public_url": "https://alex.avibe.bot",
            "running": False,
            "start": {"ok": False, "error": "cloudflared_spawn_failed"},
        }

    monkeypatch.setattr(remote_access, "pair", fake_pair)

    result = cli.cmd_remote_pair(
        SimpleNamespace(
            pairing_key=None,
            backend_url="https://backend.test",
            device_name="Mac Studio",
            json=True,
        )
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["pairing"] == {"ok": True}
    assert payload["start"]["ok"] is False
    assert payload["error"] == "cloudflared_spawn_failed"


def test_cmd_remote_setup_explains_before_prompting_for_key(monkeypatch, capsys):
    events = []

    monkeypatch.setattr(remote_access, "status", lambda: {"ok": True, "paired": False})
    monkeypatch.setattr("builtins.input", lambda prompt: events.append(("ready", prompt)) or "")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: events.append(("key", prompt)) or "vrp_prompt")

    def fake_pair(pairing_key: str, backend_url: str, device_name: str):
        events.append(("pair", pairing_key, backend_url, device_name))
        return {
            "ok": True,
            "public_url": "https://alex.avibe.bot",
            "running": True,
            "start": {"ok": True},
        }

    monkeypatch.setattr(remote_access, "pair", fake_pair)

    result = cli.cmd_remote_setup(SimpleNamespace(remote_command=None))

    assert result == 0
    assert events == [
        ("ready", "Press Enter when you have copied the pairing key, or Ctrl+C to cancel."),
        ("key", "Paste pairing key (input hidden): "),
        ("pair", "vrp_prompt", "https://avibe.bot", "avibe"),
    ]
    output = capsys.readouterr().out
    assert "Open https://avibe.bot" in output
    assert "Create a new remote-access bot" in output
    assert "Copy the one-time pairing key" in output
    assert output.index("Open https://avibe.bot") < output.index("Pairing this device")


def test_cmd_remote_setup_shows_existing_pairing_without_prompt(monkeypatch, capsys):
    events = []

    monkeypatch.setattr(
        remote_access,
        "status",
        lambda: {
            "ok": True,
            "paired": True,
            "running": True,
            "public_url": "https://alex.avibe.bot",
        },
    )
    monkeypatch.setattr("builtins.input", lambda prompt: events.append(("ready", prompt)) or "")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: events.append(("key", prompt)) or "vrp_prompt")
    monkeypatch.setattr(remote_access, "pair", lambda *args, **kwargs: events.append(("pair", args, kwargs)))

    result = cli.cmd_remote_setup(SimpleNamespace(remote_command=None))

    assert result == 0
    assert events == []
    output = capsys.readouterr().out
    assert "Remote access is already configured." in output
    assert "https://alex.avibe.bot" in output
    assert "vibe remote pair" in output


def test_cmd_remote_pair_maps_invalid_key_to_user_action(monkeypatch, capsys):
    monkeypatch.setattr(
        remote_access,
        "pair",
        lambda *args, **kwargs: {"ok": False, "error": "invalid_pairing_key", "status": 400},
    )

    result = cli.cmd_remote_pair(
        SimpleNamespace(
            pairing_key="vrp_bad",
            backend_url="https://backend.test",
            device_name="Mac Studio",
            json=False,
        )
    )

    assert result == 1
    error_output = capsys.readouterr().err
    assert "Pairing key is invalid or expired." in error_output
    assert "https://avibe.bot" in error_output
    assert "vibe remote" in error_output


def test_cmd_remote_pair_missing_key_fails_without_request(monkeypatch, capsys):
    pair_calls = []

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")
    monkeypatch.setattr(remote_access, "pair", lambda *args, **kwargs: pair_calls.append(args))

    result = cli.cmd_remote_pair(
        SimpleNamespace(
            pairing_key=None,
            backend_url="https://backend.test",
            device_name="Mac Studio",
            json=False,
        )
    )

    assert result == 1
    assert pair_calls == []
    assert "missing pairing key" in capsys.readouterr().err


@pytest.mark.parametrize("raw_value", ["nan", "inf", "-inf"])
def test_restart_parser_rejects_non_finite_delay_seconds(raw_value):
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["restart", "--delay-seconds", raw_value])


def test_stop_pid_handles_process_lookup_race(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "posix", raising=False)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "write_shutdown_intent", lambda *args, **kwargs: None)

    def _kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(runtime.os, "kill", _kill)

    assert runtime.stop_pid(12345) is False


def test_stop_pid_handles_permission_error(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "posix", raising=False)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(runtime, "write_shutdown_intent", lambda *args, **kwargs: None)

    def _kill(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(runtime.os, "kill", _kill)

    assert runtime.stop_pid(12345) is False


def test_stop_pid_writes_shutdown_intent_before_sigterm(monkeypatch):
    monkeypatch.setattr(runtime.os, "name", "posix", raising=False)
    alive_results = iter([True, False])
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: next(alive_results))
    calls = []

    def _kill(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(runtime.os, "kill", _kill)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime, "write_shutdown_intent", lambda *args, **kwargs: calls.append(("intent", args, kwargs)))

    assert runtime.stop_pid(12345) is True
    assert calls[0][0] == "intent"
    assert calls[0][1] == (12345,)
    assert calls[0][2]["signum"] == signal.SIGTERM
    assert calls[1] == (12345, signal.SIGTERM)


def test_start_ui_reuses_existing_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(runtime, "ui_server_healthy", lambda host, port: host == "127.0.0.1" and port == 5123)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: (
            f"{sys.executable} -c "
            "\"from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5123)\""
            if pid == 12345
            else None
        ),
    )

    def fail_spawn(*_args, **_kwargs):
        raise AssertionError("start_ui should not spawn when an existing UI process is healthy")

    monkeypatch.setattr(runtime, "spawn_background", fail_spawn)

    assert runtime.start_ui("127.0.0.1", 5123) == 12345


def test_start_ui_does_not_reuse_unrelated_pid_with_healthy_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(runtime, "ui_server_healthy", lambda host, port: True)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: "/usr/bin/unrelated --work" if pid == 12345 else None)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda host, port: True)

    def fail_stop(pid, timeout=5):
        raise AssertionError(f"unrelated pid should not be stopped: {pid}")

    def fake_spawn(args, pid_path, stdout_name, stderr_name, env=None):
        pid_path.write_text("67890", encoding="utf-8")
        return 67890

    monkeypatch.setattr(runtime, "stop_pid", fail_stop)
    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)

    assert runtime.start_ui("127.0.0.1", 5123) == 67890
    assert paths.get_runtime_ui_pid_path().read_text(encoding="utf-8") == "67890"


def test_start_ui_replaces_stale_live_pid_when_health_check_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")
    stopped = []

    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(runtime, "ui_server_healthy", lambda host, port: False)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: (
            f"{sys.executable} -c "
            "\"from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5123)\""
            if pid == 12345
            else None
        ),
    )
    monkeypatch.setattr(runtime, "stop_pid", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda host, port: True)

    def fake_spawn(args, pid_path, stdout_name, stderr_name, env=None):
        assert args[-1] == "from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5123)"
        pid_path.write_text("67890", encoding="utf-8")
        return 67890

    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)

    assert runtime.start_ui("127.0.0.1", 5123) == 67890
    assert stopped == [12345]
    assert paths.get_runtime_ui_pid_path().read_text(encoding="utf-8") == "67890"


def test_start_ui_does_not_stop_unrelated_reused_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(runtime, "ui_server_healthy", lambda host, port: False)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: "/usr/bin/unrelated --work" if pid == 12345 else None)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda host, port: True)

    def fail_stop(pid, timeout=5):
        raise AssertionError(f"unrelated pid should not be stopped: {pid}")

    def fake_spawn(args, pid_path, stdout_name, stderr_name, env=None):
        pid_path.write_text("67890", encoding="utf-8")
        return 67890

    monkeypatch.setattr(runtime, "stop_pid", fail_stop)
    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)

    assert runtime.start_ui("127.0.0.1", 5123) == 67890
    assert paths.get_runtime_ui_pid_path().read_text(encoding="utf-8") == "67890"


def test_start_ui_waits_for_replacement_health(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    waited = []

    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda host, port: waited.append((host, port)) or True)
    monkeypatch.setattr(runtime, "spawn_background", lambda *args, **kwargs: 67890)

    assert runtime.start_ui("127.0.0.1", 5123) == 67890
    assert waited == [("127.0.0.1", 5123)]


def test_start_ui_can_skip_replacement_health_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()

    def fail_wait(host, port):
        raise AssertionError(f"start_ui should not wait for health: {host}:{port}")

    monkeypatch.setattr(runtime, "wait_for_ui_server", fail_wait)
    monkeypatch.setattr(runtime, "spawn_background", lambda *args, **kwargs: 67890)

    assert runtime.start_ui("127.0.0.1", 5123, wait_for_ready=False) == 67890


def test_ui_health_url_uses_loopback_for_wildcard_bind():
    assert runtime._ui_health_url("0.0.0.0", 5100) == "http://127.0.0.1:5100/health"
    assert runtime._ui_health_url("::", 5100) == "http://[::1]:5100/health"


def test_shutdown_intent_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"cmd-{pid}")

    runtime.write_shutdown_intent(12345, reason="test")
    payload = runtime.consume_shutdown_intent(12345, signal.SIGTERM)

    assert payload is not None
    assert payload["target_pid"] == 12345
    assert payload["sender_pid"] == os.getpid()
    assert not runtime.get_shutdown_intent_path().exists()


def test_shutdown_intent_rejects_stale_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    runtime.write_json(
        runtime.get_shutdown_intent_path(),
        {
            "target_pid": 12345,
            "signum": signal.SIGTERM,
            "created_at": 900.0,
        },
    )

    assert runtime.consume_shutdown_intent(12345, signal.SIGTERM) is None
