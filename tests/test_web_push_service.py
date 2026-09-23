from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.web_push import (
    DEFAULT_WEB_PUSH_TIMEOUT_SECONDS,
    DEFAULT_WEB_PUSH_TTL_SECONDS,
    load_or_create_vapid_keys,
    send_web_push,
)
from storage import web_push_service
from storage.db import create_sqlite_engine
from storage.migrations import run_migrations


def _payload(endpoint: str = "https://push.example.test/sub/1") -> dict:
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        },
    }


def test_subscription_upsert_and_disable(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        row = web_push_service.upsert_subscription(conn, user_key="local", payload=_payload(), user_agent="ua1")
        assert row["enabled"] is True
        assert row["user_agent"] == "ua1"
        assert web_push_service.count_enabled(conn, user_key="local") == 1

        updated = web_push_service.upsert_subscription(conn, user_key="local", payload=_payload(), user_agent="ua2")
        assert updated["id"] == row["id"]
        assert updated["user_agent"] == "ua2"
        assert web_push_service.count_enabled(conn, user_key="local") == 1

        assert web_push_service.disable_subscription(
            conn,
            endpoint=_payload()["endpoint"],
            user_key="someone-else",
        ) is False
        assert web_push_service.count_enabled(conn, user_key="local") == 1

        assert web_push_service.disable_subscription(conn, endpoint=_payload()["endpoint"], user_key="local") is True
        assert web_push_service.count_enabled(conn, user_key="local") == 0


def test_explicit_disable_clears_delivery_failure_marker(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        row = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload(),
            device_id="device-1",
        )
        web_push_service.mark_send_failure(conn, endpoint=row["endpoint"])

        assert web_push_service.disable_device_subscription(
            conn,
            user_key="remote:user-a",
            device_id="device-1",
        ) is True

        disabled = web_push_service.get_by_endpoint(
            conn,
            endpoint=row["endpoint"],
            user_key="remote:user-a",
        )
        assert disabled is not None
        assert disabled["enabled"] is False
        assert disabled["last_failure_at"] is None


def test_subscription_upsert_disables_previous_endpoint_for_same_device(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        first = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/old"),
            device_id="device-1",
        )
        second = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/new"),
            device_id="device-1",
        )
        other_device = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/other"),
            device_id="device-2",
        )

        assert first["id"] != second["id"]
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=first["endpoint"],
            user_key="remote:user-a",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=second["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=other_device["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 2


def test_attach_device_to_enabled_subscription_does_not_reenable_disabled_endpoint(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        row = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/dead"),
            device_id="device-1",
        )
        web_push_service.mark_send_failure(conn, endpoint=row["endpoint"], disable=True)

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/dead"),
            device_id="device-1",
        )

        assert synced is None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 0


def test_attach_device_to_enabled_subscription_preserves_same_origin_legacy_rows(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        legacy_same_origin = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/legacy"),
        )
        current = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
        )
        other_device = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/other-device"),
            device_id="device-2",
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-1",
        )

        assert synced is not None
        assert synced["device_id"] == "device-1"
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=legacy_same_origin["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=current["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=other_device["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 3


def test_attach_device_to_enabled_subscription_disables_client_known_previous_endpoints(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
        )
        current = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
        )
        other_legacy = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/other-legacy"),
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-1",
            previous_endpoints=[previous["endpoint"]],
        )

        assert synced is not None
        assert synced["device_id"] == "device-1"
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=previous["endpoint"],
            user_key="remote:user-a",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=current["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=other_legacy["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 2


def test_attach_device_to_enabled_subscription_cleans_previous_endpoint_when_current_unknown(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-1",
            previous_endpoints=[previous["endpoint"]],
        )

        assert synced is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=previous["endpoint"],
            user_key="remote:user-a",
        ) is None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 0


def test_attach_device_to_enabled_subscription_preserves_existing_label_when_missing(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        row = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_label="iPhone",
            device_id="device-1",
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-1",
        )

        assert synced is not None
        assert synced["id"] == row["id"]
        assert synced["device_label"] == "iPhone"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, "endpoint_required"),
        ({"endpoint": "http://example.test", "keys": {"p256dh": "x", "auth": "y"}}, "endpoint_must_be_https"),
        ({"endpoint": "https://example.test", "keys": {}}, "p256dh_required"),
        ({"endpoint": "https://example.test", "keys": {"p256dh": "x"}}, "auth_required"),
    ],
)
def test_validate_subscription_payload_rejects_bad_input(payload, error):
    with pytest.raises(ValueError, match=error):
        web_push_service.validate_subscription_payload(payload)


def test_vapid_keys_are_stable(tmp_path):
    key_path = tmp_path / "web_push_vapid.json"

    first = load_or_create_vapid_keys(key_path)
    second = load_or_create_vapid_keys(key_path)

    assert first == second
    assert first.public_key
    assert "PRIVATE KEY" in first.private_key_pem
    stored = json.loads(key_path.read_text(encoding="utf-8"))
    assert stored["public_key"] == first.public_key


def test_vapid_keys_are_stable_under_concurrent_first_use(tmp_path):
    key_path = tmp_path / "web_push_vapid.json"

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: load_or_create_vapid_keys(key_path), range(16)))

    assert len({key.public_key for key in keys}) == 1
    assert len({key.private_key_pem for key in keys}) == 1
    stored = json.loads(key_path.read_text(encoding="utf-8"))
    assert stored["public_key"] == keys[0].public_key


def test_send_web_push_passes_vapid_signer_and_timeout(monkeypatch, tmp_path):
    keys = load_or_create_vapid_keys(tmp_path / "web_push_vapid.json")
    calls = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("pywebpush.webpush", fake_webpush)

    send_web_push(
        subscription={
            "endpoint": "https://push.example.test/sub/1",
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        },
        payload={"title": "Hello"},
        vapid_keys=keys,
    )

    assert len(calls) == 1
    assert calls[0]["timeout"] == DEFAULT_WEB_PUSH_TIMEOUT_SECONDS
    assert calls[0]["ttl"] == DEFAULT_WEB_PUSH_TTL_SECONDS
    assert not isinstance(calls[0]["vapid_private_key"], str)
    assert calls[0]["subscription_info"]["endpoint"] == "https://push.example.test/sub/1"
