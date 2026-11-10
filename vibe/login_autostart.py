"""Launch Avibe at desktop login on macOS, for CLI installs.

``vibe autostart on`` writes one per-user LaunchAgent, and that file's
existence is the setting: there is no config key and no state row. launchd runs
the login entry once at each desktop login (``RunAtLoad``, no ``KeepAlive``),
and the entry starts Avibe or replaces a service left over from another login
session. Nothing runs between logins, so ``vibe stop`` needs no marker: a
stopped Avibe stays stopped until the next login.

The agent names the stable launcher, the path upgrades keep on the active
install, and never an install generation or a virtual environment, so an
upgrade has nothing to rewrite.

The label, the plist path and the login entry's name are persisted shapes: a
LaunchAgent written by any release keeps naming them.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from config import paths
from vibe.macos_session_diagnostics import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    CommandResult,
    MacOSSessionProbe,
    SessionContext,
    run_bounded_command,
)

LABEL = "bot.avibe.autostart"
LOGIN_ENTRY_COMMAND = "__login-start"
LOG_FILENAME = "autostart.log"
RESTART_TRIGGER = "login-autostart"

_LAUNCHCTL = "/bin/launchctl"
# Unloading waits for a login entry that is still running to end.
_LAUNCHCTL_CHANGE_TIMEOUT_SECONDS = 10.0
_FAILURE_DETAIL_LIMIT = 200
# uv and pip write the interpreter this way when its path is too long for a
# shebang line or holds a space.
_SH_TRAMPOLINE_RE = re.compile(r"""^'''exec' (?P<quote>['"])(?P<path>.+)(?P=quote) "\$0" "\$@"$""")


class AutostartError(Exception):
    """Why a ``vibe autostart`` command did not do what it was asked; the message says what changed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return paths.get_logs_dir() / LOG_FILENAME


def require_supported() -> None:
    """Raise ``AutostartError`` unless this is a macOS CLI install acting on the default home."""

    from vibe.upgrade import is_desktop_managed_runtime

    if sys.platform != "darwin":
        raise AutostartError(
            "autostart_unsupported_platform",
            "Launch at login is available on macOS only. Nothing was changed.",
        )
    if is_desktop_managed_runtime():
        raise AutostartError(
            "autostart_desktop_managed",
            "This Runtime is managed by the Avibe desktop app, which has its own launch-at-login setting. "
            "Nothing was changed.",
        )
    # launchd starts the login entry without AVIBE_HOME, so it can only ever
    # start the default home.
    custom_home = os.environ.get(paths.AVIBE_HOME_ENV)
    default_home = Path.home() / paths.AVIBE_HOME_DIRNAME
    if custom_home and Path(custom_home).expanduser().resolve() != default_home.resolve():
        raise AutostartError(
            "autostart_custom_home",
            f"Launch at login starts the default Avibe home ({default_home}), and this command runs with "
            f"{paths.AVIBE_HOME_ENV}={custom_home}. Nothing was changed.",
        )


def stable_launcher() -> Path:
    """The launcher the LaunchAgent runs: the one upgrades keep on the active install."""

    from vibe.launcher_path import get_running_vibe_path
    from vibe.upgrade import _is_stable_launcher_path

    running = get_running_vibe_path()
    if running is None:
        raise AutostartError(
            "autostart_no_stable_launcher",
            "Could not find the `vibe` launcher this command ran through. Run `vibe autostart on` through the "
            "installed `vibe` command. Nothing was changed.",
        )
    launcher = Path(running)
    if not _is_stable_launcher_path(launcher) or (launcher.parent.parent / "pyvenv.cfg").is_file():
        raise AutostartError(
            "autostart_no_stable_launcher",
            f"{launcher} is inside an install generation or a virtual environment, which an upgrade replaces. "
            "Run `vibe autostart on` through the `vibe` command on your PATH. Nothing was changed.",
        )
    return launcher


def launcher_interpreter(launcher: str | os.PathLike[str]) -> str | None:
    """The interpreter a console-script launcher runs, with every link resolved.

    macOS attributes a process that launchd started to its own executable, so
    privacy permissions are granted to this path, not to the launcher or to the
    terminal Avibe was started from before.
    """

    try:
        with open(launcher, "rb") as handle:
            lines = handle.read(4096).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or not lines[0].startswith("#!"):
        return None
    interpreter = lines[0][2:].strip()
    if interpreter == "/bin/sh" and len(lines) > 1:
        match = _SH_TRAMPOLINE_RE.match(lines[1].strip())
        if match is None:
            return None
        interpreter = match.group("path")
    elif " " in interpreter:
        # ``#!/usr/bin/env python3``: which interpreter runs depends on PATH.
        return None
    if not os.path.isabs(interpreter):
        return None
    return os.path.realpath(interpreter)


def agent_definition(launcher: Path) -> dict[str, object]:
    log = str(log_path())
    return {
        "Label": LABEL,
        "ProgramArguments": [str(launcher), LOGIN_ENTRY_COMMAND],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _write_agent(plist: Path, definition: Mapping[str, object]) -> None:
    plist.parent.mkdir(parents=True, exist_ok=True)
    # launchd reads only ``.plist`` files here, so an interrupted write is inert.
    replacement = plist.parent / f".{plist.name}.{uuid4().hex}.tmp"
    try:
        with replacement.open("wb") as handle:
            plistlib.dump(dict(definition), handle, sort_keys=True)
        os.chmod(replacement, 0o644)
        os.replace(replacement, plist)
    except BaseException:
        with contextlib.suppress(OSError):
            replacement.unlink()
        raise


def _installed_launcher(plist: Path) -> str | None:
    try:
        with plist.open("rb") as handle:
            arguments = plistlib.load(handle).get("ProgramArguments")
    except Exception:  # noqa: BLE001 - a file edited by hand must not break `status`
        return None
    if isinstance(arguments, list) and arguments and isinstance(arguments[0], str):
        return arguments[0]
    return None


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*arguments: str, timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    return run_bounded_command([_LAUNCHCTL, *arguments], timeout)


def _failure_detail(result: CommandResult) -> str:
    if result.error_class:
        return result.error_class
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    return (lines[0] if lines else f"exit {result.returncode}")[:_FAILURE_DETAIL_LIMIT]


def _agent_loaded() -> bool | None:
    """Whether the agent is loaded in this user's desktop session; ``None`` when launchctl did not answer."""

    result = _launchctl("print", f"{_gui_domain()}/{LABEL}")
    return None if result.returncode is None else result.returncode == 0


def _state(*, enabled: bool, launcher: str | None, loaded: bool | None) -> dict[str, object]:
    return {
        "enabled": enabled,
        "loaded": loaded,
        "label": LABEL,
        "plist_path": str(agent_plist_path()),
        "launcher": launcher,
        "interpreter": launcher_interpreter(launcher) if launcher else None,
        "log_path": str(log_path()),
    }


def status() -> dict[str, object]:
    require_supported()
    plist = agent_plist_path()
    enabled = plist.is_file()
    return _state(
        enabled=enabled,
        launcher=_installed_launcher(plist) if enabled else None,
        loaded=_agent_loaded(),
    )


def enable() -> dict[str, object]:
    """Write the LaunchAgent and load it into this user's desktop session when one is open.

    Loading runs the login entry once, as a login would.
    """

    require_supported()
    launcher = stable_launcher()
    plist = agent_plist_path()
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _write_agent(plist, agent_definition(launcher))

    domain = _gui_domain()
    # The domain exists while this user has a desktop session, whoever asks:
    # over SSH the agent still loads into it.
    desktop_session = _launchctl("print", domain).returncode == 0
    if desktop_session:
        # Loading an agent that is already loaded fails, so a repeated `on`
        # replaces the loaded copy with the file just written.
        _launchctl("bootout", f"{domain}/{LABEL}", timeout=_LAUNCHCTL_CHANGE_TIMEOUT_SECONDS)
        result = _launchctl("bootstrap", domain, str(plist), timeout=_LAUNCHCTL_CHANGE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            # A desktop session that refuses the agent now may refuse it at the
            # next login too, so this is not reported as on.
            raise AutostartError(
                "autostart_not_loaded",
                f"{plist} was written, but launchctl could not load it into your desktop session: "
                f"{_failure_detail(result)}. It is tried again at the next desktop login; "
                "`vibe autostart status` shows whether it is loaded.",
            )
    return _state(enabled=True, launcher=str(launcher), loaded=desktop_session)


def disable() -> dict[str, object]:
    """Unload and remove this label's agent, and nothing else. A running Avibe keeps running."""

    require_supported()
    plist = agent_plist_path()
    unloaded = (
        _launchctl("bootout", f"{_gui_domain()}/{LABEL}", timeout=_LAUNCHCTL_CHANGE_TIMEOUT_SECONDS).returncode == 0
    )
    removed = plist.is_file() or plist.is_symlink()
    if removed:
        plist.unlink()
    return {
        "enabled": False,
        "removed": removed,
        "unloaded": unloaded,
        "label": LABEL,
        "plist_path": str(plist),
    }


def _privacy_lines(interpreter: object) -> list[str]:
    target = interpreter or "the Python interpreter that runs it"
    return [
        "An Avibe started at login has its own macOS privacy permissions, not your terminal's.",
        f"If workspaces are in protected folders or on external volumes, add {target} under "
        "System Settings > Privacy & Security > Full Disk Access.",
    ]


def _loaded_line(action: str, state: Mapping[str, object]) -> str:
    loaded = state["loaded"]
    if loaded:
        return "yes (it runs once now and at each desktop login)" if action == "on" else "yes"
    if loaded is None:
        return "unknown (launchctl did not answer)"
    if action == "on":
        return "no (no desktop session is open for this user; it takes effect at the next desktop login)"
    return "no (it loads at the next desktop login)"


def _launcher_line(state: Mapping[str, object]) -> str:
    launcher = state["launcher"]
    if not launcher:
        return "unknown (the LaunchAgent could not be read; run `vibe autostart on` again)"
    if not os.path.exists(str(launcher)):
        return f"{launcher} (missing; run `vibe autostart on` again)"
    return str(launcher)


def render(action: str, state: Mapping[str, object]) -> list[str]:
    """The text ``vibe autostart <action>`` prints for a person at a terminal."""

    if action == "off":
        if not (state["removed"] or state["unloaded"]):
            return ["Launch at login is already off."]
        lines = ["Launch at login is off."]
        if state["removed"]:
            lines.append(f"  Removed: {state['plist_path']}")
        lines.append("A running Avibe keeps running.")
        return lines
    if not state["enabled"]:
        return ["Launch at login:", "  Status: off"]
    lines = ["Launch at login is on."] if action == "on" else ["Launch at login:", "  Status: on"]
    lines.extend(
        [
            f"  Loaded: {_loaded_line(action, state)}",
            f"  Launcher: {_launcher_line(state)}",
            f"  Interpreter: {state['interpreter'] or 'unknown'}",
            f"  LaunchAgent: {state['plist_path']}",
            f"  Log: {state['log_path']}",
        ]
    )
    lines.extend(_privacy_lines(state["interpreter"]))
    return lines


def _login_decision(identity: Mapping[str, object], context: SessionContext) -> tuple[bool, str]:
    """Whether the running service is to be replaced from this desktop session, and why."""

    if identity.get("desktop_runtime_id"):
        return False, "belongs to the Avibe desktop app, which has its own launch at login"
    if identity.get("macos_login_session_state") == "stale_login_session":
        return True, "outlived its login session"
    if context.service_asid is None or context.gui_asid is None:
        return False, (
            "could not be compared with this desktop session "
            f"(service: {context.service_error_class or 'unknown'}, "
            f"desktop session: {context.gui_error_class or 'unknown'})"
        )
    if context.service_asid != context.gui_asid:
        return True, (
            f"runs in another login session (audit session {context.service_asid}, "
            f"this desktop session {context.gui_asid})"
        )
    return False, "already runs in this desktop session"


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} autostart: {message}", flush=True)


def run_login_entry(*, start: Callable[[], int]) -> int:
    """Start Avibe at a desktop login, or replace a service left over from another session.

    launchd runs this inside the user's desktop session. ``start`` starts the
    service and the Web UI as ``vibe start --no-open-browser`` does. A running
    service is replaced only through the restart supervisor, and only when it
    reports that its login session ended or its audit session is known to
    differ from this desktop session's; anything inconclusive is left alone.
    """

    if sys.platform != "darwin":
        _log("the login entry runs on macOS only; nothing to do")
        return 1

    from vibe import internal_client, runtime

    # Only a process holding the service lock is a service to judge. A pid
    # record without the lock is left to the start, which adopts a service that
    # is still starting and discards a record that outlived a reboot.
    service_pid = runtime.resolve_service_owner_pid(include_starting=False)
    if service_pid is None:
        _log("no Avibe service is running; starting it")
        return start()

    identity = internal_client.health_identity_sync() or {}
    context = MacOSSessionProbe(
        service_pid=service_pid,
        service_pid_is_target=True,
        command_runner=run_bounded_command,
    ).capture_context()
    replace, reason = _login_decision(identity, context)
    if not replace:
        _log(f"the service (pid {service_pid}) {reason}; nothing to do")
        return 0

    from vibe.launcher_path import cache_running_vibe_path
    from vibe.restart_supervisor import schedule_restart

    result = schedule_restart(delay_seconds=0.0, vibe_path=cache_running_vibe_path(), trigger=RESTART_TRIGGER)
    _log(f"the service (pid {service_pid}) {reason}; restart scheduled (job {result['job_id']})")
    return 0
