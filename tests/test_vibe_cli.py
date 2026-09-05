import asyncio
import builtins
import hashlib
import json
import os
import pytest
import re
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


def test_retention_help_reads_raw_config_without_loading_or_migrating(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"runtime": {"agent_events_trace_retention_days": 90}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.paths, "get_config_path", lambda: config_path)

    def _load_should_not_run(*_args, **_kwargs):
        raise AssertionError("help construction must not load or migrate V2Config")

    monkeypatch.setattr(cli.V2Config, "load", _load_should_not_run)
    assert cli._configured_trace_retention_days("en") == 90
    cli.build_parser()


def test_retention_help_ignores_oversized_configured_window(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"runtime": {"agent_events_trace_retention_days": 1_000_000}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.paths, "get_config_path", lambda: config_path)

    assert cli._configured_trace_retention_days("en") == 30


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


@pytest.mark.parametrize("state", ["running", "starting", "stopped", "error", "degraded"])
@pytest.mark.parametrize("ok", [False, None, True], ids=["failed", "in-flight", "succeeded"])
def test_a_restart_record_survives_every_status_write(tmp_path, monkeypatch, state, ok):
    """Nothing about writing runtime status touches the restart record.

    The record is the restart subsystem's own account of its last job, and doctor
    reads it against live facts rather than against anything a status write
    remembered. Seeded across every outcome and every state a caller can write, so
    a status path added later inherits the assertion instead of needing its own.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    restart_path = runtime.get_restart_status_path()
    payload = {"ok": ok, "state": "failed" if ok is False else "succeeded", "job_id": "job-1"}
    runtime.write_json(restart_path, payload)

    runtime.write_status(state, detail="pid=123")

    assert runtime.read_status()["state"] == state
    assert runtime.read_json(restart_path) == payload


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


def test_render_status_includes_internal_server_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_json(
        paths.get_internal_server_status_path(),
        {
            "state": "error",
            "error": "internal_server_unavailable",
            "detail": "internal dispatch socket owner mismatch",
        },
    )

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["internal_server"] == {
        "state": "error",
        "error": "internal_server_unavailable",
        "detail": "internal dispatch socket owner mismatch",
    }


def test_render_status_does_not_report_a_ready_internal_server_without_a_service(tmp_path, monkeypatch):
    """A live "ready" with no service owner is stale, not a report.

    The internal server runs on the service process's loop and owns a socket
    that dies with it, so it cannot outlive its owner. A SIGKILL leaves no
    shutdown path to correct the file, which would otherwise make `vibe status`
    claim a ready internal server against a stopped service.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_json(paths.get_internal_server_status_path(), {"state": "ready"})
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=False: None)

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["running"] is False
    assert payload["internal_server"] == {"state": "stopped", "stale": True}


def test_render_status_keeps_a_recorded_error_when_the_service_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_json(
        paths.get_internal_server_status_path(),
        {"state": "error", "error": "internal_server_unavailable"},
    )
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=False: None)

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    # A terminal state is why the service is gone; do not overwrite it.
    assert payload["internal_server"]["state"] == "error"
    assert "stale" not in payload["internal_server"]


def _age_status_record(seconds: float) -> None:
    """Backdate the persisted status by `seconds`, leaving everything else alone."""

    path = paths.get_runtime_status_path()
    status = runtime.read_json(path) or {}
    status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
    runtime.write_json(path, status)


def test_render_status_keeps_a_recent_starting_record_over_a_stopped_service(tmp_path, monkeypatch):
    """The window `starting` exists to cover, and it must still be covered.

    Between the spawn and the moment the child takes the instance lock nothing
    resolves as running. Reporting that window as `stopped` would make every
    healthy start look like a failure, so a resolved `stopped` does not overwrite
    a fresh `starting`.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_status("starting", detail="waiting for service process", service_pid=4242)
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=False: None)

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["state"] == "starting"


def test_render_status_stops_believing_a_starting_record_that_outlived_its_start(tmp_path, monkeypatch):
    """`starting` is a claim about a process expected to arrive, so it expires.

    Unlike `setup` and `error`, which describe a machine deliberately not
    running, `starting` describes one in flight -- and a release that dies inside
    its own startup leaves that record behind with nothing coming to replace it.
    Believing it forever is how an instance with no service reports that it is
    coming up, for eight days. The deadline is `wait_for_service_ready`'s own:
    past the point where the waiter gives up, the record no longer describes a
    machine coming up.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_status("starting", detail="waiting for service process", service_pid=4242)
    _age_status_record(runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS + 30)
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=False: None)

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["state"] == "stopped"
    assert payload["running"] is False


def test_render_status_treats_an_undatable_starting_record_as_expired(tmp_path, monkeypatch):
    """An undatable `starting` that is believed lasts forever.

    Every status write stamps `updated_at`, so a record without a readable one
    cannot be dated at all -- and the whole point of the deadline is that this
    state must not be permanent. Defaulting the other way would restore the
    original bug through the one input the deadline cannot measure.
    """

    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    runtime.ensure_dirs()
    runtime.write_status("starting", detail="waiting for service process", service_pid=4242)
    status_path = paths.get_runtime_status_path()
    runtime.write_json(status_path, {**(runtime.read_json(status_path) or {}), "updated_at": "not a timestamp"})
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=False: None)

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["state"] == "stopped"


def test_render_status_reports_degraded_show_checkpoints_without_git(monkeypatch):
    monkeypatch.setattr("core.git_binary.resolve_git", lambda: None)

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["show_git_checkpoints"] == "degraded: Git checkpoint service unavailable"


def test_doctor_reports_degraded_show_checkpoints_without_git(monkeypatch):
    monkeypatch.setattr("core.git_binary.resolve_git", lambda: None)

    assert cli._show_git_checkpoint_items() == [
        {
            "status": "warn",
            "message": "Show Page checkpointing is degraded because Git is unavailable",
            "code": "runtime.show_git_unavailable",
        }
    ]


def test_doctor_surfaces_configuration_recovery_warnings(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    doctor_path = tmp_path / "doctor.json"
    warning = "Recovered invalid config section 'platforms': sk-leaked-platform-token"
    config = cli.V2Config.default()
    config.language = "zh"
    config.load_warnings = (warning,)

    monkeypatch.setattr(paths, "get_config_path", lambda: config_path)
    monkeypatch.setattr(paths, "get_runtime_doctor_path", lambda: doctor_path)
    monkeypatch.setattr(cli.V2Config, "load", lambda _path: config)
    monkeypatch.setattr(cli, "_home_migration_items", lambda: [])
    monkeypatch.setattr(cli, "_service_lifecycle_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_service_install_family_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_restart_state_items", lambda: [])
    monkeypatch.setattr(cli, "_runtime_architecture_items", lambda: [])
    monkeypatch.setattr(cli, "_show_git_checkpoint_items", lambda: [])
    monkeypatch.setattr(cli, "_managed_dependencies_doctor_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_show_runtime_doctor_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_local_cli_installation_items", lambda: [])
    monkeypatch.setattr(cli.api, "detect_cli", lambda _path: {})

    result = cli._doctor()

    config_group = next(group for group in result["groups"] if group["name"] == "Configuration")
    recovery_item = next(item for item in config_group["items"] if item.get("code") == "config.recovery")
    assert recovery_item["status"] == "warn"
    assert recovery_item["code"] == "config.recovery"
    assert recovery_item["message"] != warning
    assert recovery_item["action"] == cli.i18n_t("error.configRecovery.action", "zh")
    assert "sk-leaked-platform-token" not in json.dumps(result)
    assert result["summary"]["warn"] >= 1


def test_status_and_doctor_use_running_checkpoint_service_state(monkeypatch):
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda **_kwargs: 1234)
    monkeypatch.setattr("core.show_git.show_git_checkpointing_active", lambda: False)
    monkeypatch.setattr("core.git_binary.resolve_git", lambda: object())

    payload = json.loads(runtime.render_status(detect_extra_processes=False))

    assert payload["show_git_checkpoints"] == "degraded: Git checkpoint service unavailable"
    assert cli._show_git_checkpoint_items()[0]["code"] == "runtime.show_git_unavailable"


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


def _no_live_runtime_processes(monkeypatch):
    """Keep cmd_start's process-reuse probes off the developer's real state."""

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: None)


def test_cmd_start_ensures_services_without_stopping(monkeypatch):
    calls = []
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )

    _no_live_runtime_processes(monkeypatch)
    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: calls.append(("status", args)))
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda host, port, **kwargs: calls.append(("start_ui", host, port, kwargs)) or 5678,
    )
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: pid)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: calls.append(("runtime_status", args)))

    assert cli.cmd_start() == 0

    service_call = next(call for call in calls if call[0] == "start_service")
    ui_call = next(call for call in calls if call[0] == "start_ui")
    assert service_call[1]["wait_for_ready"] is False
    assert service_call[1]["memory_ui_secret"] == ui_call[3]["memory_ui_secret"]
    assert ui_call[1:3] == ("127.0.0.1", 5123)
    assert not any(call == "stop" for call in calls)


def test_cmd_start_keeps_ui_up_while_service_lock_is_slow(monkeypatch):
    calls = []
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )

    _no_live_runtime_processes(monkeypatch)
    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: calls.append(("status", args)))
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda host, port, **kwargs: calls.append(("start_ui", host, port, kwargs)) or 5678,
    )
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: None)
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: calls.append(("runtime_status", args)))

    assert cli.cmd_start() == 0

    service_index = next(i for i, call in enumerate(calls) if call[0] == "start_service")
    ui_index = next(i for i, call in enumerate(calls) if call[0] == "start_ui")
    assert service_index < ui_index
    assert calls[service_index][1]["memory_ui_secret"] == calls[ui_index][3]["memory_ui_secret"]
    assert ("runtime_status", ("starting", "waiting for service process", 1234, 5678)) in calls
    assert ("runtime_status", ("starting", "service process is still starting", 1234, 5678)) in calls


