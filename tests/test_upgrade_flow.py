from __future__ import annotations

import ast
from contextlib import nullcontext
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api, cli, runtime
from vibe import upgrade as vibe_upgrade
from vibe.runtime import ServiceLauncher
from vibe.upgrade import (
    AtomicActivation,
    MemoryRequirementUnreadableError,
    PIP_DOWNLOAD_DEST_PLACEHOLDER,
    UpgradePlan,
    build_upgrade_plan,
    configured_memory_enabled,
    has_newer_version,
    get_current_vibe_bin_dir,
    get_current_uv_tool_dir,
    get_latest_version_info,
    get_restart_command,
    get_restart_environment,
    get_restart_invocation_command,
    get_restart_shell_command,
    defer_upgrade_activation,
    get_running_vibe_path,
    get_safe_cwd,
    pinned_package_spec,
    release_asset_specs,
    execute_upgrade_plan,
    restart_is_pending,
)


@pytest.fixture(autouse=True)
def _tree_is_not_an_installed_distribution(monkeypatch):
    """Every test here describes a machine, not the machine running the tests.

    Installed provider metadata differs between a source checkout and CI. Empty
    provider metadata keeps forward-plan tests deterministic; tests for a mixed
    provider state replace it explicitly.
    """

    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: [])
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)



def test_build_upgrade_plan_stages_custom_legacy_uv_launcher(monkeypatch):
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
    assert "UV_TOOL_DIR" in plan.env
    assert "UV_TOOL_BIN_DIR" in plan.env
    assert plan.env["PATH"] == "/usr/bin"
    assert plan.preflight_error is None
    assert plan.activation is not None
    assert plan.activation.launcher == Path("/custom/bin/vibe")


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


def test_build_upgrade_plan_stages_uv_install_for_a_stable_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / "bin" / "vibe"
    target = tmp_path / "old" / "vibe"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    monkeypatch.setattr("vibe.upgrade.config_paths.get_vibe_remote_dir", lambda: tmp_path / "home")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path=str(launcher),
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.activation is not None
    assert plan.activation.launcher == launcher
    assert plan.env is not None
    tool_dir = Path(plan.env["UV_TOOL_DIR"])
    bin_dir = Path(plan.env["UV_TOOL_BIN_DIR"])
    assert tool_dir == bin_dir.parent / "uv" / "tools"
    assert bin_dir.parent.parent == tmp_path / "home" / "runtime" / "install-generations"


def test_build_upgrade_plan_refuses_uv_install_without_activation_point(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/tmp/.local/share/uv/tools/avibe-os/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.activation is None
    assert plan.preflight_error is not None


def test_activate_upgrade_candidate_replaces_launcher_only_after_verification(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / "bin" / "vibe"
    old = tmp_path / "old" / "vibe"
    candidate = tmp_path / "generation" / "bin" / "vibe"
    old.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    old.write_text("old\n", encoding="utf-8")
    candidate.write_text("new\n", encoding="utf-8")
    candidate.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(old)
    activation = upgrade.AtomicActivation(launcher=launcher, candidate_launcher=candidate)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _activation: upgrade.IntegrityResult(True, 1))

    upgrade.activate_upgrade_candidate(activation)

    assert launcher.is_symlink()
    assert launcher.resolve() == candidate.resolve()
    assert old.read_text(encoding="utf-8") == "old\n"


def test_activate_upgrade_candidate_replaces_windows_hardlink_launcher(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    old_generation = tmp_path / "home" / "runtime" / "install-generations" / "old"
    candidate = tmp_path / "home" / "runtime" / "install-generations" / "new" / "bin" / "vibe.exe"
    launcher.parent.mkdir(parents=True)
    old_launcher = old_generation / "bin" / "vibe.exe"
    old_launcher.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    old_launcher.write_text("old\n", encoding="utf-8")
    os.link(old_launcher, launcher)
    os.utime(old_generation, (0, 0))
    candidate.write_text("new\n", encoding="utf-8")
    candidate.chmod(0o755)
    activation = upgrade.AtomicActivation(launcher=launcher, candidate_launcher=candidate)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: tmp_path / "home" / "runtime" / "install-generations")
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _activation: upgrade.IntegrityResult(True, 1))

    upgrade.activate_upgrade_candidate(activation)

    assert launcher.is_symlink()
    assert launcher.resolve() == candidate.resolve()


def test_activation_leaves_every_other_generation_untouched(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "generations"
    launcher = tmp_path / "bin" / "vibe"
    previous = root / "previous" / "bin" / "vibe"
    candidate = root / "candidate" / "bin" / "vibe"
    unrelated = root / "unrelated" / "bin" / "vibe"
    for path in (previous, candidate, unrelated):
        path.parent.mkdir(parents=True)
        path.write_text(path.parent.parent.name, encoding="utf-8")
        path.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(previous)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _activation: upgrade.IntegrityResult(True, 1))

    upgrade.activate_upgrade_candidate(upgrade.AtomicActivation(launcher, candidate, root / "previous"))

    assert launcher.resolve() == candidate.resolve()
    assert previous.exists()
    assert unrelated.exists()


def test_activation_compares_source_generation_by_filesystem_identity(monkeypatch, tmp_path):
    from vibe import upgrade

    physical_home = tmp_path / "physical-home"
    logical_home = tmp_path / "logical-home"
    logical_home.symlink_to(physical_home, target_is_directory=True)
    root = physical_home / "runtime" / "install-generations"
    source = root / "source" / "bin" / "vibe"
    launcher = tmp_path / "bin" / "vibe"
    source.parent.mkdir(parents=True)
    launcher.parent.mkdir()
    source.write_text("source", encoding="utf-8")
    launcher.symlink_to(source)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    activation = upgrade.AtomicActivation(
        launcher,
        root / "candidate" / "bin" / "vibe",
        logical_home / "runtime" / "install-generations" / "source" / "bin" / "vibe",
    )

    assert upgrade.atomic_activation_source_is_current(activation)


def test_activation_compares_a_conventional_launcher_snapshot(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "runtime" / "install-generations"
    old = tmp_path / "legacy" / "old-vibe"
    new = tmp_path / "legacy" / "new-vibe"
    launcher = tmp_path / "bin" / "vibe"
    for path in (old, new):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
        path.chmod(0o755)
    launcher.parent.mkdir()
    launcher.symlink_to(old)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    activation = upgrade.AtomicActivation(launcher, root / "candidate" / "bin" / "vibe", old)

    assert upgrade.atomic_activation_source_is_current(activation)
    launcher.unlink()
    launcher.symlink_to(new)
    assert not upgrade.atomic_activation_source_is_current(activation)


def test_installer_activation_uses_the_shared_locked_boundary(monkeypatch, tmp_path):
    from vibe import upgrade

    events: list[str] = []

    class Lock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, *_args):
            events.append("lock-exit")

    activation = upgrade.AtomicActivation(tmp_path / "vibe", tmp_path / "candidate")
    monkeypatch.setattr(upgrade, "atomic_upgrade_lock", Lock)
    monkeypatch.setattr(upgrade, "activation_block_reason", lambda _activation: None)
    monkeypatch.setattr(upgrade, "activate_upgrade_candidate", lambda _activation: events.append("activate"))

    upgrade.activate_installer_candidate(activation)

    assert events == ["lock-enter", "activate", "lock-exit"]


