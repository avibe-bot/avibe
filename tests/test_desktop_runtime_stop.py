"""A desktop Runtime acts only on processes carrying its id.

``vibe stop --expect-runtime-id`` signals only the Runtime it scanned. A plain
stop or restart from a desktop caller, and a desktop start reusing what already
runs, claim only their own Runtime's processes and otherwise refuse, leaving
everything running.

Targets are real child processes whose environment does or does not carry
``AVIBE_DESKTOP_RUNTIME_ID`` and whose command line, or for a backend
installer whose environment, names the role they stand for. Discovery is
never substituted: the stop finds them by scanning.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from config import paths
from config.v2_config import V2Config
from tests.ui_server_test_helpers import csrf_headers
from vibe import cli, desktop_backends, desktop_runtime, remote_access, restart_supervisor, runtime, ui_server
from vibe.i18n import t as i18n_t

# Random, so no process another test file started with a fixed id is ever in scope.
RUNTIME_ID = secrets.token_hex(32)
OTHER_ID = secrets.token_hex(32)
# Printed once the program is running, so it no longer needs its files.
SLEEP = "import time; print('ready', flush=True); time.sleep(120)"
REPO_ROOT = Path(__file__).resolve().parents[1]
# The arguments the OpenCode server manager appends to the configured executable.
OPENCODE_SERVE = ["serve", "--hostname=127.0.0.1", "--port=4096"]
# Programs that carry the manager's stamp: the server, and the agent work it runs.
OPENCODE_STAMPED = {"opencode", "opencode-native", "opencode-configured", "opencode-agent-work"}

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")


@pytest.fixture
def bundle(tmp_path) -> Path:
    """Files shaped like a desktop bundle, so each role runs with the argv Avibe launches it with."""

    root = tmp_path / "bundle"
    (root / "vibe").mkdir(parents=True)
    (root / "vibe" / "__init__.py").write_text("", encoding="utf-8")
    (root / "vibe" / "service_main.py").write_text(SLEEP + "\n", encoding="utf-8")
    # Next to the entry, as in a bundle: a service found by its record alone
    # is recognised by this.
    (root / "vibe" / "runtime.py").write_text("", encoding="utf-8")
    (root / "vibe" / "ui_server.py").write_text(f"def run_ui_server(host, port):\n    {SLEEP}\n", encoding="utf-8")
    # OpenCode runs natively, through an npm install's `node` shim, or as
    # whatever executable `agents.opencode.cli_path` names; here each is this
    # interpreter under that name.
    (root / "bin").mkdir()
    (root / "bin" / "opencode").write_text(SLEEP + "\n", encoding="utf-8")
    (root / "serve").write_text(SLEEP + "\n", encoding="utf-8")
    for name in ("node", "opencode", "my-opencode", "cloudflared"):
        (root / name).symlink_to(sys.executable)
    return root


@pytest.fixture
def argv_for(bundle):
    def _argv_for(role: str | None) -> list[str]:
        return {
            "service": [sys.executable, str(bundle / "vibe" / "service_main.py")],
            "ui": [sys.executable, "-c", "from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 0)"],
            # An installer is known by its environment, whatever it runs.
            "installer": [sys.executable, "-c", SLEEP, "npm-cli.js", "install"],
            "opencode": [str(bundle / "node"), str(bundle / "bin" / "opencode"), *OPENCODE_SERVE],
            "opencode-native": [str(bundle / "opencode"), *OPENCODE_SERVE],
            "opencode-configured": [str(bundle / "my-opencode"), *OPENCODE_SERVE],
            "connector": [str(bundle / "cloudflared"), "-c", SLEEP, "tunnel", "run"],
            # Other programs whose command lines only mention a role.
            "ui-lookalike": [sys.executable, "-c", SLEEP, "vibe.ui_server", "run_ui_server"],
            "opencode-lookalike": [sys.executable, "-c", SLEEP, "opencode", "serve"],
            "server-lookalike": [sys.executable, "-c", SLEEP, "app.py", *OPENCODE_SERVE],
            "opencode-agent-work": [sys.executable, "-c", SLEEP, "agent-tool"],
            "opencode-server-helper": [sys.executable, "-c", SLEEP, "opencode-server-helper"],
            None: [sys.executable, "-c", SLEEP, "agent-cli"],
        }[role]

    return _argv_for


def _child_env(runtime_id: str | None, role: str | None = None, owner_pid: int | None = None) -> dict[str, str]:
    inherited = (desktop_runtime.DESKTOP_RUNTIME_ID_ENV, desktop_runtime.DESKTOP_ROLE_ENV)
    env = {key: value for key, value in os.environ.items() if key not in inherited}
    if runtime_id is not None:
        env[desktop_runtime.DESKTOP_RUNTIME_ID_ENV] = runtime_id
    if role in OPENCODE_STAMPED:
        env[desktop_runtime.DESKTOP_ROLE_ENV] = desktop_runtime.DESKTOP_OPENCODE_ROLE
    if role == "installer":
        env[desktop_runtime.DESKTOP_ROLE_ENV] = desktop_runtime.DESKTOP_INSTALLER_ROLE
        env[desktop_backends.PROCESS_IDENTITY_ENV] = desktop_backends.new_process_identity_marker()
        # Whoever started the tree owns it: this test, unless another process is named.
        pid = os.getpid() if owner_pid is None else owner_pid
        env[desktop_runtime.DESKTOP_INSTALLER_OWNER_ENV] = f"{pid}:{runtime.process_create_time(pid)!r}"
    return env


@pytest.fixture
def spawn(argv_for, bundle):
    children: list[subprocess.Popen] = []

    def _spawn(
        runtime_id: str | None, role: str | None = "service", owner: subprocess.Popen | None = None
    ) -> subprocess.Popen:
        argv = argv_for(role)
        child = subprocess.Popen(
            argv,
            cwd=bundle,
            env=_child_env(runtime_id, role, None if owner is None else owner.pid),
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        children.append(child)
        assert child.stdout.readline() == "ready\n"
        return child

    yield _spawn
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        child.stdout.close()


def _alive(child: subprocess.Popen) -> bool:
    # A signalled child stays a zombie until this test reaps it.
    return child.poll() is None


@pytest.fixture
def stop_env(monkeypatch):
    """Record every side effect a stop may have."""

    effects: dict[str, list] = {"signals": [], "stop_pid": [], "remote_access": [], "status": []}
    real_kill = os.kill

    def recording_kill(pid, sig):
        if sig != 0:
            effects["signals"].append((pid, sig))
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", recording_kill)
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=5: effects["stop_pid"].append(pid) or False)
    # The OpenCode server is stopped with its tool tree; record that stop the same way.
    from modules.agents.opencode.server import OpenCodeServerManager

    monkeypatch.setattr(
        OpenCodeServerManager,
        "_terminate_pid_tree_sync",
        staticmethod(lambda pid, timeout=5.0: effects["stop_pid"].append(pid) or False),
    )
    monkeypatch.setattr(remote_access, "stop", lambda: effects["remote_access"].append(True) or {"ok": True})
    # The full stop reaps the installers of this process's own Runtime id,
    # which here would be the host's; the scoped stop reaps those of its id.
    reap = desktop_backends.reap_abandoned_desktop_backend_installs
    monkeypatch.setattr(
        desktop_backends,
        "reap_abandoned_desktop_backend_installs",
        lambda runtime_id=None: True if runtime_id is None else reap(runtime_id),
    )
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: effects["status"].append(args))
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    paths.get_logs_dir().mkdir(parents=True, exist_ok=True)
    return effects


def _after_first_scan(monkeypatch, hook) -> list[int]:
    """Run ``hook`` on the processes the stop's first scan found; returns each scan's size."""

    real_scan = runtime.processes_carrying_marker
    scans: list[int] = []

    def scan(fingerprint, **kwargs):
        found = real_scan(fingerprint, **kwargs)
        scans.append(len(found))
        if len(scans) == 1:
            hook(found)
        return found

    monkeypatch.setattr(runtime, "processes_carrying_marker", scan)
    return scans


def _signalled(effects, child: subprocess.Popen) -> bool:
    return any(pid == child.pid for pid, _sig in effects["signals"])


def _opencode_pid_path():
    return paths.get_logs_dir() / "opencode_server.json"


def _stderr_lines(capsys) -> list[str]:
    return capsys.readouterr().err.strip().splitlines()


def _assert_refused_untouched(stop_env, capsys, reason, *children):
    assert json.loads(_stderr_lines(capsys)[-1]) == {"reason": reason}
    assert stop_env["signals"] == []
    assert stop_env["stop_pid"] == []
    assert stop_env["remote_access"] == []
    assert stop_env["status"] == []
    assert all(_alive(child) for child in children)


def test_the_service_ui_installer_and_opencode_carrying_the_id_are_all_stopped(spawn, stop_env, bundle):
    ui = spawn(RUNTIME_ID, "ui")
    # Started by the UI, the installer tree is abandoned once the UI has stopped.
    children = [ui, spawn(RUNTIME_ID, "installer", owner=ui)]
    children += [spawn(RUNTIME_ID, role) for role in ("service", "opencode", "opencode-native", "opencode-configured")]
    # An update may already have replaced or moved the files they started from.
    shutil.rmtree(bundle)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    for child in children:
        child.wait(timeout=10)
        assert (child.pid, signal.SIGTERM) in stop_env["signals"]
    # With no service or UI of this home left, the tunnel connector stops too.
    assert stop_env["remote_access"] == [True]
    assert stop_env["status"] == [("stopped",)]


@pytest.mark.parametrize("foreign_id", [OTHER_ID, None])
def test_processes_without_the_id_are_left_running_whatever_the_pidfiles_say(spawn, stop_env, foreign_id):
    service = spawn(RUNTIME_ID)
    foreign = {role: spawn(foreign_id, role) for role in ("service", "ui", "installer", "opencode")}
    paths.get_runtime_pid_path().write_text(str(foreign["service"].pid), encoding="utf-8")
    paths.get_runtime_ui_pid_path().write_text(str(foreign["ui"].pid), encoding="utf-8")
    _opencode_pid_path().write_text(json.dumps({"pid": foreign["opencode"].pid, "port": 4096}), encoding="utf-8")

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    for child in foreign.values():
        assert _alive(child) and not _signalled(stop_env, child)
    assert stop_env["stop_pid"] == []


def test_a_garbage_opencode_pidfile_does_not_hide_the_opencode_server_carrying_the_id(spawn, stop_env):
    opencode = spawn(RUNTIME_ID, "opencode")
    _opencode_pid_path().write_bytes(b"\x00{not json")

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    opencode.wait(timeout=10)
    assert (opencode.pid, signal.SIGTERM) in stop_env["signals"]


def test_the_stop_and_its_ancestors_are_never_signalled(spawn, bundle, tmp_path):
    # The desktop host starts the stop, so the stop and every process above it
    # carry the id too. Here the parent even looks like this Runtime's UI.
    service, installer = spawn(RUNTIME_ID), spawn(RUNTIME_ID, "installer")
    result_path = tmp_path / "stop-result.json"
    script = (
        "import json, subprocess, sys\n"
        f"stop = subprocess.run([sys.executable, '-m', 'vibe', 'stop', '--expect-runtime-id', {RUNTIME_ID!r}],"
        f" cwd={str(REPO_ROOT)!r}, capture_output=True, text=True)\n"
        f"open({str(result_path)!r}, 'w').write(json.dumps({{'code': stop.returncode, 'stderr': stop.stderr}}))\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", "from vibe.ui_server import run_ui_server; " + script],
        cwd=bundle,
        env=_child_env(RUNTIME_ID),
        start_new_session=True,
    )
    try:
        parent.wait(timeout=60)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)

    # The parent lived to write this, and returned on its own.
    assert parent.returncode == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["code"] == 2
    reported = json.loads(result["stderr"].strip().splitlines()[-1])
    assert reported["failed"] == "ui"
    assert sorted(reported["remaining"], key=lambda item: item["role"]) == [
        {"pid": installer.pid, "role": "installer"},
        {"pid": parent.pid, "role": "ui"},
    ]
    service.wait(timeout=10)
    # Its owner, this test, is still alive.
    assert _alive(installer)


@pytest.mark.parametrize(
    ("role", "failure", "language", "diagnostic"),
    [
        ("service", "service", None, "ERROR: Avibe service did not stop; preserving pidfile and aborting."),
        ("installer", "installer", None, "ERROR: Desktop backend installer processes did not stop."),
        ("opencode", "opencode", None, "ERROR: The OpenCode server this Runtime started did not stop."),
        ("opencode", "opencode", "zh", "错误：此 Runtime 启动的 OpenCode 服务未能停止。"),
    ],
)
def test_a_role_process_carrying_the_id_at_the_rescan_fails_the_stop(
    spawn, stop_env, capsys, monkeypatch, role, failure, language, diagnostic
):
    ui = spawn(RUNTIME_ID, "ui")
    late: list[subprocess.Popen] = []
    # Started after the scan, it is never signalled; the rescan still sees it.
    _after_first_scan(monkeypatch, lambda _found: late.append(spawn(RUNTIME_ID, role)))
    if language is not None:
        paths.get_config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.get_config_path().write_text(json.dumps({"language": language}), encoding="utf-8")

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 2

    ui.wait(timeout=10)
    assert _alive(late[0]) and not _signalled(stop_env, late[0])
    lines = _stderr_lines(capsys)
    assert lines[-2] == diagnostic
    assert json.loads(lines[-1]) == {"failed": failure, "remaining": [{"pid": late[0].pid, "role": role}]}
    assert stop_env["status"] == [("error", cli._STOP_FAILURES[failure][1])]


def test_a_pid_recycled_after_the_scan_is_not_signalled(spawn, stop_env, monkeypatch):
    service = spawn(RUNTIME_ID)

    def recycle(found):
        for process in found:
            if process.pid == service.pid:
                # The scanned process was an earlier holder of this pid.
                process._ident = (process.pid, process.create_time() - 60)

    scans = _after_first_scan(monkeypatch, recycle)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 2

    assert len(scans) == 2
    assert _alive(service) and not _signalled(stop_env, service)


# A role is the argv shape Avibe launches it with, not words its command line contains.
# An OpenCode server's arguments decide only with the manager's stamp, which
# alone decides nothing.
@pytest.mark.parametrize(
    "program",
    [None, "ui-lookalike", "opencode-lookalike", "opencode-server-helper", "server-lookalike", "opencode-agent-work"],
)
def test_other_programs_carrying_the_id_are_reported_and_left_running(spawn, stop_env, capsys, program):
    service, agent = spawn(RUNTIME_ID), spawn(RUNTIME_ID, program)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    assert _alive(agent) and not _signalled(stop_env, agent)
    reported = json.loads(_stderr_lines(capsys)[-1])
    assert reported == {"left_running": [{"pid": agent.pid, "name": psutil.Process(agent.pid).name()}]}
    assert stop_env["status"] == [("stopped",)]


def test_a_foreign_service_lock_holder_refuses_the_stop(spawn, stop_env, capsys):
    foreign = spawn(OTHER_ID)
    lock_path = runtime.get_service_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w", encoding="utf-8") as held:
        assert runtime._try_lock_file(held)
        assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 3

    _assert_refused_untouched(stop_env, capsys, "service_runtime_id_mismatch", foreign)


@pytest.mark.parametrize("late_role", [None, "installer"])
@pytest.mark.parametrize("lock", ["held", "unprobeable"])
def test_a_service_lock_this_runtime_is_not_shown_to_own_keeps_the_shared_status(
    spawn, stop_env, monkeypatch, lock, late_role
):
    # While this Runtime's UI is stopped, a successor's service already holds
    # the lock, or the lock cannot be probed to show that none does.
    ui = spawn(RUNTIME_ID, "ui")
    late: list[subprocess.Popen] = []
    if late_role is not None:
        _after_first_scan(monkeypatch, lambda _found: late.append(spawn(RUNTIME_ID, late_role)))
    lock_path = runtime.get_service_lock_path()

    with contextlib.ExitStack() as stack:
        if lock == "held":
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            assert runtime._try_lock_file(stack.enter_context(lock_path.open("w", encoding="utf-8")))
        else:
            lock_path.mkdir(parents=True)
        # The successor still serves the tunnel; with no holder known, the
        # connector's stop cannot be shown safe, and fails.
        assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == (0 if (lock, late_role) == ("held", None) else 2)

    ui.wait(timeout=10)
    assert stop_env["status"] == []
    assert stop_env["remote_access"] == []


@pytest.mark.parametrize("late_role", [None, "installer"])
def test_no_service_can_take_the_lock_before_the_stop_s_connector_and_status_land(
    spawn, stop_env, monkeypatch, late_role
):
    # A successor's service that takes the lock right after the stop decides
    # the status is its own would publish a status the stop then overwrites,
    # and its UI would reconcile a tunnel the stop then takes down.
    record = cli._write_status
    lock_free_at: list[tuple[str, bool]] = []

    def write(*args, **kwargs):
        lock_free_at.append(("status", runtime.service_instance_lock_available()[0]))
        record(*args, **kwargs)

    def stop_connector():
        lock_free_at.append(("connector", runtime.service_instance_lock_available()[0]))
        return {"ok": True}

    monkeypatch.setattr(cli, "_write_status", write)
    monkeypatch.setattr(remote_access, "stop", stop_connector)
    ui = spawn(RUNTIME_ID, "ui")
    if late_role is not None:
        _after_first_scan(monkeypatch, lambda _found: spawn(RUNTIME_ID, late_role))

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == (0 if late_role is None else 2)

    ui.wait(timeout=10)
    assert len(stop_env["status"]) == 1
    # A failed stop leaves the connector to the Runtime still running.
    expected = [("connector", False), ("status", False)] if late_role is None else [("status", False)]
    assert lock_free_at == expected


def test_the_tunnel_connector_is_left_to_a_ui_that_still_serves_it(spawn, stop_env):
    service = spawn(RUNTIME_ID)
    # Whoever started it, a running UI of this home serves the tunnel.
    other_ui = spawn(None, "ui")
    paths.get_runtime_ui_pid_path().write_text(str(other_ui.pid), encoding="utf-8")

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    assert stop_env["remote_access"] == []
    assert stop_env["status"] == [("stopped",)]


def _connector_stop_raises():
    raise RuntimeError("connector state unreadable")


@pytest.mark.parametrize(
    "connector_stop", [lambda: {"ok": False, "error": "cloudflared_stop_failed"}, _connector_stop_raises]
)
def test_a_tunnel_connector_that_does_not_stop_fails_the_stop(spawn, stop_env, capsys, monkeypatch, connector_stop):
    ui = spawn(RUNTIME_ID, "ui")
    monkeypatch.setattr(remote_access, "stop", connector_stop)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 2

    ui.wait(timeout=10)
    lines = _stderr_lines(capsys)
    assert lines[-2] == "ERROR: The remote access tunnel could not be stopped."
    assert json.loads(lines[-1]) == {"failed": "remote_access", "remaining": []}
    assert stop_env["status"] == [("error", cli._STOP_FAILURES["remote_access"][1])]


def test_quit_takes_the_tunnel_down_and_the_next_ui_start_brings_it_back(spawn, monkeypatch, capsys):
    # Quit and uninstall end with this stop. It stops the connector through its
    # own files and leaves remote access enabled, so the UI of the next launch
    # reconciles the tunnel back.
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *args, **kwargs: None)
    ui, connector = spawn(RUNTIME_ID, "ui"), spawn(RUNTIME_ID, "connector")
    remote_access._pid_path().write_text(str(connector.pid), encoding="utf-8")

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    ui.wait(timeout=10)
    connector.wait(timeout=10)
    assert not remote_access._pid_path().exists()
    # Started by the UI, the connector carries its id, and is gone on return.
    assert not [line for line in _stderr_lines(capsys) if "left_running" in line]
    relaunched = V2Config.load()
    assert relaunched.remote_access.vibe_cloud.enabled
    starts: list[V2Config] = []
    monkeypatch.setattr(remote_access, "start", lambda config=None: starts.append(config) or {"ok": True})

    ui_server._reconcile_remote_access_for_ui_start(relaunched)

    assert starts == [relaunched]


def _lock_path_is_a_directory(monkeypatch):
    runtime.get_service_lock_path().mkdir(parents=True)


def _lock_call_fails(monkeypatch):
    # Not contention: the lock operation itself failed, so no holder is known.
    import fcntl

    def _flock(fd, operation):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(fcntl, "flock", _flock)


@pytest.mark.parametrize("unprobeable", [_lock_path_is_a_directory, _lock_call_fails])
def test_a_service_lock_that_cannot_be_probed_refuses_the_stop(stop_env, capsys, monkeypatch, unprobeable):
    unprobeable(monkeypatch)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 3

    _assert_refused_untouched(stop_env, capsys, "service_identity_unavailable")


def test_invalid_expected_id_is_refused(stop_env, capsys):
    assert cli.cmd_stop(expect_runtime_id="A" * 64) == 3

    _assert_refused_untouched(stop_env, capsys, "invalid_runtime_id")


def test_with_nothing_running_and_a_free_lock_the_stop_succeeds(stop_env):
    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    assert stop_env["signals"] == []
    assert stop_env["remote_access"] == [True]
    assert stop_env["status"] == [("stopped",)]


def test_a_full_stop_keeps_a_surviving_opencode_server_non_fatal(spawn, stop_env, monkeypatch):
    # Only the desktop host replaces the bundle OpenCode runs from; a full stop
    # also serves people and the upgrade and restart flows, and keeps its exit.
    opencode = spawn(RUNTIME_ID, "opencode")
    _opencode_pid_path().write_text(json.dumps({"pid": opencode.pid, "port": 4096}), encoding="utf-8")
    monkeypatch.setattr(runtime, "stop_service", lambda **kwargs: False)
    monkeypatch.setattr(runtime, "stop_ui", lambda **kwargs: False)
    monkeypatch.setattr(runtime, "extra_service_process_pids", lambda owner_pid=None: [])

    assert cli.cmd_stop() == 0

    assert stop_env["stop_pid"] == [opencode.pid]
    assert _alive(opencode)
    assert stop_env["status"] == [("stopped",)]


@contextlib.contextmanager
def _service_lock_held_for(child: subprocess.Popen | None):
    """Hold the service lock with ``child`` as its recorded holder, as a running service does.

    With no ``child`` the lock is held and nothing names its holder.
    """

    lock_path = runtime.get_service_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as held:
        assert runtime._try_lock_file(held)
        if child is not None:
            held.write(json.dumps({"pid": child.pid}))
            held.flush()
            paths.get_runtime_pid_path().write_text(str(child.pid), encoding="utf-8")
        yield


@pytest.fixture
def caller(monkeypatch, tmp_path):
    """Give this process a desktop provenance: the id in its environment, and the tree it runs from.

    Spawn children first: the tree's interpreter becomes ``sys.executable``.
    """

    paths.ensure_data_dirs()

    def _caller(env_id: str | None, root_id: str | None = None) -> None:
        if env_id is None:
            monkeypatch.delenv(desktop_runtime.DESKTOP_RUNTIME_ID_ENV, raising=False)
        else:
            monkeypatch.setenv(desktop_runtime.DESKTOP_RUNTIME_ID_ENV, env_id)
        if root_id is not None:
            root = tmp_path / "install" / "3.0.0" / root_id[:16]
            (root / "python" / "bin").mkdir(parents=True)
            (root / "python" / "bin" / "python3").write_bytes(b"")
            marker = {"archive_sha256": root_id, "python_entrypoint": "python/bin/python3"}
            (root / ".avibe-runtime.json").write_text(json.dumps(marker), encoding="utf-8")
            monkeypatch.setattr(sys, "executable", str(root / "python" / "bin" / "python3"))

    return _caller


@pytest.fixture
def running_runtime(spawn):
    """This Runtime's service holding the lock, and its UI on record."""

    paths.ensure_data_dirs()
    service = spawn(RUNTIME_ID)
    ui = spawn(RUNTIME_ID, "ui")
    paths.get_runtime_ui_pid_path().write_text(str(ui.pid), encoding="utf-8")
    with _service_lock_held_for(service):
        yield service, ui


