from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import psutil
import pytest

from vibe import install_generations as retention
from vibe import upgrade

REAL_RUNNING_PATHS = retention._running_paths


def _isolate_collector(monkeypatch, cwd: Path, pid: int = 99990):
    from types import SimpleNamespace

    collector = SimpleNamespace(
        pid=pid,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(sys.executable), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(cwd),
    )
    real_process = retention.psutil.Process
    monkeypatch.setattr(retention.os, "getpid", lambda: pid)
    monkeypatch.setattr(
        retention.psutil,
        "Process",
        lambda candidate=None: collector if candidate == pid else real_process(candidate),
    )
    return collector, real_process


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "home with 空格" / "runtime" / "install-generations"
    launcher = tmp_path / "stable" / "vibe"
    launcher.parent.mkdir()
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _: upgrade.IntegrityResult(True, 1))
    monkeypatch.setattr(retention, "_running_paths", lambda: set())
    return root, launcher


def _candidate(root, name):
    path = root / name / "bin" / "vibe"
    path.parent.mkdir(parents=True)
    path.write_text(f"#!/bin/sh\n# {path}\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _activate(root, launcher, name):
    candidate = _candidate(root, name)
    source = upgrade._launcher_generation(launcher, root)
    upgrade.activate_installer_candidate(upgrade.AtomicActivation(launcher, candidate, source))
    return candidate


def _owned(root):
    return {path.name for path in root.iterdir() if (path / retention.RECEIPT).exists()}


@pytest.mark.parametrize("link", ["symlink", "hardlink", "copy"])
def test_repeated_activation_keeps_current_and_previous_for_all_launcher_shapes(installation, monkeypatch, link):
    root, launcher = installation
    if link != "symlink":
        def replace(path, target):
            if link == "hardlink":
                os.link(target, path)
            else:
                shutil.copy2(target, path)
        monkeypatch.setattr(upgrade, "_prepare_launcher_replacement", replace)
    for index in range(8):
        name = f"1725900000-1234-{index}" if index % 2 else f"{index:032x}"
        prior = upgrade._launcher_generation(launcher, root)
        candidate = _activate(root, launcher, name)
        assert len(_owned(root)) == min(index + 1, 2)
        assert upgrade._launcher_generation(launcher, root) == candidate.parent.parent
        if prior:
            assert prior.is_dir()


def test_identical_copy_fallback_launchers_remain_bounded(installation, monkeypatch):
    root, launcher = installation

    def replace(path, target):
        shutil.copy2(target, path)

    monkeypatch.setattr(upgrade, "_prepare_launcher_replacement", replace)
    for index in range(8):
        candidate = _candidate(root, f"copy-{index}")
        candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        candidate.chmod(0o755)
        activation = upgrade.AtomicActivation(
            launcher,
            candidate,
            upgrade._launcher_generation(launcher, root),
        )
        upgrade.activate_installer_candidate(activation)
        assert len(_owned(root)) == min(index + 1, 2)


def test_unowned_history_and_unpublished_candidates_are_never_collected(installation):
    root, launcher = installation
    historical = [_candidate(root, name) for name in ("1693000000-123-456", "a" * 32)]
    staging = root / "incomplete-staging"
    staging.mkdir()
    for index in range(4):
        _activate(root, launcher, f"owned-{index}")
    assert len(_owned(root)) == 2
    assert all(path.exists() for path in historical)
    assert staging.exists()
    assert not any((path.parent.parent / retention.RECEIPT).exists() for path in historical)


def test_every_recorded_stable_launcher_protects_its_target(installation, tmp_path):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    alias = tmp_path / "alternative" / "vibe"
    upgrade.activate_launcher_target(alias, first)
    for index in range(4):
        _activate(root, launcher, f"next-{index}")
    assert alias.resolve() == first
    assert first.exists()
    assert _owned(root) == {"first", "next-2", "next-3"}
    alias.unlink()
    _activate(root, launcher, "last")
    assert not first.exists()


def test_unowned_generation_is_not_adopted_by_alias_activation(installation, tmp_path, monkeypatch):
    root, launcher = installation
    _activate(root, launcher, "first")
    original_write = retention.write_atomic

    def fail(*args, **kwargs):
        raise PermissionError("test-owned receipt denial")

    monkeypatch.setattr(retention, "write_atomic", fail)
    unowned = _activate(root, launcher, "receipt-failed")
    monkeypatch.setattr(retention, "write_atomic", original_write)

    alias = tmp_path / "alternative" / "vibe"
    upgrade.activate_launcher_target(alias, unowned)
    assert not (unowned.parent.parent / retention.RECEIPT).exists()

    for index in range(4):
        _activate(root, launcher, f"next-{index}")
    alias.unlink()
    _activate(root, launcher, "after-alias")

    assert unowned.exists()
    assert not (unowned.parent.parent / retention.RECEIPT).exists()


@pytest.mark.parametrize("link", ["symlink", "hardlink", "copy"])
def test_existing_alias_receipt_is_reserved_before_launcher_publication(
    installation, tmp_path, monkeypatch, link,
):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    alias = tmp_path / "alternative" / "vibe"
    receipt = first.parent.parent / retention.RECEIPT
    original_prepare = upgrade._prepare_launcher_replacement
    observed = []

    def prepare(replacement, target):
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        observed.append(str(alias) in payload["launchers"])
        assert not alias.exists()
        if link == "symlink":
            original_prepare(replacement, target)
        elif link == "hardlink":
            os.link(target, replacement)
        else:
            shutil.copy2(target, replacement)

    monkeypatch.setattr(upgrade, "_prepare_launcher_replacement", prepare)
    upgrade.activate_launcher_target(alias, first)
    assert observed == [True]
    if link == "symlink":
        assert alias.resolve() == first
    else:
        assert alias.read_bytes() == first.read_bytes()


@pytest.mark.parametrize("entrypoint", ["upgrade", "launcher-target"])
def test_existing_alias_receipt_failure_leaves_launcher_and_receipt_unchanged(
    installation, tmp_path, monkeypatch, entrypoint,
):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    alias = tmp_path / entrypoint / "vibe"
    receipt = first.parent.parent / retention.RECEIPT
    before = receipt.read_bytes()

    def deny_receipt_write(*args, **kwargs):
        raise PermissionError("read-only receipt and generation directory")

    monkeypatch.setattr(retention, "write_atomic", deny_receipt_write)
    with pytest.raises(PermissionError, match="read-only"):
        if entrypoint == "upgrade":
            upgrade.activate_upgrade_candidate(
                upgrade.AtomicActivation(alias, first, first.parent.parent),
            )
        else:
            upgrade.activate_launcher_target(alias, first)

    assert not alias.exists()
    assert receipt.read_bytes() == before
    assert first.exists()
    assert upgrade._launcher_generation(launcher, root) == first.parent.parent


@pytest.mark.parametrize("marker", ["missing", "stale", "points-to-other-owned"])
def test_copied_launcher_is_retained_even_when_generation_marker_is_wrong(
    installation, tmp_path, marker,
):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    alias = tmp_path / "other" / "vibe"
    upgrade.activate_launcher_target(alias, first)
    alias.unlink()
    shutil.copy2(first, alias)
    marker_path = alias.parent / ".vibe.avibe-generation"
    if marker == "missing":
        marker_path.unlink()
    elif marker == "stale":
        marker_path.write_text(str(root / "absent"))
    else:
        other = _activate(root, launcher, "other")
        marker_path.write_text(str(other.parent.parent))
    for index in range(4):
        _activate(root, launcher, f"next-{index}")
    assert first.exists()
    assert _owned(root) == {"first", "next-2", "next-3"}


@pytest.mark.parametrize("reference", ["service", "ui", "source-handoff"])
def test_running_logical_interpreter_and_source_handoff_survive(installation, monkeypatch, reference):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    logical_python = first.parent.parent / "uv" / "tools" / "avibe-os" / "bin" / "python"
    logical_python.parent.mkdir(parents=True)
    logical_python.symlink_to(sys.executable)
    kept_path = logical_python if reference != "source-handoff" else first.parent.parent
    monkeypatch.setattr(retention, "_running_paths", lambda: {kept_path})
    for index in range(4):
        _activate(root, launcher, f"next-{index}")
    assert _owned(root) == {"first", "next-2", "next-3"}
    monkeypatch.setattr(retention, "_running_paths", lambda: set())
    _activate(root, launcher, "last")
    assert not first.exists()


def test_process_scan_reads_real_logical_argv_and_handoff_arguments(tmp_path, monkeypatch):
    python = tmp_path / "logical-env" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    source = tmp_path / "source"
    process = subprocess.Popen(
        [str(python), "-c", "import sys; print('ready', flush=True); sys.stdin.read()", "--source-generation", str(source)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        _isolate_collector(monkeypatch, tmp_path)
        monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([psutil.Process(process.pid)]))
        paths = retention._running_paths()
        assert python in paths
        assert source in paths
    finally:
        process.communicate(timeout=10)


def test_installation_owner_process_is_scanned_for_another_user_updater(
    installation, tmp_path, monkeypatch,
):
    from types import SimpleNamespace
    from config import paths

    root, launcher = installation
    first = _activate(root, launcher, "first")
    python = first.parent.parent / "uv" / "tools" / "avibe-os" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    service_pid = 24679
    worker_pid = 24680
    service = SimpleNamespace(
        pid=service_pid,
        uids=lambda: SimpleNamespace(real=1001),
        username=lambda: "installation-owner",
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(python), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(root),
    )
    worker = SimpleNamespace(
        pid=worker_pid,
        uids=lambda: SimpleNamespace(real=1001),
        username=lambda: "installation-owner",
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(python), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(root),
    )
    pid_path = tmp_path / "service.pid"
    pid_path.write_text(str(service_pid))
    monkeypatch.setattr(paths, "get_runtime_pid_path", lambda: pid_path)
    monkeypatch.setattr(paths, "get_runtime_ui_pid_path", lambda: tmp_path / "missing-ui.pid")
    monkeypatch.setattr(retention, "_filesystem_owner", lambda _: retention._OwnerIdentity(uid=1000))
    collector, real_process = _isolate_collector(monkeypatch, root, pid=99991)
    monkeypatch.setattr(
        retention.psutil,
        "Process",
        lambda pid=None: (
            service
            if pid == service_pid
            else collector
            if pid == collector.pid
            else real_process(pid)
        ),
    )
    monkeypatch.setattr(retention, "_running_paths", REAL_RUNNING_PATHS)
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([worker]))

    for index in range(3):
        _activate(root, launcher, f"next-{index}")

    assert first.exists()


def test_process_owner_visibility_denial_defers_collection(installation, monkeypatch):
    from types import SimpleNamespace

    root, launcher = installation
    first = _activate(root, launcher, "first")
    _isolate_collector(monkeypatch, root)

    def denied():
        raise psutil.AccessDenied(24681)

    worker = SimpleNamespace(
        pid=24681,
        uids=denied,
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(first), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(root),
    )
    monkeypatch.setattr(retention, "_running_paths", REAL_RUNNING_PATHS)
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([worker]))

    candidate = _activate(root, launcher, "next")

    assert candidate.exists()
    assert first.exists()


