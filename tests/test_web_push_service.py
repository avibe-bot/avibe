from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from core import web_push
from core.web_push import (
    DEFAULT_WEB_PUSH_TIMEOUT_SECONDS,
    DEFAULT_WEB_PUSH_TTL_SECONDS,
    MAX_NOTIFICATION_BODY_SCALARS,
    MAX_NOTIFICATION_PLAINTEXT_BYTES,
    MAX_NOTIFICATION_TAG_BYTES,
    MAX_NOTIFICATION_TITLE_SCALARS,
    load_or_create_vapid_keys,
    serialize_notification_payload,
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


def _decode_b64url_json(value: str) -> dict:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding))


def _test_badge_vapid_keys() -> web_push.VapidKeys:
    private_key = ec.derive_private_key(1, ec.SECP256R1())
    return web_push.VapidKeys(
        private_key_pem=private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
        public_key=web_push._b64url_no_padding(
            private_key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        ),
    )


def test_badge_update_fixture_relay_contract(monkeypatch, caplog):
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/web_push/badge_request_fixture.json"
        ).read_text(encoding="utf-8")
    )
    monkeypatch.setattr(web_push.time, "time", lambda: fixture["test_clock"])
    caplog.set_level(logging.INFO, logger="httpx")
    seen = []

    def relay(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": {
                    "authorization": request.headers["authorization"],
                    "content-type": request.headers["content-type"],
                },
                "body_utf8": request.content.decode("utf-8"),
            }
        )
        badge = json.loads(request.content)["badge"]
        return httpx.Response(
            201,
            request=request,
            json={
                "ok": True,
                "registration_id": fixture["registration_id"],
                "badge": badge,
                "apns_status": 200,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(relay)) as client:
        for request_fixture in fixture["requests"]:
            web_push.send_badge_update(
                subscription={
                    "endpoint": (
                        f"{fixture['aud_origin']}/api/push/apns/"
                        f"{fixture['registration_id']}"
                    )
                },
                badge=json.loads(request_fixture["body_utf8"])["badge"],
                vapid_keys=_test_badge_vapid_keys(),
                client=client,
            )

    assert seen == fixture["requests"]
    assert fixture["registration_id"] not in caplog.text
    assert "/api/push/apns/<redacted>/badge" in caplog.text
    match = re.fullmatch(
        r"vapid t=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+),"
        r"k=([A-Za-z0-9_-]+)",
        seen[0]["headers"]["authorization"],
    )
    assert match is not None
    token = match.group(1)
    header, claims_part, signature_part = token.split(".")
    claims = _decode_b64url_json(claims_part)
    assert claims["aud"] == fixture["aud_origin"]
    assert claims["exp"] == fixture["exp"]
    assert 0 < fixture["exp"] - fixture["test_clock"] <= 24 * 60 * 60
    public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(),
        base64.urlsafe_b64decode(match.group(2) + "=" * (-len(match.group(2)) % 4)),
    )
    signature = base64.urlsafe_b64decode(
        signature_part + "=" * (-len(signature_part) % 4)
    )
    public_key.verify(
        utils.encode_dss_signature(
            int.from_bytes(signature[:32], "big"),
            int.from_bytes(signature[32:], "big"),
        ),
        f"{header}.{claims_part}".encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )


def test_badge_update_rejects_non_contract_success_body(tmp_path):
    keys = load_or_create_vapid_keys(tmp_path / "vapid.json")
    registration_id = "B" * 43

    def relay(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            request=request,
            json={
                "ok": True,
                "registration_id": registration_id,
                "badge": True,
                "apns_status": 200,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(relay)) as client:
        with pytest.raises(web_push.BadgeUpdateProtocolError):
            web_push.send_badge_update(
                subscription={
                    "endpoint": f"https://avibe.bot/api/push/apns/{registration_id}"
                },
                badge=1,
                vapid_keys=keys,
                client=client,
            )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (f"https://avibe.bot/api/push/apns/{'A' * 43}", True),
        (f"https://avibe.bot/api/push/apns/{'A' * 42}", False),
        (f"https://avibe.bot/api/push/apns/{'A' * 43}?x=1", False),
        (f"https://avibe.bot.evil.test/api/push/apns/{'A' * 43}", False),
        (f"https://avibe.bot:443/api/push/apns/{'A' * 43}", False),
        (f"https://AVIBE.BOT/api/push/apns/{'A' * 43}", False),
        (f"https://push.example.test/sub/{'A' * 43}", False),
        (f"http://avibe.bot/api/push/apns/{'A' * 43}", False),
    ],
)
def test_native_relay_subscription_filter_is_strict(endpoint, expected):
    assert web_push.is_native_relay_subscription({"endpoint": endpoint}) is expected


