"""Why the configured Codex CLI cannot start: a closed set of repair causes.

Avibe starts the configured Codex executable for the Model Hub catalog export
and for every app-server. When that executable cannot run, each later turn
fails the same way, so the turn needs a repair, not a retry. Recognition reads
only what the failed launch produced: the operating system's error for the
exec, or the published npm launcher's report that its platform package or
native executable is missing or not executable. Callers receive a cause, never
the process output. Anything else stays unrecognized and keeps its own handling.
"""

from __future__ import annotations

import errno
import re

from core.watch_worker import decode_watch_worker_spawn_errno

CLI_NOT_FOUND = "cli_not_found"
INSTALL_INCOMPLETE = "install_incomplete"
PERMISSION_DENIED = "permission_denied"

_COPY_KEYS = {
    CLI_NOT_FOUND: "error.codexCliNotFound",
    INSTALL_INCOMPLETE: "error.codexCliInstallIncomplete",
    PERMISSION_DENIED: "error.codexCliPermissionDenied",
}
_DIAGNOSTICS = {
    CLI_NOT_FOUND: "Codex CLI executable was not found",
    INSTALL_INCOMPLETE: "Codex CLI installation is incomplete",
    PERMISSION_DENIED: "Codex CLI is not permitted to run",
}

# The npm launcher's own report that its platform package, or in newer
# launchers its native executable, is missing (seen from 0.121.0 to 0.162.1).
_MISSING_PLATFORM_PACKAGE = re.compile(
    r"^Error: Missing optional dependency @openai/codex-\S+", re.MULTILINE
)
# Node's report when an older launcher spawns a native executable that is
# missing, or any launcher one that is not executable.
_NATIVE_SPAWN_FAILURE = re.compile(
    r"^Error: spawn .+ (?P<code>ENOENT|EACCES)$", re.MULTILINE
)


class CodexLaunchError(RuntimeError):
    """The configured Codex CLI cannot start until it is repaired."""

    def __init__(self, cause: str) -> None:
        if cause not in _COPY_KEYS:
            raise ValueError(f"Unknown Codex launch failure: {cause}")
        self.cause = cause
        super().__init__(_DIAGNOSTICS[cause])

    @property
    def copy_key(self) -> str:
        """The localized repair instruction for this cause."""

        return _COPY_KEYS[self.cause]

    @property
    def retry_reason(self) -> str:
        """The durable reason an unwritten input waits for an explicit retry."""

        return f"codex_cli_{self.cause}"


def launch_failure_from_errno(code: int | None) -> str | None:
    """The cause of an exec the operating system refused, by error number."""

    if code == errno.ENOENT:
        return CLI_NOT_FOUND
    if code == errno.EACCES:
        return PERMISSION_DENIED
    return None


def launch_failure_from_stderr(stderr: str) -> str | None:
    """The cause a failed launch reported on stderr; the text is not retained."""

    spawn_errno = decode_watch_worker_spawn_errno(stderr)
    if spawn_errno is not None:
        return launch_failure_from_errno(spawn_errno)
    text = stderr.replace("\r\n", "\n")
    if _MISSING_PLATFORM_PACKAGE.search(text):
        return INSTALL_INCOMPLETE
    spawn = _NATIVE_SPAWN_FAILURE.search(text)
    if spawn is None:
        return None
    return INSTALL_INCOMPLETE if spawn.group("code") == "ENOENT" else PERMISSION_DENIED