def test_process_exit_during_owner_scan_is_ignored(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: tmp_path / "missing-root")
    _isolate_collector(monkeypatch, tmp_path)

    def exited():
        raise psutil.NoSuchProcess(24682)

    worker = SimpleNamespace(pid=24682, uids=exited)
    idle = SimpleNamespace(pid=0, uids=lambda: (_ for _ in ()).throw(AssertionError("PID 0 inspected")))
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([idle, worker]))

    assert retention._running_paths()


def test_current_collector_bare_interpreter_does_not_defer(monkeypatch, tmp_path):
    from types import SimpleNamespace

    pid = os.getpid()
    process = SimpleNamespace(
        pid=pid,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: ["python", "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(tmp_path),
    )
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: tmp_path / "missing-root")
    monkeypatch.setattr(retention.psutil, "Process", lambda _: process)
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([process]))

    assert retention._running_paths()


def test_windows_process_exit_during_owner_inspection_is_ignored(monkeypatch):
    from types import SimpleNamespace

    process = SimpleNamespace(pid=24683, is_running=lambda: False)
    monkeypatch.setattr(retention.os, "name", "nt")
    monkeypatch.setattr(
        retention,
        "_windows_process_owner",
        lambda _: (_ for _ in ()).throw(OSError(87, "process exited")),
    )

    with pytest.raises(psutil.NoSuchProcess):
        retention._process_owner(process)


