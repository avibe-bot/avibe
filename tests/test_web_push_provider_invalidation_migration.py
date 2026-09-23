"""Existing disabled Web Push rows must not gain repair consent on upgrade."""

import sqlite3

import pytest

from storage import migrations, web_push_service
from storage.db import create_sqlite_engine


pytestmark = pytest.mark.no_sqlite_template


def test_upgrade_keeps_legacy_disabled_failure_opted_out(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision="20260907_0061")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            insert into web_push_subscriptions (
                id, user_key, endpoint, p256dh, auth, enabled, last_failure_at,
                failure_count, created_at, updated_at
            ) values (?, 'remote:user-a', ?, 'key', 'auth', ?, ?, 1, 'old', 'old')
            """,
            [
                (
                    "opted-out",
                    "https://push.example.test/sub/opted-out",
                    0,
                    "2026-09-01T00:00:00Z",
                ),
                (
                    "still-enabled",
                    "https://push.example.test/sub/still-enabled",
                    1,
                    "2026-09-01T00:00:00Z",
                ),
            ],
        )

    migrations.run_migrations(path)
    engine = create_sqlite_engine(path)
    with engine.begin() as conn:
        disabled = web_push_service.get_by_endpoint(
            conn,
            endpoint="https://push.example.test/sub/opted-out",
            user_key="remote:user-a",
        )
        assert disabled is not None
        assert disabled["last_failure_at"] is not None
        assert disabled["provider_invalidated_at"] is None
        assert web_push_service.upsert_background_rotated_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/sub/rotated",
                "keys": {"p256dh": "key", "auth": "auth"},
            },
            previous_endpoints=[disabled["endpoint"]],
        ) is None
        enabled = web_push_service.get_by_endpoint(
            conn,
            endpoint="https://push.example.test/sub/still-enabled",
            user_key="remote:user-a",
        )
        assert enabled is not None
        assert enabled["enabled"] is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone() == (
            "20260923_0062",
        )
