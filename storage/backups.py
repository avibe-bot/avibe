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
    transition: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    @property
    def order_key(self) -> tuple[datetime, int, str]:
        return self.timestamp, self.suffix, self.root.name

    @property
    def group_key(self) -> object:
        # A backup with no recorded transition cannot be shown to be an attempt
        # at the same rollback point as any other, so it stands alone.
        return self.transition or self.root.name


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
        transition=_manifest_transition(manifest),
    )


def _manifest_transition(manifest: dict) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The schema move a backup was taken for, when the manifest records one.

    A failed upgrade leaves the recorded revisions untouched, so every retry of
    it reports the same move. That is what makes the move usable as the identity
    of one rollback point across attempts. Manifests written before these fields
    existed simply have no transition.
    """

    from_revisions = manifest.get("from_revisions")
    to_revisions = manifest.get("to_revisions")
    if not isinstance(from_revisions, list) or not isinstance(to_revisions, list):
        return None
    return (
        tuple(sorted(str(revision) for revision in from_revisions)),
        tuple(sorted(str(revision) for revision in to_revisions)),
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


def _keep_priority(candidates: Iterable[_BackupCandidate]) -> list[_BackupCandidate]:
    """Order backups by how much a rollback would want them, best first.

    Retries of one unfinished migration are attempts at a single rollback point
    rather than one rollback point each, and the two worth keeping are the ends.
    The first predates any partially applied schema change -- an upgrade can
    commit part of its work before raising, after which every later copy is of
    the half-migrated database. The last carries every write made since. Copies
    in between are worse than both on both counts, which is exactly why plain
    newest-first is the wrong order here: it fills the window with the newest
    two attempts and discards the only clean one.

    Backups that stand alone are their own group, so for a healthy machine --
    where each backup marks a different completed migration -- this stays the
    familiar newest-first.
    """

    groups: dict[object, list[_BackupCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.group_key, []).append(candidate)

    ranked: list[_BackupCandidate] = []
    superseded: list[_BackupCandidate] = []
    for group in sorted(groups.values(), key=lambda item: max(entry.order_key for entry in item), reverse=True):
        members = sorted(group, key=lambda item: item.order_key)
        ranked.append(members[-1])
        if len(members) > 1:
            ranked.append(members[0])
        superseded.extend(members[1:-1])
    return ranked + superseded


def prune_state_backups(
    backups_dir: Path,
    *,
    json_retention: int | None = JSON_STATE_BACKUP_RETENTION,
    sqlite_retention: int | None = SQLITE_BACKUP_RETENTION,
    protect: Path | None = None,
) -> list[Path]:
    """Keep a bounded rollback window of backups created by Avibe.

    Unknown files, symlinks, incomplete backups, and directories without a
    recognized manifest are intentionally left untouched.

    `protect` names a backup that must survive this call. It takes the first
    slot of its kind rather than an extra one, so the window stays bounded; it
    exists because timestamp order is not proof of being newest. A clock
    corrected backwards, or state moved from a machine whose clock ran ahead,
    leaves future-dated copies that would otherwise rank above a backup taken
    seconds ago and delete it.
    """

    limits = {
        kind: max(0, retention)
        for kind, retention in (("json", json_retention), ("sqlite", sqlite_retention))
        if retention is not None
    }
    candidates = _managed_candidates(backups_dir)
    protected = protect.expanduser().resolve() if protect is not None else None
    removed: list[Path] = []
    for kind, limit in limits.items():
        matching = [candidate for candidate in candidates if candidate.kind == kind]
        ranked = _keep_priority(matching)
        if protected is not None:
            ranked.sort(key=lambda candidate: candidate.root != protected)
        keep = {candidate.root for candidate in ranked[:limit]}
        for candidate in sorted(matching, key=lambda item: item.order_key):
            if candidate.root not in keep and _remove_candidate(candidate):
                removed.append(candidate.root)
    return removed


def _fsync(path: Path) -> None:
    """Push a file, or a directory's entries, to stable storage.

    Syncing the directory is what makes a rename durable on POSIX, and a backup
    is worth nothing if a crash can lose it. Windows has no equivalent and
    refuses to open a directory at all, so a rejection here is the platform's
    answer rather than a failure to report.
    """

    flags = os.O_RDONLY
    if path.is_dir():
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        logger.debug("Cannot open %s to fsync it on this platform", path, exc_info=True)
        return
    try:
        os.fsync(fd)
    except OSError:
        logger.debug("fsync is unavailable for %s on this platform", path, exc_info=True)
    finally:
        os.close(fd)


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
    """Add a consistent, self-identifying SQLite backup to a bounded rollback window.

    Bounding the window belongs here rather than at the call sites. A backup is
    a rollback point the moment it is durable, so whether the migration that
    follows it succeeds says nothing about how many older copies are still
    worth keeping -- and every caller that pruned on that outcome instead left
    a failing migration adding one full copy of the database per attempt,
    forever. Owning it at the one function that grows the window is what makes
    the bound true for callers not yet written, including the ones that opt out
    of their own pruning.

    Owning it here also means this call is the only thing that can destroy what
    it just produced, so it never does: the copy is on stable storage before any
    older one is deleted, and it is named as protected rather than trusted to
    rank newest by its own timestamp.
    """

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
        _fsync(temp_db)
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
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _fsync(manifest_path)
        _fsync(backup_dir)
        _fsync(target_root)
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    # Everything above has to happen before the prune, and each for its own
    # reason. The fsyncs make the replacement survive a crash first, so the
    # filesystem can never persist the deletion of durable copies while losing
    # the one that replaced them. Being after the try means a failure while
    # pruning cannot reach the cleanup path and delete the rollback point this
    # call just made. And it has to follow the manifest write, because a
    # directory without one is not yet a recognized candidate and would not
    # count itself against the bound.
    prune_state_backups(target_root, json_retention=None, protect=backup_dir)
    return backup_dir
