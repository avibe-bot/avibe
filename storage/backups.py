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

#: The name a restore gives the database it takes out of service, inside the
#: backup directory it restored from. That directory is the record of "we rolled
#: back to this point", so the database rolled back FROM belongs beside it -- and
#: living inside the window means the existing bound already covers it, instead
#: of a second growth path needing rules of its own.
REPLACED_DATABASE_NAME = "vibe.sqlite.replaced"

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
    #: Where this backup falls among the managed backups written into its
    #: window, counted by the code that wrote it, or None for one written before
    #: the field existed. This is what the wall clock cannot say: a clock that
    #: steps backward between two attempts dates the second backup before the
    #: first, and every rule that reads write order out of timestamps then reads
    #: it backwards.
    sequence: int | None = None

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
        """When the backup's own name says it was taken.

        Never an answer to which backup came first: a clock that steps backward
        names a later one with an earlier stamp. Read `written_order` for that,
        and use this only to break its ties.
        """

        return (self.timestamp, self.suffix, self.root.name)

    @property
    def written_order(self) -> tuple[int, int, datetime, int, str]:
        """The order the backups were written in, as their writers recorded it.

        The one declaration of that order, because every rule that re-derived it
        from the timestamps was wrong in the same way: the stamp is a label the
        writer chose, and a clock corrected backwards between two attempts
        reverses it. Both things pruning decides -- which position leaves the
        window, and which copies a position keeps -- read it from here.

        A backup with no counter was written by a release that did not have the
        field, so it precedes every counted one. That ordering is a fact about
        which binary made it, not a guess: this release always writes the
        counter, and the release before it never could. Among themselves those
        backups have no recorded order and the stamps are all there is -- the
        one place a clock still decides anything here, and only for backups an
        installed release already made, which nothing can retrofit. Rewriting
        their manifests to invent an order would freeze that same guess into the
        artifact a rollback is supposed to be able to trust.
        """

        if self.sequence is None:
            return (0, 0, *self.order_key)
        return (1, self.sequence, *self.order_key)


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
        sequence=_manifest_sequence(manifest),
    )


def _normalized_position(revisions: Iterable[str]) -> tuple[str, ...]:
    """One canonical name for a place in the schema history.

    Alembic reports the heads of a branched history in no fixed order, so the
    same place can be read back as a differently ordered list. Both the code
    that writes a backup and the code that groups backups derive the name here,
    because two spellings of one position would make the writer count a fresh
    sequence for a position the pruner already knows.
    """

    return tuple(sorted(set(revisions)))


def _manifest_position(manifest: dict) -> tuple[str, ...] | None:
    """The revisions the copied database carried, when the manifest records them.

    An empty list is a position -- an unversioned database is a place in the
    schema history like any other -- so this answers None only when the field is
    absent or malformed.
    """

    recorded = manifest.get("from_revisions")
    if not isinstance(recorded, list) or not all(isinstance(item, str) for item in recorded):
        return None
    return _normalized_position(recorded)


def _manifest_sequence(manifest: dict) -> int | None:
    """Where the writer counted this backup among the window's backups.

    Absent for every backup written before the field existed, which is why no
    caller may require it. `bool` is rejected because it is an `int` that never
    came from a counter.
    """

    recorded = manifest.get("backup_sequence")
    if not isinstance(recorded, int) or isinstance(recorded, bool) or recorded < 0:
        return None
    return recorded


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


def next_backup_sequence(backups_dir: Path) -> int:
    """The number to record in the backup about to be written here.

    Counted from the window rather than from a stored counter, because the
    window is the only thing that survives a crash between two attempts. It
    stays monotonic because pruning always keeps the newest backup it keeps at
    all, so the highest number is still there to count from; a window pruned
    down to nothing restarts at zero with nothing left to compare against.

    Counted across both windows sharing the directory, since the number says
    when a backup was written and comparisons never cross kinds anyway. One
    counter is also one fewer thing that can disagree with itself.

    Two writers racing here would both claim one number. The migration lock is
    held across this call on every path that reaches it, so that race is not
    reachable today, and its effect would be a position keeping one copy too
    many rather than one too few.
    """

    recorded = [
        candidate.sequence
        for candidate in _managed_candidates(backups_dir)
        if candidate.sequence is not None
    ]
    return max(recorded, default=-1) + 1


