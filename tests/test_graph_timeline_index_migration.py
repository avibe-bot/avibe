"""Released writers need no new protocol: SQLite maintains the timestamp index."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import event
from sqlalchemy.engine import Engine

from core.services import agent_graph
from storage import migrations
from storage.models import agent_runs
from tests.test_agent_graph_service import NOW, _insert_run, _insert_session
from storage.db import create_sqlite_engine

pytestmark = pytest.mark.no_sqlite_template
PREVIOUS = "20261122_0098"
REVISION = "20261123_0099"
INDEX = "ix_agent_runs_timeline_times"


def test_graph_timeline_index_upgrade_rollback_and_old_writes(tmp_path, caplog):
    path = tmp_path / "graph.sqlite"
    migrations.run_migrations(path, revision=PREVIOUS)
    engine = create_sqlite_engine(path)
    with engine.begin() as c:
        _insert_session(c, "s", scope_id=None)
        for rid in ("r0", "r1", "r2"):
            _insert_run(c, rid, session_id="s")
        c.exec_driver_sql("UPDATE agent_runs SET started_at=NULL WHERE id='r0'")
        c.exec_driver_sql("UPDATE agent_runs SET started_at='2026-07-22T20:40:03.100-04:00', "
                          "completed_at='2026-07-23T01:02:03Z' WHERE id='r1'")
    try:
        before = agent_graph.build_graph(engine=engine, now=NOW)
        caplog.set_level(logging.INFO, logger="storage.migrations")
        notice = f"Building SQLite index {INDEX}"
        build_count = 0
        plans = []

        def observe(conn, cursor, sql, params, context, many):
            nonlocal build_count
            if sql.startswith("CREATE INDEX") and INDEX in sql:
                assert caplog.messages.count(notice) == 1, "Notice must precede the actual DDL"
                build_count += 1
            if sql.startswith("SELECT id, started_at, completed_at FROM agent_runs"):
                cursor.execute("EXPLAIN QUERY PLAN " + sql, params)
                plans.extend(row[-1] for row in cursor.fetchall())

        event.listen(Engine, "before_cursor_execute", observe)
        try:
            migrations.run_migrations(path, revision=REVISION)
            migrations.run_migrations(path, revision=REVISION)
            assert build_count == caplog.messages.count(notice) == 1
            assert agent_graph.build_graph(engine=engine, now=NOW) == before
            assert plans and all(f"COVERING INDEX {INDEX} (id=?)" in plan for plan in plans)
        finally:
            event.remove(Engine, "before_cursor_execute", observe)

        with closing(sqlite3.connect(path)) as old:
            columns = [r[2] for r in old.execute(f"PRAGMA index_info({INDEX})")]
            assert columns == list(next(i for i in agent_runs.indexes if i.name == INDEX).columns.keys())
            assert columns == ["id", "started_at", "completed_at"]
            # Pre-index writer. It doesn't update a projection or even advance
            # updated_at. Both UPDATE and REPLACE must immediately change hits.
            old.execute("UPDATE agent_runs SET started_at='2026-07-23T01:01:01Z' WHERE id='r0'")
            old.execute("INSERT OR REPLACE INTO agent_runs(id, run_type, status, session_id, created_at, "
                        "updated_at, metadata_json, cancel_requested) SELECT id, run_type, status, session_id, "
                        "created_at, updated_at, metadata_json, cancel_requested FROM agent_runs WHERE id='r1'")
            old.commit()
        changed = agent_graph.build_graph(engine=engine, now=NOW)
        runs = {r['id']: r for r in changed['nodes'][0]['runs']}
        assert runs['r0']['started_at'] == '2026-07-23T01:01:01Z'
        assert runs['r1']['started_at'] is None and runs['r1']['completed_at'] is None
        command.downgrade(migrations.alembic_config(path), PREVIOUS)
        with closing(sqlite3.connect(path)) as c:
            assert c.execute(f"PRAGMA index_info({INDEX})").fetchall() == []
        assert agent_graph.build_graph(engine=engine, now=NOW) == changed
        migrations.run_migrations(path, revision=REVISION)
        assert agent_graph.build_graph(engine=engine, now=NOW) == changed
    finally:
        engine.dispose()
