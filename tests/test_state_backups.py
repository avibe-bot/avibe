from __future__ import annotations

import errno
import json
import os
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
            to_revisions={"new"},
            now=datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc),
        )

        with sqlite3.connect(backup_dir / "vibe.sqlite") as backup:
            assert backup.execute("select value from records").fetchone() == ("preserved",)
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["managed_by"] == "avibe"
        assert manifest["kind"] == "sqlite-migration"
        assert manifest["from_revisions"] == []
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


def _stamp(path: Path, revision: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("create table if not exists alembic_version (version_num varchar(32) not null)")
        conn.execute("delete from alembic_version")
        conn.execute("insert into alembic_version (version_num) values (?)", (revision,))


def _fails_after_committing(db_path: Path, statement: str):
    """An upgrade that commits work and then raises.

    The shape a SQLite table rebuild inside an autocommit block leaves when a
    later check fails: the work is on disk, alembic never stamped, so every
    entry point that touches the store retries it.
    """

    def _upgrade(*args, **kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute(statement)
        raise RuntimeError("upgrade failed after committing")

    return _upgrade


def test_repeated_migration_failures_keep_the_rollback_window_bounded(monkeypatch, tmp_path: Path) -> None:
    # A migration that fails is retried, and every service entry point that
    # touches the store retries it once more. Pruning used to be gated on the
    # upgrade and the import that follow, so the one situation that produces
    # attempts without end was also the one where nothing ever reclaimed them.
    # Assert the bound itself rather than a count of attempts: whatever the
    # retry storm does, the window it leaves behind is within the retention
    # bound and every copy in it is restorable.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    before_the_storm = _db_contents(db_path)

    def failing_upgrade(*args, **kwargs):
        # Committing before failing is the ordinary shape, not a contrived one:
        # each attempt leaves the database a little further from where the storm
        # started, which is exactly what makes the earliest copy the valuable one
        # and every later copy a worse answer to the same question.
        with sqlite3.connect(db_path) as conn:
            conn.execute("create table if not exists boom (attempt integer)")
            conn.execute("insert into boom (attempt) values ((select count(*) from boom))")
        raise RuntimeError("migration boom")

    monkeypatch.setattr("storage.migrations.command.upgrade", failing_upgrade)

    for _ in range(SQLITE_BACKUP_RETENTION + 3):
        with pytest.raises(RuntimeError, match="migration boom"):
            ensure_sqlite_state(db_path=db_path, state_dir=state_dir)

    surviving = _sqlite_backup_roots(state_dir / "backups")
    assert 0 < len(surviving) <= SQLITE_BACKUP_RETENTION * 2
    for name in surviving:
        with sqlite3.connect(state_dir / "backups" / name / "vibe.sqlite") as backup:
            assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)
    # Bounded is half the promise. The attempts all copy a database sitting at
    # the same revisions, so a window counting copies fills with them and evicts
    # the one copy taken before the first attempt -- the only one predating
    # whatever the failing migration did on its way down, and the one an operator
    # reaches for. Counting rollback positions instead keeps it here for as long
    # as the storm lasts.
    assert any(
        _db_contents(state_dir / "backups" / name / "vibe.sqlite") == before_the_storm
        for name in surviving
    )


def _db_contents(path: Path) -> list[tuple[str, list[tuple]]]:
    with sqlite3.connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table' order by name")
        ]
        return [(table, conn.execute(f'select * from "{table}"').fetchall()) for table in tables]


def test_every_call_holds_the_database_as_it_stands_at_that_call(tmp_path: Path) -> None:
    # The one promise the window makes, stated as a property over however the
    # database moves rather than as a list of the ways it can. That distinction
    # is the whole history of this change: earlier revisions tried to recognise a
    # copy already held and reuse it, ranking copies by wall-clock adjacency,
    # then by the schema transition each attempt recorded, then by a fingerprint
    # of each copy's schema, then by the revision stamp -- and each rule was
    # defeated by a movement that leaves every label a backup can compare exactly
    # as it was. Two of them are exercised below: a migration that commits rows
    # without touching schema or stamp, and an operator restoring a copy and then
    # serving writes under a stamp that never moved.
    db_path = tmp_path / "vibe.sqlite"
    backups_dir = tmp_path / "backups"
    _stamp(db_path, "20260806_0047")
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table payload (value text)")
        conn.execute("insert into payload (value) values ('original')")

    taken: list[Path] = []

    def rollback_point() -> Path:
        backup = create_sqlite_migration_backup(db_path, backups_dir=backups_dir)
        assert _db_contents(backup / "vibe.sqlite") == _db_contents(db_path)
        taken.append(backup)
        return backup

    def write(statement: str) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(statement)

    first = rollback_point()
    write("update payload set value = 'rewritten'")
    rollback_point()
    shutil.copy(first / "vibe.sqlite", db_path)
    write("insert into payload (value) values ('accepted after the restore')")
    rollback_point()
    write("create table added (value text)")
    rollback_point()
    _stamp(db_path, "20260809_0049")
    rollback_point()

    assert len(set(taken)) == len(taken)
    # Two stamps were visited, so the window holds two positions, and of each the
    # first and the last copy taken there. The pre-restore original is the first
    # copy at its position and survives; the copy holding the writes accepted
    # after the restore is the last one there and survives too. Neither rule
    # alone gets both -- that is why the pair is what a position keeps.
    assert set(_sqlite_backup_roots(backups_dir)) == {taken[0].name, taken[3].name, taken[4].name}