def test_successful_badge_baseline_is_durable_and_failure_does_not_advance_it(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        row = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload(f"https://avibe.bot/api/push/apns/{'B' * 43}"),
        )
        assert web_push_service.get_last_successful_badge(
            conn, endpoint=row["endpoint"]
        ) is None
        web_push_service.mark_send_success(
            conn,
            endpoint=row["endpoint"],
            badge_count=0,
        )
        assert web_push_service.get_last_successful_badge(
            conn, endpoint=row["endpoint"]
        ) == 0
        web_push_service.mark_send_failure(conn, endpoint=row["endpoint"])
        assert web_push_service.get_last_successful_badge(
            conn, endpoint=row["endpoint"]
        ) == 0
        web_push_service.mark_send_success(
            conn,
            endpoint=row["endpoint"],
            badge_count=3,
        )
        assert web_push_service.get_last_successful_badge(
            conn, endpoint=row["endpoint"]
        ) == 3

    engine.dispose()


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
        web_push_service.mark_send_failure(conn, endpoint=row["endpoint"], disable=True)

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
        assert disabled["provider_invalidated_at"] is None


def test_late_provider_failure_does_not_undo_logout_opt_out(tmp_path):
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
        assert web_push_service.disable_device_subscription(
            conn,
            user_key="remote:user-a",
            device_id="device-1",
        )
        web_push_service.mark_send_failure(conn, endpoint=row["endpoint"], disable=True)
        disabled = web_push_service.get_by_endpoint(
            conn,
            endpoint=row["endpoint"],
            user_key="remote:user-a",
        )
        assert disabled is not None
        assert disabled["enabled"] is False
        assert disabled["provider_invalidated_at"] is None


def test_late_send_success_preserves_provider_invalidation(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        row = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload(),
        )
        web_push_service.mark_send_failure(conn, endpoint=row["endpoint"], disable=True)
        invalidated = web_push_service.get_by_endpoint(
            conn, endpoint=row["endpoint"], user_key="remote:user-a"
        )
        assert invalidated is not None
        assert invalidated["provider_invalidated_at"] is not None
        web_push_service.mark_send_success(conn, endpoint=row["endpoint"])
        after_success = web_push_service.get_by_endpoint(
            conn, endpoint=row["endpoint"], user_key="remote:user-a"
        )
        assert after_success is not None
        assert after_success["enabled"] is False
        assert after_success["provider_invalidated_at"] == invalidated["provider_invalidated_at"]
        assert after_success["last_failure_at"] == invalidated["last_failure_at"]


@pytest.mark.parametrize("submitted_device_id", ["device-1", "stale-device"])
def test_logout_disables_rotated_endpoint_when_old_endpoint_was_submitted(
    tmp_path, submitted_device_id
):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/old"),
            device_id="device-1",
        )
        rotated = web_push_service.upsert_background_rotated_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/new"),
            previous_endpoints=[previous["endpoint"]],
        )
        assert rotated is not None
        assert rotated["device_id"] == "device-1"
        other_device = web_push_service.upsert_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/other"),
            device_id="device-2",
        )
        other_user = web_push_service.upsert_subscription(
            conn, user_key="remote:user-b", payload=_payload("https://push.example.test/sub/user-b"),
            device_id="device-1",
        )

        assert web_push_service.disable_device_subscription(
            conn, user_key="remote:user-a", endpoint=previous["endpoint"],
            device_id=submitted_device_id,
        )
        assert web_push_service.get_enabled_by_endpoint(
            conn, endpoint=rotated["endpoint"], user_key="remote:user-a",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn, endpoint=other_device["endpoint"], user_key="remote:user-a",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn, endpoint=other_user["endpoint"], user_key="remote:user-b",
        ) is not None


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


def test_background_rotation_requires_active_or_provider_failed_previous(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
            device_id="device-1",
        )
        current = _payload("https://push.example.test/sub/current")

        accepted = web_push_service.upsert_background_rotated_subscription(
            conn,
            user_key="remote:user-a",
            payload=current,
            previous_endpoints=[previous["endpoint"]],
        )
        assert accepted is not None
        assert accepted["endpoint"] == current["endpoint"]
        assert accepted["device_id"] == "device-1"

        web_push_service.disable_subscription(
            conn,
            endpoint=accepted["endpoint"],
            user_key="remote:user-a",
        )
        rejected = web_push_service.upsert_background_rotated_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/next"),
            previous_endpoints=[accepted["endpoint"]],
        )
        assert rejected is None


def test_background_rotation_accepts_provider_failed_previous(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
            device_id="device-1",
        )
        web_push_service.mark_send_failure(
            conn,
            endpoint=previous["endpoint"],
            disable=True,
        )

        accepted = web_push_service.upsert_background_rotated_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            previous_endpoints=[previous["endpoint"]],
        )

        assert accepted is not None
        assert accepted["endpoint"] == "https://push.example.test/sub/current"
        assert accepted["device_id"] == "device-1"
        consumed = web_push_service.get_by_endpoint(
            conn, endpoint=previous["endpoint"], user_key="remote:user-a",
        )
        assert consumed is not None
        assert consumed["provider_invalidated_at"] is None


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


