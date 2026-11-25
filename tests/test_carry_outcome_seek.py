"""The no-failure case seeks a single outcome range; it never walks successful history."""
import sqlite3
from contextlib import closing

import pytest
from alembic import command

from storage import migrations
from storage.carry_projection import _CARRY_EDGES


@pytest.mark.no_sqlite_template
def test_outcome_seek_upgrade_and_downgrade(tmp_path):
    path = tmp_path / 'state.sqlite'
    previous, revision = '20261123_0099', '20261125_0100'
    migrations.run_migrations(path, revision=previous)
    migrations.run_migrations(path, revision=revision)
    with closing(sqlite3.connect(path)) as conn:
        sql = str(_CARRY_EDGES)
        params = {'member': 'fixture', 'bound': None}
        plan = [r[-1] for r in conn.execute('EXPLAIN QUERY PLAN ' + sql, params)]
        assert any('SEARCH t USING COVERING INDEX ix_session_turns_session_outcome (session_id=? AND terminal_outcome=?)' in step for step in plan)
        assert any('SEARCH d USING INDEX ix_message_deliveries_turn' in step for step in plan)
        assert not any(step.startswith('SCAN t') or step.startswith('SCAN d') for step in plan)
    command.downgrade(migrations.alembic_config(path), previous)
    with closing(sqlite3.connect(path)) as conn:
        assert not conn.execute('pragma index_info(ix_session_turns_session_outcome)').fetchall()
