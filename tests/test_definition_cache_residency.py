"""Resident mirrors are live work, not a second copy of definition history.

Lifecycle suites cover retirement outcomes. These tests protect the distinct
storage/caching boundary: history never gets hydrated by a reload, yet retired
rows remain addressable and mutable without being cached again.
"""
from __future__ import annotations

import asyncio
from importlib import import_module

import pytest
from sqlalchemy import event, select, update

from core.scheduled_tasks import ScheduledTaskStore
from core.watches import ManagedWatchStore, WatchRuntimeStateStore
from storage.background import SQLiteBackgroundTaskStore
from storage.models import run_definitions
from vibe import cli


@pytest.mark.parametrize("kind,total,retired", [("watch", 3800, 3500), ("scheduled", 4000, 3500)])
def test_reload_excludes_retirement_history_without_changing_by_id_or_lists(kind, total, retired):
    db = SQLiteBackgroundTaskStore()
    stamp = "2026-11-01T00:00:00+00:00"
    encode = db._watch_values if kind == "watch" else db._scheduled_task_values
    rows = [encode({
        "id": f"{kind}-{i}", "enabled": i >= retired, "mode": "once",
        "schedule_type": "at", "run_at": "2030-01-01T00:00:00+00:00", "timezone": "UTC",
        "retired_at": stamp if i < retired else None, "created_at": stamp, "updated_at": stamp,
        "last_finished_at": stamp if i < retired else None,
        "last_exit_code": 64 if i < retired else None, "shell_command": "true",
    }) for i in range(total)]
    with db.engine.begin() as conn:
        conn.execute(run_definitions.insert(), rows)
        # Paused definitions are still resident; they are not retirement history.
        conn.execute(update(run_definitions).where(run_definitions.c.id == f"{kind}-{retired}").values(enabled=0))
    store = ManagedWatchStore() if kind == "watch" else ScheduledTaskStore()
    resident = store.list_watches if kind == "watch" else store.list_tasks
    get = store.get_watch if kind == "watch" else store.get_task
    list_all = db.list_watches if kind == "watch" else db.list_scheduled_tasks
    get_projected = db.get_watch if kind == "watch" else db.get_scheduled_task
    statements = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(store.sqlite_backend.engine, "before_cursor_execute", capture)
    try:
        # Exercise data_version too: ordinary external writes must take the same
        # bounded path as startup/recovery reloads, not a separate all-history path.
        store.maybe_reload()
        statements.clear()
        with db.engine.begin() as conn:
            conn.execute(update(run_definitions).where(run_definitions.c.id == f"{kind}-{retired}").values(name="paused"))
        assert store.maybe_reload()
        assert len(resident()) == total - retired
        assert {item.id for item in resident()} == {f"{kind}-{i}" for i in range(retired, total)}
        statement, parameters = statements[0]
        with db.engine.connect() as conn:
            plan = "\n".join(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters))
        assert "SEARCH run_definitions USING INDEX ix_run_definitions_resident" in plan
        assert "USE TEMP B-TREE" not in plan
        assert "retired_at IS NULL" in statement

        historical = f"{kind}-0"
        before = get_projected(historical)
        assert before["lifecycle_state"] == "finished"
        statements.clear()
        assert get(historical).retired_at == stamp
        # Cache-only callers on the controller loop may miss on a retired row.
        # Its fallback must stay one indexed record read, not UI/Run enrichment.
        assert len(statements) == 1
        statement, parameters = statements[0]
        with db.engine.connect() as conn:
            plan = "\n".join(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters))
        assert "SEARCH run_definitions USING INDEX sqlite_autoindex_run_definitions_1 (id=?)" in plan
        if kind == "scheduled":
            assert "SEARCH agent_runs USING INDEX sqlite_autoindex_agent_runs_1 (id=?) LEFT-JOIN" in plan
        else:
            assert "agent_runs" not in statement
        assert len(resident()) == total - retired
        assert len(list_all()) == total
        assert get_projected(historical) == before
        # A disabled/retired definition can still be paused or removed. Neither
        # a read nor a write may put it back into the resident mirror.
        assert store.set_enabled(historical, False).retired_at == stamp
        assert len(resident()) == total - retired
        remove = store.remove_watch if kind == "watch" else store.remove_task
        assert remove(historical)
        assert get_projected(historical) is None
        assert get_projected(historical, include_removed=True)["lifecycle_state"] == "removed"
    finally:
        store.sqlite_backend.close()
        db.close()


