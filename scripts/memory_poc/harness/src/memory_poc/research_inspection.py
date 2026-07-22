"""Research-only inspection of the isolated provider root.

This module is POC evidence code. It is not part of the production-shaped
provider read path and its findings never decide a delivery or POC gate.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResearchInspection:
    markdown_present: bool | None
    sqlite_present: bool | None
    outcome: str


def inspect_isolated_root(everos_root: Path) -> ResearchInspection:
    """Read only the harness-owned root for retention evidence."""
    try:
        if not _is_directory(everos_root):
            return ResearchInspection(markdown_present=False, sqlite_present=False, outcome="not_observed")
        markdown_present = _has_markdown(everos_root)
        sqlite_present = _has_sqlite(everos_root / ".index" / "sqlite")
    except OSError:
        return ResearchInspection(markdown_present=None, sqlite_present=None, outcome="unavailable")
    if markdown_present and sqlite_present:
        outcome = "observed"
    elif markdown_present or sqlite_present:
        outcome = "partial"
    else:
        outcome = "not_observed"
    return ResearchInspection(
        markdown_present=markdown_present,
        sqlite_present=sqlite_present,
        outcome=outcome,
    )


def _is_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _has_markdown(root: Path) -> bool:
    for directory, _subdirectories, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            info = (Path(directory) / filename).lstat()
            if stat.S_ISREG(info.st_mode):
                return True
    return False


def _has_sqlite(directory: Path) -> bool:
    if not _is_directory(directory):
        return False
    for child in directory.iterdir():
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and child.suffix == ".db":
            return True
    return False


def _raise_walk_error(error: OSError) -> None:
    raise error
