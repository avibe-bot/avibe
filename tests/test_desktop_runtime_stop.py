"""``vibe stop --expect-runtime-id``: a stop that signals only the Runtime it verified.

Targets are real child processes whose environment does or does not carry
``AVIBE_DESKTOP_RUNTIME_ID`` and whose command line names the program they
stand for; only how the service is *found* (the lock owner) is substituted.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace

import psutil
import pytest

from config import paths
from core.process_isolation import fingerprint_process_marker
from vibe import cli, desktop_backends, desktop_runtime, remote_access, runtime

RUNTIME_ID = "a" * 64
OTHER_ID = "b" * 64
UI = ("vibe.ui_server", "run_ui_server")
OPENCODE = ("opencode", "serve")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")


@pytest.fixture
def spawn():
    children: list[subprocess.Popen] = []

    def _spawn(runtime_id: str | None, *program: str) -> subprocess.Popen:
        env = {key: value for key, value in os.environ.items() if key != desktop_runtime.DESKTOP_RUNTIME_ID_ENV}
        if runtime_id is not None:
            env[desktop_runtime.DESKTOP_RUNTIME_ID_ENV] = runtime_id
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", *program],
            env=env,
            start_new_session=True,
        )
        children.append(child)
        return child

    yield _spawn
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def _alive(child: subprocess.Popen) -> bool:
    # A signalled child stays a zombie until this test reaps it.
    return child.poll() is None


@pytest.fixture
def stop_env(monkeypatch):
    """Record every side effect a stop may have; point resolution at test children."""

    effects: dict[str, list] = {"signals": [], "stop_pid": [], "remote_access": [], "reaps": [], "status": []}
    real_kill = os.kill

    def recording_kill(pid, sig):
        if sig != 0:
            effects["signals"].append((pid, sig))
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", recording_kill)
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=5: effects["stop_pid"].append(pid) or False)
    monkeypatch.setattr(remote_access, "stop", lambda: effects["remote_access"].append(True) or {"ok": True})
    monkeypatch.setattr(
        desktop_backends,
        "reap_abandoned_desktop_backend_installs",
        lambda *, runtime_id=None: effects["reaps"].append(runtime_id) or True,
    )
    monkeypatch.setattr(cli, "_write_status", lambda *args, **kwargs: effects["status"].append(args))
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    paths.get_logs_dir().mkdir(parents=True, exist_ok=True)

    def targets(
        service: subprocess.Popen | None,
        ui: subprocess.Popen | None = None,
        *,
        services=None,
        opencode: subprocess.Popen | None = None,
    ):
        resolved = iter(services) if services is not None else None

        def resolve_service_owner_pid(*, include_starting=True):
            if resolved is not None:
                return next(resolved).pid
            return None if service is None else service.pid

        monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_service_owner_pid)
        if ui is not None:
            paths.get_runtime_ui_pid_path().write_text(str(ui.pid), encoding="utf-8")
        if opencode is not None:
            _opencode_pid_path().write_text(json.dumps({"pid": opencode.pid, "port": 4096}), encoding="utf-8")

    effects["targets"] = targets
    return effects


def _signalled(effects, child: subprocess.Popen) -> bool:
    return any(pid == child.pid for pid, _sig in effects["signals"])


def _opencode_pid_path():
    return paths.get_logs_dir() / "opencode_server.json"


def _last_stderr_json(capsys):
    return json.loads(capsys.readouterr().err.strip().splitlines()[-1])


def test_matching_service_ui_and_opencode_are_all_stopped(spawn, stop_env):
    service, ui, opencode = spawn(RUNTIME_ID), spawn(RUNTIME_ID, *UI), spawn(RUNTIME_ID, *OPENCODE)
    stop_env["targets"](service, ui, opencode=opencode)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    for child in (service, ui, opencode):
        child.wait(timeout=10)
        assert (child.pid, signal.SIGTERM) in stop_env["signals"]
    assert stop_env["remote_access"] == [True]
    assert stop_env["reaps"] == [RUNTIME_ID]
    assert stop_env["status"] == [("stopped",)]
    assert not paths.get_runtime_ui_pid_path().exists()
    assert not _opencode_pid_path().exists()


@pytest.mark.parametrize(
    ("service_id", "reason"),
    [
        (OTHER_ID, "service_runtime_id_mismatch"),
        (None, "service_runtime_id_mismatch"),
        ("access-denied", "service_identity_unavailable"),
    ],
)
def test_unverified_service_refuses_the_whole_stop(spawn, stop_env, monkeypatch, capsys, service_id, reason):
    service = spawn(RUNTIME_ID if service_id == "access-denied" else service_id)
    ui, opencode = spawn(RUNTIME_ID, *UI), spawn(RUNTIME_ID, *OPENCODE)
    stop_env["targets"](service, ui, opencode=opencode)
    if service_id == "access-denied":
        real_open = desktop_runtime.open_desktop_runtime_provenance

        def open_provenance(pid):
            if pid == service.pid:
                raise psutil.AccessDenied(pid)
            return real_open(pid)

        monkeypatch.setattr(desktop_runtime, "open_desktop_runtime_provenance", open_provenance)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 3

    assert _last_stderr_json(capsys) == {"reason": reason}
    assert stop_env["signals"] == []
    assert stop_env["stop_pid"] == []
    assert stop_env["remote_access"] == []
    assert stop_env["reaps"] == []
    assert stop_env["status"] == []
    assert _alive(service) and _alive(ui) and _alive(opencode)


def test_invalid_expected_id_is_refused(stop_env, capsys):
    stop_env["targets"](None)

    assert cli.cmd_stop(expect_runtime_id="A" * 64) == 3

    assert _last_stderr_json(capsys) == {"reason": "invalid_runtime_id"}
    assert stop_env["remote_access"] == [] and stop_env["reaps"] == []


@pytest.mark.parametrize("service_running", [False, True])
def test_without_a_ui_remote_access_is_stopped_only_through_a_verified_service(spawn, stop_env, service_running):
    service = spawn(RUNTIME_ID) if service_running else None
    stop_env["targets"](service)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    # With nothing of this Runtime running there is no owner to stop the tunnel
    # through: another Runtime may be between restarts and about to adopt it.
    assert stop_env["remote_access"] == ([True] if service_running else [])
    assert stop_env["reaps"] == [RUNTIME_ID]
    assert stop_env["status"] == [("stopped",)]


def test_a_service_resolved_after_the_check_is_never_signalled(spawn, stop_env):
    verified, successor = spawn(RUNTIME_ID), spawn(RUNTIME_ID)
    stop_env["targets"](None, services=[verified, successor, successor, successor])

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    verified.wait(timeout=10)
    assert _alive(successor)
    assert not _signalled(stop_env, successor)


def test_a_pid_recycled_after_the_check_is_not_signalled(spawn, stop_env, monkeypatch):
    service = spawn(RUNTIME_ID)
    stop_env["targets"](service)
    real_open = desktop_runtime.open_desktop_runtime_provenance
    opened: list[int] = []

    def open_provenance(pid):
        process, identity = real_open(pid)
        opened.append(pid)
        if len(opened) == 1:
            return process, identity
        # From here on the pid is held by a stranger born later, without the id.
        return process, replace(identity, create_time=identity.create_time + 60, worker_fingerprint=None)

    monkeypatch.setattr(desktop_runtime, "open_desktop_runtime_provenance", open_provenance)

    cli.cmd_stop(expect_runtime_id=RUNTIME_ID)

    assert len(opened) >= 2
    assert not _signalled(stop_env, service)
    assert _alive(service)


def test_extra_service_processes_are_left_to_a_full_stop(spawn, stop_env, monkeypatch):
    service, extra = spawn(RUNTIME_ID), spawn(RUNTIME_ID)
    stop_env["targets"](service)
    monkeypatch.setattr(runtime, "extra_service_process_pids", lambda owner_pid=None: [extra.pid])

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    assert _alive(extra)
    assert not _signalled(stop_env, extra)


@pytest.mark.parametrize(
    ("ui_id", "reason"),
    [
        (OTHER_ID, "ui_runtime_id_mismatch"),
        (None, "ui_runtime_id_mismatch"),
        ("command-unreadable", "ui_identity_unavailable"),
    ],
)
def test_matching_service_with_an_unverified_ui_stops_only_the_service(
    spawn, stop_env, monkeypatch, capsys, ui_id, reason
):
    service = spawn(RUNTIME_ID)
    ui = spawn(RUNTIME_ID if ui_id == "command-unreadable" else ui_id, *UI)
    stop_env["targets"](service, ui)
    if ui_id == "command-unreadable":
        real_command = runtime.get_process_command
        monkeypatch.setattr(runtime, "get_process_command", lambda pid: None if pid == ui.pid else real_command(pid))

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    assert _alive(ui)
    assert not _signalled(stop_env, ui)
    assert _last_stderr_json(capsys) == {"skipped": "ui", "reason": reason}
    # Remote access and backend installs belong to the UI that is left running.
    assert stop_env["remote_access"] == []
    assert stop_env["reaps"] == []
    assert paths.get_runtime_ui_pid_path().read_text(encoding="utf-8") == str(ui.pid)


@pytest.mark.parametrize("had_ui", [True, False])
def test_a_ui_that_took_the_pidfile_during_the_stop_keeps_its_remote_access(
    spawn, stop_env, monkeypatch, capsys, had_ui
):
    service = spawn(RUNTIME_ID)
    verified_ui = spawn(RUNTIME_ID, *UI) if had_ui else None
    stop_env["targets"](service, verified_ui)
    successor: list[subprocess.Popen] = []
    real_clear = runtime._clear_service_pid_reservation

    def service_stopped_while_a_ui_took_over(pid):
        real_clear(pid)
        if verified_ui is not None:
            verified_ui.kill()
            verified_ui.wait(timeout=10)
        successor.append(spawn(OTHER_ID, *UI))
        paths.get_runtime_ui_pid_path().write_text(str(successor[0].pid), encoding="utf-8")

    monkeypatch.setattr(runtime, "_clear_service_pid_reservation", service_stopped_while_a_ui_took_over)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    assert _alive(successor[0])
    assert not _signalled(stop_env, successor[0])
    assert _last_stderr_json(capsys) == {"skipped": "ui", "reason": "ui_changed"}
    assert stop_env["remote_access"] == []
    assert stop_env["reaps"] == []
    assert paths.get_runtime_ui_pid_path().read_text(encoding="utf-8") == str(successor[0].pid)


@pytest.mark.parametrize("opencode_id", [OTHER_ID, None])
def test_an_opencode_server_another_runtime_started_is_left_running(spawn, stop_env, opencode_id):
    # The shared pidfile names the server of whichever Runtime wrote it last,
    # for example a successor that started its own after this service exited.
    service, opencode = spawn(RUNTIME_ID), spawn(opencode_id, *OPENCODE)
    stop_env["targets"](service, opencode=opencode)

    assert cli.cmd_stop(expect_runtime_id=RUNTIME_ID) == 0

    service.wait(timeout=10)
    assert _alive(opencode)
    assert not _signalled(stop_env, opencode)
    assert stop_env["stop_pid"] == []
    assert _opencode_pid_path().exists()


@pytest.mark.parametrize("runtime_id", [RUNTIME_ID, None])
def test_provenance_is_read_from_a_real_child_environment(spawn, runtime_id):
    child = spawn(runtime_id)

    process, identity = desktop_runtime.open_desktop_runtime_provenance(child.pid)

    assert process.pid == child.pid
    assert identity.marker_readable
    assert identity.worker_fingerprint == (None if runtime_id is None else fingerprint_process_marker(runtime_id))
