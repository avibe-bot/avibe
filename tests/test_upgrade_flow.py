from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api, cli
from vibe.upgrade import (
    UpgradePlan,
    build_upgrade_plan,
    has_newer_version,
    get_current_vibe_bin_dir,
    get_latest_version_info,
    get_restart_command,
    get_restart_environment,
    get_restart_invocation_command,
    get_restart_shell_command,
    get_running_vibe_path,
    get_safe_cwd,
)


def test_build_upgrade_plan_uses_uv_and_preserves_tool_bin_dir(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "uv"
    assert plan.command == ["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"]
    assert plan.env is not None
    assert plan.env["UV_TOOL_BIN_DIR"] == "/custom/bin"
    assert plan.env["PATH"] == "/usr/bin"


def test_build_upgrade_plan_forces_legacy_uv_tool_install(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/vibe-remote/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "uv"
    assert plan.command == ["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade", "--force"]


def test_build_upgrade_plan_uses_pip_for_non_uv_install():
    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "pip"
    assert plan.command == ["/usr/bin/python3", "-m", "pip", "install", "--upgrade", "avibe-os"]
    assert plan.env == {"PATH": "/usr/bin"}


def test_build_upgrade_plan_uses_env_package_spec(monkeypatch):
    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "/tmp/vibe_remote-9999.0.0-py3-none-any.whl")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/vibe-remote/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.command == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        "/tmp/vibe_remote-9999.0.0-py3-none-any.whl",
        "--upgrade",
        "--force",
    ]


def test_build_upgrade_plan_finds_uv_outside_current_path(monkeypatch):
    monkeypatch.setattr(
        "vibe.upgrade.shutil.which",
        lambda command, path=None: None if command == "uv" else "/custom/bin/vibe",
    )
    monkeypatch.setattr(
        "vibe.upgrade.os.path.exists",
        lambda path: path in {"/home/test/.local/bin/uv", "/custom/bin/vibe"},
    )
    monkeypatch.setattr(
        "vibe.upgrade.os.access",
        lambda path, mode: path in {"/home/test/.local/bin/uv", "/custom/bin/vibe"},
    )

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/test"},
    )

    assert plan.method == "uv"
    assert plan.command == ["/home/test/.local/bin/uv", "tool", "install", "avibe-os", "--upgrade"]


def test_get_current_vibe_bin_dir_resolves_launcher_target(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "vibe.upgrade.os.path.islink",
        lambda path: path in {"/usr/local/bin/vibe", "/home/test/.local/bin/vibe"},
    )
    monkeypatch.setattr(
        "vibe.upgrade.os.readlink",
        lambda path: {
            "/usr/local/bin/vibe": "/home/test/.local/bin/vibe",
            "/home/test/.local/bin/vibe": "/home/test/.local/share/uv/tools/vibe-remote/bin/vibe",
        }[path],
    )

    bin_dir = get_current_vibe_bin_dir(vibe_path="/usr/local/bin/vibe")

    assert bin_dir == "/home/test/.local/bin"


