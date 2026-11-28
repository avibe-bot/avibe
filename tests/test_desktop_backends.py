from __future__ import annotations

import json
import hashlib
import io
import stat
import tarfile
import zipfile
from typing import Any
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from config import paths as config_paths
from storage.lock import MigrationFileLock, MigrationLockTimeout
from vibe import desktop_backends
from vibe.runtime import pid_alive


def _desktop_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    node = runtime / "tools" / ("node.exe" if os.name == "nt" else "node")
    npm_cli = runtime / "tools" / "npm" / "bin" / "npm-cli.js"
    node.parent.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    node.write_bytes(b"MZ\0\0" if os.name == "nt" else b"\x7fELF")
    node.chmod(0o755)
    npm_cli.write_text("// npm\n", encoding="utf-8")
    return {
        "AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime),
        "VIBE_SHOW_RUNTIME_NODE_BIN": str(node),
        "AVIBE_DESKTOP_NPM_CLI": str(npm_cli),
        "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"),
        "PATH": str(tmp_path / "external-bin"),
        "NPM_CONFIG_REGISTRY": "https://attacker.invalid/",
        "npm_config_prefix": str(tmp_path / "system-prefix"),
        "NODE_OPTIONS": "--require attacker.js",
    }


def _write_native(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ\0\0" if os.name == "nt" else b"\x7fELF")
    path.chmod(0o755)


def _fake_npm_install(
    backend: str,
    *,
    codex_layout: str = "hoisted",
    calls: list[tuple[list[str], dict[str, str], Path]] | None = None,
):
    spec = desktop_backends.BACKEND_SPECS[backend]

    def fake_run(command, *, cwd, env, timeout_seconds):
        if "--prefix" not in command:
            # The installed executable's ``--version`` probe.
            return subprocess.CompletedProcess(command, 0, f"{backend} 1.2.3", "")
        staging = Path(command[command.index("--prefix") + 1])
        package_dir = staging / "node_modules" / Path(*spec.package_path)
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": spec.package, "version": "1.2.3"}),
            encoding="utf-8",
        )
        if backend == "codex":
            os_name, arch = desktop_backends._native_target()
            target_name = f"codex-{os_name}-{arch}"
            if codex_layout == "nested":
                target_root = package_dir / "node_modules" / "@openai" / target_name
            else:
                target_root = staging / "node_modules" / "@openai" / target_name
            executable = target_root / "vendor" / "target-triple" / "bin" / (
                "codex.exe" if os_name == "win32" else "codex"
            )
        elif backend == "claude":
            os_name, arch = desktop_backends._native_target()
            target_root = staging / "node_modules" / "@anthropic-ai" / f"claude-code-{os_name}-{arch}"
            executable = target_root / ("claude.exe" if os_name == "win32" else "claude")
        else:
            os_name, arch = desktop_backends._native_target()
            target_os = "windows" if os_name == "win32" else os_name
            target_root = staging / "node_modules" / f"opencode-{target_os}-{arch}"
            executable = target_root / "bin" / ("opencode.exe" if os_name == "win32" else "opencode")
        _write_native(executable)
        if backend == "codex":
            runtime_root = executable.parent.parent
            (runtime_root / "codex-package.json").write_text("{}", encoding="utf-8")
            helpers = [
                runtime_root
                / "bin"
                / ("codex-code-mode-host.exe" if os_name == "win32" else "codex-code-mode-host"),
                runtime_root / "codex-path" / ("rg.exe" if os_name == "win32" else "rg"),
            ]
            if os_name == "win32":
                helpers.extend(
                    [
                        runtime_root / "codex-resources" / "codex-command-runner.exe",
                        runtime_root / "codex-resources" / "codex-windows-sandbox-setup.exe",
                    ]
                )
            else:
                helpers.append(runtime_root / "codex-resources" / "zsh" / "bin" / "zsh")
                if os_name == "linux":
                    helpers.append(runtime_root / "codex-resources" / "bwrap")
            for helper in helpers:
                _write_native(helper)
        if calls is not None:
            calls.append((command, dict(env), cwd))
        return subprocess.CompletedProcess(command, 0, "installed", "")

    return fake_run


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_install_publishes_verified_native_backend(monkeypatch, tmp_path, backend):
    env = _desktop_env(tmp_path)
    env["CODEX_HOME"] = str(tmp_path / "custom-codex-home")
    env["OPENAI_API_KEY"] = "fixture-key"
    calls: list[tuple[list[str], dict[str, str], Path]] = []
    monkeypatch.setattr(desktop_backends, "_run_command", _fake_npm_install(backend, calls=calls))
    activated: list[str] = []

    result = desktop_backends.install_desktop_backend(
        backend,
        base_env=env,
        activate=lambda path: activated.append(path),
    )

    assert result.version == "1.2.3"
    assert result.path == activated[0]
    assert desktop_backends.resolve_published_desktop_backend(backend, env) == result.path
    descriptor = json.loads((Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / backend / "current.json").read_text())
    assert descriptor["package"] == desktop_backends.BACKEND_SPECS[backend].package
    assert not Path(descriptor["executable"]).is_absolute()
    assert ".staging-" not in descriptor["executable"]

    command, install_env, cwd = calls[0]
    assert command[:2] == [env["VIBE_SHOW_RUNTIME_NODE_BIN"], env["AVIBE_DESKTOP_NPM_CLI"]]
    assert command[-1] == desktop_backends.BACKEND_SPECS[backend].package
    assert "--ignore-scripts" in command
    assert f"--registry={desktop_backends.NPM_REGISTRY}" in command
    assert cwd == Path(install_env["NPM_CONFIG_PREFIX"])
    assert install_env["NPM_CONFIG_REGISTRY"] == desktop_backends.NPM_REGISTRY
    assert install_env["NPM_CONFIG_PREFIX"] == command[command.index("--prefix") + 1]
    assert install_env["NPM_CONFIG_CACHE"] == str(cwd / ".npm-cache")
    assert install_env["NPM_CONFIG_USERCONFIG"].startswith(install_env["NPM_CONFIG_PREFIX"])
    assert "npm_config_prefix" not in install_env
    assert "NODE_OPTIONS" not in install_env
    assert "OPENAI_API_KEY" not in install_env
    if backend == "codex":
        runtime_env = desktop_backends.desktop_backend_subprocess_environment(
            "codex",
            result.path,
            env,
        )
        assert runtime_env is not None
        assert runtime_env["CODEX_HOME"] == env["CODEX_HOME"]
        assert runtime_env["OPENAI_API_KEY"] == env["OPENAI_API_KEY"]
        expected_helper_dir = Path(result.path).parent.parent / "codex-path"
        assert Path(runtime_env["PATH"].split(os.pathsep)[0]) == expected_helper_dir


