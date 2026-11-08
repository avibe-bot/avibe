"""The safety net starts from new accepted messages, never a historical flood.

The independent table/index migration must keep the older named-column writer
contract and be mechanically additive within the released compatibility epoch.
"""
from contextlib import closing
import sqlite3

from alembic import command
import pytest

from storage import migrations
from storage.migration_policy import classify_migration

pytestmark = pytest.mark.no_sqlite_template
PREVIOUS = "20261108_0076"
HEAD = "20261108_0082"


def test_delegation_projection_upgrade_and_fixture_downgrade_preserve_released_data(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision=PREVIOUS)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO agent_runs(id, run_type, status, created_at, updated_at, metadata_json) "
                     "VALUES ('historical', 'agent_run', 'succeeded', 'old', 'old', '{}')")
        original = conn.execute("SELECT * FROM agent_runs WHERE id='historical'").fetchone()
    migrations.run_migrations(path)
    migration = migrations.alembic_dir() / "versions" / "20261108_0079_session_delegations.py"
    assert classify_migration(migration).permitted == "additive"
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT * FROM session_delegations").fetchall() == []
        assert conn.execute("SELECT * FROM agent_runs WHERE id='historical'").fetchone() == original
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("1",)
        conn.execute("INSERT INTO session_delegations VALUES ('sesA', 'sesB', 'msg', 'run', 'notified', 'notice', 'now')")
        # An old binary updating its own columns cannot erase the new projection.
        conn.execute("UPDATE agent_runs SET updated_at='new' WHERE id='historical'")
        assert conn.execute("SELECT notice_run_id FROM session_delegations").fetchone() == ("notice",)
    # This exercises the isolated schema fixture's reverse DDL, not the product's
    # rollback API (same-epoch binary rollback keeps the newer schema as-is).
    command.downgrade(migrations.alembic_config(path), PREVIOUS)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='session_delegations'").fetchone() is None
        assert conn.execute("SELECT id FROM agent_runs").fetchall() == [("historical",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name IN "
                            "('ix_agent_runs_session_status','ix_run_definitions_session_pending')").fetchall() == []
    migrations.run_migrations(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM session_delegations").fetchall() == []
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
