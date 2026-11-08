"""The bridge is conservative; supported future state opens without old startup writes.

Existing preflight tests only refuse unknown stamps. These cases cover the one
intentional relaxation, all shared openers, and real SQLite crash atomicity.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from alembic import command
import pytest

from storage import importer, migrations
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines
from storage.settings_service import SQLiteSettingsService
from tests.test_sqlite_migration_preflight import _snapshot

pytestmark = pytest.mark.no_sqlite_template


@pytest.fixture
def bridge_db(tmp_path):
    path = tmp_path / "state" / "vibe.sqlite"
    importer.ensure_sqlite_state(db_path=path, state_dir=path.parent)
    importer.reset_ensured_sqlite_state()
    return path


def _future(path, *, epoch="1", stamp="20990101_9999"):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", (stamp,))
        conn.execute("UPDATE state_meta SET value_json = ? WHERE key = 'compatibility_epoch'", (epoch,))
    # Model an older executable's new process, not an unsupported live breaking
    # upgrade under a process whose open decision is already cached.
    dispose_cached_sqlite_engines()


def _open(kind, path):
    if kind == "migration":
        return migrations.run_migrations(path)
    if kind == "bootstrap":
        return migrations.initialize_background_tables(path)
    if kind == "readiness":
        return migrations.background_tables_ready(path)
    if kind == "indexes":
        return migrations.ensure_background_indexes(path)
    if kind in {"ensure", "repair"}:
        return getattr(importer, kind + "_sqlite_state")(db_path=path, state_dir=path.parent)
    if kind == "background":
        store = SQLiteBackgroundTaskStore(path)
        store.close()
    elif kind == "settings":
        store = SQLiteSettingsService(path)
        store.engine.dispose()
    else:
        engine = create_sqlite_engine(path, read_only=kind == "read-only")
        engine.dispose()


OPENERS = ["migration", "bootstrap", "readiness", "indexes", "ensure", "repair", "background", "settings", "read-only"]


@pytest.mark.parametrize("kind", OPENERS)
def test_compatible_future_open_has_no_startup_mutation_or_receipt_adoption(bridge_db, kind):
    _future(bridge_db)
    # If old import/repair runs, it will consume these deliberately absent
    # receipts. Compatibility is the release contract, not old import markers.
    with closing(sqlite3.connect(bridge_db)) as conn, conn:
        conn.execute("DELETE FROM state_meta WHERE key != 'compatibility_epoch'")
        conn.execute("ALTER TABLE agents ADD COLUMN future_optional TEXT")
        conn.execute("ALTER TABLE agents ADD COLUMN future_defaulted INTEGER NOT NULL DEFAULT 7")
    before = _snapshot(bridge_db)
    _open(kind, bridge_db)
    assert _snapshot(bridge_db) == before
    assert importer.validated_sqlite_backup_attempt(bridge_db) is None
    # A compatible open is not a read-only store: ordinary old named-column
    # writes continue to work and new-runtime data survives the reopen.
    engine = create_sqlite_engine(bridge_db)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO state_meta(key, value_json, updated_at) VALUES ('old-writer', '{}', '2026-11-08')"
        )
    engine.dispose()
    assert migrations.preflight_database(bridge_db).compatible_open


@pytest.mark.parametrize("kind", OPENERS)
@pytest.mark.parametrize("epoch", ["2", "true", '"1"', "null", "1.0", "-1", "not-json"])
def test_incompatible_epoch_refuses_every_opener_before_mutation(bridge_db, kind, epoch):
    _future(bridge_db, epoch=epoch)
    before = _snapshot(bridge_db)
    if kind == "readiness":
        assert _open(kind, bridge_db) is False
    else:
        with pytest.raises(migrations.IncompatibleDatabaseRevisionError, match="compatibility_epoch"):
            _open(kind, bridge_db)
    assert _snapshot(bridge_db) == before


@pytest.mark.parametrize("damage", ["no-marker", "column", "new-required-column", "index", "symbol", "partial", "mixed", "ancestor"])
def test_epoch_does_not_excuse_inconsistent_schema_or_non_exact_stamps(bridge_db, damage):
    _future(bridge_db)
    with closing(sqlite3.connect(bridge_db)) as conn, conn:
        if damage == "no-marker":
            conn.execute("DELETE FROM state_meta WHERE key = 'compatibility_epoch'")
        elif damage == "column":
            conn.execute("ALTER TABLE agents RENAME COLUMN description TO lost_description")
        elif damage == "new-required-column":
            conn.execute("ALTER TABLE auth_codes ADD COLUMN future_required TEXT NOT NULL")
        elif damage == "index":
            conn.execute("DROP INDEX ix_messages_turn_id")
        elif damage in {"symbol", "partial"}:
            conn.execute("UPDATE alembic_version SET version_num = ?", ("head" if damage == "symbol" else "20990101_999",))
        elif damage == "mixed":
            conn.execute("INSERT INTO alembic_version VALUES ('20261108_0076')")
    before = _snapshot(bridge_db)
    with pytest.raises(migrations.IncompatibleDatabaseRevisionError):
        migrations.run_migrations(bridge_db, revision="20261107_0075" if damage == "ancestor" else "head")
    assert _snapshot(bridge_db) == before


def test_bridge_upgrade_keeps_verified_snapshot_and_same_head_does_not_make_another(tmp_path):
    path = tmp_path / "vibe.sqlite"
    migrations.run_migrations(path, revision="20261107_0075")
    before = _snapshot(path)["database.sql"]
    migrations.run_migrations(path)
    backups = list((tmp_path / "backups").glob("*/vibe.sqlite"))
    assert len(backups) == 1
    assert _snapshot(backups[0])["database.sql"] == before
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("1",)
    migrations.run_migrations(path)
    assert list((tmp_path / "backups").glob("*/vibe.sqlite")) == backups
    with pytest.raises(RuntimeError, match="restoring discards writes"):
        command.downgrade(migrations.alembic_config(path), "20261107_0075")


@pytest.mark.parametrize("crash", ["before-commit", "after-commit"])
def test_bridge_schema_stamp_and_epoch_survive_process_crash_as_one_commit(tmp_path, crash):
    path = tmp_path / "vibe.sqlite"
    migrations.run_migrations(path, revision="20261107_0075")
    graph = tmp_path / "alembic"
    shutil.copytree(migrations.alembic_dir(), graph, ignore=shutil.ignore_patterns("__pycache__"))
    bridge = graph / "versions" / "20261108_0076_compatibility_epoch.py"
    source = bridge.read_text()
    # Add real DDL to the bridge in this isolated graph to catch Python's
    # non-transactional legacy DDL behavior, not just rollback of an INSERT.
    bridge.write_text(source.replace(
        "    advance_compatibility_epoch(op.get_bind(), 1)",
        '    op.execute("CREATE TABLE crash_probe(value TEXT)")\n    advance_compatibility_epoch(op.get_bind(), 1)',
    ))
    result = subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from sqlalchemy import Engine, event
from storage import migrations
migrations.alembic_dir = lambda: Path(sys.argv[2])
def crash_before_commit(conn, cursor, statement, parameters, context, executemany):
    if sys.argv[3] == 'before-commit' and statement.startswith('UPDATE alembic_version SET') and '20261108_0076' in statement:
        os._exit(71)
event.listen(Engine, 'after_cursor_execute', crash_before_commit)
migrations.run_migrations(Path(sys.argv[1]))
os._exit(72)
""", str(path), str(graph), crash], check=False, capture_output=True, text=True)
    assert result.returncode == (71 if crash == "before-commit" else 72), result.stderr
    with closing(sqlite3.connect(path)) as conn:
        epoch = conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone()
        stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        table = conn.execute("SELECT name FROM sqlite_master WHERE name='crash_probe'").fetchone()
    if crash == "before-commit":
        assert (epoch, stamp, table) == (None, ("20261107_0075",), None)
    else:
        assert (epoch, stamp, table) == (("1",), ("20261108_0076",), ("crash_probe",))