def test_attach_device_to_enabled_subscription_recovers_unknown_rotated_endpoint(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
            device_id="device-1",
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-1",
            previous_endpoints=[previous["endpoint"]],
        )

        assert synced is not None
        assert synced["endpoint"] == "https://push.example.test/sub/current"
        assert synced["device_id"] == "device-1"
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=previous["endpoint"],
            user_key="remote:user-a",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=synced["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 1


def test_attach_device_to_enabled_subscription_recovers_after_provider_disabled_previous(
    tmp_path,
):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
            device_id="device-1",
        )
        web_push_service.mark_send_failure(
            conn,
            endpoint=previous["endpoint"],
            disable=True,
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-1",
            previous_endpoints=[previous["endpoint"]],
        )

        assert synced is not None
        assert synced["endpoint"] == "https://push.example.test/sub/current"
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=synced["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 1


@pytest.mark.parametrize("repair_mode", ["foreground", "background"])
def test_repair_consumes_previous_invalidation_before_later_logout(tmp_path, repair_mode):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/a"),
            device_id="device-1",
        )
        web_push_service.mark_send_failure(conn, endpoint=previous["endpoint"], disable=True)
        replacement = _payload("https://push.example.test/sub/b")
        if repair_mode == "foreground":
            repaired = web_push_service.attach_device_to_enabled_subscription(
                conn, user_key="remote:user-a", payload=replacement, device_id="device-1",
                previous_endpoints=[previous["endpoint"]],
            )
        else:
            repaired = web_push_service.upsert_background_rotated_subscription(
                conn, user_key="remote:user-a", payload=replacement,
                previous_endpoints=[previous["endpoint"]],
            )
        assert repaired is not None
        consumed = web_push_service.get_by_endpoint(
            conn, endpoint=previous["endpoint"], user_key="remote:user-a",
        )
        assert consumed is not None
        assert consumed["provider_invalidated_at"] is None
        assert web_push_service.disable_device_subscription(
            conn, user_key="remote:user-a", device_id="device-1",
        )
        assert web_push_service.attach_device_to_enabled_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/c"),
            device_id="device-1", previous_endpoints=[repaired["endpoint"], previous["endpoint"]],
        ) is None
        assert web_push_service.upsert_background_rotated_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/c"),
            previous_endpoints=[repaired["endpoint"], previous["endpoint"]],
        ) is None


def test_attach_device_to_enabled_subscription_does_not_cross_devices(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/previous"),
            device_id="device-1",
        )

        synced = web_push_service.attach_device_to_enabled_subscription(
            conn,
            user_key="remote:user-a",
            payload=_payload("https://push.example.test/sub/current"),
            device_id="device-2",
            previous_endpoints=[previous["endpoint"]],
        )

        assert synced is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=previous["endpoint"],
            user_key="remote:user-a",
        ) is not None
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 1


def test_failed_foreground_reconciliation_preserves_invalidation(tmp_path):
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)

    with engine.begin() as conn:
        previous = web_push_service.upsert_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/previous"),
            device_id="device-1",
        )
        web_push_service.mark_send_failure(conn, endpoint=previous["endpoint"], disable=True)

        assert web_push_service.attach_device_to_enabled_subscription(
            conn, user_key="remote:user-a", payload=_payload("https://push.example.test/sub/current"),
            device_id="device-2", previous_endpoints=[previous["endpoint"]],
        ) is None
        remaining = web_push_service.get_by_endpoint(
            conn, endpoint=previous["endpoint"], user_key="remote:user-a",
        )
        assert remaining is not None
        assert remaining["provider_invalidated_at"] is not None


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


def test_notification_serializer_preserves_unicode_and_shared_limits():
    payload = {
        "title": "任务完成" * 100,
        "body": "已完成：你好，世界 🌏" * 100,
        "url": "/chat/ses_123?msg=msg_456&from=notification",
        "tag": "session:ses_123" * 20,
        "badge_count": 3,
        "category": "sessions",
    }

    serialized = serialize_notification_payload(payload)
    decoded = json.loads(serialized)

    assert len(decoded["title"]) <= MAX_NOTIFICATION_TITLE_SCALARS
    assert len(decoded["body"]) <= MAX_NOTIFICATION_BODY_SCALARS
    assert len(decoded["tag"].encode("utf-8")) <= MAX_NOTIFICATION_TAG_BYTES
    assert "任务完成" in decoded["title"]
    assert "🌏" in decoded["body"]
    assert len(serialized.encode("utf-8")) <= MAX_NOTIFICATION_PLAINTEXT_BYTES
    assert "\\u" not in serialized


def test_notification_serializer_rejects_control_characters_in_tag():
    with pytest.raises(ValueError, match="notification_tag_contains_control_character"):
        serialize_notification_payload({"title": "x", "body": "y", "tag": "x\n"})


def test_notification_serializer_rejects_unbounded_non_text_fields():
    with pytest.raises(ValueError, match="notification_payload_too_large"):
        serialize_notification_payload(
            {
                "title": "",
                "body": "",
                "tag": "x",
                "extra": "x" * (MAX_NOTIFICATION_PLAINTEXT_BYTES + 1),
            }
        )