def test_cmd_start_against_a_live_service_neither_resets_its_uptime_nor_skips_the_wait(monkeypatch):
    """`vibe start` is idempotent, and has to be observably idempotent.

    `write_status` carries `started_at` forward only across consecutive `running`
    writes, so announcing a `starting` transition for a service this command did
    not start resets the recorded uptime to now -- every status consumer, the
    dashboard included, then reads a service that has been up for weeks as one
    that just began starting.

    The wait is the other half, and it is the half that was broken the other way:
    the predicate that used to guard it was the service lock, taken before the
    database is migrated, so it read true for a process that had not finished
    starting and skipped the wait in exactly the case the wait exists for. So
    only the provisional write is conditional; the wait is always asked, and a
    service that is already up answers it on the first probe.
    """

    calls = []
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: None)
    # The live pair: `start_service` hands back the pid that already holds the
    # lock, which is what makes this a reuse rather than a start.
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **kwargs: 1234)
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: 5678)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(cli.runtime, "start_ui", lambda host, port, **kwargs: 5678)
    monkeypatch.setattr(
        cli.runtime,
        "wait_for_service_ready",
        lambda pid, timeout=None: calls.append(("wait", pid)) or pid,
    )
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: calls.append(("runtime_status", args)))

    assert cli.cmd_start() == 0

    assert ("wait", 1234) in calls, "the readiness wait must be asked even for a service already up"
    announced = [args[0] for kind, args in calls if kind == "runtime_status"]
    assert "starting" not in announced, f"a reused service was announced as starting: {announced}"


def test_cmd_start_fails_only_when_slow_service_exits(monkeypatch):
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )
    statuses = []

    _no_live_runtime_processes(monkeypatch)
    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: 1234)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(cli.runtime, "start_ui", lambda host, port, **kwargs: 5678)
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: None)
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: False)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: statuses.append(args))

    with pytest.raises(RuntimeError):
        cli.cmd_start()

    assert ("error", "service process exited before startup completed", 1234, 5678) in statuses


def _memory_start_config(*, memory_enabled: bool = True, language: str = "en") -> SimpleNamespace:
    return SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
        memory=SimpleNamespace(enabled=memory_enabled),
        language=language,
    )


def test_cmd_start_restarts_a_surviving_ui_so_it_shares_the_new_service_secret(monkeypatch):
    calls = []
    config = _memory_start_config()

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: 5678)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(
        cli.runtime,
        "stop_ui",
        lambda **kwargs: calls.append(("stop_ui", kwargs)) or True,
    )
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda host, port, **kwargs: calls.append(("start_ui", kwargs)) or 9012,
    )
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: pid)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: None)

    assert cli.cmd_start() == 0

    kinds = [call[0] for call in calls]
    assert kinds == ["start_service", "stop_ui", "start_ui"]
    assert calls[1][1] == {"stop_remote_access": False}
    service_secret = calls[0][1]["memory_ui_secret"]
    assert service_secret
    assert calls[2][1]["memory_ui_secret"] == service_secret


@pytest.mark.parametrize(
    ("language", "expected_warning"),
    [
        ("en", "Memory Settings content is unavailable"),
        ("zh", "记忆设置内容暂不可用"),
    ],
)
def test_cmd_start_never_signs_with_a_secret_a_reused_service_cannot_verify(
    monkeypatch,
    capsys,
    language,
    expected_warning,
):
    calls = []
    config = _memory_start_config(language=language)

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **kwargs: 1234)
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: None)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(
        cli.runtime,
        "stop_ui",
        lambda **kwargs: calls.append(("stop_ui", kwargs)) or True,
    )
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda host, port, **kwargs: calls.append(("start_ui", kwargs)) or 9012,
    )
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: pid)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: None)

    assert cli.cmd_start() == 0

    assert [call[0] for call in calls] == ["start_service", "start_ui"]
    assert calls[1][1]["memory_ui_secret"] is None
    output = capsys.readouterr().out
    assert expected_warning in output
    assert "vibe stop" in output


def test_cmd_start_keeps_a_reused_pair_untouched(monkeypatch, capsys):
    calls = []
    config = _memory_start_config()

    monkeypatch.setattr(cli.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **kwargs: 1234)
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: 5678)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: calls.append(("start_service", kwargs)) or 1234)
    monkeypatch.setattr(
        cli.runtime,
        "stop_ui",
        lambda **kwargs: calls.append(("stop_ui", kwargs)) or True,
    )
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda host, port, **kwargs: calls.append(("start_ui", kwargs)) or 5678,
    )
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: pid)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: None)

    assert cli.cmd_start() == 0

    assert [call[0] for call in calls] == ["start_service", "start_ui"]
    assert calls[1][1]["memory_ui_secret"] is None
    assert "Memory Settings content is unavailable" not in capsys.readouterr().out


def test_live_ui_server_pid_reads_only_a_verified_ui_process(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    cli.paths.get_runtime_ui_pid_path().parent.mkdir(parents=True, exist_ok=True)
    cli.paths.get_runtime_ui_pid_path().write_text("4321\n", encoding="utf-8")

    monkeypatch.setattr(cli.runtime, "ui_pid_file_points_to_running_ui", lambda: True)
    assert cli._live_ui_server_pid() == 4321

    monkeypatch.setattr(cli.runtime, "ui_pid_file_points_to_running_ui", lambda: False)
    assert cli._live_ui_server_pid() is None


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


def test_fast_service_lifecycle_doctor_skips_extra_process_scan(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    cli.paths.ensure_data_dirs()
    cli.runtime.write_status("running", "healthy", 1234, None)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "service_lock_holder_pid", lambda: 1234)

    def fail_process_scan(*args, **kwargs):
        raise AssertionError("fast diagnostics must not scan service processes")

    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", fail_process_scan)

    items = cli._service_lifecycle_items(detect_extra_processes=False)

    skipped = next(item for item in items if item.get("code") == "runtime.deep_service_process_scan_skipped")
    assert skipped["status"] == "pass"


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


def test_fast_service_install_doctor_skips_extra_process_scan(monkeypatch):
    monkeypatch.setattr(cli, "_current_cli_install_family", lambda: cli.PACKAGE_NAME)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: None)

    def fail_process_scan(*args, **kwargs):
        raise AssertionError("fast diagnostics must not scan service processes")

    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", fail_process_scan)

    items = cli._service_install_family_items(detect_extra_processes=False)

    assert any(item["status"] == "pass" for item in items)


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
    paths.ensure_data_dirs()
    monkeypatch.setattr(cli.runtime, "service_instance_lock_attached_to_process", lambda: False)
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 1234)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda owner_pid=None: [2222])
    monkeypatch.setattr(cli.runtime, "stop_pid", lambda pid, timeout=5: stopped.append(pid) or True)
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: None)

    result = cli._repair_duplicate_service_processes()

    assert result["status"] == "repaired"
    assert stopped == [2222]
    assert result["stopped_pids"] == [2222]


def _stub_repair_service_restart(monkeypatch, *, live_ui_pid):
    """Wire _start_service_after_repair onto fakes and record what each side got."""

    calls = {"service_secret": [], "ui_secret": [], "stopped_ui": 0}
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: live_ui_pid)
    monkeypatch.setattr(
        cli.runtime,
        "start_service",
        lambda *, memory_ui_secret=None, **kwargs: calls["service_secret"].append(memory_ui_secret) or 4321,
    )
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": live_ui_pid})
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args, **kwargs: None)

    def _stop_ui(*args, **kwargs):
        calls["stopped_ui"] += 1

    monkeypatch.setattr(cli.runtime, "stop_ui", _stop_ui)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(
        cli,
        "_ensure_config",
        lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=5123)),
    )
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda host, port, *, memory_ui_secret=None, **kwargs: calls["ui_secret"].append(memory_ui_secret) or 8765,
    )
    return calls


def test_repair_restarts_surviving_ui_with_the_new_service_secret(monkeypatch):
    calls = _stub_repair_service_restart(monkeypatch, live_ui_pid=9999)

    result = cli._start_service_after_repair("duplicate-service-processes", "ok", "failed", stopped_pids=[2222])

    assert result["status"] == "repaired"
    # A bare CLI holds no process secret, so the replacement service must be
    # given a freshly minted one and the surviving UI restarted onto the same
    # value -- otherwise the pair verifies and signs with different secrets.
    assert calls["stopped_ui"] == 1
    assert calls["service_secret"] == calls["ui_secret"]
    assert calls["service_secret"][0]


def test_repair_leaves_the_ui_alone_when_none_is_running(monkeypatch):
    calls = _stub_repair_service_restart(monkeypatch, live_ui_pid=None)

    result = cli._start_service_after_repair("duplicate-service-processes", "ok", "failed", stopped_pids=[2222])

    assert result["status"] == "repaired"
    assert calls["stopped_ui"] == 0
    assert calls["ui_secret"] == []


def test_repair_still_reports_success_when_the_ui_restart_fails(monkeypatch):
    calls = _stub_repair_service_restart(monkeypatch, live_ui_pid=9999)
    monkeypatch.setattr(
        cli.runtime,
        "start_ui",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("port busy")),
    )

    result = cli._start_service_after_repair("duplicate-service-processes", "ok", "failed", stopped_pids=[2222])

    # The service repair itself succeeded; a UI that cannot be realigned is
    # logged, not escalated into a failed repair.
    assert result["status"] == "repaired"
    assert calls["service_secret"][0]


def test_repair_stale_install_runtime_stops_only_legacy_extra_process(monkeypatch):
    stopped = []
    refreshed = []
    paths.ensure_data_dirs()
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
    paths.ensure_data_dirs()
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
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: None)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: 3333)
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
    paths.ensure_data_dirs()
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
    monkeypatch.setattr(cli, "_live_ui_server_pid", lambda: None)
    monkeypatch.setattr(cli.runtime, "start_service", lambda **kwargs: 3333)
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
    paths.ensure_data_dirs()
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


def test_repair_doctor_targets_skips_empty_home_without_post_doctor(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_doctor", lambda: (_ for _ in ()).throw(AssertionError("skipped repair must not run doctor")))

    result = cli._repair_doctor_targets(["home-migration"], dry_run=False)

    assert result["ok"] is True
    assert result["results"][0]["status"] == "skipped"
    assert "doctor" not in result
    assert not (home / ".avibe").exists()
    assert not (home / ".vibe_remote").exists()


def test_repair_all_targets_skips_empty_home_without_initializing(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))

    def fail_probe(*args, **kwargs):
        raise AssertionError("empty-home repair must not probe runtime state")

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", fail_probe)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", fail_probe)
    monkeypatch.setattr(cli, "_current_cli_install_family", fail_probe)
    monkeypatch.setattr(cli, "_doctor", fail_probe)

    result = cli._repair_doctor_targets([], dry_run=False)

    assert result["ok"] is True
    assert {item["status"] for item in result["results"]} == {"skipped"}
    assert "doctor" not in result
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


