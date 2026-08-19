from __future__ import annotations

import errno
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from storage import backups
from storage.backups import (
    SQLITE_BACKUP_RETENTION,
    _JSON_BACKUP_RE,
    create_sqlite_migration_backup,
    prune_state_backups,
)
from storage.background import SQLiteBackgroundTaskStore
from storage.importer import _backup_json_state, ensure_sqlite_state
from storage.migrations import run_migrations


def _legacy_json_backup(backups_dir: Path, timestamp: str) -> Path:
    path = backups_dir / f"sqlite-state-migration-{timestamp}"
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(
        json.dumps({"created_at": "2026-07-01T00:00:00+00:00", "files": {}}),
        encoding="utf-8",
    )
    return path


def _legacy_sqlite_backup(backups_dir: Path, name: str) -> Path:
    path = backups_dir / name
    path.write_bytes(b"sqlite backup")
    path.with_name(path.name + "-wal").write_bytes(b"wal")
    path.with_name(path.name + "-shm").write_bytes(b"shm")
    return path


def _table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?", (table,)
        ).fetchone() is not None


def _sqlite_backup_roots(backups_dir: Path) -> list[str]:
    """Names in the sqlite rollback window.

    Legacy copies keep -wal/-shm companions beside them, and JSON snapshots are
    a separate window with its own bound despite the similar name. Derive that
    exclusion from the module's own pattern rather than restating it here.
    """

    return sorted(
        path.name
        for path in backups_dir.iterdir()
        if not path.name.endswith(("-wal", "-shm")) and not _JSON_BACKUP_RE.fullmatch(path.name)
    )


def test_prune_state_backups_keeps_bounded_rollbacks_and_unknown_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(parents=True)
    json_backups = [
        _legacy_json_backup(backups_dir, f"2026070{day}T010000Z")
        for day in range(1, 6)
    ]
    sqlite_backups = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in range(1, 5)
    ]
    unknown = backups_dir / "manual-keep.sqlite"
    unknown.write_bytes(b"user managed")
    active_named = [backups_dir / name for name in ("vibe.sqlite", "vibe.sqlite-wal", "vibe.sqlite-shm")]
    for path in active_named:
        path.write_bytes(b"not a managed backup")

    removed = prune_state_backups(backups_dir)

    assert set(removed) == set(json_backups[:2] + sqlite_backups[:2])
    assert all(not path.exists() for path in json_backups[:2])
    assert all(path.exists() for path in json_backups[2:])
    for path in sqlite_backups[:2]:
        assert not path.exists()
        assert not path.with_name(path.name + "-wal").exists()
        assert not path.with_name(path.name + "-shm").exists()
    assert all(path.exists() for path in sqlite_backups[2:])
    assert unknown.read_bytes() == b"user managed"
    assert all(path.exists() for path in active_named)


def test_prune_state_backups_preserves_invalid_or_incomplete_candidates(tmp_path: Path) -> None:
    backups_dir = tmp_path / "backups"
    incomplete = backups_dir / "sqlite-state-migration-20260701T010000Z"
    incomplete.mkdir(parents=True)
    invalid = backups_dir / "avibe-sqlite-migration-20260701T010000Z"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("{}", encoding="utf-8")
    invalid_date = backups_dir / "sqlite-state-migration-20269999T999999Z"
    invalid_date.mkdir()
    (invalid_date / "manifest.json").write_text(
        json.dumps({"created_at": "invalid", "files": {}}),
        encoding="utf-8",
    )

    assert prune_state_backups(backups_dir, json_retention=0, sqlite_retention=0) == []
    assert incomplete.exists()
    assert invalid.exists()
    assert invalid_date.exists()


