"""Research-only inspection of the isolated provider root.

This module is POC evidence code. It is not part of the production-shaped
provider read path and its findings never decide a delivery or POC gate.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResearchInspection:
    markdown_present: bool | None
    sqlite_present: bool | None
    outcome: str


@dataclass(frozen=True)
class RetentionInspection:
    """Read-only category counts from the isolated provider root.

    Values intentionally describe only locations and row/file counts. Raw
    message, MemCell, Markdown, and provider response payloads never leave the
    isolated root through this POC inspector.
    """

    unprocessed_buffer_rows: int | None
    memcell_rows: int | None
    profile_files: int | None
    episode_files: int | None
    atomic_fact_files: int | None
    sqlite_files: int | None


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


def inspect_retention(everos_root: Path) -> RetentionInspection:
    """Inspect only version-pinned storage categories under an owned root."""
    try:
        if not _is_directory(everos_root):
            return RetentionInspection(None, None, 0, 0, 0, 0)
        sqlite_paths = _sqlite_paths(everos_root / ".index" / "sqlite")
        buffer_rows = _table_row_count(sqlite_paths, "unprocessed_buffer")
        memcell_rows = _table_row_count(sqlite_paths, "memcell")
        profile_files, episode_files, atomic_fact_files = _visible_memory_file_counts(everos_root)
    except (OSError, sqlite3.Error):
        return RetentionInspection(None, None, None, None, None, None)
    return RetentionInspection(
        unprocessed_buffer_rows=buffer_rows,
        memcell_rows=memcell_rows,
        profile_files=profile_files,
        episode_files=episode_files,
        atomic_fact_files=atomic_fact_files,
        sqlite_files=len(sqlite_paths),
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


def _sqlite_paths(directory: Path) -> tuple[Path, ...]:
    if not _is_directory(directory):
        return ()
    paths: list[Path] = []
    for child in directory.iterdir():
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and child.suffix == ".db":
            paths.append(child)
    return tuple(sorted(paths))


def _table_row_count(paths: tuple[Path, ...], table: str) -> int | None:
    """Count a known table through SQLite's read-only URI, never select payloads."""
    saw_table = False
    total = 0
    for path in paths:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in connection.execute("select name from sqlite_master where type = 'table'")
                if isinstance(row[0], str)
            }
            if table not in tables:
                continue
            saw_table = True
            row = connection.execute(f"select count(*) from {table}").fetchone()
            if row is None or not isinstance(row[0], int):
                raise sqlite3.DatabaseError("retention_count_invalid")
            total += row[0]
        finally:
            connection.close()
    return total if saw_table else None


def _visible_memory_file_counts(root: Path) -> tuple[int, int, int]:
    profile_files = 0
    episode_files = 0
    atomic_fact_files = 0
    for directory, _subdirectories, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        path = Path(directory)
        for filename in filenames:
            candidate = path / filename
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or candidate.suffix != ".md":
                continue
            parts = candidate.parts
            if filename == "user.md" and "users" in parts:
                profile_files += 1
            elif "episodes" in parts:
                episode_files += 1
            elif ".atomic_facts" in parts:
                atomic_fact_files += 1
    return profile_files, episode_files, atomic_fact_files


def _raise_walk_error(error: OSError) -> None:
    raise error