@pytest.mark.parametrize(
    ("env_id", "root_id", "refused"),
    [
        pytest.param(OTHER_ID, OTHER_ID, True, id="env-A-root-A"),
        pytest.param(RUNTIME_ID, RUNTIME_ID, False, id="env-B-root-B"),
        pytest.param(RUNTIME_ID, OTHER_ID, True, id="env-B-root-A"),
        pytest.param(None, OTHER_ID, True, id="root-A"),
        pytest.param(None, RUNTIME_ID, False, id="root-B"),
        pytest.param(None, None, False, id="no-provenance"),
    ],
)
def test_a_desktop_caller_acts_only_for_the_runtime_it_came_from(running_runtime, caller, env_id, root_id, refused):
    caller(env_id, root_id)

    refusal = runtime.desktop_provenance_refusal()

    if refused:
        assert (refusal.part, refusal.reason) == ("service", "runtime_id_mismatch")
    else:
        assert refusal is None


@pytest.mark.parametrize(
    ("service_id", "ui_id", "include_ui", "expected"),
    [
        pytest.param(None, RUNTIME_ID, True, ("service", "runtime_id_mismatch"), id="id-less-service"),
        pytest.param(RUNTIME_ID, OTHER_ID, True, ("ui", "runtime_id_mismatch"), id="foreign-ui"),
        pytest.param(RUNTIME_ID, None, True, ("ui", "runtime_id_mismatch"), id="id-less-ui"),
        # A restart of the service alone leaves the UI where it is.
        pytest.param(RUNTIME_ID, OTHER_ID, False, None, id="service-scope-foreign-ui"),
    ],
)
def test_every_part_a_desktop_caller_would_act_on_must_be_its_own(
    spawn, caller, service_id, ui_id, include_ui, expected
):
    service = spawn(service_id)
    ui = spawn(ui_id, "ui")
    paths.get_runtime_ui_pid_path().write_text(str(ui.pid), encoding="utf-8")
    caller(RUNTIME_ID)

    with _service_lock_held_for(service):
        refusal = runtime.desktop_provenance_refusal(include_ui=include_ui)

    assert (None if refusal is None else (refusal.part, refusal.reason)) == expected


