"""The additive checkpoint index must preserve released canonical/metadata rows.

Runtime differential tests exercise selection; this test owns the distinct
schema-upgrade/rollback contract, including older readers ignoring capsules.
"""

import sqlite3

import pytest
from alembic import command

from storage import migrations

pytestmark = pytest.mark.no_sqlite_template
INDEX = "ix_agent_events_session_type_context_seq"


def test_runtime_checkpoint_index_upgrade_and_downgrade_preserve_rows(tmp_path):
    path = tmp_path / "runtime-index.sqlite"
    migrations.run_migrations(path, revision="20261108_0083")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO agent_events (id, platform, event_type, visibility, context_seq, "
            "content_json, metadata_json, created_at, updated_at) "
            "VALUES ('checkpoint', 'avibe', 'context_compaction', 'context', 1, "
            "'{\"version\":1}', '{\"other\":true,\"vibey_runtime\":{\"version\":1}}', 'now', 'now')"
        )
        before = conn.execute("SELECT * FROM agent_events WHERE id='checkpoint'").fetchone()
    migrations.run_migrations(path, revision="20261112_0084")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM agent_events WHERE id='checkpoint'").fetchone() == before
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (INDEX,)).fetchone()[0]
        assert "(session_id, event_type, context_seq)" in sql
        assert "WHERE context_seq IS NOT NULL" in sql
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("2",)
    # Same-epoch structural rollback in this hermetic fixture, not a product
    # downgrade command: older binaries normally open the additive schema as-is.
    command.downgrade(migrations.alembic_config(path), "20261108_0083")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM agent_events WHERE id='checkpoint'").fetchone() == before
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (INDEX,)).fetchone() is None
    migrations.run_migrations(path, revision="20261112_0084")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM agent_events WHERE id='checkpoint'").fetchone() == before
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