def test_get_latest_version_info_uses_override_metadata_url(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text('{"info": {"version": "9999.0.0"}}', encoding="utf-8")
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.0")

    assert info == {"current": "2.2.0", "latest": "9999.0.0", "has_update": True, "error": None}


def test_get_latest_version_info_ignores_prerelease_for_stable_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.8rc1"},
          "releases": {
            "2.2.7": [{}],
            "2.2.8rc1": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.7")

    assert info == {"current": "2.2.7", "latest": "2.2.7", "has_update": False, "error": None}


def test_get_latest_version_info_allows_newer_prerelease_for_prerelease_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.8rc2"},
          "releases": {
            "2.2.7": [{}],
            "2.2.8rc1": [{}],
            "2.2.8rc2": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.8rc1")

    assert info == {"current": "2.2.8rc1", "latest": "2.2.8rc2", "has_update": True, "error": None}


def test_get_latest_version_info_allows_newer_dotted_dev_for_prerelease_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.9.dev2"},
          "releases": {
            "2.2.8": [{}],
            "2.2.9.dev1": [{}],
            "2.2.9.dev2": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.9.dev1")

    assert info == {"current": "2.2.9.dev1", "latest": "2.2.9.dev2", "has_update": True, "error": None}


def test_get_latest_version_info_detects_post_release_for_stable_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.8.post1"},
          "releases": {
            "2.2.8": [{}],
            "2.2.8.post1": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.8")

    assert info == {"current": "2.2.8", "latest": "2.2.8.post1", "has_update": True, "error": None}


def test_has_newer_version_handles_prerelease_without_packaging():
    assert has_newer_version("2.2.8rc2", "2.2.8rc1") is True
    assert has_newer_version("2.2.8", "2.2.8rc2") is True
    assert has_newer_version("2.2.8.post1", "2.2.8") is True
    assert has_newer_version("2.2.8rc1", "2.2.8") is False
    assert has_newer_version("2.2.9.dev2", "2.2.9.dev1") is True


def test_has_newer_version_ignores_local_build_segment():
    # Regression: a source/dev install reports a setuptools-scm local version
    # such as "3.0.4rc4.dev0+gf6ca08af6.d20260624". The old parser could not
    # parse it and fell back to comparing only pure-digit components, so it
    # ranked the build below the latest stable on PyPI ("3.0.3"). That made the
    # updater "upgrade" on every cycle, restart, and DM "updated to 3.0.3" once
    # a minute forever. The local segment must be ignored and the dev/rc build
    # must sort correctly.
    local_build = "3.0.4rc4.dev0+gf6ca08af6.d20260624"
    assert has_newer_version("3.0.3", local_build) is False
    assert has_newer_version(local_build, "3.0.3") is True
    assert has_newer_version("3.0.4rc4", local_build) is True
    assert has_newer_version(local_build, "3.0.4rc4") is False
    assert has_newer_version("3.0.4+meta", "3.0.4") is False
    assert has_newer_version("3.0.4", "3.0.4+meta") is False
    assert has_newer_version("2.2.9rc1.dev2", "2.2.9rc1.dev1") is True
    # Two local builds of the same release are incomparable -> treated equal.
    assert has_newer_version("3.0.4+build2", "3.0.4+build1") is False
    assert has_newer_version("3.0.4+build1", "3.0.4+build2") is False


def test_has_newer_version_orders_dev_before_prerelease():
    # A dev release of a final sorts before that release's alphas/betas/rcs.
    assert has_newer_version("2.2.9a1", "2.2.9.dev1") is True
    assert has_newer_version("2.2.9.dev1", "2.2.9a1") is False


def test_get_latest_version_info_no_update_for_local_dev_build(monkeypatch, tmp_path):
    # Integration-level guard for the notification/restart loop: a local dev
    # build whose release is ahead of everything on PyPI must report no update.
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "3.0.3"},
          "releases": {
            "3.0.2": [{}],
            "3.0.3": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("3.0.4rc4.dev0+gf6ca08af6.d20260624")

    assert info["has_update"] is False


def test_get_latest_version_info_offers_rc_to_local_dev_build(monkeypatch, tmp_path):
    # A dev build is treated as a pre-release, so a matching-release rc on PyPI
    # is a legitimate upgrade and must be offered. Locks in the allow_prereleases
    # policy so a future change can't silently regress it.
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "3.0.4rc1"},
          "releases": {
            "3.0.3": [{}],
            "3.0.4rc1": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("3.0.4.dev0+gf6ca08af6.d20260624")

    assert info == {
        "current": "3.0.4.dev0+gf6ca08af6.d20260624",
        "latest": "3.0.4rc1",
        "has_update": True,
        "error": None,
    }


def test_get_running_vibe_path_prefers_cached_launcher(monkeypatch):
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", "/custom/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: "/other/bin/vibe")

    resolved = get_running_vibe_path(argv0="vibe")

    assert resolved == "/custom/bin/vibe"


def test_get_running_vibe_path_preserves_launcher_symlink(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "vibe.upgrade.shutil.which",
        lambda *args, **kwargs: "/home/test/.local/bin/vibe",
    )

    resolved = get_running_vibe_path(argv0="vibe")

    assert resolved == "/home/test/.local/bin/vibe"


def test_get_running_vibe_path_skips_stale_cached_launcher(monkeypatch):
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", "/stale/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: path != "/stale/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: path != "/stale/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: "/fresh/bin/vibe")

    resolved = get_running_vibe_path(argv0="vibe")

    assert resolved == "/fresh/bin/vibe"


def test_get_restart_command_falls_back_to_python_module(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)

    command = get_restart_command(python_executable="/usr/bin/python3", argv0="python")

    assert command == ["/usr/bin/python3", "-c", "from vibe.cli import main; main()"]


def test_restart_invocation_command_adds_explicit_restart(monkeypatch, tmp_path):
    vibe_path = tmp_path / "bin" / "vibe"
    vibe_path.parent.mkdir()
    vibe_path.write_text("#!/bin/sh\n", encoding="utf-8")
    vibe_path.chmod(0o755)
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", str(vibe_path))

    command = get_restart_invocation_command()

    assert command == [str(vibe_path), "restart"]


def test_restart_shell_command_adds_explicit_restart(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)

    command = get_restart_shell_command(python_executable="/usr/bin/python3", argv0="python")

    assert command == "/usr/bin/python3 -c 'from vibe.cli import main; main()' restart"


def test_get_restart_environment_adds_source_root_for_python_fallback(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)

    env = get_restart_environment(argv0="python", base_env={"PYTHONPATH": "/existing/path"})

    source_root = str(Path(__file__).resolve().parents[1])
    assert env is not None
    assert env["PYTHONPATH"] == f"{source_root}{os.pathsep}/existing/path"


def test_get_restart_environment_normalizes_relative_pythonpath_entries(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    env = get_restart_environment(argv0="python", base_env={"PYTHONPATH": f".{os.pathsep}src"})

    source_root = str(Path(__file__).resolve().parents[1])
    assert env is not None
    assert env["PYTHONPATH"] == f"{source_root}{os.pathsep}{tmp_path}{os.pathsep}{tmp_path / 'src'}"


def test_do_upgrade_uses_upgrade_plan_env_and_restarts(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env={"UV_TOOL_BIN_DIR": "/custom/bin"},
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", lambda **kwargs: calls.setdefault("restart_kwargs", kwargs))

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["run_cmd"] = cmd
            calls["run_kwargs"] = kwargs
        else:
            raise AssertionError(f"unexpected subprocess command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is True
    assert calls["run_cmd"] == plan.command
    assert calls["run_kwargs"]["capture_output"] is True
    assert calls["run_kwargs"]["text"] is True
    assert calls["run_kwargs"]["timeout"] == 120
    assert calls["run_kwargs"]["env"] == plan.env
    safe_cwd = calls["run_kwargs"].get("cwd")
    assert safe_cwd and os.path.isabs(safe_cwd), f"subprocess.run cwd must be an absolute path, got {safe_cwd!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 2.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
    }


def test_do_upgrade_auto_restart_does_not_block_on_runtime_prepare(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    events: list[str] = []

    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", lambda **kwargs: events.append("restart") or {"job_id": "restart"})

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            events.append("upgrade")
        else:
            raise AssertionError(f"unexpected subprocess command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert events == ["upgrade", "restart"]


def test_do_upgrade_running_runtime_honors_show_runtime_skip_for_restart(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "1")
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", lambda **kwargs: calls.setdefault("restart_kwargs", kwargs))
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is True
    assert calls["restart_kwargs"]["prepare_show_runtime"] is False


def test_do_upgrade_reports_restart_scheduling_failure_as_partial_success(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )

    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)

    def fail_restart(**kwargs):
        raise RuntimeError("bad launcher")

    monkeypatch.setattr(api, "schedule_restart", fail_restart)
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is False
    assert result["message"] == "Upgrade successful, but restart scheduling failed. Please restart vibe."
    assert "Restart scheduling failed" in result["output"]
    assert "bad launcher" in result["output"]


def test_do_upgrade_without_auto_restart_prepares_runtime(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)

    def fail_restart(**kwargs):
        raise AssertionError("schedule_restart should not run when auto_restart is disabled")

    monkeypatch.setattr(api, "schedule_restart", fail_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["upgrade_cmd"] = cmd
            calls["upgrade_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        if cmd == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]:
            calls["runtime_prepare_cmd"] = cmd
            calls["runtime_prepare_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="runtime ready", stderr="")
        raise AssertionError(f"unexpected subprocess command: {cmd}")

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    result = api.do_upgrade(auto_restart=False)

    assert result["ok"] is True
    assert result["restarting"] is False
    assert result["output"] == "done\n\nruntime ready"
    assert calls["runtime_prepare_cmd"] == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]
    assert calls["runtime_prepare_kwargs"]["capture_output"] is True
    assert calls["runtime_prepare_kwargs"]["text"] is True
    assert calls["runtime_prepare_kwargs"]["timeout"] == 600  # prepare now budgets for Show Runtime + askill
    assert calls["runtime_prepare_kwargs"]["cwd"] == calls["upgrade_kwargs"]["cwd"]


def test_do_upgrade_keeps_runtime_stopped_when_it_was_not_running(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: False)

    def fail_restart(**kwargs):
        raise AssertionError("schedule_restart should not run when Avibe was not running")

    monkeypatch.setattr(api, "schedule_restart", fail_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["upgrade_cmd"] = cmd
            calls["upgrade_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        if cmd == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]:
            calls["runtime_prepare_cmd"] = cmd
            calls["runtime_prepare_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="runtime ready", stderr="")
        raise AssertionError(f"unexpected subprocess command: {cmd}")

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is False
    assert result["message"] == "Upgrade successful. Please restart vibe."
    assert calls["runtime_prepare_cmd"] == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]


def test_api_runtime_process_was_running_checks_service_and_ui_pid_files(monkeypatch, tmp_path):
    service_pid_path = tmp_path / "service.pid"
    ui_pid_path = tmp_path / "ui.pid"
    service_pid_path.write_text("111", encoding="utf-8")
    ui_pid_path.write_text("222", encoding="utf-8")

    monkeypatch.setattr(api.paths, "get_runtime_pid_path", lambda: service_pid_path)
    monkeypatch.setattr(api.paths, "get_runtime_ui_pid_path", lambda: ui_pid_path)

    from vibe import runtime

    service_running = False
    ui_running = False

    def fake_ui_running(pid_path):
        return pid_path == ui_pid_path and ui_running

    monkeypatch.setattr(runtime, "service_process_running", lambda: service_running)
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", fake_ui_running)

    assert api._runtime_process_was_running() is False
    ui_running = True
    assert api._runtime_process_was_running() is True
    ui_running = False
    service_running = True
    assert api._runtime_process_was_running() is True


def test_cli_runtime_process_was_running_uses_service_process_state(monkeypatch):
    service_running = False

    monkeypatch.setattr(cli.runtime, "service_process_running", lambda: service_running)
    monkeypatch.setattr(cli.runtime, "ui_pid_file_points_to_running_ui", lambda: False)

    assert cli._runtime_process_was_running() is False
    service_running = True
    assert cli._runtime_process_was_running() is True


def test_cmd_upgrade_uses_upgrade_plan_env(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env={"UV_TOOL_BIN_DIR": "/custom/bin"},
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)

    def fake_schedule_restart(**kwargs):
        calls["restart_kwargs"] = kwargs
        return {"job_id": "restart"}

    monkeypatch.setattr(cli, "schedule_restart", fake_schedule_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
        else:
            raise AssertionError(f"unexpected subprocess command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.cmd_upgrade()

    assert result == 0
    assert calls["cmd"] == plan.command
    assert calls["kwargs"]["capture_output"] is True
    assert calls["kwargs"]["text"] is True
    assert calls["kwargs"]["env"] == plan.env
    assert "cwd" in calls["kwargs"], "subprocess.run must specify cwd to avoid stale venv cwd"
    assert os.path.isabs(calls["kwargs"]["cwd"]), f"cwd must be absolute, got {calls['kwargs']['cwd']!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 0.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
    }


def test_cmd_upgrade_running_runtime_honors_show_runtime_skip_for_restart(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "true")
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)

    def fake_schedule_restart(**kwargs):
        calls["restart_kwargs"] = kwargs
        return {"job_id": "restart"}

    monkeypatch.setattr(cli, "schedule_restart", fake_schedule_restart)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    assert cli.cmd_upgrade() == 0
    assert calls["restart_kwargs"]["prepare_show_runtime"] is False


def test_cmd_upgrade_reports_restart_scheduling_failure_as_partial_success(monkeypatch, capsys):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )

    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)

    def fail_restart(**kwargs):
        raise RuntimeError("bad launcher")

    monkeypatch.setattr(cli, "schedule_restart", fail_restart)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    assert cli.cmd_upgrade() == 2
    output = capsys.readouterr().out
    assert "Upgrade installed, but restart scheduling failed." in output
    assert "Restart error: bad launcher" in output
    assert "Run `vibe restart` to use the new version." in output
    assert "Upgrade failed" not in output


def test_cmd_upgrade_keeps_runtime_stopped_when_it_was_not_running(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: False)

    def fail_restart(**kwargs):
        raise AssertionError("schedule_restart should not run when Avibe was not running")

    monkeypatch.setattr(cli, "schedule_restart", fail_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        if cmd == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]:
            calls["runtime_prepare_cmd"] = cmd
            calls["runtime_prepare_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="runtime ready", stderr="")
        raise AssertionError(f"unexpected subprocess command: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.cmd_upgrade() == 0
    assert calls["cmd"] == plan.command
    assert calls["runtime_prepare_cmd"] == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]


def test_cmd_upgrade_skips_install_when_already_latest(monkeypatch):
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": False, "latest": "2.2.0"})

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called when already latest")

    monkeypatch.setattr(cli.subprocess, "run", fail_run)

    assert cli.cmd_upgrade() == 0


def test_get_safe_cwd_returns_absolute_existing_dir():
    cwd = get_safe_cwd()
    assert os.path.isabs(cwd)
    assert os.path.isdir(cwd)


def test_get_safe_cwd_falls_back_when_home_invalid(monkeypatch):
    monkeypatch.setenv("HOME", "/nonexistent_dir_for_test")
    cwd = get_safe_cwd()
    assert os.path.isabs(cwd)
    assert os.path.isdir(cwd)
    assert cwd != "/nonexistent_dir_for_test"