def test_a_desktop_caller_that_cannot_read_the_service_refuses(running_runtime, caller, monkeypatch):
    caller(RUNTIME_ID)
    service, _ui = running_runtime
    real_environ = psutil.Process.environ

    def environ(process):
        if process.pid == service.pid:
            raise psutil.AccessDenied(process.pid)
        return real_environ(process)

    monkeypatch.setattr(psutil.Process, "environ", environ)

    refusal = runtime.desktop_provenance_refusal()

    assert (refusal.part, refusal.reason) == ("service", "identity_unavailable")


def test_with_nothing_running_a_desktop_caller_may_proceed(caller):
    caller(OTHER_ID, OTHER_ID)

    assert runtime.desktop_provenance_refusal() is None


def test_a_plain_stop_from_another_desktop_runtime_stops_nothing(running_runtime, caller, stop_env, capsys):
    caller(OTHER_ID)

    assert cli.cmd_stop() == 3

    assert _stderr_lines(capsys)[-1] == i18n_t(
        "desktopRuntime.stopRefused", "en", part="service", reason="runtime_id_mismatch"
    )
    assert stop_env["signals"] == []
    assert stop_env["stop_pid"] == []
    assert stop_env["remote_access"] == []
    assert stop_env["status"] == []
    assert all(_alive(child) for child in running_runtime)


