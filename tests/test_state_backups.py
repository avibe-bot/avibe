from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from storage import backups as backups_module
from storage.backups import create_sqlite_migration_backup, prune_state_backups
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
        assert oldest.exists()
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

    assert all(path.exists() for path in existing)


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

    assert all(path.exists() for path in existing)


def test_retried_failing_migration_reuses_one_rollback_backup(monkeypatch, tmp_path: Path) -> None:
    # The property: retrying an upgrade that cannot succeed costs one backup per
    # distinct database state, not one per attempt, and buys that bound without
    # giving up a rollback point. A failed upgrade rolls back and leaves the
    # database untouched, so each retry was copying bytes that were already backed
    # up; on a full-size database a caller retried per request filled the disk,
    # and retention cannot reclaim a rollback copy while the failure it exists for
    # is still live.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(exist_ok=True)
    existing = [
        _legacy_sqlite_backup(backups_dir, f"vibe-pre-0026-repair-2026070{day}T020000Z.sqlite")
        for day in (7, 8, 9)
    ]

    def _blocked(*args, **kwargs):
        raise RuntimeError("upgrade blocked")

    monkeypatch.setattr("storage.migrations.command.upgrade", _blocked)

    made = []
    for _ in range(8):
        with pytest.raises(RuntimeError, match="upgrade blocked"):
            ensure_sqlite_state(db_path=db_path, state_dir=state_dir)
        made.append(tuple(sorted(backups_dir.glob("avibe-sqlite-migration-*"))))

    # The property is that retrying stops producing copies -- not that it never
    # produces a second one. The first attempt materializes the WAL sidecar, which is
    # a genuinely different database state and so genuinely deserves its own rollback
    # point. What filled the disk was growth per attempt, and that is what must stop.
    assert len(set(made[1:])) == 1, "retries must stop producing new backups"
    assert len(made[-1]) <= 2, "bounded by distinct database states, not by attempts"
    assert all(path.exists() for path in existing)


def test_backup_reuse_sees_a_commit_that_only_touched_the_wal(tmp_path: Path) -> None:
    # Reuse is only safe if "the same state" is decided from the bytes a copy would
    # read. Avibe runs SQLite in WAL mode, so a commit can land entirely in
    # vibe.sqlite-wal and leave the main database file untouched -- this test asserts
    # that is what happened before it asserts anything else. An identity read from
    # the main file's metadata would call that an already-copied state and hand back
    # a rollback point missing the commit, which is the one thing a rollback point
    # must never do.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    backups_dir = state_dir / "backups"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("create table records (value text not null)")
        writer.execute("insert into records values ('before')")
        writer.commit()

        first = create_sqlite_migration_backup(
            db_path, backups_dir=backups_dir, from_revisions={"old"}, to_revisions={"new"}
        )
        reused = create_sqlite_migration_backup(
            db_path, backups_dir=backups_dir, from_revisions={"old"}, to_revisions={"new"}
        )
        assert reused == first, "an unchanged database must reuse the copy it already made"

        before = db_path.stat()
        writer.execute("insert into records values ('after')")
        writer.commit()
        after = db_path.stat()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), (
            "this test is only meaningful while the commit stays out of the main file"
        )

        fresh = create_sqlite_migration_backup(
            db_path, backups_dir=backups_dir, from_revisions={"old"}, to_revisions={"new"}
        )
        assert fresh != first, "a commit living only in the WAL is still a new state"
        with sqlite3.connect(fresh / "vibe.sqlite") as backup:
            assert backup.execute("select count(*) from records").fetchone() == (2,)
    finally:
        writer.close()


def test_unreadable_wal_refuses_instead_of_reading_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Absence and unreadability are different facts, and only one of them is safe
    # to record. SQLite deletes the WAL on a clean close, so a database really can
    # have a main-file-only identity -- which means treating an unreadable WAL as
    # absent does not degrade the identity, it forges a previously valid one. This
    # test builds that exact collision: the same main file, once with no WAL beside
    # it and once with a WAL holding a commit, so an identity that drops the WAL
    # makes the two states equal and hands back a rollback point missing the commit.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    backups_dir = state_dir / "backups"

    seed = sqlite3.connect(db_path)
    try:
        seed.execute("PRAGMA journal_mode = WAL")
        seed.execute("create table records (value text not null)")
        seed.execute("insert into records values ('before')")
        seed.commit()
    finally:
        seed.close()
    wal_path = db_path.with_name(db_path.name + "-wal")
    assert not wal_path.exists(), "a clean close must leave no WAL, or the collision is not real"

    first = create_sqlite_migration_backup(
        db_path, backups_dir=backups_dir, from_revisions={"old"}, to_revisions={"new"}
    )

    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        before = db_path.stat()
        writer.execute("insert into records values ('after')")
        writer.commit()
        after = db_path.stat()
        assert wal_path.exists()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), (
            "this test is only meaningful while the commit stays out of the main file"
        )

        # Deny the read at the digest rather than with chmod: a permission bit is
        # ignored when the suite runs as root, and the property under test is what
        # the code does with a failed read, not which errno produced it.
        real_digest = backups_module._file_digest

        def _deny_wal(path: Path) -> str:
            if path.name.endswith("-wal"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_digest(path)

        monkeypatch.setattr(backups_module, "_file_digest", _deny_wal)

        existing = set(backups_dir.iterdir())
        with pytest.raises(RuntimeError, match="cannot identify database component"):
            create_sqlite_migration_backup(
                db_path, backups_dir=backups_dir, from_revisions={"old"}, to_revisions={"new"}
            )
        assert set(backups_dir.iterdir()) == existing, (
            "refusing must leave the database and its backups untouched"
        )
        with sqlite3.connect(first / "vibe.sqlite") as stale:
            assert stale.execute("select count(*) from records").fetchone() == (1,), (
                "the backup that must not be reused is the one taken before the WAL commit"
            )
    finally:
        writer.close()


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