def test_create_sqlite_migration_backup_is_consistent_without_copying_live_sidecars(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("create table records (value text not null)")
        writer.execute("insert into records values ('preserved')")
        writer.commit()
        assert db_path.with_name("vibe.sqlite-wal").exists()
        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        oldest = _legacy_sqlite_backup(backups_dir, "vibe-pre-0026-repair-20260708T020000Z.sqlite")
        previous = _legacy_sqlite_backup(backups_dir, "vibe-pre-0026-repair-20260709T020000Z.sqlite")

        backup_dir = create_sqlite_migration_backup(
            db_path,
            from_revisions={"old"},
            to_revisions={"new"},
            now=datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc),
        )

        with sqlite3.connect(backup_dir / "vibe.sqlite") as backup:
            assert backup.execute("select value from records").fetchone() == ("preserved",)
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["managed_by"] == "avibe"
        assert manifest["kind"] == "sqlite-migration"
        assert manifest["from_revisions"] == ["old"]
        assert manifest["to_revisions"] == ["new"]
        # Legacy repair copies are part of the same sqlite window, so adding a
        # backup prunes them to the same bound instead of accumulating beside
        # them -- companions included.
        assert not oldest.exists()
        assert not oldest.with_name(oldest.name + "-wal").exists()
        assert previous.exists()
        assert not (backup_dir / "vibe.sqlite-wal").exists()
        assert not (backup_dir / "vibe.sqlite-shm").exists()
        assert db_path.exists()
        assert db_path.with_name("vibe.sqlite-wal").exists()
    finally:
        writer.close()


def test_json_backup_creation_applies_retention(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "settings.json").write_text("{}", encoding="utf-8")

    for _ in range(5):
        _backup_json_state(state_dir)

    assert len(list((state_dir / "backups").glob("sqlite-state-migration-*"))) == 5
    ensure_sqlite_state(db_path=state_dir / "vibe.sqlite", state_dir=state_dir)

    backups = sorted((state_dir / "backups").glob("sqlite-state-migration-*"))
    assert len(backups) == 3
    assert all(json.loads((path / "manifest.json").read_text(encoding="utf-8"))["managed_by"] == "avibe" for path in backups)


def test_failed_json_backup_removes_its_incomplete_directory(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "storage.importer.shutil.copy2",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")),
    )

    with pytest.raises(OSError, match="full"):
        _backup_json_state(state_dir)

    assert list((state_dir / "backups").iterdir()) == []


def test_failed_sqlite_backup_keeps_existing_rollback_window(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(parents=True)
    db_path = state_dir / "vibe.sqlite"
    db_path.write_bytes(b"not opened")
    existing = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in (8, 9)
    ]
    monkeypatch.setattr("storage.backups.sqlite3.connect", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(OSError, match="full"):
        create_sqlite_migration_backup(db_path)

    assert all(path.exists() for path in existing)


def test_startup_prunes_only_after_migration_backup_succeeds(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    backups_dir = state_dir / "backups"
    backups_dir.mkdir()
    existing = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in (7, 8, 9)
    ]
    monkeypatch.setattr(
        "storage.migrations.create_sqlite_migration_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")),
    )

    with pytest.raises(OSError, match="full"):
        ensure_sqlite_state(db_path=db_path, state_dir=state_dir)

    assert all(path.exists() for path in existing)


