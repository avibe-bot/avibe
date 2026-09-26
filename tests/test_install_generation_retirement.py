"""Recovery-oriented retirement; arbitrary stale processes are not pinned."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import psutil
import pytest

from config import paths
from vibe import install_generations as retention
from vibe import upgrade
from vibe import runtime

OFFICIAL_PATHS = retention._runtime_paths
INSTALLER_IS_LIVE = retention._installer_is_live


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "home with 空格" / "runtime" / "install-generations"
    launcher = tmp_path / "stable" / "vibe"
    launcher.parent.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(launcher.parent), os.environ.get("PATH", "")]))
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _: upgrade.IntegrityResult(True, 1))
    monkeypatch.setattr(retention, "_runtime_paths", lambda: set(), raising=False)
    monkeypatch.setattr(retention, "_invocation_paths", lambda: set(), raising=False)
    monkeypatch.setattr(retention, "_installer_is_live", lambda _: False)
    return root, launcher


def candidate(
    root, name, *, layout="uv/tools", package="avibe-os",
    export_name="vibe", entry_name="vibe",
):
    generation = root / name
    environment = generation / layout / package
    target = environment / "bin" / "vibe"
    target.parent.mkdir(parents=True)
    target.write_text(f"#!/bin/sh\n# {generation}\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    (environment / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    exported = generation / "bin" / export_name
    exported.parent.mkdir()
    exported.symlink_to(target)
    (environment / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "{package}" }}]\n'
        f'entrypoints = [{{ name = {json.dumps(entry_name)}, install-path = {json.dumps(str(exported))} }}]\n',
        encoding="utf-8",
    )
    return exported


def activate(root, launcher, name, **kwargs):
    exported = candidate(root, name, **kwargs)
    upgrade.activate_installer_candidate(upgrade.AtomicActivation(
        launcher, exported, upgrade._launcher_generation(launcher, root),
    ))
    return exported


def collect(launcher):
    return retention.collect_install_generations(launcher)


@pytest.mark.parametrize("link", ["symlink", "hardlink", "copy"])
@pytest.mark.parametrize("layout", ["tools", "uv/tools"])
def test_repeated_installations_leave_only_selected_generation(
    installation, monkeypatch, link, layout,
):
    root, launcher = installation
    if link != "symlink":
        monkeypatch.setattr(
            upgrade, "_prepare_launcher_replacement",
            lambda path, target: os.link(target.resolve(), path) if link == "hardlink" else shutil.copy2(target, path),
        )
    for index in range(6):
        name = f"1600000000-999999-{index}" if index % 2 else f"{index:032x}"
        current = activate(root, launcher, name, layout=layout)
        assert current.is_file()
        assert {path.name for path in root.iterdir()} == {name}
        assert upgrade._launcher_generation(launcher, root) == current.parent.parent


@pytest.mark.parametrize("layout", ["tools", "uv/tools"])
@pytest.mark.parametrize("entry_name,export_name", [
    ("vibe", "vibe"), ("vibe", "vibe.exe"), ("vibe.exe", "vibe.exe"),
])
def test_uv_logical_entrypoint_and_platform_export_converge(
    installation, layout, entry_name, export_name,
):
    """uv strips EXE_SUFFIX from receipt names, not from Windows export paths.

    These are portable artifact/activation cases, not native Windows execution.
    """
    root, launcher = installation
    for index in range(3):
        exported = candidate(
            root, f"generation-{index}", layout=layout,
            entry_name=entry_name, export_name=export_name,
        )
        generation = exported.parent.parent
        assert retention._uv_installation(generation) is not None
        upgrade.activate_installer_candidate(upgrade.AtomicActivation(
            launcher, exported, upgrade._launcher_generation(launcher, root),
        ))
        assert {path.name for path in root.iterdir()} == {generation.name}
        assert launcher.resolve() == exported.resolve()


@pytest.mark.parametrize("entry_name,export_name", [
    ("other", "vibe.exe"), ("vibe", "other.exe"),
    ("vibe", "vibe.exe.exe"), ("vibe.exe", "vibe"),
])
def test_receipt_export_normalization_is_not_general_suffix_matching(
    installation, entry_name, export_name,
):
    root, launcher = installation
    unknown = candidate(root, "unknown", entry_name=entry_name, export_name=export_name)
    assert retention._uv_installation(unknown.parent.parent) is None
    activate(root, launcher, "selected")
    assert unknown.exists()


@pytest.mark.parametrize("foreign_kind", ["symlink", "script"])
def test_foreign_path_command_does_not_block_managed_retirement(
    installation, monkeypatch, tmp_path, foreign_kind,
):
    root, launcher = installation
    foreign = tmp_path / "foreign-bin" / "vibe"
    foreign.parent.mkdir()
    foreign_contents = "#!/bin/sh\n# unrelated installation outside the managed root\nexit 0\n"
    if foreign_kind == "symlink":
        target = tmp_path / "foreign-target"
        target.write_text(foreign_contents)
        target.chmod(0o755)
        foreign.symlink_to(target)
    else:
        foreign.write_text(foreign_contents)
        foreign.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(foreign.parent), os.environ["PATH"]]))
    assert shutil.which("vibe") == str(foreign)

    for index in range(3):
        current = activate(root, launcher, f"generation-{index}")

    assert {path.name for path in root.iterdir()} == {"generation-2"}
    assert launcher.resolve() == current.resolve()
    assert foreign.read_text() == foreign_contents
    assert subprocess.run([str(launcher)], check=False).returncode == 0


@pytest.mark.parametrize("link", ["symlink", "hardlink", "copy"])
def test_unrecognized_in_root_selected_generation_cannot_authorize_retirement(
    installation, link,
):
    root, launcher = installation
    old = candidate(root, "old")
    selected = candidate(root, "selected")
    (root / "selected" / "uv" / "tools" / "avibe-os" / "uv-receipt.toml").unlink()
    if link == "symlink":
        launcher.symlink_to(selected)
    elif link == "hardlink":
        os.link(selected.resolve(), launcher)
    else:
        shutil.copy2(selected, launcher)
        upgrade._update_launcher_generation_marker(launcher, selected, root)
    assert collect(launcher) == []
    assert old.exists()
    assert subprocess.run([str(launcher)], check=False).returncode == 0
    # A later recognized activation restores ordinary retirement.
    current = activate(root, launcher, "recovered")
    assert not old.exists()
    assert selected.exists()  # Unknown installations never become garbage by name.
    assert current.exists()


@pytest.mark.parametrize("selector", ["primary", "path"])
def test_in_root_wrapper_preserves_the_selected_command_dependency(
    installation, tmp_path, monkeypatch, selector,
):
    root, launcher = installation
    old = candidate(root, "old")
    wrapper = root / "unknown" / "bin" / "vibe"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(f'#!/bin/sh\nexec "{old}" "$@"\n')
    wrapper.chmod(0o755)
    if selector == "primary":
        selected_command = launcher
    else:
        selected_command = tmp_path / "path-first" / "vibe"
        selected_command.parent.mkdir()
        monkeypatch.setenv("PATH", os.pathsep.join([
            str(selected_command.parent), os.environ["PATH"],
        ]))
    selected_command.symlink_to(wrapper)
    assert subprocess.run([str(selected_command)], check=False).returncode == 0
    if selector == "primary":
        collect(launcher)
    else:
        current = activate(root, launcher, "current")
        assert current.exists()
    assert old.exists()
    assert subprocess.run([str(selected_command)], check=False).returncode == 0


def test_reclaims_fifteen_historical_generations_without_activation_receipts(installation):
    root, launcher = installation
    for index in range(15):
        candidate(
            root, f"{index:032x}", layout="tools" if index % 2 else "uv/tools",
            package="vibe-remote" if index % 3 else "avibe-os",
        )
    unknown = root / "not-an-install"
    unknown.mkdir()
    (unknown / "user-file").write_text("preserve")
    selected = activate(root, launcher, "selected")
    assert selected.is_file()
    assert {path.name for path in root.iterdir()} == {"selected", "not-an-install"}
    assert (unknown / "user-file").read_text() == "preserve"


def test_canonical_uv_history_is_retired_after_a_managed_activation(installation, tmp_path):
    root, launcher = installation
    candidate(root, "old-one", layout="tools")
    candidate(root, "old-two")
    canonical = tmp_path / "canonical-uv" / "bin" / "vibe"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("#!/bin/sh\nexit 0\n")
    canonical.chmod(0o755)
    launcher.symlink_to(canonical)
    collect(launcher)
    assert {path.name for path in root.iterdir()} == {"old-one", "old-two"}
    assert launcher.resolve() == canonical
    assert canonical.is_file()
    current = activate(root, launcher, "selected")
    assert current.exists()
    assert {path.name for path in root.iterdir()} == {"selected"}
    assert canonical.is_file()


def test_official_runtime_and_current_invocation_survive_until_handoff(
    installation, monkeypatch,
):
    root, launcher = installation
    service = candidate(root, "service")
    ui = candidate(root, "ui")
    invocation = candidate(root, "invocation")
    monkeypatch.setattr(retention, "_runtime_paths", lambda: {service, ui})
    monkeypatch.setattr(retention, "_invocation_paths", lambda: {invocation})
    current = activate(root, launcher, "current")
    assert {path.name for path in root.iterdir()} == {"service", "ui", "invocation", "current"}
    monkeypatch.setattr(retention, "_runtime_paths", lambda: {current})
    monkeypatch.setattr(retention, "_invocation_paths", lambda: {current})
    collect(launcher)
    assert {path.name for path in root.iterdir()} == {"current"}


def test_unregistered_old_process_is_retired_and_current_command_still_runs(
    installation, tmp_path, monkeypatch,
):
    root, launcher = installation
    old = candidate(root, "old")
    generation = old.parent.parent
    environment = generation / "uv" / "tools" / "avibe-os"
    module = environment / "retired_feature.py"
    module.write_text("VALUE = 'old'\n")
    python = environment / "bin" / "python"
    python.symlink_to(sys.executable)
    process = subprocess.Popen(
        [str(python), "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); print('ready', flush=True); "
         "sys.stdin.readline(); import retired_feature", str(environment)],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    monkeypatch.setattr(
        retention.psutil, "process_iter",
        lambda *args, **kwargs: pytest.fail("retirement must not enumerate unrelated processes"),
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        current = activate(root, launcher, "current")
        assert not generation.exists()
        assert current.exists()
        assert subprocess.run([str(launcher)], check=False).returncode == 0
    finally:
        _, error = process.communicate("\n", timeout=10)
    assert process.returncode != 0
    assert "ModuleNotFoundError" in error


@pytest.mark.parametrize("state", [None, "future-state", "unknown", "scheduled", "running"])
def test_pending_or_unknown_restart_defers_retirement(installation, state):
    root, launcher = installation
    old = candidate(root, "old")
    current = candidate(root, "current")
    launcher.symlink_to(current)
    path = upgrade.runtime_mod.get_restart_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": state}))
    assert collect(launcher) == []
    assert old.exists()
    path.write_text(json.dumps({"state": "succeeded"}))
    collect(launcher)
    assert not old.exists()
    assert current.exists()


def test_another_installer_protects_its_source_even_before_candidate_is_complete(
    installation, monkeypatch,
):
    root, launcher = installation
    source = candidate(root, "source")
    staging = root / "incomplete-stage"
    staging.mkdir()
    monkeypatch.setattr(retention, "_installer_is_live", lambda path: path == staging)
    selected = activate(root, launcher, "selected")
    assert source.exists()
    assert staging.exists()
    monkeypatch.setattr(retention, "_installer_is_live", lambda _: False)
    collect(launcher)
    assert not source.exists()
    assert selected.exists()
    assert staging.exists()  # Unknown/incomplete directories are not adopted.


def test_cleanup_failure_cannot_invalidate_successful_activation(installation, monkeypatch):
    root, launcher = installation
    old = candidate(root, "old")
    (root / "old" / "uv" / "tools" / "avibe-os" / "lib").mkdir()
    monkeypatch.setattr(
        retention.shutil, "rmtree",
        lambda _: (_ for _ in ()).throw(PermissionError("test-owned deletion failure")),
    )
    selected = activate(root, launcher, "selected")
    assert old.exists()
    assert launcher.resolve() == selected.resolve()
    assert selected.exists()


def test_generation_symlinks_and_external_uv_data_are_never_collected(installation, tmp_path):
    root, launcher = installation
    external = candidate(tmp_path / "external", "unrelated")
    root.mkdir(parents=True)
    alias = root / "directory-alias"
    alias.symlink_to(external.parent.parent, target_is_directory=True)
    selected = activate(root, launcher, "selected")
    assert alias.is_symlink()
    assert external.exists()
    assert selected.exists()


def test_failed_candidate_is_discarded_but_selected_install_is_unchanged(
    installation, monkeypatch,
):
    from vibe import cli

    root, launcher = installation
    selected = activate(root, launcher, "selected")
    failed = candidate(root, "failed")
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _: upgrade.IntegrityResult(False))
    assert cli._dispatch_installer_activation([
        "--launcher", str(launcher), "--candidate", str(failed),
        "--source-generation", str(selected.parent.parent),
    ]) == 1
    assert launcher.resolve() == selected.resolve()
    assert selected.exists()
    assert not failed.parent.parent.exists()


@pytest.mark.parametrize("surface", ["manual", "automatic"])
def test_repeated_real_upgrade_callers_retire_history(installation, monkeypatch, surface):
    from vibe import api, cli

    root, launcher = installation
    caller = cli if surface == "manual" else api
    monkeypatch.setattr(caller, "_runtime_process_was_running", lambda: surface == "automatic")
    monkeypatch.setattr(caller, "schedule_restart", lambda **kwargs: {"job_id": "test"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: str(launcher))
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: str(launcher))
    version = {"error": None, "has_update": True, "latest": "99"}
    monkeypatch.setattr(cli, "get_latest_version", lambda: version)
    monkeypatch.setattr(api, "get_version_info", lambda: version)
    monkeypatch.setattr(cli, "_prepare_show_runtime_after_install", lambda *_: None)
    for index in range(5):
        exported = candidate(root, f"{index:032x}")
        activation = upgrade.AtomicActivation(launcher, exported, upgrade._launcher_generation(launcher, root))
        plan = upgrade.UpgradePlan(command=["test-uv"], env={}, method="uv", activation=activation)
        monkeypatch.setattr(caller, "build_upgrade_plan", lambda **kwargs: plan)
        monkeypatch.setattr(caller.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "", ""))
        result = cli.cmd_upgrade() if surface == "manual" else api.do_upgrade(auto_restart=True)
        assert result == 0 if surface == "manual" else result["ok"]
        assert {path.name for path in root.iterdir()} == {f"{index:032x}"}
        assert exported.exists()


@pytest.mark.parametrize("record", ["service-pid", "ui-pid", "status", "lock-holder"])
def test_real_official_child_is_kept_until_exit(installation, monkeypatch, tmp_path, record):
    root, launcher = installation
    exported = candidate(root, "official")
    python = exported.parent.parent / "uv" / "tools" / "avibe-os" / "bin" / "python"
    python.symlink_to(sys.executable)
    process = subprocess.Popen(
        [str(python), "-c", "import sys; print('ready', flush=True); sys.stdin.readline()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=tmp_path, text=True,
    )
    monkeypatch.setattr(retention, "_runtime_paths", OFFICIAL_PATHS)
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (True, None))
    try:
        assert process.stdout.readline().strip() == "ready"
        paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
        if record == "status":
            paths.get_runtime_status_path().write_text(json.dumps({"service_pid": process.pid}))
        elif record == "lock-holder":
            monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (False, process.pid))
        else:
            path = paths.get_runtime_pid_path() if record == "service-pid" else paths.get_runtime_ui_pid_path()
            path.write_text(str(process.pid))
        assert python in OFFICIAL_PATHS()
        current = activate(root, launcher, "selected")
        assert exported.exists()
        assert current.exists()
    finally:
        process.communicate("\n", timeout=10)
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (True, None))
    collect(launcher)
    assert not exported.parent.parent.exists()
    assert current.exists()


@pytest.mark.parametrize("boundary", ["cmdline", "exe", "lock-owner", "status", "pid"])
def test_unreadable_official_reference_defers_without_invalidating_activation(
    installation, monkeypatch, boundary,
):
    root, launcher = installation
    old = candidate(root, "official")
    monkeypatch.setattr(retention, "_runtime_paths", OFFICIAL_PATHS)
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (True, None))
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    if boundary == "lock-owner":
        monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (False, None))
    elif boundary == "status":
        paths.get_runtime_status_path().write_text("{")
    elif boundary == "pid":
        paths.get_runtime_pid_path().write_text("unknown")
    else:
        paths.get_runtime_pid_path().write_text("123")

        def denied():
            raise psutil.AccessDenied(123)

        process = SimpleNamespace(
            status=lambda: psutil.STATUS_RUNNING, cmdline=lambda: [str(old)],
            exe=lambda: str(old),
        )
        setattr(process, boundary, denied)
        monkeypatch.setattr(retention.psutil, "Process", lambda _: process)
    current = activate(root, launcher, "selected")
    assert old.exists()
    assert launcher.resolve() == current.resolve()


def test_windows_official_base_python_child_keeps_redirector_generation(installation, monkeypatch):
    root, launcher = installation
    old = candidate(root, "official-ui")
    monkeypatch.setattr(retention, "_runtime_paths", OFFICIAL_PATHS)
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (True, None))
    monkeypatch.setattr(retention, "sys", SimpleNamespace(platform="win32"))
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    paths.get_runtime_ui_pid_path().write_text("123")
    monkeypatch.setattr(retention.psutil, "Process", lambda _: SimpleNamespace(
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [sys.executable, "-c", "from vibe.ui_server import run; run()"],
        exe=lambda: sys.executable,
        parent=lambda: SimpleNamespace(exe=lambda: str(old.with_name("python.exe"))),
    ))
    current = activate(root, launcher, "selected")
    assert old.exists()
    assert current.exists()


@pytest.mark.parametrize("parent_state", ["missing", "exited", "denied"])
@pytest.mark.parametrize("child_state", ["live", "exited", "in-root", "zombie"])
def test_windows_redirector_loss_is_not_the_official_child_exiting(
    installation, monkeypatch, parent_state, child_state,
):
    root, launcher = installation
    old = candidate(root, "official-ui")
    monkeypatch.setattr(retention, "_runtime_paths", OFFICIAL_PATHS)
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda: (True, None))
    monkeypatch.setattr(retention, "sys", SimpleNamespace(platform="win32"))
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    paths.get_runtime_ui_pid_path().write_text("123")
    inspections = []

    def parent():
        inspections.append("parent")
        if parent_state == "missing":
            return None

        def executable():
            if parent_state == "denied":
                raise psutil.AccessDenied(122)
            raise psutil.NoSuchProcess(122)

        return SimpleNamespace(exe=executable)

    child = SimpleNamespace(
        status=lambda: psutil.STATUS_ZOMBIE if child_state == "zombie" else psutil.STATUS_RUNNING,
        cmdline=lambda: [
            str(old.with_name("python.exe")) if child_state == "in-root" else sys.executable,
            "-c", "from vibe.ui_server import run; run()",
        ],
        exe=lambda: sys.executable,
        parent=parent,
        is_running=lambda: child_state != "exited",
    )
    monkeypatch.setattr(retention.psutil, "Process", lambda _: child)
    current = activate(root, launcher, "selected")
    must_keep = child_state in {"live", "in-root"} or (
        child_state == "exited" and parent_state == "denied"
    )
    assert old.exists() is must_keep
    assert current.exists()
    if child_state in {"in-root", "zombie"}:
        assert not inspections


@pytest.mark.parametrize("interruption", ["write-failure", "interrupted-write", "rename-failure"])
def test_shell_marker_publication_failure_cannot_poison_future_collection(
    installation, monkeypatch, interruption,
):
    root, launcher = installation
    old = candidate(root, "old")
    launcher.symlink_to(old)
    source = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    source = source.rsplit('\nmain "$@"', 1)[0]
    hooks = {
        "write-failure": r"""