def test_installer_activation_reports_its_protocol_version(capsys):
    from vibe import cli

    assert cli._dispatch_installer_activation(["--protocol-version"]) == 0
    assert capsys.readouterr().out == "2\n"


def test_installer_activation_snapshot_comes_from_shared_launcher_identity(monkeypatch, tmp_path, capsys):
    from vibe import cli, upgrade

    root = tmp_path / "generations"
    generation = root / "active"
    target = generation / "bin" / "vibe"
    launcher = tmp_path / "bin" / "vibe"
    target.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    target.write_text("active\n", encoding="utf-8")
    launcher.symlink_to(target)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(cli, "atomic_uv_install_root", lambda: root)

    assert cli._dispatch_installer_activation(["--snapshot", "--launcher", str(launcher)]) == 0
    assert capsys.readouterr().out == f"{generation.resolve()}\n"


def test_installer_rejects_a_snapshot_superseded_by_runtime_activation(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "generations"
    launcher = tmp_path / "bin" / "vibe"
    old = root / "old" / "bin" / "vibe"
    runtime_candidate = root / "runtime" / "bin" / "vibe"
    installer_candidate = root / "installer" / "bin" / "vibe"
    for path in (old, runtime_candidate, installer_candidate):
        path.parent.mkdir(parents=True)
        path.write_text(path.parent.parent.name, encoding="utf-8")
        path.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(old)
    installer_activation = upgrade.AtomicActivation(launcher, installer_candidate, root / "old")
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(upgrade, "restart_is_pending", lambda: False)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _activation: upgrade.IntegrityResult(True, 1))

    upgrade.activate_upgrade_candidate(upgrade.AtomicActivation(launcher, runtime_candidate, root / "old"))

    with pytest.raises(RuntimeError, match="changed while the installer was staging"):
        upgrade.activate_installer_candidate(installer_activation)
    assert launcher.resolve() == runtime_candidate.resolve()