def test_repeated_migration_failures_keep_the_rollback_window_bounded(monkeypatch, tmp_path: Path) -> None:
    # A migration that fails is retried, and the OAuth callback retries it once
    # per unauthenticated request. Every attempt copies the whole database, and
    # pruning used to be gated on the upgrade and the import that follow -- so
    # the one situation that produces attempts without end was also the one
    # where nothing ever reclaimed them. Assert the bound itself rather than a
    # count of attempts: whatever the retry storm does, the window it leaves
    # behind is the retention bound, and what survives is the newest copies.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    monkeypatch.setattr(
        "storage.migrations.command.upgrade",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("migration boom")),
    )

    created: list[str] = []
    for _ in range(SQLITE_BACKUP_RETENTION + 3):
        with pytest.raises(RuntimeError, match="migration boom"):
            ensure_sqlite_state(db_path=db_path, state_dir=state_dir)
        created.append(max(_sqlite_backup_roots(state_dir / "backups")))

    surviving = _sqlite_backup_roots(state_dir / "backups")
    assert len(surviving) == SQLITE_BACKUP_RETENTION
    assert surviving == sorted({created[0], created[-1]})
    # A bounded window is only worth keeping if it is still restorable.
    for name in surviving:
        with sqlite3.connect(state_dir / "backups" / name / "vibe.sqlite") as backup:
            assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_retry_window_keeps_the_snapshot_taken_before_a_partial_upgrade(monkeypatch, tmp_path: Path) -> None:
    # An upgrade can commit part of its work and then raise -- a SQLite table
    # rebuild inside an autocommit block that fails a later check is exactly
    # that shape. From the next attempt on, every copy is of the half-migrated
    # database, so keeping the newest copies fills the window with damage and
    # discards the only snapshot that can undo it. Consecutive manifests show
    # the run never reached where it was headed, which is what makes the
    # attempts one episode; the window keeps its ends.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")

    def _partially_commit_then_fail(*args, **kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute("create table if not exists half_migrated (value text)")
        raise RuntimeError("upgrade failed after committing")

    monkeypatch.setattr("storage.migrations.command.upgrade", _partially_commit_then_fail)

    for _ in range(SQLITE_BACKUP_RETENTION + 2):
        with pytest.raises(RuntimeError, match="upgrade failed after committing"):
            run_migrations(db_path)

    surviving = _sqlite_backup_roots(state_dir / "backups")
    assert len(surviving) == SQLITE_BACKUP_RETENTION
    intact = [
        name
        for name in surviving
        if not _table_exists(state_dir / "backups" / name / "vibe.sqlite", "half_migrated")
    ]
    assert intact, "the snapshot taken before the first partial upgrade must survive the retries"


def test_future_dated_copies_do_not_outrank_the_migration_in_progress(monkeypatch, tmp_path: Path) -> None:
    # A clock corrected backwards, or state carried from a machine running
    # ahead, leaves unrelated copies dated after everything the current
    # migration produces. Being newest on disk is not the same as being newest
    # in creation order: ranked by timestamp those copies take the window, and
    # both ends of the failing migration -- the clean snapshot and the latest
    # one -- are lost to backups that have nothing to do with it.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    backups_dir = state_dir / "backups"
    backups_dir.mkdir()
    from_the_future = _legacy_sqlite_backup(backups_dir, "vibe-pre-0026-repair-20990101T020000Z.sqlite")

    def _partially_commit_then_fail(*args, **kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute("create table if not exists half_migrated (value text)")
        raise RuntimeError("upgrade failed after committing")

    monkeypatch.setattr("storage.migrations.command.upgrade", _partially_commit_then_fail)

    for _ in range(SQLITE_BACKUP_RETENTION + 1):
        with pytest.raises(RuntimeError, match="upgrade failed after committing"):
            run_migrations(db_path)

    surviving = _sqlite_backup_roots(backups_dir)
    assert len(surviving) == SQLITE_BACKUP_RETENTION
    assert from_the_future.name not in surviving, "a future timestamp is not a claim on the window"
    assert any(
        not _table_exists(backups_dir / name / "vibe.sqlite", "half_migrated") for name in surviving
    ), "the snapshot from before the partial upgrade must keep its slot"


def _attempt(
    db_path: Path,
    backups_dir: Path,
    *,
    hour: int,
    frm: tuple[str, ...] = ("20260806_0047",),
) -> Path:
    """One migration attempt's backup, at a chosen wall-clock hour."""

    return create_sqlite_migration_backup(
        db_path,
        backups_dir=backups_dir,
        from_revisions=frm,
        to_revisions=("20260811_0051",),
        now=datetime(2026, 7, 10, hour, 0, tzinfo=timezone.utc),
    )


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("create table marker (value text)")


def _write(path: Path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("insert into marker (value) values (?)", (value,))


def _rows(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return sorted(row[0] for row in conn.execute("select value from marker"))


def _partially_apply(path: Path) -> None:
    """The shape an upgrade leaves when it commits work and then raises.

    A table appears; the revision the database is stamped with does not move,
    because alembic never reached the stamp.
    """

    with sqlite3.connect(path) as conn:
        conn.execute("create table if not exists half_migrated (value text)")


def test_the_copy_that_keeps_a_state_is_the_one_holding_its_writes(tmp_path: Path) -> None:
    # Two attempts at the same failing migration are copies of one state, so one
    # slot covers both -- and it has to be the copy carrying the writes made
    # since the other. A clock correction landing mid-storm stamps the later
    # attempt as the earlier of the two, so choosing by wall-clock time keeps the
    # copy that is missing everything written in between. Order comes from a
    # counter this module writes instead.
    db_path = tmp_path / "vibe.sqlite"
    _database(db_path)
    backups_dir = tmp_path / "backups"

    _write(db_path, "before")
    clean = _attempt(db_path, backups_dir, hour=1)
    _partially_apply(db_path)
    _attempt(db_path, backups_dir, hour=15)
    _write(db_path, "after")
    corrected = _attempt(db_path, backups_dir, hour=5)

    assert _sqlite_backup_roots(backups_dir) == sorted({clean.name, corrected.name})
    assert _rows(corrected / "vibe.sqlite") == ["after", "before"]


def test_a_storm_of_identical_retries_holds_one_slot_between_them(tmp_path: Path) -> None:
    # Why the window survives a retry storm at all. Every attempt after the
    # first copies the same half-migrated database, so they are one state and
    # hold one slot between them however many there are -- which is what leaves
    # room for the snapshot taken before the damage, a state nothing else can
    # produce.
    db_path = tmp_path / "vibe.sqlite"
    _database(db_path)
    backups_dir = tmp_path / "backups"

    clean = _attempt(db_path, backups_dir, hour=1)
    _partially_apply(db_path)
    for hour in range(2, 10):
        latest = _attempt(db_path, backups_dir, hour=hour)

    assert _sqlite_backup_roots(backups_dir) == sorted({clean.name, latest.name})


def test_restoring_a_rollback_point_makes_it_the_current_state_again(tmp_path: Path) -> None:
    # An operator restores the clean snapshot, the service comes back up on the
    # old schema and accepts writes, and the migration fails again. The restored
    # database is that clean state once more, so the snapshot taken after it is a
    # newer copy of the same state and supersedes the one restored from. That is
    # the whole point: it is the only copy that is both clean and holds the
    # writes made since the restore, and a rollback that landed on the original
    # would lose every one of them.
    db_path = tmp_path / "vibe.sqlite"
    _database(db_path)
    backups_dir = tmp_path / "backups"

    _write(db_path, "before")
    restored_from = _attempt(db_path, backups_dir, hour=1)
    _partially_apply(db_path)
    _attempt(db_path, backups_dir, hour=2)

    shutil.copy(restored_from / "vibe.sqlite", db_path)
    _write(db_path, "after restore")
    restored = _attempt(db_path, backups_dir, hour=3)
    _partially_apply(db_path)
    latest = _attempt(db_path, backups_dir, hour=4)

    assert _sqlite_backup_roots(backups_dir) == sorted({restored.name, latest.name})
    assert _rows(restored / "vibe.sqlite") == ["after restore", "before"]


def test_a_duplicated_sequence_cannot_take_a_state_out_of_the_window(
    monkeypatch, tmp_path: Path
) -> None:
    # Sequence allocation reads the highest sequence on disk and adds one, and
    # two processes can do that at the same moment and both win -- `storage.
    # migrations` serializes upgrades with a lock that is process-local, so the
    # UI server and the controller can reach this at once. The collision leaves
    # ordering within a state falling back on the clock, and a process that
    # stalled between reserving its directory and copying can publish the
    # already-damaged database stamped earlier than the clean copy that beat it
    # there. Identifying a copy by what it is rather than by its place in a run
    # of attempts is what makes that harmless: the clean snapshot is a state of
    # its own, and no ordering accident can take its slot.
    monkeypatch.setattr(backups, "_next_sequence", lambda _root: 1)
    db_path = tmp_path / "vibe.sqlite"
    _database(db_path)
    backups_dir = tmp_path / "backups"

    clean = _attempt(db_path, backups_dir, hour=2)
    _partially_apply(db_path)
    _attempt(db_path, backups_dir, hour=1)
    latest = _attempt(db_path, backups_dir, hour=3)

    assert _sqlite_backup_roots(backups_dir) == sorted({clean.name, latest.name})


def test_a_migration_that_moves_only_the_revision_is_a_new_rollback_point(tmp_path: Path) -> None:
    # Not every migration changes the schema. One that only moves data and
    # stamps the new revision leaves the DDL identical on both sides of it, and
    # the two copies are still different rollback points: restoring the wrong
    # one leaves the database at a revision whose migration has not run against
    # its data. So the revisions belong in the identity next to the fingerprint,
    # and neither half stands in for the other.
    #
    # Here the data-only migration settles and the next one starts failing. The
    # snapshot taken before the data moved is the only way back behind it, and
    # it has to outrank a second copy of the state the retries are stuck in.
    db_path = tmp_path / "vibe.sqlite"
    _database(db_path)
    backups_dir = tmp_path / "backups"

    before = _attempt(db_path, backups_dir, hour=1, frm=("20260806_0047",))
    _attempt(db_path, backups_dir, hour=2, frm=("20260809_0049",))
    after = _attempt(db_path, backups_dir, hour=3, frm=("20260809_0049",))

    assert _sqlite_backup_roots(backups_dir) == sorted({before.name, after.name})


def test_new_backup_reaches_stable_storage_before_older_ones_are_deleted(monkeypatch, tmp_path: Path) -> None:
    # A backup that replaces durable copies has to be durable first. If the
    # filesystem is free to persist the deletions while still holding the new
    # rename and manifest in cache, a power loss here leaves no rollback point
    # at all -- the one outcome this whole window exists to prevent.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    with sqlite3.connect(db_path) as writer:
        writer.execute("create table records (value text not null)")
    backups_dir = state_dir / "backups"
    backups_dir.mkdir()
    for day in (7, 8, 9):
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")

    journal: list[tuple[str, Path]] = []
    for name in ("_fsync_file", "_fsync_directory"):
        real = getattr(backups, name)
        monkeypatch.setattr(backups, name, lambda path, _real=real: (journal.append(("fsync", path)), _real(path))[1])
    real_remove = backups._remove_candidate
    monkeypatch.setattr(
        backups,
        "_remove_candidate",
        lambda candidate: (journal.append(("remove", candidate.root)), real_remove(candidate))[1],
    )

    backup_dir = create_sqlite_migration_backup(db_path, now=datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc))

    removals = [index for index, (action, _) in enumerate(journal) if action == "remove"]
    assert removals, "the prune must have had something to delete for this ordering to mean anything"
    synced = {path for action, path in journal[: removals[0]] if action == "fsync"}
    assert backup_dir / "manifest.json" in synced
    assert backup_dir in synced
    assert backup_dir.parent in synced


def test_storage_failure_while_flushing_deletes_nothing(monkeypatch, tmp_path: Path) -> None:
    # A sync that fails because the disk is full or the device errored says the
    # new copy may not be on it. Swallowing that and pruning anyway deletes
    # durable rollback points in exchange for one that might not exist after the
    # next power loss -- the exact trade this window exists to refuse. A
    # platform that has no directory sync is a different answer and stays quiet.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    with sqlite3.connect(db_path) as writer:
        writer.execute("create table records (value text not null)")
    backups_dir = state_dir / "backups"
    backups_dir.mkdir()
    existing = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in (7, 8, 9)
    ]

    def _out_of_space(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backups.os, "fsync", _out_of_space)

    with pytest.raises(OSError) as failure:
        create_sqlite_migration_backup(db_path, now=datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc))

    assert failure.value.errno == errno.ENOSPC
    assert all(path.exists() for path in existing), "a backup that may not be durable prunes nothing"
    assert not list(backups_dir.glob("avibe-sqlite-migration-*")), "the incomplete backup is cleaned up"


def test_startup_keeps_json_rollbacks_when_new_snapshot_fails(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(parents=True)
    existing = [
        _legacy_json_backup(backups_dir, f"2026070{day}T010000Z")
        for day in range(1, 6)
    ]
    monkeypatch.setattr(
        "storage.importer._backup_json_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")),
    )

    with pytest.raises(OSError, match="full"):
        ensure_sqlite_state(db_path=state_dir / "vibe.sqlite", state_dir=state_dir)

    assert all(path.exists() for path in existing)