def _next_graph(tmp_path, monkeypatch, body, *, epoch=None):
    graph = tmp_path / "next-alembic"
    shutil.copytree(migrations.alembic_dir(), graph, ignore=shutil.ignore_patterns("__pycache__"))
    (graph / "versions" / "20261109_0077_next.py").write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision = '20261109_0077'\ndown_revision = '20261108_0076'\n"
        "branch_labels = None\ndepends_on = None\nMIGRATION_COMPATIBILITY = 'breaking'\n"
        + (f"COMPATIBILITY_EPOCH = {epoch}\n" if epoch is not None else "")
        + "def upgrade():\n" + body + "\ndef downgrade():\n    pass\n"
    )
    monkeypatch.setattr(migrations, "alembic_dir", lambda: graph)


def test_known_forward_epoch_upgrade_does_not_require_new_columns_before_migration(bridge_db, tmp_path, monkeypatch):
    from sqlalchemy import Column, String, Table
    from storage.models import metadata

    # New models require a table which a real bridge database cannot yet have.
    # The old preflight compared against these models *before* migrating it.
    old_cfg = migrations.alembic_config(tmp_path / "released.sqlite")
    table = Table("epoch_extension", metadata, Column("value", String))
    try:
        _next_graph(tmp_path, monkeypatch, '    op.create_table("epoch_extension", sa.Column("value", sa.String()))\n')
        # The existing release guard must still be able to construct the old
        # release's graph with today's env.py and models before upgrading it.
        command.upgrade(old_cfg, "head")
        migrations.run_migrations(bridge_db)
        assert migrations.preflight_database(bridge_db).compatibility_epoch == 1
        with closing(sqlite3.connect(bridge_db)) as conn:
            assert conn.execute("SELECT value FROM epoch_extension").fetchall() == []
    finally:
        metadata.remove(table)


