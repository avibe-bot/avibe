"""Bounded SQLite optimizer maintenance."""

from __future__ import annotations

import threading

from storage import sqlite_optimizer


class _Connection:
    def __init__(self, *, cancel_event: threading.Event | None = None) -> None:
        self.statements: list[str] = []
        self.cancel_event = cancel_event

    def exec_driver_sql(self, statement: str):
        self.statements.append(statement)
        if self.cancel_event is not None and statement == "PRAGMA optimize":
            self.cancel_event.set()
        return None


class _Begin:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.begin_calls = 0

    def begin(self) -> _Begin:
        self.begin_calls += 1
        return _Begin(self.connection)


def test_startup_optimizer_uses_bounded_all_table_mask() -> None:
    connection = _Connection()
    engine = _Engine(connection)

    result = sqlite_optimizer.run(engine, startup=True)

    assert result == {
        "status": "ok",
        "startup": True,
        "analysis_limit": 1000,
        "optimize": "PRAGMA optimize=0x10002",
    }
    assert engine.begin_calls == 1
    assert connection.statements == [
        "PRAGMA analysis_limit=1000",
        "PRAGMA optimize=0x10002",
    ]


def test_daily_optimizer_uses_normal_mask_and_observes_cancellation() -> None:
    cancel_event = threading.Event()
    connection = _Connection(cancel_event=cancel_event)
    engine = _Engine(connection)

    result = sqlite_optimizer.run(engine, startup=False, cancel_event=cancel_event)

    assert result == {"status": "cancelled"}
    assert connection.statements == [
        "PRAGMA analysis_limit=1000",
        "PRAGMA optimize",
    ]


def test_cancelled_optimizer_does_not_open_connection() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    engine = _Engine(_Connection())

    assert sqlite_optimizer.run(engine, startup=True, cancel_event=cancel_event) == {"status": "cancelled"}
    assert engine.begin_calls == 0
