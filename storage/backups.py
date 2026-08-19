from __future__ import annotations

import errno
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

#: Retention counts rollback POSITIONS, not copies. A position keeps the first
#: and the last copy taken there, so the ceiling is twice these numbers and is
#: only reached by repeated attempts at one upgrade. See `prune_state_backups`.
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
    #: The revisions the copied database was stamped with, or None when the
    #: backup does not record them. Two copies taken at the same revisions were
    #: taken to roll back to the same place in the schema history.
    position: tuple[str, ...] | None = None

    @property
    def position_key(self) -> tuple[str, str]:
        """Which rollback position this copy belongs to.

        A copy that does not record its revisions is its own position rather
        than a member of a shared unknown one. Grouping unknowns together would
        let one of them stand in for another, and nothing here knows they are
        interchangeable; keeping them separate reproduces exactly the
        newest-N-copies behavior those backups have always had.
        """

        if self.position is None:
            return ("copy", self.root.name)
        return ("revisions", "\x1f".join(self.position))

    @property
    def order_key(self) -> tuple[datetime, int, str]:
        """Creation order, as the backup's own name records it.

        A movable clock can reorder this, and that is tolerable here, because
        ordering only decides which of the older copies to drop first. The copy
        a call just made is not defended by outranking them -- it is handed to
        `prune_state_backups` as protected, which no clock can undo.
        """

        return (self.timestamp, self.suffix, self.root.name)


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
        position=_manifest_position(manifest),
    )


