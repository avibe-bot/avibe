from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


logger = logging.getLogger(__name__)

JSON_STATE_BACKUP_RETENTION = 3
SQLITE_BACKUP_RETENTION = 2
BACKUP_MANIFEST_VERSION = 1

_JSON_BACKUP_RE = re.compile(
    r"^sqlite-state-migration-(?P<timestamp>\d{8}T\d{6}Z)(?:-(?P<suffix>\d+))?$"
)
_SQLITE_BACKUP_RE = re.compile(
    r"^avibe-sqlite-migration-(?P<timestamp>\d{8}T\d{6}Z)(?:-(?P<suffix>\d+))?$"
)
_LEGACY_SQLITE_REPAIR_RE = re.compile(
    r"^vibe-pre-(?:live-)?\d{4}(?:-release-head)?-repair-"
    r"(?P<timestamp>\d{8}T\d{6}Z)\.sqlite$"
)


@dataclass(frozen=True)
class _BackupCandidate:
    root: Path
    companions: tuple[Path, ...]
    kind: str
    timestamp: datetime
    suffix: int

    @property
    def order_key(self) -> tuple[datetime, int, str]:
        return self.timestamp, self.suffix, self.root.name


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_manifest(path: Path) -> dict | None:
    if path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _directory_candidate(path: Path) -> _BackupCandidate | None:
    if path.is_symlink() or not path.is_dir():
        return None

    json_match = _JSON_BACKUP_RE.fullmatch(path.name)
    sqlite_match = _SQLITE_BACKUP_RE.fullmatch(path.name)
    match = json_match or sqlite_match
    if match is None:
        return None

    manifest = _read_manifest(path / "manifest.json")
    if manifest is None:
        return None

    if json_match is not None:
        is_current_manifest = (
            manifest.get("managed_by") == "avibe"
            and manifest.get("kind") == "json-state-migration"
            and manifest.get("schema_version") == BACKUP_MANIFEST_VERSION
        )
        is_legacy_manifest = isinstance(manifest.get("created_at"), str) and isinstance(manifest.get("files"), dict)
        if not (is_current_manifest or is_legacy_manifest):
            return None
        kind = "json"
    else:
        if not (
            manifest.get("managed_by") == "avibe"
            and manifest.get("kind") == "sqlite-migration"
            and manifest.get("schema_version") == BACKUP_MANIFEST_VERSION
            and manifest.get("database") == "vibe.sqlite"
        ):
            return None
        if not (path / "vibe.sqlite").is_file() or (path / "vibe.sqlite").is_symlink():
            return None
        kind = "sqlite"

    timestamp = _parse_timestamp(match.group("timestamp"))
    if timestamp is None:
        return None
    return _BackupCandidate(
        root=path,
        companions=(),
        kind=kind,
        timestamp=timestamp,
        suffix=int(match.group("suffix") or 0),
    )


def _legacy_sqlite_candidate(path: Path) -> _BackupCandidate | None:
    if path.is_symlink() or not path.is_file():
        return None
    match = _LEGACY_SQLITE_REPAIR_RE.fullmatch(path.name)
    if match is None:
        return None
    timestamp = _parse_timestamp(match.group("timestamp"))
    if timestamp is None:
        return None
    companions = tuple(
        companion
        for suffix in ("-wal", "-shm")
        if (companion := path.with_name(path.name + suffix)).is_file() and not companion.is_symlink()
    )
    return _BackupCandidate(
        root=path,
        companions=companions,
        kind="sqlite",
        timestamp=timestamp,
        suffix=0,
    )


def _managed_candidates(backups_dir: Path) -> list[_BackupCandidate]:
    try:
        entries = list(backups_dir.iterdir())
    except OSError:
        return []

    candidates: list[_BackupCandidate] = []
    for entry in entries:
        candidate = _directory_candidate(entry) or _legacy_sqlite_candidate(entry)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _remove_candidate(candidate: _BackupCandidate) -> bool:
    if candidate.root.is_symlink():
        return False
    try:
        for companion in candidate.companions:
            companion.unlink(missing_ok=True)
        if candidate.root.is_dir():
            shutil.rmtree(candidate.root)
        else:
            candidate.root.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to prune managed state backup %s", candidate.root, exc_info=True)
        return False
    return True


def prune_state_backups(
    backups_dir: Path,
    *,
    json_retention: int | None = JSON_STATE_BACKUP_RETENTION,
    sqlite_retention: int | None = SQLITE_BACKUP_RETENTION,
) -> list[Path]:
    """Keep a bounded rollback window of backups created by Avibe.

    Unknown files, symlinks, incomplete backups, and directories without a
    recognized manifest are intentionally left untouched.
    """

    limits = {
        kind: max(0, retention)
        for kind, retention in (("json", json_retention), ("sqlite", sqlite_retention))
        if retention is not None
    }
    candidates = _managed_candidates(backups_dir)
    removed: list[Path] = []
    for kind, limit in limits.items():
        matching = sorted((candidate for candidate in candidates if candidate.kind == kind), key=lambda item: item.order_key)
        for candidate in matching[: max(0, len(matching) - limit)]:
            if _remove_candidate(candidate):
                removed.append(candidate.root)
    return removed


def _unique_backup_dir(backups_dir: Path, *, now: datetime) -> Path:
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffixes = [
        int(match.group("suffix") or 0)
        for entry in backups_dir.iterdir()
        if (match := _SQLITE_BACKUP_RE.fullmatch(entry.name)) is not None
        and match.group("timestamp") == timestamp
    ]
    suffix = max(suffixes, default=-1) + 1
    candidate = backups_dir / f"avibe-sqlite-migration-{timestamp}{f'-{suffix}' if suffix else ''}"
    while candidate.exists() or candidate.is_symlink():
        suffix += 1
        candidate = backups_dir / f"avibe-sqlite-migration-{timestamp}-{suffix}"
    return candidate


def create_sqlite_migration_backup(
    db_path: Path,
    *,
    backups_dir: Path | None = None,
    from_revisions: Iterable[str] = (),
    to_revisions: Iterable[str] = (),
    now: datetime | None = None,
) -> Path:
    """Create a consistent, self-identifying SQLite backup before migration."""

    source_path = db_path.expanduser().resolve()
    created_at = now or datetime.now(timezone.utc)
    target_root = (backups_dir or source_path.parent / "backups").expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    backup_dir = _unique_backup_dir(target_root, now=created_at)
    backup_dir.mkdir(mode=0o700)
    temp_db = backup_dir / "vibe.sqlite.tmp"
    backup_db = backup_dir / "vibe.sqlite"

    try:
        with sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True) as source:
            with sqlite3.connect(temp_db) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode = DELETE")
                check = destination.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    raise sqlite3.DatabaseError(f"SQLite backup quick_check failed: {check!r}")
        os.chmod(temp_db, 0o600)
        temp_db.replace(backup_db)
        manifest = {
            "schema_version": BACKUP_MANIFEST_VERSION,
            "managed_by": "avibe",
            "kind": "sqlite-migration",
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "database": "vibe.sqlite",
            "from_revisions": sorted(set(from_revisions)),
            "to_revisions": sorted(set(to_revisions)),
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    return backup_dir
