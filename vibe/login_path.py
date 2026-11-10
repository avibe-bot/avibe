"""The ``PATH`` the user's terminal would give the Runtime.

The service and the Web UI start with the environment of whatever started them,
and every Agent Session inherits it from there. A caller that is not a terminal
has a shorter ``PATH`` than the user's: a non-interactive SSH command, cron, and
launchd all give ``/usr/bin:/bin:/usr/sbin:/sbin`` or little more, because
Homebrew, ``~/.local/bin``, nvm and the like extend ``PATH`` only in shell
startup files. A Runtime restarted that way lost ``vibe``, ``gh`` and ``uv`` in
every Session, with nothing to say why.

So a Runtime start asks the user's login shell what ``PATH`` it ends up with,
the same way the desktop application does for a Runtime it starts
(``desktop/runtime-host/src/login_path.rs``). A caller whose ``PATH`` already
holds every entry of the login shell's keeps its own, unchanged, so a start
from a terminal is what it always was. Nothing here is required for the Runtime
to start: every failure leaves the caller's ``PATH`` in place.
"""

from __future__ import annotations

import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from itertools import takewhile
from pathlib import Path

try:
    import pwd
except ImportError:  # pragma: no cover - Windows import path
    pwd = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: How long the login shell may take to print its ``PATH``.
#:
#: Startup files that load nvm, conda, or a prompt framework commonly take one
#: or two seconds. The budget is only spent in full when a startup file waits
#: for input or hangs, and the cost of that is the caller's ``PATH``, not a
#: failed start.
LOGIN_PATH_TIMEOUT_SECONDS = 10.0

