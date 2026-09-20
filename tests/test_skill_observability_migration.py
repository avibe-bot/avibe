"""Exercise the real migration chain, never a models-only substitute."""

import sqlite3

from alembic import command

from storage import migrations

import pytest


pytestmark = pytest.mark.no_sqlite_template


def test_skill_schema_upgrade_downgrade_preserves_existing_events(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision="20260821_0060")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO agent_events (id, platform, event_type, visibility, content_json, "
            "metadata_json, created_at, updated_at) VALUES (?, 'web', ?, ?, '{}', '{}', ?, ?)",
            [
                (key, kind, visibility, stamp, stamp)
                for key, kind, visibility, stamp in (
                    ("tool", "tool_call", "trace", "2026-08-01T00:00:00.000000Z"),
                    ("message", "result", "user", "2026-08-02T00:00:00Z"),
                    ("unknown", "future.event", "trace", "unparseable"),
                )
            ],
        )
        original = conn.execute("SELECT * FROM agent_events ORDER BY id").fetchall()
    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM agent_events ORDER BY id").fetchall() == original
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("20260907_0061",)
        assert conn.execute("SELECT count(*) FROM skill_usage_daily").fetchone() == (0,)
        assert {row[2] for row in conn.execute("PRAGMA foreign_key_list(skill_usage_daily)")} == {
            "scopes",
            "agent_sessions",
        }
        index_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='uq_skill_usage_daily_grain'").fetchone()[0]
        assert "coalesce(scope_id, '')" in index_sql and "coalesce(session_id, '')" in index_sql
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            "INSERT INTO agent_events (id, platform, event_type, visibility, content_json, metadata_json, "
            "created_at, updated_at) VALUES ('skill', 'web', 'skill.load_result', 'trace', '{}', '{}', 'now', 'now')"
        )
    command.downgrade(migrations.alembic_config(path), "20260821_0060")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM agent_events ORDER BY id").fetchall() == original
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='skill_usage_daily'").fetchone() is None
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='ix_agent_events_skill_created_id'").fetchone()
            is None
        )
    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM agent_events ORDER BY id").fetchall() == original
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
