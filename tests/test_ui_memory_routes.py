from __future__ import annotations

from typing import Any

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
from tests.ui_server_test_helpers import csrf_headers
from vibe import internal_client, ui_memory_routes
from vibe.ui_server import app


BASE_URL = "http://127.0.0.1:15131"
LOCAL_ENV = {"REMOTE_ADDR": "127.0.0.1"}


def _save_config(memory: MemoryConfig | None = None) -> None:
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=memory or MemoryConfig(),
    ).save()


def _local_headers() -> dict[str, str]:
    return {"Origin": BASE_URL}


def _request_options() -> dict[str, Any]:
    return {
        "base_url": BASE_URL,
        "environ_base": LOCAL_ENV,
    }


@pytest.mark.parametrize(
    ("enabled_platforms", "expected"),
    [
        (["slack"], True),
        (["discord"], True),
        (["telegram"], True),
        (["lark"], True),
        (["wechat"], True),
        (["feishu"], False),
        ([], False),
    ],
)
def test_attachment_capture_availability_follows_enabled_platform_allowlist(
    enabled_platforms: list[str],
    expected: bool,
) -> None:
    """MEMORY-IM-ATTACH-003: UI availability follows enabled capture platforms."""

    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    config.platforms.enabled = enabled_platforms

    assert ui_memory_routes._memory_im_attachment_capture_available(config) is expected


def test_memory_settings_patch_uses_loss_explicit_confirmation() -> None:
    current = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )

    target, confirm_loss = ui_memory_routes._memory_settings_patch(
        current,
        {
            "confirm_loss": True,
            "processing": {
                "embedding": {
                    "base_url": "https://embed.example.test/v1",
                    "model": "embed-v2",
                    "api_key": "secret",
                }
            },
        },
    )

    assert confirm_loss is True
    assert target["processing"]["embedding"]["model"] == "embed-v2"
    with pytest.raises(ValueError, match="invalid_memory_patch"):
        ui_memory_routes._memory_settings_patch(current, {"confirm": True})


def test_memory_settings_get_is_no_store_and_never_projects_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(
        MemoryConfig(
            processing=MemoryProcessingConfig(
                llm=MemoryEndpointConfig(
                    "https://llm.example.test/v1",
                    "chat",
                    "llm-secret",
                )
            )
        )
    )

    response = app.test_client().get(
        "/api/memory/settings",
        headers=_local_headers(),
        **_request_options(),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.get_json()
    assert body["processing"]["llm"]["api_key"] is None
    assert body["processing"]["llm"]["has_api_key"] is True
    assert "recovery_intent" not in body
    assert "embedding_change_pending" not in body


def test_memory_status_proxies_coherent_runtime_state_and_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config()

    async def status():
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "state": "degraded",
                "reason": "memory_provider_unavailable",
            },
        }

    monkeypatch.setattr(internal_client, "memory_status", status)
    response = app.test_client().get(
        "/api/memory/status",
        headers=_local_headers(),
        **_request_options(),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "state": "degraded",
        "reason": "memory_provider_unavailable",
    }
    assert response.headers["cache-control"] == "no-store"


def test_memory_wake_is_non_destructive_and_preserves_closed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config()
    calls: list[bool] = []

    async def wake():
        calls.append(True)
        return {
            "status_code": 503,
            "body": {
                "ok": False,
                "operation": "wake",
                "state": "degraded",
                "error": "memory_provider_unavailable",
            },
        }

    monkeypatch.setattr(internal_client, "memory_wake", wake)
    client = app.test_client()
    response = client.post(
        "/api/memory/runtime/wake",
        json={},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "operation": "wake",
        "state": "degraded",
        "error": "memory_provider_unavailable",
    }
    assert calls == [True]


def _assert_memory_destructive_route_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    path: str,
    client_name: str,
    operation: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config()
    calls: list[dict[str, object]] = []

    async def destructive(**kwargs):
        calls.append(kwargs)
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "operation": operation,
                "result": "completed",
                "data_deleted": True,
                "data_remaining": False,
                "roots": [],
            },
        }

    monkeypatch.setattr(internal_client, client_name, destructive)
    client = app.test_client()
    without_csrf = client.post(
        path,
        json={"confirm_loss": True},
        **_request_options(),
    )
    headers = csrf_headers(client, BASE_URL)
    for payload in (
        {},
        {"confirm_loss": False},
        {"confirm": True},
        {"confirm_loss": True, "extra": True},
    ):
        rejected = client.post(
            path,
            json=payload,
            headers=headers,
            **_request_options(),
        )
        assert rejected.status_code == 400
        assert rejected.get_json() == {
            "ok": False,
            "operation": operation,
            "error": "memory_loss_confirmation_required",
            "result": "unchanged",
        }
    accepted = client.post(
        path,
        json={"confirm_loss": True},
        headers=headers,
        **_request_options(),
    )

    assert without_csrf.status_code == 403
    assert accepted.status_code == 200
    assert accepted.get_json()["operation"] == operation
    assert accepted.get_json()["data_deleted"] is True
    assert accepted.headers["cache-control"] == "no-store"
    assert calls == [{"confirm_loss": True, "user_key": "avibe:local"}]


