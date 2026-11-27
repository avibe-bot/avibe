"""The opt-in incident table must not rewrite tasks that predate it."""
import sqlite3
from contextlib import closing

import pytest
from alembic import command

from storage import migrations


@pytest.mark.no_sqlite_template
def test_command_failure_incident_upgrade_and_downgrade(tmp_path):
    path = tmp_path / 'state.sqlite'
    previous, revision = '20261125_0100', '20261127_0101'
    migrations.run_migrations(path, revision=previous)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("insert into run_definitions(id,definition_type,enabled,created_at,updated_at,metadata_json) "
                     "values('legacy','scheduled',1,'then','then','{}')")
        conn.commit()
    migrations.run_migrations(path, revision=revision)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('select count(*) from command_failure_incidents').fetchone() == (0,)
        conn.execute("insert into command_failure_incidents values('legacy','test','fire',1,'test',0)")
        conn.commit()
    command.downgrade(migrations.alembic_config(path), previous)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("select metadata_json from run_definitions where id='legacy'").fetchone() == ('{}',)
        assert not conn.execute('pragma table_info(command_failure_incidents)').fetchall()
