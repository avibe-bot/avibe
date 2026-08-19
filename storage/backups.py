from __future__ import annotations

import errno
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

# What a backup's database actually was: the revisions it was stamped with, and
# a fingerprint of the schema those revisions claim to describe. Two copies are
# the same rollback point only if they agree on both.
_State = tuple[tuple[str, ...], str]

# A platform or filesystem declining to sync a directory is an answer, not a
# fault. Anything else -- ENOSPC, EIO -- means the data may not be on the disk,
# so it must reach the caller instead of being logged and forgotten.
_UNSUPPORTED_SYNC_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EACCES", "EPERM", "EINVAL", "EISDIR", "ENOSYS", "ENOTSUP", "EOPNOTSUPP")
    if hasattr(errno, name)
)


@dataclass(frozen=True)
class _BackupCandidate:
    root: Path
    companions: tuple[Path, ...]
    kind: str
    timestamp: datetime
    suffix: int
    sequence: int | None = None
    state: _State | None = None

    @property
    def order_key(self) -> tuple[bool, int, datetime, int, str]:
        """Creation order, measured by us where possible.

        The sequence is a counter this module writes and increments from its own
        previous writes, so it is the one ordering input nothing outside the
        process can move. Wall-clock time can: an NTP correction, a manual
        change, or state carried from a machine running ahead all reorder
        timestamps after the fact, and a copy that lands out of order takes the
        slot of the one that actually holds more of the user's writes.

        Backups written before the counter existed have no sequence, and sort
        below every backup that has one -- which is simply true, since the
        counter only started being written later. Among themselves the timestamp
        is the best evidence available.
        """

        return (
            self.sequence is not None,
            self.sequence or 0,
            self.timestamp,
            self.suffix,
            self.root.name,
        )


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
        sequence=_manifest_sequence(manifest),
        state=_manifest_state(manifest),
    )