@pytest.mark.parametrize("escape", [
    '    op.execute("COMMIT")\n',
    '    with op.get_context().autocommit_block():\n        op.execute("SELECT 1")\n',
    '    op.get_bind().connection.driver_connection.executescript("CREATE TABLE escaped(id INTEGER);")\n',
    '    op.execute("PRAGMA foreign_keys=OFF")\n',
    '    op.drop_table("epoch_parent")\n',
])
def test_new_migrations_cannot_escape_the_schema_stamp_epoch_transaction(bridge_db, tmp_path, monkeypatch, escape):
    with closing(sqlite3.connect(bridge_db)) as conn, conn:
        conn.executescript(
            "CREATE TABLE epoch_parent(id INTEGER PRIMARY KEY);"
            "CREATE TABLE epoch_child(parent_id INTEGER REFERENCES epoch_parent(id) ON DELETE CASCADE);"
            "INSERT INTO epoch_parent VALUES (1); INSERT INTO epoch_child VALUES (1);"
        )
    _next_graph(tmp_path, monkeypatch, '    op.execute("CREATE TABLE atomic_probe(id INTEGER)")\n' + escape)
    before = _snapshot(bridge_db)["database.sql"]
    with pytest.raises(Exception, match="not authorized"):
        migrations.run_migrations(bridge_db)
    assert _snapshot(bridge_db)["database.sql"] == before


