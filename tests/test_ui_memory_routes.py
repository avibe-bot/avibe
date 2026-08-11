from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import httpx
import pytest

from config.v2_config import (
    AgentsConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from core.memory.operation_lock import MemoryOperationLease
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, internal_client, remote_access, ui_memory_routes, ui_server
from vibe.ui_server import app


def _save_config(tmp_path) -> None:
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()


def _save_memory(memory: MemoryConfig) -> V2Config:
    from config.v2_config import memory_config_to_payload

    return api.save_memory_config(
        memory_config_to_payload(memory, include_secrets=True),
        recovery_intent=memory.recovery_intent,
    )


def _save_remote_config(tmp_path) -> V2Config:
    _save_config(tmp_path)
    config = V2Config.load()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.public_url = "https://alex.avibe.bot"
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    config.save()
    return config


def _renewable_remote_session_cookie(
    config: V2Config,
    email: str,
    subject: str,
) -> str:
    expires_at = int(time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    payload = {
        "email": email,
        "sub": subject,
        "instance_id": config.remote_access.vibe_cloud.instance_id,
        "iat": expires_at - remote_access.SESSION_TTL_SECONDS,
        "exp": expires_at,
    }
    payload_text = urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe="")
    signature = remote_access._session_signature(
        config.remote_access.vibe_cloud.session_secret,
        payload_text,
    )
    return f"{payload_text}.{signature}"


def _local_headers() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1:15131"}


def test_memory_settings_are_direct_loopback_only_and_write_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()

    response = client.get(
        "/api/memory/settings",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.get_json()["processing"]["llm"]["api_key"] is None
    assert response.get_json()["processing"]["llm"]["has_api_key"] is False
    assert response.get_json()["rebuild_required"] is False
    assert "recovery_intent" not in response.get_json()
    assert "embedding_change_pending" not in response.get_json()
    assert "diagnostics" not in response.get_json()


def test_memory_settings_get_accepts_same_origin_referer_without_origin(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/api/memory/settings",
        headers={"Referer": "http://127.0.0.1:15131/settings"},
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_memory_direct_loopback_predicate_rejects_forwarding(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_REMOTE_TRUSTED_PROXY_IPS", "127.0.0.1")
    with app.test_request_context(
        "/api/memory/status",
        base_url="http://127.0.0.1:15131",
        headers={
            "Origin": "http://127.0.0.1:15131",
            "X-Forwarded-Host": "127.0.0.1:15131",
        },
    ):
        assert ui_server.is_direct_loopback_memory_request() is False


def test_memory_authenticated_avibe_cloud_uses_the_remote_workbench_principal(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_config(tmp_path)
    calls: list[tuple[str, str]] = []

    async def profile(*, user_key: str):
        calls.append(("profile", user_key))
        return {"status_code": 200, "body": {"status": "ok", "items": []}}

    async def clear(*, user_key: str):
        calls.append(("clear", user_key))
        return {"status_code": 200, "body": {"status": "completed", "epoch": 2}}

    async def failures(*, user_key: str):
        calls.append(("failures", user_key))
        return {
            "status_code": 200,
            "body": {"status": "ok", "items": [], "recovery": None},
        }

    async def maintenance(*, user_key: str):
        calls.append(("maintenance", user_key))
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "data_exists": False,
                "can_clear": True,
                "clear_recovery": None,
            },
        }

    monkeypatch.setattr(internal_client, "memory_profile", profile)
    monkeypatch.setattr(internal_client, "memory_clear", clear)
    monkeypatch.setattr(internal_client, "memory_failures", failures)
    monkeypatch.setattr(internal_client, "memory_maintenance", maintenance)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    remote_headers = {"Origin": "https://alex.avibe.bot"}

    settings_response = client.get(
        "/api/memory/settings",
        headers=remote_headers,
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    profile_response = client.get(
        "/api/memory/profile",
        headers=remote_headers,
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    failures_response = client.get(
        "/api/memory/failures",
        headers=remote_headers,
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    maintenance_response = client.get(
        "/api/memory/maintenance",
        headers=remote_headers,
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    clear_response = client.post(
        "/api/memory/clear",
        json={"confirm": True},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert settings_response.status_code == 200
    assert "diagnostics" not in settings_response.get_json()
    assert profile_response.status_code == 200
    assert failures_response.status_code == 200
    assert maintenance_response.status_code == 200
    assert clear_response.status_code == 200
    assert calls == [
        ("profile", "avibe:remote:user-1"),
        ("failures", "avibe:remote:user-1"),
        ("maintenance", "avibe:remote:user-1"),
        ("clear", "avibe:remote:user-1"),
    ]


def test_memory_diagnostics_patch_is_rejected_for_remote_ui(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    calls: list[bool] = []

    async def reconcile():
        calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    response = client.patch(
        "/api/memory/settings",
        json={"enabled": False, "diagnostics": {"log_provider_calls": True}},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"status": "failed", "error": "memory_invalid_input"}
    assert calls == []
    assert V2Config.load().memory.diagnostics.log_provider_calls is True


def test_memory_diagnostics_patch_is_rejected_for_local_ui(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[bool] = []

    async def reconcile():
        calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"diagnostics": {"log_provider_calls": True}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"status": "failed", "error": "memory_invalid_input"}
    assert calls == []
    assert V2Config.load().memory.diagnostics.log_provider_calls is True


def test_memory_avibe_cloud_read_still_requires_same_origin(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    missing_origin = client.get(
        "/api/memory/settings",
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    cross_origin = client.get(
        "/api/memory/settings",
        headers={"Origin": "https://attacker.example"},
        base_url="https://alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert missing_origin.status_code == 403
    assert missing_origin.get_json() == {"status": "failed", "error": "memory_disabled"}
    assert cross_origin.status_code == 403
    assert cross_origin.get_json() == {"status": "failed", "error": "memory_disabled"}


def test_memory_status_proxies_controller_over_uds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def status():
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "source": {
                    "status": "unavailable",
                    "observed_at": None,
                    "reason": "memory_disabled",
                },
                "health": None,
            },
        }

    monkeypatch.setattr(internal_client, "memory_status", status)
    response = app.test_client().get(
        "/api/memory/status",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "source": {
            "status": "unavailable",
            "observed_at": None,
            "reason": "memory_disabled",
        },
        "health": None,
    }
    assert response.headers["cache-control"] == "no-store"


def test_memory_processing_record_proxies_signed_operator_and_composite_payload(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    user_keys: list[str] = []

    async def processing_record(*, user_key: str):
        user_keys.append(user_key)
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "runtime": {
                    "source": {
                        "status": "unavailable",
                        "observed_at": None,
                        "reason": "memory_disabled",
                    },
                    "health": None,
                },
                "sources": {
                    "everos": {"status": "available", "observed_at": "now", "reason": None},
                    "capture": {"status": "unavailable", "observed_at": None, "reason": "busy"},
                    "calls": {"status": "available", "observed_at": "now", "reason": None},
                },
                "anomalies": {
                    "source": {"status": "available", "observed_at": "now", "reason": None},
                    "items": [],
                },
                "maintenance": {
                    "source": {"status": "available", "observed_at": "now", "reason": None},
                    "data_exists": False,
                    "can_clear": True,
                    "clear_recovery": None,
                },
            },
        }

    monkeypatch.setattr(internal_client, "memory_processing_record", processing_record)
    response = app.test_client().get(
        "/api/memory/processing-record",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["sources"]["capture"] == {
        "status": "unavailable",
        "observed_at": None,
        "reason": "busy",
    }
    assert response.headers["cache-control"] == "no-store"
    assert user_keys == ["avibe:local"]


def test_memory_maintenance_proxies_the_local_clear_facts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    user_keys: list[str] = []

    async def maintenance(*, user_key: str):
        user_keys.append(user_key)
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "data_exists": True,
                "can_clear": False,
                "clear_recovery": {
                    "operation_id": "clear-42",
                    "state": "recovery_needed",
                    "can_resume": False,
                    "can_abort": True,
                },
            },
        }

    monkeypatch.setattr(internal_client, "memory_maintenance", maintenance)
    response = app.test_client().get(
        "/api/memory/maintenance",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["can_clear"] is False
    assert response.get_json()["clear_recovery"]["operation_id"] == "clear-42"
    assert response.get_json()["clear_recovery"]["can_resume"] is False
    assert response.headers["cache-control"] == "no-store"
    assert user_keys == ["avibe:local"]


@pytest.mark.parametrize("can_clear", (False, True))
def test_memory_maintenance_preserves_the_runtime_clear_capability(
    monkeypatch,
    tmp_path,
    can_clear: bool,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def maintenance(*, user_key: str):
        assert user_key == "avibe:local"
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "data_exists": False,
                "can_clear": can_clear,
                "clear_recovery": None,
            },
        }

    monkeypatch.setattr(internal_client, "memory_maintenance", maintenance)
    response = app.test_client().get(
        "/api/memory/maintenance",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["can_clear"] is can_clear


def test_memory_failures_proxy_is_direct_loopback_only_and_no_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    user_keys: list[str] = []

    async def failures(*, user_key: str):
        user_keys.append(user_key)
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "items": [
                    {
                        "id": f"ma_{'5' * 64}",
                        "kind": "delivery_abandoned",
                        "state": "manual_required",
                        "operation": "add",
                        "occurred_at": "2026-01-01T00:00:00.000Z",
                        "error_code": "memory_provider_timeout",
                        "request_id": None,
                        "attempts": 3,
                        "generation": 1,
                    }
                ],
                "recovery": None,
            },
        }

    monkeypatch.setattr(internal_client, "memory_failures", failures)
    client = app.test_client()
    response = client.get(
        "/api/memory/failures",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    forwarded = client.get(
        "/api/memory/failures",
        headers={**_local_headers(), "X-Forwarded-Host": "127.0.0.1:15131"},
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["items"][0] == {
        "id": f"ma_{'5' * 64}",
        "kind": "delivery_abandoned",
        "state": "manual_required",
        "operation": "add",
        "occurred_at": "2026-01-01T00:00:00.000Z",
        "error_code": "memory_provider_timeout",
        "request_id": None,
        "attempts": 3,
        "generation": 1,
    }
    assert response.headers["cache-control"] == "no-store"
    assert forwarded.status_code == 403
    assert user_keys == ["avibe:local"]


def test_memory_search_requires_csrf_and_only_forwards_query_and_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[tuple[str, dict[str, object], str]] = []

    async def search(query: str, policy: dict[str, object], *, user_key: str):
        calls.append((query, policy, user_key))
        return {"status_code": 200, "body": {"status": "ok", "items": [], "warnings": []}}

    monkeypatch.setattr(internal_client, "memory_search", search)
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    response = client.post(
        "/api/memory/search",
        json={
            "query": "find this",
            "policy": {
                "mode": "keyword",
                "max_results": 3,
                "include_profile": False,
                "include_current_session": False,
            },
        },
        headers=headers,
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert calls == [
        (
            "find this",
            {
                "mode": "keyword",
                "max_results": 3,
                "include_profile": False,
                "include_current_session": False,
            },
            "avibe:local",
        )
    ]
    assert response.headers["cache-control"] == "no-store"


def test_memory_log_routes_forward_only_valid_query_and_are_no_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[tuple[object, ...]] = []

    async def memory_log(*, cursor: str | None, limit: int, user_key: str):
        calls.append(("list", cursor, limit, user_key))
        return {
            "status_code": 200,
            "body": {"status": "ok", "entries": [], "next_cursor": None},
        }

    async def memory_log_entry(memcell_id: str, *, user_key: str):
        calls.append(("detail", memcell_id, user_key))
        return {
            "status_code": 200,
            "body": {"status": "ok", "entry": {"memcell_id": memcell_id}},
        }

    monkeypatch.setattr(internal_client, "memory_log", memory_log)
    monkeypatch.setattr(internal_client, "memory_log_entry", memory_log_entry)
    client = app.test_client()
    request_options = {
        "headers": _local_headers(),
        "base_url": "http://127.0.0.1:15131",
        "environ_base": {"REMOTE_ADDR": "127.0.0.1"},
    }

    listed = client.get("/api/memory/log?cursor=opaque_cursor&limit=17", **request_options)
    detail = client.get("/api/memory/log/entry?memcell_id=mc_1", **request_options)
    duplicate = client.get("/api/memory/log?limit=1&limit=2", **request_options)
    invalid_id = client.get("/api/memory/log/entry?memcell_id=../secret", **request_options)

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert detail.headers["cache-control"] == "no-store"
    assert calls == [
        ("list", "opaque_cursor", 17, "avibe:local"),
        ("detail", "mc_1", "avibe:local"),
    ]
    for response in (duplicate, invalid_id):
        assert response.status_code == 400
        assert response.get_json() == {"status": "failed", "error": "memory_invalid_input"}


def test_memory_settings_enable_reconciles_through_controller(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load().memory
    current.processing.embedding = MemoryEndpointConfig(
        "https://embed.example.test/v1",
        "embed",
        None,
    )
    _save_memory(current)

    def direct_probe_must_not_run(*_args, **_kwargs):
        raise AssertionError("UI settings route must not probe provider credentials directly")

    async def reconcile():
        return {"status_code": 200, "body": {"ok": True, "state": "ready"}}

    async def status():
        return {"status_code": 200, "body": {"state": "disabled", "data_exists": False}}

    monkeypatch.setattr("core.memory.everos.EverOSPort.processing_healthy", direct_probe_must_not_run)
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    monkeypatch.setattr(internal_client, "memory_status", status)
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    response = client.patch(
        "/api/memory/settings",
        json={
            "enabled": True,
            "processing": {
                "llm": {"base_url": "https://llm.example.test/v1", "model": "chat", "api_key": "llm-key"},
                "embedding": {
                    "base_url": "https://embed.example.test/v1",
                    "model": "embed",
                    "api_key": "embedding-key",
                },
            },
        },
        headers=headers,
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["enabled"] is True
    assert body["processing"]["llm"]["api_key"] is None
    assert body["processing"]["llm"]["has_api_key"] is True
    assert body["runtime"] == {"ok": True, "state": "ready"}


def test_memory_settings_patch_rejects_the_retired_proactive_capture_field(monkeypatch, tmp_path) -> None:
    """The opt-in flag is gone; a stale client sending it must fail loudly."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    def reconcile_must_not_run(*_args, **_kwargs):
        raise AssertionError("an invalid patch must be rejected before it is persisted")

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile_must_not_run)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"proactive_capture": True},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"status": "failed", "error": "memory_invalid_input"}


def test_memory_enable_rolls_back_when_live_sidecar_reconciliation_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load().memory
    current.processing.embedding = MemoryEndpointConfig(
        "https://embed.example.test/v1",
        "embed",
        None,
    )
    _save_memory(current)

    async def status():
        return {"status_code": 200, "body": {"state": "disabled", "data_exists": False}}

    calls: list[bool] = []

    async def reconcile():
        calls.append(True)
        return {"status_code": 200, "body": {"ok": False, "error": "memory_sidecar_unavailable"}}

    monkeypatch.setattr(internal_client, "memory_status", status)
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "enabled": True,
            "processing": {
                "llm": {"base_url": "https://llm.example.test/v1", "model": "chat", "api_key": "llm-key"},
                "embedding": {
                    "base_url": "https://embed.example.test/v1",
                    "model": "embed",
                    "api_key": "embed-key",
                },
            },
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert response.get_json() == {"status": "failed", "error": "memory_sidecar_unavailable"}
    assert calls == [True, True]
    assert V2Config.load().memory.enabled is False


def test_memory_api_key_only_save_rolls_back_when_reconciliation_fails(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load().memory
    current.processing.llm = MemoryEndpointConfig(
        "https://llm.example.test/v1",
        "chat",
        "old-key",
    )
    _save_memory(current)
    observed_keys: list[str | None] = []

    async def reconcile():
        observed_keys.append(V2Config.load().memory.processing.llm.api_key)
        if len(observed_keys) == 1:
            return {
                "status_code": 200,
                "body": {"ok": False, "error": "memory_sidecar_unavailable"},
            }
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"llm": {"api_key": "new-key"}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_sidecar_unavailable",
    }
    assert observed_keys == ["new-key", "old-key"]
    assert V2Config.load().memory.processing.llm.api_key == "old-key"


def test_memory_embedding_identity_change_requires_confirm_and_saves_nothing(
    monkeypatch,
    tmp_path,
) -> None:
    """Scenario: MEMORY-REBUILD-101"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(current.memory)
    from config.paths import get_config_path

    config_before = get_config_path().read_bytes()
    calls: list[bool] = []

    async def rebuild(*, user_key: str):
        calls.append(True)
        assert user_key
        return {"status_code": 200, "body": {"ok": True, "result": "completed_empty"}}

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"embedding": {"model": "embed-v2"}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_embedding_rebuild_required",
    }
    assert calls == []
    assert get_config_path().read_bytes() == config_before
    assert V2Config.load().memory.processing.embedding.model == "embed-v1"
    assert V2Config.load().memory.recovery_intent is None


def test_memory_first_embedding_identity_requires_confirm_and_saves_nothing(
    monkeypatch,
    tmp_path,
) -> None:
    """Scenario: MEMORY-REBUILD-101"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    from config.paths import get_config_path

    config_before = get_config_path().read_bytes()
    calls: list[str] = []

    async def reconcile():
        calls.append("reconcile")
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    async def rebuild(*, user_key: str):
        calls.append(f"rebuild:{user_key}")
        return {
            "status_code": 200,
            "body": {"ok": True, "result": "completed_empty"},
        }

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {
                "embedding": {
                    "base_url": "https://embed.example.test/v1",
                    "model": "embed-v1",
                }
            }
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_embedding_rebuild_required",
    }
    assert calls == []
    assert get_config_path().read_bytes() == config_before
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.base_url is None
    assert persisted.processing.embedding.model is None
    assert persisted.recovery_intent is None


def test_memory_confirmed_first_embedding_identity_runs_retained_rebuild(
    monkeypatch,
    tmp_path,
) -> None:
    """Scenario: MEMORY-REBUILD-001"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    observed: list[tuple[str | None, str | None, str | None]] = []

    async def rebuild(*, user_key: str):
        persisted = V2Config.load().memory
        observed.append(
            (
                persisted.processing.embedding.base_url,
                persisted.processing.embedding.model,
                persisted.recovery_intent,
            )
        )
        assert user_key == "avibe:local"
        persisted.recovery_intent = None
        _save_memory(persisted)
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "result": "completed_empty",
                "state": "disabled",
            },
        }

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {
                "embedding": {
                    "base_url": "https://embed.example.test/v1",
                    "model": "embed-v1",
                }
            },
            "confirm_rebuild": True,
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["runtime"] == {
        "ok": True,
        "result": "completed_empty",
        "state": "disabled",
    }
    assert observed == [
        (
            "https://embed.example.test/v1",
            "embed-v1",
            "rebuild",
        )
    ]
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.base_url == "https://embed.example.test/v1"
    assert persisted.processing.embedding.model == "embed-v1"
    assert persisted.recovery_intent is None


def test_memory_first_embedding_api_key_only_does_not_require_rebuild(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[str] = []

    async def reconcile():
        calls.append("reconcile")
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    async def rebuild(*, user_key: str):
        calls.append(f"rebuild:{user_key}")
        return {
            "status_code": 200,
            "body": {"ok": True, "result": "completed_empty"},
        }

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"embedding": {"api_key": "embed-key"}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["processing"]["embedding"] == {
        "base_url": None,
        "model": None,
        "api_key": None,
        "has_api_key": True,
    }
    assert calls == ["reconcile"]
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.api_key == "embed-key"
    assert persisted.recovery_intent is None


def test_memory_confirmed_embedding_change_persists_marker_and_awaits_rebuild(
    monkeypatch,
    tmp_path,
) -> None:
    """Scenario: MEMORY-REBUILD-001"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(current.memory)
    observed: list[tuple[str | None, str | None]] = []

    async def rebuild(*, user_key: str):
        assert user_key
        persisted = V2Config.load().memory
        observed.append((persisted.processing.embedding.model, persisted.recovery_intent))
        persisted.recovery_intent = None
        _save_memory(persisted)
        return {
            "status_code": 200,
            "body": {"ok": True, "result": "completed_empty", "state": "disabled"},
        }

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {"embedding": {"model": "embed-v2"}},
            "confirm_rebuild": True,
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["rebuild_required"] is False
    assert observed == [("embed-v2", "rebuild")]
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.model == "embed-v2"
    assert persisted.recovery_intent is None


def test_memory_confirmed_rebuild_failure_keeps_candidate(monkeypatch, tmp_path) -> None:
    """Scenario: MEMORY-REBUILD-201"""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(current.memory)

    async def rebuild(*, user_key: str):
        assert user_key
        return {
            "status_code": 409,
            "body": {
                "ok": False,
                "error": "memory_rebuild_root_busy",
                "result": "root_busy",
            },
        }

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {"embedding": {"model": "embed-v2"}},
            "confirm_rebuild": True,
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["error"] == "memory_rebuild_root_busy"
    assert body["rebuild_required"] is True
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.model == "embed-v2"
    assert persisted.recovery_intent == "rebuild"


def test_memory_confirmed_identity_correction_replaces_pending_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=False,
        recovery_intent="rebuild",
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig(
                "https://invalid-embed.example.test/v1",
                "embed-v1",
                "embed-key",
            ),
        ),
    )
    _save_memory(current.memory)
    rebuild_calls: list[bool] = []
    reconcile_calls: list[bool] = []

    async def rebuild(*, user_key: str):
        assert user_key
        rebuild_calls.append(True)
        candidate = V2Config.load().memory
        assert candidate.processing.embedding.base_url == (
            "https://corrected-embed.example.test/v1"
        )
        assert candidate.recovery_intent == "rebuild"
        return {
            "status_code": 409,
            "body": {
                "ok": False,
                "error": "memory_rebuild_failed",
                "result": "failed",
            },
        }

    async def reconcile():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {
                "embedding": {
                    "base_url": "https://corrected-embed.example.test/v1",
                },
            },
            "confirm_rebuild": True,
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["error"] == "memory_rebuild_failed"
    assert body["runtime"] == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    assert body["rebuild_required"] is True
    assert rebuild_calls == [True]
    assert reconcile_calls == []
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.base_url == (
        "https://corrected-embed.example.test/v1"
    )
    assert persisted.recovery_intent == "rebuild"


def test_memory_api_key_only_under_pending_marker_does_not_reconcile(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=False,
        recovery_intent="rebuild",
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(current.memory)
    rebuild_calls: list[bool] = []
    reconcile_calls: list[bool] = []

    async def rebuild(*, user_key: str):
        assert user_key
        rebuild_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "result": "completed"}}

    async def reconcile():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"embedding": {"api_key": "embed-key-2"}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["rebuild_required"] is True
    assert rebuild_calls == []
    assert reconcile_calls == []
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.api_key == "embed-key-2"
    assert persisted.recovery_intent == "rebuild"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "chat-v2"),
        ("base_url", "https://llm-v2.example.test/v1"),
    ],
)
def test_memory_llm_change_under_pending_marker_still_reconciles(
    monkeypatch,
    tmp_path,
    field: str,
    value: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load().memory
    current.recovery_intent = "rebuild"
    current.processing.llm = MemoryEndpointConfig(
        "https://llm.example.test/v1",
        "chat-v1",
        "llm-key",
    )
    current.processing.embedding = MemoryEndpointConfig(
        "https://embed.example.test/v1",
        "embed-v1",
        "embed-key",
    )
    _save_memory(current)
    reconcile_calls: list[bool] = []

    async def reconcile():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"llm": {field: value}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert reconcile_calls == [True]
    persisted = V2Config.load().memory
    assert getattr(persisted.processing.llm, field) == value
    assert persisted.recovery_intent == "rebuild"


def test_memory_runtime_rebuild_requires_exact_confirm(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[bool] = []

    async def rebuild(*, user_key: str):
        calls.append(True)
        assert user_key
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "result": "completed",
                "job_id": "must-not-leak",
                "api_key": "must-not-leak",
            },
        }

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.post(
        "/api/memory/runtime/rebuild",
        json={},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"status": "failed", "error": "memory_invalid_input"}
    assert calls == []

    ok = client.post(
        "/api/memory/runtime/rebuild",
        json={"confirm": True},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert ok.status_code == 200
    assert ok.get_json() == {"ok": True, "result": "completed"}
    assert calls == [True]


@pytest.mark.parametrize(
    ("internal_status", "internal_body", "expected_status", "expected_body"),
    [
        (
            202,
            {"ok": True, "result": "completed"},
            503,
            {"ok": False, "error": "memory_rebuild_failed", "result": "failed"},
        ),
        (
            200,
            {"ok": True},
            503,
            {"ok": False, "error": "memory_rebuild_failed", "result": "failed"},
        ),
        (
            200,
            {
                "ok": False,
                "error": "memory_rebuild_root_busy",
                "result": "root_busy",
                "state": "disabled",
                "job_id": "must-not-leak",
                "api_key": "must-not-leak",
            },
            409,
            {
                "ok": False,
                "error": "memory_rebuild_root_busy",
                "result": "root_busy",
                "state": "disabled",
            },
        ),
        (
            503,
            {
                "ok": False,
                "error": "memory_rebuild_failed",
                "result": "failed",
            },
            503,
            {"ok": False, "error": "memory_rebuild_failed", "result": "failed"},
        ),
    ],
)
def test_memory_runtime_rebuild_exposes_only_final_closed_results(
    monkeypatch,
    tmp_path,
    internal_status: int,
    internal_body: dict,
    expected_status: int,
    expected_body: dict,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def rebuild(*, user_key: str):
        assert user_key == "avibe:local"
        return {"status_code": internal_status, "body": internal_body}

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.post(
        "/api/memory/runtime/rebuild",
        json={"confirm": True},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == expected_status
    assert response.status_code != 202
    assert response.get_json() == expected_body


def test_memory_runtime_rebuild_joins_duplicate_and_survives_caller_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    pending = V2Config.load().memory
    pending.recovery_intent = "rebuild"
    _save_memory(pending)
    rebuild_started = asyncio.Event()
    release_rebuild = asyncio.Event()
    second_request_entered = asyncio.Event()
    user_key_calls = 0
    rebuild_calls: list[str] = []
    mutation_calls: list[str] = []

    def user_key() -> str:
        nonlocal user_key_calls
        user_key_calls += 1
        if user_key_calls == 2:
            second_request_entered.set()
        return "avibe:local"

    async def rebuild(*, user_key: str):
        rebuild_calls.append(user_key)
        rebuild_started.set()
        await release_rebuild.wait()
        settled = V2Config.load().memory
        settled.recovery_intent = None
        _save_memory(settled)
        return {"status_code": 200, "body": {"ok": True, "result": "completed_empty"}}

    async def unexpected_reconcile():
        mutation_calls.append("reconcile")
        return {"status_code": 200, "body": {"ok": True}}

    async def unexpected_clear(*, user_key: str):
        mutation_calls.append(f"clear:{user_key}")
        return {"status_code": 200, "body": {"status": "completed"}}

    async def unexpected_restart():
        mutation_calls.append("restart")
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(ui_memory_routes, "_memory_ui_user_key", user_key)
    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    monkeypatch.setattr(internal_client, "reconcile_memory", unexpected_reconcile)
    monkeypatch.setattr(internal_client, "memory_clear", unexpected_clear)
    monkeypatch.setattr(internal_client, "memory_restart", unexpected_restart)

    async def scenario() -> tuple[httpx.Response, list[httpx.Response]]:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        headers = {
            "Origin": "http://127.0.0.1:15131",
            "X-Vibe-CSRF-Token": "rebuild-csrf",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:15131",
            cookies={"vibe_csrf_token": "rebuild-csrf"},
        ) as client:
            first = asyncio.create_task(
                client.post("/api/memory/runtime/rebuild", json={"confirm": True}, headers=headers)
            )
            await rebuild_started.wait()
            second = asyncio.create_task(
                client.post("/api/memory/runtime/rebuild", json={"confirm": True}, headers=headers)
            )
            await second_request_entered.wait()

            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            mutations = await asyncio.gather(
                client.patch("/api/memory/settings", json={"enabled": True}, headers=headers),
                client.post("/api/memory/clear", json={"confirm": True}, headers=headers),
                client.post("/api/memory/runtime/restart", json={}, headers=headers),
            )
            assert not second.done()
            release_rebuild.set()
            return await second, mutations

    second_response, mutation_responses = asyncio.run(scenario())

    assert rebuild_calls == ["avibe:local"]
    assert second_response.status_code == 200
    assert second_response.json() == {"ok": True, "result": "completed_empty"}
    assert [response.status_code for response in mutation_responses] == [409, 409, 409]
    assert all(
        response.json() == {"status": "failed", "error": "memory_operation_in_progress"}
        for response in mutation_responses
    )
    assert mutation_calls == []
    assert V2Config.load().memory.enabled is False


def test_confirmed_settings_rebuild_and_retry_share_owner_while_settings_lock_is_held(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load().memory
    current.processing.embedding = MemoryEndpointConfig(
        "https://embed.example.test/v1",
        "embed-v1",
        "embed-key",
    )
    _save_memory(current)
    rebuild_started = asyncio.Event()
    release_rebuild = asyncio.Event()
    retry_entered = asyncio.Event()
    user_key_calls = 0
    rebuild_calls: list[str] = []

    def user_key() -> str:
        nonlocal user_key_calls
        user_key_calls += 1
        if user_key_calls == 2:
            retry_entered.set()
        return "avibe:local"

    async def rebuild(*, user_key: str):
        rebuild_calls.append(user_key)
        persisted = V2Config.load().memory
        assert persisted.processing.embedding.model == "embed-v2"
        assert persisted.recovery_intent == "rebuild"
        rebuild_started.set()
        await release_rebuild.wait()
        persisted = V2Config.load().memory
        persisted.recovery_intent = None
        _save_memory(persisted)
        return {"status_code": 200, "body": {"ok": True, "result": "completed_empty"}}

    monkeypatch.setattr(ui_memory_routes, "_memory_ui_user_key", user_key)
    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        headers = {
            "Origin": "http://127.0.0.1:15131",
            "X-Vibe-CSRF-Token": "rebuild-csrf",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:15131",
            cookies={"vibe_csrf_token": "rebuild-csrf"},
        ) as client:
            confirmed = asyncio.create_task(
                client.patch(
                    "/api/memory/settings",
                    json={
                        "processing": {"embedding": {"model": "embed-v2"}},
                        "confirm_rebuild": True,
                    },
                    headers=headers,
                )
            )
            await rebuild_started.wait()
            retry = asyncio.create_task(
                client.post("/api/memory/runtime/rebuild", json={"confirm": True}, headers=headers)
            )
            await retry_entered.wait()
            assert not retry.done()
            release_rebuild.set()
            return await asyncio.gather(confirmed, retry)

    confirmed_response, retry_response = asyncio.run(scenario())

    assert rebuild_calls == ["avibe:local"]
    assert confirmed_response.status_code == 200
    assert confirmed_response.json()["runtime"] == {
        "ok": True,
        "result": "completed_empty",
    }
    assert retry_response.status_code == 200
    assert retry_response.json() == {"ok": True, "result": "completed_empty"}
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.model == "embed-v2"
    assert persisted.recovery_intent is None


def test_memory_config_stale_controller_settlement_cannot_clobber_newer_candidate(
    monkeypatch,
    tmp_path,
) -> None:
    """Cross-process CAS: a stale settlement loses to a newer confirmed write."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    baseline = V2Config.load()
    baseline.memory = MemoryConfig(
        enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(baseline.memory)

    stale_snapshot = V2Config.load().memory
    from core.memory.runtime import MemoryRuntime

    runtime = object.__new__(MemoryRuntime)

    async def rebuild(*, user_key: str):
        assert user_key == "avibe:local"
        # The route has already durably written embed-v2 + intent. A Controller
        # operation holding embed-v1 cannot settle or overwrite that candidate.
        assert runtime._settle_rebuild_intent(stale_snapshot) is None
        confirmed = V2Config.load().memory
        assert confirmed.processing.embedding.model == "embed-v2"
        assert confirmed.recovery_intent == "rebuild"
        assert runtime._settle_rebuild_intent(confirmed) is not None
        return {"status_code": 200, "body": {"ok": True, "result": "completed_empty"}}

    monkeypatch.setattr(internal_client, "memory_rebuild", rebuild)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {"embedding": {"model": "embed-v2"}},
            "confirm_rebuild": True,
        },
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.model == "embed-v2"
    assert persisted.recovery_intent is None


def test_memory_settings_controller_lease_returns_busy_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Controller ownership is authoritative even if the UI lost its local task."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    baseline = MemoryConfig(
        enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                "https://llm.example.test/v1", "chat", "llm-key"
            ),
            embedding=MemoryEndpointConfig(
                "https://embed.example.test/v1", "embed-v1", "embed-key"
            ),
        ),
    )
    _save_memory(baseline)
    config_path = tmp_path / "config" / "config.json"
    before = config_path.read_bytes()
    monkeypatch.setattr(ui_memory_routes, "_rebuild_request_task", None)
    monkeypatch.setattr(ui_memory_routes, "_rebuild_request_task_loop", None)
    monkeypatch.setattr(
        internal_client,
        "reconcile_memory",
        lambda: (_ for _ in ()).throw(AssertionError("busy save must not reconcile")),
    )
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()

    try:
        client = app.test_client()
        response = client.patch(
            "/api/memory/settings",
            json={"processing": {"llm": {"model": "chat-next"}}},
            headers=csrf_headers(client, "http://127.0.0.1:15131"),
            base_url="http://127.0.0.1:15131",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
    finally:
        lease.release()

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }
    assert config_path.read_bytes() == before


def test_overlapping_memory_settings_patches_never_interleave_save_and_rollback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    baseline = V2Config.load()
    baseline.memory = MemoryConfig(
        enabled=False,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(baseline.memory)
    observed: list[str] = []
    first_reconcile_entered = asyncio.Event()
    release_first_reconcile = asyncio.Event()
    accepted_persisted = asyncio.Event()
    save_memory_config = api.save_memory_config

    def save(memory_payload, **kwargs):
        saved = save_memory_config(memory_payload, **kwargs)
        if saved.memory.processing.llm.model == "chat-accepted":
            accepted_persisted.set()
        return saved

    async def reconcile():
        model = V2Config.load().memory.processing.llm.model
        observed.append(model)
        if not first_reconcile_entered.is_set():
            first_reconcile_entered.set()
            await release_first_reconcile.wait()
        if model == "chat-rejected":
            return {"status_code": 200, "body": {"ok": False, "error": "memory_clear_failed"}}
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(api, "save_memory_config", save)
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)

    async def scenario():
        rejected = asyncio.create_task(
            ui_memory_routes._apply_memory_settings_patch(
                {"processing": {"llm": {"model": "chat-rejected"}}},
                user_key="local",
            )
        )
        await first_reconcile_entered.wait()
        accepted = asyncio.create_task(
            ui_memory_routes._apply_memory_settings_patch(
                {"processing": {"llm": {"model": "chat-accepted"}}},
                user_key="local",
            )
        )
        # Give the second request every chance to persist while the first is
        # still inside reconciliation: without serialization it lands here and
        # the first request's rollback then overwrites it. The wait must time
        # out once the sequence is serialized, so keep it short.
        try:
            await asyncio.wait_for(accepted_persisted.wait(), timeout=0.5)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        release_first_reconcile.set()
        return await rejected, await accepted

    rejected_response, accepted_response = asyncio.run(scenario())

    assert rejected_response.status_code == 409
    assert accepted_response.status_code == 200
    # The rejected request rolls its own candidate back and the accepted request
    # then starts from that restored baseline; neither observes the other's
    # half-applied state.
    assert observed == ["chat-rejected", "chat", "chat-accepted"]
    assert V2Config.load().memory.processing.llm.model == "chat-accepted"


def test_memory_clear_requires_the_global_csrf_proof(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[bool] = []

    async def clear():
        calls.append(True)
        return {"status_code": 200, "body": {"status": "completed", "epoch": 2}}

    monkeypatch.setattr(internal_client, "memory_clear", clear)
    response = app.test_client().post(
        "/api/memory/clear",
        json={"confirm": True},
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("action", ["resume", "abort"])
def test_memory_clear_recovery_forwards_exact_operation(
    monkeypatch,
    tmp_path,
    action: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[tuple[str, str, str]] = []

    async def recover(operation_id: str, *, action: str, user_key: str):
        calls.append((operation_id, action, user_key))
        return {
            "status_code": 200,
            "body": {"status": "aborted" if action == "abort" else "completed"},
        }

    monkeypatch.setattr(internal_client, "memory_clear_recovery", recover)
    client = app.test_client()
    response = client.post(
        f"/api/memory/clear/{action}",
        json={"operation_id": "clear-42"},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert calls == [("clear-42", action, "avibe:local")]


def test_memory_runtime_restart_calls_the_dedicated_transport(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[bool] = []

    async def restart():
        calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "ready"}}

    monkeypatch.setattr(internal_client, "memory_restart", restart)
    client = app.test_client()
    response = client.post(
        "/api/memory/runtime/restart",
        json={},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "state": "ready"}
    assert calls == [True]


def test_memory_runtime_restart_rejects_cross_origin_callers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[bool] = []

    async def restart():
        calls.append(True)
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(internal_client, "memory_restart", restart)
    response = app.test_client().post(
        "/api/memory/runtime/restart",
        json={},
        headers={"Origin": "http://evil.example"},
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 403
    assert calls == []


def test_memory_runtime_restart_maps_internal_unavailability(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def restart():
        raise internal_client.InternalServerUnavailable("controller offline")

    monkeypatch.setattr(internal_client, "memory_restart", restart)
    client = app.test_client()
    response = client.post(
        "/api/memory/runtime/restart",
        json={},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_sidecar_unavailable",
    }


def test_memory_restart_route_shares_retained_request_and_blocks_settings_after_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    restart_started = asyncio.Event()
    release_restart = asyncio.Event()
    patch_entered = asyncio.Event()
    restart_calls: list[bool] = []
    reconcile_calls: list[bool] = []

    async def restart():
        restart_calls.append(True)
        restart_started.set()
        await release_restart.wait()
        return {"status_code": 200, "body": {"ok": True, "state": "ready"}}

    async def reconcile():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "memory_restart", restart)
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    apply_settings_patch = ui_memory_routes._apply_memory_settings_patch

    async def tracked_settings_patch(*args, **kwargs):
        patch_entered.set()
        return await apply_settings_patch(*args, **kwargs)

    monkeypatch.setattr(
        ui_memory_routes,
        "_apply_memory_settings_patch",
        tracked_settings_patch,
    )

    async def _go() -> None:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        headers = {
            "Origin": "http://127.0.0.1:15131",
            "X-Vibe-CSRF-Token": "restart-csrf",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:15131",
            cookies={"vibe_csrf_token": "restart-csrf"},
        ) as client:
            first = asyncio.create_task(
                client.post("/api/memory/runtime/restart", json={}, headers=headers)
            )
            await restart_started.wait()
            second = asyncio.create_task(
                client.post("/api/memory/runtime/restart", json={}, headers=headers)
            )
            await asyncio.sleep(0)
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass

            patch = asyncio.create_task(
                client.patch(
                    "/api/memory/settings",
                    json={"enabled": False},
                    headers=headers,
                )
            )
            await patch_entered.wait()
            await asyncio.sleep(0)
            assert restart_calls == [True]
            assert reconcile_calls == []
            assert not second.done()
            assert not patch.done()

            release_restart.set()
            second_response = await second
            patch_response = await patch

        assert second_response.status_code == 200
        assert second_response.json() == {"ok": True, "state": "ready"}
        assert patch_response.status_code == 200
        assert reconcile_calls == [True]

    asyncio.run(_go())


def test_memory_restart_route_isolates_remote_session_cookie_renewal(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_remote_config(tmp_path)
    restart_started = asyncio.Event()
    release_restart = asyncio.Event()
    second_waiter_joined = asyncio.Event()
    restart_calls: list[bool] = []
    restart_waiters = 0

    async def restart():
        restart_calls.append(True)
        restart_started.set()
        await release_restart.wait()
        return {"status_code": 200, "body": {"ok": True, "state": "ready"}}

    monkeypatch.setattr(internal_client, "memory_restart", restart)
    restart_request_task = ui_memory_routes._memory_restart_request_task

    def tracked_restart_request_task():
        nonlocal restart_waiters
        restart_waiters += 1
        if restart_waiters == 2:
            second_waiter_joined.set()
        return restart_request_task()

    monkeypatch.setattr(
        ui_memory_routes,
        "_memory_restart_request_task",
        tracked_restart_request_task,
    )

    async def _go() -> tuple[httpx.Response, httpx.Response]:
        completed_after_hooks = 0
        after_hooks_released = asyncio.Event()

        async def hold_after_hooks(response):
            nonlocal completed_after_hooks
            completed_after_hooks += 1
            if completed_after_hooks == 2:
                after_hooks_released.set()
            await after_hooks_released.wait()
            return response

        # Run this after the normal hooks so both renewal mutations are present
        # before either response is sent. Sharing a Response then fails
        # deterministically instead of depending on ASGI scheduling.
        app._after_request_handlers.insert(0, hold_after_hooks)
        try:
            origin = "https://alex.avibe.bot"
            first_transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 12345))
            second_transport = httpx.ASGITransport(app=app, client=("203.0.113.11", 12346))
            first_cookies = {
                remote_access.SESSION_COOKIE_NAME: _renewable_remote_session_cookie(
                    config,
                    "one@example.com",
                    "user-1",
                ),
                "vibe_csrf_token": "csrf-one",
            }
            second_cookies = {
                remote_access.SESSION_COOKIE_NAME: _renewable_remote_session_cookie(
                    config,
                    "two@example.com",
                    "user-2",
                ),
                "vibe_csrf_token": "csrf-two",
            }
            async with (
                httpx.AsyncClient(
                    transport=first_transport,
                    base_url=origin,
                    cookies=first_cookies,
                ) as first_client,
                httpx.AsyncClient(
                    transport=second_transport,
                    base_url=origin,
                    cookies=second_cookies,
                ) as second_client,
            ):
                first = asyncio.create_task(
                    first_client.post(
                        "/api/memory/runtime/restart",
                        json={},
                        headers={"Origin": origin, "X-Vibe-CSRF-Token": "csrf-one"},
                    )
                )
                await restart_started.wait()
                second = asyncio.create_task(
                    second_client.post(
                        "/api/memory/runtime/restart",
                        json={},
                        headers={"Origin": origin, "X-Vibe-CSRF-Token": "csrf-two"},
                    )
                )
                await second_waiter_joined.wait()
                release_restart.set()
                return await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
        finally:
            app._after_request_handlers.remove(hold_after_hooks)

    first_response, second_response = asyncio.run(_go())

    def renewed_subject(response: httpx.Response) -> str:
        session_headers = [
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith(f"{remote_access.SESSION_COOKIE_NAME}=")
        ]
        assert len(session_headers) == 1
        cookie_value = session_headers[0].split(";", 1)[0].split("=", 1)[1]
        payload = remote_access.parse_session_cookie(config, cookie_value)
        assert payload is not None
        return str(payload["sub"])

    assert restart_calls == [True]
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert renewed_subject(first_response) == "user-1"
    assert renewed_subject(second_response) == "user-2"


def test_memory_disable_under_pending_marker_still_reconciles(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=True,
        recovery_intent="rebuild",
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(current.memory)
    reconcile_calls = []

    async def reconcile():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"enabled": False},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert reconcile_calls == [True]
    persisted = V2Config.load().memory
    assert persisted.enabled is False
    assert persisted.recovery_intent == "rebuild"


def test_memory_settings_save_is_refused_while_rebuild_is_running(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    current = V2Config.load()
    current.memory = MemoryConfig(
        enabled=False,
        recovery_intent="rebuild",
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v1", "embed-key"),
        ),
    )
    _save_memory(current.memory)
    reconcile_calls: list[bool] = []

    async def reconcile():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "state": "ready"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    monkeypatch.setattr(ui_memory_routes, "_memory_rebuild_request_running", lambda: True)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"enabled": True},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }
    assert reconcile_calls == []
    persisted = V2Config.load().memory
    assert persisted.enabled is False
    assert persisted.recovery_intent == "rebuild"