def _manifest_position(manifest: dict) -> tuple[str, ...] | None:
    """The revisions the copied database carried, when the manifest records them.

    An empty list is a position -- an unversioned database is a place in the
    schema history like any other -- so this answers None only when the field is
    absent or malformed.
    """

    recorded = manifest.get("from_revisions")
    if not isinstance(recorded, list) or not all(isinstance(item, str) for item in recorded):
        return None
    return tuple(sorted(set(recorded)))


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
    protect: Path | None = None,
) -> list[Path]:
    """Keep a bounded rollback window of backups created by Avibe.

    The window holds the newest `retention` rollback POSITIONS, and of each one
    the first and the last copy taken there. Counting positions rather than
    copies is what makes the bound survive a migration that keeps failing: every
    entry point that reaches `run_migrations` re-attempts it and copies the
    database again, so a window counting copies fills with re-attempts of one
    upgrade and evicts the copy taken before the first of them -- the only one in
    the window predating the damage, and the one a user reaching for a rollback
    wants. Positions do not accumulate that way, because those re-attempts all
    copy a database sitting at the same revisions.

    Keeping two copies per position, the first and the last, is not a doubled
    quota; it is the pair that brackets everything that happened there. Which one
    is worth keeping cannot be decided from the position alone, and the two
    plausible rules fail on opposite cases: a re-attempt after a partial repair
    makes the later copy the damaged one, while an operator who restores a copy
    and keeps serving writes makes the later copy the only one holding that
    work. Keeping both ends answers both without asking the label a question it
    cannot answer. A healthy machine is unaffected -- successive upgrades each
    start from a different revision, so each is its own position with one copy in
    it -- and so are copies whose position is unknown, since each is its own
    position and the window is again the newest `retention` copies.

    `protect` -- the backup its caller has just finished writing -- is kept ahead
    of all of them. Ranking it there rather than trusting it to sort highest is
    the difference between a bound and a bug: a copy left behind by a machine
    whose clock ran ahead is dated into the future forever, and every ordering
    rule that decides the fresh copy's fate by comparing timestamps hands the
    slot to that stale one instead.

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
        positions: dict[tuple[str, str], list[_BackupCandidate]] = {}
        for candidate in matching:
            positions.setdefault(candidate.position_key, []).append(candidate)
        ordered = sorted(
            positions.values(),
            key=lambda group: max(item.order_key for item in group),
            reverse=True,
        )
        if protect is not None:
            ordered.sort(key=lambda group: all(item.root != protect for item in group))
        keep: set[Path] = set()
        for group in ordered[:limit]:
            keep |= _kept_within_position(group, protect)
        for candidate in sorted(matching, key=lambda item: item.order_key):
            if candidate.root not in keep and _remove_candidate(candidate):
                removed.append(candidate.root)
    return removed


def _kept_within_position(group: list[_BackupCandidate], protect: Path | None) -> set[Path]:
    """The copies to keep from one rollback position: the first and the last.

    `protect` takes the last slot when it is here, for the same reason it ranks
    first among positions -- a clock that ran ahead can leave a stale copy
    sorting later than the one just written.
    """

    ordered = sorted(group, key=lambda item: item.order_key)
    protected = next((item for item in group if item.root == protect), None)
    latest = protected if protected is not None else ordered[-1]
    return {ordered[0].root, latest.root}


def _fsync_file(path: Path) -> None:
    """Push a file's contents to stable storage.

    A failure here is never tolerable: it says the copy may not survive the
    crash it was made for, and raising is what stops the caller from deleting
    the copies it was meant to replace.

    The descriptor is opened for writing because on Windows `os.fsync` reaches
    `FlushFileBuffers`, which refuses a handle without write access. A
    read-only descriptor would turn every pre-migration backup on that platform
    into a failed schema upgrade -- the flush is here to make an upgrade safe,
    so it must not be the thing that stops one.
    """

    fd = os.open(path, os.O_RDWR)
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


def _stamped_revisions(connection: sqlite3.Connection) -> tuple[str, ...]:
    """The alembic revisions the database behind `connection` is stamped with.

    A database with no `alembic_version` table yields the empty tuple, which is
    what an unversioned database is. This is recorded, never compared: a stamp
    names where a database claims to be, not what it holds.
    """

    try:
        rows = connection.execute("select version_num from alembic_version").fetchall()
    except sqlite3.DatabaseError:
        return ()
    return tuple(sorted({str(row[0]) for row in rows}))


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
    to_revisions: Iterable[str] = (),
    now: datetime | None = None,
) -> Path:
    """Hold a rollback point for the database as it stands, in a bounded window.

    Bounding the window belongs here rather than at the call sites. A backup is
    a rollback point the moment it is durable, so whether the migration that
    follows it succeeds says nothing about how many older copies are still
    worth keeping -- and every caller that pruned on that outcome instead left
    a failing migration adding one full copy of the database per attempt,
    forever. Owning it at the one function that grows the window is what makes
    the bound true for callers not yet written, including the ones that opt out
    of their own pruning.

    The copy is unconditional, and the window promises exactly one thing: a
    restorable copy of the database as it stands at this call. Nothing here
    tries to recognize a copy it already holds and reuse it. Earlier revisions
    of this change did, by ranking the copies on wall-clock adjacency, then on
    the schema transition each attempt recorded, then on a fingerprint of each
    copy's schema, and finally by skipping the copy when a backup carried the
    same revision stamp -- and every one of those is a label standing in for the
    database's contents. Labels can be made to agree while the contents differ:
    a migration that commits row changes and then fails moves neither schema nor
    stamp, and an operator who restores a copy and keeps serving writes moves
    the contents under a stamp that never changed. Reusing a copy on any of
    those grounds hands back a rollback point missing committed data, which is
    worse than having none, because it is reported as one.

    The copy reaches stable storage before any older one is deleted, and is
    protected from its own prune, so this call can never destroy what it just
    produced.

    The manifest records the revisions read back from the copy rather than
    anything the caller reported, because between a caller sampling them and
    this function running, another process can advance the database.
    """

    source_path = db_path.expanduser().resolve()
    created_at = now or datetime.now(timezone.utc)
    target_root = (backups_dir or source_path.parent / "backups").expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    # The entry for the window itself has to be on the disk before anything in
    # it counts as durable; a crash that keeps the upgrade but loses this
    # directory leaves no rollback point at all. Unconditionally, because the
    # attempt that created the directory is also the attempt whose sync can
    # fail: it leaves a root that exists without a durable entry, and every
    # later attempt that treated an existing root as a synced one would inherit
    # that silently, for as long as the window lives.
    _fsync_directory(target_root.parent)

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
                from_revisions = _stamped_revisions(destination)
        os.chmod(temp_db, 0o600)
        _fsync_file(temp_db)
        temp_db.replace(backup_db)
        manifest = {
            "schema_version": BACKUP_MANIFEST_VERSION,
            "managed_by": "avibe",
            "kind": "sqlite-migration",
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "database": "vibe.sqlite",
            "from_revisions": list(from_revisions),
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
    # because a directory without one is not yet a recognized candidate and
    # would not count itself against the bound.
    prune_state_backups(target_root, json_retention=None, protect=backup_dir)
    return backup_dir