def test_a_plain_stop_refuses_while_nothing_names_the_service_lock_holder(spawn, caller, stop_env, capsys):
    # The UI and the tunnel connector serve whichever service holds the lock.
    ui = spawn(RUNTIME_ID, "ui")
    paths.get_runtime_ui_pid_path().write_text(str(ui.pid), encoding="utf-8")
    caller(RUNTIME_ID)

    with _service_lock_held_for(None):
        assert cli.cmd_stop() == 3

    assert _stderr_lines(capsys)[-1] == i18n_t(
        "desktopRuntime.stopRefused", "en", part="service", reason="identity_unavailable"
    )
    assert stop_env["signals"] == []
    assert stop_env["stop_pid"] == []
    assert stop_env["remote_access"] == []
    assert stop_env["status"] == []
    assert _alive(ui)


@pytest.mark.parametrize("server_id", [RUNTIME_ID, OTHER_ID])
def test_a_plain_stop_stops_only_its_own_runtime_s_opencode_server(spawn, caller, stop_env, server_id):
    # The server record may still name one another Runtime's service started.
    server = spawn(server_id, "opencode")
    _opencode_pid_path().write_text(json.dumps({"pid": server.pid}), encoding="utf-8")
    caller(RUNTIME_ID)

    assert cli.cmd_stop() == 0

    assert stop_env["stop_pid"] == ([server.pid] if server_id == RUNTIME_ID else [])
    assert stop_env["status"] == [("stopped",)]
    assert _alive(server)