def test_windows_live_invalid_parameter_owner_error_stays_fail_closed(monkeypatch):
    from types import SimpleNamespace

    process = SimpleNamespace(pid=24688, is_running=lambda: True, username=lambda: None)
    error = OSError(87, "invalid parameter")
    monkeypatch.setattr(retention.os, "name", "nt")
    monkeypatch.setattr(
        retention,
        "_windows_process_owner",
        lambda _: (_ for _ in ()).throw(error),
    )

    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._process_owner(process)


def test_windows_owner_probe_falls_back_to_process_username(monkeypatch):
    from types import SimpleNamespace

    process = SimpleNamespace(
        pid=24684,
        is_running=lambda: True,
        username=lambda: "installation-owner",
    )
    monkeypatch.setattr(retention.os, "name", "nt")
    monkeypatch.setattr(
        retention,
        "_windows_process_owner",
        lambda _: (_ for _ in ()).throw(OSError(5, "access denied")),
    )

    assert retention._process_owner(process) == retention._OwnerIdentity(
        name="installation-owner",
    )


def test_incomparable_windows_owner_identity_defers_collection():
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._owners_match_any(
            retention._OwnerIdentity(name="installation-owner"),
            [retention._OwnerIdentity(sid="S-1-5-18")],
        )


def test_process_exit_during_executable_scan_is_ignored(monkeypatch, tmp_path):
    from types import SimpleNamespace

    collector_pid = 99998
    collector = SimpleNamespace(
        pid=collector_pid,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(sys.executable), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(tmp_path),
    )
    worker = SimpleNamespace(
        pid=24685,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(sys.executable), "-c", "pass"],
        exe=lambda: (_ for _ in ()).throw(psutil.NoSuchProcess(24685)),
        cwd=lambda: str(tmp_path),
    )
    real_process = retention.psutil.Process
    monkeypatch.setattr(retention.os, "getpid", lambda: collector_pid)
    monkeypatch.setattr(
        retention.psutil,
        "Process",
        lambda pid=None: collector if pid == collector_pid else real_process(pid),
    )
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([collector, worker]))

    assert retention._running_paths()