def test_verify_upgrade_candidate_follows_uv_launcher_to_tool_environment(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / "bin" / "vibe"
    tool_bin = tmp_path / "tools" / "avibe-os" / "bin"
    tool_bin.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(tool_bin / "vibe")
    (tool_bin / "vibe").write_text("#!/bin/sh\n", encoding="utf-8")
    (tool_bin / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    (tool_bin / "vibe").chmod(0o755)
    (tool_bin / "python3").chmod(0o755)
    activation = upgrade.AtomicActivation(launcher=launcher, candidate_launcher=launcher)
    monkeypatch.setattr(upgrade, "verify_python_environment", lambda python: upgrade.IntegrityResult(True, 2))

    result = upgrade.verify_upgrade_candidate(activation)

    assert result.ok is True
    assert result.checked_files == 2


def test_verify_upgrade_candidate_searches_staged_tools_for_windows_python(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "generations"
    candidate = root / "generation" / "bin" / "vibe.exe"
    python = root / "generation" / "tools" / "avibe-os" / "Scripts" / "python.exe"
    candidate.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.write_text("python\n", encoding="utf-8")
    candidate.chmod(0o755)
    python.chmod(0o755)
    monkeypatch.setattr(upgrade.os, "name", "nt")
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    observed = {}

    def verify(path):
        observed["path"] = path
        return upgrade.IntegrityResult(True, 3)

    monkeypatch.setattr(upgrade, "verify_python_environment", verify)

    result = upgrade.verify_upgrade_candidate(upgrade.AtomicActivation(candidate, candidate))

    assert result.ok is True
    assert observed["path"] == python


def test_verify_upgrade_candidate_rejects_launcher_that_fails_startup_probe(monkeypatch, tmp_path):
    from vibe import upgrade

    candidate = tmp_path / "bin" / "vibe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    candidate.chmod(0o755)
    python = candidate.parent / "python3"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(upgrade, "verify_python_environment", lambda _python: upgrade.IntegrityResult(True, 3))

    result = upgrade.verify_upgrade_candidate(upgrade.AtomicActivation(candidate, candidate))

    assert result.ok is False
    assert "launcher probe failed" in result.failures[0]


def test_atomic_activation_source_must_still_be_active_under_lock(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    old = root / "old" / "bin" / "vibe"
    new = root / "new" / "bin" / "vibe"
    launcher = tmp_path / ".local" / "bin" / "vibe"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_text(path.parent.parent.name, encoding="utf-8")
        path.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(old)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    activation = upgrade.AtomicActivation(launcher, root / "candidate" / "bin" / "vibe", root / "old")

    assert upgrade.atomic_activation_source_is_current(activation)
    launcher.unlink()
    launcher.symlink_to(new)
    assert not upgrade.atomic_activation_source_is_current(activation)


def test_launcher_generation_recognizes_windows_hardlink(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    generation = root / "old"
    source = generation / "bin" / "vibe.exe"
    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    source.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    source.write_text("old\n", encoding="utf-8")
    os.link(source, launcher)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    assert upgrade._launcher_generation(launcher, root) == generation


def test_launcher_generation_does_not_match_hardlink_inode_on_another_device(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    generation = root / "old"
    source = generation / "bin" / "vibe.exe"
    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    source.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    source.write_text("old\n", encoding="utf-8")
    launcher.write_text("old\n", encoding="utf-8")
    original_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        if path == launcher:
            return SimpleNamespace(st_dev=1, st_ino=7, st_mode=stat.S_IFREG)
        if path == source:
            return SimpleNamespace(st_dev=2, st_ino=7, st_mode=stat.S_IFREG)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    assert upgrade._generation_for_hardlink(launcher, root) is None


def test_hardlink_generation_lookup_degrades_when_root_cannot_be_enumerated(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    root.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher\n", encoding="utf-8")
    original_iterdir = Path.iterdir

    def denied(path):
        if path == root.resolve():
            raise PermissionError("denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    assert upgrade._generation_for_hardlink(launcher, root) is None


def test_copy_marker_must_match_the_live_launcher(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    old = root / "old"
    current = root / "current"
    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    marker = launcher.parent / ".vibe.exe.avibe-generation"
    for generation, content in ((old, "old\n"), (current, "current\n")):
        candidate = generation / "bin" / "vibe.exe"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(content, encoding="utf-8")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("current\n", encoding="utf-8")
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    marker.write_text(str(old), encoding="utf-8")
    assert upgrade._launcher_generation(launcher, root) is None

    marker.write_text(str(current), encoding="utf-8")
    assert upgrade._launcher_generation(launcher, root) == current.resolve()


def test_launcher_is_current_process_only_matches_windows_launcher(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / "bin" / "vibe.exe"
    launcher.parent.mkdir()
    launcher.write_text("launcher\n", encoding="utf-8")
    monkeypatch.setattr(upgrade.os, "name", "nt")
    monkeypatch.setattr(upgrade.sys, "argv", [str(launcher)])

    assert upgrade.launcher_is_current_process(launcher)
    assert not upgrade.launcher_is_current_process(tmp_path / "bin" / "other.exe")


def test_launcher_is_current_process_ignores_inherited_launcher_hint(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / "bin" / "vibe.exe"
    launcher.parent.mkdir()
    launcher.write_text("launcher\n", encoding="utf-8")
    monkeypatch.setattr(upgrade.os, "name", "nt")
    monkeypatch.setattr(upgrade.sys, "argv", [str(tmp_path / "service_main.py")])
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", str(launcher))

    assert not upgrade.launcher_is_current_process(launcher)


def test_deferred_activation_dispatch_activates_then_schedules_restart(monkeypatch, tmp_path):
    from vibe import cli

    launcher = tmp_path / "bin" / "vibe.exe"
    candidate = tmp_path / "generation" / "bin" / "vibe.exe"
    calls: list[str] = []
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(cli, "atomic_upgrade_lock", lambda: nullcontext())
    monkeypatch.setattr(cli, "activation_block_reason", lambda _activation: None)
    monkeypatch.setattr(cli, "activate_upgrade_candidate", lambda _activation: calls.append("activate"))
    monkeypatch.setattr(cli, "schedule_restart", lambda **_kwargs: calls.append("restart"))

    result = cli._dispatch_deferred_upgrade_activation(
        [
            "--parent-pid",
            "123",
            "--launcher",
            str(launcher),
            "--candidate",
            str(candidate),
            "--restart",
        ]
    )

    assert result == 0
    assert calls == ["activate", "restart"]


def test_deferred_activation_seeds_restart_before_releasing_install_lock(monkeypatch, tmp_path):
    from vibe import cli

    events: list[str] = []

    class Lock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, *_args):
            events.append("lock-exit")

    monkeypatch.setattr(cli.runtime, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(cli, "atomic_upgrade_lock", Lock)
    monkeypatch.setattr(cli, "activation_block_reason", lambda _activation: None)
    monkeypatch.setattr(cli, "activate_upgrade_candidate", lambda _activation: events.append("activate"))
    monkeypatch.setattr(cli, "schedule_restart", lambda **_kwargs: events.append("restart"))

    result = cli._dispatch_deferred_upgrade_activation(
        [
            "--parent-pid",
            "123",
            "--launcher",
            str(tmp_path / "vibe.exe"),
            "--candidate",
            str(tmp_path / "candidate.exe"),
            "--restart",
        ]
    )

    assert result == 0
    assert events == ["lock-enter", "activate", "restart", "lock-exit"]


def test_deferred_offline_activation_prepares_show_runtime(monkeypatch, tmp_path):
    from vibe import cli

    calls: list[str] = []
    launcher = tmp_path / "vibe.exe"
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(cli, "atomic_upgrade_lock", lambda: nullcontext())
    monkeypatch.setattr(cli, "activation_block_reason", lambda _activation: None)
    monkeypatch.setattr(cli, "activate_upgrade_candidate", lambda _activation: calls.append("activate"))
    monkeypatch.setattr(cli, "_prepare_show_runtime_after_install", lambda path: calls.append(f"prepare:{path}"))

    result = cli._dispatch_deferred_upgrade_activation(
        [
            "--parent-pid",
            "123",
            "--launcher",
            str(launcher),
            "--candidate",
            str(tmp_path / "candidate.exe"),
            "--prepare-show-runtime",
        ]
    )

    assert result == 0
    assert calls == ["activate", f"prepare:{launcher}"]


def test_deferred_upgrade_activation_uses_candidate_python(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    candidate = tmp_path / "generation" / "bin" / "vibe.exe"
    candidate_python = tmp_path / "generation" / "tools" / "avibe-os" / "Scripts" / "python.exe"
    candidate.parent.mkdir(parents=True)
    candidate_python.parent.mkdir(parents=True)
    candidate.write_text("candidate\n", encoding="utf-8")
    candidate_python.write_text("python\n", encoding="utf-8")
    activation = AtomicActivation(launcher, candidate, tmp_path / "generation")
    calls = {}

    monkeypatch.setattr(upgrade, "_candidate_python", lambda _candidate: candidate_python)
    monkeypatch.setattr(upgrade.runtime_mod, "process_create_time", lambda _pid: 123.0)
    monkeypatch.setattr(upgrade, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(upgrade, "isolated_probe_environment", lambda: {"PATH": "clean"})
    monkeypatch.setattr(upgrade.config_paths, "get_logs_dir", lambda: tmp_path / "logs")

    class Process:
        pid = 456

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(upgrade.subprocess, "Popen", fake_popen)

    process = defer_upgrade_activation(
        activation,
        parent_pid=123,
        restart_required=True,
        prepare_show_runtime=True,
    )

    assert process.pid == 456
    assert calls["command"][:4] == [str(candidate_python), "-c", "from vibe.cli import main; main()", "__activate-upgrade"]
    assert "--parent-pid" in calls["command"]
    assert "--restart" in calls["command"]
    assert "--prepare-show-runtime" in calls["command"]
    assert calls["kwargs"]["env"] == {"PATH": "clean"}


def test_deferred_activation_rejects_missing_source_when_launcher_changed(monkeypatch, tmp_path):
    from vibe import cli

    calls: list[str] = []
    monkeypatch.setattr(cli.runtime, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(cli, "atomic_upgrade_lock", lambda: nullcontext())
    monkeypatch.setattr(cli, "activation_block_reason", lambda _activation: "superseded")
    monkeypatch.setattr(cli, "discard_atomic_uv_install_generation", lambda _path: calls.append("discard"))
    monkeypatch.setattr(cli, "activate_upgrade_candidate", lambda _activation: calls.append("activate"))

    result = cli._dispatch_deferred_upgrade_activation(
        ["--parent-pid", "123", "--launcher", str(tmp_path / "vibe.exe"), "--candidate", str(tmp_path / "candidate")]
    )

    assert result == 1
    assert calls == ["discard"]


def test_cli_launcher_path_uses_hardlinked_generation(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    generation = root / "old"
    source = generation / "bin" / "vibe.exe"
    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    source.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    source.write_text("old\n", encoding="utf-8")
    source.chmod(0o755)
    os.link(source, launcher)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    saved = ServiceLauncher(python=str(launcher), main="service_main.py")

    assert upgrade.get_cli_launcher_path(saved) == source


def test_activate_upgrade_candidate_falls_back_to_hardlink(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / ".local" / "bin" / "vibe"
    candidate = tmp_path / "generation" / "bin" / "vibe"
    launcher.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    launcher.write_text("old\n", encoding="utf-8")
    candidate.write_text("new\n", encoding="utf-8")
    candidate.chmod(0o755)
    activation = upgrade.AtomicActivation(launcher=launcher, candidate_launcher=candidate)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _activation: upgrade.IntegrityResult(True, 1))
    monkeypatch.setattr(Path, "symlink_to", lambda self, _target: (_ for _ in ()).throw(OSError("symlink denied")))

    upgrade.activate_upgrade_candidate(activation)

    assert not launcher.is_symlink()
    assert launcher.read_text(encoding="utf-8") == "new\n"


def test_activate_upgrade_candidate_falls_back_to_copy_when_links_fail(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / ".local" / "bin" / "vibe"
    root = tmp_path / "home" / "runtime" / "install-generations"
    candidate = root / "generation" / "bin" / "vibe"
    launcher.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    launcher.write_text("old\n", encoding="utf-8")
    candidate.write_text("new\n", encoding="utf-8")
    candidate.chmod(0o755)
    activation = upgrade.AtomicActivation(launcher=launcher, candidate_launcher=candidate)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _activation: upgrade.IntegrityResult(True, 1))
    monkeypatch.setattr(Path, "symlink_to", lambda self, _target: (_ for _ in ()).throw(OSError("symlink denied")))
    monkeypatch.setattr(upgrade.os, "link", lambda _target, _replacement: (_ for _ in ()).throw(OSError("cross-device link")))

    upgrade.activate_upgrade_candidate(activation)

    assert not launcher.is_symlink()
    assert launcher.read_text(encoding="utf-8") == "new\n"
    assert upgrade._launcher_generation(launcher, root) == root / "generation"


def test_is_uv_tool_install_uses_logical_atomic_generation_path(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    executable = root / "generation" / "tools" / "avibe-os" / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    assert upgrade.is_uv_tool_install(str(executable))


def test_custom_legacy_bin_is_a_stable_launcher_without_uv_environment(monkeypatch, tmp_path):
    from vibe import upgrade

    launcher = tmp_path / "custom-tools" / "vibe.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)

    assert upgrade._is_stable_launcher_path(launcher)


def test_uv_environment_entry_point_is_not_a_stable_launcher(monkeypatch, tmp_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    launcher = root / "generation" / "tools" / "avibe-os" / "bin" / "vibe.exe"
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)

    assert not upgrade._is_stable_launcher_path(launcher)


def test_restart_is_pending_until_the_seed_marker_is_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    status = runtime.get_restart_status_path()
    status.parent.mkdir(parents=True)
    runtime.write_json(status, {"state": "scheduled", "supervisor_pid": None})

    assert restart_is_pending()

    runtime.write_json(status, {"state": "succeeded", "supervisor_pid": None})
    assert not restart_is_pending()


def test_legacy_restart_record_expires_even_when_its_pid_was_reused(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    status = runtime.get_restart_status_path()
    status.parent.mkdir(parents=True)
    runtime.write_json(status, {"state": "scheduled", "supervisor_pid": 456})
    os.utime(status, (0, 0))
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 456)

    assert not restart_is_pending()


def test_restart_state_with_a_non_object_shape_is_not_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    status = runtime.get_restart_status_path()
    status.parent.mkdir(parents=True)
    status.write_text("[]", encoding="utf-8")

    assert not restart_is_pending()


@pytest.mark.parametrize("tools_path", [Path("uv/tools"), Path("tools")], ids=["bridged", "legacy"])
def test_doctor_resolves_hardlinked_atomic_generation(monkeypatch, tmp_path, tools_path):
    from vibe import upgrade

    root = tmp_path / "home" / "runtime" / "install-generations"
    generation = root / "old"
    launcher_source = generation / "bin" / "vibe.exe"
    launcher = tmp_path / ".local" / "bin" / "vibe.exe"
    site_packages = generation / tools_path / "avibe-os" / "lib" / "python3.12" / "site-packages"
    launcher_source.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    launcher_source.write_text("launcher\n", encoding="utf-8")
    os.link(launcher_source, launcher)
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(cli, "atomic_uv_install_root", lambda: root)

    assert site_packages in cli._uv_tool_site_packages_for_vibe(launcher)


def test_do_upgrade_keeps_active_launcher_when_staged_install_fails(monkeypatch, tmp_path):
    launcher = tmp_path / "bin" / "vibe"
    old = tmp_path / "old" / "vibe"
    candidate = tmp_path / "generation" / "bin" / "vibe"
    old.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    old.write_text("old\n", encoding="utf-8")
    candidate.write_text("partial\n", encoding="utf-8")
    candidate.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(old)
    plan = UpgradePlan(
        command=["uv", "tool", "install", "avibe-os", "--upgrade"],
        env={},
        method="uv",
        activation=AtomicActivation(launcher, candidate),
    )
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: str(launcher))
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: False)
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(plan.command, 1, stdout="", stderr="interrupted"),
    )

    result = api.do_upgrade(auto_restart=False)

    assert result["ok"] is False
    assert launcher.resolve() == old.resolve()


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


#: How each installer spells "do it even though you believe it is already done".
#: Written as a mapping rather than as two assertions so that an installer added
#: to `build_upgrade_plan` later fails this test until someone decides what its
#: word is, instead of silently shipping an exact install that can no-op.
FORCES_THE_INSTALL = {"uv": "--force", "pip": "--force-reinstall"}


def test_an_exact_plan_never_asks_for_an_upgrade_and_always_forces_the_install(monkeypatch):
    # The explicit Memory install targets the running core release. Asking for an
    # upgrade would widen that request, while omitting force lets the installer
    # declare the already-present core requirement satisfied without applying the
    # matching optional package.
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    uv_plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
        package_name="avibe-os",
    )
    assert uv_plan.method == "uv"
    assert uv_plan.command == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        "avibe-os==3.0.10",
        # Unconditional here: the version already installed is the newer one, so
        # without it uv reports the tool as satisfied and installs nothing.
        "--force",
    ]
    assert "--upgrade" not in uv_plan.command
    assert uv_plan.env["UV_TOOL_DIR"] != "/tmp/.local/share/uv/tools"
    assert uv_plan.activation is not None
    assert uv_plan.activation.launcher == Path("/custom/bin/vibe")
    assert Path(uv_plan.env["UV_TOOL_DIR"]).is_relative_to(vibe_upgrade.atomic_uv_install_root())

    pip_plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
        package_name="avibe-os",
    )
    assert pip_plan.method == "pip"
    assert pip_plan.command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "avibe-os==3.0.10",
    ]

    for plan in (uv_plan, pip_plan):
        assert "--upgrade" not in plan.command
        assert FORCES_THE_INSTALL[plan.method] in plan.command


def test_an_exact_memory_plan_names_both_index_pins_when_no_source_is_given(monkeypatch):
    # The repair reaches an index by default. A caller that names no install
    # source gets exactly the pins it got before install sources existed.
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    uv_plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.10",
    )
    assert uv_plan.command == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        "avibe-os==3.0.10",
        "--with",
        "avibe-memory==3.0.10",
        "--force",
    ]
    assert uv_plan.preflight_command == [
        "/usr/local/bin/uv",
        "pip",
        "install",
        "--dry-run",
        "--python",
        "/tmp/.local/share/uv/tools/avibe-os/bin/python",
        "avibe-os==3.0.10",
        "avibe-memory==3.0.10",
    ]
    assert uv_plan.activation is not None
    assert uv_plan.env["UV_TOOL_DIR"] != "/tmp/.local/share/uv/tools"
    assert uv_plan.env["UV_TOOL_BIN_DIR"] != "/custom/bin"

    pip_plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.10",
    )
    assert pip_plan.command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "avibe-os==3.0.10",
        "avibe-memory==3.0.10",
    ]
    assert pip_plan.preflight_command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "download",
        "--dest",
        PIP_DOWNLOAD_DEST_PLACEHOLDER,
        "--no-deps",
        "avibe-os==3.0.10",
        "avibe-memory==3.0.10",
    ]


PREVIEW_VERSION = "3.0.16rc1"
PREVIEW_CORE_URL = (
    "https://github.com/avibe-bot/avibe/releases/download/gh-v3.0.16rc1/avibe_os-3.0.16rc1-py3-none-any.whl"
)
PREVIEW_MEMORY_URL = (
    "https://github.com/avibe-bot/avibe/releases/download/gh-v3.0.16rc1/avibe_memory-3.0.16rc1-py3-none-any.whl"
)


def _installed_from(monkeypatch, origin: str | None) -> None:
    """Record where the installer says this copy of core came from."""

    monkeypatch.setattr("vibe.upgrade._recorded_install_origin", lambda _package: origin)


def test_the_companion_comes_from_the_release_core_was_installed_from(monkeypatch):
    _installed_from(monkeypatch, PREVIEW_CORE_URL)

    assert release_asset_specs(PREVIEW_VERSION) == (PREVIEW_CORE_URL, PREVIEW_MEMORY_URL)


def test_an_official_prerelease_on_pypi_keeps_its_index_pins(monkeypatch):
    # `publish.yml` accepts official `vX.Y.ZrcN` tags and publishes them to
    # PyPI. Such a build carries a version indistinguishable from a `gh-v*`
    # one, so only its origin can say that `gh-v3.0.16rc1` was never created.
    _installed_from(monkeypatch, None)

    assert release_asset_specs(PREVIEW_VERSION) is None


@pytest.mark.parametrize(
    "spelling",
    ["3.0.16rc1", "3.0.16RC1", "3.0.16-rc-1", "v3.0.16rc1", "3.0.16.rc.1"],
)
def test_one_normalization_matches_the_origin_and_names_the_companion(monkeypatch, spelling):
    # The recorded URL spells the version the way a wheel filename does, so the
    # match against the running version must normalize both the same way.
    _installed_from(monkeypatch, PREVIEW_CORE_URL)

    assert release_asset_specs(spelling) == (PREVIEW_CORE_URL, PREVIEW_MEMORY_URL)


@pytest.mark.parametrize(
    ("origin", "version"),
    [
        # An index install records no origin at all: it is repairable by name.
        (None, PREVIEW_VERSION),
        ("", PREVIEW_VERSION),
        # Somewhere that is not a release asset of this repository.
        ("https://example.com/gh-v3.0.16rc1/avibe_os-3.0.16rc1-py3-none-any.whl", PREVIEW_VERSION),
        ("https://github.com/other/repo/releases/download/gh-v3.0.16rc1/avibe_os-3.0.16rc1-py3-none-any.whl", PREVIEW_VERSION),
        ("file:///tmp/avibe_os-3.0.16rc1-py3-none-any.whl", PREVIEW_VERSION),
        ("https://github.com/avibe-bot/avibe/releases/download/avibe_os-3.0.16rc1-py3-none-any.whl", PREVIEW_VERSION),
        (
            "https://github.com/avibe-bot/avibe/releases/download/gh-v3.0.16rc1/nested/avibe_os-3.0.16rc1-py3-none-any.whl",
            PREVIEW_VERSION,
        ),
        (
            "https://github.com/avibe-bot/avibe/releases/download/../gh-v3.0.16rc1/avibe_os-3.0.16rc1-py3-none-any.whl",
            PREVIEW_VERSION,
        ),
        # The recorded asset names a different distribution or version than the
        # one being repaired, so it cannot say where this pair lives.
        (PREVIEW_MEMORY_URL, PREVIEW_VERSION),
        (PREVIEW_CORE_URL, "3.0.16rc2"),
        (PREVIEW_CORE_URL, "3.0.16"),
        (PREVIEW_CORE_URL, ""),
        (PREVIEW_CORE_URL, "not-a-version"),
    ],
)
def test_an_origin_that_cannot_name_this_pair_keeps_index_pins(monkeypatch, origin, version):
    _installed_from(monkeypatch, origin)

    assert release_asset_specs(version) is None


def test_a_missing_or_unreadable_origin_record_reads_as_an_index_install(monkeypatch):
    class _Distribution:
        def __init__(self, recorded):
            self._recorded = recorded

        def read_text(self, _name):
            return self._recorded

    recorded_values = [None, "", "not json", "[]", "{}", '{"url": 7}']
    for recorded in recorded_values:
        monkeypatch.setattr(
            "importlib.metadata.distribution",
            lambda _name, recorded=recorded: _Distribution(recorded),
        )
        assert vibe_upgrade._recorded_install_origin("avibe-os") is None, recorded

    def _raise(_name):
        raise RuntimeError("metadata is unreadable")

    monkeypatch.setattr("importlib.metadata.distribution", _raise)
    assert vibe_upgrade._recorded_install_origin("avibe-os") is None


def test_a_recorded_origin_is_read_from_the_installers_own_pep_610_record(monkeypatch):
    class _Distribution:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"url": PREVIEW_CORE_URL, "archive_info": {}})

    monkeypatch.setattr("importlib.metadata.distribution", lambda _name: _Distribution())

    assert vibe_upgrade._recorded_install_origin("avibe-os") == PREVIEW_CORE_URL


def test_an_exact_plan_installs_the_named_sources_instead_of_index_pins(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    shared = {
        "base_env": {"PATH": "/usr/bin"},
        "version": PREVIEW_VERSION,
        "package_name": "avibe-os",
        "memory_package": True,
        "memory_version": PREVIEW_VERSION,
        "core_spec": PREVIEW_CORE_URL,
        "memory_spec": PREVIEW_MEMORY_URL,
    }
    uv_plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        **shared,
    )
    assert uv_plan.command == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        PREVIEW_CORE_URL,
        "--with",
        f"avibe-memory @ {PREVIEW_MEMORY_URL}",
        "--force",
    ]
    pip_plan = build_upgrade_plan(python_executable="/usr/bin/python3", uv_path=None, **shared)
    assert pip_plan.command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        PREVIEW_CORE_URL,
        f"avibe-memory @ {PREVIEW_MEMORY_URL}",
    ]

    # Every command an installer runs has to reach the release. A pin left in
    # any one of them resolves against an index that never served this version,
    # which is the failure the sources exist to remove.
    for plan in (uv_plan, pip_plan):
        commands = [
            command
            for command in (plan.command, plan.preflight_command, plan.preflight_fallback_command)
            if command
        ]
        assert len(commands) >= 2, "an exact Memory plan resolves before it installs"
        for command in commands:
            assert PREVIEW_CORE_URL in command
            assert f"avibe-memory @ {PREVIEW_MEMORY_URL}" in command
            assert f"avibe-os=={PREVIEW_VERSION}" not in command
            assert f"avibe-memory=={PREVIEW_VERSION}" not in command
        assert "--upgrade" not in plan.command
        assert FORCES_THE_INSTALL[plan.method] in plan.command