def _restart_status_payload(**overrides) -> dict:
    """Return the status the restart supervisor writes, defaulted to a failure."""

    payload = {
        "ok": False,
        "job_id": "0d1f2e3a4b5c",
        "supervisor_pid": 4242,
        "supervisor_started_at": 1.0,
        "state": "failed",
        "trigger": "auto-update",
        "delay_seconds": 0.0,
        "scope": "all",
        "old_pid": 111,
        "new_pid": None,
        "log_path": "/tmp/vibe-restart-0d1f2e3a4b5c.log",
        "error": "start runtime failed: Config 'model_hub'\n  contains unknown fields",
        "created_at": "2026-08-11T04:50:31Z",
        "stage_durations": {"restart_total_seconds": 1.5},
    }
    payload.update(overrides)
    return payload


def _seed_restart_status(payload: dict, *, age_seconds: float = 0.0) -> Path:
    paths.ensure_data_dirs()
    restart_path = runtime.get_restart_status_path()
    runtime.write_json(restart_path, payload)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(restart_path, (stamp, stamp))
    return restart_path


def _stub_service_liveness(
    monkeypatch,
    *,
    owner_pid=None,
    starting_pid=None,
    extra_pids=(),
    lock_held=None,
    holder_finished_starting=True,
):
    """Describe a machine state, and let the real predicates read it.

    Stubbing ``verified_service_running`` itself would only assert which helper
    doctor calls. These are the sources underneath it and underneath the broader
    ``service_process_running``, so a test can name a machine state -- a lock
    owner, a pid that only reserved itself, a stray process -- and assert the
    verdict the real predicates produce for it.

    ``lock_held`` defaults to whether a lock owner was named, which is the usual
    case. Pass it True with no ``owner_pid`` for the state where the two come
    apart: the lock is held but its record answers no pid, either because the
    service has not written it yet or because it was corrupted.

    ``holder_finished_starting`` is the holder's own report, and it is a separate
    axis from the lock because the lock is taken before the database is migrated:
    a holder can occupy the instance for days without ever having finished
    starting. Describing a lock owner without it would describe only half a
    machine state.
    """

    reserved = owner_pid if starting_pid is None else starting_pid
    holds_lock = owner_pid is not None if lock_held is None else lock_held

    def resolve_owner(*, include_starting=False, **_kwargs):
        return reserved if include_starting else owner_pid

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", lambda *_a, **_kw: list(extra_pids))
    monkeypatch.setattr(cli.runtime, "service_instance_lock_available", lambda: (not holds_lock, owner_pid))
    monkeypatch.setattr(
        cli.runtime,
        "service_instance_started",
        lambda pid: holder_finished_starting and pid == owner_pid,
    )


@pytest.mark.parametrize(
    "age_seconds",
    [0.0, cli.DOCTOR_RESTART_RESULT_RETENTION_SECONDS + 60],
    ids=["fresh", "past-retention"],
)
def test_recorded_restart_failure_with_no_service_is_a_doctor_failure(monkeypatch, age_seconds):
    """A restart that failed and left nothing running is a failure at any age.

    Age is what used to decide the verdict, so both sides of the retention
    boundary are exercised against the same recorded failure: while the instance
    is down, neither side may call it healthy nor offer to delete the one record
    of why it is down.
    """

    _seed_restart_status(_restart_status_payload(), age_seconds=age_seconds)
    _stub_service_liveness(monkeypatch)

    items = cli._restart_state_items()

    assert [item["status"] for item in items] == ["fail"]
    item = items[0]
    assert item["code"] == "runtime.restart_failed"
    assert "model_hub" in item["message"]
    assert "\n" not in item["message"]
    assert "vibe-restart-0d1f2e3a4b5c.log" in item["message"]
    assert not item.get("repairable")
    assert "stale-restart-state" not in item.get("action", "")


def test_the_failure_action_only_tells_the_reader_to_run_real_commands(monkeypatch):
    """Every command the action names is one the reader can actually run.

    An action is actionable on its own or not at all. An earlier revision of this
    told the reader to "apply the extra-service-process repair listed in this
    report", which is a dependency on what else got rendered: that item is behind
    `--deep`, and the default run is exactly where a reader of this item lands, so
    the instruction resolved to nothing. Naming the command instead is the fix, and
    parsing it here is what keeps the name honest -- a renamed or misspelled repair
    target fails against the real parser rather than against a user who is already
    down. Extracted rather than asserted literally, so a command added to this text
    later is checked too.
    """

    _seed_restart_status(_restart_status_payload())
    _stub_service_liveness(monkeypatch)

    action = cli._restart_state_items()[0]["action"]
    commands = [text for text in re.findall(r"`([^`]+)`", action) if text.startswith("vibe ")]
    assert commands, f"the action names no runnable command: {action}"

    parser = cli.build_parser()
    for command in commands:
        parser.parse_args(shlex.split(command)[1:])

    assert "vibe doctor repair duplicate-service-processes" in commands

    # No command twice. `duplicate-service-processes` starts a clean service on
    # the no-owner path it is prescribed for, so a trailing "then start again"
    # earns `ServiceAlreadyRunningError` and reads as a recovery that failed --
    # which is why this asserts the shape rather than that one wording: any
    # repair that already does what a later step repeats fails here.
    assert len(commands) == len(set(commands)), f"the action asks for a command twice: {commands}"


@pytest.mark.parametrize(
    ("liveness", "expected"),
    [
        ({}, "fail"),
        ({"starting_pid": 5555}, "fail"),
        ({"extra_pids": (7777,)}, "fail"),
        ({"starting_pid": 5555, "extra_pids": (5555,)}, "fail"),
        ({"owner_pid": 4321, "holder_finished_starting": False}, "fail"),
        ({"lock_held": True}, "fail"),
        ({"owner_pid": 4321}, "warn"),
        ({"owner_pid": 4321, "extra_pids": (7777,)}, "warn"),
    ],
    ids=[
        "nothing-running",
        "pid-reserved-but-never-locked",
        "stray-process-holding-no-lock",
        "half-started-child-both-reserved-and-scanned",
        "lock-owner-still-starting",
        "lock-held-but-its-record-answers-no-pid",
        "lock-owner-that-finished-starting",
        "that-owner-beside-a-stray-process",
    ],
)
def test_a_restart_failure_is_downtime_until_a_started_service_says_otherwise(monkeypatch, liveness, expected):
    """One record against every liveness shape: only a service that finished starting ends downtime.

    The shapes that hold no lock are the wreckage a failed restart leaves: a pid
    that reserved itself and never acquired the lock, a stray process the scan can
    see, or one child that is both. Reading any of them as a running service would
    suppress the very failure that produced it.

    The two lock-holding failures are the ones this PR exists for. A holder is not
    a service by virtue of holding the lock, because the lock is taken before the
    database is migrated: the fifth shape is a process that took it and hung in a
    migration, which is #1567's eight-day outage, and the sixth is a holder whose
    record answers no pid, so nothing it did can be verified at all. Both occupy
    the instance without serving it, and calling either one recovery is how the
    outage stayed invisible behind a green health check.

    Only a holder that published its own start ends the failure -- with or without
    a stray process beside it, which is a separate item's problem.
    """

    _seed_restart_status(
        _restart_status_payload(),
        age_seconds=cli.DOCTOR_RESTART_RESULT_RETENTION_SECONDS + 60,
    )
    _stub_service_liveness(monkeypatch, **liveness)

    items = cli._restart_state_items()

    assert [item["status"] for item in items] == [expected]
    if expected == "warn":
        assert items[0]["repair"]["target"] == "stale-restart-state"


@pytest.mark.parametrize(
    "extra",
    [{}, {"service_alive": True}, {"service_alive": False}, {"whatever_a_future_writer_adds": True}],
    ids=["bare", "claims-a-service-survived", "claims-nothing-survived", "an-unknown-field"],
)
def test_only_live_facts_decide_downtime_not_what_the_record_claims(monkeypatch, extra):
    """No field in the record can talk doctor out of a verdict about right now.

    A record is written at failure time and read arbitrarily later, so anything it
    says about what was alive is a snapshot that may since have been falsified --
    by a child that took the lock after the supervisor gave up, or by any later
    start. The rule reads the record for the outcome and the machine for liveness,
    and nothing else. Seeded with the field a previous revision of this PR wrote,
    both ways round, so a record produced by any version reaches the same verdict.
    """

    _seed_restart_status(
        {**_restart_status_payload(), **extra},
        age_seconds=cli.DOCTOR_RESTART_RESULT_RETENTION_SECONDS + 60,
    )
    _stub_service_liveness(monkeypatch)

    assert [item["status"] for item in cli._restart_state_items()] == ["fail"]


def test_restart_state_items_classify_non_failures_without_probing_the_service(monkeypatch):
    """A record the supervisor did not fail keeps the classification it always had.

    The probes raise instead of returning a pid so this also pins the
    short-circuit: only a recorded failure may cost doctor a process probe.
    """

    def fail_probe(*args, **kwargs):
        raise AssertionError("classifying a non-failure must not probe the service")

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", fail_probe)
    monkeypatch.setattr(cli.runtime, "extra_service_process_pids", fail_probe)
    succeeded = _restart_status_payload(ok=True, state="succeeded", error=None, new_pid=222)

    _seed_restart_status(succeeded)
    assert [item["status"] for item in cli._restart_state_items()] == ["pass"]

    _seed_restart_status(succeeded, age_seconds=cli.DOCTOR_RESTART_RESULT_RETENTION_SECONDS + 60)
    stale = cli._restart_state_items()
    assert [item["status"] for item in stale] == ["warn"]
    assert stale[0]["repair"]["target"] == "stale-restart-state"