def test_verifiable_linux_kernel_thread_is_ignored(monkeypatch, tmp_path):
    from types import SimpleNamespace

    collector_pid = 99997
    kernel_pid = 24686
    collector = SimpleNamespace(
        pid=collector_pid,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(sys.executable), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(tmp_path),
    )
    kernel_thread = SimpleNamespace(
        pid=kernel_pid,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [],
        exe=lambda: (_ for _ in ()).throw(
            AssertionError("kernel thread executable must not be inspected")
        ),
        cwd=lambda: (_ for _ in ()).throw(
            AssertionError("kernel thread cwd must not be inspected")
        ),
    )
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == Path("/proc") / str(kernel_pid) / "status":
            return "Name:\tkworker\nKthread:\t1\n"
        return real_read_text(path, *args, **kwargs)

    real_process = retention.psutil.Process
    monkeypatch.setattr(retention.os, "getpid", lambda: collector_pid)
    monkeypatch.setattr(retention.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        retention.psutil,
        "Process",
        lambda pid=None: collector if pid == collector_pid else real_process(pid),
    )
    monkeypatch.setattr(
        retention,
        "_filesystem_owner",
        lambda _: retention._OwnerIdentity(uid=os.getuid()),
    )
    monkeypatch.setattr(
        retention.psutil,
        "process_iter",
        lambda: iter([collector, kernel_thread]),
    )

    assert retention._running_paths()


def test_empty_userspace_command_line_still_defers_collection(monkeypatch):
    from types import SimpleNamespace

    process = SimpleNamespace(pid=24687, cmdline=lambda: [])
    monkeypatch.setattr(retention, "_is_verifiable_kernel_thread", lambda _: False)

    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._process_arguments(process)