def test_memory_repair_public_route_requires_exact_loss_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """MEMORY-REPAIR-201: the public Repair boundary fails closed."""

    _assert_memory_destructive_route_contract(
        monkeypatch,
        tmp_path,
        "/api/memory/repair",
        "memory_repair",
        "repair",
    )


def test_memory_delete_data_public_route_requires_exact_loss_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """MEMORY-DELETE-DATA-001: Delete data is a distinct confirmed intent."""

    _assert_memory_destructive_route_contract(
        monkeypatch,
        tmp_path,
        "/api/memory/delete-data",
        "memory_delete_data",
        "delete_data",
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/memory/runtime/restart",
        "/api/memory/runtime/rebuild",
        "/api/memory/factory-reset",
        "/api/memory/runtime/factory-reset",
        "/api/memory/clear",
    ],
)
def test_retired_recovery_routes_do_not_exist(path: str) -> None:
    assert path not in {route.path for route in app.routes}


def test_embedding_identity_change_requires_loss_confirmation_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(
        MemoryConfig(
            enabled=True,
            mode="custom",
            processing=MemoryProcessingConfig(
                llm=MemoryEndpointConfig(
                    "https://llm.example.test/v1",
                    "chat",
                    "llm-key",
                ),
                embedding=MemoryEndpointConfig(
                    "https://embed.example.test/v1",
                    "embed-v1",
                    "embed-key",
                ),
            ),
        )
    )

    async def unexpected_preflight(**_kwargs):
        pytest.fail("unconfirmed destructive reconfiguration reached preflight")

    monkeypatch.setattr(internal_client, "memory_preflight", unexpected_preflight)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {
                "embedding": {
                    "model": "embed-v2",
                }
            }
        },
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_loss_confirmation_required",
    }
    assert V2Config.load().memory.processing.embedding.model == "embed-v1"


def test_confirmed_embedding_identity_change_uses_unified_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(
        MemoryConfig(
            enabled=True,
            mode="custom",
            processing=MemoryProcessingConfig(
                llm=MemoryEndpointConfig(
                    "https://llm.example.test/v1",
                    "chat",
                    "llm-key",
                ),
                embedding=MemoryEndpointConfig(
                    "https://embed.example.test/v1",
                    "embed-v1",
                    "embed-key",
                ),
            ),
        )
    )
    preflights: list[dict[str, object]] = []
    reconfigures: list[dict[str, object]] = []

    async def preflight(*, payload, user_key):
        preflights.append({"payload": payload, "user_key": user_key})
        return {"status_code": 200, "body": {"ok": True}}

    async def reconfigure(*, confirm_loss, memory, user_key):
        reconfigures.append(
            {
                "confirm_loss": confirm_loss,
                "memory": memory,
                "user_key": user_key,
            }
        )
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "operation": "reconfigure",
                "state": "running",
                "result": "completed",
                "data_deleted": True,
                "data_remaining": False,
                "roots": [],
            },
        }

    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    monkeypatch.setattr(internal_client, "memory_reconfigure", reconfigure)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "confirm_loss": True,
            "processing": {"embedding": {"model": "embed-v2"}},
        },
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 200
    assert len(preflights) == 1
    assert preflights[0]["user_key"] == "avibe:local"
    assert len(reconfigures) == 1
    assert reconfigures[0]["confirm_loss"] is True
    assert reconfigures[0]["memory"]["processing"]["embedding"]["model"] == "embed-v2"


def test_processing_record_routes_remain_native_and_provider_log_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config()
    calls: list[tuple[object, ...]] = []

    async def entries(*, cursor, limit, project, user_key):
        calls.append(("list", cursor, limit, project, user_key))
        return {
            "status_code": 200,
            "body": {"status": "ok", "entries": [], "next_cursor": None},
        }

    async def entry(memcell_id, *, project, user_key):
        calls.append(("detail", memcell_id, project, user_key))
        return {
            "status_code": 200,
            "body": {"status": "ok", "entry": {"memcell_id": memcell_id}},
        }

    monkeypatch.setattr(internal_client, "memory_processing_record_entries", entries)
    monkeypatch.setattr(internal_client, "memory_processing_record_entry", entry)
    client = app.test_client()
    listed = client.get(
        "/api/memory/processing-record/entries?cursor=opaque&limit=17&project=notes",
        headers=_local_headers(),
        **_request_options(),
    )
    detailed = client.get(
        "/api/memory/processing-record/entry?memcell_id=mc_1&project=notes",
        headers=_local_headers(),
        **_request_options(),
    )

    assert listed.status_code == detailed.status_code == 200
    assert calls == [
        ("list", "opaque", 17, "notes", "avibe:local"),
        ("detail", "mc_1", "notes", "avibe:local"),
    ]
    route_paths = {route.path for route in app.routes}
    assert "/api/memory/calls" not in route_paths
    assert "/api/memory/provider-calls" not in route_paths
