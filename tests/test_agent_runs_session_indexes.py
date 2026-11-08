"""Session-scoped Run reads seek each ownership arm instead of scanning history."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event

from storage import migrations

from storage.background import SQLiteBackgroundTaskStore
from storage.pagination import PageRequest


def test_session_run_page_seeks_all_ownership_arms(tmp_path: Path, sqlite_db_factory):
    """An unindexed callback/source arm makes SQLite scan even the indexed arm.

    Existing projection/pagination tests assert results and enrichment count, not
    the plan of the Run selection. Use the actual statement issued by the store;
    a correct result alone cannot expose a history-wide scan.
    """
    path = sqlite_db_factory(tmp_path / "runs.sqlite")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into agent_runs "
            "(id, run_type, status, session_id, callback_session_id, source_kind, source_actor, "
            "created_at, updated_at, metadata_json) "
            "values (?, 'agent_run', 'succeeded', ?, ?, ?, ?, '2026-11-07T00:00:00Z', "
            "'2026-11-07T00:00:00Z', '{}')",
            [(f"history-{i:05}", "other", "other", "agent", "other") for i in range(2000)]
            + [
                ("match-1", "target", None, None, None),
                ("match-2", None, "target", None, None),
                ("match-3", None, None, "agent", "target"),
                ("match-4", "target", "target", "agent", "target"),
                ("not-an-agent", None, None, "callback", "target"),
            ],
        )
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM agent_runs" in statement:
            statements.append((statement, parameters))

    store = SQLiteBackgroundTaskStore(path, read_only=True)
    event.listen(store.engine, "before_cursor_execute", record)
    try:
        page = store.list_runs_page(session_id="target", page_request=PageRequest(page=1, limit=3))
        assert [row["id"] for row in page.items] == ["match-4", "match-3", "match-2"]
        assert page.has_more
        page = store.list_runs_page(session_id="target", page_request=PageRequest(page=2, limit=3))
        assert [row["id"] for row in page.items] == ["match-1"]
        assert not page.has_more
    finally:
        event.remove(store.engine, "before_cursor_execute", record)
        store.close()
    assert len(statements) == 2
    with sqlite3.connect(path) as conn:
        for statement, parameters in statements:
            plan = [row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + statement, parameters)]
            assert not any("SCAN agent_runs" in detail for detail in plan), plan
            for keys in ("session_id=?", "callback_session_id=?", "source_kind=? AND source_actor=?"):
                assert any("SEARCH agent_runs" in detail and keys in detail for detail in plan), plan


@pytest.mark.no_sqlite_template
def test_session_run_indexes_upgrade_and_downgrade_preserve_rows(tmp_path: Path):
    """The optimization upgrades deployed databases and downgrades without data loss."""
    from tests.migration_helpers import downgrade_legacy_schema

    path = tmp_path / "upgrade.sqlite"
    migrations.run_migrations(path, revision="20261107_0074")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agent_runs (id, run_type, status, session_id, callback_session_id, "
            "source_kind, source_actor, created_at, updated_at, metadata_json) "
            "values ('existing', 'agent_run', 'succeeded', 'execution', 'callback', 'agent', "
            "'source', '2026-11-07T00:00:00Z', '2026-11-07T00:00:00Z', '{}')"
        )
        before = conn.execute("select * from agent_runs").fetchall()
        before_indexes = conn.execute("select name, sql from sqlite_master where type='index' order by name").fetchall()
    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select * from agent_runs").fetchall() == before
        assert [row[2] for row in conn.execute("pragma index_info(ix_agent_runs_callback_session)")] == ["callback_session_id"]
        assert [row[2] for row in conn.execute("pragma index_info(ix_agent_runs_source_actor)")] == ["source_kind", "source_actor"]
    downgrade_legacy_schema(migrations.alembic_config(path), "20261107_0074")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select * from agent_runs").fetchall() == before
        assert conn.execute("select name, sql from sqlite_master where type='index' order by name").fetchall() == before_indexes