@pytest.mark.parametrize(
    ("value", "index", "previous"),
    [("python", 0, None), ("old", 2, "--source-generation")],
)
def test_relative_interpreter_and_source_arguments_are_visible(tmp_path, value, index, previous):
    assert retention._relative_path_argument(
        value,
        index=index,
        previous=previous,
        cwd=tmp_path,
    ) == Path(value)


def test_only_the_current_bare_interpreter_is_ignored(tmp_path):
    assert retention._relative_path_argument(
        "python",
        index=0,
        previous=None,
        cwd=tmp_path,
        ignore_bare_interpreter=True,
    ) is None
    assert retention._relative_path_argument(
        "python",
        index=0,
        previous=None,
        cwd=tmp_path,
    ) == Path("python")
    assert retention._relative_path_argument(
        "bin/python",
        index=0,
        previous=None,
        cwd=tmp_path,
        ignore_bare_interpreter=True,
    ) == Path("bin/python")


def test_inline_path_options_are_visible_as_relative_references(tmp_path):
    assert retention._relative_path_argument(
        "--source-generation=old",
        index=1,
        previous="python",
        cwd=tmp_path,
    ) == Path("old")
    assert retention._relative_path_argument(
        "old/bin/vibe",
        index=2,
        previous="python",
        cwd=tmp_path,
    ) == Path("old/bin/vibe")


def test_relative_process_path_with_changed_cwd_defers_collection(installation, tmp_path, monkeypatch):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    generation = first.parent.parent
    python = generation / "bin" / "python"
    python.symlink_to(sys.executable)
    changed_cwd = tmp_path / "changed-cwd"
    changed_cwd.mkdir()
    child_env = os.environ.copy()
    child_env["PATH"] = f"{generation / 'bin'}{os.pathsep}{child_env.get('PATH', '')}"
    process = subprocess.Popen(
        [
            "python",
            "-c",
            "import os, sys; os.chdir(sys.argv[1]); print('ready', flush=True); sys.stdin.read()",
            str(changed_cwd),
        ],
        cwd=generation,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    _isolate_collector(monkeypatch, changed_cwd)
    monkeypatch.setattr(retention, "_running_paths", REAL_RUNNING_PATHS)
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([psutil.Process(process.pid)]))
    try:
        assert process.stdout.readline().strip() == "ready"
        for index in range(3):
            _activate(root, launcher, f"relative-{index}")
        assert first.exists()
    finally:
        process.communicate(timeout=10)
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([]))
    _activate(root, launcher, "after-relative")
    assert not first.exists()


def test_real_process_keeps_old_environment_until_it_exits(installation, monkeypatch):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    python = first.parent.parent / "uv" / "tools" / "avibe-os" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    process = subprocess.Popen(
        [str(python), "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    _isolate_collector(monkeypatch, root)
    monkeypatch.setattr(retention, "_running_paths", REAL_RUNNING_PATHS)
    try:
        assert process.stdout.readline().strip() == "ready"
        child = psutil.Process(process.pid)
        monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([child]))
        for index in range(4):
            _activate(root, launcher, f"next-{index}")
        assert first.exists()
        assert len(_owned(root)) == 3
    finally:
        process.communicate(timeout=10)
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([]))
    _activate(root, launcher, "last")
    assert not first.exists()
    assert len(_owned(root)) == 2


