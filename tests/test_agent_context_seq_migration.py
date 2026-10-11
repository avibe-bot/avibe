"""20261002_0063: ``context_seq`` on ``messages`` and ``agent_events`` (agent-core-contracts/transcript.md).

Exercises the real migration chain on a database holding released rows: they keep
every column and stay outside any model context, ``agent_events.sequence`` keeps
its released value and index, and ``context_seq`` is unique per Session in each
table.
"""

import sqlite3

import pytest
from alembic import command

from storage import migrations

pytestmark = pytest.mark.no_sqlite_template

_INDEXES = {"messages": "uq_messages_session_context_seq", "agent_events": "uq_agent_events_session_context_seq"}
# Two rows of one Session and one of another, per table.
_IDS = {"messages": ("msg_a1", "msg_a2", "msg_b1"), "agent_events": ("evt_a1", "evt_a2", "evt_b1")}


def _index_sql(conn, name):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()
    return row[0] if row else None


def test_context_seq_upgrade_and_downgrade_keep_released_rows(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision="20260923_0062")
    stamp = "2026-09-30T00:00:00.000000Z"
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO messages (id, session_id, platform, author, type, content_text, content_json, "
            "metadata_json, created_at, updated_at) VALUES (?, ?, 'slack', ?, ?, ?, '{}', '{}', ?, ?)",
            [
                ("msg_a1", "ses_a", "user", "user", "看一下目录", stamp, stamp),
                ("msg_a2", "ses_a", "agent", "result", "done", stamp, stamp),
                ("msg_b1", "ses_b", "user", "user", "hi", stamp, stamp),
            ],
        )
        conn.executemany(
            "INSERT INTO agent_events (id, session_id, turn_id, platform, event_type, visibility, sequence, "
            "content_json, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, 'turn_1', 'slack', 'tool_call', 'trace', ?, '{}', '{}', ?, ?)",
            [("evt_a1", "ses_a", 7, stamp, stamp), ("evt_a2", "ses_a", 8, stamp, stamp), ("evt_b1", "ses_b", None, stamp, stamp)],
        )
        released = {}
        for table in _INDEXES:
            cursor = conn.execute(f"SELECT * FROM {table} ORDER BY id")
            released[table] = (", ".join(column[0] for column in cursor.description), cursor.fetchall())

    def assert_released_rows_unchanged(conn):
        for table, (columns, rows) in released.items():
            assert conn.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall() == rows

    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("20261002_0063",)
        assert_released_rows_unchanged(conn)
        assert _index_sql(conn, "ix_agent_events_turn_sequence_id") is not None
        for table, (first, same_session, other_session) in _IDS.items():
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE context_seq IS NOT NULL").fetchone() == (0,)
            assert _index_sql(conn, _INDEXES[table]).lower().endswith("where context_seq is not null")
            conn.execute(f"UPDATE {table} SET context_seq = 1 WHERE id IN (?, ?)", (first, other_session))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE {table} SET context_seq = 1 WHERE id = ?", (same_session,))
        conn.commit()

    command.downgrade(migrations.alembic_config(path), "20260923_0062")
    with sqlite3.connect(path) as conn:
        assert_released_rows_unchanged(conn)
        assert [_index_sql(conn, index) for index in _INDEXES.values()] == [None, None]

    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert_released_rows_unchanged(conn)
        assert all(_index_sql(conn, index) is not None for index in _INDEXES.values())
        for table, (first, _same_session, other_session) in _IDS.items():
            assert conn.execute(
                f"SELECT id FROM {table} WHERE context_seq = 1 ORDER BY id"
            ).fetchall() == [(first,), (other_session,)]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