def test_startup_keeps_json_rollbacks_when_import_after_snapshot_fails(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(parents=True)
    existing = [
        _legacy_json_backup(backups_dir, f"2026070{day}T010000Z")
        for day in range(1, 6)
    ]
    monkeypatch.setattr(
        "storage.importer._parse_json_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid import")),
    )

    with pytest.raises(ValueError, match="invalid import"):
        ensure_sqlite_state(db_path=state_dir / "vibe.sqlite", state_dir=state_dir)

    assert all(path.exists() for path in existing)


def test_startup_keeps_sqlite_rollbacks_when_import_after_upgrade_fails(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    backups_dir = state_dir / "backups"
    backups_dir.mkdir()
    existing = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in (7, 8, 9)
    ]
    monkeypatch.setattr(
        "storage.importer._parse_json_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid import")),
    )

    with pytest.raises(ValueError, match="invalid import"):
        ensure_sqlite_state(db_path=db_path, state_dir=state_dir)

    # What has to survive a failure is the ability to roll back, not every copy
    # ever made. Asserting the latter reads as the stronger guarantee and is
    # what let a repeated failure accumulate one full database per attempt.
    surviving = _sqlite_backup_roots(backups_dir)
    assert len(surviving) == SQLITE_BACKUP_RETENTION
    assert existing[-1].exists(), "the newest pre-existing rollback must be kept"
    assert any(name.startswith("avibe-sqlite-migration-") for name in surviving), (
        "the backup taken for this attempt is the rollback point for it"
    )