def test_process_scan_uses_existing_command_fallback_for_macos_denial(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from vibe import runtime

    python = tmp_path / "generation" / "bin" / "python"
    _isolate_collector(monkeypatch, tmp_path)

    def denied():
        raise psutil.AccessDenied(123)
    fake = SimpleNamespace(
        pid=123, username=lambda: psutil.Process().username(),
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING, cmdline=denied, exe=lambda: sys.executable,
        cwd=lambda: str(tmp_path),
    )
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([fake]))
    monkeypatch.setattr(runtime, "get_process_command", lambda _: f'"{python}" -c pass')
    assert python in retention._running_paths()
    ambiguous_python = tmp_path / "python space" / "bin" / "python"
    ambiguous_python.parent.mkdir(parents=True)
    ambiguous_python.symlink_to(sys.executable)
    prefix = tmp_path / "python"
    prefix.write_text("not the interpreter", encoding="utf-8")
    prefix.chmod(0o755)
    monkeypatch.setattr(runtime, "get_process_command", lambda _: f"{ambiguous_python} -c pass")
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._running_paths()
    spaced_python = tmp_path / "generation with space" / "bin" / "python"
    spaced_python.parent.mkdir(parents=True)
    spaced_python.symlink_to(sys.executable)
    monkeypatch.setattr(runtime, "get_process_command", lambda _: f"{spaced_python} -c pass")
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._running_paths()
    monkeypatch.setattr(runtime, "get_process_command", lambda _: '"bin/python" -c pass')
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._running_paths()
    monkeypatch.setattr(runtime, "get_process_command", lambda _: None)
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._running_paths()
    fake.exe = denied
    monkeypatch.setattr(runtime, "get_process_command", lambda _: f'"{python}" -c pass')
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._running_paths()


def test_process_command_fallback_requires_quoted_path_options(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from vibe import runtime

    python = tmp_path / "generation" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    source = tmp_path / "source generation"
    source.mkdir()

    def denied():
        raise psutil.AccessDenied(124)

    fake = SimpleNamespace(
        pid=124,
        username=lambda: psutil.Process().username(),
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=denied,
        exe=lambda: sys.executable,
        cwd=lambda: str(tmp_path),
    )
    collector_pid = 99999
    collector = SimpleNamespace(
        pid=collector_pid,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: [str(sys.executable), "-c", "pass"],
        exe=lambda: sys.executable,
        cwd=lambda: str(tmp_path),
    )
    real_process = retention.psutil.Process
    monkeypatch.setattr(retention.os, "getpid", lambda: collector_pid)
    monkeypatch.setattr(
        retention.psutil,
        "Process",
        lambda pid=None: collector if pid == collector_pid else real_process(pid),
    )
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter([fake, collector]))

    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda _: f'"{python}" --source-generation {source}',
    )
    with pytest.raises(retention._ProcessInspectionUnavailable):
        retention._running_paths()

    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda _: f'"{python}" --source-generation "{source}"',
    )
    assert source in retention._running_paths()


def test_recorded_service_and_ui_are_checked_even_for_a_different_user(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from config import paths

    live_python = tmp_path / "generation" / "bin" / "python"
    _isolate_collector(monkeypatch, tmp_path)
    records = (paths.get_runtime_pid_path(), paths.get_runtime_ui_pid_path())
    for index, record in enumerate(records):
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(str(123 + index))
    processes = [
        SimpleNamespace(
            pid=pid, username=lambda: "another-user", status=lambda: psutil.STATUS_RUNNING,
            cmdline=lambda: [str(live_python), "-c", "pass"], exe=lambda: sys.executable,
            cwd=lambda: str(tmp_path),
        )
        for pid in (123, 124)
    ]
    monkeypatch.setattr(retention.psutil, "process_iter", lambda: iter(processes))
    assert live_python in retention._running_paths()


@pytest.mark.parametrize("payload", [
    {"state": "scheduled"}, {"state": "running"}, [], None, "malformed",
    {"state": "future-handoff"}, {"state": "unknown"}, {"state": None}, {},
])
def test_pending_or_unreadable_restart_defers_collection(installation, payload):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    _activate(root, launcher, "second")
    status = upgrade.runtime_mod.get_restart_status_path()
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps(payload) if payload != "malformed" else "{", encoding="utf-8")
    candidate = _candidate(root, "third")
    activation = upgrade.AtomicActivation(launcher, candidate, root / "second")
    with upgrade.atomic_upgrade_lock():
        assert retention.collect_before_activation(activation) == []
    assert first.exists()
    status.write_text(json.dumps({"state": "succeeded"}))
    upgrade.activate_installer_candidate(activation)
    assert not first.exists()


