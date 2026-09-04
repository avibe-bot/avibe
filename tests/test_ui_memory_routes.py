from __future__ import annotations

import ast
import builtins
from pathlib import Path
from typing import Any

import pytest

from config.v2_config import (
    AgentsConfig,
    MemoryCloudCapabilities,
    MemoryCloudConfig,
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


def test_platform_embedding_transition_accepts_confirmed_data_loss() -> None:
    current = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=True,
            mode="platform",
            cloud=MemoryCloudConfig(
                scope="platform",
                capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
                embedding_identity="emb-v2",
                applied_embedding_identity="emb-v1",
                transition_notice_pending=True,
                model_access_key="mak-current",
                proxy_base_url="https://backend.example.test/v1/model",
                source_instance_id="instance-1",
            ),
        ),
    )

    target, confirm_loss = ui_memory_routes._memory_settings_patch(
        current,
        {"acknowledge_transition": True, "confirm_loss": True},
    )

    assert confirm_loss is True
    assert target["cloud"]["applied_embedding_identity"] == "emb-v2"
    assert target["cloud"]["transition_notice_pending"] is False
    assert target["cloud"]["organization_attached"] is False


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


def test_custom_rerank_candidate_failure_blocks_settings_persistence(
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

    async def preflight(*, payload, user_key):
        preflights.append({"payload": payload, "user_key": user_key})
        return {
            "status_code": 409,
            "body": {
                "ok": False,
                "error": "memory_rerank_unavailable",
                "diagnostic": {"side": "rerank", "message": "invalid_candidate"},
            },
        }

    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={
            "processing": {
                "rerank": {
                    "base_url": "https://rerank.example.test/v1/inference",
                    "model": "rerank-model",
                    "api_key": "invalid-key",
                    "provider": "deepinfra",
                }
            }
        },
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_rerank_unavailable",
        "diagnostic": {"side": "rerank", "message": "invalid_candidate"},
    }
    assert len(preflights) == 1
    candidate = preflights[0]["payload"]["memory"]
    rerank = candidate["processing"]["rerank"]
    assert rerank["base_url"] == "https://rerank.example.test/v1/inference"
    assert rerank["model"] == "rerank-model"
    assert rerank["api_key"] == "invalid-key"
    assert rerank["provider"] == "deepinfra"
    assert rerank["has_api_key"] is True
    assert preflights[0]["user_key"] == "avibe:local"
    assert V2Config.load().memory.processing.rerank is None


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

    async def reconfigure(*, confirm_loss, memory, expected_memory, user_key):
        reconfigures.append(
            {
                "confirm_loss": confirm_loss,
                "memory": memory,
                "expected_memory": expected_memory,
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
    assert (
        reconfigures[0]["expected_memory"]["processing"]["embedding"]["model"]
        == "embed-v1"
    )


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


def test_memory_list_route_forwards_agent_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """MEMORY-LIST-008: Settings can select the derived Agent owner."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config()
    calls: list[dict[str, object]] = []

    async def memory_list(**kwargs):
        calls.append(kwargs)
        return {
            "status_code": 200,
            "body": {"status": "ok", "items": [], "warnings": []},
        }

    monkeypatch.setattr(internal_client, "memory_list", memory_list)
    client = app.test_client()
    response = client.post(
        "/api/memory/list",
        json={
            "project": "write",
            "page": 1,
            "limit": 100,
            "origin": "agent",
        },
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 200
    assert calls == [
        {
            "user_key": "avibe:local",
            "project": "write",
            "page": 1,
            "cursor": None,
            "limit": 100,
            "origin": "agent",
        }
    ]

    invalid = client.post(
        "/api/memory/list",
        json={"project": "write", "page": 1, "origin": ["agent"]},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )
    assert invalid.status_code == 400
    assert invalid.get_json() == {
        "status": "failed",
        "error": "memory_invalid_input",
    }
    over_limit = client.post(
        "/api/memory/list",
        json={"project": "write", "page": 1, "limit": 101},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )
    assert over_limit.status_code == 400
    assert len(calls) == 1


def test_memory_list_ui_route_stays_host_owned_when_runtime_import_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """UI Memory routes stay host-owned when implementation imports are blocked."""

    tree = ast.parse(Path(ui_memory_routes.__file__).read_text(encoding="utf-8"))
    implementation_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("avibe_memory.")
        and node.module != "core.memory_loader"
    ]
    assert implementation_imports == []

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config()
    calls: list[dict[str, object]] = []

    async def memory_list(**kwargs):
        calls.append(kwargs)
        return {
            "status_code": 200,
            "body": {"status": "ok", "items": [], "next_cursor": None},
        }

    monkeypatch.setattr(internal_client, "memory_list", memory_list)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("avibe_memory.") and name != "core.memory_loader":
            raise RuntimeError(f"optional implementation initializer: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    from core.memory_loader import MEMORY_LIST_CURSOR_MAX_BYTES

    client = app.test_client()
    settings = client.get(
        "/api/memory/settings",
        headers=_local_headers(),
        **_request_options(),
    )

    assert settings.status_code == 200
    assert settings.get_json()["status"] == "ok"

    async def memory_search(query, policy, **kwargs):
        assert query == "hello"
        assert policy["mode"] == "keyword"
        assert kwargs == {"user_key": "avibe:local", "project": None}
        return {"status_code": 200, "body": {"status": "ok", "items": []}}

    monkeypatch.setattr(internal_client, "memory_search", memory_search)
    search = client.post(
        "/api/memory/search",
        json={"query": "hello", "policy": {"mode": "keyword"}},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert search.status_code == 200
    assert search.get_json() == {"status": "ok", "items": []}

    response = client.post(
        "/api/memory/list",
        json={"project": "all", "cursor": "a" * MEMORY_LIST_CURSOR_MAX_BYTES},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "items": [],
        "next_cursor": None,
    }
    assert calls == [
        {
            "user_key": "avibe:local",
            "project": "all",
            "page": None,
            "cursor": "a" * MEMORY_LIST_CURSOR_MAX_BYTES,
            "limit": 20,
            "origin": None,
        }
    ]

    invalid = client.post(
        "/api/memory/list",
        json={"project": "all", "cursor": "a" * (MEMORY_LIST_CURSOR_MAX_BYTES + 1)},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert invalid.status_code == 400
    assert invalid.get_json() == {
        "status": "failed",
        "error": "memory_invalid_input",
    }
    assert len(calls) == 1