def test_doctor_repair_dry_run_does_not_probe_runtime(monkeypatch):
    def fail_runtime_probe(*args, **kwargs):
        raise AssertionError("dry-run must not touch runtime probes")

    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", fail_runtime_probe)

    result = cli._repair_doctor_targets(["duplicate-service-processes"], dry_run=True)

    assert result["ok"] is True
    assert result["results"][0]["status"] == "planned"


def test_memory_runtime_doctor_repair_dry_run_does_not_reach_controller(monkeypatch):
    monkeypatch.setattr(
        "vibe.internal_client.memory_install_runtime_sync",
        lambda: (_ for _ in ()).throw(
            AssertionError("dry-run must not reach the controller")
        ),
    )

    result = cli._repair_doctor_targets(["memory-runtime"], dry_run=True)

    assert result["ok"] is True
    assert result["results"] == [
        {
            "target": "memory-runtime",
            "status": "planned",
            "message": result["results"][0]["message"],
        }
    ]


def test_show_runtime_doctor_fast_mode_reports_local_state_without_network(monkeypatch):
    status = {
        "provider": "manifest-cache",
        "platform": "linux-x64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": {"runtime_version": "runtime-ref"},
        "archive": {
            "name": "vibe-show-runtime-node-linux-x64.tgz",
            "url": "https://github.com/avibe-bot/avibe/releases/download/v3.0.5/vibe-show-runtime-node-linux-x64.tgz",
        },
        "install": {"state": "absent", "install_dir": None},
    }
    manager = SimpleNamespace(
        status=lambda: status,
        probe_archive_reachability=lambda: (_ for _ in ()).throw(AssertionError("fast Doctor must not probe network")),
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=False)

    assert next(item for item in items if item.get("code") == "show_runtime.not_ready")["repair"]["target"] == "show-runtime"
    assert next(item for item in items if item.get("code") == "show_runtime.archive_probe_skipped")["status"] == "pass"


@pytest.mark.parametrize("language", ["en", "zh"])
def test_show_runtime_doctor_retires_legacy_source_with_actionable_replacement(
    monkeypatch,
    language,
):
    manager = SimpleNamespace(
        status=lambda: {
            "provider": "github",
            "platform": "linux-x64",
            "explicit_command": None,
            "node_available": True,
            "node_supported": True,
            "install": {"state": "absent", "install_dir": None},
        },
        archive_cache_status=lambda: {"candidate_count": 0, "candidate_bytes": 0, "skipped_reason": None},
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)

    items = cli._show_runtime_doctor_items(deep=False)

    failure = next(item for item in items if item.get("code") == "show_runtime.provider_unsupported")
    assert failure["status"] == "fail"
    assert failure["action"] == cli.i18n_t("runtime.doctor.providerUnsupportedAction", language)


def test_show_runtime_doctor_reports_skipped_archive_inspection_as_warn(monkeypatch):
    status = {
        "provider": "manifest-cache",
        "platform": "linux-x64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": {"runtime_version": "runtime-ref"},
        "archive": {
            "name": "vibe-show-runtime-node-linux-x64.tgz",
            "url": "https://github.com/avibe-bot/avibe/releases/download/v3.0.5/vibe-show-runtime-node-linux-x64.tgz",
        },
        "installed": True,
    }
    manager = SimpleNamespace(
        status=lambda: status,
        probe_archive_reachability=lambda: (_ for _ in ()).throw(AssertionError("fast Doctor must not probe network")),
        archive_cache_status=lambda: {
            "candidate_count": 0,
            "candidate_bytes": 0,
            "skipped_reason": "runtime_install_already_running",
        },
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=False)

    skipped = next(item for item in items if item.get("code") == "show_runtime.archive_cache_skipped")
    assert skipped["status"] == "warn"
    clean = [item for item in items if item.get("code") == "show_runtime.archive_cache_clean"]
    assert not clean  # an uninspected cache must not be reported as clean


def test_managed_dependencies_doctor_uses_one_status_contract(monkeypatch):
    offline_calls: list[bool] = []

    def status(*, offline=False):
        offline_calls.append(offline)
        return {
            "deps": [
                {"id": "askill", "required": True, "installed": False, "status": "missing"},
                {"id": "avault", "required": True, "installed": True, "status": "ready", "version": "0.1.6"},
                {
                    "id": "model-hub-engine",
                    "required": True,
                    "installed": False,
                    "status": "upgrade_required",
                    "version": "v7.2.105",
                },
                {"id": "show-runtime", "required": True, "installed": False, "status": "missing"},
                {"id": "tmux", "required": False, "installed": False, "status": "missing"},
                {"id": "git-runtime", "required": False, "installed": True, "status": "ready"},
                {"id": "node", "required": True, "installed": False, "status": "missing"},
            ]
        }

    monkeypatch.setattr(cli.api, "dependencies_status", status)
    monkeypatch.setattr(cli.api, "askill_auto_install_supported", lambda: True)

    items = cli._managed_dependencies_doctor_items(deep=False)

    assert offline_calls == [True]
    assert next(item for item in items if item.get("code") == "dependencies.askill.not_ready")["repair"]["target"] == "askill"
    assert next(item for item in items if item.get("code") == "dependencies.avault.ready")["status"] == "pass"
    assert next(
        item for item in items if item.get("code") == "dependencies.model-hub-engine.not_ready"
    )["repair"]["target"] == "model-hub-engine"
    assert next(item for item in items if item.get("code") == "dependencies.git-runtime.ready")["status"] == "pass"
    assert next(item for item in items if item.get("code") == "dependencies.tmux.not_ready")["status"] == "warn"
    assert next(item for item in items if item.get("code") == "dependencies.node.not_ready")["status"] == "fail"


@pytest.mark.parametrize(
    (
        "dependency_status",
        "installed",
        "required",
        "reason",
        "expected_severity",
        "expected_repair",
    ),
    [
        pytest.param("ready", True, False, None, "pass", False, id="ready"),
        pytest.param(
            "missing",
            False,
            False,
            "memory_runtime_missing",
            "warn",
            True,
            id="optional-missing",
        ),
        pytest.param(
            "error",
            False,
            False,
            "memory_runtime_install_failed",
            "warn",
            True,
            id="optional-error",
        ),
        pytest.param(
            "error",
            False,
            True,
            "memory_runtime_install_failed",
            "fail",
            True,
            id="required-error",
        ),
        pytest.param(
            "unsupported",
            False,
            True,
            "memory_runtime_unsupported",
            "fail",
            False,
            id="required-unsupported",
        ),
    ],
)
def test_managed_dependencies_doctor_reports_memory_runtime_states(
    monkeypatch,
    dependency_status,
    installed,
    required,
    reason,
    expected_severity,
    expected_repair,
):
    """MEMORY-RUNTIME-001: Doctor projects the disk-truthful runtime contract."""

    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {
                    "id": "memory-runtime",
                    "required": required,
                    "installed": installed,
                    "status": dependency_status,
                    "reason": reason,
                },
                {
                    "id": "git-runtime",
                    "required": False,
                    "installed": True,
                    "status": "ready",
                },
            ]
        },
    )

    items = cli._managed_dependencies_doctor_items(deep=True)

    runtime_item = next(
        item
        for item in items
        if item.get("code") == f"dependencies.memory-runtime.{dependency_status}"
    )
    assert runtime_item["status"] == expected_severity
    assert runtime_item["dependency_status"] == dependency_status
    assert runtime_item.get("dependency_reason") == reason
    assert runtime_item["dependency_required"] is required
    assert (runtime_item.get("repair") or {}).get("target") == (
        "memory-runtime" if expected_repair else None
    )


def test_managed_dependencies_doctor_probes_model_hub_engine_archive(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {
                    "id": "model-hub-engine",
                    "required": True,
                    "installed": False,
                    "status": "missing",
                },
                {
                    "id": "git-runtime",
                    "required": False,
                    "installed": True,
                    "status": "ready",
                },
            ]
        },
    )
    probes = []
    monkeypatch.setattr(
        "vibe.model_hub_runtime.installer.EngineRuntimeManager",
        lambda: SimpleNamespace(
            probe_archive_reachability=lambda: probes.append(True)
            or {"ok": True, "checked": True}
        ),
    )

    items = cli._managed_dependencies_doctor_items(deep=True)

    missing = next(
        item
        for item in items
        if item.get("code") == "dependencies.model-hub-engine.not_ready"
    )
    assert missing["repair"]["target"] == "model-hub-engine"
    assert probes == [True]


@pytest.mark.parametrize(
    ("language", "expected_label"),
    [("en", "Model Hub engine (CPA)"), ("zh", "Model Hub 引擎（CPA）")],
)
@pytest.mark.parametrize(
    ("status", "installed", "code"),
    [("ready", True, "ready"), ("missing", False, "not_ready")],
)
def test_managed_dependencies_doctor_localizes_model_hub_engine_label(
    monkeypatch,
    language,
    expected_label,
    status,
    installed,
    code,
):
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {
                    "id": "model-hub-engine",
                    "required": True,
                    "installed": installed,
                    "status": status,
                },
                {
                    "id": "git-runtime",
                    "required": False,
                    "installed": True,
                    "status": "ready",
                },
            ]
        },
    )

    items = cli._managed_dependencies_doctor_items()

    item = next(
        item
        for item in items
        if item.get("code") == f"dependencies.model-hub-engine.{code}"
    )
    assert expected_label in item["message"]
    if language == "zh":
        assert "Model Hub engine (CPA)" not in item["message"]


def test_managed_dependencies_doctor_suppresses_unsupported_askill_repair(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {"id": "askill", "required": True, "installed": False, "status": "missing"},
                {"id": "git-runtime", "required": False, "installed": True, "status": "ready"},
            ]
        },
    )
    monkeypatch.setattr(cli.api, "askill_auto_install_supported", lambda: False)

    items = cli._managed_dependencies_doctor_items(deep=False)

    missing = next(item for item in items if item.get("code") == "dependencies.askill.not_ready")
    assert "repair" not in missing
    assert "askill.sh" in missing["action"]


