"""The additive tracking table preserves existing persisted rows in both directions."""

import sqlite3

from alembic import command
import pytest

from storage import migrations, web_push_service
from storage.db import create_sqlite_engine
from storage.models import live_activity_trackings, metadata
from tests.migration_helpers import migration_seed_data
from tests.test_turn_last_reply_store import _seed

pytestmark = pytest.mark.no_sqlite_template


def test_tracking_migration_preserves_all_preexisting_rows(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision="20261120_0092")
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
    command.downgrade(migrations.alembic_config(path), "20261120_0092")
    assert data() == before


def test_head_shaped_unversioned_database_preserves_pending_tracking(tmp_path):
    path = tmp_path / "state.sqlite"
    engine = create_sqlite_engine(path)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(live_activity_trackings.insert().values(
            activity_id="activity-1", owner_user_key="local", session_id="session-1", turn_id="turn-1",
            push_registration_id="r" * 43, activity_token="ab" * 32, status="tracking", last_phase="working",
            last_timestamp=100, confirmed_at=100, requested_at=100, created_at=100, updated_at=100,
            expires_at=28900, started_at=100, pending_state='{"phase":"needsYou"}', retry_count=3, retry_at=1000,
        ))
    engine.dispose()
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM live_activity_trackings").fetchall()

    migrations.run_migrations(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM live_activity_trackings").fetchall() == before
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert {row[1] for row in conn.execute("PRAGMA index_list(live_activity_trackings)")} >= {
            "ix_live_activity_status", "ix_live_activity_registration",
        }
