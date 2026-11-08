"""Migration and query-plan contracts for ``vibe session list`` indexes."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from storage import migrations


@pytest.mark.no_sqlite_template
def test_session_list_indexes_upgrade_and_downgrade_preserve_rows(tmp_path: Path):
    path = tmp_path / "upgrade.sqlite"
    migrations.run_migrations(path, revision="20261108_0079")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agent_runs (id, run_type, status, session_id, source_kind, source_actor, "
            "created_at, updated_at, metadata_json) values "
            "('existing', 'agent_run', 'succeeded', 'execution', 'agent', 'source', "
            "'2026-11-08T00:00:00Z', '2026-11-08T00:00:00Z', '{}')"
        )
        before_rows = conn.execute("select * from agent_runs").fetchall()
        before_indexes = conn.execute(
            "select name, sql from sqlite_master where type='index' order by name"
        ).fetchall()

    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select * from agent_runs").fetchall() == before_rows
        assert [row[2] for row in conn.execute("pragma index_info(ix_agent_runs_source_session)")] == [
            "source_kind",
            "session_id",
            "source_actor",
        ]
        assert [row[2] for row in conn.execute("pragma index_info(ix_agent_sessions_status_visibility_activity)")] == [
            "status",
            "visibility",
            "last_active_at",
            "created_at",
            "id",
        ]
        assert [row[2] for row in conn.execute("pragma index_info(ix_agent_sessions_scope_status_visibility_activity)")] == [
            "scope_id",
            "status",
            "visibility",
            "last_active_at",
            "created_at",
            "id",
        ]

    command.downgrade(migrations.alembic_config(path), "20261108_0079")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select * from agent_runs").fetchall() == before_rows
        assert conn.execute(
            "select name, sql from sqlite_master where type='index' order by name"
        ).fetchall() == before_indexes