def test_managed_dependencies_doctor_does_not_offer_unsupported_platform_repair(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {
                    "id": "tmux",
                    "required": False,
                    "installed": False,
                    "status": "missing",
                    "reason": "tmux_platform_unsupported",
                },
                {"id": "git-runtime", "required": False, "installed": True, "status": "ready"},
            ]
        },
    )

    items = cli._managed_dependencies_doctor_items(deep=False)

    unsupported = next(
        item for item in items if item.get("code") == "dependencies.tmux.platform_unsupported"
    )
    assert unsupported["status"] == "warn"
    assert "repair" not in unsupported
    assert not any(item.get("code") == "dependencies.tmux.not_ready" for item in items)


def test_managed_dependencies_doctor_treats_unsupported_cpa_as_nonfatal(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {
                    "id": "model-hub-engine",
                    "required": False,
                    "installed": False,
                    "status": "unsupported",
                    "reason": "model_hub_engine_platform_unsupported",
                },
                {
                    "id": "git-runtime",
                    "required": False,
                    "installed": True,
                    "status": "ready",
                },
            ]
        },
    )

    items = cli._managed_dependencies_doctor_items(deep=False)

    unsupported = next(
        item
        for item in items
        if item.get("code") == "dependencies.model-hub-engine.platform_unsupported"
    )
    assert unsupported["status"] == "warn"
    assert "repair" not in unsupported
    assert not any(
        item.get("code") == "dependencies.model-hub-engine.not_ready"
        for item in items
    )


def test_managed_dependencies_doctor_accepts_usable_system_git(monkeypatch):
    monkeypatch.setattr(cli.api, "dependencies_status", lambda **_kwargs: {"deps": []})
    monkeypatch.setattr(
        "core.git_runtime.git_runtime_status",
        lambda: {
            "resolution": "system",
            "path": "/usr/bin/git",
            "version": None,
            "managed": {
                "installed": False,
                "status": "missing",
                "reason": "git_platform_unsupported",
            },
        },
    )

    items = cli._managed_dependencies_doctor_items(deep=False)

    system_git = next(
        item for item in items if item.get("code") == "dependencies.git-runtime.system_ready"
    )
    assert system_git["status"] == "pass"
    assert not any(item.get("code") == "dependencies.git-runtime.not_ready" for item in items)


def test_managed_dependencies_doctor_does_not_offer_unsupported_git_runtime_repair(monkeypatch):
    monkeypatch.setattr(cli.api, "dependencies_status", lambda **_kwargs: {"deps": []})
    monkeypatch.setattr(
        "core.git_runtime.git_runtime_status",
        lambda: {
            "resolution": "none",
            "path": None,
            "version": None,
            "managed": {
                "installed": False,
                "status": "missing",
                "reason": "git_platform_unsupported",
            },
        },
    )

    items = cli._managed_dependencies_doctor_items(deep=False)

    unsupported = next(
        item
        for item in items
        if item.get("code") == "dependencies.git-runtime.platform_unsupported"
    )
    assert unsupported["status"] == "warn"
    assert "repair" not in unsupported


def test_managed_dependencies_doctor_rejects_unsupported_archive_url_without_repair(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {"id": "git-runtime", "required": False, "installed": False, "status": "missing"},
            ]
        },
    )
    manager = SimpleNamespace(
        probe_archive_reachability=lambda: {
            "ok": False,
            "checked": False,
            "reason": "git_archive_url_unsupported",
            "url": "http://example.test/git.tar.gz",
        }
    )
    monkeypatch.setattr("core.git_runtime.GitRuntimeManager", lambda **_kwargs: manager)

    items = cli._managed_dependencies_doctor_items(deep=True)

    unsupported = next(
        item
        for item in items
        if item.get("code") == "dependencies.git-runtime.archive_url_unsupported"
    )
    assert unsupported["status"] == "warn"
    assert "repair" not in unsupported
    assert not any(item.get("code") == "dependencies.git-runtime.not_ready" for item in items)


def test_managed_dependencies_doctor_deep_reports_retry_exhaustion(monkeypatch):
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {"id": "askill", "required": True, "installed": False, "status": "missing"},
                {"id": "git-runtime", "required": False, "installed": True, "status": "ready"},
            ]
        },
    )
    monkeypatch.setattr(
        "core.dependency_network.probe_url",
        lambda *_args, **_kwargs: {
            "ok": False,
            "checked": True,
            "download_error": {
                "kind": "timeout",
                "url": "https://askill.sh",
                "attempts": 2,
                "retryable": True,
            },
        },
    )

    items = cli._managed_dependencies_doctor_items(deep=True)

    failure = next(item for item in items if item.get("code") == "dependencies.askill.download_timeout_failed")
    assert "after 2 attempts" in failure["message"]


def test_repair_managed_dependency_preserves_structured_download_error():
    error = {"kind": "dns", "attempts": 3, "retryable": True}

    result = cli._repair_managed_dependency(
        "avault",
        lambda force: {
            "ok": False,
            "reason": "avault_download_failed",
            "message": "download failed",
            "download_error": error,
        },
    )

    assert result["status"] == "failed"
    assert result["download_error"] == error


def test_memory_runtime_doctor_repair_uses_controller_ipc(monkeypatch):
    calls = []

    def install_runtime():
        calls.append(True)
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(
        "vibe.internal_client.memory_install_runtime_sync",
        install_runtime,
    )

    result = cli._repair_memory_runtime()

    assert calls == [True]
    assert result["target"] == "memory-runtime"
    assert result["status"] == "repaired"


def test_memory_runtime_doctor_repair_preserves_controller_failure(monkeypatch):
    download_error = {"kind": "timeout", "attempts": 2}
    monkeypatch.setattr(
        "vibe.internal_client.memory_install_runtime_sync",
        lambda: {
            "status_code": 200,
            "body": {
                "ok": False,
                "reason": "memory_runtime_install_failed",
                "download_error": download_error,
            },
        },
    )

    result = cli._repair_memory_runtime()

    assert result["status"] == "failed"
    assert result["reason"] == "memory_runtime_install_failed"
    assert result["download_error"] == download_error


@pytest.mark.parametrize(
    "reason",
    [
        "memory_runtime_preparation_import_timeout",
        "memory_runtime_preparation_import_failed",
        "memory_runtime_preparation_scrubber_timeout",
        "memory_runtime_preparation_scrubber_failed",
        "memory_runtime_preparation_sync_contract_failed",
        "memory_runtime_preparation_failed",
    ],
)
@pytest.mark.parametrize("language", ["en", "zh"])
def test_doctor_localizes_bounded_memory_runtime_preparation_reasons(reason, language):
    projected = cli._doctor_memory_reason(reason, language)

    assert projected != reason
    assert projected != "unknown error"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            "Memory is running. Turn it off in Settings > Memory before repairing the runtime.",
        ),
        (
            "zh",
            "记忆功能正在运行。请先在「设置 > 记忆」中关闭记忆，再修复运行时。",
        ),
    ],
)
def test_doctor_localizes_stopped_memory_repair_prerequisite(language, expected):
    projected = cli._doctor_memory_reason(
        "memory_runtime_install_requires_stopped_memory",
        language,
    )

    assert projected == expected


def test_show_runtime_doctor_deep_mode_distinguishes_missing_release_asset(monkeypatch):
    archive_url = (
        "https://github.com/avibe-bot/avibe/releases/download/"
        "v3.0.5/vibe-show-runtime-node-linux-x64.tgz"
    )
    status = {
        "provider": "manifest-cache",
        "platform": "linux-x64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": {"runtime_version": "runtime-ref"},
        "archive": {"name": "vibe-show-runtime-node-linux-x64.tgz", "url": archive_url},
        "installed": False,
    }
    manager = SimpleNamespace(
        status=lambda: status,
        probe_archive_reachability=lambda: {
            "ok": False,
            "checked": True,
            "reason": "runtime_archive_download_failed",
            "download_error": {
                "kind": "http",
                "message": "HTTP 404 Not Found",
                "url": archive_url,
                "host": "github.com",
                "http_status": 404,
            },
        },
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=True)

    failure = next(item for item in items if item.get("code") == "show_runtime.archive_http_404")
    assert failure["status"] == "fail"
    assert "proxy or security gateway" in failure["action"]


def test_show_runtime_doctor_reports_unchecked_manifest_download_failure(monkeypatch):
    archive_url = "https://example.test/runtime.tgz"
    status = {
        "provider": "manifest-cache",
        "platform": "linux-x64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": {"runtime_version": "runtime-ref"},
        "archive": {"name": "runtime.tgz", "url": archive_url},
        "installed": False,
    }
    manager = SimpleNamespace(
        status=lambda: status,
        probe_archive_reachability=lambda: {
            "ok": False,
            "checked": False,
            "reason": "runtime_manifest_download_failed",
            "download_error": {
                "kind": "timeout",
                "message": "Connection timed out",
                "url": "https://example.test/manifest.json",
                "attempts": 2,
            },
        },
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=True)

    failure = next(item for item in items if item.get("code") == "show_runtime.manifest_timeout_failed")
    assert failure["status"] == "fail"
    assert "after 2 attempts" in failure["message"]
    assert not any(item.get("code") == "show_runtime.archive_probe_unsupported" for item in items)


def test_show_runtime_doctor_reports_unsupported_archive_url(monkeypatch):
    archive_url = "http://example.test/runtime.tgz"
    status = {
        "provider": "manifest-cache",
        "platform": "linux-x64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": {"runtime_version": "runtime-ref"},
        "archive": {"name": "runtime.tgz", "url": archive_url},
        "installed": False,
    }
    manager = SimpleNamespace(
        status=lambda: status,
        probe_archive_reachability=lambda: {
            "ok": False,
            "checked": False,
            "reason": "runtime_archive_url_unsupported",
            "url": archive_url,
        },
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=True)

    failure = next(item for item in items if item.get("code") == "show_runtime.archive_url_unsupported")
    assert failure["status"] == "fail"
    assert "HTTPS or file URL" in failure["action"]
    not_ready = next(item for item in items if item.get("code") == "show_runtime.not_ready")
    assert "repair" not in not_ready
    assert "doctor repair" not in not_ready["action"]


