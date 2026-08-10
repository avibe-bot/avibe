from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    PrivateSqliteDatabase,
    remove_anchored_entry,
    remove_confined_path,
)


def test_remove_confined_path_refuses_to_follow_a_swapped_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "managed"
    target.mkdir(mode=0o700)
    (target / "nested").mkdir(mode=0o700)
    (target / "nested" / "owned.txt").write_text("owned", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("outside must survive", encoding="utf-8")
    moved = home / "held-managed"

    from core.memory import confined_filesystem

    real_scandir = confined_filesystem.os.scandir
    swapped = False

    def swap_directory_for_symlink(path):
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            target.rename(moved)
            target.symlink_to(outside, target_is_directory=True)
        return real_scandir(path)

    monkeypatch.setattr(confined_filesystem.os, "scandir", swap_directory_for_symlink)
    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, target)

    assert swapped
    assert target.is_symlink()
    assert victim.read_text(encoding="utf-8") == "outside must survive"


def test_private_sqlite_database_prepares_and_hardens_owned_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)

    database.prepare()
    connection = database.connect()
    try:
        connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o644)

    database.harden(sync_parent=True)

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert home.stat().st_mode & 0o777 == 0o700


def test_private_sqlite_transaction_commits_before_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []

    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            events.append("commit")
            super().commit()

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with database.transaction() as connection:
        connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
        connection.execute("INSERT INTO item VALUES ('committed')")

    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM item").fetchone() == (
            "committed",
        )
    assert events == ["begin", "commit", "close", "harden"]


def test_private_sqlite_transaction_body_failure_remains_primary_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []
    body_error = RuntimeError("body failed")

    real_connect = sqlite3.connect

    class FailingCleanupConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()
            raise OSError("rollback cleanup failed")

        def close(self) -> None:
            events.append("close")
            super().close()
            raise OSError("close cleanup failed")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingCleanupConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(RuntimeError) as raised:
        with database.transaction() as connection:
            connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
            raise body_error

    with real_connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'item'"
        ).fetchone() is None
    assert raised.value is body_error
    assert events == ["begin", "rollback", "close", "harden"]


def test_private_sqlite_transaction_commit_failure_remains_primary_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []
    commit_error = sqlite3.OperationalError("commit failed")

    real_connect = sqlite3.connect

    class FailingCommitConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            events.append("commit")
            raise commit_error

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()
            raise OSError("rollback cleanup failed")

        def close(self) -> None:
            events.append("close")
            super().close()
            raise OSError("close cleanup failed")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingCommitConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(sqlite3.OperationalError) as raised:
        with database.transaction() as connection:
            connection.execute("CREATE TABLE item (value TEXT NOT NULL)")

    with real_connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'item'"
        ).fetchone() is None
    assert raised.value is commit_error
    assert events == ["begin", "commit", "rollback", "close", "harden"]


def test_private_sqlite_transaction_rolls_back_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InjectedCancellation(BaseException):
        pass

    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
    events: list[str] = []
    cancellation = InjectedCancellation()

    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(InjectedCancellation) as raised:
        with database.transaction() as connection:
            connection.execute("INSERT INTO item VALUES ('rolled back')")
            raise cancellation

    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM item").fetchall() == []
    assert raised.value is cancellation
    assert events == ["begin", "rollback", "close", "harden"]


def test_private_sqlite_transaction_translates_hardening_failure_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class JournalHardeningError(RuntimeError):
        pass

    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    events: list[str] = []
    translated = JournalHardeningError("journal files could not be hardened safely")

    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "BEGIN IMMEDIATE":
                events.append("begin")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            events.append("commit")
            super().commit()

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=TrackingConnection)

    from core.memory import confined_filesystem

    real_chmod = confined_filesystem.os.chmod
    harden_error = ConfinedFilesystemError("unsafe SQLite sidecar")

    def chmod(target, mode: int) -> None:
        if Path(target) == path and mode == 0o600:
            events.append("harden")
            raise harden_error
        real_chmod(target, mode)

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)
    monkeypatch.setattr(confined_filesystem.os, "chmod", chmod)

    with pytest.raises(JournalHardeningError) as raised:
        with database.transaction(
            translate_harden_error=lambda _error: translated,
        ) as connection:
            connection.execute("CREATE TABLE item (value TEXT NOT NULL)")
            connection.execute("INSERT INTO item VALUES ('durable')")

    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM item").fetchone() == ("durable",)
    assert raised.value is translated
    assert raised.value.__cause__ is harden_error
    assert events == ["begin", "commit", "close", "harden"]


def test_private_sqlite_transaction_closes_setup_failure_without_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    setup_error = sqlite3.OperationalError("SQLite setup failed")
    events: list[str] = []
    real_connect = sqlite3.connect

    class FailingSetupConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=(), /):
            if sql == "PRAGMA synchronous=FULL":
                raise setup_error
            return super().execute(sql, parameters)

        def close(self) -> None:
            events.append("close")
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingSetupConnection)

    from core.memory import confined_filesystem

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError) as raised:
        with database.transaction():
            pytest.fail("transaction body must not run after setup failure")

    assert raised.value is setup_error
    assert events == ["close"]


def test_private_sqlite_database_rejects_an_unsafe_sidecar(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    outside = tmp_path / "outside"
    outside.write_bytes(b"")
    sidecar = path.with_name(f"{path.name}-wal")
    sidecar.symlink_to(outside)

    with pytest.raises(ConfinedFilesystemError):
        database.connect()


@pytest.mark.parametrize("unsafe_kind", ("hardlink", "fifo"))
def test_private_sqlite_database_rejects_unsafe_database_entries(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = home / "journal.sqlite"
    if unsafe_kind == "hardlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        os.link(outside, path)
    else:
        os.mkfifo(path, mode=0o600)

    with pytest.raises(ConfinedFilesystemError):
        PrivateSqliteDatabase(home, path).prepare()


def test_private_sqlite_database_rejects_unowned_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    path = home / "journal.sqlite"
    database = PrivateSqliteDatabase(home, path)
    database.prepare()
    real_uid = os.getuid()

    from core.memory import confined_filesystem

    monkeypatch.setattr(confined_filesystem.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ConfinedFilesystemError):
        database.connect()


def test_remove_confined_path_unlinks_links_without_touching_their_victims(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("outside must survive", encoding="utf-8")

    symlink = home / "symlink"
    symlink.symlink_to(victim)
    hardlink = home / "hardlink"
    os.link(victim, hardlink)

    remove_confined_path(home, symlink)
    remove_confined_path(home, hardlink)

    assert not symlink.exists()
    assert not hardlink.exists()
    assert victim.read_text(encoding="utf-8") == "outside must survive"


def test_remove_confined_path_rejects_root_outside_and_special_entries(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("outside must survive", encoding="utf-8")
    fifo = home / "fifo"
    os.mkfifo(fifo, mode=0o600)

    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, home)
    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, outside)
    with pytest.raises(ConfinedFilesystemError):
        remove_confined_path(home, fifo)

    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert outside.read_text(encoding="utf-8") == "outside must survive"


def test_remove_anchored_entry_requires_the_expected_directory_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = home / "target"
    target.mkdir(mode=0o700)
    identity = os.lstat(target)
    target.rename(home / "moved")
    target.mkdir(mode=0o700)
    descriptor = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(ConfinedFilesystemError):
            remove_anchored_entry(
                descriptor,
                target.name,
                expected_identity=(identity.st_dev, identity.st_ino),
            )
    finally:
        os.close(descriptor)

    assert target.is_dir()
    assert (home / "moved").is_dir()