def find_restorable_backup(backups_dir: Path, *, written_at_or_after: int) -> Path | None:
    """The rollback point taken after a caller's own earlier observation, if any.

    The other half of `next_backup_sequence`: a caller reads that number before
    handing the database to code it does not trust, and reads it back through
    here afterwards. Any backup numbered at or above it was written after the
    observation, so this answers "did a migration episode begin while I was
    watching, and where is its rollback point" without consulting a clock, a
    revision stamp, or anything the watched code reported about itself. Those
    were all tried and are all labels: two attempts can leave a stamp and a
    schema identical while the rows differ, and a clock corrected backwards
    between two attempts dates the later copy first.

    The answer is the NEWEST such copy, which is the one closest to the data the
    machine actually had: every attempt copies the database as it stood before
    its own migration, so an older copy discards whatever was committed between
    the attempts. It is not certified undamaged -- an attempt that commits rows
    and then fails leaves them in the next attempt's copy -- and nothing here can
    certify one, for the reason `_kept_within_position` gives: which copy at a
    position is the good one cannot be decided from the position. So the
    automatic answer loses no data, and the first copy at that position stays in
    the window for an operator who has diagnosed the damage and wants to go
    further back by hand.

    Backups with no recorded number are never eligible. One was written by a
    release that did not have the field, which places it before any observation
    this release could have made.
    """

    # Filtering on a recorded sequence also guarantees a directory backup with a
    # readable `vibe.sqlite`: only `_directory_candidate` reads the field, and it
    # only returns a sqlite candidate when that file is present.
    candidates = [
        candidate
        for candidate in _managed_candidates(backups_dir)
        if candidate.kind == "sqlite"
        and candidate.sequence is not None
        and candidate.sequence >= written_at_or_after
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.written_order).root


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
            key=lambda group: max(item.written_order for item in group),
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
    sorting later than the one just written. It never takes the first slot: the
    copy a call has just made is the newest thing at this position, and letting
    it hold both ends would drop the only copy predating the damage.
    """

    ordered = sorted(group, key=lambda item: item.written_order)
    protected = next((item for item in group if item.root == protect), None)
    latest = protected if protected is not None else ordered[-1]
    first = next((item for item in ordered if item.root != latest.root), latest)
    return {first.root, latest.root}


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

    It also records where this copy falls among the backups already in the
    window. Write order is knowable only here, while the copy is being made;
    anything reading it back later has nothing but the wall clock, and a clock
    that steps backward between two attempts reports the order reversed.
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
        position = _normalized_position(from_revisions)
        manifest = {
            "schema_version": BACKUP_MANIFEST_VERSION,
            "managed_by": "avibe",
            "kind": "sqlite-migration",
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "database": "vibe.sqlite",
            "from_revisions": list(position),
            "to_revisions": sorted(set(to_revisions)),
            # Additive on purpose: a backup is recognized by an exact
            # `schema_version`, so raising it would make every copy this release
            # writes invisible to the release before it -- unrecognized, never
            # pruned, and never offered as a rollback point. An older reader
            # ignores this field and prunes the window exactly as it does today.
            "backup_sequence": next_backup_sequence(target_root),
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


def _duplicate_file(source: Path, destination: Path) -> None:
    """Copy `source` to `destination` without a window where only one copy exists.

    By link where the filesystem allows it, by copy where it does not.
    """

    try:
        os.link(source, destination)
    except OSError:
        logger.debug("Hard link unavailable for %s; copying it to %s instead", source, destination, exc_info=True)
        shutil.copy2(str(source), str(destination))


def _swap_live_database(db_path: Path, replacement: Path, *, into: Path) -> Path | None:
    """Put `replacement` at the live path, keeping all of what was there.

    Displacing the old generation and installing the new one are one operation
    because the invariant is one: at every instant a failure can be observed, the
    live path holds a database together with the sidecars that belong to it.
    Split across two functions, neither owns that -- the displacement ends with
    the live log already cleared and the caller has not yet renamed anything into
    place, so an interrupted install leaves a database missing its own committed
    tail, and no amount of care in either half closes a gap that exists between
    them.

    The write-ahead log and shared-memory sidecars move with the displaced
    database, and that is not tidiness: a `-wal` left behind belongs to the
    generation being displaced, and SQLite would replay it into the restored
    database and call the result recovered. Moving them under the displaced
    database's own name keeps them where SQLite will find them for it instead.

    A previous displacement into this same rollback point is replaced, but only
    once the new one is whole on disk -- staged under its own name, then renamed
    over. The bound has to come from somewhere: a full copy of the database per
    rollback attempt is the growth `prune_state_backups` exists to prevent. But
    the copy being dropped is a working database an operator may still need, and
    deleting it to make room for one not yet written trades a bounded directory
    for a window in which neither generation is anywhere.

    Nothing leaves the live path until the displaced generation is whole under
    its own name, and that ordering is the whole design rather than a detail of
    it. This runs to free the live pathname for a restore that is itself the
    recovery from a failure, so its own failure has to leave the machine no worse
    than it found it: on any error the live database is still there, still with
    its own write-ahead log, and still the database it was. Every step before the
    commit therefore duplicates rather than moves -- by link where the filesystem
    allows it, by copy where it does not, which costs a transient second copy on
    a cross-filesystem layout and is the price of not having a window where the
    only copy is in flight.
    """

    if not db_path.exists():
        os.replace(replacement, db_path)
        return None
    replaced = into / REPLACED_DATABASE_NAME
    staging = into / f"{REPLACED_DATABASE_NAME}.incoming"
    sidecar_suffixes = ("-wal", "-shm")

    def sidecars_of(base: Path) -> tuple[Path, ...]:
        return tuple(base.with_name(base.name + suffix) for suffix in sidecar_suffixes)

    for stale in (staging, *sidecars_of(staging)):
        stale.unlink(missing_ok=True)

    live_sidecars: list[Path] = []
    duplications: list[tuple[Path, Path]] = [(db_path, staging)]
    for suffix in sidecar_suffixes:
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file() and not sidecar.is_symlink():
            live_sidecars.append(sidecar)
            duplications.append((sidecar, staging.with_name(staging.name + suffix)))

    for source, destination in duplications:
        _duplicate_file(source, destination)

    # The previous displacement's sidecars go first and its database last. A
    # database briefly without its write-ahead log is still that database; a
    # write-ahead log briefly without its database is a log SQLite would replay
    # into whatever arrives under that name next.
    for stale_sidecar in sidecars_of(replaced):
        stale_sidecar.unlink(missing_ok=True)
    os.replace(staging, replaced)
    for suffix in sidecar_suffixes:
        staged_sidecar = staging.with_name(staging.name + suffix)
        if staged_sidecar.exists():
            os.replace(staged_sidecar, replaced.with_name(replaced.name + suffix))

    # The displacement is made durable BEFORE the live path commits, and that
    # ordering is the same invariant as the one above, stated against power loss
    # instead of against an exception. A rename is visible to this process the
    # moment it returns and durable only once its directory is synced, so leaving
    # both syncs until after the live commit admits a crash that persists the new
    # live pathname while the directory entry naming the displaced generation is
    # still only in cache. That generation holds everything the failed release
    # committed and exists nowhere else -- the rollback point is a copy of an
    # older database, not of this one -- so losing it is the one loss this whole
    # function is built to prevent.
    for displaced in (replaced, *sidecars_of(replaced)):
        if displaced.is_file():
            _fsync_file(displaced)
    _fsync_directory(into)

    # Only now does anything leave the live path, and only the sidecars: the
    # displaced generation already holds its own copy of them, and the database
    # arriving under this name must not inherit them. The database itself stays
    # until the rename below replaces it.
    for sidecar in live_sidecars:
        sidecar.unlink(missing_ok=True)
    try:
        os.replace(replacement, db_path)
    except Exception:
        # The live generation goes back exactly as it was. Its database never
        # left this path; its log did, and the displaced copy is where it is, so
        # it is put back before the failure surfaces -- otherwise a restore that
        # failed would still have cost the machine everything that database
        # committed since its last checkpoint.
        for sidecar in live_sidecars:
            displaced_sidecar = replaced.with_name(replaced.name + sidecar.name[len(db_path.name) :])
            if not displaced_sidecar.is_file():
                continue
            try:
                _duplicate_file(displaced_sidecar, sidecar)
            except OSError:
                logger.warning("Failed to restore %s after an interrupted database swap", sidecar, exc_info=True)
        raise
    return replaced


def restore_sqlite_backup(backup_dir: Path, db_path: Path) -> Path | None:
    """Put a rollback point back into service, destroying nothing.

    A restore is a swap, never an overwrite. The database it takes out of service
    holds whatever the version being rolled back from committed before it failed,
    and that is the only copy of that data anywhere; a restore that dropped it
    would make the recovery step the unrecoverable one. It is moved into the
    rollback point's own directory and its path is returned, for the caller to
    record where an operator will look for it.

    The rollback point itself is also left intact. Its copy is read, not moved:
    the restore's own outcome can be the bad one, and the same point has to still
    be there to reach for again.

    The replacement is copied and verified BEFORE anything about the live
    database changes. Every cheaper order ends the same way -- a rollback point
    that turns out to be unreadable, discovered after the working database is
    already gone, leaving the machine with no database rather than a failed
    rollback.

    Restoring does not grow the backup window, so it does not prune: it adds one
    file inside a directory the window already counts, and that directory's
    eviction takes the file with it.
    """

    source_db = backup_dir.expanduser().resolve() / "vibe.sqlite"
    target = db_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".restoring")
    staged_paths = (staged, *(staged.with_name(staged.name + suffix) for suffix in ("-wal", "-shm")))

    for stale in staged_paths:
        stale.unlink(missing_ok=True)
    try:
        with sqlite3.connect(f"{source_db.as_uri()}?mode=ro", uri=True) as source:
            with sqlite3.connect(staged) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode = DELETE")
                check = destination.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    raise sqlite3.DatabaseError(f"SQLite rollback point quick_check failed: {check!r}")
        os.chmod(staged, 0o600)
        _fsync_file(staged)
    except Exception:
        for stale in staged_paths:
            stale.unlink(missing_ok=True)
        raise

    replaced = _swap_live_database(target, staged, into=source_db.parent)
    # Only the live directory, because the swap already synced the rollback point
    # it displaced into -- it has to, in the middle of its own sequence rather
    # than after it, and a second sync out here would suggest the ordering is
    # decided in two places when the whole reason that function exists is that it
    # is decided in one.
    _fsync_directory(target.parent)
    return replaced