def test_the_first_copy_at_a_position_survives_however_the_clock_moves(tmp_path: Path) -> None:
    # A position keeps its first and its last copy, and "first" was read back out
    # of the backup's timestamp -- the same mistake, one layer down, as every
    # rule this change already discarded: a label standing in for a fact only the
    # writer knew. A clock corrected backwards between two attempts dates the
    # later copy earlier, so the retry after a partial migration becomes both the
    # first and the last copy of its position and evicts the clean one it was
    # supposed to bracket. Stated as the property -- the database as it stood
    # when the position was first copied stays restorable -- because the clock
    # can be wrong in more ways than a test can list.
    db_path = tmp_path / "vibe.sqlite"
    backups_dir = tmp_path / "backups"
    _stamp(db_path, "20260806_0047")
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table payload (value text)")
        conn.execute("insert into payload (value) values ('clean')")
    before_the_storm = _db_contents(db_path)

    # Each attempt is stamped earlier than the one before it, and none of them
    # moves the revision, so all three copies belong to one position.
    taken: list[Path] = []
    for moment, damage in (
        (datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc), "first retry"),
        (datetime(2026, 8, 6, 11, 59, tzinfo=timezone.utc), "second retry"),
        (datetime(2026, 8, 6, 11, 58, tzinfo=timezone.utc), None),
    ):
        taken.append(create_sqlite_migration_backup(db_path, backups_dir=backups_dir, now=moment))
        if damage is not None:
            with sqlite3.connect(db_path) as conn:
                conn.execute("update payload set value = ?", (damage,))

    surviving = _sqlite_backup_roots(backups_dir)
    assert surviving == sorted({taken[0].name, taken[2].name})
    assert any(_db_contents(backups_dir / name / "vibe.sqlite") == before_the_storm for name in surviving)


def _as_pre_sequence_backup(backup_dir: Path) -> Path:
    """Turn a backup into one the previous release would have written.

    Produced by the current writer and then stripped, rather than hand-built, so
    a change to the manifest cannot leave this fixture describing a shape no
    release ever wrote.
    """

    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["backup_sequence"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return backup_dir


def test_backups_from_before_the_counter_still_come_first(tmp_path: Path) -> None:
    # A window is mixed for exactly as long as it takes the copies an older
    # release wrote to age out, and during that time the counter cannot order it
    # by itself. Falling back to the stamps for the whole group puts the clock
    # back in charge of the one decision it was just taken off -- with a clock
    # corrected backwards, a retry dated earlier than the clean copy from the
    # previous release becomes the first copy of the position and evicts it.
    #
    # The fact that settles it is not in the timestamps: a copy without a
    # counter was written by a release that did not have one, so it precedes
    # every counted copy no matter what either name says.
    db_path = tmp_path / "vibe.sqlite"
    backups_dir = tmp_path / "backups"
    _stamp(db_path, "20260806_0047")
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table payload (value text)")
        conn.execute("insert into payload (value) values ('clean')")
    before_the_storm = _db_contents(db_path)

    from_the_previous_release = _as_pre_sequence_backup(
        create_sqlite_migration_backup(
            db_path, backups_dir=backups_dir, now=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        )
    )
    retries: list[Path] = []
    for moment, damage in (
        (datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc), "first retry"),
        (datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc), "second retry"),
    ):
        with sqlite3.connect(db_path) as conn:
            conn.execute("update payload set value = ?", (damage,))
        retries.append(create_sqlite_migration_backup(db_path, backups_dir=backups_dir, now=moment))

    surviving = _sqlite_backup_roots(backups_dir)
    assert surviving == sorted({from_the_previous_release.name, retries[-1].name})
    assert any(_db_contents(backups_dir / name / "vibe.sqlite") == before_the_storm for name in surviving)