def _manifest_sequence(manifest: dict) -> int | None:
    value = manifest.get("sequence")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _manifest_state(manifest: dict) -> _State | None:
    """What the database was, as the manifest recorded it.

    Both halves are required, because either one alone mistakes a different
    rollback point for the same one: a partially applied upgrade moves the
    schema while the revisions stand still, and a data-only migration moves the
    revisions while the schema stands still.

    Manifests written before the fingerprint existed have no state, and a backup
    with no state can only be compared to itself.
    """

    revisions = manifest.get("from_revisions")
    digest = manifest.get("schema_digest")
    if not isinstance(revisions, list) or not isinstance(digest, str) or not digest:
        return None
    return tuple(sorted(str(revision) for revision in revisions)), digest


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

    Two copies of the same database in the same state are the same rollback
    point taken twice, and the newer one dominates: it holds every write the
    older one holds, plus the writes made since. So the window keeps the newest
    copy of each distinct state, newest state first. That is the whole policy --
    there is no second rule for it to disagree with.

    It is what makes a retry storm survivable. An upgrade can commit part of its
    work and then raise -- a SQLite table rebuild inside an autocommit block
    that fails a later check is exactly that shape -- and from the next attempt
    on, every copy is of the half-migrated database. Those retries are all one
    state, so they hold one slot between them instead of crowding out the
    snapshot taken before the damage, which is a state nothing else can produce.

    Reading the state off the database instead of inferring it from the events
    around the backup is the point. Earlier revisions of this code tried to
    recognise a run of retries from wall-clock time, and then from the schema
    transition each attempt recorded; the environment moves both. A clock
    correction reorders the attempts. `command.upgrade` walks several revisions
    in one call, and entering an autocommit block commits the version stamps
    already earned, so the transition an attempt reports is not the one the
    attempt before it reported. A fingerprint taken from the copy moves only
    when the database moves -- including backwards, which is what an operator
    restoring a rollback point does, and why a post-restore snapshot correctly
    reads as the state it was restored to rather than as one more retry.

    On a healthy machine every migration finishes and every backup is a state of
    its own, so this is the familiar newest-first. Backups too old to record a
    state stand alone, for the same reason: nothing about them can be compared.
    """

    best: dict[object, _BackupCandidate] = {}
    superseded: list[_BackupCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.order_key, reverse=True):
        key = candidate.state if candidate.state is not None else candidate.root
        if key in best:
            superseded.append(candidate)
        else:
            best[key] = candidate
    return list(best.values()) + superseded


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
        matching = [candidate for candidate in candidates if candidate.kind == kind]
        keep = {candidate.root for candidate in _keep_priority(matching)[:limit]}
        for candidate in sorted(matching, key=lambda item: item.order_key):
            if candidate.root not in keep and _remove_candidate(candidate):
                removed.append(candidate.root)
    return removed


def _fsync_file(path: Path) -> None:
    """Push a file's contents to stable storage.

    A failure here is never tolerable: it says the copy may not survive the
    crash it was made for, and raising is what stops the caller from deleting
    the copies it was meant to replace.
    """

    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """Push a directory's own entries to stable storage, where that is a thing.

    Syncing the directory is how a rename becomes durable on POSIX. Windows has
    no equivalent and refuses to open a directory at all, and some filesystems
    reject the call; those refusals are the platform answering. A storage error
    is not an answer, so it propagates like any other.
    """

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        if error.errno not in _UNSUPPORTED_SYNC_ERRNOS:
            raise
        logger.debug("Directory sync is unavailable for %s on this platform", path)
        return
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in _UNSUPPORTED_SYNC_ERRNOS:
            raise
        logger.debug("Directory sync is unavailable for %s on this filesystem", path)
    finally:
        os.close(fd)


def _next_sequence(backups_dir: Path) -> int:
    """One past the highest sequence any backup on disk carries.

    Counting from what survives rather than from a stored high-water mark is
    what keeps the counter honest across pruning: the new backup only has to
    outrank the backups that still exist, and reusing a number whose backup was
    deleted costs nothing. It cannot collide with a live one by construction.
    """

    return max(
        (
            candidate.sequence
            for candidate in _managed_candidates(backups_dir)
            if candidate.sequence is not None
        ),
        default=0,
    ) + 1


def _schema_digest(connection: sqlite3.Connection) -> str:
    """Fingerprint the schema of the database behind `connection`.

    Taken from the copy this call just wrote, so it describes the bytes being
    preserved rather than anything reported about them. That is what makes it
    usable as an identity: a partially applied upgrade changes the schema
    without changing the revision the database is stamped with, and restoring a
    rollback point changes it back.

    `sqlite_master` is a handful of rows next to a copy of the whole database,
    so the measurement is free in the only place it is taken.
    """

    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name, tbl_name"
    ):
        for column in row:
            digest.update(b"\x00" if column is None else str(column).encode("utf-8"))
            digest.update(b"\x1f")
        digest.update(b"\x1e")
    return digest.hexdigest()


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
    it just produced, so it never does: the copy reaches stable storage before
    any older one is deleted, and it is stamped with the next sequence, which
    puts it ahead of every backup on disk without having to trust a clock.

    The manifest also records what the copy actually is -- the revisions it was
    stamped with and a fingerprint of its schema -- which is what lets the window
    tell a fresh rollback point from another copy of one it already holds.
    """

    source_path = db_path.expanduser().resolve()
    created_at = now or datetime.now(timezone.utc)
    target_root = (backups_dir or source_path.parent / "backups").expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    # Read before anything is written, so the count is of finished backups only.
    sequence = _next_sequence(target_root)
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
                schema_digest = _schema_digest(destination)
        os.chmod(temp_db, 0o600)
        _fsync_file(temp_db)
        temp_db.replace(backup_db)
        manifest = {
            "schema_version": BACKUP_MANIFEST_VERSION,
            "managed_by": "avibe",
            "kind": "sqlite-migration",
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "database": "vibe.sqlite",
            "sequence": sequence,
            "schema_digest": schema_digest,
            "from_revisions": sorted(set(from_revisions)),
            "to_revisions": sorted(set(to_revisions)),
        }
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _fsync_file(manifest_path)
        _fsync_directory(backup_dir)
        _fsync_directory(target_root)
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    # Everything above has to happen before the prune, and each for its own
    # reason. The fsyncs put the replacement on the disk first, so the
    # filesystem can never persist the deletion of durable copies while losing
    # the one that replaced them -- and if a sync reports a storage failure, the
    # cleanup above runs and nothing is pruned at all. Being after the try means
    # a failure while pruning cannot reach that cleanup and delete the rollback
    # point this call just made. And it has to follow the manifest write,
    # because a directory without one is not yet a recognized candidate: it
    # would neither count itself against the bound nor carry the sequence and
    # state that decide which copies the bound keeps.
    prune_state_backups(target_root, json_retention=None)
    return backup_dir