def test_failed_schema_upgrade_keeps_existing_sqlite_rollbacks(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    db_path.parent.mkdir()
    run_migrations(db_path, revision="20260627_0025")
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir()
    existing = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in (7, 8, 9)
    ]
    monkeypatch.setattr(
        "storage.migrations.command.upgrade",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("upgrade failed")),
    )

    with pytest.raises(RuntimeError, match="upgrade failed"):
        run_migrations(db_path)

    # Same property as the import-failure case: a rollback point survives the
    # failure, bounded rather than accumulated.
    surviving = _sqlite_backup_roots(backups_dir)
    assert len(surviving) == SQLITE_BACKUP_RETENTION
    assert existing[-1].exists(), "the newest pre-existing rollback must be kept"
    assert any(name.startswith("avibe-sqlite-migration-") for name in surviving), (
        "the backup taken for this attempt is the rollback point for it"
    )


def test_run_migrations_backs_up_only_when_existing_schema_advances(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    db_path.parent.mkdir()

    run_migrations(db_path, revision="20260627_0025")
    assert not list((db_path.parent / "backups").glob("avibe-sqlite-migration-*"))

    run_migrations(db_path)
    first_backups = list((db_path.parent / "backups").glob("avibe-sqlite-migration-*"))
    assert len(first_backups) == 1

    run_migrations(db_path)
    assert list((db_path.parent / "backups").glob("avibe-sqlite-migration-*")) == first_backups


def test_background_store_schema_upgrade_uses_migration_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    db_path.parent.mkdir()
    run_migrations(db_path, revision="20260627_0025")

    store = SQLiteBackgroundTaskStore(db_path)
    store.close()

    backups = list((db_path.parent / "backups").glob("avibe-sqlite-migration-*"))
    assert len(backups) == 1
