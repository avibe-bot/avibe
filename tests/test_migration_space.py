"""Low-space admission must precede even implicit read/write-open checkpoints."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from storage import backups, importer, migration_space as space, migrations
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines

pytestmark = pytest.mark.no_sqlite_template


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "state" / "vibe.sqlite"
    migrations.run_migrations(path, revision="20261112_0088")
    # Keep a writer open to leave committed pages in WAL. Admission may update
    # SHM/lock bookkeeping but must not change main/WAL or backup evidence.
    keeper = sqlite3.connect(path)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("INSERT INTO state_meta(key,value_json,updated_at) VALUES ('space-test',?,'now')", ('"' + "x" * 20000 + '"',))
    keeper.commit()
    assert Path(str(path) + "-wal").stat().st_size > 0
    yield path
    keeper.close()


def protected_bytes(path):
    files = [path, Path(str(path) + "-wal"), Path(str(path) + "-journal"), path.with_name("." + path.name + ".backup-lifecycle.json")]
    files += [p for p in (path.parent / "backups").rglob("*") if p.is_file()]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}


def free(monkeypatch, available):
    monkeypatch.setattr(space.shutil, "disk_usage", lambda _: SimpleNamespace(free=available))


@pytest.mark.parametrize("existing_backup", [False, True])
@pytest.mark.parametrize("entry", ["migration", "repair", "factory", "factory_after_read", "backup"])
def test_low_disk_refuses_before_checkpoint_backup_or_directory_creation(old_db, tmp_path, monkeypatch, entry, existing_backup):
    root = old_db.parent / "backups"
    assert not root.exists()
    if existing_backup:
        root.mkdir()
        (root / "sqlite-retained.sqlite").write_bytes(b"retained fixture backup")
        old_db.with_name("." + old_db.name + ".backup-lifecycle.json").write_text('{"version":1,"retain":"fixture"}')
    before = protected_bytes(old_db)
    free(monkeypatch, 1)
    def forbidden(*args, **kwargs):
        pytest.fail("capacity refusal reached mutating path")
    monkeypatch.setattr(backups, "_try_clone_sqlite_snapshot", forbidden)
    monkeypatch.setattr(backups, "prune_state_backups", forbidden)
    monkeypatch.setattr(migrations.command, "upgrade", forbidden)
    monkeypatch.setattr(importer, "_ensure_sqlite_target_dirs", forbidden)
    override = tmp_path / "other" / "backups"
    with pytest.raises(space.InsufficientMigrationSpaceError, match="Free at least") as error:
        if entry == "migration":
            migrations.run_migrations(old_db)
        elif entry == "repair":
            importer.repair_sqlite_state(db_path=old_db, state_dir=old_db.parent)
        elif entry.startswith("factory"):
            dispose_cached_sqlite_engines()
            if entry == "factory_after_read":
                readonly = create_sqlite_engine(old_db, read_only=True)
                with readonly.connect() as conn:
                    conn.exec_driver_sql("SELECT count(*) FROM alembic_version").scalar_one()
                readonly.dispose()
            create_sqlite_engine(old_db)
        else:
            backups.create_sqlite_migration_backup(old_db, backups_dir=override)
    assert protected_bytes(old_db) == before
    assert root.exists() == existing_backup and not override.parent.exists()
    assert "conservative reserve" in str(error.value)
    assert error.value.plan.required_bytes > 0


def test_threshold_is_aggregate_and_exact_bytes_are_accepted(old_db, monkeypatch):
    plan = space.plan_space(old_db, migration=True, coordinated=True)
    assert plan.catalog_reference_rows == 0
    assert plan.temporary_bytes == 0
    assert plan.required_bytes > plan.backup_bytes
    free(monkeypatch, plan.backup_bytes)
    with pytest.raises(space.InsufficientMigrationSpaceError):
        space.plan_space(old_db, migration=True, coordinated=True).require()
    free(monkeypatch, plan.required_bytes)
    space.plan_space(old_db, migration=True, coordinated=True).require()
    free(monkeypatch, plan.required_bytes - 1)
    with pytest.raises(space.InsufficientMigrationSpaceError, match="Free at least 0.01 GB"):
        space.plan_space(old_db, migration=True, coordinated=True).require()


def test_reference_count_matches_guarded_migration_index(old_db):
    cases = [
        ({"entries_ref": "a" * 64}, 1),
        ({"entries_ref": "b" * 64, "entries": None}, 0),
        ({"entries_ref": "c" * 64, "entries": []}, 0),
        ({"entries_ref": "d" * 63}, 0),
        ({"entries_ref": "E" * 64}, 0),
        ({"entries_ref": 7}, 0),
        (None, 0),
        (["a" * 64], 0),
    ]
    with sqlite3.connect(old_db) as conn:
        for i, (body, _) in enumerate(cases):
            conn.execute("INSERT INTO agent_events(id,platform,event_type,visibility,content_json,metadata_json,created_at,updated_at) "
                         "VALUES (?,'test','skill.catalog_result','trace',?,'{}','now','now')", (str(i), json.dumps(body)))
        conn.execute("INSERT INTO agent_events(id,platform,event_type,visibility,content_json,metadata_json,created_at,updated_at) "
                     "VALUES ('bad','test','skill.catalog_result','trace','not json','{}','now','now')")
    plan = space.plan_space(old_db, migration=True, coordinated=True)
    assert plan.catalog_reference_rows == sum(expected for _, expected in cases)
    assert plan.temporary_bytes > 0
    with closing(sqlite3.connect(f"{old_db.as_uri()}?mode=ro", uri=True)) as conn:
        import importlib
        boundary = importlib.import_module(space._BOUNDARY_MODULE)
        query = ("EXPLAIN QUERY PLAN SELECT count(*) FROM agent_events INDEXED BY ix_agent_events_skill_created_id "
                 "WHERE visibility = 'trace' AND event_type IN ('skill.catalog_result', 'skill.load_result') "
                 f"AND ({boundary._PREDICATE})")
        detail = "\n".join(row[-1] for row in conn.execute(query))
        assert "USING INDEX ix_agent_events_skill_created_id" in detail


def test_readonly_ready_and_current_future_opens_do_not_need_upgrade_space(tmp_path, monkeypatch):
    path = tmp_path / "current.sqlite"
    migrations.run_migrations(path)
    free(monkeypatch, 0)
    assert migrations.run_migrations(path).backup_revisions is None
    assert migrations.background_tables_ready(path)
    engine = create_sqlite_engine(path)
    engine.dispose()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE alembic_version SET version_num='20991231_9999'")
    dispose_cached_sqlite_engines()
    assert migrations.run_migrations(path).compatible_open
    engine = create_sqlite_engine(path)
    engine.dispose()


def test_readonly_old_schema_query_is_not_an_upgrade(old_db, monkeypatch):
    free(monkeypatch, 0)
    before = protected_bytes(old_db)
    assert migrations.background_tables_ready(old_db)
    engine = create_sqlite_engine(old_db, read_only=True)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").scalar_one() == "2"
    engine.dispose()
    assert protected_bytes(old_db) == before


def test_standalone_backup_always_reserves_full_logical_copy(old_db, monkeypatch):
    plan = space.plan_space(old_db)
    assert plan.backup_bytes == old_db.stat().st_size + Path(str(old_db) + "-wal").stat().st_size
    assert plan.database_workspace_bytes >= Path(str(old_db) + "-wal").stat().st_size
    free(monkeypatch, plan.required_bytes - 1)
    monkeypatch.setattr(backups, "_try_clone_sqlite_snapshot", lambda *args: pytest.fail("clone cannot waive reserve"))
    with pytest.raises(space.InsufficientMigrationSpaceError):
        backups.create_sqlite_migration_backup(old_db)


def test_unreadable_capacity_fails_closed_instead_of_inventing_free_bytes(old_db, monkeypatch):
    def denied(_):
        raise OSError("fixture quota reading unavailable")
    monkeypatch.setattr(space.shutil, "disk_usage", denied)
    with pytest.raises(space.MigrationSpaceCheckError, match="could not be verified"):
        space.plan_space(old_db).require()


def test_sqlite_temp_order_uses_sqlite_tmpdir_before_tmpdir(tmp_path, monkeypatch):
    first, second = tmp_path / "sqlite", tmp_path / "python"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("SQLITE_TMPDIR", str(first))
    monkeypatch.setenv("TMPDIR", str(second))
    with closing(sqlite3.connect(":memory:")) as conn:
        assert space._sqlite_temp_directory(conn) == first


def test_unknown_pool_sharing_checks_all_role_reserve_against_every_path(old_db, tmp_path, monkeypatch):
    # Same-device aliases cannot cause the caller to spend reported free bytes
    # twice; lowest reading wins (also conservative for path-specific quotas).
    destination = tmp_path / "backup-volume"
    destination.mkdir()
    before = space.plan_space(old_db, backups_dir=destination, migration=True)
    assert before.temporary_bytes > 0
    monkeypatch.setattr(space.shutil, "disk_usage", lambda p: SimpleNamespace(
        free=before.required_bytes - 1 if p == destination else before.required_bytes * 10,
    ))
    with pytest.raises(space.InsufficientMigrationSpaceError):
        space.plan_space(old_db, backups_dir=destination, migration=True).require()


def _crashed_database(tmp_path, *, wal):
    path = tmp_path / "crashed.sqlite"
    migrations.run_migrations(path, revision="20261112_0088")
    # No live keeper: this is the close-checkpoint risk the RO guard prevents.
    subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
wal = sys.argv[2] == 'wal'
conn.execute('PRAGMA journal_mode=' + ('WAL' if wal else 'DELETE'))
conn.execute('PRAGMA wal_autocheckpoint=0')
conn.execute('CREATE TABLE crash_fixture(data BLOB)')
conn.commit()
conn.execute('PRAGMA cache_size=1')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE alembic_version SET version_num='uncommitted'" if not wal else "SELECT 1")
for _ in range(20):
    conn.execute('INSERT INTO crash_fixture VALUES (zeroblob(8192))')
if wal:
    conn.commit()
os._exit(0)
""", str(path), "wal" if wal else "journal"], check=True)
    return path