printf() {
    if [ "$1" = '%s\n' ] && [ "${2:-}" = "$$" ]; then return 1; fi
    builtin printf "$@"
}
""",
        "interrupted-write": r"""
printf() {
    if [ "$1" = '%s\n' ] && [ "${2:-}" = "$$" ]; then exit 91; fi
    builtin printf "$@"
}
""",
        "rename-failure": r"""
mv() {
    case "${*: -1}" in */.avibe-installing) return 1 ;; esac
    command mv "$@"
}
""",
    }
    # A publication failure must precede source snapshot and package installation.
    script = source + hooks[interruption] + r"""
VIBE_TOOL_BIN_DIR="$VIBE_TEST_STABLE_BIN"
launcher_destination_is_available() { return 0; }
resolve_binary_path() { echo snapshot >> "$VIBE_TEST_ADMISSION_TRACE"; exit 92; }
uv() { echo uv >> "$VIBE_TEST_ADMISSION_TRACE"; return 93; }
if uv_tool_install avibe-os; then exit 94; else exit 95; fi
"""
    trace = root.parent / "admission-trace"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ, "HOME": str(root.parent.parent),
            "AVIBE_HOME": str(root.parent.parent),
            "VIBE_TEST_STABLE_BIN": str(launcher.parent),
            "VIBE_TEST_ADMISSION_TRACE": str(trace),
        },
        cwd=root.parent.parent, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode in {91, 95}
    assert not trace.exists()
    staged = [generation for generation in root.iterdir() if generation.name != "old"]
    assert all(not (generation / retention.INSTALLER_PID).exists() for generation in staged)
    if interruption != "interrupted-write":
        assert not staged
    monkeypatch.setattr(retention, "_installer_is_live", INSTALLER_IS_LIVE)
    for index in range(3):
        current = activate(root, launcher, f"selected-{index}")
    assert not old.exists()
    assert current.exists()
    assert {generation.name for generation in root.iterdir()} == {
        "selected-2", *(generation.name for generation in staged),
    }


@pytest.mark.parametrize("contents", ["", "not-a-pid"])
def test_corrupt_published_guid_marker_is_not_guessed_dead(
    installation, monkeypatch, contents,
):
    root, launcher = installation
    old = candidate(root, "old")
    stage = root / "0123456789abcdef0123456789abcdef"
    stage.mkdir()
    (stage / retention.INSTALLER_PID).write_text(contents)
    monkeypatch.setattr(retention, "_installer_is_live", INSTALLER_IS_LIVE)
    current = activate(root, launcher, "selected")
    assert old.exists()
    assert current.exists()
    assert launcher.resolve() == current.resolve()
    assert (stage / retention.INSTALLER_PID).read_text() == contents


@pytest.mark.parametrize("legacy", [False, True])
def test_live_installer_pid_pins_handoff_without_process_table_scan(
    installation, monkeypatch, legacy,
):
    root, launcher = installation
    created = int(psutil.Process(os.getpid()).create_time())
    old = candidate(root, f"{created}-{os.getpid()}-17" if legacy else "staging")
    if not legacy:
        retention.mark_install_generation(old, os.getpid())
    monkeypatch.setattr(retention, "_installer_is_live", INSTALLER_IS_LIVE)
    current = activate(root, launcher, "selected")
    assert old.exists()
    assert current.exists()


@pytest.mark.parametrize("condition", ["dead", "reused", "zombie"])
def test_old_marker_does_not_permanently_pin_retired_generation(installation, monkeypatch, condition):
    root, launcher = installation
    old = candidate(root, "old")
    retention.mark_install_generation(old, 123)
    marker = old.parent.parent / retention.INSTALLER_PID
    monkeypatch.setattr(retention, "_installer_is_live", INSTALLER_IS_LIVE)

    def process(_):
        if condition == "dead":
            raise psutil.NoSuchProcess(123)
        return SimpleNamespace(
            status=lambda: psutil.STATUS_ZOMBIE if condition == "zombie" else psutil.STATUS_RUNNING,
            create_time=lambda: marker.stat().st_mtime + 10,
        )

    monkeypatch.setattr(retention.psutil, "Process", process)
    current = activate(root, launcher, "selected")
    assert not old.parent.parent.exists()
    assert current.exists()


@pytest.mark.parametrize("shape", [
    "malformed-receipt", "wrong-package", "outside-export", "nested-alias",
    "extra-tool", "user-data", "missing-receipt",
])
def test_unknown_installation_shapes_are_retained(installation, tmp_path, shape):
    root, launcher = installation
    old = candidate(root, "unknown")
    generation = old.parent.parent
    environment = generation / "uv" / "tools" / "avibe-os"
    receipt = environment / "uv-receipt.toml"
    if shape == "malformed-receipt":
        receipt.write_text("not a receipt")
    elif shape == "wrong-package":
        receipt.write_text(receipt.read_text().replace("avibe-os", "other-tool"))
    elif shape == "outside-export":
        receipt.write_text(receipt.read_text().replace(json.dumps(str(old)), json.dumps(str(tmp_path / "vibe"))))
    elif shape == "nested-alias":
        moved = tmp_path / "external-environment"
        environment.rename(moved)
        environment.symlink_to(moved, target_is_directory=True)
    elif shape == "extra-tool":
        (environment.parent / "other-tool").mkdir()
    elif shape == "user-data":
        (generation / "user-data").write_text("not ours")
    else:
        receipt.unlink()
    current = activate(root, launcher, "selected")
    assert generation.exists()
    assert current.exists()


@pytest.mark.parametrize("layout,extra", [
    ("uv/tools", "tools/userdata/keep.txt"),
    ("tools", "uv/other/keep.txt"),
    ("tools", "uv/tools/other-pkg/keep.txt"),
    ("uv/tools", "bin/keep.txt"),
])
def test_mixed_layout_or_unexported_bin_data_is_not_an_installation(installation, layout, extra):
    root, launcher = installation
    old = candidate(root, "mixed", layout=layout)
    sentinel = old.parent.parent / extra
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("not installation data")
    current = activate(root, launcher, "selected")
    assert sentinel.read_text() == "not installation data"
    assert old.exists()
    assert current.exists()


@pytest.mark.parametrize("alias_target", ["outside", "inside"])
@pytest.mark.parametrize("explicit", [False, True])
def test_alias_invocation_cannot_retire_the_path_selected_command(
    installation, monkeypatch, tmp_path, alias_target, explicit,
):
    root, launcher = installation
    selected = activate(root, launcher, "selected")
    alias = tmp_path / "secondary" / "vibe"
    alias.parent.mkdir()
    if alias_target == "outside":
        target = tmp_path / "canonical-uv" / "vibe"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
    else:
        target = candidate(root, "other")
    alias.symlink_to(target)
    monkeypatch.setattr(upgrade, "get_running_vibe_path", lambda: str(alias))
    retention.collect_install_generations(alias if explicit else None)
    assert selected.exists()
    assert launcher.resolve() == selected.resolve()
    assert subprocess.run([str(launcher)], check=False).returncode == 0


def test_partial_removal_retains_uv_evidence_and_next_pass_finishes(installation, monkeypatch):
    root, launcher = installation
    old = candidate(root, "old")
    environment = old.parent.parent / "uv" / "tools" / "avibe-os"
    library = environment / "lib"
    library.mkdir()
    (library / "locked.pyd").write_text("loaded")
    original = retention.shutil.rmtree

    def fail_midway(path):
        if path == library:
            raise PermissionError("loaded library")
        original(path)

    monkeypatch.setattr(retention.shutil, "rmtree", fail_midway)
    current = activate(root, launcher, "selected")
    assert (environment / "uv-receipt.toml").is_file()
    assert (environment / "pyvenv.cfg").is_file()
    assert current.exists()
    monkeypatch.setattr(retention.shutil, "rmtree", original)
    collect(launcher)
    assert not environment.exists()
    assert {path.name for path in root.iterdir()} == {"selected"}


def test_unknown_plain_launcher_defers_but_missing_copy_marker_recovers(installation):
    root, launcher = installation
    old = candidate(root, "old")
    launcher.write_text("unknown wrapper")
    collect(launcher)
    assert old.exists()
    selected = candidate(root, "selected")
    shutil.copy2(selected, launcher)
    collect(launcher)
    assert not old.exists()
    assert selected.exists()
    assert launcher.read_bytes() == selected.read_bytes()


def test_hardlink_alias_first_still_protects_real_selected_generation(installation):
    root, launcher = installation
    old = candidate(root, "retire")
    alias = root / "aaa-alias"
    alias.symlink_to(root / "selected", target_is_directory=True)
    selected = candidate(root, "selected")
    os.link(selected.resolve(), launcher)
    assert upgrade._launcher_generation(launcher, root) == root / "selected"
    collect(launcher)
    assert not old.exists()
    assert selected.exists()
    assert alias.is_symlink()


def test_staging_marker_failure_after_publication_is_nonfatal(installation, monkeypatch):
    root, launcher = installation
    exported = candidate(root, "selected")
    retention.mark_install_generation(exported, os.getpid())
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path.name == retention.INSTALLER_PID:
            raise PermissionError("readonly marker")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    upgrade.activate_installer_candidate(upgrade.AtomicActivation(launcher, exported))
    assert launcher.resolve() == exported.resolve()
    assert exported.exists()


def test_installer_diagnostics_restore_logging_without_a_traceback(
    installation, monkeypatch, capsys, caplog,
):
    from vibe import cli

    root, launcher = installation
    exported = candidate(root, "selected")
    handlers = list(retention.logger.handlers)
    level = retention.logger.level

    def denied():
        raise PermissionError("official PID visibility denied")

    monkeypatch.setattr(retention, "_runtime_paths", denied)
    result = cli._dispatch_installer_activation([
        "--launcher", str(launcher), "--candidate", str(exported),
    ])
    diagnostic = capsys.readouterr().err
    assert result == 0
    assert exported.exists()
    assert launcher.resolve() == exported.resolve()
    assert "official PID visibility denied" in diagnostic
    assert "Traceback" not in diagnostic
    assert any(record.exc_info for record in caplog.records)
    assert retention.logger.handlers == handlers
    assert retention.logger.level == level


@pytest.mark.parametrize("state,owner,old_status,marker_job,deferred", [
    ("failed", "live", False, "active", False),
    ("error", "dead", False, "active", False),
    ("cancelled", "dead", False, "active", False),
    ("skipped", "dead", False, "active", False),
    (None, "dead", False, "active", False),
    ("running", "dead", True, "active", False),
    ("scheduled", "seed", True, "active", False),
    ("succeeded", "dead", True, "active", False),
    ("succeeded", "reused", True, "active", False),
    ("succeeded", "legacy", True, "active", False),
    ("succeeded", "live", False, "previous-job", False),
    ("succeeded", "live", True, "active", True),
    ("succeeded", "live", True, None, True),
    ("succeeded", "dead", False, "active", True),
    ("unknown", "dead", True, "active", True),
    ("future-state", "dead", True, "active", True),
    ("unreadable", "dead", True, "active", True),
])
def test_pending_followup_respects_restart_ownership(
    installation, monkeypatch, state, owner, old_status, marker_job, deferred,
):
    """A stale follow-up cannot veto normal activations indefinitely.

    Existence-only coverage missed failed, superseded and abandoned jobs.
    A matching supervisor still owns its successful tail until it consumes the
    follow-up; unknown evidence and the existing seed grace remain conservative.
    """
    from vibe import restart_supervisor

    root, launcher = installation
    old = candidate(root, "old")
    restart_supervisor.mark_pending_restart(
        trigger="web-ui-config-pending", restart_job_id=marker_job,
    )
    marker = paths.get_runtime_dir() / "pending_restart.json"
    marker_bytes = marker.read_bytes()
    status = runtime.get_restart_status_path()
    if state is not None:
        payload = {
            "job_id": "active", "state": state,
            "supervisor_pid": None if owner == "seed" else 123,
        }
        if owner not in {"legacy", "seed"}:
            payload["supervisor_started_at"] = 100.0
        runtime.write_json(status, payload)
        if old_status:
            os.utime(status, (1, 1))
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: owner not in {"dead", "seed"})
    monkeypatch.setattr(runtime, "process_create_time", lambda pid: 200.0 if owner == "reused" else 100.0)
    if state == "unreadable":
        original_read = Path.read_text

        def denied(path, *args, **kwargs):
            if path == status:
                raise PermissionError("restart status unavailable")
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)

    for index in range(3):
        current = activate(root, launcher, f"selected-{index}")
    assert current.exists()
    assert launcher.resolve() == current.resolve()
    assert old.exists() is deferred
    expected = {"old", "selected-0", "selected-1", "selected-2"} if deferred else {"selected-2"}
    assert {path.name for path in root.iterdir()} == expected
    assert marker.read_bytes() == marker_bytes  # Only the supervisor consumes it.


def test_stage_is_marked_before_uv_and_failed_helper_transfer_stays_precommit(
    installation, monkeypatch, tmp_path,
):
    root, launcher = installation
    exported = candidate(root, "candidate")
    activation = upgrade.AtomicActivation(launcher, exported)
    plan = upgrade.UpgradePlan(["uv-fixture"], None, "uv", activation=activation)

    def run(command, **kwargs):
        assert (root / "candidate" / retention.INSTALLER_PID).read_text() == str(os.getpid())
        return subprocess.CompletedProcess(command, 0)

    upgrade.execute_upgrade_plan(plan, run=run)
    terminated = []
    monkeypatch.setattr(upgrade, "_candidate_python", lambda _: Path(sys.executable))
    monkeypatch.setattr(upgrade.runtime_mod, "process_create_time", lambda _: 1)
    monkeypatch.setattr(upgrade, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(upgrade.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        pid=123, terminate=lambda: terminated.append(123),
    ))
    monkeypatch.setattr(retention, "write_atomic", lambda *args: (_ for _ in ()).throw(PermissionError("readonly")))
    with pytest.raises(PermissionError, match="readonly"):
        upgrade.defer_upgrade_activation(activation, parent_pid=os.getpid())
    assert terminated == [123]
    assert not launcher.exists()
    assert exported.exists()


@pytest.mark.parametrize("ready", [True, False])
def test_runtime_start_collects_only_after_readiness(installation, monkeypatch, ready):
    root, launcher = installation
    old = candidate(root, "old")
    current = candidate(root, "current")
    launcher.symlink_to(current)
    monkeypatch.setattr(upgrade, "get_running_vibe_path", lambda: str(launcher))
    monkeypatch.setattr(runtime, "_resolve_service_pid", lambda **kwargs: 123)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda *args, **kwargs: 123 if ready else None)
    monkeypatch.setattr(runtime, "_raise_service_started_but_never_ran",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not ready")))
    if ready:
        assert runtime.start_service() == 123
        assert not old.parent.parent.exists()
    else:
        with pytest.raises(RuntimeError, match="not ready"):
            runtime.start_service()
        assert old.exists()
    assert current.exists()


@pytest.mark.parametrize("layout", ["tools", "uv/tools"])
@pytest.mark.parametrize("copy", [False, True])
def test_real_uv_installations_are_identified_and_retired(installation, tmp_path, monkeypatch, layout, copy):
    """Consume real uv receipts and launchers without network or production roots."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    root, launcher = installation
    wheel = tmp_path / "avibe_os-0.0.0-py3-none-any.whl"
    info = "avibe_os-0.0.0.dist-info"
    files = {
        "vibe/__init__.py": "",
        "vibe/cli.py": "def main():\n    print('isolated current install')\n",
        f"{info}/METADATA": "Metadata-Version: 2.1\nName: avibe-os\nVersion: 0.0.0\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": "[console_scripts]\nvibe = vibe.cli:main\n",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
        archive.writestr(f"{info}/RECORD", "\n".join(f"{name},," for name in files) + f"\n{info}/RECORD,,\n")
    if copy:
        monkeypatch.setattr(upgrade, "_prepare_launcher_replacement", lambda path, target: shutil.copy2(target, path))
    for index in range(3):
        generation = root / f"{index:032x}"
        env = {
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "AVIBE_HOME": str(root.parent.parent),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "UV_TOOL_DIR": str(generation / layout),
            "UV_TOOL_BIN_DIR": str(generation / "bin"),
            "UV_PYTHON_INSTALL_DIR": str(tmp_path / "pythons"),
            "UV_PYTHON_DOWNLOADS": "never",
        }
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        result = subprocess.run(
            [uv, "--offline", "--no-config", "--no-cache", "tool", "install",
             "--no-index", "--python", sys.executable, str(wheel)],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        exported = generation / "bin" / ("vibe.exe" if os.name == "nt" else "vibe")
        assert retention._uv_installation(generation) is not None
        upgrade.activate_installer_candidate(upgrade.AtomicActivation(
            launcher, exported, upgrade._launcher_generation(launcher, root),
        ))
        assert {path.name for path in root.iterdir()} == {generation.name}
        result = subprocess.run([str(launcher)], env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "isolated current install"