def test_concurrent_installer_protects_staging_and_older_source_until_it_finishes(installation):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    staging = _candidate(root, "concurrent")
    marker = staging.parent.parent / retention.INSTALLER_PID
    marker.write_text(str(os.getpid()))
    for index in range(3):
        _activate(root, launcher, f"next-{index}")
    assert first.exists()
    assert staging.exists()
    marker.unlink()
    _activate(root, launcher, "last")
    assert not first.exists()
    assert staging.exists()  # unpublished candidate still belongs to its caller
    assert len(_owned(root)) == 2


def test_own_installer_marker_does_not_disable_collection(installation):
    root, launcher = installation
    _activate(root, launcher, "first")
    _activate(root, launcher, "second")
    candidate = _candidate(root, "third")
    (candidate.parent.parent / retention.INSTALLER_PID).write_text(str(os.getpid()))
    upgrade.activate_installer_candidate(upgrade.AtomicActivation(launcher, candidate, root / "second"))
    assert _owned(root) == {"second", "third"}


def test_stale_installer_pid_does_not_pin_owned_history(installation, monkeypatch):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    staging = _candidate(root, "abandoned")
    (staging.parent.parent / retention.INSTALLER_PID).write_text("123")
    def absent(_):
        raise psutil.NoSuchProcess(123)
    monkeypatch.setattr(retention.psutil, "Process", absent)
    for index in range(3):
        _activate(root, launcher, f"next-{index}")
    assert not first.exists()
    assert staging.exists()  # a crashed, unreceipted candidate is not owned


def test_reused_installer_pid_does_not_own_the_marker(installation, monkeypatch):
    from types import SimpleNamespace

    root, _ = installation
    candidate = _candidate(root, "abandoned")
    marker = candidate.parent.parent / retention.INSTALLER_PID
    marker.write_text("123")
    monkeypatch.setattr(retention.psutil, "Process", lambda _: SimpleNamespace(
        status=lambda: psutil.STATUS_RUNNING,
        create_time=lambda: marker.stat().st_mtime + 10,
    ))
    assert not retention._installer_is_live(candidate.parent.parent)


def test_failed_candidate_is_discarded_without_touching_selected_generations(installation, monkeypatch):
    from vibe import cli

    root, launcher = installation
    first = _activate(root, launcher, "first")
    candidate = _candidate(root, "failed")
    monkeypatch.setattr(upgrade, "verify_upgrade_candidate", lambda _: upgrade.IntegrityResult(False))
    assert cli._dispatch_installer_activation([
        "--launcher", str(launcher), "--candidate", str(candidate),
        "--source-generation", str(first.parent.parent),
    ]) == 1
    assert first.exists()
    assert not candidate.parent.parent.exists()
    assert launcher.resolve() == first


def test_collection_does_not_follow_generation_symlinks_or_touch_other_state(installation, tmp_path):
    root, launcher = installation
    _activate(root, launcher, "first")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / retention.RECEIPT).write_text(json.dumps({"version": 1, "launchers": [str(launcher)]}))
    sentinel = outside / "state"
    sentinel.write_text("keep")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    for index in range(3):
        _activate(root, launcher, f"next-{index}")
    assert sentinel.read_text() == "keep"
    assert (root / "alias").is_symlink()


def test_malformed_receipt_defers_collection_but_allows_activation(installation):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    (first.parent.parent / retention.RECEIPT).write_text('{"version": 9}')
    for index in range(3):
        _activate(root, launcher, f"next-{index}")
    assert first.exists()
    assert launcher.resolve() == root / "next-2" / "bin" / "vibe"


def test_canonical_uv_install_does_not_protect_unreferenced_owned_generations(installation, tmp_path, monkeypatch):
    root, launcher = installation
    _activate(root, launcher, "first")
    canonical = tmp_path / ".local" / "share" / "uv" / "tools" / "avibe-os" / "bin" / "vibe"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("#!/canonical/python\n")
    canonical.chmod(0o755)
    upgrade.activate_launcher_target(launcher, canonical)
    monkeypatch.setattr(retention, "_running_paths", lambda: {canonical.with_name("python")})
    for index in range(3):
        _activate(root, launcher, f"next-{index}")
    assert _owned(root) == {"next-1", "next-2"}
    assert canonical.exists()


