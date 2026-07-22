from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import HarnessError


def workspace_root() -> Path:
    """Locate the checkout from the installed editable harness source."""
    current = Path(__file__).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "scripts" / "memory_poc" / "CONTRACT.md").is_file():
            return candidate
    raise HarnessError("workspace_root_not_found")


def harness_root() -> Path:
    return workspace_root() / "scripts" / "memory_poc" / "harness"


def runtime_root(root: Path | None = None) -> Path:
    return (root or workspace_root()) / ".runtime" / "memory-poc"


def ensure_owner_directory(path: Path) -> Path:
    """Create a mode-0700 directory and reject unsafe replacements."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HarnessError("unsafe_runtime_directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise HarnessError("runtime_directory_owner_mismatch")
    os.chmod(path, 0o700)
    return path


def ensure_regular_file_mode(path: Path, mode: int = 0o600) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HarnessError("unsafe_runtime_file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise HarnessError("runtime_file_owner_mismatch")
    os.chmod(path, mode)