def test_a_forward_upgrade_keeps_index_pins_and_admits_no_install_source(monkeypatch):
    # A forward upgrade resolves whichever release is newest, so there is no
    # known release to name. Ignoring a source here would report an install
    # from the release that never happened.
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)
    forward = {
        "python_executable": "/usr/bin/python3",
        "base_env": {"PATH": "/usr/bin"},
        "memory_enabled": True,
        "package_spec": "avibe-os==3.1.0",
    }

    plan = build_upgrade_plan(**forward)
    assert "avibe-os[memory]==3.1.0" in plan.command
    assert "avibe-memory==3.1.0" in plan.command

    for sources in (
        {"core_spec": PREVIEW_CORE_URL},
        {"memory_spec": PREVIEW_MEMORY_URL},
        {"core_spec": PREVIEW_CORE_URL, "memory_spec": PREVIEW_MEMORY_URL},
    ):
        with pytest.raises(ValueError):
            build_upgrade_plan(**forward, **sources)


def _metadata_records(monkeypatch, distribution: str, version: str) -> None:
    """One distribution provides `vibe`, and its metadata records `version`."""

    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: [distribution])
    monkeypatch.setattr("importlib.metadata.version", lambda name: version if name == distribution else "0")


def test_a_forward_upgrade_forces_the_install_once_metadata_stops_describing_the_code(monkeypatch):
    """A forward install cannot trust stale metadata over the running files."""

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    _metadata_records(monkeypatch, "avibe-os", "3.0.11")
    monkeypatch.setattr("vibe.__version__", "3.0.10")

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "pip"
    # Still an upgrade: the version being asked for is whatever the index has
    # newest. Forced as well, because the metadata pip would consult to decide is
    # describing a release that is not what is running.
    assert "--upgrade" in plan.command
    assert "--force-reinstall" in plan.command


