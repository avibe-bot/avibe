from __future__ import annotations

import hashlib
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


# Every on-disk file SQLite reads to answer for this database. ``-shm`` is
# deliberately absent: it is a volatile index into the WAL, rebuilt from it, and
# never content of its own.
_DB_COMPONENT_SUFFIXES = ("", "-wal", "-journal")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(source_path: Path) -> dict[str, dict[str, object]]:
    """Fingerprint the bytes a copy of this database would read.

    Metadata cannot answer this. Avibe runs SQLite in WAL mode (``storage/db.py``,
    ``storage/alembic/env.py``), so a commit can land entirely in
    ``vibe.sqlite-wal`` and leave the main file's size and mtime untouched, and
    timestamp resolution is a property of the filesystem rather than of SQLite. An
    identity built from either would match a copy taken before that commit and hand
    back a rollback point missing it, which is the one failure this whole mechanism
    exists to prevent. So the identity is content, for every component.

    Read before the copy, never after. A commit landing between this read and the
    copy makes the copy strictly newer than the identity recorded beside it, so the
    next attempt sees a changed database and takes a fresh backup. Reuse therefore
    can only return a copy at or after the state it is being reused for -- never one
    missing a commit the live database already has.
    """

    identity: dict[str, dict[str, object]] = {}
    for suffix in _DB_COMPONENT_SUFFIXES:
        component = source_path.with_name(source_path.name + suffix)
        try:
            size = component.stat().st_size
            digest = _file_digest(component)
        except FileNotFoundError:
            # Absence is a state too: a database with no WAL beside it must not
            # match a copy taken when it had one. This is the only read outcome
            # that means the component is not there.
            continue
        except OSError as exc:
            # Every other read failure fails closed. A permission or I/O error
            # says the component could not be read -- not that it is absent --
            # and recording it as absent yields a main-file-only identity that
            # can equal one recorded before the WAL existed. Reuse would then
            # hand back a rollback point missing every commit still living in
            # that unreadable WAL, which is precisely the failure the content
            # digest was introduced to prevent; collapsing the two states here
            # would reintroduce it through the error path.
            #
            # Refusing leaves the database untouched: the caller takes no backup
            # and runs no migration. That is the safe direction -- a schema
            # upgrade whose rollback point cannot be established is the risk this
            # whole mechanism exists to cover.
            raise RuntimeError(
                f"cannot identify database component {component.name}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        identity[suffix or "-db"] = {"size": size, "sha256": digest}
    return identity


def _backup_already_covering(
    target_root: Path,
    *,
    source: dict[str, dict[str, object]],
    from_revisions: list[str],
    to_revisions: list[str],
) -> Path | None:
    """Return a backup of this exact upgrade attempt, if one is already on disk.

    A failed upgrade rolls back and leaves the source database untouched, so the
    next attempt copies bytes that are already backed up. Retrying an upgrade that
    cannot succeed therefore grew the backup directory without bound, and because
    a rollback copy must outlive the failure that made it necessary, retention
    could not reclaim any of it. Reusing the copy keeps every rollback point while
    bounding the directory by distinct states rather than by attempts.
    """

    try:
        entries = sorted(target_root.iterdir())
    except OSError:
        return None

    reusable: list[_BackupCandidate] = []
    for entry in entries:
        # Accept exactly what the pruner recognizes as a managed SQLite backup:
        # reusing a directory it does not track would move the copy outside the
        # retention window that is supposed to reclaim it.
        candidate = _directory_candidate(entry)
        if candidate is None or candidate.kind != "sqlite":
            continue
        manifest = _read_manifest(entry / "manifest.json")
        if manifest is None:
            continue
        # Match the target as well as the source: a backup names the upgrade it
        # protects, and reusing one across a different target would leave a
        # manifest that misdescribes its own contents.
        if (
            manifest.get("source") != source
            or manifest.get("from_revisions") != from_revisions
            or manifest.get("to_revisions") != to_revisions
        ):
            continue
        reusable.append(candidate)
    if not reusable:
        return None
    # Newest, so a reused backup is the one retention would keep longest.
    return max(reusable, key=lambda item: item.order_key).root


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
    source_identity = _source_identity(source_path)
    sorted_from = sorted(set(from_revisions))
    sorted_to = sorted(set(to_revisions))
    reusable = _backup_already_covering(
        target_root,
        source=source_identity,
        from_revisions=sorted_from,
        to_revisions=sorted_to,
    )
    if reusable is not None:
        logger.info("Reusing pre-migration SQLite backup at %s", reusable)
        return reusable
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
            "from_revisions": sorted_from,
            "to_revisions": sorted_to,
            # Identifies the bytes this copy holds, so a retry of the same upgrade
            # can recognize its own backup instead of making another. Backups
            # written before this field simply never match and are left alone.
            "source": source_identity,
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    return backup_dir
