"""`vibe autostart` and the login entry its LaunchAgent runs (macOS, CLI installs).

Every case runs the real CLI entry against the isolated home, with a recording
fake in place of ``launchctl``: the developer's launchd is never asked anything.
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe import cli, internal_client, login_autostart, restart_supervisor, runtime
from vibe.macos_session_diagnostics import CommandResult

pytestmark = pytest.mark.skipif(os.name == "nt", reason="launchd is faked on any POSIX host; there is no uid on Windows")

LABEL = "bot.avibe.autostart"
SERVICE_PID = 4242


class FakeLaunchctl:
    """Answers ``launchctl`` from a table and records every call.

    A target nothing was said about is one launchd does not know.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], CommandResult] = {}

    def knows(self, *arguments: str, output: str = "") -> None:
        self.answers[arguments] = CommandResult(0, output)

    def __call__(self, command, timeout) -> CommandResult:
        assert command[0] == "/bin/launchctl"
        arguments = tuple(command[1:])
        self.calls.append(list(arguments))
        return self.answers.get(arguments, CommandResult(113, "Bad request.\nCould not find service"))


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A macOS CLI install in the isolated home: a stable launcher that selects an install generation."""

    home = Path.home()
    interpreter = tmp_path / "uv-python" / "cpython-3.13.1-macos-aarch64-none" / "bin" / "python3.13"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    generation = home / ".avibe" / "runtime" / "install-generations" / "generation-1"
    environment = generation / "uv" / "tools" / "avibe-os" / "bin"
    environment.mkdir(parents=True)
    (environment / "python").symlink_to(interpreter)
    script = environment / "vibe"
    script.write_text(f"#!{environment / 'python'}\n", encoding="utf-8")
    script.chmod(0o755)
    exported = generation / "bin" / "vibe"
    exported.parent.mkdir()
    exported.symlink_to(script)
    launcher = home / ".local" / "bin" / "vibe"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(exported)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", str(launcher))
    monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)
    launchctl = FakeLaunchctl()
    monkeypatch.setattr(login_autostart, "run_bounded_command", launchctl)
    return SimpleNamespace(
        home=home,
        launcher=launcher,
        exported=exported,
        interpreter=os.path.realpath(interpreter),
        plist=home / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        log=(home / ".avibe").resolve() / "logs" / "autostart.log",
        gui=f"gui/{os.getuid()}",
        launchctl=launchctl,
    )


@pytest.fixture
def vibe(monkeypatch):
    """Run the real CLI entry point and report its exit code."""

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["vibe", *argv])
        with pytest.raises(SystemExit) as exited:
            cli.main()
        return int(exited.value.code or 0)

    return run


def test_on_writes_one_agent_for_the_stable_launcher_and_loads_it(mac, vibe, capsys):
    mac.launchctl.knows("print", mac.gui)
    mac.launchctl.knows("bootstrap", mac.gui, str(mac.plist))

    assert vibe("autostart", "on") == 0

    # The persisted shape, key for key: it fires once per login and nothing restarts it.
    assert plistlib.loads(mac.plist.read_bytes()) == {
        "Label": "bot.avibe.autostart",
        "ProgramArguments": [str(mac.launcher), "__login-start"],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(mac.log),
        "StandardErrorPath": str(mac.log),
    }
    # The launcher selects a generation, and the file names neither it nor its
    # interpreter, so an upgrade has nothing to rewrite.
    assert b"install-generations" not in mac.plist.read_bytes()
    assert mac.log.parent.is_dir()
    # A loaded copy is replaced, never loaded twice.
    assert mac.launchctl.calls == [
        ["print", mac.gui],
        ["bootout", f"{mac.gui}/{LABEL}"],
        ["bootstrap", mac.gui, str(mac.plist)],
    ]
    out = capsys.readouterr().out
    assert "Launch at login is on." in out
    assert "Loaded: yes" in out
    assert "has its own macOS privacy permissions" in out
    assert f"add {mac.interpreter} under System Settings > Privacy & Security > Full Disk Access" in out


def test_on_without_a_desktop_session_installs_the_agent_for_the_next_login(mac, vibe, capsys):
    assert vibe("autostart", "on") == 0

    assert plistlib.loads(mac.plist.read_bytes())["ProgramArguments"] == [str(mac.launcher), "__login-start"]
    assert mac.launchctl.calls == [["print", mac.gui]]
    assert "it takes effect at the next desktop login" in capsys.readouterr().out


def test_on_fails_when_the_desktop_session_refuses_the_agent(mac, vibe, capsys):
    mac.launchctl.knows("print", mac.gui)
    refused = CommandResult(5, "Bootstrap failed: 5: Input/output error\nTry re-running the command as root.")
    mac.launchctl.answers[("bootstrap", mac.gui, str(mac.plist))] = refused

    # Not reported as on: a session that refuses the agent now may refuse it at the next login.
    assert vibe("autostart", "on") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{mac.plist} was written, but launchctl could not load it" in captured.err
    assert "Bootstrap failed: 5: Input/output error" in captured.err
    assert mac.plist.is_file()


def test_off_unloads_and_removes_only_its_own_agent(mac, vibe, capsys):
    mac.launchctl.knows("print", mac.gui)
    mac.launchctl.knows("bootstrap", mac.gui, str(mac.plist))
    assert vibe("autostart", "on") == 0
    other = mac.plist.with_name("com.example.other.plist")
    other.write_bytes(b"someone else's agent")
    mac.launchctl.knows("bootout", f"{mac.gui}/{LABEL}")
    mac.launchctl.calls.clear()
    capsys.readouterr()

    assert vibe("autostart", "off") == 0

    assert not mac.plist.exists()
    assert other.read_bytes() == b"someone else's agent"
    assert mac.launchctl.calls == [["bootout", f"{mac.gui}/{LABEL}"]]
    assert "Launch at login is off." in capsys.readouterr().out

    # With nothing loaded and no file, there is nothing left to turn off.
    del mac.launchctl.answers[("bootout", f"{mac.gui}/{LABEL}")]
    assert vibe("autostart", "off") == 0
    assert capsys.readouterr().out == "Launch at login is already off.\n"


def test_status_reports_the_agent_that_is_installed(mac, vibe, capsys):
    assert vibe("autostart", "status", "--json") == 0
    off = json.loads(capsys.readouterr().out)
    assert (off["kind"], off["enabled"], off["loaded"], off["launcher"]) == ("autostart_status", False, False, None)

    mac.launchctl.knows("print", mac.gui)
    mac.launchctl.knows("bootstrap", mac.gui, str(mac.plist))
    assert vibe("autostart", "on") == 0
    mac.launchctl.knows("print", f"{mac.gui}/{LABEL}")
    capsys.readouterr()

    assert vibe("autostart", "status", "--json") == 0
    on = json.loads(capsys.readouterr().out)
    assert on == {
        "schema_version": 1,
        "ok": True,
        "kind": "autostart_status",
        "enabled": True,
        "loaded": True,
        "label": "bot.avibe.autostart",
        "plist_path": str(mac.plist),
        "launcher": str(mac.launcher),
        # What macOS grants privacy permissions to: the interpreter, every link resolved.
        "interpreter": mac.interpreter,
        "log_path": str(mac.log),
    }

    assert vibe("autostart", "status") == 0
    text = capsys.readouterr().out
    assert "  Status: on\n  Loaded: yes\n" in text
    assert f"add {mac.interpreter} under System Settings > Privacy & Security > Full Disk Access" in text


@pytest.mark.parametrize(
    ("shape", "resolved"),
    [
        ("#!{interpreter}\n", True),
        # uv and pip write this when the path is too long for a shebang or holds a space.
        ("#!/bin/sh\n'''exec' '{interpreter}' \"$0\" \"$@\"\n' '''\n", True),
        ("#!/usr/bin/env python3\n", False),
        ("\x7fELF", False),
    ],
)
def test_the_interpreter_is_read_from_the_launcher(tmp_path, shape, resolved):
    interpreter = tmp_path / "uv python" / "bin" / "python3.13"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    link = tmp_path / "python"
    link.symlink_to(interpreter)
    launcher = tmp_path / "vibe"
    launcher.write_text(shape.format(interpreter=link), encoding="utf-8")

    expected = os.path.realpath(interpreter) if resolved else None
    assert login_autostart.launcher_interpreter(launcher) == expected


def _refuse(refusal: str, mac, monkeypatch, tmp_path) -> None:
    if refusal == "not-macos":
        monkeypatch.setattr(sys, "platform", "linux")
    elif refusal == "desktop-managed":
        monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    elif refusal == "another-home":
        monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "another-home"))
    elif refusal == "generation-launcher":
        monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", str(mac.exported))
    elif refusal == "virtualenv-launcher":
        entry = tmp_path / "checkout" / ".venv" / "bin" / "vibe"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/bin/sh\n", encoding="utf-8")
        entry.chmod(0o755)
        (entry.parent.parent / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", str(entry))


@pytest.mark.parametrize(
    ("action", "refusal", "code", "says"),
    [
        ("on", "not-macos", "autostart_unsupported_platform", "macOS only"),
        ("off", "not-macos", "autostart_unsupported_platform", "macOS only"),
        ("status", "not-macos", "autostart_unsupported_platform", "macOS only"),
        ("on", "desktop-managed", "autostart_desktop_managed", "its own launch-at-login setting"),
        ("off", "desktop-managed", "autostart_desktop_managed", "its own launch-at-login setting"),
        ("status", "desktop-managed", "autostart_desktop_managed", "its own launch-at-login setting"),
        # launchd starts the login entry without AVIBE_HOME: it could only start the default home.
        ("on", "another-home", "autostart_custom_home", "starts the default Avibe home"),
        # Paths an upgrade replaces must never be written into the agent.
        ("on", "generation-launcher", "autostart_no_stable_launcher", "install generation"),
        ("on", "virtualenv-launcher", "autostart_no_stable_launcher", "virtual environment"),
    ],
)
def test_autostart_refuses_and_changes_nothing(mac, vibe, monkeypatch, tmp_path, capsys, action, refusal, code, says):
    mac.plist.parent.mkdir(parents=True)
    installed = action != "on"
    if installed:
        mac.plist.write_bytes(b"an agent an earlier `on` wrote")
    _refuse(refusal, mac, monkeypatch, tmp_path)

    assert vibe("autostart", action, "--json") == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert (payload["ok"], payload["code"]) == (False, code)
    assert says in payload["error"]
    assert captured.out == ""
    assert mac.plist.exists() is installed
    assert mac.launchctl.calls == []


def _launchctl_print(target: str, asid: int) -> str:
    aqua = "    session = Aqua\n" if target.startswith("gui/") else ""
    return f"{target} = {{\n{aqua}    security context = {{\n        uid = 501\n        asid = {asid}\n    }}\n}}\n"


HEALTHY = {"macos_login_session_state": "healthy"}
STALE = {"macos_login_session_state": "stale_login_session"}
DESKTOP_APP = {"macos_login_session_state": "healthy", "desktop_runtime_id": "a" * 64}


@pytest.mark.parametrize(
    ("service_pid", "identity", "service_asid", "desktop_asid", "expected", "logged"),
    [
        pytest.param(None, None, None, None, "start", "no Avibe service is running", id="none-running"),
        pytest.param(SERVICE_PID, HEALTHY, 100, 100, None, "already runs in this desktop session", id="same-session"),
        pytest.param(SERVICE_PID, STALE, 100, 100, "restart", "outlived its login session", id="stale"),
        pytest.param(SERVICE_PID, HEALTHY, 77, 100, "restart", "runs in another login session", id="other-session"),
        # A service that does not answer is still judged by its audit session.
        pytest.param(SERVICE_PID, None, 77, 100, "restart", "runs in another login session", id="other-session-silent"),
        pytest.param(SERVICE_PID, HEALTHY, None, 100, None, "could not be compared", id="service-session-unknown"),
        pytest.param(SERVICE_PID, HEALTHY, 77, None, None, "could not be compared", id="desktop-session-unknown"),
        pytest.param(SERVICE_PID, DESKTOP_APP, 77, 100, None, "belongs to the Avibe desktop app", id="desktop-app"),
    ],
)
def test_the_login_entry_starts_replaces_or_leaves_the_service(
    mac, vibe, monkeypatch, capsys, service_pid, identity, service_asid, desktop_asid, expected, logged
):
    started: list[dict] = []
    restarted: list[dict] = []
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda *, include_starting=True: service_pid)
    monkeypatch.setattr(internal_client, "health_identity_sync", lambda: identity)
    monkeypatch.setattr(cli, "cmd_start", lambda **kwargs: started.append(kwargs) or 0)
    monkeypatch.setattr(
        restart_supervisor, "schedule_restart", lambda **kwargs: restarted.append(kwargs) or {"job_id": "job-1"}
    )
    if service_asid is not None:
        target = f"pid/{service_pid}"
        mac.launchctl.knows("print", target, output=_launchctl_print(target, service_asid))
    if desktop_asid is not None:
        mac.launchctl.knows("print", mac.gui, output=_launchctl_print(mac.gui, desktop_asid))

    # The name the LaunchAgent was written with.
    assert vibe("__login-start") == 0

    # As `vibe start --no-open-browser`: nobody is at the screen yet.
    assert started == ([{"open_browser": False}] if expected == "start" else [])
    assert len(restarted) == (1 if expected == "restart" else 0)
    if expected == "restart":
        # The whole Runtime, through the stable launcher: the Web UI lives in the old session too.
        restart = restarted[0]
        assert (restart["delay_seconds"], restart["vibe_path"], restart["trigger"]) == (
            0.0,
            str(mac.launcher),
            "login-autostart",
        )
        assert restart.get("scope", "all") == "all"
    out = capsys.readouterr().out
    assert logged in out
    if expected is None:
        assert "nothing to do" in out