def test_watch_retirement_evicts_immediately_and_resume_republishes():
    store = ManagedWatchStore()
    try:
        watch = store.add_watch(
            name=None, session_key="", command=[], shell_command="true", prefix=None, cwd=None,
            mode="once", timeout_seconds=60, lifetime_timeout_seconds=0,
            retry_exit_codes=[75], retry_delay_seconds=30, post_to=None, deliver_key=None,
        )
        assert store.mark_cycle_result(watch.id, exit_code=64, error=None, disable=True)
        assert store.list_watches() == []
        assert store.get_watch(watch.id).last_exit_code == 64
        assert store.set_enabled(watch.id, True).retired_at is None
        assert [item.id for item in store.list_watches()] == [watch.id]
    finally:
        store.sqlite_backend.close()


@pytest.mark.asyncio
async def test_startup_observes_only_the_named_watch_without_reloading_either_mirror(monkeypatch):
    store, runtime = ManagedWatchStore(), WatchRuntimeStateStore()
    writer = SQLiteBackgroundTaskStore()
    try:
        watch = store.add_watch(
            name=None, session_key="", command=[], shell_command="true", prefix=None, cwd=None,
            mode="once", timeout_seconds=60, lifetime_timeout_seconds=0,
            retry_exit_codes=[75], retry_delay_seconds=30, post_to=None, deliver_key=None,
        )
        monkeypatch.setattr(store, "maybe_reload", lambda: pytest.fail("startup reloaded the definition mirror"))
        monkeypatch.setattr(runtime, "load", lambda: pytest.fail("startup loaded every runtime"))
        observed = []
        read_watch = store.sqlite_backend.get_watch

        def read_one(watch_id):
            observed.append(watch_id)
            return read_watch(watch_id)

        monkeypatch.setattr(store.sqlite_backend, "get_watch", read_one)

        async def finish():
            await asyncio.sleep(0.02)
            with writer.engine.begin() as conn:
                conn.execute(update(run_definitions).where(run_definitions.c.id == watch.id).values(
                    enabled=0, retired_at="2026-11-01", last_finished_at="2026-11-01", last_exit_code=64,
                ))

        finishing = asyncio.create_task(finish())
        observed_watch, _ = await cli._wait_for_watch_startup(
            store, runtime, watch.id, timeout_seconds=2, poll_interval_seconds=0.01,
        )
        await finishing
        assert observed_watch.last_exit_code == 64
        assert len(observed) >= 2 and set(observed) == {watch.id}
    finally:
        store.sqlite_backend.close()
        runtime._sqlite.close()
        writer.close()


def test_resident_index_upgrade_and_downgrade_preserve_retirement(monkeypatch):
    revision = import_module("storage.alembic.versions.20261107_0074_resident_definitions_index")
    db = SQLiteBackgroundTaskStore()
    try:
        db.upsert_watch({
            "id": "retired", "enabled": False, "retired_at": "2026-11-01", "mode": "once",
            "created_at": "2026-11-01", "updated_at": "2026-11-01",
        })
        with db.engine.begin() as conn:
            monkeypatch.setattr(revision.op, "get_bind", lambda: conn)
            before = dict(conn.execute(select(run_definitions)).mappings().one())
            revision.downgrade()
            assert not conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='ix_run_definitions_resident'").first()
            revision.upgrade()
            assert conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='ix_run_definitions_resident'").first()
            assert dict(conn.execute(select(run_definitions)).mappings().one()) == before
    finally:
        db.close()