def test_a_stale_rename_pair_forces_the_next_install(monkeypatch):
    """Two disagreeing distribution records cannot make an upgrade a no-op."""

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "vibe.upgrade._distributions_providing_this_package",
        lambda: ["avibe-os", "vibe-remote"],
    )
    recorded = {"avibe-os": "3.0.11", "vibe-remote": "3.0.10"}
    monkeypatch.setattr("importlib.metadata.version", lambda name: recorded[name])
    monkeypatch.setattr("vibe.__version__", "3.0.10")

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert "--force-reinstall" in plan.command


def test_a_forward_upgrade_on_an_undamaged_install_is_left_to_the_installer(monkeypatch):
    """Forcing is the exception, and has to stay one.

    `--force-reinstall` makes pip rebuild and rewrite every dependency in the
    tree, so making it unconditional turns each routine upgrade into a much longer
    and much wider write on a machine nobody is watching. The ordinary case is an
    install whose metadata and files agree, and there the installer's own
    already-satisfied judgement is correct and cheaper.
    """

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    _metadata_records(monkeypatch, "avibe-os", "3.0.11")
    monkeypatch.setattr("vibe.__version__", "3.0.11")

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert "--upgrade" in plan.command
    assert "--force-reinstall" not in plan.command


@pytest.mark.parametrize(
    ("case", "distributions", "recorded", "running"),
    [
        # A source checkout or an editable install: nothing published to compare.
        ("no distribution provides the package", [], "3.0.11", "3.0.11"),
        # Mid-rename, or a vendored environment. Both are asked, and here both
        # agree with the code, so there is nothing to force -- the count of
        # distributions is not by itself a disagreement.
        ("two distributions provide it and both agree", ["avibe-os", "vibe-remote"], "3.0.11", "3.0.11"),
        # A regression build. Its version describes a tree, not a release, so a
        # disagreement with published metadata is expected rather than evidence.
        ("the running version names no release", ["avibe-os"], "3.0.11", "0.0.0.dev0+abc1234"),
        ("the recorded version names no release", ["avibe-os"], "0.0.0.dev0+abc1234", "3.0.11"),
    ],
)
def test_an_unknown_install_shape_is_never_forced_on_a_guess(monkeypatch, case, distributions, recorded, running):
    """Only a disagreement between two published releases is evidence.

    Every other answer is an environment this measurement cannot read, and the
    honest response to one is to leave the installer alone rather than to force a
    full reinstall on a machine whose shape we guessed at. Stated as the property
    -- unknown means unforced -- so a shape nobody has met yet inherits it.
    """

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: distributions)
    monkeypatch.setattr("importlib.metadata.version", lambda name: recorded)
    monkeypatch.setattr("vibe.__version__", running)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert "--force-reinstall" not in plan.command, case