@pytest.mark.parametrize("entry", ["stop", "restart-job"])
def test_a_stop_from_a_runtime_s_own_tree_reaps_its_abandoned_installers(spawn, caller, stop_env, monkeypatch, entry):
    # Only the interpreter names the Runtime; the environment names none.
    owner = spawn(RUNTIME_ID, "ui")
    installer = spawn(RUNTIME_ID, "installer", owner=owner)
    owner.kill()
    owner.wait(timeout=10)
    caller(None, RUNTIME_ID)

    if entry == "stop":
        assert cli.cmd_stop() == 0
    else:
        reaped_before_the_successor = []

        def start_runtime_processes(**kwargs):
            reaped_before_the_successor.append(_signalled(stop_env, installer))
            raise RuntimeError("the successor is not started here")

        monkeypatch.setattr(
            restart_supervisor, "_stop_runtime_for_restart", lambda **kwargs: (True, {}, 0.0, None, True, 0.0)
        )
        monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", start_runtime_processes)
        restart_supervisor._run_restart_job(job_id="jobreap", delay_seconds=0, vibe_path=None, trigger="test")
        assert reaped_before_the_successor == [True]

    assert (installer.pid, signal.SIGTERM) in stop_env["signals"]


def test_a_plain_restart_from_another_desktop_runtime_schedules_nothing(
    running_runtime, caller, stop_env, capsys, monkeypatch
):
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: pytest.fail("no restart may be scheduled"))
    caller(None, OTHER_ID)

    assert cli.cmd_restart() == 3

    assert _stderr_lines(capsys)[-1] == i18n_t(
        "desktopRuntime.restartRefused", "en", part="service", reason="runtime_id_mismatch"
    )
    assert stop_env["signals"] == []
    assert all(_alive(child) for child in running_runtime)


