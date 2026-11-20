"""The registry is an additive successor to the Live Activity schema."""

import sqlite3

from alembic import command
import pytest

from storage import migrations, web_push_service
from storage.db import create_sqlite_engine
from storage.models import live_activity_trackings


@pytest.mark.no_sqlite_template
def test_registry_upgrade_and_downgrade_preserve_live_activity_and_subscription(tmp_path):
    path = tmp_path / "state.sqlite"
    previous, registry = "20261120_0093", "20261120_0094"
    migrations.run_migrations(path, revision=previous)
    engine = create_sqlite_engine(path)
    with engine.begin() as conn:
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:account",
            device_id="phone",
            payload={
                "endpoint": "https://example.invalid/apns/phone",
                "keys": {"p256dh": "fixture", "auth": "fixture"},
            },
        )
        conn.execute(
            live_activity_trackings.insert().values(
                activity_id="activity",
                owner_user_key="remote:account",
                session_id="session",
                turn_id="turn",
                push_registration_id="r" * 43,
                activity_token="ab" * 32,
                status="tracking",
                last_phase="working",
                last_timestamp=100,
                confirmed_at=100,
                requested_at=100,
                created_at=100,
                updated_at=100,
                started_at=100,
                expires_at=200,
            )
        )
    engine.dispose()

    def snapshot(expected_revision, has_registry):
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [(expected_revision,)]
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables = {"remote_device_sessions", "remote_device_push_links", "remote_device_deadlines"}
            assert names & tables == (tables if has_registry else set())
            return (
                db.execute("SELECT * FROM live_activity_trackings").fetchall(),
                db.execute("SELECT * FROM web_push_subscriptions").fetchall(),
                db.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone(),
            )

    before = snapshot(previous, False)
    migrations.run_migrations(path, revision=registry)
    assert snapshot(registry, True) == before
    command.downgrade(migrations.alembic_config(path), previous)
    assert snapshot(previous, False) == before
