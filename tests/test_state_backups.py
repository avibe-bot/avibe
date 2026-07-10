from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
