"""The additive tracking table preserves existing persisted rows in both directions."""

import sqlite3

from alembic import command
import pytest

from storage import migrations, web_push_service
from storage.db import create_sqlite_engine
from tests.migration_helpers import migration_seed_data
from tests.test_turn_last_reply_store import _seed

pytestmark = pytest.mark.no_sqlite_template


def test_tracking_migration_preserves_all_preexisting_rows(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision="20261118_0091")
    engine = create_sqlite_engine(path)
    with engine.begin() as conn:
        _seed(conn)
        for enabled in [False, True]:
            endpoint = f"https://push.example.test/{enabled}"
            web_push_service.upsert_subscription(
                conn,
                user_key="local",
                payload={
                    "endpoint": endpoint,
                    "keys": {"p256dh": "test", "auth": "test"},
                },
            )
            if not enabled:
                web_push_service.disable_subscription(conn, endpoint=endpoint)
    engine.dispose()

    def data():
        with sqlite3.connect(path) as conn:
            return [row for row in migration_seed_data(conn) if not row.startswith('INSERT INTO "alembic_version"')]

    before = data()
    migrations.run_migrations(path)
    assert data() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM live_activity_trackings").fetchone() == (0,)
    command.downgrade(migrations.alembic_config(path), "20261118_0091")
    assert data() == before
