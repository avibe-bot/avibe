"""S2 owns exactly the proven 3.2.6 boundary, not arbitrary backup failures.

The reserve-refusal tests deliberately disable predecessor proof. These cases
exercise completed and incomplete predecessor receipts, so they catch a blanket
refusal, skipped pending repair, unsafe omission on I/O errors, and notices
escaping a rolled-back schema transaction. Capacity
is stubbed only here; actual non-cloning/disk-full evidence belongs to Incus.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import errno
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from storage import backups, importer, migration_space, migrations
from tests.test_migration_space import protected_bytes
from tests.test_sqlite_readiness import COMPLETED, DATA_MARKER

pytestmark = pytest.mark.no_sqlite_template
NOTICE = "storage.epoch3_backup_skipped"
NOTICE_LOG = (
    "The disk is nearly full, so Avibe updated its database without keeping a backup copy. "
    "You can still go back to 3.2.6; 3.2.5 and older can no longer open this database. "
    "Freeing some disk space is recommended."
)


@pytest.fixture
def predecessor(tmp_path):
    path = tmp_path / "state" / "vibe.sqlite"
    migrations.run_migrations(path, revision="20261112_0088")
    with closing(sqlite3.connect(path)) as conn, conn:
        for key, value in (
            (importer.JSON_IMPORT_MARKER, "2026-11-12T12:00:00+00:00"),
            (importer.BACKGROUND_IMPORT_MARKER, "2026-11-12T12:00:00+00:00"),
            (DATA_MARKER, COMPLETED),
        ):
            conn.execute("INSERT INTO state_meta(key,value_json,updated_at) VALUES (?,?,'old')", (key, json.dumps(value)))
        conn.execute(
            "INSERT INTO agent_events(id,platform,event_type,visibility,content_json,metadata_json,created_at,updated_at) "
            "VALUES ('history','test','skill.catalog_result','trace',?, '{}','old','old')",
            ('{"entries":[{"name":"old inline content"}]}',),
        )
    return path


def capacity(monkeypatch, value):
    monkeypatch.setattr(migration_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=value))


def unsupported(source, destination):
    raise OSError(errno.ENOTSUP, "fixture filesystem does not clone")


def marker(path):
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT value_json FROM state_meta WHERE key=?", (NOTICE,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize("kind", ["clone", "copy", "omitted"])
def test_exact_boundary_clones_before_reserve_or_copies_or_omits(predecessor, monkeypatch, caplog, kind):
    path = predecessor
    with closing(sqlite3.connect(path)) as conn:
        history = conn.execute("SELECT * FROM agent_events").fetchall()
    capacity(monkeypatch, 10**12 if kind == "copy" else 1)
    monkeypatch.setattr(backups, "_clone_file", shutil.copyfile if kind == "clone" else unsupported)
    decision = migrations.preflight_for_write(path)
    assert decision.clone_first_backup and decision.imports_complete and not decision.skip_snapshot
    result = migrations.run_migrations(path)
    assert not result.skip_snapshot and result.backup_omitted is (kind == "omitted")
    copies = list((path.parent / "backups").glob("*/manifest.json"))
    assert len(copies) == (0 if kind == "omitted" else 1)
    if copies:
        manifest = json.loads(copies[0].read_text())
        assert manifest["copy_method"] == ("filesystem_clone" if kind == "clone" else "sqlite_online_backup")
        with closing(sqlite3.connect(copies[0].parent / "vibe.sqlite")) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("20261112_0088",)
            assert conn.execute("SELECT * FROM agent_events").fetchall() == history
    notice = marker(path)
    if kind == "omitted":
        assert set(notice) == {"converted_at", "database_bytes"}
        assert notice["database_bytes"] > 0
        assert datetime.fromisoformat(notice["converted_at"].replace("Z", "+00:00")).tzinfo is not None
    else:
        assert notice is None
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("20261114_0089",)
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("3",)
        assert conn.execute("SELECT * FROM agent_events").fetchall() == history
    # A read-only WAL connection may create an empty WAL/SHM pair. Establish
    # that bookkeeping before checking that a repeated startup changes no data.
    migrations.preflight_database(path, read_only=True)
    before = protected_bytes(path)
    migrations.run_migrations(path)
    assert protected_bytes(path) == before  # restart does not recreate a receipt or copy
    assert marker(path) == notice
    assert sum(record.getMessage() == NOTICE_LOG for record in caplog.records) == (1 if kind == "omitted" else 0)


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES, errno.ENOSPC])
def test_real_clone_errors_never_authorize_omission(predecessor, monkeypatch, error):
    capacity(monkeypatch, 1)
    def fail(source, destination):
        destination.write_bytes(b"partial")
        raise OSError(error, "fixture clone failed")
    monkeypatch.setattr(backups, "_clone_file", fail)
    before = migrations._epoch3_transaction_state(predecessor)
    with pytest.raises(OSError) as caught:
        migrations.run_migrations(predecessor)
    assert caught.value.errno == error
    assert migrations._epoch3_transaction_state(predecessor) == before
    assert marker(predecessor) is None
    assert not list((predecessor.parent / "backups").glob("avibe-sqlite-*"))


@pytest.mark.parametrize("invalid", ["proof", "old-target", "future-batch"])
def test_unproved_paths_keep_reserve_before_any_clone(predecessor, monkeypatch, tmp_path, invalid):
    if invalid == "proof":
        monkeypatch.setattr(migrations, "VERIFIED_PREDECESSOR_EPOCH", None)
    elif invalid == "old-target":
        # An actual older database must not gain the exception for its batch.
        predecessor = tmp_path / "older.sqlite"
        migrations.run_migrations(predecessor, revision="20261112_0087")
    else:
        graph = tmp_path / "graph"
        shutil.copytree(migrations.alembic_dir(), graph, ignore=shutil.ignore_patterns("__pycache__"))
        (graph / "versions" / "20990101_9001_later.py").write_text(
            "revision='20990101_9001'\ndown_revision='20261114_0089'\n"
            "branch_labels=None\ndepends_on=None\nMIGRATION_COMPATIBILITY='additive'\n"
            "def upgrade(): pass\ndef downgrade(): pass\n"
        )
        monkeypatch.setattr(migrations, "alembic_dir", lambda: graph)
    capacity(monkeypatch, 1)
    monkeypatch.setattr(backups, "_clone_file", lambda *args: pytest.fail("unproven boundary tried clone"))
    migrations.preflight_database(predecessor, read_only=True)
    before = protected_bytes(predecessor)
    with pytest.raises(migration_space.InsufficientMigrationSpaceError):
        migrations.run_migrations(predecessor)
    assert protected_bytes(predecessor) == before


@pytest.mark.parametrize("receipt", ["missing", "malformed", "pending", "missing-json", "missing-background", "fresh-unpaired"])
@pytest.mark.parametrize("cloned", [True, False])
def test_incomplete_imports_still_repair_after_clone_or_omission(predecessor, tmp_path, monkeypatch, receipt, cloned):
    if receipt == "fresh-unpaired":
        # Exercise real predecessor importer semantics, not a fabricated
        # completed marker. Hold its graph at 0088 only for fixture creation.
        from alembic.script import ScriptDirectory
        graph = tmp_path / "predecessor-graph"
        shutil.copytree(migrations.alembic_dir(), graph, ignore=shutil.ignore_patterns("__pycache__"))
        (graph / "versions" / "20261114_0089_coordinated_writers.py").unlink()
        with monkeypatch.context() as old:
            old.setattr(migrations, "alembic_dir", lambda: graph)
            assert ScriptDirectory.from_config(migrations.alembic_config()).get_current_head() == "20261112_0088"
            predecessor = tmp_path / "fresh" / "vibe.sqlite"
            importer.repair_sqlite_state(db_path=predecessor, state_dir=predecessor.parent)
    else:
        with closing(sqlite3.connect(predecessor)) as conn, conn:
            if receipt in {"missing", "missing-json", "missing-background"}:
                key = {"missing": DATA_MARKER, "missing-json": importer.JSON_IMPORT_MARKER,
                       "missing-background": importer.BACKGROUND_IMPORT_MARKER}[receipt]
                conn.execute("DELETE FROM state_meta WHERE key=?", (key,))
            else:
                value = "invalid JSON" if receipt == "malformed" else json.dumps({"schema_version": 3, "state": "pending"})
                conn.execute("UPDATE state_meta SET value_json=? WHERE key=?", (value, DATA_MARKER))
    decision = migrations.preflight_for_write(predecessor)
    assert decision.clone_first_backup and not decision.imports_complete
    capacity(monkeypatch, 1)
    monkeypatch.setattr(backups, "_clone_file", shutil.copyfile if cloned else unsupported)
    result = migrations.run_migrations(predecessor)
    assert not result.skip_snapshot
    assert result.backup_omitted is (not cloned)
    if cloned:
        assert len(list((predecessor.parent / "backups").glob("*/manifest.json"))) >= 1
        lifecycle = json.loads(predecessor.with_name("." + predecessor.name + ".backup-lifecycle.json").read_text())
        assert lifecycle["phase"] == "migrated"
        assert marker(predecessor) is None
    else:
        assert marker(predecessor) is not None
        monkeypatch.setattr(importer, "begin_sqlite_backup_validation", lambda *args: pytest.fail("adopted prior backup"))
        monkeypatch.setattr(importer, "complete_sqlite_backup_validation", lambda *args: pytest.fail("validated prior backup"))
        monkeypatch.setattr(importer, "prune_state_backups", lambda *args: pytest.fail("pruned prior backup"))
    repairs = []
    original = importer._run_sqlite_data_migrations
    def repair(conn):
        repairs.append(True)
        return original(conn)
    monkeypatch.setattr(importer, "_run_sqlite_data_migrations", repair)
    # Model death after conversion but before importer entry: only persisted
    # provenance, not the first call's in-memory decision, crosses this boundary.
    importer.reset_ensured_sqlite_state()
    importer.repair_sqlite_state(db_path=predecessor, state_dir=predecessor.parent)
    assert repairs == [True]
    assert (marker(predecessor) is not None) is (not cloned)


@pytest.mark.parametrize("failure", ["full", "io"])
def test_partial_conversion_rolls_back_schema_epoch_and_notice(predecessor, monkeypatch, failure):
    capacity(monkeypatch, 1)
    monkeypatch.setattr(backups, "_clone_file", unsupported)
    before = migrations._epoch3_transaction_state(predecessor)
    with closing(sqlite3.connect(predecessor)) as conn:
        history = conn.execute("SELECT * FROM agent_events").fetchall()
    reached = []
    def fail_after_marker(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO state_meta" in statement and "storage.epoch3_backup_skipped" in statement:
            assert conn.connection.driver_connection.in_transaction
            reached.append(True)
            error = sqlite3.OperationalError("database or disk is full" if failure == "full" else "disk I/O error")
            error.sqlite_errorcode = 13 if failure == "full" else 10
            raise error
    event.listen(Engine, "after_cursor_execute", fail_after_marker)
    try:
        expected = migrations.DatabaseDiskFullError if failure == "full" else SQLAlchemyOperationalError
        with pytest.raises(expected) as caught:
            migrations.run_migrations(predecessor)
        if failure == "full":
            assert caught.value.rollback_confirmed is True
    finally:
        event.remove(Engine, "after_cursor_execute", fail_after_marker)
    assert reached == [True]
    assert migrations._epoch3_transaction_state(predecessor) == before
    assert marker(predecessor) is None
    with closing(sqlite3.connect(predecessor)) as conn:
        assert conn.execute("SELECT * FROM agent_events").fetchall() == history
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
    # Removing the failure is enough; no restamp/restore or manual repair.
    migrations.run_migrations(predecessor)
    assert marker(predecessor) is not None


@pytest.mark.parametrize("phase", ["migrating", "migrated", "validating", "validated"])
def test_omission_restart_repairs_without_adopting_same_target_old_attempt(predecessor, monkeypatch, phase):
    from tests.test_sqlite_backup_expiry import _attempt

    with closing(sqlite3.connect(predecessor)) as conn, conn:
        conn.execute("DELETE FROM state_meta WHERE key=?", (DATA_MARKER,))
    _attempt(predecessor, phase=phase, revision="20261114_0089")
    # Model a stale same-target attempt retained beside a restored predecessor.
    # A migrating record is rejected already; the other phases are adoptable
    # without the durable no-copy provenance added by this change.
    with closing(sqlite3.connect(predecessor)) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num='20261112_0088'")
    before = {k: v for k, v in protected_bytes(predecessor).items()
              if "/backups/" in k or k.endswith("backup-lifecycle.json")}
    capacity(monkeypatch, 1)
    monkeypatch.setattr(backups, "_clone_file", unsupported)
    assert migrations.run_migrations(predecessor).backup_omitted
    with closing(sqlite3.connect(predecessor)) as conn, conn:
        conn.execute("UPDATE state_meta SET value_json=json_set(value_json,'$.notified_at','2026-11-16T12:00:00Z') WHERE key=?", (NOTICE,))
    decision = migrations.preflight_for_write(predecessor)
    assert decision.backup_omitted and not decision.imports_complete
    importer.reset_ensured_sqlite_state()
    report = importer.repair_sqlite_state(db_path=predecessor, state_dir=predecessor.parent)
    assert report.backup_attempt_id is None
    after = {k: v for k, v in protected_bytes(predecessor).items()
             if "/backups/" in k or k.endswith("backup-lifecycle.json")}
    assert after == before
    assert marker(predecessor)["notified_at"] == "2026-11-16T12:00:00Z"


@pytest.mark.parametrize("value", ["not json", "{}", '{"database_bytes":true,"converted_at":"2026-11-16T12:00:00Z"}'])
def test_malformed_omission_provenance_is_not_trusted(predecessor, monkeypatch, value):
    capacity(monkeypatch, 1)
    monkeypatch.setattr(backups, "_clone_file", unsupported)
    migrations.run_migrations(predecessor)
    with closing(sqlite3.connect(predecessor)) as conn, conn:
        conn.execute("UPDATE state_meta SET value_json=? WHERE key=?", (value, NOTICE))
    with pytest.raises(migrations.IncompatibleDatabaseRevisionError, match="invalid epoch-3 backup omission receipt"):
        migrations.preflight_database(predecessor, read_only=True)
    assert importer.probe_current_sqlite_state(predecessor) is False
    importer.reset_ensured_sqlite_state()
    with pytest.raises(migrations.IncompatibleDatabaseRevisionError, match="invalid epoch-3 backup omission receipt"):
        importer.ensure_sqlite_state(db_path=predecessor, state_dir=predecessor.parent)


def test_omission_never_adopts_previous_backup_lifecycle(predecessor, monkeypatch):
    capacity(monkeypatch, 1)
    monkeypatch.setattr(backups, "_clone_file", unsupported)
    receipt = predecessor.with_name("." + predecessor.name + ".backup-lifecycle.json")
    receipt.write_text('{"unrelated_previous_attempt":"retain exactly"}')
    original = receipt.read_bytes()
    monkeypatch.setattr(importer, "begin_sqlite_backup_validation", lambda *args: pytest.fail("adopted old attempt"))
    monkeypatch.setattr(importer, "_run_sqlite_data_migrations", lambda *args: pytest.fail("replayed importer"))
    result = importer.repair_sqlite_state(db_path=predecessor, state_dir=predecessor.parent)
    assert result.backup_attempt_id is None
    assert receipt.read_bytes() == original
    assert marker(predecessor) is not None