@pytest.mark.parametrize("declared, written", [(2, 1), (None, 2), (2, 2)])
def test_epoch_mismatch_or_unsupported_target_rolls_back_before_commit(bridge_db, tmp_path, monkeypatch, declared, written):
    _next_graph(
        tmp_path, monkeypatch,
        f'    op.execute("UPDATE state_meta SET value_json = \'{written}\' WHERE key = \'compatibility_epoch\'")\n',
        epoch=declared,
    )
    before = _snapshot(bridge_db)["database.sql"]
    with pytest.raises((RuntimeError, ValueError), match="compatibility_epoch"):
        migrations.run_migrations(bridge_db)
    assert _snapshot(bridge_db)["database.sql"] == before


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("mutation", ["annotation", "default", "namespace", "inside-upgrade"])
def test_mutable_python_epoch_cannot_weaken_any_revision_commit(bridge_db, tmp_path, monkeypatch, mutation, intermediate):
    with closing(sqlite3.connect(bridge_db)) as conn, conn:
        conn.execute("CREATE TABLE epoch_probe(value TEXT)")
        conn.execute("INSERT INTO epoch_probe VALUES ('before')")
    body = (
        '    op.create_index("uq_epoch_probe", "epoch_probe", ["value"], unique=True)\n'
        '    from storage.compatibility import advance_compatibility_epoch\n'
        '    advance_compatibility_epoch(op.get_bind(), COMPATIBILITY_EPOCH)\n'
    )
    if mutation == "inside-upgrade":
        body = '    globals()["COMPATIBILITY_EPOCH"] = 1\n' + body
    _next_graph(tmp_path, monkeypatch, body, epoch=2)
    version = migrations.alembic_dir() / "versions" / "20261109_0077_next.py"
    source = version.read_text()
    if mutation == "annotation":
        source = source.replace("COMPATIBILITY_EPOCH = 2", 'COMPATIBILITY_EPOCH: globals().__setitem__("COMPATIBILITY_EPOCH", 1) = 2')
        # Python 3.14 defers annotation evaluation; force the same mutation in
        # that interpreter while retaining the eager 3.10-3.13 reproduction.
        source += '\nglobals().get("__annotate__", lambda _format: None)(1)\n'
    elif mutation == "default":
        source += '\ndef helper(unused=globals().__setitem__("COMPATIBILITY_EPOCH", 1)):\n    pass\n'
    elif mutation == "namespace":
        source += '\nglobals().update(COMPATIBILITY_EPOCH=1)\n'
    version.write_text(source)
    if intermediate:
        (version.parent / "20261110_0078_after.py").write_text(
            "from alembic import op\nrevision = '20261110_0078'\ndown_revision = '20261109_0077'\n"
            "MIGRATION_COMPATIBILITY = 'additive'\ndef upgrade():\n"
            '    op.create_index("ix_after_probe", "epoch_probe", ["value"])\n'
        )
    before = _snapshot(bridge_db)["database.sql"]
    with pytest.raises(RuntimeError, match="compatibility_epoch does not match"):
        migrations.run_migrations(bridge_db)
    assert _snapshot(bridge_db)["database.sql"] == before
    # The old write contract still works; even an intermediate restrictive
    # migration did not commit under the old epoch before a later head failed.
    with closing(sqlite3.connect(bridge_db)) as conn, conn:
        conn.execute("INSERT INTO epoch_probe VALUES ('before')")


def test_sqlite_busy_is_not_misdiagnosed_as_an_incompatible_epoch(bridge_db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    dispose_cached_sqlite_engines()
    connect = sqlite3.connect
    with closing(connect(bridge_db)) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(migrations.sqlite3, "connect", lambda *args, **kw: connect(*args, **{**kw, "timeout": 0.01}))
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            migrations.preflight_database(bridge_db)
        with pytest.raises(OperationalError, match="locked"):
            create_sqlite_engine(bridge_db)
        writer.rollback()


@pytest.mark.parametrize("epoch", ["2", "true", "null", '"1"'])
def test_same_head_fast_probe_cannot_bypass_epoch_refusal(bridge_db, epoch):
    _future(bridge_db, stamp="20261108_0076", epoch=epoch)
    assert not importer.probe_current_sqlite_state(bridge_db)
    before = _snapshot(bridge_db)
    with pytest.raises(migrations.IncompatibleDatabaseRevisionError, match="compatibility_epoch"):
        importer.ensure_sqlite_state(db_path=bridge_db, state_dir=bridge_db.parent)
    assert _snapshot(bridge_db) == before
