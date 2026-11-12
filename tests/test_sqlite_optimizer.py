"""Bounded SQLite optimizer maintenance."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from storage import sqlite_optimizer


class _Connection:
    def __init__(self, *, version: str = "3.50.4", cancel_event: threading.Event | None = None) -> None:
        self.version = version
        self.statements: list[str] = []
        self.cancel_event = cancel_event

    def exec_driver_sql(self, statement: str):
        self.statements.append(statement)
        if statement == "SELECT sqlite_version()":
            return SimpleNamespace(scalar_one=lambda: self.version)
        if self.cancel_event is not None and statement == "PRAGMA optimize=0x10002":
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
    def __init__(self, connection: _Connection, *, mode: str | None = None) -> None:
        self.connection = connection
        self.begin_calls = 0
        self.url = SimpleNamespace(query={"mode": mode} if mode else {})

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
        "sqlite_version": "3.50.4",
        "analysis_limit": 1000,
        "optimize": "PRAGMA optimize=0x10002",
        "fallback_analyze": False,
    }
    assert engine.begin_calls == 1
    assert connection.statements == [
        "SELECT sqlite_version()",
        "PRAGMA analysis_limit=1000",
        "PRAGMA optimize=0x10002",
    ]


def test_legacy_supported_sqlite_uses_bounded_analyze_fallback() -> None:
    connection = _Connection(version="3.45.3")
    engine = _Engine(connection)

    result = sqlite_optimizer.run(engine, startup=False)

    assert result["status"] == "ok"
    assert result["fallback_analyze"] is True
    assert connection.statements == [
        "SELECT sqlite_version()",
        "PRAGMA analysis_limit=1000",
        "PRAGMA optimize=0x10002",
        "ANALYZE",
    ]


def test_unsupported_sqlite_does_not_claim_statistics() -> None:
    connection = _Connection(version="3.31.1")
    engine = _Engine(connection)

    assert sqlite_optimizer.run(engine, startup=True) == {
        "status": "unsupported",
        "sqlite_version": "3.31.1",
    }
    assert connection.statements == ["SELECT sqlite_version()"]


def test_read_only_engine_does_not_open_or_mutate_connection() -> None:
    engine = _Engine(_Connection(), mode="ro")

    assert sqlite_optimizer.run(engine, startup=True) == {"status": "read_only"}
    assert engine.begin_calls == 0


def test_daily_optimizer_observes_cancellation() -> None:
    cancel_event = threading.Event()
    connection = _Connection(cancel_event=cancel_event)
    engine = _Engine(connection)

    result = sqlite_optimizer.run(engine, startup=False, cancel_event=cancel_event)

    assert result == {"status": "cancelled"}
    assert connection.statements == [
        "SELECT sqlite_version()",
        "PRAGMA analysis_limit=1000",
        "PRAGMA optimize=0x10002",
    ]


def test_cancelled_optimizer_does_not_open_connection() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    engine = _Engine(_Connection())

    assert sqlite_optimizer.run(engine, startup=True, cancel_event=cancel_event) == {"status": "cancelled"}
    assert engine.begin_calls == 0


def test_real_sqlite_refreshes_statistics_on_fresh_connections(tmp_path) -> None:
    db_path = tmp_path / "planner.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE deliveries (id INTEGER PRIMARY KEY, state TEXT NOT NULL, message_id TEXT NOT NULL)"
            )
            conn.exec_driver_sql("CREATE INDEX ix_deliveries_state ON deliveries(state)")
            conn.exec_driver_sql("CREATE INDEX ix_deliveries_message ON deliveries(message_id)")
            conn.execute(
                text("INSERT INTO deliveries(state, message_id) VALUES (:state, :message_id)"),
                [{"state": "accepted", "message_id": f"message-{i:05d}"} for i in range(1_000)],
            )

        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE name = 'sqlite_stat1'").first() is None

        first = sqlite_optimizer.run(engine, startup=True)
        assert first["status"] == "ok"
        with engine.connect() as conn:
            stat_before_growth = conn.exec_driver_sql(
                "SELECT stat FROM sqlite_stat1 WHERE idx = 'ix_deliveries_message'"
            ).scalar_one()
            before_plan = conn.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM deliveries WHERE message_id = 'message-00001'"
            ).all()
        assert stat_before_growth.startswith("1000 ")
        assert "ix_deliveries_message" in before_plan[0][3]

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO deliveries(state, message_id) VALUES (:state, :message_id)"),
                [{"state": "accepted", "message_id": f"message-{i:05d}"} for i in range(1_000, 12_000)],
            )

        second = sqlite_optimizer.run(engine, startup=False)
        assert second["status"] == "ok"
        with engine.connect() as conn:
            stat_after_growth = conn.exec_driver_sql(
                "SELECT stat FROM sqlite_stat1 WHERE idx = 'ix_deliveries_message'"
            ).scalar_one()
            after_plan = conn.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM deliveries WHERE message_id = 'message-00001'"
            ).all()
        assert stat_after_growth.startswith("12000 ")
        assert "ix_deliveries_message" in after_plan[0][3]
    finally:
        engine.dispose()
