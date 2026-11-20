"""The registry is an additive successor to the Live Activity schema."""

import sqlite3

from alembic import command
import pytest

from storage import migrations, web_push_service
from storage.db import create_sqlite_engine
from storage.models import live_activity_trackings


@pytest.mark.no_sqlite_template
def test_device_functions_migration_round_trip_preserves_prior_operations(tmp_path):
    path = tmp_path / "state.sqlite"
    previous, current = "20261120_0094", "20261121_0095"
    migrations.run_migrations(path, revision=previous)
    original = ("op-old", 1, "session", "instance", "alarm", "fixture", "2030-01-01", "UTC", None, "2026-01-01", "2026-01-01")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO system_schedule_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)", original)
    migrations.run_migrations(path, revision=current)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT registry_activity_id FROM system_schedule_operations").fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM device_activity_events").fetchone() == (0,)
    command.downgrade(migrations.alembic_config(path), previous)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM system_schedule_operations").fetchall() == [original]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='device_health_links'").fetchone() is None
    migrations.run_migrations(path, revision=current)


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