def test_show_runtime_doctor_only_treats_explicit_head_failure_as_probe_unsupported(monkeypatch):
    archive_url = "https://example.test/runtime.tgz"
    status = {
        "provider": "manifest-cache",
        "platform": "linux-x64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": {"runtime_version": "runtime-ref"},
        "archive": {"name": "runtime.tgz", "url": archive_url},
        "installed": False,
    }
    manager = SimpleNamespace(
        status=lambda: status,
        probe_archive_reachability=lambda: {
            "ok": False,
            "checked": False,
            "reason": "runtime_archive_probe_unsupported",
            "download_error": {
                "kind": "http",
                "message": "HTTP 405 Method Not Allowed",
                "url": archive_url,
                "http_status": 405,
            },
        },
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=True)

    warning = next(item for item in items if item.get("code") == "show_runtime.archive_probe_unsupported")
    assert warning["status"] == "warn"
    assert not any(item.get("code") == "show_runtime.archive_http_error" for item in items)


def test_show_runtime_doctor_identifies_legacy_upstream_fallback(monkeypatch):
    status = {
        "provider": "archive",
        "platform": "darwin-arm64",
        "explicit_command": None,
        "node_available": True,
        "node_version": "22.14.0",
        "node_supported": True,
        "manifest": None,
        "archive": {
            "name": "vibe-show-runtime-node-darwin-arm64.tgz",
            "url": "https://github.com/avibe-bot/vibe-show-runtime/releases/latest/download/"
            "vibe-show-runtime-node-darwin-arm64.tgz",
        },
        "installed": False,
    }
    manager = SimpleNamespace(status=lambda: status)
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **_kwargs: manager)

    items = cli._show_runtime_doctor_items(deep=False)

    failure = next(item for item in items if item.get("code") == "show_runtime.legacy_archive_provider")
    assert failure["status"] == "fail"
    assert "does not publish release assets" in failure["action"]
    assert "repair" not in next(item for item in items if item.get("code") == "show_runtime.not_ready")


@pytest.mark.parametrize(
    ("owner_result", "expected_status", "message_fragment"),
    [
        (
            {
                "ok": True,
                "outcome": "healthy",
                "provider": "manifest-cache",
                "platform": "linux-x64",
                "install_dir": "/runtime/old",
            },
            "skipped",
            "no repair is needed",
        ),
        (
            {
                "ok": True,
                "outcome": "repaired",
                "was_installed": True,
                "provider": "manifest-cache",
                "platform": "linux-x64",
                "install_dir": "/runtime/new",
            },
            "repaired",
            "Reinstalled and started",
        ),
        (
            {
                "ok": False,
                "outcome": "failed",
                "reason": "runtime_start_verification_failed",
                "verification": {"state": "undetermined", "detail": "probe failed"},
                "verification_phase": "before",
                "installed": True,
            },
            "failed",
            "no reinstall was attempted",
        ),
        (
            {
                "ok": False,
                "outcome": "failed",
                "reason": "runtime_start_health_timeout",
                "verification": {
                    "state": "not_startable",
                    "reason": "runtime_start_health_timeout",
                },
                "verification_phase": "after",
                "was_installed": True,
                "installed": True,
            },
            "failed",
            "reinstalled but could not start",
        ),
    ],
)
def test_repair_show_runtime_renders_manager_owned_outcomes(
    monkeypatch,
    owner_result,
    expected_status,
    message_fragment,
):
    manager = SimpleNamespace(repair=lambda: owner_result)
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda: manager)

    result = cli._repair_show_runtime()

    assert result["status"] == expected_status
    assert message_fragment in result["message"]


def test_repair_show_runtime_returns_structured_download_failure(monkeypatch):
    archive_url = "https://github.com/avibe-bot/avibe/releases/download/v-test/runtime.tgz"
    manager = SimpleNamespace(
        repair=lambda: {
            "ok": False,
            "outcome": "failed",
            "reason": "runtime_archive_download_failed",
            "provider": "manifest-cache",
            "platform": "linux-x64",
            "download_error": {
                "kind": "timeout",
                "message": "Connection timed out",
                "url": archive_url,
            },
        }
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda: manager)

    result = cli._repair_show_runtime()

    assert result["status"] == "failed"
    assert result["reason"] == "runtime_archive_download_failed"
    assert result["download_error"]["kind"] == "timeout"
    assert archive_url in result["message"]


@pytest.mark.parametrize(("language", "message_fragment"), [("en", "Fix or remove"), ("zh", "\u4fee\u590d\u6216\u79fb\u9664")])
def test_repair_show_runtime_renders_explicit_command_failure(
    monkeypatch,
    language,
    message_fragment,
):
    manager = SimpleNamespace(
        repair=lambda: {
            "ok": False,
            "outcome": "failed",
            "reason": "runtime_start_health_timeout",
            "explicit_command": "/custom/runtime",
            "installed": True,
        }
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda: manager)
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)

    result = cli._repair_show_runtime()

    assert result["status"] == "failed"
    assert result["explicit_command"] == "/custom/runtime"
    assert message_fragment in result["message"]


