"""The PATH a Runtime starts with: the login shell's when the caller's is shorter."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from vibe import login_path, runtime
from vibe.login_path import LoginPath
from vibe.upgrade import DESKTOP_MANAGED_RUNTIME_ENV

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX login shells only")

LOGIN = "/opt/homebrew/bin:/home/user/.local/bin:/usr/bin:/bin"


def _fake_shell(directory: Path, startup: str) -> str:
    """A stand-in login shell: ``startup`` runs first, then the script it was given.

    The three flags are dropped the way a real shell consumes them, so a lookup
    that passes other flags evaluates the wrong argument and prints no PATH.
    """

    shell = directory / "shell"
    shell.write_text(f'#!/bin/sh\n{startup}\nshift 3\neval "$1"\n', encoding="utf-8")
    shell.chmod(0o755)
    return str(shell)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_path_a_startup_file_exports_is_returned_without_its_banner(tmp_path):
    shell = _fake_shell(tmp_path, "echo 'Welcome back'\nPATH=\"/home/user/.nvm/bin:$PATH\"; export PATH")

    resolved = login_path.resolve_with(shell, 10.0)

    assert resolved.outcome == "resolved"
    assert resolved.value.startswith("/home/user/.nvm/bin:")
    assert "\n" not in resolved.value and "Welcome" not in resolved.value


def test_a_startup_file_that_never_finishes_falls_back_and_leaves_nothing_running(tmp_path):
    pid_file = tmp_path / "pid"
    # `sleep` runs in a subprocess, not in the shell, as a blocking line in
    # `.zshrc` does; the fallback must not leave it behind.
    shell = _fake_shell(tmp_path, f"sleep 30 &\necho $! > '{pid_file}'\nwait")

    started = time.monotonic()
    resolved = login_path.resolve_with(shell, 1.5)

    assert resolved == LoginPath(None, "timeout")
    assert time.monotonic() - started < 8
    child = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child)


@pytest.mark.parametrize(
    ("caller", "login", "expected"),
    [
        # A non-interactive SSH command: the login shell's PATH replaces it.
        ("/usr/bin:/bin:/usr/sbin:/sbin", LoginPath(LOGIN, "resolved"), LOGIN + ":/usr/sbin:/sbin"),
        # What the caller put first stays first, as a service unit's virtualenv
        # must; its other own entries follow the login shell's.
        (
            "/srv/venv/bin:/usr/bin:/custom/bin:/bin",
            LoginPath(LOGIN, "resolved"),
            "/srv/venv/bin:" + LOGIN + ":/custom/bin",
        ),
        # A terminal already has every entry, in its own order: nothing changes.
        (
            "/venv/bin:/usr/bin:/home/user/.local/bin:/opt/homebrew/bin:/bin",
            LoginPath(LOGIN, "resolved"),
            "/venv/bin:/usr/bin:/home/user/.local/bin:/opt/homebrew/bin:/bin",
        ),
        # No answer from the shell: the caller's PATH stays.
        ("/usr/bin:/bin", LoginPath(None, "timeout"), "/usr/bin:/bin"),
    ],
)
def test_runtime_environment_takes_the_login_path_only_when_the_callers_is_shorter(
    monkeypatch, caller, login, expected
):
    monkeypatch.setattr(login_path, "login_shell_path", lambda: login)

    environment = login_path.runtime_environment({"PATH": caller, "HOME": "/home/user"})

    assert environment == {"PATH": expected, "HOME": "/home/user"}


def test_a_desktop_managed_runtime_keeps_the_path_the_desktop_resolved(monkeypatch):
    monkeypatch.setattr(login_path, "login_shell_path", lambda: pytest.fail("the login shell was consulted"))
    env = {"PATH": "/runtime/bin:/usr/bin", DESKTOP_MANAGED_RUNTIME_ENV: "1"}

    assert login_path.runtime_environment(env) == env


def test_the_service_and_the_web_ui_start_with_the_login_shell_path(monkeypatch, tmp_path):
    """A restart over non-interactive SSH left every Session without `vibe`, `gh` and `uv`."""

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv(DESKTOP_MANAGED_RUNTIME_ENV, raising=False)
    monkeypatch.setattr(login_path, "login_shell_path", lambda: LoginPath(LOGIN, "resolved"))
    monkeypatch.setattr(runtime.paths, "get_runtime_pid_path", lambda: tmp_path / "vibe.pid")
    monkeypatch.setattr(runtime.paths, "get_runtime_ui_pid_path", lambda: tmp_path / "vibe-ui.pid")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    monkeypatch.setattr(runtime, "get_service_main_path", lambda: Path("/tmp/main.py"))
    monkeypatch.setattr(runtime, "service_instance_lock_available", lambda **kwargs: (True, 0))
    monkeypatch.setattr(runtime, "maybe_systemd_scope_prefix", lambda: [])
    monkeypatch.setattr(runtime, "wait_for_service_pid", lambda pid, *args, **kwargs: True)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout=None: pid)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda host, port: True)
    started: dict[str, str] = {}

    def spawn_service(args, stdout_name, stderr_name, env=None, *, hand_over, **kwargs):
        started["service"] = env["PATH"]
        process = type("Process", (), {"pid": 12345, "poll": lambda self: None})()
        hand_over(process)
        return process

    def spawn_ui(args, pid_path, stdout_name, stderr_name, env=None, **kwargs):
        started["ui"] = (os.environ if env is None else env)["PATH"]
        return 5678

    monkeypatch.setattr(runtime, "spawn_service_background_process", spawn_service)
    monkeypatch.setattr(runtime, "spawn_background", spawn_ui)

    runtime.start_service()
    runtime.start_ui("127.0.0.1", 5123)

    assert started == {"service": LOGIN, "ui": LOGIN}
