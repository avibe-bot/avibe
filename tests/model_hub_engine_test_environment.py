"""Hermetic inputs/guards for trusted native fixture drivers, not a sandbox."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def child_environment(home: Path, source: Path) -> dict[str, str]:
    home.mkdir()
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
    }}
    environment.update({name: str(home) for name in (
        "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
        "TMP", "TEMP", "TMPDIR",
    )})
    environment.update(AVIBE_HOME=str(home / ".avibe"), PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    return environment


def install_child_audit(root: Path) -> None:
    """Catch accidental Python write/network escapes in the trusted driver.

    This is a fixture regression guard, not containment of arbitrary native
    code. The actual child is the reviewed local-only Python HTTP fixture.
    """
    root = root.resolve()
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)) and args[2] & write_flags:
            if os.fsdecode(args[0]) == os.devnull:
                return  # deliberate null child streams, not a user-state write
            path = Path(os.fsdecode(args[0])).resolve()
            assert path.is_relative_to(root), f"fixture write escaped owned root: {path}"
        elif event == "socket.getaddrinfo":
            assert args[0] in {"127.0.0.1", "::1", "localhost"}, "fixture attempted external DNS"
        elif event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple):
                assert address[0] in {"127.0.0.1", "::1"}, "fixture attempted external network"
            elif isinstance(address, str):
                assert Path(address).resolve().is_relative_to(root), "fixture attempted foreign local IPC"

    sys.addaudithook(audit)