def test_runtime_prepare_strict_does_not_report_policy_skip_as_ready(monkeypatch, capsys):
    manager = SimpleNamespace(
        prepare=lambda **_kwargs: {
            "policy": {
                "state": "skipped",
                "reason": "VIBE_INSTALL_SKIP_SHOW_RUNTIME",
            },
            "install": {"state": "absent", "reason": None},
            "runtime": {"state": "unchecked", "reason": None},
            "status": {},
        }
    )
    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: manager)
    monkeypatch.setattr(cli, "_ensure_askill_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_tmux_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_git_during_prepare", lambda **_kwargs: {"ok": True, "mode": "system"})
    monkeypatch.setattr(cli, "_ensure_avault_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_model_hub_engine_during_prepare", lambda **_kwargs: {"ok": True})

    exit_code = cli.cmd_runtime(
        SimpleNamespace(
            runtime_command="prepare",
            offline=False,
            force=False,
            json=False,
            strict=True,
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Show Runtime ready" not in captured.out
    assert "VIBE_INSTALL_SKIP_SHOW_RUNTIME" in captured.err


def test_runtime_prepare_reports_cpa_failure_without_blocking_avibe_upgrade(
    monkeypatch,
    capsys,
):
    manager = SimpleNamespace(
        prepare=lambda **_kwargs: {
            "ok": True,
            "policy": {"state": "allowed", "reason": None},
            "install": {"state": "installed", "reason": None},
            "runtime": {"state": "unchecked", "reason": None},
            "status": {
                "install": {
                    "state": "installed",
                    "install_dir": "/runtime/show",
                }
            },
        }
    )
    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: manager)
    monkeypatch.setattr(cli, "_ensure_askill_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_tmux_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_git_during_prepare", lambda **_kwargs: {"ok": True, "mode": "system"})
    monkeypatch.setattr(cli, "_ensure_avault_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        cli,
        "_ensure_model_hub_engine_during_prepare",
        lambda **_kwargs: {
            "ok": False,
            "reason": "model_hub_engine_archive_download_failed",
        },
    )

    exit_code = cli.cmd_runtime(
        SimpleNamespace(
            runtime_command="prepare",
            offline=False,
            force=False,
            json=True,
            strict=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["model_hub_engine"] == {
        "ok": False,
        "reason": "model_hub_engine_archive_download_failed",
    }


@pytest.mark.parametrize(
    ("cpa_result", "expected", "stream"),
    (
        ({"ok": True, "changed": True}, "Model Hub 引擎已安装。", "out"),
        ({"ok": True, "changed": False}, "Model Hub 引擎已就绪。", "out"),
        (
            {"ok": False, "skipped": True, "reason": "offline"},
            "Model Hub 引擎：已跳过（offline）。",
            "out",
        ),
        (
            {"ok": False, "reason": "download_failed"},
            "Model Hub 引擎尚未就绪：download_failed",
            "err",
        ),
    ),
)
def test_runtime_prepare_localizes_cpa_output(
    monkeypatch,
    capsys,
    cpa_result,
    expected,
    stream,
):
    manager = SimpleNamespace(
        prepare=lambda **_kwargs: {
            "ok": True,
            "policy": {"state": "allowed", "reason": None},
            "install": {"state": "installed", "reason": None},
            "runtime": {"state": "unchecked", "reason": None},
            "status": {"install": {"state": "installed", "install_dir": None}},
        }
    )
    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: manager)
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")
    monkeypatch.setattr(cli, "_ensure_askill_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_tmux_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_git_during_prepare", lambda **_kwargs: {"ok": True, "mode": "system"})
    monkeypatch.setattr(cli, "_ensure_avault_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        cli,
        "_ensure_model_hub_engine_during_prepare",
        lambda **_kwargs: cpa_result,
    )

    assert (
        cli.cmd_runtime(
            SimpleNamespace(
                runtime_command="prepare",
                offline=False,
                force=False,
                json=False,
                strict=False,
            )
        )
        == 0
    )

    captured = capsys.readouterr()
    assert expected in getattr(captured, stream)
    assert "Model Hub engine" not in captured.out + captured.err


def test_runtime_prepare_force_does_not_report_explicit_command_as_replaced(monkeypatch, capsys):
    manager = SimpleNamespace(
        prepare=lambda **_kwargs: {
            "ok": False,
            "reason": "VIBE_SHOW_RUNTIME_BIN",
            "policy": {
                "state": "skipped",
                "reason": "VIBE_SHOW_RUNTIME_BIN",
            },
            "install": {"state": "installed", "reason": None},
            "runtime": {"state": "unchecked", "reason": None},
            "status": {"install": {"state": "installed", "install_dir": None}},
        }
    )
    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: manager)
    monkeypatch.setattr(cli, "_ensure_askill_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_tmux_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_git_during_prepare", lambda **_kwargs: {"ok": True, "mode": "system"})
    monkeypatch.setattr(cli, "_ensure_avault_during_prepare", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "_ensure_model_hub_engine_during_prepare", lambda **_kwargs: {"ok": True})

    exit_code = cli.cmd_runtime(
        SimpleNamespace(
            runtime_command="prepare",
            offline=False,
            force=True,
            json=False,
            strict=True,
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Show Runtime ready" not in captured.out
    assert "VIBE_SHOW_RUNTIME_BIN" in captured.err


@pytest.mark.parametrize("consumer", ["memory", "model-hub", "tmux"])
def test_runtime_clean_reclaims_each_shared_consumer_in_preview_and_real_run(
    monkeypatch,
    capsys,
    tmp_path,
    consumer,
):
    if consumer == "memory":
        from avibe_memory.artifact import MemoryArtifactManager

        manager = MemoryArtifactManager(
            runtime_dir=tmp_path / "memory-runtime",
            provider_root=tmp_path / "memory-provider",
            offline=True,
        )
    elif consumer == "model-hub":
        from vibe.model_hub_runtime.installer import EngineRuntimeManager

        manager = EngineRuntimeManager(
            runtime_dir=tmp_path / "model-hub-runtime",
            offline=True,
        )
    else:
        from core.tmux_runtime import TmuxRuntimeManager

        manager = TmuxRuntimeManager(
            runtime_dir=tmp_path / "tmux-runtime",
            offline=True,
        )

    from core.managed_runtime import runtime_platform_tag

    versions_dir = manager.runtime_dir / "versions"
    current_install = versions_dir / "current"
    stale_install = versions_dir / "stale"
    current_install.mkdir(parents=True)
    stale_install.mkdir()
    manifest_sha = "a" * 64
    current_archive_sha = "b" * 64
    stale_archive_sha = "c" * 64
    binary_sha = hashlib.sha256(b"fixture").hexdigest()
    for install_dir, version, archive_sha in (
        (current_install, "current", current_archive_sha),
        (stale_install, "stale", stale_archive_sha),
    ):
        binary = install_dir / manager.spec.default_bin_path
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("fixture", encoding="utf-8")
        binary.chmod(0o755)
        (install_dir / manager.spec.metadata_filename).write_text(
            json.dumps(
                {
                    "provider": "manifest",
                    "runtime_id": manager.spec.runtime_id,
                    "runtime_version": version,
                    "platform": runtime_platform_tag(),
                    "manifest_sha256": manifest_sha,
                    "manifest_source": "package:tests/runtime-manifest.json",
                    "archive_name": f"fixture-{version}.tar.gz",
                    "archive_sha256": archive_sha,
                    "binary_sha256": binary_sha,
                    "bin_path": manager.spec.default_bin_path,
                }
            ),
            encoding="utf-8",
        )
    (manager.runtime_dir / "current.json").write_text(
        json.dumps(
            {
                "provider": "manifest",
                "runtime_id": manager.spec.runtime_id,
                "runtime_version": "current",
                "platform": runtime_platform_tag(),
                "install_dir": str(current_install),
                "manifest_sha256": manifest_sha,
                "archive_sha256": current_archive_sha,
                "bin_path": manager.spec.default_bin_path,
            }
        ),
        encoding="utf-8",
    )

    class FakeShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {
                "ok": True,
                "removed": [],
                "archives": {"removed_count": 0, "candidate_count": 0},
            }

    def clean_consumer(*, keep_previous, dry_run):
        assert keep_previous == 0
        return manager.clean(keep_previous=keep_previous, dry_run=dry_run)

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: FakeShowRuntimeManager())
    monkeypatch.setattr(
        cli,
        "_managed_runtime_cleaners",
        lambda: ((manager.spec.runtime_id, clean_consumer),),
    )
    parser = cli.build_parser()

    preview_args = parser.parse_args(
        ["runtime", "clean", "--keep-previous", "0", "--dry-run", "--json"]
    )
    assert cli.cmd_runtime(preview_args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert str(stale_install) in preview[manager.spec.runtime_id]["removed"]
    assert current_install.is_dir()
    assert stale_install.is_dir()

    real_args = parser.parse_args(
        ["runtime", "clean", "--keep-previous", "0", "--json"]
    )
    assert cli.cmd_runtime(real_args) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert str(stale_install) in cleaned[manager.spec.runtime_id]["removed"]
    assert current_install.is_dir()
    assert not stale_install.exists()


def test_runtime_clean_registry_invokes_every_current_shared_consumer(monkeypatch):
    from core import tmux_runtime
    from avibe_memory import artifact as memory_artifact
    from vibe.model_hub_runtime import installer as model_hub_installer

    calls = []

    def clean_git(*, keep_previous, dry_run):
        calls.append(("git", keep_previous, dry_run))
        return {"ok": True, "removed": []}

    class FakeManager:
        def __init__(self, runtime_id):
            self.runtime_id = runtime_id

        def clean(self, *, keep_previous, dry_run):
            calls.append((self.runtime_id, keep_previous, dry_run))
            return {"ok": True, "removed": []}

    monkeypatch.setattr(cli, "_clean_git_runtime", clean_git)
    monkeypatch.setattr(
        memory_artifact,
        "get_memory_artifact_manager",
        lambda: FakeManager("memory-runtime"),
    )
    monkeypatch.setattr(
        model_hub_installer,
        "EngineRuntimeManager",
        lambda: FakeManager("model_hub_engine"),
    )
    monkeypatch.setattr(
        tmux_runtime,
        "get_tmux_runtime_manager",
        lambda: FakeManager("tmux"),
    )

    results = cli._clean_managed_runtime_consumers(keep_previous=2, dry_run=True)

    assert list(results) == [
        "git",
        "memory-runtime",
        "model_hub_engine",
        "tmux",
    ]
    assert calls == [
        ("git", 2, True),
        ("memory-runtime", 2, True),
        ("model_hub_engine", 2, True),
        ("tmux", 2, True),
    ]


def test_runtime_clean_isolates_missing_memory_implementation(monkeypatch):
    from core import tmux_runtime
    from vibe.model_hub_runtime import installer as model_hub_installer

    calls = []
    original_import = builtins.__import__

    def core_only_import(name, *args, **kwargs):
        if name == "avibe_memory.artifact":
            raise ModuleNotFoundError(name, name="avibe_memory")
        return original_import(name, *args, **kwargs)

    class FakeManager:
        def __init__(self, runtime_id):
            self.runtime_id = runtime_id

        def clean(self, *, keep_previous, dry_run):
            calls.append((self.runtime_id, keep_previous, dry_run))
            return {"ok": True, "removed": []}

    monkeypatch.setattr(builtins, "__import__", core_only_import)
    monkeypatch.setattr(
        cli,
        "_clean_git_runtime",
        lambda **kwargs: calls.append(("git", kwargs["keep_previous"], kwargs["dry_run"]))
        or {"ok": True, "removed": []},
    )
    monkeypatch.setattr(
        model_hub_installer,
        "EngineRuntimeManager",
        lambda: FakeManager("model_hub_engine"),
    )
    monkeypatch.setattr(
        tmux_runtime,
        "get_tmux_runtime_manager",
        lambda: FakeManager("tmux"),
    )

    results = cli._clean_managed_runtime_consumers(keep_previous=2, dry_run=True)

    assert results["memory-runtime"] == {
        "ok": True,
        "removed": [],
        "skipped": True,
        "reason": "memory_implementation_unavailable",
    }
    assert results["git"]["ok"] is True
    assert results["model_hub_engine"]["ok"] is True
    assert results["tmux"]["ok"] is True
    assert calls == [
        ("git", 2, True),
        ("model_hub_engine", 2, True),
        ("tmux", 2, True),
    ]


def test_runtime_clean_reports_broken_memory_implementation_import(monkeypatch):
    original_import = builtins.__import__

    def broken_memory_import(name, *args, **kwargs):
        if name == "avibe_memory.artifact":
            raise ModuleNotFoundError("missing companion dependency", name="memory_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_memory_import)
    cleaners = dict(cli._managed_runtime_cleaners())

    with pytest.raises(ModuleNotFoundError, match="missing companion dependency"):
        cleaners["memory-runtime"](keep_previous=2, dry_run=True)


def test_runtime_clean_returns_nonzero_and_reports_git_exception_reason(monkeypatch, capsys):
    from core import git_runtime

    class FakeShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "removed": [], "archives": {"removed_count": 0}}

    def fail_git_manager():
        raise OSError("git runtime directory unreadable")

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: FakeShowRuntimeManager())
    monkeypatch.setattr(git_runtime, "get_git_runtime_manager", fail_git_manager)
    monkeypatch.setattr(cli, "_managed_runtime_cleaners", lambda: (("git", cli._clean_git_runtime),))
    args = cli.build_parser().parse_args(["runtime", "clean"])

    assert cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert "git_clean_failed" in captured.err
    assert "Git Runtime" in captured.err


def test_runtime_clean_returns_nonzero_for_show_failure_without_false_archive_success(monkeypatch, capsys):
    class FailingShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {
                "ok": False,
                "removed": [],
                "archives": {
                    "outcome": "skipped",
                    "skipped_reason": "archive_inspection_failed",
                },
            }

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: FailingShowRuntimeManager())
    monkeypatch.setattr(cli, "_managed_runtime_cleaners", lambda: ())
    args = cli.build_parser().parse_args(["runtime", "clean"])

    assert cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert "archive_inspection_failed" in captured.err
    assert "unknown" not in captured.err
    assert "downloaded Show Runtime archive" not in captured.out


@pytest.mark.parametrize(
    ("archives", "expected_reason", "prints_partial_summary"),
    [
        (
            {
                "outcome": "skipped",
                "skipped_reason": "archive_inspection_failed",
                "failed_count": 0,
            },
            "archive_inspection_failed",
            False,
        ),
        ({"outcome": "skipped", "failed_count": 1}, "archive_removal_failed", False),
        ({"outcome": "partial", "failed_count": 0}, "archive_removal_failed", True),
    ],
)
def test_runtime_clean_nested_show_archive_failure_controls_output_and_exit(
    monkeypatch,
    capsys,
    archives,
    expected_reason,
    prints_partial_summary,
):
    class ShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "removed": [], "archives": archives}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: ShowRuntimeManager())
    monkeypatch.setattr(cli, "_managed_runtime_cleaners", lambda: ())
    args = cli.build_parser().parse_args(["runtime", "clean"])

    assert cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert expected_reason in captured.err
    if prints_partial_summary:
        assert "archive(s) could not be removed" in captured.err
    else:
        assert "downloaded Show Runtime archive(s) (0 B)" not in captured.out