@pytest.mark.parametrize("failure", ["enumerate", "processes", "remove", "receipt"])
def test_cleanup_failure_never_invalidates_successful_activation(installation, monkeypatch, failure):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    _activate(root, launcher, "second")
    def fail(*args, **kwargs):
        raise PermissionError("test-owned denial")
    if failure == "enumerate":
        original = Path.iterdir
        monkeypatch.setattr(Path, "iterdir", lambda path: fail() if path == root else original(path))
    elif failure == "processes":
        monkeypatch.setattr(retention, "_running_paths", fail)
    elif failure == "remove":
        monkeypatch.setattr(retention.shutil, "rmtree", fail)
    else:
        monkeypatch.setattr(retention, "write_atomic", fail)
    candidate = _activate(root, launcher, "third")
    assert launcher.resolve() == candidate
    assert candidate.exists()
    if failure != "receipt":
        assert first.exists()


def test_partial_cleanup_failure_restores_ownership_receipt(installation, monkeypatch):
    root, launcher = installation
    first = _activate(root, launcher, "first")
    _activate(root, launcher, "second")
    original_rmtree = retention.shutil.rmtree

    def partial_failure(path):
        if path == first.parent.parent:
            (path / retention.RECEIPT).unlink()
            raise PermissionError("test-owned partial cleanup failure")
        original_rmtree(path)

    monkeypatch.setattr(retention.shutil, "rmtree", partial_failure)
    _activate(root, launcher, "third")

    assert (first.parent.parent / retention.RECEIPT).exists()


def test_failed_receipt_stays_unowned_while_later_owned_generations_are_bounded(
    installation, monkeypatch, caplog,
):
    root, launcher = installation
    _activate(root, launcher, "first")
    original_write = retention.write_atomic

    def fail(*args, **kwargs):
        raise PermissionError("test-owned receipt denial")

    monkeypatch.setattr(retention, "write_atomic", fail)
    unowned = _activate(root, launcher, "receipt-failed")
    assert launcher.resolve() == unowned
    assert "unowned generation will be retained" in caplog.text
    assert not (unowned.parent.parent / retention.RECEIPT).exists()

    monkeypatch.setattr(retention, "write_atomic", original_write)
    for index in range(4):
        current = _activate(root, launcher, f"next-{index}")
        assert current.exists()
        assert unowned.exists()
        assert not (unowned.parent.parent / retention.RECEIPT).exists()
    assert _owned(root) == {"next-2", "next-3"}
    assert {generation.name for generation in root.iterdir()} == {"receipt-failed", "next-2", "next-3"}


@pytest.mark.parametrize("surface", ["manual", "automatic"])
def test_repeated_real_upgrade_callers_are_bounded(installation, monkeypatch, surface):
    from vibe import api, cli

    root, launcher = installation
    caller = cli if surface == "manual" else api
    monkeypatch.setattr(caller, "_runtime_process_was_running", lambda: surface == "automatic")
    monkeypatch.setattr(caller, "schedule_restart", lambda **kwargs: {"job_id": "test"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: str(launcher))
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: str(launcher))
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "99"})
    monkeypatch.setattr(
        api,
        "get_version_info",
        lambda: {"error": None, "has_update": True, "latest": "99"},
    )
    monkeypatch.setattr(cli, "_prepare_show_runtime_after_install", lambda *_: None)
    for index in range(5):
        candidate = _candidate(root, f"{index:032x}")
        activation = upgrade.AtomicActivation(launcher, candidate, upgrade._launcher_generation(launcher, root))
        plan = upgrade.UpgradePlan(command=["test-uv"], env={}, method="uv", activation=activation)
        monkeypatch.setattr(caller, "build_upgrade_plan", lambda **kwargs: plan)
        monkeypatch.setattr(caller.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "", ""))
        result = cli.cmd_upgrade() if surface == "manual" else api.do_upgrade(auto_restart=True)
        assert result == 0 if surface == "manual" else result["ok"]
        assert len(_owned(root)) == min(index + 1, 2)
        assert candidate.exists()