_START_MARKER = b"__AVIBE_LOGIN_PATH_START__"
_END_MARKER = b"__AVIBE_LOGIN_PATH_END__"
# Startup files may print banners, so the value is fenced by markers. The
# commands are absolute and the arguments need no quoting, so sh, bash, zsh,
# fish, and nushell all run it as written.
_PRINT_PATH_SCRIPT = (
    "/usr/bin/printf %s __AVIBE_LOGIN_PATH_START__; /usr/bin/printenv PATH; "
    "/usr/bin/printf %s __AVIBE_LOGIN_PATH_END__"
)
# What a login starts from before any startup file runs: launchd's ``PATH`` on
# macOS, the usual login default elsewhere.
_SYSTEM_PATH = (
    "/usr/bin:/bin:/usr/sbin:/sbin"
    if sys.platform == "darwin"
    else "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
# Startup output is not read past this; a ``PATH`` is far smaller.
_MAX_OUTPUT_BYTES = 256 * 1024
_EXIT_POLL_SECONDS = 0.02


@dataclass(frozen=True)
class LoginPath:
    """The login shell's ``PATH``, or the stable code for why there is none."""

    value: str | None
    outcome: str


def _login_shell() -> str:
    """The account's login shell, which is the one a new terminal runs.

    The account record comes first: cron exports ``SHELL=/bin/sh`` whatever the
    account's shell is, and a shell that does not read the user's startup files
    would report the short ``PATH`` this lookup exists to replace.
    """

    candidates: list[str] = []
    if pwd is not None:
        try:
            candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
        except (KeyError, OSError):
            pass
    candidates.append(os.environ.get("SHELL", ""))
    for candidate in candidates:
        if candidate and os.path.isabs(candidate):
            return candidate
    return "/bin/zsh" if sys.platform == "darwin" else "/bin/sh"


def login_shell_path() -> LoginPath:
    """Ask the user's login shell for its ``PATH``."""

    return resolve_with(_login_shell(), LOGIN_PATH_TIMEOUT_SECONDS)


def resolve_with(shell: str, timeout: float) -> LoginPath:
    """Run ``shell`` as an interactive login shell and read the ``PATH`` it prints.

    Interactive as well as login: nvm's installer writes to ``.zshrc`` and
    ``.bashrc``, which only interactive shells read. The shell runs in the home
    directory, as a new terminal does, so a tool that picks versions by
    directory does not hand the Runtime one project's ``PATH``.

    It starts from the system ``PATH``, not the caller's. Startup files extend
    whatever ``PATH`` they are given, so a shell that inherited the caller's
    would answer with the caller's entries mixed into its own, and the caller's
    leading entries could no longer be told from the shell's.
    """

    deadline = time.monotonic() + timeout
    home = Path.home()
    try:
        process = subprocess.Popen(
            [shell, "-l", "-i", "-c", _PRINT_PATH_SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(home) if home.is_dir() else None,
            env={**os.environ, "PATH": _SYSTEM_PATH},
            close_fds=True,
            # Its own session: no controlling terminal to take from the caller,
            # and one process group that holds everything a startup file starts.
            start_new_session=True,
        )
    except OSError:
        return LoginPath(None, "spawn_failed")
    try:
        # A process a startup file leaves behind can hold stdout open after the
        # shell is gone, so the value is taken as soon as both markers arrive.
        result = _read_marked_path(process.stdout.fileno(), deadline)
        if result.value is not None:
            # PATH is already printed; let the shell finish its logout hooks
            # (zsh `.zlogout`) within the remaining budget.
            _wait_then_end(process, deadline)
        else:
            _end_process_group(process)
    finally:
        process.stdout.close()
    return result


def _read_marked_path(fd: int, deadline: float) -> LoginPath:
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            parsed = _parse_marked_path(bytes(output))
            if parsed is not None:
                return parsed
            if len(output) > _MAX_OUTPUT_BYTES:
                return LoginPath(None, "output_too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return LoginPath(None, "timeout")
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return LoginPath(None, "no_path")
            if not chunk:
                return LoginPath(None, "no_path")
            output.extend(chunk)


def _parse_marked_path(output: bytes) -> LoginPath | None:
    """The value between the markers, or ``None`` until the end marker has arrived."""

    start = output.find(_START_MARKER)
    if start < 0:
        return None
    start += len(_START_MARKER)
    end = output.find(_END_MARKER, start)
    if end < 0:
        return None
    value = output[start:end].rstrip(b"\r\n")
    if not value or b"\0" in value:
        return LoginPath(None, "no_path")
    return LoginPath(os.fsdecode(value), "resolved")


def _wait_then_end(process: subprocess.Popen, deadline: float) -> None:
    """Let a successful shell finish; end its group only if it does not."""

    while process.poll() is None:
        if time.monotonic() >= deadline:
            _end_process_group(process)
            return
        time.sleep(_EXIT_POLL_SECONDS)


def _end_process_group(process: subprocess.Popen) -> None:
    """End the login shell and everything its startup files started.

    A startup file that blocks in a subprocess would otherwise outlive the
    shell. The group is signalled before the shell is reaped: until then its
    leader's pid, and so the group id, cannot be reused by an unrelated process.
    """

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def _entries(path: str) -> list[str]:
    seen: set[str] = set()
    entries: list[str] = []
    for entry in path.split(os.pathsep):
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)
    return entries


@lru_cache(maxsize=8)
def _runtime_path(inherited: str) -> str:
    """The ``PATH`` a Runtime started with ``inherited`` should have.

    Cached for the process: one start launches the service and the Web UI from
    the same environment, and asks the shell once.
    """

    login = login_shell_path()
    if login.value is None:
        logger.warning(
            "Could not read the login shell's PATH (%s); Avibe starts with the caller's PATH.",
            login.outcome,
        )
        return inherited
    login_entries = _entries(login.value)
    inherited_entries = _entries(inherited)
    inherited_set = set(inherited_entries)
    missing = [entry for entry in login_entries if entry not in inherited_set]
    if not missing:
        return inherited
    logger.warning(
        "The caller's PATH lacks %d of the login shell's entries (%s); Avibe starts with the login shell's PATH.",
        len(missing),
        os.pathsep.join(missing[:5]) + (os.pathsep + "..." if len(missing) > 5 else ""),
    )
    # What the caller put ahead of everything the shell knows stays ahead: a
    # service unit that leads with its own virtualenv must keep resolving its
    # own interpreter, whatever the account's startup files add.
    login_set = set(login_entries)
    own_lead = list(takewhile(lambda entry: entry not in login_set, inherited_entries))
    own_rest = [entry for entry in inherited_entries[len(own_lead) :] if entry not in login_set]
    return os.pathsep.join([*own_lead, *login_entries, *own_rest])


def runtime_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a Runtime process starts with: ``env`` with the user's ``PATH``.

    A caller whose ``PATH`` already holds every login-shell entry keeps it as it
    is. Otherwise the login shell's ``PATH`` replaces the entries the two share:
    the caller's own leading entries stay first and its other own entries
    follow. A desktop-managed Runtime is left alone: the desktop application
    resolved its ``PATH`` when it started it, and a restart carries that.
    """

    from vibe.upgrade import is_desktop_managed_runtime

    result = dict(os.environ if env is None else env)
    if os.name == "nt" or is_desktop_managed_runtime(result):
        return result
    inherited = result.get("PATH", "")
    path = _runtime_path(inherited)
    if path != inherited:
        result["PATH"] = path
    return result