def test_a_restart_job_for_another_desktop_runtime_fails_before_stopping_anything(
    running_runtime, caller, stop_env, monkeypatch
):
    # The job acts on whatever runs when it starts, whoever scheduled it.
    monkeypatch.setattr(
        restart_supervisor, "_stop_runtime_for_restart", lambda **kwargs: pytest.fail("nothing may be stopped")
    )
    caller(OTHER_ID)

    rc = restart_supervisor._run_restart_job(job_id="jobrefused", delay_seconds=0, vibe_path=None, trigger="test")

    assert rc == 3
    status = runtime.read_json(runtime.get_restart_status_path())
    assert (status["ok"], status["state"]) == (False, "failed")
    assert status["error"].startswith("restart refused: ")
    assert stop_env["signals"] == []
    assert all(_alive(child) for child in running_runtime)


def test_a_web_restart_from_another_desktop_runtime_is_refused_and_not_announced(running_runtime, caller, monkeypatch):
    monkeypatch.setattr(
        restart_supervisor, "schedule_restart", lambda **kwargs: pytest.fail("no restart may be scheduled")
    )
    runtime.write_status("running", "running", running_runtime[0].pid, running_runtime[1].pid)
    caller(OTHER_ID)
    client = ui_server.app.test_client()

    response = client.post("/api/control", json={"action": "restart"}, headers=csrf_headers(client))

    assert response.status_code == 409
    payload = response.get_json()
    assert (payload["code"], payload["part"], payload["reason"]) == (
        "restart_refused",
        "service",
        "runtime_id_mismatch",
    )
    assert runtime.read_status()["state"] == "running"
    assert all(_alive(child) for child in running_runtime)


def test_a_config_restart_from_another_desktop_runtime_says_it_was_refused(running_runtime, caller, monkeypatch):
    monkeypatch.setattr(
        restart_supervisor, "schedule_restart", lambda **kwargs: pytest.fail("no restart may be scheduled")
    )
    runtime.write_status("running", "running", running_runtime[0].pid, running_runtime[1].pid)
    caller(OTHER_ID)

    result = ui_server._schedule_service_restart_for_config_fallback()

    assert (result["ok"], result["code"]) == (False, "restart_refused")
    assert runtime.read_status()["state"] == "running"


@pytest.mark.parametrize(
    ("caller_id", "service_id", "outcome"),
    [
        pytest.param(RUNTIME_ID, OTHER_ID, "runtime_id_mismatch", id="foreign"),
        pytest.param(RUNTIME_ID, None, "runtime_id_mismatch", id="id-less"),
        pytest.param(RUNTIME_ID, RUNTIME_ID, "reused", id="own"),
        # A start that is not a desktop Runtime's reuses whatever runs, as it always has.
        pytest.param(None, OTHER_ID, "reused", id="no-desktop-id"),
    ],
)
def test_a_desktop_start_reuses_only_its_own_runtime_s_service(spawn, stop_env, caller, caller_id, service_id, outcome):
    service = spawn(service_id)
    caller(caller_id)
    info = runtime.ProcessStartInfo()

    with _service_lock_held_for(service):
        if outcome == "reused":
            assert runtime.start_service(wait_for_ready=False, start_info=info) == service.pid
            assert (info.pid, info.reused) == (service.pid, True)
        else:
            with pytest.raises(runtime.DesktopRuntimeClaimRefused) as refused:
                runtime.start_service(wait_for_ready=False, start_info=info)
            assert (refused.value.part, refused.value.reason) == ("service", outcome)
            assert info.pid is None

    assert stop_env["signals"] == []
    assert _alive(service)


@pytest.mark.parametrize("record", ["missing", "corrupt", "another-program"])
def test_a_desktop_start_names_the_lock_holder_whatever_the_service_record_says(spawn, stop_env, caller, record):
    foreign = spawn(OTHER_ID)
    program = spawn(RUNTIME_ID, None)
    caller(RUNTIME_ID)

    with _service_lock_held_for(foreign):
        pid_path = paths.get_runtime_pid_path()
        if record == "missing":
            pid_path.unlink()
        elif record == "corrupt":
            pid_path.write_text("not a pid", encoding="utf-8")
        else:
            pid_path.write_text(str(program.pid), encoding="utf-8")
        with pytest.raises(runtime.DesktopRuntimeClaimRefused) as refused:
            runtime.start_service(wait_for_ready=False, start_info=runtime.ProcessStartInfo())

    assert (refused.value.part, refused.value.reason) == ("service", "runtime_id_mismatch")
    assert stop_env["signals"] == []
    assert _alive(foreign)


@pytest.mark.parametrize("ui_id", [OTHER_ID, None, "unreadable"])
def test_a_desktop_start_neither_reuses_nor_replaces_a_ui_of_another_runtime(
    spawn, stop_env, caller, monkeypatch, ui_id
):
    ui = spawn(None if ui_id == "unreadable" else ui_id, "ui")
    paths.get_runtime_ui_pid_path().write_text(str(ui.pid), encoding="utf-8")
    if ui_id == "unreadable":
        real_command = runtime.get_process_command
        monkeypatch.setattr(runtime, "get_process_command", lambda pid: None if pid == ui.pid else real_command(pid))
    caller(RUNTIME_ID)

    with pytest.raises(runtime.DesktopRuntimeClaimRefused) as refused:
        runtime.start_ui("127.0.0.1", 5123, start_info=runtime.ProcessStartInfo())

    reason = "identity_unavailable" if ui_id == "unreadable" else "runtime_id_mismatch"
    assert (refused.value.part, refused.value.reason) == ("ui", reason)
    assert stop_env["signals"] == []
    assert stop_env["stop_pid"] == []
    assert _alive(ui)