def test_runtime_clean_dry_run_partial_archive_preview_is_success(monkeypatch, capsys):
    class ShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {
                "ok": True,
                "removed": [],
                "archives": {
                    "outcome": "partial",
                    "candidate_count": 1,
                    "candidate_bytes": 1024,
                    "failed_count": 0,
                },
            }

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: ShowRuntimeManager())
    monkeypatch.setattr(cli, "_managed_runtime_cleaners", lambda: ())
    args = cli.build_parser().parse_args(["runtime", "clean", "--dry-run"])

    assert cli.cmd_runtime(args) == 0
    captured = capsys.readouterr()
    assert "Would remove 1 downloaded Show Runtime archive(s) (1.0 KiB)." in captured.out
    assert captured.err == ""


def test_runtime_clean_json_keeps_nested_failure_payload_and_exits_nonzero(monkeypatch, capsys):
    archives = {
        "outcome": "skipped",
        "skipped_reason": "archive_inspection_failed",
        "failed_count": 0,
    }

    class ShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "removed": [], "archives": archives}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: ShowRuntimeManager())
    monkeypatch.setattr(cli, "_managed_runtime_cleaners", lambda: ())
    args = cli.build_parser().parse_args(["runtime", "clean", "--json"])

    assert cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["archives"] == archives
    assert captured.err == ""


def test_runtime_clean_success_reports_every_consumer_and_exits_zero(monkeypatch, capsys):
    class FakeShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "removed": ["show-old"], "archives": {"removed_count": 0}}

    def cleaner(count):
        return lambda **_kwargs: {"ok": True, "removed": [f"old-{index}" for index in range(count)]}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: FakeShowRuntimeManager())
    monkeypatch.setattr(
        cli,
        "_managed_runtime_cleaners",
        lambda: (
            ("git", cleaner(1)),
            ("memory-runtime", cleaner(2)),
            ("model_hub_engine", cleaner(3)),
            ("tmux", cleaner(4)),
        ),
    )
    args = cli.build_parser().parse_args(["runtime", "clean"])

    assert cli.cmd_runtime(args) == 0
    captured = capsys.readouterr()
    assert "Removed 1 Git Runtime cache item(s)." in captured.out
    assert "Removed 2 Memory Runtime cache item(s)." in captured.out
    assert "Removed 3 Model Hub Runtime cache item(s)." in captured.out
    assert "Removed 4 tmux Runtime cache item(s)." in captured.out
    assert captured.err == ""


def test_runtime_clean_previews_shared_consumer_archive_candidates(monkeypatch, capsys):
    class FakeShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "removed": [], "archives": {"candidate_count": 0}}

    def clean_memory(**_kwargs):
        return {
            "ok": True,
            "removed": [],
            "archives": {
                "candidate_count": 2,
                "candidate_bytes": 2048,
            },
        }

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: FakeShowRuntimeManager())
    monkeypatch.setattr(
        cli,
        "_managed_runtime_cleaners",
        lambda: (("memory-runtime", clean_memory),),
    )
    args = cli.build_parser().parse_args(["runtime", "clean", "--dry-run"])

    assert cli.cmd_runtime(args) == 0
    captured = capsys.readouterr()
    assert "Would remove 2 downloaded Memory Runtime archive(s) (2.0 KiB)." in captured.out
    assert captured.err == ""


def test_runtime_clean_does_not_render_shared_archive_failure_as_zero_success(monkeypatch, capsys):
    class FakeShowRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "removed": [], "archives": {"removed_count": 0}}

    def clean_memory(**_kwargs):
        return {
            "ok": True,
            "removed": [],
            "archives": {
                "outcome": "skipped",
                "skipped_reason": "archive_inspection_failed",
            },
        }

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda _args: FakeShowRuntimeManager())
    monkeypatch.setattr(
        cli,
        "_managed_runtime_cleaners",
        lambda: (("memory-runtime", clean_memory),),
    )
    args = cli.build_parser().parse_args(["runtime", "clean"])

    assert cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert "Memory Runtime cleanup failed (archive_inspection_failed)" in captured.err
    assert "archive_inspection_failed" in captured.err
    assert "downloaded Memory Runtime archive" not in captured.out


@pytest.mark.parametrize("help_args", [("runtime", "--help"), ("runtime", "clean", "--help")])
@pytest.mark.parametrize(
    ("language", "consumer_scope", "failure_contract"),
    [
        ("en", "Show, Git, Memory, Model Hub, and tmux", "exit nonzero if any cleanup fails"),
        ("zh", "Show、Git、Memory、Model Hub 和 tmux", "任一清理失败时以非零状态退出"),
    ],
)
def test_runtime_clean_help_names_consumers_and_failure_exit(
    monkeypatch,
    capsys,
    help_args,
    language,
    consumer_scope,
    failure_contract,
):
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(help_args)

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert cli.i18n_t("runtime.clean.commandHelp", language) in output
    assert consumer_scope in output
    assert failure_contract in output


@pytest.mark.parametrize("legacy_source", ["github", "github-source", "GitHub-Source"])
def test_runtime_manager_migrates_legacy_source_to_packaged_manifest(
    monkeypatch,
    caplog,
    tmp_path,
    legacy_source,
):
    from core import show_runtime

    monkeypatch.setenv("VIBE_SHOW_RUNTIME_SOURCE", legacy_source)
    monkeypatch.setattr(show_runtime, "_WARNED_RETIRED_RUNTIME_SOURCES", set())
    with caplog.at_level("WARNING", logger="core.show_runtime"):
        manager = show_runtime.ShowRuntimeManager(
            workspace_root=tmp_path / "show",
            runtime_dir=tmp_path / "runtime",
        )
        second_manager = show_runtime.ShowRuntimeManager(
            workspace_root=tmp_path / "show-2",
            runtime_dir=tmp_path / "runtime-2",
        )

    assert manager.runtime_source == "manifest-cache"
    assert second_manager.runtime_source == "manifest-cache"
    warnings = [record.message for record in caplog.records if "is retired" in record.message]
    assert warnings == [
        f"VIBE_SHOW_RUNTIME_SOURCE={legacy_source.lower()} is retired; using manifest-cache instead"
    ]


def test_doctor_repair_refreshes_diagnostics_after_repair(monkeypatch):
    paths.ensure_data_dirs()
    restart_path = runtime.get_restart_status_path()
    runtime.write_json(restart_path, {"state": "running", "supervisor_pid": 4242})
    old_timestamp = time.time() - 120
    os.utime(restart_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda pid: False)
    refreshed = []
    doctor_calls = []
    monkeypatch.setattr(cli, "_write_refreshed_runtime_status", lambda: refreshed.append(True))
    monkeypatch.setattr(cli, "_doctor", lambda *, deep=False: doctor_calls.append(deep) or {"ok": True, "groups": []})

    result = cli._repair_doctor_targets(["stale-restart-state"], dry_run=False)

    assert result["ok"] is True
    assert result["results"][0]["status"] == "repaired"
    assert refreshed == [True]
    assert doctor_calls == [False]
    assert result["doctor"] == {"ok": True, "groups": []}


def test_bare_doctor_repair_keeps_post_repair_refresh_local(monkeypatch):
    handlers = {
        "_repair_home_migration": "repaired",
        "_repair_stale_install_runtime": "skipped",
        "_repair_duplicate_service_processes": "skipped",
        "_repair_stale_restart_state": "skipped",
    }
    for name, status in handlers.items():
        target = name.removeprefix("_repair_").replace("_", "-")
        monkeypatch.setattr(
            cli,
            name,
            lambda *, dry_run=False, target=target, status=status: {
                "target": target,
                "status": status,
                "message": status,
            },
        )
    doctor_calls = []
    monkeypatch.setattr(cli, "_doctor", lambda *, deep=False: doctor_calls.append(deep) or {"ok": True})

    result = cli._repair_doctor_targets([], dry_run=False)

    assert result["ok"] is True
    assert doctor_calls == [False]


def test_dependency_or_deep_repair_requests_deep_post_repair_refresh(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_repair_askill",
        lambda *, dry_run=False: {"target": "askill", "status": "repaired", "message": "done"},
    )
    monkeypatch.setattr(
        cli,
        "_repair_stale_restart_state",
        lambda *, dry_run=False: {
            "target": "stale-restart-state",
            "status": "repaired",
            "message": "done",
        },
    )
    doctor_calls = []
    monkeypatch.setattr(cli, "_doctor", lambda *, deep=False: doctor_calls.append(deep) or {"ok": True})

    cli._repair_doctor_targets(["askill"], dry_run=False)
    cli._repair_doctor_targets(["stale-restart-state"], dry_run=False, deep=True)

    assert doctor_calls == [True, True]


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


def test_doctor_parser_accepts_show_runtime_repair_target():
    parser = cli.build_parser()
    args = parser.parse_args(["doctor", "repair", "show-runtime", "--yes"])

    assert args.doctor_repair_targets == ["show-runtime"]


def test_doctor_parser_accepts_managed_dependency_repair_targets():
    parser = cli.build_parser()

    for target in ("askill", "avault", "git-runtime", "memory-runtime", "tmux"):
        args = parser.parse_args(["doctor", "repair", target, "--yes"])
        assert args.doctor_repair_targets == [target]
    assert args.yes is True


def test_doctor_parser_accepts_fast_and_deep_modes():
    parser = cli.build_parser()

    default_args = parser.parse_args(["doctor"])
    fast_args = parser.parse_args(["doctor", "--fast"])
    deep_args = parser.parse_args(["doctor", "--deep"])

    assert default_args.doctor_deep is False
    assert fast_args.doctor_deep is False
    assert deep_args.doctor_deep is True


def test_cmd_doctor_passes_default_fast_mode_to_diagnostics(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["doctor"])
    calls = []

    monkeypatch.setattr(
        cli,
        "_doctor",
        lambda *, deep=True: calls.append(deep)
        or {"mode": "fast", "groups": [], "summary": {"pass": 0, "warn": 0, "fail": 0}, "ok": True},
    )

    assert cli.cmd_doctor(args) == 0
    assert calls == [False]
    capsys.readouterr()


def test_cmd_doctor_passes_deep_mode_to_diagnostics(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["doctor", "--deep"])
    calls = []

    monkeypatch.setattr(
        cli,
        "_doctor",
        lambda *, deep=False: calls.append(deep)
        or {"mode": "deep", "groups": [], "summary": {"pass": 0, "warn": 0, "fail": 0}, "ok": True},
    )

    assert cli.cmd_doctor(args) == 0
    assert calls == [True]
    capsys.readouterr()


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
