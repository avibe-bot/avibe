"""Resolve the Git executable used by platform-owned features."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResolvedGit:
    path: Path
    source: str  # "vendored" | "system"


def _resolve_vendored() -> ResolvedGit | None:
    from core.git_runtime import get_git_runtime_manager

    path = get_git_runtime_manager().resolve_git_path()
    return ResolvedGit(path=path, source="vendored") if path is not None else None


def _macos_command_line_tools_available() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/xcode-select", "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def resolve_git() -> ResolvedGit | None:
    vendored = _resolve_vendored()
    if vendored is not None:
        return vendored

    candidate = shutil.which("git")
    if not candidate:
        return None
    path = Path(candidate)
    if sys.platform == "darwin" and path == Path("/usr/bin/git"):
        if not _macos_command_line_tools_available():
            return None
    return ResolvedGit(path=path, source="system")