def _start_config(monkeypatch):
    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
    )
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_handover_superseded_desktop_runtime", lambda **kwargs: 0)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda cfg: "127.0.0.1")


@pytest.mark.parametrize(
    ("found", "part", "reason"),
    [
        pytest.param("service", "service", "runtime_id_mismatch", id="foreign-service"),
        pytest.param("lock-holder", "service", "identity_unavailable", id="unnamed-lock-holder"),
        pytest.param("ui", "ui", "runtime_id_mismatch", id="foreign-ui"),
    ],
)
def test_a_refused_desktop_start_leaves_everything_as_it_found_it(
    spawn, stop_env, caller, capsys, monkeypatch, found, part, reason
):
    # Refused before anything is announced: nothing is started or stopped, and
    # the status another Runtime published stays as it is.
    # With the lock held and no holder named, no service process is running either.
    foreign = spawn(OTHER_ID, {"service": "service", "lock-holder": None, "ui": "ui"}[found])
    _start_config(monkeypatch)
    monkeypatch.setattr(runtime, "start_service", lambda **kwargs: pytest.fail("no service may be started"))
    monkeypatch.setattr(runtime, "start_ui", lambda *args, **kwargs: pytest.fail("no UI may be started"))
    caller(RUNTIME_ID)
    status_path = paths.get_runtime_status_path()

    with contextlib.ExitStack() as held:
        if found == "ui":
            paths.get_runtime_ui_pid_path().write_text(str(foreign.pid), encoding="utf-8")
            runtime.write_status("running", "started", None, foreign.pid)
        else:
            held.enter_context(_service_lock_held_for(foreign if found == "service" else None))
            runtime.write_status("running", f"pid={foreign.pid}", foreign.pid, None)
        recorded = status_path.read_bytes()
        assert cli.cmd_start(open_browser=False) == 3

    assert _stderr_lines(capsys)[-1] == i18n_t("desktopRuntime.claimRefused", "en", part=part, reason=reason)
    assert stop_env["status"] == []
    assert status_path.read_bytes() == recorded
    assert stop_env["signals"] == []
    assert stop_env["stop_pid"] == []
    assert _alive(foreign)


def test_a_ui_record_naming_another_program_does_not_refuse_a_desktop_start(spawn, stop_env, caller):
    # Quit leaves the UI record behind, and its pid may since name any program.
    program = spawn(OTHER_ID, None)
    paths.get_runtime_ui_pid_path().write_text(str(program.pid), encoding="utf-8")
    caller(RUNTIME_ID)

    runtime.claim_desktop_runtime_start()

    assert stop_env["signals"] == []
    assert _alive(program)


def test_a_desktop_start_whose_service_lost_the_lock_to_another_runtime_rolls_back_only_its_own(
    spawn, stop_env, caller, capsys, monkeypatch
):
    # This start spawned its service and UI, but another Runtime's service took
    # the lock first. The start stops the two it spawned, and not the one that
    # took the lock.
    own_service = spawn(RUNTIME_ID)
    own_ui = spawn(RUNTIME_ID, "ui")
    foreign: list[subprocess.Popen] = []
    _start_config(monkeypatch)
    held = contextlib.ExitStack()

    def start_service(**kwargs):
        paths.get_runtime_pid_path().write_text(str(own_service.pid), encoding="utf-8")
        foreign.append(spawn(OTHER_ID))
        held.enter_context(_service_lock_held_for(foreign[0]))
        return kwargs["start_info"].capture(own_service.pid, reused=False)

    def start_ui(*args, start_info, **kwargs):
        paths.get_runtime_ui_pid_path().write_text(str(own_ui.pid), encoding="utf-8")
        return start_info.capture(own_ui.pid, reused=False)

    monkeypatch.setattr(runtime, "start_service", start_service)
    monkeypatch.setattr(runtime, "start_ui", start_ui)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout: foreign[0].pid)
    caller(RUNTIME_ID)

    with held:
        assert cli.cmd_start(open_browser=False) == 3

    assert _stderr_lines(capsys)[-1] == i18n_t(
        "desktopRuntime.claimRefused", "en", part="service", reason="runtime_id_mismatch"
    )
    assert stop_env["stop_pid"] == [own_ui.pid, own_service.pid]
    assert not _signalled(stop_env, foreign[0])
    assert _alive(foreign[0])


def test_a_failed_desktop_start_undoes_nothing_another_runtime_started_meanwhile(spawn, stop_env, caller, monkeypatch):
    # This start spawned its service and UI; before it finished, another
    # Runtime's UI replaced the UI record. Undoing the start stops the UI it
    # spawned and leaves the one on record.
    own_service = spawn(RUNTIME_ID)
    own_ui = spawn(RUNTIME_ID, "ui")
    foreign_ui = spawn(OTHER_ID, "ui")
    _start_config(monkeypatch)

    def start_service(**kwargs):
        paths.get_runtime_pid_path().write_text(str(own_service.pid), encoding="utf-8")
        return kwargs["start_info"].capture(own_service.pid, reused=False)

    def start_ui(*args, start_info, **kwargs):
        paths.get_runtime_ui_pid_path().write_text(str(own_ui.pid), encoding="utf-8")
        return start_info.capture(own_ui.pid, reused=False)

    def wait_for_service_ready(pid, timeout):
        paths.get_runtime_ui_pid_path().write_text(str(foreign_ui.pid), encoding="utf-8")
        raise RuntimeError("the readiness wait failed")

    monkeypatch.setattr(runtime, "start_service", start_service)
    monkeypatch.setattr(runtime, "start_ui", start_ui)
    monkeypatch.setattr(runtime, "wait_for_service_ready", wait_for_service_ready)
    caller(RUNTIME_ID)

    with pytest.raises(RuntimeError, match="the readiness wait failed"):
        cli.cmd_start(open_browser=False)

    assert stop_env["stop_pid"] == [own_ui.pid, own_service.pid]
    assert not _signalled(stop_env, foreign_ui)
    assert _alive(foreign_ui)