def test_codex_accepts_nested_target_package(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    monkeypatch.setattr(
        desktop_backends,
        "_run_command",
        _fake_npm_install("codex", codex_layout="nested"),
    )

    result = desktop_backends.install_desktop_backend("codex", base_env=env)

    assert Path(result.path).name == ("codex.exe" if os.name == "nt" else "codex")


def test_codex_rejects_an_incomplete_runtime_before_publication(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    complete_install = _fake_npm_install("codex")

    def install_without_ripgrep(command, **kwargs):
        completed = complete_install(command, **kwargs)
        staging = Path(command[command.index("--prefix") + 1])
        ripgrep_name = "rg.exe" if os.name == "nt" else "rg"
        next(
            staging.glob(
                f"node_modules/@openai/codex-*/vendor/*/codex-path/{ripgrep_name}"
            )
        ).unlink()
        return completed

    monkeypatch.setattr(desktop_backends, "_run_command", install_without_ripgrep)

    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("codex", base_env=env)

    assert raised.value.code == "incomplete_backend_runtime"
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "codex"
    assert not (backend_root / "current.json").exists()
    assert list(backend_root.glob("releases/*")) == []


def test_failed_install_keeps_current_descriptor_and_removes_staging(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude"
    backend_root.mkdir(parents=True)
    previous = b'{"existing":true}\n'
    (backend_root / "current.json").write_bytes(previous)
    monkeypatch.setattr(
        desktop_backends,
        "_run_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "npm failed"),
    )

    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("claude", base_env=env)

    assert raised.value.code == "npm_install_failed"
    assert (backend_root / "current.json").read_bytes() == previous
    assert list(backend_root.glob(".staging-*")) == []
    assert list(backend_root.glob("releases/*")) == []


def test_descriptor_publication_failure_does_not_activate_config(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    monkeypatch.setattr(desktop_backends, "_run_command", _fake_npm_install("opencode"))
    state = {"path": "opencode"}

    def activate(path):
        state["path"] = path

    monkeypatch.setattr(
        desktop_backends,
        "_write_current_descriptor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(desktop_backends.DesktopBackendError):
        desktop_backends.install_desktop_backend("opencode", base_env=env, activate=activate)

    assert state["path"] == "opencode"
    assert list((Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "opencode" / "releases").iterdir()) == []


def test_descriptor_is_published_before_activation_and_restored_on_failure(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])
    backend_root = root / "claude"
    old_executable = backend_root / "releases" / "old" / "claude"
    _write_native(old_executable)
    old_descriptor = {
        "schema_version": desktop_backends.CURRENT_DESCRIPTOR_SCHEMA_VERSION,
        "backend": "claude",
        "package": "@anthropic-ai/claude-code",
        "version": "1.0.0",
        "executable": old_executable.relative_to(root).as_posix(),
    }
    backend_root.mkdir(parents=True, exist_ok=True)
    (backend_root / "current.json").write_text(json.dumps(old_descriptor), encoding="utf-8")
    monkeypatch.setattr(desktop_backends, "_run_command", _fake_npm_install("claude"))

    def fail_activation(new_path):
        assert desktop_backends.resolve_published_desktop_backend("claude", env) == new_path
        raise OSError("config write failed")

    with pytest.raises(desktop_backends.DesktopBackendError):
        desktop_backends.install_desktop_backend("claude", base_env=env, activate=fail_activation)

    assert desktop_backends.resolve_published_desktop_backend("claude", env) == str(old_executable)
    assert [path.name for path in (backend_root / "releases").iterdir()] == ["old"]


def test_resolver_rejects_descriptor_traversal_and_non_native_file(tmp_path):
    env = _desktop_env(tmp_path)
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "codex"
    backend_root.mkdir(parents=True)
    outside = tmp_path / "outside" / "codex"
    outside.parent.mkdir()
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)

    base = {
        "schema_version": desktop_backends.CURRENT_DESCRIPTOR_SCHEMA_VERSION,
        "backend": "codex",
        "package": "@openai/codex",
        "version": "1.2.3",
    }
    (backend_root / "current.json").write_text(
        json.dumps({**base, "executable": "../outside/codex"}),
        encoding="utf-8",
    )
    assert desktop_backends.resolve_published_desktop_backend("codex", env) is None

    local = backend_root / "releases" / "one" / "codex"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    local.chmod(0o755)
    (backend_root / "current.json").write_text(
        json.dumps(
            {
                **base,
                "executable": local.relative_to(Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    assert desktop_backends.resolve_published_desktop_backend("codex", env) is None


# --- The installer tree is owned by the process that started it (#2131) ---

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Random, so no process another test file started is ever in scope.
_TREE_RUNTIME_ID = secrets.token_hex(32)
# npm-shaped: the leader starts one child in its group and one that leaves it
# (and ignores SIGTERM), then works until stopped. Each child names the test's
# temporary directory, which is how the conftest signal guard knows it as this
# test's own once its leader no longer parents it.
_INSTALLER_TREE_SCRIPT = """
import json, os, subprocess, sys, time
member = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", {pids!r}])
escaped = subprocess.Popen(
    [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)", {pids!r}],
    start_new_session=True,
)
with open({pids!r} + ".tmp", "w") as handle:
    json.dump([os.getpid(), member.pid, escaped.pid], handle)
os.replace({pids!r} + ".tmp", {pids!r})
time.sleep(120)
"""
# `start_ui` launches the UI, the only process that installs, as `<python> -c "<this>..."`.
_UI_LAUNCH = "from vibe.ui_server import run_ui_server; "
_INSTALL_OWNER_SCRIPT = (
    _UI_LAUNCH
    + """import json, sys
from vibe import desktop_backends
desktop_backends.install_desktop_backend("claude", base_env=json.loads(sys.argv[1]))
"""
)


def _installer_env(tmp_path: Path, npm_script: str) -> dict[str, str]:
    env = _desktop_env(tmp_path)
    node = Path(env["VIBE_SHOW_RUNTIME_NODE_BIN"])
    node.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    node.chmod(0o755)
    Path(env["AVIBE_DESKTOP_NPM_CLI"]).write_text(npm_script, encoding="utf-8")
    return env


@pytest.fixture
def owner_runtime_id(monkeypatch):
    # The install owner's Runtime id, which every installer it starts inherits.
    monkeypatch.setenv(desktop_backends.DESKTOP_RUNTIME_ID_ENV, _TREE_RUNTIME_ID)
    return _TREE_RUNTIME_ID


@pytest.fixture
def installer_trees(tmp_path, owner_runtime_id):
    """Makes install environments, each with its own backend root and npm tree."""

    started: list[int] = []

    def make(name: str):
        pids_path = tmp_path / name / "installer-pids.json"

        def wait_for_tree() -> list[int]:
            deadline = time.monotonic() + 15
            while not pids_path.exists():
                assert time.monotonic() < deadline, "installer tree did not start"
                time.sleep(0.05)
            pids = json.loads(pids_path.read_text(encoding="utf-8"))
            started.extend(pids)
            return pids

        return _installer_env(tmp_path / name, _INSTALLER_TREE_SCRIPT.format(pids=str(pids_path))), wait_for_tree

    yield make
    for pid in started:
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture
def installer_tree(installer_trees):
    return installer_trees("tree")


@pytest.fixture
def children():
    """Processes a test starts, killed and reaped when it ends."""

    started: list[subprocess.Popen] = []
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def _running(pids: list[int]) -> list[int]:
    return [pid for pid in pids if pid_alive(pid)]


def _install_records() -> list[Path]:
    records = config_paths.get_runtime_dir() / "desktop-backend-installs"
    return sorted(records.glob("*.json")) if records.exists() else []


def _install_lock_is_free(env: dict[str, str]) -> bool:
    lock = MigrationFileLock(
        Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude" / ".install.lock",
        timeout_seconds=0,
    )
    try:
        lock.acquire()
    except MigrationLockTimeout:
        return False
    lock.release()
    return True


def _stagings(env: dict[str, str]) -> list[Path]:
    return sorted((Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude").glob(".staging-*"))


def _start_owner(children: list[subprocess.Popen], env: dict[str, str], wait_for_tree) -> list[int]:
    """A live stand-in for the UI: it installs with ``env`` and owns the tree that starts."""

    children.append(subprocess.Popen([sys.executable, "-c", _INSTALL_OWNER_SCRIPT, json.dumps(env)], cwd=_REPO_ROOT))
    pids = wait_for_tree()
    assert _running(pids) == pids
    return pids


def _start_ui_stand_in(children: list[subprocess.Popen], tmp_path: Path, env: dict[str, str]) -> int:
    """A process launched the way the UI is, running with ``env``."""

    ready = tmp_path / f"ui-{len(children)}.ready"
    script = _UI_LAUNCH + "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(120)"
    children.append(subprocess.Popen([sys.executable, "-c", script, str(ready)], cwd=_REPO_ROOT, env=env))
    deadline = time.monotonic() + 15
    while not ready.exists():
        assert time.monotonic() < deadline, "UI stand-in did not start"
        time.sleep(0.05)
    return children[-1].pid


def _shift_create_time(monkeypatch, pid: int) -> None:
    """psutil shows ``pid`` started at another time, as macOS can after sleep while it keeps running."""

    from vibe import runtime

    real_create_time = runtime.process_create_time
    monkeypatch.setattr(
        runtime, "process_create_time", lambda other: real_create_time(other) + 1.0 if other == pid else real_create_time(other)
    )


def _start_tree_naming(children: list[subprocess.Popen], env: dict[str, str], wait_for_tree, owner: str | None):
    """An installer tree of this Runtime that names ``owner``, or no owner at all."""

    tree_env = {
        **os.environ,
        desktop_backends.DESKTOP_ROLE_ENV: desktop_backends.DESKTOP_INSTALLER_ROLE,
        desktop_backends.PROCESS_IDENTITY_ENV: desktop_backends.new_process_identity_marker(),
    }
    if owner is not None:
        tree_env[desktop_backends.DESKTOP_INSTALLER_OWNER_ENV] = owner
    npm = Path(env["AVIBE_DESKTOP_NPM_CLI"]).read_text(encoding="utf-8")
    children.append(subprocess.Popen([sys.executable, "-c", npm], env=tree_env))
    return wait_for_tree()


def _start_killed_owner(env: dict[str, str], wait_for_tree) -> list[int]:
    """An owner that dies (SIGKILL) mid-install, before it can drain its tree."""

    owner = subprocess.Popen(
        [sys.executable, "-c", _INSTALL_OWNER_SCRIPT, json.dumps(env)],
        cwd=_REPO_ROOT,
    )
    try:
        pids = wait_for_tree()
    finally:
        owner.kill()
        owner.wait(timeout=10)
    assert _running(pids) == pids
    assert len(_install_records()) == 1
    assert len(_stagings(env)) == 1
    return pids


@pytest.fixture
def quiet_stop(monkeypatch):
    from vibe import cli, remote_access, runtime

    # The full stop's service half scans live processes; nothing of this test
    # runs there, and the real host must not be touched. The UI half reads
    # only this test's UI pidfile.
    monkeypatch.setattr(runtime, "stop_service", lambda **kwargs: False)
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=True: None)
    monkeypatch.setattr(remote_access, "stop", lambda: {"ok": True})
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda *args: False)
    statuses: list[tuple] = []
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: statuses.append(args))
    return cli, statuses


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_ui_shutdown_signal_drains_the_installer_tree_before_returning(monkeypatch, installer_tree):
    import uvicorn

    from vibe import ui_server

    monkeypatch.setattr(desktop_backends, "_INSTALLERS_CLOSED", False)
    env, wait_for_tree = installer_tree
    outcome: dict[str, str] = {}

    def install() -> None:
        try:
            desktop_backends.install_desktop_backend("claude", base_env=env)
        except desktop_backends.DesktopBackendError as exc:
            outcome["code"] = exc.code

    worker = threading.Thread(target=install)
    worker.start()
    pids = wait_for_tree()

    ui_server._create_ui_server(uvicorn.Config(ui_server.app)).handle_exit(signal.SIGTERM, None)

    assert _running(pids) == []
    worker.join(timeout=30)
    assert outcome == {"code": "npm_install_failed"}
    # The install's own cleanup removes its staging, then its record.
    assert _install_records() == []
    assert _install_lock_is_free(env)
    # A drained owner starts no installer that could outlive it.
    with pytest.raises(desktop_backends.DesktopBackendError) as refused:
        desktop_backends.install_desktop_backend("claude", base_env=env)
    assert refused.value.code == "install_shutting_down"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("scoped", [False, True], ids=["full", "scoped"])
def test_stop_reaps_the_tree_of_an_owner_that_died_before_draining(monkeypatch, installer_tree, quiet_stop, scoped):
    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    pids = _start_killed_owner(env, wait_for_tree)

    exit_code = cli.cmd_stop(expect_runtime_id=_TREE_RUNTIME_ID) if scoped else cli.cmd_stop()

    assert exit_code == 0
    assert _running(pids) == []
    assert _install_lock_is_free(env)
    assert statuses == [("stopped",)]
    # The stop reads no record; its staging and record wait for the next claim.
    assert len(_stagings(env)) == 1
    assert len(_install_records()) == 1
    _patch_fake_install(monkeypatch)
    desktop_backends.install_desktop_backend("claude", base_env=env)
    assert _stagings(env) == []
    assert _install_records() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("failing", ["service", "ui"])
def test_a_full_stop_that_fails_still_reaps_the_tree_of_a_dead_owner(
    monkeypatch, installer_tree, quiet_stop, children, failing
):
    from vibe import runtime

    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    pids = _start_killed_owner(env, wait_for_tree)
    # The stop reports this half, a process of the caller's own Runtime, as
    # still running.
    children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]))
    running = children[-1].pid
    if failing == "service":
        monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda include_starting=True: running)
    else:
        config_paths.get_runtime_ui_pid_path().write_text(str(running), encoding="utf-8")
        monkeypatch.setattr(runtime, "stop_ui", lambda **kwargs: False)

    assert cli.cmd_stop() == 2

    assert _running(pids) == []
    assert _install_lock_is_free(env)
    assert statuses == [("error", f"{failing} stop failed")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("ui_pidfile", [None, "not-a-pid"], ids=["missing", "corrupt"])
@pytest.mark.parametrize("caller", ["stop", "restart"])
def test_the_tree_of_a_live_owner_stays_whatever_the_ui_pidfile_says(
    monkeypatch, installer_tree, quiet_stop, children, ui_pidfile, caller
):
    from vibe import restart_supervisor

    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    pids = _start_owner(children, env, wait_for_tree)
    config_paths.ensure_data_dirs()
    if ui_pidfile is not None:
        config_paths.get_runtime_ui_pid_path().write_text(ui_pidfile, encoding="utf-8")

    if caller == "stop":
        assert cli.cmd_stop() == 0
        assert statuses == [("stopped",)]
    else:
        seen_at_start: list[list[int]] = []

        def start(start_ui=True, **kwargs):
            seen_at_start.append(_running(pids))
            raise RuntimeError("no Runtime starts in this test")

        monkeypatch.setattr(restart_supervisor, "_stop_service_for_restart", lambda runtime_ids: (True, 0.0))
        monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
        monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", start)

        restart_supervisor._run_restart_job(job_id="jobliveowner", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

        # The reap let the restart go on to start the successor.
        assert seen_at_start == [pids]
    assert _running(pids) == pids


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_live_owner_whose_create_time_shifted_keeps_its_tree(monkeypatch, installer_trees, children):
    # A Claude install runs, macOS then shows its UI started at another time,
    # and a second install's claim scans the Runtime for abandoned trees.
    pids = _start_owner(children, *installer_trees("first"))
    _shift_create_time(monkeypatch, children[-1].pid)
    second_env, _ = installer_trees("second")
    _patch_fake_install(monkeypatch)

    desktop_backends.install_desktop_backend("claude", base_env=second_env)

    assert _running(pids) == pids


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("holder", ["not-a-ui", "ui-of-another-runtime", "ui-without-a-runtime-id"])
def test_a_tree_whose_owner_pid_now_names_another_process_is_reaped(
    tmp_path, installer_tree, quiet_stop, children, holder
):
    from vibe import runtime

    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    # The owner exited and its pid now belongs to a process started later:
    # this test, or a UI that is not one of this Runtime.
    if holder == "not-a-ui":
        pid = os.getpid()
    else:
        ui_env = {key: value for key, value in os.environ.items() if key != desktop_backends.DESKTOP_RUNTIME_ID_ENV}
        if holder == "ui-of-another-runtime":
            ui_env[desktop_backends.DESKTOP_RUNTIME_ID_ENV] = secrets.token_hex(32)
        pid = _start_ui_stand_in(children, tmp_path, ui_env)
    reused = f"{pid}:{runtime.process_create_time(pid) - 1.0!r}"
    pids = _start_tree_naming(children, env, wait_for_tree, reused)

    assert cli.cmd_stop() == 0

    assert _running(pids) == []
    assert statuses == [("stopped",)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize(
    ("shape", "kept"),
    [
        ("token-of-a-live-ui", True),
        ("another-token-at-a-ui-of-this-runtime", False),
        ("released-value-of-a-live-ui", True),
        ("released-value-of-an-exited-owner", False),
    ],
)
def test_an_owner_value_is_judged_by_the_identity_its_shape_carries(
    tmp_path, installer_tree, quiet_stop, children, shape, kept
):
    """#2511: an owner value this build writes names its owner by start token, so
    another token at its pid means the pid was reused -- even by another UI of the
    same Runtime, which the released ``pid:create_time`` value could not tell from
    its owner. Only a released value keeps that "a UI of this Runtime" fallback,
    because macOS can shift the create time it carries while its owner runs."""

    from vibe import runtime

    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    if shape == "released-value-of-an-exited-owner":
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(10)
        owner = f"{exited.pid}:{1.0!r}"
    else:
        ui_pid = _start_ui_stand_in(children, tmp_path, dict(os.environ))
        if shape == "token-of-a-live-ui":
            owner = f"v2:{ui_pid}:{runtime.process_start_token(ui_pid)}"
        elif shape == "another-token-at-a-ui-of-this-runtime":
            owner = f"v2:{ui_pid}:{runtime.process_start_token(ui_pid)}-earlier"
        else:
            # The create time macOS can show for an owner that keeps running.
            owner = f"{ui_pid}:{runtime.process_create_time(ui_pid) - 1.0!r}"
    pids = _start_tree_naming(children, env, wait_for_tree, owner)

    assert cli.cmd_stop() == 0

    assert statuses == [("stopped",)]
    assert _running(pids) == (pids if kept else [])


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("owner", ["missing", "malformed", "uninspectable", "token-unreadable"])
@pytest.mark.parametrize("scoped", [False, True], ids=["full", "scoped"])
def test_a_tree_whose_owner_cannot_be_told_fails_every_caller_closed(
    monkeypatch, installer_tree, quiet_stop, children, capsys, owner, scoped
):
    from vibe import runtime

    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    named = {"missing": None, "malformed": "not-an-owner"}.get(owner)
    if owner == "uninspectable":
        # Shown started at another time, and nothing else about it can be read.
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        children.append(live)
        named = f"{live.pid}:{runtime.process_create_time(live.pid)!r}"
        _shift_create_time(monkeypatch, live.pid)

        def unreadable(real):
            def read(process):
                if process.pid == live.pid:
                    raise psutil.AccessDenied(process.pid)
                return real(process)

            return read

        for method in ("environ", "cmdline"):
            monkeypatch.setattr(psutil.Process, method, unreadable(getattr(psutil.Process, method)))
    elif owner == "token-unreadable":
        # It names its owner by start token, which cannot be read now.
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        children.append(live)
        named = f"v2:{live.pid}:{runtime.process_start_token(live.pid)}"
        real_token = runtime.process_start_token
        monkeypatch.setattr(
            runtime, "process_start_token", lambda pid: None if pid == live.pid else real_token(pid)
        )
    pids = _start_tree_naming(children, env, wait_for_tree, named)
    npm_runs: list[bool] = []
    _patch_fake_install(monkeypatch, before=lambda: npm_runs.append(True))

    assert (cli.cmd_stop(expect_runtime_id=_TREE_RUNTIME_ID) if scoped else cli.cmd_stop()) == 2

    assert statuses == [("error", "desktop backend install drain failed")]
    if scoped:
        reported = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert reported["failed"] == "installer"
        assert sorted(item["pid"] for item in reported["remaining"]) == sorted(pids)
    assert _running(pids) == pids
    with pytest.raises(desktop_backends.DesktopBackendError) as refused:
        desktop_backends.install_desktop_backend("claude", base_env=env)
    assert refused.value.code == "install_locked"
    assert npm_runs == []
    assert _running(pids) == pids
    assert _install_lock_is_free(env)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_two_uis_of_one_runtime_each_keep_their_own_tree(installer_trees, children):
    # Each installs into its own root, so neither waits on the other's lock,
    # and the second one's claim sees the first one's tree by the Runtime id.
    first = _start_owner(children, *installer_trees("first"))
    second = _start_owner(children, *installer_trees("second"))

    assert _running(first) == first
    assert _running(second) == second


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX file permissions")
def test_an_unreadable_records_directory_hides_no_tree_and_authorises_no_cleanup(
    monkeypatch, installer_tree, quiet_stop
):
    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    pids = _start_killed_owner(env, wait_for_tree)
    records = _install_records()
    records[0].parent.chmod(0)
    npm_runs: list[bool] = []
    _patch_fake_install(monkeypatch, before=lambda: npm_runs.append(True))
    try:
        # The scan finds the tree; no listing is asked whether one exists.
        assert cli.cmd_stop(expect_runtime_id=_TREE_RUNTIME_ID) == 0
        assert _running(pids) == []
        # An empty or failed listing never reads as "nothing to clean up".
        with pytest.raises(desktop_backends.DesktopBackendError) as refused:
            desktop_backends.install_desktop_backend("claude", base_env=env)
    finally:
        records[0].parent.chmod(0o700)

    assert refused.value.code == "install_locked"
    assert npm_runs == []
    assert _install_records() == records
    assert len(_stagings(env)) == 1
    assert _install_lock_is_free(env)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_drain_during_the_version_probe_does_not_return_while_the_probe_runs(
    monkeypatch, tmp_path, owner_runtime_id
):
    env = _desktop_env(tmp_path)
    monkeypatch.setattr(desktop_backends, "_INSTALLERS_CLOSED", False)
    # The installed executable is a script that answers no probe until stopped.
    monkeypatch.setattr(desktop_backends, "_has_native_magic", lambda _path: True)
    probe = tmp_path / "probe"
    fake_npm = _fake_npm_install("claude")
    real_run = desktop_backends._run_command

    def run(command, **kwargs):
        if "--prefix" not in command:
            return real_run(command, **kwargs)
        completed = fake_npm(command, **kwargs)
        staging = Path(command[command.index("--prefix") + 1])
        next(staging.glob("node_modules/@anthropic-ai/*/claude")).write_text(
            "#!/bin/sh\n"
            f'echo "$$ $AVIBE_DESKTOP_ROLE $AVIBE_DESKTOP_RUNTIME_ID" > "{probe}.tmp"\n'
            f'/bin/mv "{probe}.tmp" "{probe}"\n'
            "exec /bin/sleep 120\n",
            encoding="utf-8",
        )
        return completed

    monkeypatch.setattr(desktop_backends, "_run_command", run)
    outcome: dict[str, str] = {}

    def install() -> None:
        try:
            desktop_backends.install_desktop_backend("claude", base_env=env)
        except desktop_backends.DesktopBackendError as exc:
            outcome["code"] = exc.code

    worker = threading.Thread(target=install)
    worker.start()
    deadline = time.monotonic() + 15
    while not probe.exists():
        assert time.monotonic() < deadline, "the probe did not start"
        time.sleep(0.05)
    pid, *inherited = probe.read_text(encoding="utf-8").split()
    try:
        drained = desktop_backends.drain_desktop_backend_installs()
        assert not pid_alive(int(pid))
        assert drained
    finally:
        if pid_alive(int(pid)):
            os.kill(int(pid), signal.SIGKILL)
        worker.join(timeout=30)

    # The probe is an installer of this Runtime, which the scoped stop's scan also finds.
    assert inherited == [desktop_backends.DESKTOP_INSTALLER_ROLE, owner_runtime_id]
    assert outcome == {"code": "executable_probe_failed"}
    assert _install_records() == []
    assert _install_lock_is_free(env)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_stop_fails_and_keeps_the_record_when_the_tree_cannot_be_shown_gone(
    monkeypatch, installer_tree, quiet_stop
):
    env, wait_for_tree = installer_tree
    cli, statuses = quiet_stop
    _start_killed_owner(env, wait_for_tree)
    monkeypatch.setattr(desktop_backends, "reap_marked_processes", lambda *args, **kwargs: "unconfirmed")

    assert cli.cmd_stop() == 2

    assert statuses == [("error", "desktop backend install drain failed")]
    assert len(_install_records()) == 1
    assert len(_stagings(env)) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_install_keeps_its_lock_and_staging_while_its_tree_is_not_shown_gone(monkeypatch, tmp_path):
    env = _installer_env(tmp_path, "")
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude"
    monkeypatch.setattr(desktop_backends, "_reap_installer_tree", lambda *args: False)
    outcome: dict[str, str] = {}

    def install() -> None:
        try:
            desktop_backends.install_desktop_backend("claude", base_env=env)
        except desktop_backends.DesktopBackendError as exc:
            outcome["code"] = exc.code

    worker = threading.Thread(target=install)
    worker.start()
    worker.join(timeout=30)

    assert outcome == {"code": "install_drain_failed"}
    assert not _install_lock_is_free(env)
    assert list(backend_root.glob(".staging-*"))
    assert len(_install_records()) == 1


def _patch_fake_install(monkeypatch, *, before=None):
    fake_npm = _fake_npm_install("claude")

    def run(command, **kwargs):
        if before is not None and "--prefix" in command:
            before()
        return fake_npm(command, **kwargs)

    monkeypatch.setattr(desktop_backends, "_run_command", run)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_install_reaps_an_abandoned_installer_and_its_staging_before_it_proceeds(monkeypatch, installer_tree):
    # A UI killed mid-install leaves its tree writing into its staging; the
    # UI that replaces it must not install on top of that.
    env, wait_for_tree = installer_tree
    pids = _start_killed_owner(env, wait_for_tree)
    seen_by_npm: list[tuple] = []
    _patch_fake_install(
        monkeypatch,
        before=lambda: seen_by_npm.append((_running(pids), len(_stagings(env)), _install_records())),
    )

    result = desktop_backends.install_desktop_backend("claude", base_env=env)

    # Only the new install's own staging exists when npm runs.
    assert seen_by_npm == [([], 1, [])]
    assert result.version == "1.2.3"
    assert _stagings(env) == []
    assert _install_lock_is_free(env)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_install_is_refused_while_an_abandoned_installer_cannot_be_shown_gone(monkeypatch, installer_tree):
    env, wait_for_tree = installer_tree
    _start_killed_owner(env, wait_for_tree)
    monkeypatch.setattr(desktop_backends, "reap_marked_processes", lambda *args, **kwargs: "unconfirmed")
    npm_runs: list[bool] = []
    _patch_fake_install(monkeypatch, before=lambda: npm_runs.append(True))

    with pytest.raises(desktop_backends.DesktopBackendError) as refused:
        desktop_backends.install_desktop_backend("claude", base_env=env)

    assert refused.value.code == "install_locked"
    assert npm_runs == []
    assert _install_lock_is_free(env)
    assert len(_install_records()) == 1
    assert len(_stagings(env)) == 1


@pytest.mark.parametrize(
    "recorded",
    [
        lambda root, name: str(root / "releases" / name.removeprefix(".staging-")),
        lambda root, name: str(root / "releases" / name),
        lambda root, name: os.path.join(str(root), "..", "..", "outside", "claude", name),
        lambda root, name: os.path.join("claude", name),
    ],
    ids=["release", "not-in-a-backend-root", "traversal", "relative"],
)
def test_a_recorded_path_outside_a_staging_directory_is_never_removed(monkeypatch, tmp_path, recorded):
    env = _desktop_env(tmp_path)
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude"
    monkeypatch.chdir(tmp_path)
    path = Path(recorded(backend_root, f".staging-{'b' * 32}"))
    (path / "keep").mkdir(parents=True)
    records = config_paths.get_runtime_dir() / "desktop-backend-installs"
    records.mkdir(parents=True, exist_ok=True)
    record = records / f"{'b' * 32}.json"
    desktop_backends._write_installer_record(
        record,
        worker_fingerprint=desktop_backends.fingerprint_process_marker(desktop_backends.new_process_identity_marker()),
        identity=None,
        staging=path,
    )
    _patch_fake_install(monkeypatch)

    desktop_backends.install_desktop_backend("claude", base_env=env)

    # The record was read, and rejected: it names nothing an install made.
    assert not record.exists()
    assert (path / "keep").is_dir()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX file permissions")
def test_an_installer_record_that_cannot_be_read_is_unknown_and_kept(monkeypatch, tmp_path):
    # Only a record whose content was read and proven invalid is discarded; one
    # that cannot be read may still name a running installer tree.
    env = _desktop_env(tmp_path)
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude"
    backend_root.mkdir(parents=True, exist_ok=True)
    records = config_paths.get_runtime_dir() / "desktop-backend-installs"
    records.mkdir(parents=True, exist_ok=True)
    record = records / f"{'c' * 32}.json"
    desktop_backends._write_installer_record(
        record,
        worker_fingerprint=desktop_backends.fingerprint_process_marker(desktop_backends.new_process_identity_marker()),
        identity=None,
        staging=backend_root / f".staging-{'c' * 32}",
    )
    record.chmod(0)
    _patch_fake_install(monkeypatch)

    with pytest.raises(desktop_backends.DesktopBackendError) as refused:
        desktop_backends.install_desktop_backend("claude", base_env=env)

    assert refused.value.code == "install_locked"
    assert record.exists()


# --- Removal deletes the backends only once no installer can be writing (#2131) ---


def _remove_backends(monkeypatch, capsys, env: dict[str, str]) -> tuple[int, dict | None]:
    """``vibe desktop remove-backends`` as the desktop runs it, with ``env``'s private paths."""

    from vibe import cli

    for key in (
        "AVIBE_DESKTOP_RUNTIME_ROOT",
        "VIBE_SHOW_RUNTIME_NODE_BIN",
        "AVIBE_DESKTOP_NPM_CLI",
        "AVIBE_DESKTOP_BACKENDS_ROOT",
    ):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setattr(cli.sys, "argv", ["vibe", "desktop", "remove-backends"])
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: None)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exited:
        cli.main()
    err = capsys.readouterr().err.strip().splitlines()
    return exited.value.code, json.loads(err[-1]) if err else None


def _backend_files(env: dict[str, str]) -> set[str]:
    root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])
    # A claim leaves its lock file behind; that is not an install's file.
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.name != ".install.lock"}


def _published_backends(env: dict[str, str]) -> None:
    root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])
    for backend in ("claude", "codex", "opencode"):
        _write_native(root / backend / "releases" / "r1" / "bin" / backend)
        (root / backend / "current.json").write_text("{}", encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_removal_reaps_an_abandoned_installer_before_it_deletes_the_backends(monkeypatch, capsys, installer_tree):
    env, wait_for_tree = installer_tree
    pids = _start_killed_owner(env, wait_for_tree)
    _published_backends(env)

    assert _remove_backends(monkeypatch, capsys, env) == (0, None)

    assert _running(pids) == []
    assert _install_records() == []
    assert not os.path.lexists(env["AVIBE_DESKTOP_BACKENDS_ROOT"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_removal_keeps_every_file_while_an_installer_cannot_be_shown_gone(monkeypatch, capsys, installer_tree):
    env, wait_for_tree = installer_tree
    _start_killed_owner(env, wait_for_tree)
    _published_backends(env)
    monkeypatch.setattr(desktop_backends, "reap_marked_processes", lambda *args, **kwargs: "unconfirmed")
    files = _backend_files(env)

    assert _remove_backends(monkeypatch, capsys, env) == (3, {"reason": "install_locked"})

    assert _backend_files(env) == files
    assert len(_install_records()) == 1
    assert _install_lock_is_free(env)


def test_removal_deletes_nothing_while_any_backend_is_being_installed(
    monkeypatch, capsys, tmp_path, owner_runtime_id, children
):
    # The held root sorts last, so the ones before it are already claimed.
    env = _desktop_env(tmp_path)
    _published_backends(env)
    held = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "opencode" / ".install.lock"
    ready = tmp_path / "held"
    holder = (
        "import pathlib, sys, time\n"
        "from storage.lock import MigrationFileLock\n"
        "MigrationFileLock(pathlib.Path(sys.argv[1])).acquire()\n"
        "pathlib.Path(sys.argv[2]).touch()\n"
        "time.sleep(120)\n"
    )
    children.append(subprocess.Popen([sys.executable, "-c", holder, str(held), str(ready)], cwd=_REPO_ROOT))
    deadline = time.monotonic() + 15
    while not ready.exists():
        assert time.monotonic() < deadline, "lock holder did not start"
        time.sleep(0.05)
    monkeypatch.setattr(desktop_backends, "DESKTOP_BACKEND_REMOVAL_LOCK_TIMEOUT_SECONDS", 0.2)
    files = _backend_files(env)

    assert _remove_backends(monkeypatch, capsys, env) == (3, {"reason": "install_locked"})

    assert _backend_files(env) == files
    assert _install_lock_is_free(env)


# An install's first steps, as `install_desktop_backend` takes them.
_LATE_INSTALL = """
import pathlib, sys
from vibe import desktop_backends
root = pathlib.Path(sys.argv[1])
try:
    desktop_backends._claim_backend_root(root, timeout_seconds=30)
except desktop_backends.DesktopBackendError as exc:
    print(exc.code, flush=True)
else:
    (root / ".staging-late").mkdir()
    print("installing", flush=True)
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX file locks")
def test_an_install_waiting_on_a_removed_backend_refuses_once_it_gets_the_lock(
    monkeypatch, capsys, tmp_path, owner_runtime_id, children
):
    env = _desktop_env(tmp_path)
    _published_backends(env)
    backend = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude"
    lock_path = os.path.realpath(backend / ".install.lock")
    remove_path = desktop_backends._remove_path

    def remove_once_an_install_waits(path):
        # Every lock is held once removal starts deleting.
        if not children:
            waiter = subprocess.Popen(
                [sys.executable, "-c", _LATE_INSTALL, str(backend)], cwd=_REPO_ROOT, stdout=subprocess.PIPE, text=True
            )
            children.append(waiter)
            deadline = time.monotonic() + 15
            while lock_path not in {os.path.realpath(f.path) for f in psutil.Process(waiter.pid).open_files()}:
                assert time.monotonic() < deadline, "the install did not wait on the lock"
                time.sleep(0.05)
        remove_path(path)

    monkeypatch.setattr(desktop_backends, "_remove_path", remove_once_an_install_waits)

    assert _remove_backends(monkeypatch, capsys, env) == (0, None)

    assert children[0].communicate(timeout=30)[0] == "install_locked\n"
    assert not os.path.lexists(env["AVIBE_DESKTOP_BACKENDS_ROOT"])


def test_a_backend_an_install_begins_during_removal_keeps_its_files(monkeypatch, capsys, tmp_path, owner_runtime_id):
    env = _desktop_env(tmp_path)
    _published_backends(env)
    # A first install of a backend creates its directory after removal listed the root.
    late = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "late" / ".staging-late" / "package.json"
    remove_path = desktop_backends._remove_path

    def remove_once_an_install_began(path):
        if not late.exists():
            late.parent.mkdir(parents=True)
            late.write_text("{}", encoding="utf-8")
        remove_path(path)

    monkeypatch.setattr(desktop_backends, "_remove_path", remove_once_an_install_began)

    assert _remove_backends(monkeypatch, capsys, env) == (2, {"failed": "backend_removal_failed"})

    assert late.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX file permissions")
def test_removal_that_cannot_delete_every_file_fails(monkeypatch, capsys, tmp_path, owner_runtime_id):
    env = _desktop_env(tmp_path)
    _published_backends(env)
    stuck = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "codex" / "releases"
    stuck.chmod(0o500)
    try:
        result = _remove_backends(monkeypatch, capsys, env)
    finally:
        stuck.chmod(0o700)

    assert result == (2, {"failed": "backend_removal_failed"})
    assert (stuck / "r1").is_dir()


def test_resolve_cli_path_maps_default_cli_agy_to_published_desktop_antigravity(monkeypatch, tmp_path):
    """Default selector 'agy' resolves to the published desktop Antigravity installation."""
    from vibe.cli_paths import desktop_backend_for_cli, resolve_cli_path

    env = _desktop_env(tmp_path)
    root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])
    binary_name = "agy.exe" if os.name == "nt" else "agy"
    published = root / "antigravity" / "releases" / "r1" / "bin" / binary_name
    _write_native(published)
    descriptor = {
        "schema_version": desktop_backends.CURRENT_DESCRIPTOR_SCHEMA_VERSION,
        "backend": "antigravity",
        "package": desktop_backends.BACKEND_SPECS["antigravity"].package,
        "version": "1.2.3",
        "executable": published.relative_to(root).as_posix(),
    }
    (root / "antigravity" / "current.json").write_text(json.dumps(descriptor), encoding="utf-8")

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("AVIBE_DESKTOP_RUNTIME_ROOT", env["AVIBE_DESKTOP_RUNTIME_ROOT"])
    monkeypatch.setenv("AVIBE_DESKTOP_BACKENDS_ROOT", env["AVIBE_DESKTOP_BACKENDS_ROOT"])
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(
        "vibe.cli_paths._candidate_cli_paths",
        lambda binary, **_kwargs: [Path(binary)] if Path(binary).is_absolute() else [],
    )

    assert desktop_backend_for_cli("agy") == "antigravity"
    assert desktop_backend_for_cli("agy.exe") == "antigravity"
    assert desktop_backend_for_cli("antigravity") == "antigravity"

    expected = str(published.resolve(strict=True))
    assert resolve_cli_path("agy", include_npm_global=False) == expected
    assert resolve_cli_path("antigravity", include_npm_global=False) == expected
    assert resolve_cli_path("agy", include_npm_global=False, include_desktop=False) is None

    # A stale desktop release path ending in agy stays within the published desktop backend
    # instead of falling back to an unrelated external PATH binary.
    external = empty_bin / binary_name
    _write_native(external)
    stale_desktop_path = root / "antigravity" / "releases" / "stale" / "bin" / binary_name
    assert resolve_cli_path(str(stale_desktop_path), include_npm_global=False) == expected



def _desktop_archive_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    runtime.mkdir(parents=True)
    return {"AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime), "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"), "PATH": str(tmp_path / "external-bin")}


def _native_binary_bytes() -> bytes:
    return (b"MZ\x00\x00" if os.name == "nt" else b"\x7fELF") + b"0" * 64


def _build_archive(archive_path: Path, fmt: str, members: dict[str, bytes], *, symlink: str | None = None) -> str:
    if fmt == "tar.gz":
        with tarfile.open(archive_path, "w:gz") as tf:
            for name, content in members.items():
                info = tarfile.TarInfo(name=name)
                info.size, info.mode = len(content), 0o755
                tf.addfile(info, io.BytesIO(content))
            if symlink is not None:
                link = tarfile.TarInfo(name=symlink)
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tf.addfile(link)
    else:
        with zipfile.ZipFile(archive_path, "w") as zf:
            for name, content in members.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o755) << 16
                zf.writestr(info, content)
            if symlink is not None:
                link = zipfile.ZipInfo(symlink)
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(link, "/etc/passwd")
    return hashlib.sha256(archive_path.read_bytes()).hexdigest()


def _release_payload(asset_name: str, digest: str | None = None) -> dict[str, Any]:
    asset: dict[str, str] = {"name": asset_name, "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v1.5.16/{asset_name}"}
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {"tag_name": "v1.5.16", "assets": [asset]}


@pytest.mark.parametrize("binary_name", ["agy", "antigravity"])
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_desktop_archive_install_verifies_sha256_and_publishes_without_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary_name: str, archive_format: str
) -> None:
    env = _desktop_archive_env(tmp_path)
    assert desktop_backends.desktop_backend_toolchain(env) is None and desktop_backends.desktop_backend_toolchain(env, backend="antigravity") is not None

    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.{archive_format}"
    source_archive = tmp_path / asset_name
    exe_name = f"{binary_name}.exe" if os.name == "nt" else binary_name
    digest = _build_archive(source_archive, archive_format, {f"bin/{exe_name}": _native_binary_bytes()})

    probe_envs: list[dict[str, str]] = []
    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, digest))
    monkeypatch.setattr(desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(source_archive.read_bytes()))
    monkeypatch.setattr(desktop_backends, "_run_command", lambda cmd, *, cwd, env, timeout_seconds: probe_envs.append(dict(env)) or subprocess.CompletedProcess(cmd, 0, "1.5.16\n", ""))

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend("antigravity", base_env=env, activate=activated.append)
    assert (result.backend, result.package, result.version, result.path, Path(result.path).name) == ("antigravity", "google-antigravity/antigravity-cli", "1.5.16", activated[0], exe_name)
    assert desktop_backends.resolve_published_desktop_backend("antigravity", env) == result.path and probe_envs[0]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    sub_env = desktop_backends.desktop_backend_subprocess_environment("antigravity", result.path, env)
    assert sub_env is not None and sub_env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


@pytest.mark.parametrize(
    ("case_id", "fmt", "members", "symlink", "digest_override", "expected_code"),
    [
        ("version_mismatch", "tar.gz", {"bin/agy": _native_binary_bytes()}, None, None, "version_mismatch"),
        ("sha256_mismatch", "tar.gz", {"agy": _native_binary_bytes()}, None, "0" * 64, "checksum_mismatch"),
        ("missing_checksum", "tar.gz", {"agy": b"x"}, None, "", "missing_checksum"),
        ("traversal_tar", "tar.gz", {"../outside-agy": _native_binary_bytes()}, None, None, "invalid_archive"),
        ("symlink_tar", "tar.gz", {"bin/agy": _native_binary_bytes()}, "bin/link", None, "invalid_archive"),
        ("symlink_zip", "zip", {"bin/agy": _native_binary_bytes()}, "bin/link", None, "invalid_archive"),
    ],
)
def test_desktop_archive_install_rejects_bad_checksum_traversal_and_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case_id: str, fmt: str, members: dict[str, bytes], symlink: str | None, digest_override: str | None, expected_code: str
) -> None:
    env = _desktop_archive_env(tmp_path)
    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.{fmt}"
    archive = tmp_path / f"{case_id}.{fmt}"
    real_digest = _build_archive(archive, fmt, members, symlink=symlink)
    digest = None if digest_override == "" else (digest_override or real_digest)

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, digest))
    monkeypatch.setattr(desktop_backends, "_download_release_asset", lambda _u, dest, **_kw: dest.write_bytes(archive.read_bytes()))
    monkeypatch.setattr(desktop_backends, "_run_command", lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, "1.5.17\n", ""))
    extract_called = False
    if case_id == "sha256_mismatch":
        def _forbid(*_a: Any, **_kw: Any) -> None:
            nonlocal extract_called
            extract_called = True
        monkeypatch.setattr(desktop_backends, "_extract_release_archive", _forbid)

    activated = []
    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env, version="1.5.16" if case_id == "version_mismatch" else None, activate=activated.append)
    assert activated == []
    assert raised.value.code == expected_code and extract_called is False and not (tmp_path / "outside-agy").exists()
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "antigravity"
    assert not (backend_root / "current.json").exists() and list(backend_root.glob(".staging-*")) == [] and list(backend_root.glob("releases/*")) == []


def test_desktop_antigravity_pinned_version_fetches_tag_and_activates(monkeypatch, tmp_path):
    """Pinned Antigravity archive install fetches /releases/tags/v<ver> (never /releases/latest)."""
    import urllib.error

    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    runtime.mkdir(parents=True)
    env = {
        "AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime),
        "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"),
        "PATH": str(tmp_path / "external-bin"),
    }
    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.tar.gz"
    exe_name = "agy.exe" if os.name == "nt" else "agy"
    archive = tmp_path / asset_name
    digest = _build_archive(archive, "tar.gz", {f"bin/{exe_name}": _native_binary_bytes()})
    release_payload = {
        "tag_name": "v0.4.0",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/google-antigravity/antigravity-cli/releases/download/v0.4.0/{asset_name}",
                "digest": f"sha256:{digest}",
            }
        ],
    }

    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self, _n: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    requested_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        del timeout
        requested_urls.append(request.full_url)
        if request.full_url.endswith("/releases/tags/v0.4.0"):
            return _FakeResp(json.dumps(release_payload).encode("utf-8"))
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(desktop_backends.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        desktop_backends,
        "_download_release_asset",
        lambda _url, dest, **_kw: dest.write_bytes(archive.read_bytes()),
    )
    monkeypatch.setattr(
        desktop_backends,
        "_run_command",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, "0.4.0\n", ""),
    )

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend(
        "antigravity",
        base_env=env,
        activate=activated.append,
        version="0.4.0",
    )

    assert requested_urls == [
        "https://api.github.com/repos/google-antigravity/antigravity-cli/releases/tags/v0.4.0"
    ]
    assert all("/releases/latest" not in url for url in requested_urls)
    assert result.version == "0.4.0"
    assert activated == [result.path]
    assert desktop_backends.resolve_published_desktop_backend("antigravity", env) == result.path

    with pytest.raises(desktop_backends.DesktopBackendError) as invalid:
        desktop_backends.install_desktop_backend("antigravity", base_env=env, version="../0.4.0")
    assert invalid.value.code == "invalid_version"


def test_antigravity_windows_repair_source_matches_native_vendor_install_target(monkeypatch, tmp_path):
    """Windows agy repair recognition and native install target share the same %LOCALAPPDATA% path contract."""
    from types import SimpleNamespace

    from vibe import api, cli_repair
    from vibe.cli_repair import RepairSource, identify_repair_source, native_vendor_install_target

    monkeypatch.setattr(
        cli_repair,
        "os",
        SimpleNamespace(name="nt", environ=os.environ, pathsep=os.pathsep, curdir=os.curdir),
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    expected = local_app_data / "agy" / "bin" / "agy.exe"
    expected.parent.mkdir(parents=True)
    expected.write_text("", encoding="utf-8")

    assert native_vendor_install_target("antigravity", home=tmp_path) == expected
    assert api._native_vendor_install_target("antigravity") == str(expected)
    assert identify_repair_source("antigravity", str(expected), home=tmp_path) == RepairSource(
        kind="native",
        destination=str(expected),
    )

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    fallback = tmp_path / ".local" / "bin" / "agy.exe"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("", encoding="utf-8")

    assert native_vendor_install_target("antigravity", home=tmp_path) == fallback
    assert api._native_vendor_install_target("antigravity") == str(fallback)
    assert identify_repair_source("antigravity", str(fallback), home=tmp_path) == RepairSource(
        kind="native",
        destination=str(fallback),
    )


@pytest.mark.parametrize("managed", [False, True])
def test_antigravity_get_backend_runtime_reports_auto_update_capability_by_install_source(
    monkeypatch, tmp_path, managed
):
    """Both script and desktop installs expose Avibe-owned idle automatic updates."""
    from config.v2_config import V2Config
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    agy_path = str(tmp_path / "bin" / "agy")
    cfg = V2Config.default()
    cfg.agents.antigravity.enabled = True
    cfg.agents.antigravity.auto_update = True
    cfg.agents.antigravity.cli_path = agy_path
    cfg.save()

    async def fake_detect_cli_async(_path, _backend):
        return {"found": True, "path": agy_path, "status": "ok", "version": "0.4.0", "reason": None}

    monkeypatch.setattr(api, "detect_cli_async", fake_detect_cli_async)
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda path: managed and path == agy_path)
    monkeypatch.setattr(api, "_cached_latest", lambda _name: "0.5.0")

    runtime = api.get_backend_runtime("antigravity")

    assert runtime["ok"] is True
    assert runtime["managed_by"] == ("desktop" if managed else None)
    assert runtime["auto_update_supported"] is True
    assert runtime["auto_update"] is True


@pytest.mark.parametrize(
    ("os_name", "tools", "expected_exe", "expected_fragments"),
    [
        (
            "darwin",
            {"bash": "/bin/bash", "curl": "/usr/bin/curl"},
            "/bin/bash",
            [
                desktop_backends.CURL_INSTALL_RETRY_FLAGS,
                desktop_backends._ANTIGRAVITY_INSTALL_SH_URL,
                "--dir",
            ],
        ),
        (
            "win32",
            {"powershell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"},
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            [
                "[scriptblock]::Create((irm https://antigravity.google/cli/install.ps1 -ErrorAction Stop))",
                "--dir",
                "--skip-path",
                "--skip-aliases",
            ],
        ),
    ],
)
def test_antigravity_official_script_command_construction(
    tmp_path: Path,
    os_name: str,
    tools: dict[str, str],
    expected_exe: str,
    expected_fragments: list[str],
) -> None:
    staging = tmp_path / "staging dir"
    cmd = desktop_backends._antigravity_official_script_command(
        staging,
        os_name=os_name,
        which=tools.get,
    )
    assert cmd is not None
    assert cmd[0] == expected_exe
    joined = " ".join(cmd)
    for fragment in expected_fragments:
        assert fragment in joined
    assert str(staging) in joined
    if os_name == "win32":
        assert "| iex" not in joined


def _install_fake_shell_tools(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool_names = ("powershell.exe",) if os.name == "nt" else ("bash", "curl")
    for name in tool_names:
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)


def test_desktop_antigravity_install_uses_official_script_with_isolated_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unpinned desktop Antigravity install tries Google's official script in staging with isolated HOME."""
    env = _desktop_archive_env(tmp_path)
    _install_fake_shell_tools(Path(env["PATH"]))
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    env["HOME"] = str(real_home)
    env["USERPROFILE"] = str(real_home)
    env["LOCALAPPDATA"] = str(real_home / "AppData" / "Local")

    exe_name = "agy.exe" if os.name == "nt" else "agy"
    script_calls: list[tuple[list[str], dict[str, str], Path]] = []

    def fake_run(cmd, *, cwd, env, timeout_seconds):
        if len(cmd) == 2 and cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "1.6.0\n", "")
        script_calls.append((list(cmd), dict(env), cwd))
        _write_native(cwd / exe_name)
        return subprocess.CompletedProcess(cmd, 0, "installed via official script", "")

    def forbid_github(*_a, **_kw):
        raise AssertionError("GitHub release fallback must not be called when official script succeeds")

    monkeypatch.setattr(desktop_backends, "_run_command", fake_run)
    monkeypatch.setattr(desktop_backends, "_fetch_github_release", forbid_github)

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend("antigravity", base_env=env, activate=activated.append)

    assert len(script_calls) == 1
    cmd, script_env, staging_cwd = script_calls[0]
    assert f"--dir" in " ".join(cmd) and str(staging_cwd) in " ".join(cmd)
    assert Path(script_env["HOME"]).is_relative_to(staging_cwd)
    assert script_env["HOME"] != str(real_home)
    assert script_env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert (result.backend, result.version, result.path) == ("antigravity", "1.6.0", activated[0])
    assert desktop_backends.resolve_published_desktop_backend("antigravity", env) == result.path
    assert not (Path(result.path).parent / ".installer-home").exists()


def test_desktop_antigravity_script_failure_falls_back_to_github_and_combines_dual_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Script failure cleans staging and falls back to GitHub; dual failure reports both labelled causes."""
    env = _desktop_archive_env(tmp_path)
    _install_fake_shell_tools(Path(env["PATH"]))

    asset_name = f"agy_cli_{desktop_backends._release_archive_platform_tag('antigravity')}.tar.gz"
    exe_name = "agy.exe" if os.name == "nt" else "agy"
    source_archive = tmp_path / asset_name
    digest = _build_archive(source_archive, "tar.gz", {f"bin/{exe_name}": _native_binary_bytes()})

    def failing_script_then_probe(cmd, *, cwd, env, timeout_seconds):
        if len(cmd) == 2 and cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "1.5.16\n", "")
        (cwd / "leftover-from-script").write_text("stale", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd,
            1,
            "",
            "Fatal: Could not connect to the release server to download the manifest.\n",
        )

    monkeypatch.setattr(desktop_backends, "_run_command", failing_script_then_probe)
    monkeypatch.setattr(desktop_backends, "_fetch_github_release", lambda _r, **_kw: _release_payload(asset_name, digest))
    monkeypatch.setattr(
        desktop_backends,
        "_download_release_asset",
        lambda _u, dest, **_kw: dest.write_bytes(source_archive.read_bytes()),
    )

    activated: list[str] = []
    result = desktop_backends.install_desktop_backend("antigravity", base_env=env, activate=activated.append)
    assert result.version == "1.5.16"
    assert activated == [result.path]
    assert not (Path(result.path).parent.parent / "leftover-from-script").exists()

    def failing_github(_r, **_kw):
        raise desktop_backends.DesktopBackendError(
            "Failed to fetch release metadata for google-antigravity/antigravity-cli: HTTP Error 403",
            code="archive_download_failed",
        )

    monkeypatch.setattr(desktop_backends, "_fetch_github_release", failing_github)
    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("antigravity", base_env=env)
    assert raised.value.output is not None
    assert "[official script]" in raised.value.output
    assert "Fatal: Could not connect to the release server" in raised.value.output
    assert "[GitHub release]" in raised.value.output
    assert "HTTP Error 403" in raised.value.output
    assert raised.value.exit_code == 1
