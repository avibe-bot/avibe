from __future__ import annotations

from types import SimpleNamespace

from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


def _paired_config(instance_id: str = "instance-a") -> SimpleNamespace:
    return SimpleNamespace(
        remote_access=SimpleNamespace(
            vibe_cloud=SimpleNamespace(instance_id=instance_id),
        )
    )


def test_system_schedule_claim_is_csrf_guarded_and_bound_to_the_configured_instance(
    monkeypatch,
) -> None:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "config.v2_config.V2Config.load",
        lambda: _paired_config(),
    )
    monkeypatch.setattr(
        "storage.system_schedule_service.claim_next",
        lambda **kwargs: calls.append(kwargs) or {"action": "create", "leaseToken": "lease-a"},
    )
    client = app.test_client()

    missing_csrf = client.post(
        "/api/system-schedule/claim",
        json={
            "instance_id": "instance-a",
            "device_id": "device-a",
            "exclude_lease_tokens": [],
            "allow_create": True,
        },
    )
    assert missing_csrf.status_code == 403
    assert calls == []

    mismatch = client.post(
        "/api/system-schedule/claim",
        json={
            "instance_id": "instance-b",
            "device_id": "device-a",
            "exclude_lease_tokens": [],
            "allow_create": True,
        },
        headers=csrf_headers(client),
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json() == {"ok": False, "error": "instance_mismatch"}
    assert calls == []

    response = client.post(
        "/api/system-schedule/claim",
        json={
            "instance_id": "instance-a",
            "device_id": "device-a",
            "exclude_lease_tokens": [],
            "allow_create": True,
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "claim": {"action": "create", "leaseToken": "lease-a"},
    }
    assert calls == [
        {
            "instance_id": "instance-a",
            "device_id": "device-a",
            "exclude_lease_tokens": [],
            "allow_create": True,
        }
    ]


def test_system_schedule_receipt_forwards_the_exact_device_lease(monkeypatch) -> None:
    calls: list[dict] = []
    receipt = {
        "contract": 1,
        "requestId": "request-a",
        "operationId": "operation-a",
        "kind": "alarm",
        "state": "scheduled",
        "systemIdentifier": "opaque-a",
        "fireAt": "2026-11-18T01:00:00Z",
        "timeZone": "America/Los_Angeles",
        "authorization": "authorized",
        "error": None,
    }
    monkeypatch.setattr(
        "config.v2_config.V2Config.load",
        lambda: _paired_config(),
    )
    monkeypatch.setattr(
        "storage.system_schedule_service.record_receipt",
        lambda **kwargs: calls.append(kwargs) or {
            "action": "create",
            "operation": {"operation_id": "operation-a", "state": "scheduled"},
        },
    )
    client = app.test_client()

    response = client.post(
        "/api/system-schedule/receipt",
        json={
            "instance_id": "instance-a",
            "device_id": "device-a",
            "lease_token": "lease-a",
            "receipt": receipt,
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "action": "create",
        "operation": {"operation_id": "operation-a", "state": "scheduled"},
    }
    assert calls == [
        {
            "instance_id": "instance-a",
            "device_id": "device-a",
            "lease_token": "lease-a",
            "receipt": receipt,
        }
    ]


def test_system_schedule_routes_reject_unknown_shapes_and_map_permanent_receipt_conflicts(
    monkeypatch,
) -> None:
    from storage.system_schedule_service import SystemScheduleServiceError

    monkeypatch.setattr(
        "config.v2_config.V2Config.load",
        lambda: _paired_config(),
    )
    monkeypatch.setattr(
        "storage.system_schedule_service.record_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(
            SystemScheduleServiceError("stale_receipt", "The lease expired.")
        ),
    )
    client = app.test_client()
    headers = csrf_headers(client)

    invalid_claim = client.post(
        "/api/system-schedule/claim",
        json={
            "instance_id": "instance-a",
            "device_id": "device-a",
            "exclude_lease_tokens": [],
            "allow_create": True,
            "extra": True,
        },
        headers=headers,
    )
    assert invalid_claim.status_code == 400
    assert invalid_claim.get_json() == {"ok": False, "error": "invalid_request"}

    stale = client.post(
        "/api/system-schedule/receipt",
        json={
            "instance_id": "instance-a",
            "device_id": "device-a",
            "lease_token": "lease-a",
            "receipt": {},
        },
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.get_json() == {"ok": False, "error": "stale_receipt"}


def test_system_schedule_claim_accepts_optional_kinds_with_real_storage(monkeypatch, tmp_path):
    from tests.test_system_schedule_service import NOW, FIRE_AT
    from storage.importer import ensure_sqlite_state
    from storage.system_schedule_service import create_operation
    from storage.db import create_sqlite_engine
    from tests.test_ui_session_stream import _make_session

    ensure_sqlite_state()
    _, session = _make_session(tmp_path)
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: _paired_config())
    monkeypatch.setattr("storage.system_schedule_service._utc_now", lambda: NOW)
    create_operation(session_id=session, instance_id="instance-a", operation_id="remind",
                     kind="reminder", title="Reminder", fire_at=FIRE_AT, time_zone="UTC", now=NOW)
    client = app.test_client()
    body = {"instance_id": "instance-a", "device_id": "device-a", "exclude_lease_tokens": [], "allow_create": True}
    filtered = client.post("/api/system-schedule/claim", json={**body, "kinds": ["alarm"]}, headers=csrf_headers(client))
    assert filtered.get_json() == {"ok": True, "claim": None}
    legacy = client.post("/api/system-schedule/claim", json=body, headers=csrf_headers(client))
    assert legacy.get_json()["claim"]["request"]["kind"] == "reminder"
    invalid = client.post("/api/system-schedule/claim", json={**body, "kinds": ["calendar"]}, headers=csrf_headers(client))
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "invalid_kinds"
    create_sqlite_engine().dispose()