def test_exact_package_spec_uses_the_explicit_distribution() -> None:
    assert pinned_package_spec("3.0.10", package_name="avibe-os") == "avibe-os==3.0.10"



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


@pytest.mark.parametrize(
    "executable",
    (
        "/home/test/.local/share/uv/tools/avibe-os/bin/python",
        "/home/test/.avibe/runtime/install-generations/abc/uv/tools/avibe-os/bin/python",
    ),
)
def test_get_current_uv_tool_dir_preserves_the_logical_running_generation(executable):
    assert get_current_uv_tool_dir(executable) == str(Path(executable).parents[2])


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

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "get_version_info", lambda: {"latest": "3.0.15"})
    monkeypatch.setattr(
        api,
        "build_upgrade_plan",
        lambda **kwargs: calls.setdefault("plan_kwargs", kwargs) and plan,
    )
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
    assert calls["run_kwargs"]["timeout"] == 1800
    assert calls["run_kwargs"]["env"] == plan.env
    assert calls["plan_kwargs"]["target_version"] == "3.0.15"
    safe_cwd = calls["run_kwargs"].get("cwd")
    assert safe_cwd and os.path.isabs(safe_cwd), f"subprocess.run cwd must be an absolute path, got {safe_cwd!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 2.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
    }


def test_memory_indep_025_failed_upgrade_is_terminal_and_retryable(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/bin/python3", "-m", "pip", "install", "--upgrade", "avibe-os"],
        env=None,
        method="pip",
    )
    restart = Mock(return_value={"job_id": "retry-restart"})
    install = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                plan.command,
                1,
                stdout="",
                stderr="resolver rejected the release",
            ),
            subprocess.CompletedProcess(
                plan.command,
                0,
                stdout="installed on retry",
                stderr="",
            ),
        ]
    )

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "get_version_info", lambda: {"latest": "3.0.15"})
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "restart_is_pending", lambda: False)
    monkeypatch.setattr(
        api,
        "verify_python_environment",
        lambda _python: SimpleNamespace(ok=True, detail=""),
    )
    monkeypatch.setattr(api, "schedule_restart", restart)
    monkeypatch.setattr(api.subprocess, "run", install)

    failed = api.do_upgrade(auto_restart=True)
    succeeded = api.do_upgrade(auto_restart=True)

    assert failed == {
        "ok": False,
        "message": "Upgrade failed",
        "output": "resolver rejected the release",
        "restarting": False,
    }
    assert succeeded == {
        "ok": True,
        "message": "Upgrade successful. Restarting...",
        "output": "installed on retry",
        "restarting": True,
    }
    assert [call.args[0] for call in install.call_args_list] == [
        plan.command,
        plan.command,
    ]
    restart.assert_called_once()

