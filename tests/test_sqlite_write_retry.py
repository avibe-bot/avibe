"""Which SQLite failures a whole write transaction is retried for (``storage.db.retry_sqlite_write``).

Only writer contention (primary code ``SQLITE_BUSY``) can succeed by running the transaction again; every
other failure, ``SQLITE_LOCKED`` included, is a defect a retry would hide or repeat.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from storage import db
from storage.db import create_sqlite_engine, retry_sqlite_write


def _busy(tmp_path) -> sqlite3.OperationalError:
    path = tmp_path / "busy.sqlite"
    holder = sqlite3.connect(path, isolation_level=None)
    waiter = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            waiter.execute("BEGIN IMMEDIATE")
        return caught.value
    finally:
        holder.close()
        waiter.close()


def _busy_snapshot(tmp_path) -> sqlite3.OperationalError:
    path = tmp_path / "snapshot.sqlite"
    writer = sqlite3.connect(path, isolation_level=None)
    reader = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE t (x)")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM t").fetchall()
        writer.execute("INSERT INTO t VALUES (1)")
        # A read snapshot that another commit made stale can no longer become a writer.
        with pytest.raises(sqlite3.OperationalError) as caught:
            reader.execute("INSERT INTO t VALUES (2)")
        return caught.value
    finally:
        reader.close()
        writer.close()


def _table_locked(_tmp_path) -> sqlite3.OperationalError:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1), (2)")
        pending = conn.execute("SELECT * FROM t")
        pending.fetchone()
        with pytest.raises(sqlite3.OperationalError) as caught:
            conn.execute("DROP TABLE t")
        return caught.value
    finally:
        conn.close()


def _wrapped_busy(tmp_path) -> OperationalError:
    path = tmp_path / "wrapped.sqlite"
    engine = create_sqlite_engine(path)
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA busy_timeout = 0")
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(OperationalError) as caught:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
        return caught.value
    finally:
        holder.close()
        engine.dispose()


def _no_such_table(tmp_path) -> OperationalError:
    engine = create_sqlite_engine(tmp_path / "missing.sqlite")
    try:
        with engine.connect() as conn, pytest.raises(OperationalError) as caught:
            conn.execute(text("SELECT * FROM missing"))
        return caught.value
    finally:
        engine.dispose()


def _integrity(_tmp_path) -> IntegrityError:
    return IntegrityError("INSERT", {}, sqlite3.IntegrityError("UNIQUE constraint failed: t.x"))


@pytest.mark.parametrize(
    ("failure", "retried"),
    [
        (_busy, True),
        (_busy_snapshot, True),
        (_wrapped_busy, True),
        # Python 3.10 has no ``sqlite_errorcode``: the busy message alone stands for the code there.
        (lambda _tmp_path: sqlite3.OperationalError("database is locked"), True),
        (_table_locked, False),
        (lambda _tmp_path: sqlite3.OperationalError("database table is locked"), False),
        (_no_such_table, False),
        (_integrity, False),
        (lambda _tmp_path: RuntimeError("database is locked"), False),
    ],
    ids=[
        "busy",
        "busy-snapshot",
        "busy-through-sqlalchemy",
        "busy-without-errorcode",
        "table-locked",
        "table-locked-without-errorcode",
        "no-such-table",
        "integrity",
        "not-sqlite",
    ],
)
def test_only_writer_contention_is_retried_and_only_a_bounded_number_of_times(
    monkeypatch, tmp_path, failure, retried
) -> None:
    error = failure(tmp_path)
    pauses: list[float] = []
    monkeypatch.setattr(db.time, "sleep", pauses.append)
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(len(attempts) + 1)
        raise error

    with pytest.raises(type(error)) as raised:
        retry_sqlite_write(operation)

    assert raised.value is error
    assert attempts == ([1, 2, 3] if retried else [1])
    assert pauses == ([0.05, 0.1] if retried else [])
