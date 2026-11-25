"""The carry edge index is additive, sparse, maintained by old writers and reversible."""
import sqlite3
from contextlib import closing

import pytest
from alembic import command

from storage import migrations
from storage.carry_projection import _CARRY_EDGES


@pytest.mark.no_sqlite_template
def test_carry_lookup_upgrade_plan_and_rollback(tmp_path):
    path = tmp_path / "state.sqlite"
    previous = "20261123_0099"
    revision = "20261125_0100"
    migrations.run_migrations(path, revision=previous)
    for _ in range(2):
        migrations.run_migrations(path, revision=revision)
        with closing(sqlite3.connect(path)) as conn:
            plan = [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + str(_CARRY_EDGES),
                                                 {"member": "synthetic", "bound": None})]
            assert any("SEARCH c USING INDEX ix_message_deliveries_session_carry" in step for step in plan)
            assert not any(step.startswith("SCAN d") or step.startswith("SCAN m") for step in plan)
        command.downgrade(migrations.alembic_config(path), previous)
        with closing(sqlite3.connect(path)) as conn:
            assert not conn.execute("pragma index_info(ix_message_deliveries_session_carry)").fetchall()