def test_do_upgrade_blocks_mutation_when_memory_requirement_is_unreadable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "configured_memory_enabled",
        Mock(side_effect=MemoryRequirementUnreadableError()),
    )
    plan = Mock(side_effect=AssertionError("unreadable requirement built a plan"))
    monkeypatch.setattr(api, "build_upgrade_plan", plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")

    result = api.do_upgrade()

    assert result["ok"] is False
    assert result["reason"] == "memory_requirement_unreadable"
    assert result["restarting"] is False
    plan.assert_not_called()


def test_do_upgrade_auto_restart_does_not_block_on_runtime_prepare(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    events: list[str] = []

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(
        cli,
        "build_upgrade_plan",
        lambda **kwargs: calls.setdefault("plan_kwargs", kwargs) and plan,
    )
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
    assert calls["plan_kwargs"]["target_version"] == "2.2.0"
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

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
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

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
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


def test_cmd_upgrade_blocks_mutation_when_memory_requirement_is_unreadable(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(
        cli,
        "configured_memory_enabled",
        Mock(side_effect=MemoryRequirementUnreadableError()),
    )
    plan = Mock(side_effect=AssertionError("unreadable requirement built a plan"))
    monkeypatch.setattr(cli, "build_upgrade_plan", plan)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")

    assert cli.cmd_upgrade() == 1
    assert "persisted Memory requirement" in capsys.readouterr().out
    plan.assert_not_called()


@pytest.mark.parametrize("source", ["local", "direct"])
def test_cmd_upgrade_metadata_failure_uses_exact_memory_artifact(monkeypatch, tmp_path, capsys, source):
    artifact = tmp_path / "avibe_os-3.1.0-py3-none-any.whl"
    package_spec = (
        str(artifact)
        if source == "local"
        else "avibe-os @ https://example.test/releases/avibe_os-3.1.0-py3-none-any.whl"
    )
    calls: dict[str, Any] = {}

    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", package_spec)
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: True)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: {"job_id": "restart"})
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    def execute(plan, **kwargs):
        calls["plan"] = plan
        return subprocess.CompletedProcess(plan.command, 0, stdout="done", stderr="")

    monkeypatch.setattr(cli, "execute_upgrade_plan", execute)

    assert cli.cmd_upgrade() == 0
    assert "Attempting upgrade anyway..." in capsys.readouterr().out
    assert "avibe-memory==3.1.0" in calls["plan"].command


def test_cmd_upgrade_metadata_failure_refuses_unversioned_memory_source(monkeypatch, capsys):
    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "avibe-os")
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: True)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(
        cli,
        "execute_upgrade_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("upgrade must not execute")),
    )

    assert cli.cmd_upgrade() == 1
    output = capsys.readouterr().out
    assert "Attempting upgrade anyway..." not in output
    assert "Upgrade failed" in output


def test_cmd_upgrade_metadata_failure_keeps_core_only_fallback(monkeypatch, capsys):
    calls: dict[str, Any] = {}

    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "avibe-os")
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: {"job_id": "restart"})
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    def execute(plan, **kwargs):
        calls["plan"] = plan
        return subprocess.CompletedProcess(plan.command, 0, stdout="done", stderr="")

    monkeypatch.setattr(cli, "execute_upgrade_plan", execute)

    assert cli.cmd_upgrade() == 0
    assert "Attempting upgrade anyway..." in capsys.readouterr().out
    assert calls["plan"].command[-1] == "avibe-os"


def test_memory_enabled_forward_plan_locks_memory_to_target_release(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)
    monkeypatch.setattr("vibe.upgrade.installed_metadata_describes_running_code", lambda: True)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        memory_package=True,
        memory_version="3.0.14",
        target_version="3.1.0",
        package_spec="avibe-os>=3.1,<3.2",
    )

    target = "avibe-os[memory]<3.2,>=3.1"
    memory = "avibe-memory==3.1.0"
    assert plan.command == ["/usr/bin/python3", "-m", "pip", "install", "--upgrade", target, memory]
    assert plan.preflight_command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--upgrade",
        target,
        memory,
    ]
    assert plan.preflight_fallback_command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "download",
        "--dest",
        "{avibe-pip-download-destination}",
        target,
        memory,
    ]
    assert "<3.1" not in " ".join(plan.command + plan.preflight_command)


def test_memory_enabled_forward_plan_requires_target_release(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError, match="target release version"):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            memory_package=True,
            package_spec="avibe-os",
        )


@pytest.mark.parametrize(
    ("package_spec", "target_version"),
    [
        ("avibe-os==3.1.0", "3.1.0"),
        (
            "avibe-os @ https://example.test/releases/avibe_os-3.1.1-py3-none-any.whl",
            "3.1.1",
        ),
        ("/fixtures/avibe_os-3.1.2.tar.gz", "3.1.2"),
    ],
)
def test_memory_enabled_forward_plan_derives_target_from_versioned_spec(
    monkeypatch, package_spec, target_version
):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        package_spec=package_spec,
    )

    memory_pin = f"avibe-memory=={target_version}"
    assert memory_pin in plan.command
    assert plan.preflight_command is not None and memory_pin in plan.preflight_command
    assert plan.preflight_fallback_command is not None and memory_pin in plan.preflight_fallback_command


@pytest.mark.parametrize(
    "package_spec",
    [
        "/fixtures/avibe_os-3.2.0-py3-none-any.whl",
        "avibe-os @ https://example.test/releases/avibe_os-3.2.0-py3-none-any.whl",
        "avibe-os==3.2.0",
    ],
)
def test_exact_package_spec_overrides_conflicting_memory_target(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        target_version="3.1.0",
        package_spec=package_spec,
    )

    for command in (plan.command, plan.preflight_command, plan.preflight_fallback_command):
        assert command is not None
        assert "avibe-memory==3.2.0" in command
        assert "avibe-memory==3.1.0" not in command


def test_uv_exact_artifact_overrides_conflicting_memory_target(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.is_uv_tool_install", lambda executable: True)
    monkeypatch.setattr("vibe.upgrade.is_legacy_uv_tool_install", lambda executable: False)
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: "/usr/bin/uv")
    monkeypatch.setattr("vibe.upgrade.get_current_vibe_bin_dir", lambda vibe_path: None)

    plan = build_upgrade_plan(
        python_executable="/tools/avibe/bin/python",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        target_version="3.1.0",
        package_spec="/fixtures/avibe_os-3.2.0-py3-none-any.whl",
    )

    assert "avibe-memory==3.2.0" in plan.command
    assert "avibe-memory==3.1.0" not in plan.command
    assert plan.preflight_command is not None
    assert "avibe-memory==3.2.0" in plan.preflight_command
    assert "avibe-memory==3.1.0" not in plan.preflight_command