def test_a_position_dated_in_the_future_does_not_evict_a_newer_one(tmp_path: Path) -> None:
    # The window ranks positions against each other too, and that ranking read
    # the same timestamps. State carried from a machine running ahead is dated
    # into the future permanently, so ranking by the stamp parks it at the top
    # of the window forever and every genuinely newer position falls out beneath
    # it. Same class as the copies inside a position, so it reads the same
    # recorded order.
    db_path = tmp_path / "vibe.sqlite"
    backups_dir = tmp_path / "backups"
    taken: list[Path] = []
    for revision, moment in (
        ("20260806_0047", datetime(2099, 1, 1, tzinfo=timezone.utc)),
        ("20260809_0049", datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)),
        ("20260811_0050", datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)),
    ):
        _stamp(db_path, revision)
        taken.append(create_sqlite_migration_backup(db_path, backups_dir=backups_dir, now=moment))

    assert _sqlite_backup_roots(backups_dir) == sorted({taken[1].name, taken[2].name})


def test_the_manifest_records_the_revisions_read_from_the_copy(tmp_path: Path) -> None:
    # Callers sample the revisions before handing the work over, and another
    # process can advance the database in between. A manifest describing
    # something other than the copy it sits next to is worse than one that says
    # nothing: an operator reading it to choose a rollback point is told the copy
    # holds a schema it may never have held.
    db_path = tmp_path / "vibe.sqlite"
    backups_dir = tmp_path / "backups"
    _stamp(db_path, "20260811_0050")

    backup_dir = create_sqlite_migration_backup(
        db_path, backups_dir=backups_dir, to_revisions={"20260811_0051"}
    )

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["from_revisions"] == ["20260811_0050"]
    assert manifest["to_revisions"] == ["20260811_0051"]


def test_the_window_directory_is_durable_before_the_backup_counts(monkeypatch, tmp_path: Path) -> None:
    # Creating the backup directory also adds an entry to its parent. A crash
    # that keeps the upgraded database but loses that entry leaves no rollback
    # point at all, with every fsync this code makes having succeeded.
    #
    # Every attempt syncs, not only the one that created the directory: a
    # directory whose own entry never reached the disk is indistinguishable from
    # a durable one, so an attempt that finds the root already there has learned
    # nothing about it. Skipping the sync then propagates the first attempt's
    # failure through every later attempt, for as long as the window lives.
    synced: list[Path] = []
    monkeypatch.setattr(backups, "_fsync_directory", lambda path: synced.append(Path(path)))
    db_path = tmp_path / "state" / "vibe.sqlite"
    db_path.parent.mkdir()
    _stamp(db_path, "20260806_0047")

    for _attempt in range(2):
        synced.clear()
        create_sqlite_migration_backup(db_path)
        assert db_path.parent in synced


def test_backup_files_are_flushed_through_write_capable_handles(monkeypatch, tmp_path: Path) -> None:
    # `os.fsync` is not a read-only operation everywhere: on Windows it reaches
    # `FlushFileBuffers`, which refuses a handle opened without write access. A
    # read-only descriptor therefore turns every pre-migration backup on that
    # platform into a failed schema upgrade -- the flush is here to make an
    # upgrade safe, so it must not be the thing that stops one. Stated over
    # whatever this code flushes rather than over the call sites it has today.
    #
    # Directories are excluded because they are the opposite case: POSIX refuses
    # to open one for writing, and Windows refuses to open one at all.
    opened: dict[int, tuple[bool, int]] = {}
    flushed: list[tuple[bool, int]] = []
    real_open = os.open
    real_fsync = os.fsync

    def recording_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = (Path(path).is_dir(), flags)
        return fd

    def recording_fsync(fd):
        if fd in opened:
            flushed.append(opened[fd])
        real_fsync(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    db_path = tmp_path / "state" / "vibe.sqlite"
    db_path.parent.mkdir()
    _stamp(db_path, "20260806_0047")

    create_sqlite_migration_backup(db_path)

    file_flushes = [flags for is_dir, flags in flushed if not is_dir]
    assert file_flushes
    assert all(flags & (os.O_WRONLY | os.O_RDWR) for flags in file_flushes)


def test_a_fresh_rollback_point_outlives_copies_dated_after_it(tmp_path: Path) -> None:
    # A clock corrected backwards, or state carried from a machine running
    # ahead, leaves unrelated copies dated after everything the current
    # migration produces -- permanently, since nothing rewrites their names.
    # Ordering alone therefore cannot defend the copy a call has just made, so
    # the call protects it explicitly.
    db_path = tmp_path / "vibe.sqlite"
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    _stamp(db_path, "20260806_0047")
    for index in range(SQLITE_BACKUP_RETENTION + 1):
        _legacy_sqlite_backup(
            backups_dir, f"vibe-pre-002{index}-repair-2099010{index + 1}T020000Z.sqlite"
        )

    fresh = create_sqlite_migration_backup(db_path, backups_dir=backups_dir)

    surviving = _sqlite_backup_roots(backups_dir)
    assert len(surviving) == SQLITE_BACKUP_RETENTION
    assert fresh.name in surviving


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
