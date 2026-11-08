"""No-copy must execute the upgrade without adopting or manufacturing backup evidence.

The bridge tests cover refusal and atomic epochs, but never suppress a real
upgrade's snapshot. These tests exercise that new decision at its shared owner,
then follow the existing receipt through restart and its original expiry time.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from storage import backups, importer, migrations
from storage.compatibility import BRIDGE_REVISION, PRE_BRIDGE_REVISION
from storage.db import dispose_cached_sqlite_engines
from tests.test_sqlite_backup_expiry import EXPIRES, _attempt
from tests.test_sqlite_migration_preflight import _snapshot

pytestmark = pytest.mark.no_sqlite_template
FIRST = "20990101_9001"
SECOND = "20990101_9002"


@pytest.fixture
def ready_db(tmp_path, monkeypatch):
    path = tmp_path / "state" / "vibe.sqlite"
    importer.ensure_sqlite_state(db_path=path, state_dir=path.parent)
    # Model a prior completed import. A fresh, unpaired install deliberately
    # leaves its deferred-context migration open for its first real pairing.
    from tests.test_sqlite_readiness import COMPLETED, DATA_MARKER

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO state_meta(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (DATA_MARKER, json.dumps(COMPLETED), COMPLETED["updated_at"]),
        )
        assert importer.completed_sqlite_imports(conn)
    importer.reset_ensured_sqlite_state()
    monkeypatch.setattr(migrations, "VERIFIED_PREDECESSOR_EPOCH", 1)
    return path


def _graph(tmp_path, monkeypatch, *, second=False, opaque=False):
    graph = tmp_path / "alembic"
    shutil.copytree(migrations.alembic_dir(), graph, ignore=shutil.ignore_patterns("__pycache__"))
    previous = BRIDGE_REVISION
    for number, revision in enumerate((FIRST, SECOND) if second else (FIRST,), 1):
        body = f"op.create_index('ix_no_copy_{number}', 'agents', ['description'], unique=False)"
        if opaque and (not second or number == 2):
            body = f"op.execute(\"CREATE INDEX ix_no_copy_{number} ON agents(description)\")"
        (graph / "versions" / f"{revision}_no_copy.py").write_text(
            "from alembic import op\nimport sqlalchemy as sa\n"
            f"revision = {revision!r}\ndown_revision = {previous!r}\n"
            "branch_labels = None\ndepends_on = None\n"
            "MIGRATION_COMPATIBILITY = 'additive'\n"
            f"def upgrade():\n    {body}\ndef downgrade():\n    pass\n",
            encoding="utf-8",
        )
        previous = revision
    monkeypatch.setattr(migrations, "alembic_dir", lambda: graph)
    dispose_cached_sqlite_engines()
    return graph


def _forbid(*args, **kwargs):
    pytest.fail("no-copy reached snapshot / old repair / backup validation")


def _rollback_artifacts(path):
    return {key: value for key, value in _snapshot(path).items() if key != "database.sql"}


def test_no_copy_executes_pending_ddl_without_backup_validation_or_import(ready_db, tmp_path, monkeypatch):
    _graph(tmp_path, monkeypatch, second=True)
    before = _rollback_artifacts(ready_db)
    for name in ("create_sqlite_migration_backup", "begin_sqlite_backup_migration", "complete_sqlite_backup_migration"):
        monkeypatch.setattr(migrations, name, _forbid)
    for name in ("begin_sqlite_backup_validation", "complete_sqlite_backup_validation", "_run_sqlite_data_migrations"):
        monkeypatch.setattr(importer, name, _forbid)
    monkeypatch.setattr(backups, "_validate_migrated_database", _forbid)
    decision = migrations.preflight_database(ready_db)
    assert decision.skip_snapshot
    assert decision.backup_revisions == ({BRIDGE_REVISION}, {SECOND})
    report = importer.repair_sqlite_state(db_path=ready_db, state_dir=ready_db.parent)
    assert report.backup_attempt_id is None
    with closing(sqlite3.connect(ready_db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (SECOND,)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'ix_no_copy_%' ORDER BY name").fetchall() == [
            ("ix_no_copy_1",), ("ix_no_copy_2",),
        ]
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("1",)
    assert _rollback_artifacts(ready_db) == before
    assert migrations.run_migrations(ready_db).backup_revisions is None


@pytest.mark.parametrize("reason", ["unverified", "other-epoch", "opaque", "mixed", "json-receipt", "data-receipt"])
def test_unproved_upgrade_keeps_a_fresh_snapshot(ready_db, tmp_path, monkeypatch, reason):
    _graph(tmp_path, monkeypatch, second=reason == "mixed", opaque=reason in {"opaque", "mixed"})
    if reason in {"unverified", "other-epoch"}:
        monkeypatch.setattr(migrations, "VERIFIED_PREDECESSOR_EPOCH", None if reason == "unverified" else 2)
    if reason in {"json-receipt", "data-receipt"}:
        from storage.resource_access_service import LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
        key = importer.JSON_IMPORT_MARKER if reason == "json-receipt" else LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
        with closing(sqlite3.connect(ready_db)) as conn, conn:
            conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
    before = _snapshot(ready_db)["database.sql"]
    assert not migrations.run_migrations(ready_db).skip_snapshot
    copies = list((ready_db.parent / "backups").glob("*/vibe.sqlite"))
    assert len(copies) == 1
    assert _snapshot(copies[0])["database.sql"] == before


def test_marker_created_in_this_upgrade_never_qualifies(tmp_path, monkeypatch):
    path = tmp_path / "vibe.sqlite"
    migrations.run_migrations(path, revision=PRE_BRIDGE_REVISION)
    monkeypatch.setattr(migrations, "VERIFIED_PREDECESSOR_EPOCH", 1)
    _graph(tmp_path, monkeypatch)
    decision = migrations.run_migrations(path)
    assert decision.compatibility_epoch is None
    assert not decision.skip_snapshot
    assert len(list((tmp_path / "backups").glob("*/vibe.sqlite"))) == 1


@pytest.mark.parametrize("phase", ["migrated", "validating", "validated", "ready"])
def test_no_copy_and_restart_never_adopt_an_older_backup_attempt(ready_db, tmp_path, monkeypatch, phase):
    _attempt(ready_db, phase=phase, revision=BRIDGE_REVISION)
    _graph(tmp_path, monkeypatch)
    before = _rollback_artifacts(ready_db)
    result = importer.repair_sqlite_state(db_path=ready_db, state_dir=ready_db.parent, include_counts=False)
    assert result.backup_attempt_id is None
    assert _rollback_artifacts(ready_db) == before
    importer.reset_ensured_sqlite_state()
    # The first skip decision is process-local. A restart must still reject the
    # stale exact target before changing its phase to validating or scanning it.
    monkeypatch.setattr(backups, "_validate_migrated_database", _forbid)
    result = importer.repair_sqlite_state(db_path=ready_db, state_dir=ready_db.parent, include_counts=False)
    assert result.backup_attempt_id is None
    assert _rollback_artifacts(ready_db) == before


@pytest.mark.parametrize("phase", ["ready", "migrating", "migrated", "validating", "validated"])
def test_only_previously_ready_copy_expires_across_additive_successor(ready_db, tmp_path, monkeypatch, phase):
    copy = _attempt(ready_db, phase=phase, revision=BRIDGE_REVISION)
    original = json.loads((copy / "manifest.json").read_text())
    _graph(tmp_path, monkeypatch)
    assert migrations.run_migrations(ready_db).skip_snapshot
    assert json.loads((copy / "manifest.json").read_text()) == original
    result = backups.expire_sqlite_migration_backups(ready_db, now=EXPIRES)
    assert result["removed"] == ([copy.name] if phase == "ready" else [])
    assert copy.exists() is (phase != "ready")


@pytest.mark.parametrize("damage", ["unknown", "opaque", "epoch", "missing-marker"])
def test_expiry_does_not_infer_additivity_from_a_supported_epoch(ready_db, tmp_path, monkeypatch, damage):
    copy = _attempt(ready_db, revision=BRIDGE_REVISION)
    _graph(tmp_path, monkeypatch, opaque=damage == "opaque")
    # Emulate a successor not owned by this no-copy decision: no new receipt.
    with closing(sqlite3.connect(ready_db)) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", ("20990101_9999" if damage == "unknown" else FIRST,))
        if damage == "epoch":
            conn.execute("UPDATE state_meta SET value_json='2' WHERE key='compatibility_epoch'")
        elif damage == "missing-marker":
            conn.execute("DELETE FROM state_meta WHERE key='compatibility_epoch'")
    before = _rollback_artifacts(ready_db)
    assert backups.expire_sqlite_migration_backups(ready_db, now=EXPIRES)["status"] == "unconfirmed"
    assert copy.exists() and _rollback_artifacts(ready_db) == before


def test_crash_after_first_additive_commit_resumes_remaining_batch_without_copy(ready_db, tmp_path, monkeypatch):
    graph = _graph(tmp_path, monkeypatch, second=True)
    before = _rollback_artifacts(ready_db)
    result = subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from sqlalchemy import Engine, event
from storage import migrations
migrations.alembic_dir = lambda: Path(sys.argv[2])
migrations.VERIFIED_PREDECESSOR_EPOCH = 1
def crash(conn, cursor, statement, parameters, context, executemany):
    if statement.startswith('CREATE INDEX ix_no_copy_2'):
        os._exit(71)
event.listen(Engine, 'after_cursor_execute', crash)
migrations.run_migrations(Path(sys.argv[1]))
""", str(ready_db), str(graph)], check=False, capture_output=True, text=True)
    assert result.returncode == 71, result.stderr
    with closing(sqlite3.connect(ready_db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (FIRST,)
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("1",)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ix_no_copy_2'").fetchone() is None
    assert _rollback_artifacts(ready_db) == before
    assert migrations.run_migrations(ready_db).skip_snapshot
    with closing(sqlite3.connect(ready_db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (SECOND,)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ix_no_copy_2'").fetchone() is not None
    assert _rollback_artifacts(ready_db) == before