@pytest.mark.parametrize("wal", [True, False])
def test_crash_left_wal_or_hot_journal_refuses_without_recovery(tmp_path, monkeypatch, wal):
    path = _crashed_database(tmp_path, wal=wal)
    before = protected_bytes(path)
    free(monkeypatch, 1)
    with pytest.raises(space.InsufficientMigrationSpaceError):
        migrations.run_migrations(path)
    assert protected_bytes(path) == before
    assert not (path.parent / "backups").exists()


def test_explicit_readonly_factory_never_recovers_hot_journal(tmp_path):
    from sqlalchemy.exc import DBAPIError
    path = _crashed_database(tmp_path, wal=False)
    before = protected_bytes(path)
    with pytest.raises(DBAPIError):
        create_sqlite_engine(path, read_only=True)
    assert protected_bytes(path) == before


def test_recovered_attempt_retains_generic_policy_and_reports_later_deficit(tmp_path, monkeypatch):
    path = _crashed_database(tmp_path, wal=False)
    real_plan = space.plan_space
    calls = []
    def tracked(*args, **kwargs):
        calls.append(kwargs.get("coordinated", False))
        return real_plan(*args, **kwargs)
    monkeypatch.setattr(space, "plan_space", tracked)
    # Admission to crash recovery succeeds, then a competing disk user consumes
    # capacity. The second error must acknowledge completed recovery explicitly.
    readings = iter([10**12] * 3 + [0] * 10)
    monkeypatch.setattr(space.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(readings)))
    with pytest.raises(space.InsufficientMigrationSpaceError, match="crash recovery already completed"):
        migrations.run_migrations(path)
    assert calls == [False, False]
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261112_0088"
    assert not (path.parent / "backups").exists()


def test_non_recovery_errors_cannot_authorize_readwrite_fallback(tmp_path):
    path = tmp_path / "vibe.sqlite"
    path.with_name(path.name + "-journal").write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7")
    for message in ("database is locked", "database disk image is malformed", "disk I/O error"):
        assert not migrations._needs_hot_journal_recovery(sqlite3.OperationalError(message), path)
    legacy = sqlite3.OperationalError("attempt to write a readonly database")
    assert migrations._needs_hot_journal_recovery(legacy, path)  # Python 3.10 journal-aware fallback.
    legacy.sqlite_errorcode = 8
    assert not migrations._needs_hot_journal_recovery(legacy, path)


def test_late_failure_is_not_relabelled_as_premutation_refusal(old_db, monkeypatch):
    free(monkeypatch, 10**15)
    monkeypatch.setattr(migrations, "create_sqlite_migration_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr(migrations, "begin_sqlite_backup_migration", lambda *args: None)
    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(migrations.command, "upgrade", full)
    with pytest.raises(sqlite3.OperationalError, match="database or disk is full"):
        migrations.run_migrations(old_db)