@pytest.mark.parametrize("taken_over", ["service", "ui"])
def test_a_stop_asks_again_of_every_process_it_is_about_to_signal(
    spawn, stop_env, caller, capsys, monkeypatch, taken_over
):
    # Another Runtime replaced a part after the stop's first check passed.
    service = spawn(OTHER_ID if taken_over == "service" else RUNTIME_ID)
    ui = spawn(OTHER_ID if taken_over == "ui" else RUNTIME_ID, "ui")
    paths.get_runtime_ui_pid_path().write_text(str(ui.pid), encoding="utf-8")
    monkeypatch.setattr(runtime, "desktop_provenance_refusal", lambda **kwargs: None)
    caller(RUNTIME_ID)

    with _service_lock_held_for(service):
        assert cli.cmd_stop() == 3

    assert _stderr_lines(capsys)[-1] == i18n_t(
        "desktopRuntime.stopRefused", "en", part=taken_over, reason="runtime_id_mismatch"
    )
    foreign = service if taken_over == "service" else ui
    assert not _signalled(stop_env, foreign)
    assert _alive(foreign)
    # Nor is the tunnel connector the other Runtime's UI serves stopped.
    assert stop_env["remote_access"] == []
    assert stop_env["status"] == []
    assert stop_env["stop_pid"] == ([] if taken_over == "service" else [service.pid])


def test_a_restart_job_stops_nothing_of_a_runtime_that_took_over_after_its_check(
    spawn, stop_env, caller, monkeypatch
):
    foreign = spawn(OTHER_ID)
    monkeypatch.setattr(runtime, "desktop_provenance_refusal", lambda **kwargs: None)
    caller(RUNTIME_ID)

    with _service_lock_held_for(foreign):
        rc = restart_supervisor._run_restart_job(job_id="jobtakenover", delay_seconds=0, vibe_path=None, trigger="test")

    assert rc == 3
    status = runtime.read_json(runtime.get_restart_status_path())
    assert (status["ok"], status["state"]) == (False, "failed")
    assert status["error"].startswith("restart refused: ")
    assert stop_env["stop_pid"] == []
    assert not _signalled(stop_env, foreign)
    assert _alive(foreign)


@pytest.mark.parametrize("taken_over", ["service", "ui"])
def test_a_restart_job_s_successor_claims_only_its_own_runtime_and_undoes_what_it_started(
    spawn, stop_env, caller, monkeypatch, taken_over
):
    # Another Runtime came up while this job restarted: its UI is on record
    # when the successor starts, or its service takes the lock while the
    # successor is starting. The job neither adopts nor stops it, and stops
    # only what it started itself.
    caller(RUNTIME_ID)
    # The job runs from this Runtime's own tree, so its successor carries the id.
    monkeypatch.setenv(desktop_runtime.DESKTOP_RUNTIME_ROOT_ENV, str(Path(sys.executable).resolve().parent))
    started: dict[str, subprocess.Popen] = {}
    held = contextlib.ExitStack()

    def stop_runtime_for_restart(**kwargs):
        # The Runtime it replaces has stopped.
        if taken_over == "ui":
            started["foreign"] = spawn(OTHER_ID, "ui")
            paths.get_runtime_ui_pid_path().write_text(str(started["foreign"].pid), encoding="utf-8")
        return True, {}, 0.0, None, True, 0.0

    def start_service(**kwargs):
        if taken_over == "ui":
            pytest.fail("no service may be started")
        started["service"] = spawn(RUNTIME_ID)
        paths.get_runtime_pid_path().write_text(str(started["service"].pid), encoding="utf-8")
        started["foreign"] = spawn(OTHER_ID)
        held.enter_context(_service_lock_held_for(started["foreign"]))
        return kwargs["start_info"].capture(started["service"].pid, reused=False)

    def start_ui(*args, **kwargs):
        started["ui"] = spawn(RUNTIME_ID, "ui")
        paths.get_runtime_ui_pid_path().write_text(str(started["ui"].pid), encoding="utf-8")
        return kwargs["start_info"].capture(started["ui"].pid, reused=False)

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", stop_runtime_for_restart)
    monkeypatch.setattr(runtime, "start_service", start_service)
    monkeypatch.setattr(runtime, "start_ui", start_ui)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout: started["foreign"].pid)
    runtime.write_status("stopped", "stopped", None, None)
    status_path = paths.get_runtime_status_path()
    recorded = status_path.read_bytes()

    with held:
        rc = restart_supervisor._run_restart_job(
            job_id=f"jobsuccessor{taken_over}", delay_seconds=0, vibe_path=None, trigger="test"
        )

        assert rc == 3
        status = runtime.read_json(runtime.get_restart_status_path())
        assert (status["ok"], status["state"]) == (False, "failed")
        assert status["error"].startswith("restart refused: ")
        if taken_over == "service":
            assert stop_env["stop_pid"] == [started["ui"].pid, started["service"].pid]
        else:
            # Refused before the successor announced anything.
            assert stop_env["stop_pid"] == []
            assert status_path.read_bytes() == recorded
        assert not _signalled(stop_env, started["foreign"])
        assert _alive(started["foreign"])


def test_a_web_start_against_another_runtime_s_service_says_it_was_refused(spawn, stop_env, caller):
    foreign = spawn(OTHER_ID)
    caller(RUNTIME_ID)
    client = ui_server.app.test_client()

    with _service_lock_held_for(foreign):
        response = client.post("/api/control", json={"action": "start"}, headers=csrf_headers(client))

    assert response.status_code == 409
    payload = response.get_json()
    assert (payload["code"], payload["part"], payload["reason"]) == (
        "start_refused",
        "service",
        "runtime_id_mismatch",
    )
    assert stop_env["signals"] == []
    assert _alive(foreign)
