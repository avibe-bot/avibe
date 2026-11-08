"""Uncached initialization must prove readiness without the repair pipeline.

The previous entry always took the migration lock, even with complete receipts.
These tests own that startup boundary; they do not promise revalidation of a
process's cached target or a lease between ensure and a caller's later operation.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing

import pytest
from tests.migration_helpers import downgrade_legacy_schema
from sqlalchemy import Engine, event

from storage import importer, migrations
from storage.lock import migration_lock_path_for

pytestmark = pytest.mark.no_sqlite_template

DATA_MARKER = "migrations.legacy_deferred_resource_contexts.v1"
COMPLETED = {
    "schema_version": 3,
    "state": "completed",
    "instance_id": "synthetic-personal-instance",
    "instance_kind": "personal",
    "completed_at": "2026-11-06T00:00:00+00:00",
    "updated_at": "2026-11-06T00:00:00+00:00",
}


@pytest.fixture
def ready_db(tmp_path, sqlite_db_factory):
    """A released current database with already-completed data migration."""
    db = sqlite_db_factory(tmp_path / "state" / "vibe.sqlite")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT OR REPLACE INTO state_meta (key, value_json, updated_at) VALUES (?, ?, ?)",
            (DATA_MARKER, json.dumps(COMPLETED), COMPLETED["updated_at"]),
        )
        conn.commit()
    importer.reset_ensured_sqlite_state()
    return db


def test_current_startup_is_read_only_even_behind_migration_and_sqlite_writers(
    ready_db, monkeypatch, hold_migration_lock_elsewhere,
):
    def forbidden(*args, **kwargs):
        pytest.fail("current startup entered the write-capable repair path")

    for name in (
        "MigrationFileLock", "run_migrations", "create_sqlite_engine", "_current_counts",
        "begin_sqlite_backup_validation", "complete_sqlite_backup_validation", "prune_state_backups",
        "_ensure_sqlite_target_dirs",
    ):
        monkeypatch.setattr(importer, name, forbidden)

    statements = []
    connect = sqlite3.connect

    def read_only_connect(database, *args, **kwargs):
        assert str(database).endswith("?mode=ro")
        assert kwargs["uri"] is True
        assert kwargs["timeout"] == 0
        conn = connect(database, *args, **kwargs)
        conn.set_trace_callback(statements.append)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden_probe_write (id INTEGER)")
        return conn

    outcome = []
    errors = []

    def initialize():
        try:
            outcome.append(importer.ensure_sqlite_state(db_path=ready_db, state_dir=ready_db.parent))
        except BaseException as exc:
            errors.append(exc)

    with closing(connect(ready_db)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(importer.sqlite3, "connect", read_only_connect)
        with hold_migration_lock_elsewhere(migration_lock_path_for(ready_db)):
            worker = threading.Thread(target=initialize, daemon=True)
            worker.start()
            worker.join(10)
            assert not worker.is_alive(), "ready startup waited for a migration lock"
        writer.rollback()
    assert errors == []
    assert len(outcome) == 1
    assert outcome[0].db_path == ready_db
    assert outcome[0].imported is False
    assert outcome[0].counts == {}
    assert outcome[0].backup_attempt_id is None
    reads = [sql for sql in statements if sql.startswith("SELECT")]
    assert len(reads) == 1
    assert "COUNT" not in reads[0].upper()
    assert not any(sql.startswith(("BEGIN", "COMMIT")) for sql in statements)
    with closing(connect(ready_db)) as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + reads[0])]
    assert sum("SEARCH state_meta USING INDEX" in step for step in plan) == 4


@pytest.mark.parametrize("change", [
    "old_revision", "future_revision", "empty_revision", "multiple_revisions", "missing_version_table",
    "missing_meta_table", "missing_json_import", "missing_background_import", "missing_data_marker",
    "malformed_json_import", "non_timestamp_import", "non_string_import", "malformed_data_marker",
    "pending_data_marker", "old_completed_marker", "unknown_marker_version", "bool_marker_version",
])
def test_uncertain_receipts_never_skip_existing_repair(ready_db, monkeypatch, change):
    with closing(sqlite3.connect(ready_db)) as conn:
        if change == "old_revision":
            conn.execute("UPDATE alembic_version SET version_num = '20261020_0071'")
        elif change == "future_revision":
            conn.execute("UPDATE alembic_version SET version_num = '20990101_future'")
        elif change == "empty_revision":
            conn.execute("DELETE FROM alembic_version")
        elif change == "multiple_revisions":
            conn.execute("INSERT INTO alembic_version VALUES ('20990101_other_head')")
        elif change == "missing_version_table":
            conn.execute("DROP TABLE alembic_version")
        elif change == "missing_meta_table":
            conn.execute("DROP TABLE state_meta")
        elif change.startswith("missing_"):
            key = {
                "missing_json_import": "json_import_completed_at",
                "missing_background_import": "background_json_import_completed_at",
                "missing_data_marker": DATA_MARKER,
            }[change]
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
        else:
            key, value = {
                "malformed_json_import": ("json_import_completed_at", "not-json"),
                "non_timestamp_import": ("json_import_completed_at", '"not-a-timestamp"'),
                "non_string_import": ("json_import_completed_at", "123"),
                "malformed_data_marker": (DATA_MARKER, "not-json"),
                "pending_data_marker": (DATA_MARKER, json.dumps({**COMPLETED, "state": "pending"})),
                "old_completed_marker": (DATA_MARKER, json.dumps({**COMPLETED, "schema_version": 2})),
                "unknown_marker_version": (DATA_MARKER, json.dumps({**COMPLETED, "schema_version": 4})),
                "bool_marker_version": (DATA_MARKER, json.dumps({**COMPLETED, "schema_version": True})),
            }[change]
            conn.execute("UPDATE state_meta SET value_json = ? WHERE key = ?", (value, key))
        conn.commit()
    assert importer.probe_current_sqlite_state(ready_db) is False

    class RepairRequired(Exception):
        pass

    def repair(**kwargs):
        assert kwargs == {"db_path": ready_db, "state_dir": ready_db.parent, "primary_platform": "avibe"}
        raise RepairRequired

    monkeypatch.setattr(importer, "repair_sqlite_state", repair)
    with pytest.raises(RepairRequired):
        importer.ensure_sqlite_state(db_path=ready_db, state_dir=ready_db.parent, primary_platform="avibe")


def test_terminal_seal_is_ready_but_fresh_unpaired_state_keeps_slow_path(ready_db, tmp_path):
    with closing(sqlite3.connect(ready_db)) as conn:
        conn.execute("UPDATE state_meta SET value_json = ? WHERE key = ?", (
            json.dumps({"state": "sealed_unattributed", "schema_version": 2, "instance_id": None}), DATA_MARKER,
        ))
        conn.commit()
    assert importer.probe_current_sqlite_state(ready_db) is True

    fresh = tmp_path / "unpaired" / "vibe.sqlite"
    importer.ensure_sqlite_state(db_path=fresh, state_dir=fresh.parent)
    with closing(sqlite3.connect(fresh)) as conn:
        assert conn.execute("SELECT value_json FROM state_meta WHERE key = ?", (DATA_MARKER,)).fetchone() is None
    assert importer.probe_current_sqlite_state(fresh) is False


@pytest.mark.parametrize("state", ["missing", "empty", "corrupt"])
def test_probe_never_creates_or_changes_unknown_files(tmp_path, state):
    db = tmp_path / "absent-directory" / "vibe.sqlite"
    if state != "missing":
        db.parent.mkdir()
        db.write_bytes(b"" if state == "empty" else b"not a sqlite database")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert importer.probe_current_sqlite_state(db) is False
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
    if state == "missing":
        assert not db.parent.exists()


def test_uncached_initialization_repairs_replacement_before_returning(ready_db, tmp_path):
    importer.ensure_sqlite_state(db_path=ready_db, state_dir=ready_db.parent)
    replacement = tmp_path / "replacement" / "vibe.sqlite"
    migrations.run_migrations(replacement, revision="20261020_0071")
    replacement.replace(ready_db)
    # Model the next CLI process, not an unsupported cached-path replacement.
    importer.reset_ensured_sqlite_state()
    assert importer.probe_current_sqlite_state(ready_db) is False
    report = importer.ensure_sqlite_state(db_path=ready_db, state_dir=ready_db.parent)
    with closing(sqlite3.connect(ready_db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("20261108_0076",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'session_digests'").fetchone()
    assert report.imported is True


def test_startup_waits_for_concurrent_upgrade_instead_of_using_old_revision(ready_db, monkeypatch):
    downgrade_legacy_schema(migrations.alembic_config(ready_db), "20261020_0071")
    before_stamp = threading.Event()
    allow_stamp = threading.Event()
    repair_entered = threading.Event()
    finished = threading.Event()
    errors = []

    def pause_before_stamp(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE alembic_version SET") and not before_stamp.is_set():
            before_stamp.set()
            assert allow_stamp.wait(20)

    event.listen(Engine, "before_cursor_execute", pause_before_stamp)
    repair = importer.repair_sqlite_state

    def observed_repair(**kwargs):
        repair_entered.set()
        return repair(**kwargs)

    monkeypatch.setattr(importer, "repair_sqlite_state", observed_repair)

    def upgrade():
        try:
            migrations.run_migrations(ready_db)
        except BaseException as exc:
            errors.append(exc)

    def initialize_and_use():
        try:
            importer.ensure_sqlite_state(db_path=ready_db, state_dir=ready_db.parent)
            with closing(sqlite3.connect(ready_db)) as conn:
                assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("20261108_0076",)]
                conn.execute("SELECT session_id FROM session_digests").fetchall()
            finished.set()
        except BaseException as exc:
            errors.append(exc)

    migrator = threading.Thread(target=upgrade, daemon=True)
    caller = threading.Thread(target=initialize_and_use, daemon=True)
    try:
        migrator.start()
        assert before_stamp.wait(10)
        assert importer.probe_current_sqlite_state(ready_db) is False
        caller.start()
        assert repair_entered.wait(10)
        assert not finished.is_set()
    finally:
        allow_stamp.set()
        migrator.join(20)
        if caller.ident is not None:
            caller.join(20)
        event.remove(Engine, "before_cursor_execute", pause_before_stamp)
    assert not migrator.is_alive() and not caller.is_alive()
    assert errors == []
    assert finished.is_set()
