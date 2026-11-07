"""Snapshot safety at the rollback-copy boundary, without a running Avibe.

Existing backup tests exercise online backup/retention/restore; they do not
prove a raw clone includes committed WAL or excludes ordinary SQLite writers.
"""
from __future__ import annotations

from contextlib import closing
import errno
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from storage import backups


def _database(path: Path, *, wal_tail: bool = False) -> None:
    # Closing the last WAL connection checkpoints it. Exit a separate process
    # without SQLite close to leave committed data exclusively in the WAL.
    subprocess.run(
        [sys.executable, "-c", """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)')
conn.execute('CREATE TABLE alembic_version (version_num TEXT)')
conn.execute("INSERT INTO alembic_version VALUES ('before')")
conn.commit()
if sys.argv[2] == 'True':
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
conn.execute("INSERT INTO records VALUES (1, 'committed')")
conn.commit()
os._exit(0)
""", str(path), str(wal_tail)],
        check=True, timeout=20,
    )


def _value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute('SELECT value FROM records').fetchone()[0]


@pytest.mark.parametrize("wal_tail", [False, True])
def test_checkpoint_and_clone_exclude_sqlite_writers_and_keep_committed_data(tmp_path, monkeypatch, wal_tail):
    db = tmp_path / "vibe.sqlite"
    _database(db, wal_tail=wal_tail)
    calls = []

    def clone(source, destination):
        calls.append(source)
        wal = source.with_name(source.name + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0
        # No MigrationFileLock in this child: a cooperative migrator lock is
        # not enough to protect a raw file copy from existing store connections.
        attempt = subprocess.run(
            [sys.executable, "-c", """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0)
try:
    conn.execute("UPDATE records SET value='racing writer'")
    conn.commit()
except sqlite3.OperationalError as exc:
    assert 'locked' in str(exc), str(exc)
    sys.exit(0)
sys.exit(1)
""", str(source)],
            timeout=20,
        )
        assert attempt.returncode == 0, "writer ran between checkpoint and clone"
        shutil.copyfile(source, destination)

    monkeypatch.setattr(backups, "_clone_file", clone, raising=False)
    # Exercise the locking path even on platforms without native cloning.
    monkeypatch.setattr(backups.sys, "platform", "darwin")
    snapshot = backups.create_sqlite_migration_backup(db)
    assert calls == [db]
    assert _value(snapshot / "vibe.sqlite") == "committed"
    assert json.loads((snapshot / "manifest.json").read_text())["from_revisions"] == ["before"]
    with closing(sqlite3.connect(db, timeout=0)) as conn:
        conn.execute("UPDATE records SET value='after snapshot'")
        conn.commit()
    assert _value(db) == "after snapshot"
    assert _value(snapshot / "vibe.sqlite") == "committed"
    assert not list(snapshot.glob("*-wal"))
    assert not list(snapshot.glob("*-shm"))


@pytest.mark.parametrize("error", [errno.EXDEV, errno.ENOTSUP, errno.ENOSPC, errno.EIO])
def test_clone_errors_clean_partial_file_and_use_online_backup(tmp_path, monkeypatch, error):
    db = tmp_path / "vibe.sqlite"
    _database(db, wal_tail=True)
    attempts = []

    def fail_clone(source, destination):
        attempts.append(source)
        destination.write_bytes(b"incomplete clone")
        raise OSError(error, "clone failed")

    monkeypatch.setattr(backups.sys, "platform", "darwin")
    monkeypatch.setattr(backups, "_clone_file", fail_clone, raising=False)
    snapshot = backups.create_sqlite_migration_backup(db)
    assert attempts == [db]
    assert _value(snapshot / "vibe.sqlite") == "committed"
    assert not list(snapshot.glob("*.tmp"))


def test_busy_wal_reader_uses_online_backup_instead_of_omitting_the_tail(tmp_path, monkeypatch):
    db = tmp_path / "vibe.sqlite"
    _database(db, wal_tail=True)
    with closing(sqlite3.connect(db)) as reader, closing(sqlite3.connect(db)) as writer:
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM records").fetchone() == ("committed",)
        writer.execute("UPDATE records SET value='new WAL commit'")
        writer.commit()
        monkeypatch.setattr(backups, "_clone_file", lambda *args: pytest.fail("cloned with a live reader"), raising=False)
        snapshot = backups.create_sqlite_migration_backup(db)
        assert _value(snapshot / "vibe.sqlite") == "new WAL commit"


def test_clone_is_verified_before_publication(tmp_path, monkeypatch):
    db = tmp_path / "vibe.sqlite"
    _database(db)
    previous = backups.create_sqlite_migration_backup(db)
    monkeypatch.setattr(backups.sys, "platform", "darwin")
    monkeypatch.setattr(backups, "_clone_file", lambda src, dst: dst.write_bytes(b"corrupt clone"), raising=False)
    with pytest.raises(sqlite3.DatabaseError):
        backups.create_sqlite_migration_backup(db)
    assert list((tmp_path / "backups").iterdir()) == [previous]
    assert _value(previous / "vibe.sqlite") == "committed"


def test_native_clone_is_an_independent_verified_restorable_database(tmp_path):
    db = tmp_path / "vibe.sqlite"
    _database(db, wal_tail=True)
    # Probe the actual filesystem, not only the OS; CI's Linux tempfs/ext4 may
    # not offer reflinks. Unsupported filesystems are covered by fallback above.
    probe = tmp_path / "clone-probe"
    try:
        backups._clone_file(db, probe)
    except (OSError, AttributeError, subprocess.SubprocessError) as exc:
        pytest.skip(f"Native COW clone unavailable on this filesystem: {exc}")
    finally:
        probe.unlink(missing_ok=True)
    assert backups._try_clone_sqlite_snapshot(db, probe)
    probe.unlink()
    snapshot = backups.create_sqlite_migration_backup(db)
    copy = snapshot / "vibe.sqlite"
    assert db.stat().st_ino != copy.stat().st_ino
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE records SET value='new generation'")
        conn.commit()
    assert _value(copy) == "committed"
    backups.restore_sqlite_backup(snapshot, db)
    assert _value(db) == "committed"
    assert _value(snapshot / backups.REPLACED_DATABASE_NAME) == "new generation"