@pytest.mark.parametrize(
    "package_spec",
    [
        "avibe-os",
        "avibe-os>=3.1,<3.2",
        "avibe-os==3.1.*",
    ],
)
def test_named_package_spec_accepts_compatible_memory_target(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        target_version="3.1.4",
        package_spec=package_spec,
    )

    assert "avibe-memory==3.1.4" in plan.command


def test_named_package_spec_rejects_incompatible_memory_target(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError, match="target release version"):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            target_version="3.2.0",
            package_spec="avibe-os>=3.1,<3.2",
        )


@pytest.mark.parametrize(
    "package_spec",
    [
        "avibe-os",
        "avibe-os>=3.1,<3.2",
        "avibe-os==3.1.*",
        "git+https://example.test/avibe.git@v3.1.0",
        "avibe-os @ git+https://example.test/avibe.git@v3.1.0",
        "/fixtures/avibe.whl",
    ],
)
def test_memory_enabled_forward_plan_rejects_unversioned_fallback_specs(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError, match="target release version"):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            package_spec=package_spec,
        )


@pytest.mark.parametrize(
    "package_spec",
    [
        "git+https://example.test/avibe.git@v3.1.0",
        "avibe-os @ git+https://example.test/avibe.git@v3.1.0",
        "https://user:secret@example.test/download",
        "avibe-os @ https://user:secret@example.test/download",
        "/fixtures/avibe.whl",
    ],
)
def test_opaque_memory_source_rejects_metadata_without_exposing_spec(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError) as refusal:
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            target_version="3.1.0",
            package_spec=package_spec,
        )

    assert str(refusal.value) == "A Memory-preserving upgrade requires a target release version"


def test_memory_preflight_failure_does_not_mutate_package(monkeypatch):
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=[
            "python",
            "-m",
            "pip",
            "download",
            "--dest",
            "{avibe-pip-download-destination}",
            "avibe-os[memory]",
        ],
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing memory")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 1
    assert calls == [plan.preflight_command]


def test_legacy_pip_fallback_resolves_target_extra_before_install(tmp_path):
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=[
            "python",
            "-m",
            "pip",
            "download",
            "--dest",
            "{avibe-pip-download-destination}",
            "avibe-os[memory]",
        ],
    )
    calls: list[list[str]] = []
    scratch: Path | None = None

    def run(command, **kwargs):
        nonlocal scratch
        calls.append(command)
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="no such option: --dry-run")
        if "download" in command:
            scratch = Path(command[command.index("--dest") + 1])
            assert scratch.is_dir()
            assert "--no-deps" not in command
            return subprocess.CompletedProcess(command, 0, stdout="resolved", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 0
    assert [command[3] for command in calls] == ["install", "download", "install"]
    assert scratch is not None and not scratch.exists()


def test_legacy_pip_fallback_failure_stops_before_install():
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=[
            "python",
            "-m",
            "pip",
            "download",
            "--dest",
            "{avibe-pip-download-destination}",
            "avibe-os[memory]",
        ],
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="no such option: --dry-run")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing target Memory wheel")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 1
    assert len(calls) == 2
    assert calls[1][3] == "download"
    assert "--no-deps" not in calls[1]
    assert plan.command not in calls


def test_pip_preflight_does_not_fallback_on_resolver_failure():
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=["python", "-m", "pip", "download", "avibe-os[memory]"],
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="No matching distribution found")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 1
    assert calls == [plan.preflight_command]


def test_unreadable_memory_config_blocks_package_plan(monkeypatch):
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: (_ for _ in ()).throw(ValueError("bad config")))

    with pytest.raises(MemoryRequirementUnreadableError):
        configured_memory_enabled()


def test_missing_first_run_config_does_not_block_package_plan(monkeypatch):
    monkeypatch.setattr(
        "config.v2_config.V2Config.load",
        lambda: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert configured_memory_enabled() is False


def test_uv_forward_plan_locks_memory_to_target_release(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.is_uv_tool_install", lambda executable: True)
    monkeypatch.setattr("vibe.upgrade.is_legacy_uv_tool_install", lambda executable: False)
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: "/usr/bin/uv")
    monkeypatch.setattr("vibe.upgrade.get_current_vibe_bin_dir", lambda vibe_path: None)

    plan = build_upgrade_plan(
        python_executable="/tools/avibe/bin/python",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        memory_package=True,
        target_version="3.1.0",
        package_spec="avibe-os>=3.1,<3.2",
    )

    target = "avibe-os[memory]<3.2,>=3.1"
    memory = "avibe-memory==3.1.0"
    assert plan.command == [
        "/usr/bin/uv",
        "tool",
        "install",
        target,
        "--with",
        memory,
        "--upgrade",
        "--force",
    ]
    assert plan.preflight_command == [
        "/usr/bin/uv",
        "pip",
        "install",
        "--dry-run",
        "--python",
        "/tools/avibe/bin/python",
        "--upgrade",
        target,
        memory,
    ]


def test_explicit_memory_install_keeps_exact_package_version(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        version="3.0.14",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.14",
    )

    assert "avibe-os==3.0.14" in plan.command
    assert "avibe-os[memory]" not in " ".join(plan.command)
    assert "avibe-memory==3.0.14" in plan.command
    assert plan.preflight_command is not None
    assert "avibe-memory==3.0.14" in plan.preflight_command


@pytest.mark.parametrize("launcher", (None, "/tmp/uv/tools/avibe-os/bin/vibe"))
def test_exact_uv_repair_without_a_stable_launcher_fails_before_mutation(monkeypatch, launcher):
    monkeypatch.setattr(vibe_upgrade, "find_uv_binary", lambda **_: "/usr/bin/uv")
    plan = build_upgrade_plan(
        python_executable="/tmp/uv/tools/avibe-os/bin/python",
        vibe_path=launcher, version="3.0.14", memory_package=True,
        memory_version="3.0.14", base_env={"PATH": "/usr/bin"},
    )
    assert plan.activation is None
    assert plan.preflight_error
    run = Mock()
    with pytest.raises(ValueError, match="stable vibe launcher"):
        execute_upgrade_plan(plan, run=run)
    run.assert_not_called()


def test_with_memory_extra_preserves_vcs_url_and_local_specs(tmp_path, monkeypatch):
    from vibe.upgrade import _with_memory_extra

    assert _with_memory_extra("git+https://example.test/avibe.git@abc123#subdirectory=src") == (
        "avibe-os[memory] @ git+https://example.test/avibe.git@abc123#subdirectory=src"
    )
    assert _with_memory_extra("https://example.test/avibe.whl") == "avibe-os[memory] @ https://example.test/avibe.whl"
    assert _with_memory_extra("file:///tmp/avibe.whl") == "avibe-os[memory] @ file:///tmp/avibe.whl"
    assert _with_memory_extra("/fixtures/avibe.whl") == "avibe-os[memory] @ file:///fixtures/avibe.whl"

    monkeypatch.chdir(tmp_path)
    relative_artifact = Path("dist/avibe.whl")
    assert _with_memory_extra(str(relative_artifact)) == (
        f"avibe-os[memory] @ {(tmp_path / relative_artifact).resolve().as_uri()}"
    )


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
