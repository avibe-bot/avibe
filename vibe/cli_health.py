"""Native CLI version evidence, shared by detection and install verification.

This is executable health, not authentication or overall backend readiness.
Only the normalized version and a closed reason code leave the probe; child
output is bounded, used for recognition and never becomes a UI diagnostic.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import Literal, Mapping

from core.command_runner import SupervisedCommandStartupError, run_supervised_command

CliStatus = Literal["ok", "broken", "missing", "unknown"]
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_OUTPUT_BYTES = 8192
_VERSION = r"\d+\.\d+\.\d+(?:\.\d+)?(?:-[\w.-]+)?(?:\+[\w.-]+)?"
_VERSION_PATTERNS = {
    "claude": rf"(?:({_VERSION})\s+\(Claude Code\)|(?:Claude Code|claude)\s+({_VERSION}))",
    "codex": rf"(?:codex-cli|codex)\s+({_VERSION})(?:\s+\(build [^\r\n]+\))?",
    "opencode": rf"(?:opencode\s+)?({_VERSION}|dev|local)",
}


@dataclass(frozen=True)
class CliHealth:
    found: bool
    path: str | None
    status: CliStatus
    version: str | None = None
    reason: str | None = None

    def payload(self) -> dict:
        return asdict(self)


def cli_version(backend: str, stdout: str | None, stderr: str | None) -> str | None:
    """Recognize a complete CLI version response, not a crash's first semver.

    OpenCode deliberately prints a bare version; Claude and Codex identify
    themselves. Warnings on stderr do not replace a successful stdout version.
    """
    pattern = _VERSION_PATTERNS.get(backend)
    if pattern is None:
        return None
    text = (stdout or "").strip() or (stderr or "").strip()
    match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return next(value for value in match.groups() if value is not None)


async def probe_cli(
    backend: str,
    path: str,
    *,
    env: Mapping[str, str],
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> CliHealth:
    if backend not in _VERSION_PATTERNS:
        return CliHealth(True, path, "unknown", reason="unsupported_backend")
    # The same supplier as native backend launches owns app-private helpers
    # (notably Codex's support binaries); a generic PATH is not equivalent.
    from vibe.desktop_backends import DesktopBackendError, desktop_backend_subprocess_environment
    from vibe.upgrade import get_safe_cwd

    try:
        env = desktop_backend_subprocess_environment(backend, path, env) or env
    except DesktopBackendError:
        return CliHealth(True, path, "broken", reason="invalid_installation")
    try:
        result = await run_supervised_command(
            command=[path, "--version"],
            cwd=get_safe_cwd(),
            env=env,
            timeout_seconds=timeout_seconds,
            max_output_bytes=PROBE_OUTPUT_BYTES,
            label=f"{backend} CLI health",
            hide_console=True,
        )
    except (OSError, SupervisedCommandStartupError):
        # Failure to start our supervisor is not proof about the selected CLI.
        return CliHealth(True, path, "unknown", reason="probe_unavailable")
    if result.timed_out:
        return CliHealth(True, path, "broken", reason="probe_timeout")
    if result.exit_code != 0:
        return CliHealth(True, path, "broken", reason="probe_failed")
    if result.stdout_truncated or result.stderr_truncated:
        return CliHealth(True, path, "broken", reason="invalid_version")
    version = cli_version(backend, result.stdout, result.stderr)
    if version is None:
        return CliHealth(True, path, "broken", reason="invalid_version")
    return CliHealth(True, path, "ok", version=version)


def probe_cli_sync(backend: str, path: str, *, env: Mapping[str, str]) -> CliHealth:
    """Adapter for synchronous CLI/installer workers; HTTP awaits probe_cli."""
    return asyncio.run(probe_cli(backend, path, env=env))
