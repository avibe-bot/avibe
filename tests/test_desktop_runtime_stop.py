"""``vibe stop --expect-runtime-id``: a stop that signals only the Runtime it scanned.

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

import psutil
import pytest

from config import paths
from config.v2_config import V2Config
from vibe import cli, desktop_backends, desktop_runtime, remote_access, runtime, ui_server

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
    monkeypatch.setattr(runtime, "stop_service", lambda: False)
    monkeypatch.setattr(runtime, "stop_ui", lambda: False)
    monkeypatch.setattr(runtime, "extra_service_process_pids", lambda owner_pid=None: [])

    assert cli.cmd_stop() == 0

    assert stop_env["stop_pid"] == [opencode.pid]
    assert _alive(opencode)
    assert stop_env["status"] == [("stopped",)]
