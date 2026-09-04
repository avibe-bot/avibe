from __future__ import annotations

import ast
import builtins
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
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
    atomic_update_memory,
    memory_config_from_payload,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, internal_client, model_service, ui_memory_routes
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


def _custom_memory_with_cloud_bundle(
    *,
    scope: str = "platform",
    access_key_revision: int | None = 1,
    transition_notice_pending: bool = False,
) -> MemoryConfig:
    return MemoryConfig(
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
        cloud=MemoryCloudConfig(
            scope=scope,
            capabilities=MemoryCloudCapabilities(
                chat=True,
                embedding=True,
                memory_llm=True,
            ),
            memory_llm_source="chat_fallback",
            embedding_identity="cloud-emb-v2",
            applied_embedding_identity="cloud-emb-v1",
            revision=2,
            model_access_key="mak_opaque",
            rerank_access_key="mak_rr_deepinfra_opaque",
            access_key_revision=access_key_revision,
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
            transition_notice_pending=transition_notice_pending,
        ),
    )


def _paired_model_service_config() -> SimpleNamespace:
    return SimpleNamespace(
        remote_access=SimpleNamespace(
            vibe_cloud=SimpleNamespace(
                runtime_credentials=lambda: (
                    "https://backend.example.test",
                    "instance-1",
                    "device-secret",
                )
            )
        )
    )


def _current_model_service_status(_config, _credentials, method, suffix) -> dict:
    assert (method, suffix) == ("GET", "model-service")
    return {
        "mode": "platform",
        "capabilities": {
            "asr": False,
            "chat": True,
            "embedding": True,
            "multimodal": False,
            "memory_llm": True,
        },
        "memory_llm_source": "chat_fallback",
        "embedding_identity": "cloud-emb-v2",
        "quota": {"enforced": False},
        "revision": 2,
    }


def _persist_refreshed_cloud_bundle(config: V2Config) -> V2Config:
    candidate = deepcopy(config.memory)
    candidate.cloud.rerank_access_key = "mak_rr_dashscope_opaque"
    candidate.cloud.access_key_revision = 2
    return model_service._persist_candidate(  # noqa: SLF001
        config.memory,
        candidate,
    )


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


@pytest.mark.parametrize("access_key_revision", [None, 1])
@pytest.mark.parametrize(
    ("scope", "patch_payload"),
    [
        ("platform", {"mode": "platform", "confirm_loss": True}),
        (
            "organization",
            {"acknowledge_transition": True, "confirm_loss": True},
        ),
    ],
)
def test_noncurrent_cloud_bundle_is_refreshed_before_settings_transition(
    scope: str,
    patch_payload: dict[str, object],
    access_key_revision: int | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(
        _custom_memory_with_cloud_bundle(
            scope=scope,
            access_key_revision=access_key_revision,
            transition_notice_pending=scope == "organization",
        )
    )
    events: list[str] = []
    preflights: list[dict[str, object]] = []
    reconfigures: list[dict[str, object]] = []

    def ensure(config: V2Config) -> V2Config:
        events.append("ensure")
        assert config.memory.mode == "custom"
        assert config.memory.cloud.access_key_revision == access_key_revision

        def refresh_bundle(memory: MemoryConfig) -> MemoryConfig:
            memory.cloud.rerank_access_key = "mak_rr_dashscope_opaque"
            memory.cloud.access_key_revision = 2
            return memory

        return atomic_update_memory(refresh_bundle)

    async def preflight(*, payload, user_key):
        events.append("preflight")
        preflights.append({"payload": payload, "user_key": user_key})
        return {"status_code": 200, "body": {"ok": True}}

    async def reconfigure(*, confirm_loss, memory, expected_memory, user_key):
        events.append("reconfigure")
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

    async def unexpected_reconcile():
        pytest.fail("settings transition issued an extra reconciliation")

    monkeypatch.setattr(model_service, "ensure_model_access_key", ensure)
    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    monkeypatch.setattr(internal_client, "memory_reconfigure", reconfigure)
    monkeypatch.setattr(internal_client, "reconcile_memory", unexpected_reconcile)
    client = app.test_client()

    response = client.patch(
        "/api/memory/settings",
        json=patch_payload,
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 200
    assert events == ["ensure", "preflight", "reconfigure"]
    assert len(preflights) == len(reconfigures) == 1
    candidate_payload = reconfigures[0]["memory"]
    assert preflights[0]["payload"]["memory"] == candidate_payload
    candidate = ui_memory_routes._memory_candidate_config(  # noqa: SLF001
        V2Config.load(),
        candidate_payload,
    ).memory
    assert candidate.cloud.revision == 2
    assert candidate.cloud.access_key_revision == 2
    assert candidate.cloud.model_access_key == "mak_opaque"
    assert candidate.cloud.rerank_access_key == "mak_rr_dashscope_opaque"
    rerank = candidate.runtime_processing().rerank
    assert candidate.runtime_source() == "cloud"
    assert rerank is not None
    assert rerank.api_key == "mak_rr_dashscope_opaque"
    assert candidate.runtime_embedding_identity() == ("cloud", "cloud-emb-v2", None)
    assert reconfigures[0]["confirm_loss"] is True
    assert reconfigures[0]["user_key"] == "avibe:local"


def test_current_cloud_bundle_skips_refresh_during_settings_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(_custom_memory_with_cloud_bundle(access_key_revision=2))
    events: list[str] = []
    candidates: list[dict[str, object]] = []

    def unexpected_ensure(_config: V2Config) -> V2Config:
        raise AssertionError("current cloud bundle was refreshed")

    async def preflight(*, payload, user_key):
        events.append("preflight")
        candidates.append(payload["memory"])
        assert user_key == "avibe:local"
        return {"status_code": 200, "body": {"ok": True}}

    async def reconfigure(**_kwargs):
        events.append("reconfigure")
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

    monkeypatch.setattr(model_service, "ensure_model_access_key", unexpected_ensure)
    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    monkeypatch.setattr(internal_client, "memory_reconfigure", reconfigure)
    client = app.test_client()

    response = client.patch(
        "/api/memory/settings",
        json={"mode": "platform", "confirm_loss": True},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 200
    assert events == ["preflight", "reconfigure"]
    assert len(candidates) == 1
    candidate = ui_memory_routes._memory_candidate_config(  # noqa: SLF001
        V2Config.load(),
        candidates[0],
    ).memory
    rerank = candidate.runtime_processing().rerank
    assert rerank is not None
    assert rerank.api_key == "mak_rr_deepinfra_opaque"


def test_cloud_bundle_refresh_failure_does_not_apply_settings_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    original = _custom_memory_with_cloud_bundle(access_key_revision=1)
    _save_config(original)
    downstream_calls: list[str] = []

    def fail_ensure(_config: V2Config) -> V2Config:
        raise model_service.ModelServiceResolutionError("cloud_request_failed")

    async def preflight(**_kwargs):
        downstream_calls.append("preflight")
        return {"status_code": 200, "body": {"ok": True}}

    async def reconfigure(**_kwargs):
        downstream_calls.append("reconfigure")
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(model_service, "ensure_model_access_key", fail_ensure)
    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    monkeypatch.setattr(internal_client, "memory_reconfigure", reconfigure)
    client = app.test_client()

    response = client.patch(
        "/api/memory/settings",
        json={"mode": "platform", "confirm_loss": True},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "failed",
        "error": "memory_store_unavailable",
    }
    assert downstream_calls == []
    persisted = V2Config.load().memory
    assert persisted.mode == "custom"
    assert persisted.cloud.model_access_key == original.cloud.model_access_key
    assert persisted.cloud.rerank_access_key == original.cloud.rerank_access_key
    assert persisted.cloud.access_key_revision == 1


@pytest.mark.parametrize(
    ("downstream_outcome", "expected_status", "expected_events"),
    [
        ("preflight_failed", 409, ["ensure", "preflight"]),
        ("reconfigure_failed", 500, ["ensure", "preflight", "reconfigure"]),
        ("reconfigure_busy", 409, ["ensure", "preflight", "reconfigure"]),
    ],
)
def test_ensured_active_bundle_remains_pending_when_settings_completion_fails(
    downstream_outcome: str,
    expected_status: int,
    expected_events: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    memory = _custom_memory_with_cloud_bundle(
        access_key_revision=1,
        transition_notice_pending=True,
    )
    memory.mode = "platform"
    _save_config(memory)
    events: list[str] = []

    def ensure(config: V2Config) -> V2Config:
        events.append("ensure")
        candidate = deepcopy(config.memory)
        candidate.cloud.rerank_access_key = "mak_rr_dashscope_opaque"
        candidate.cloud.access_key_revision = 2
        return model_service._persist_candidate(  # noqa: SLF001
            config.memory,
            candidate,
        )

    async def preflight(**_kwargs):
        events.append("preflight")
        if downstream_outcome == "preflight_failed":
            return {
                "status_code": 409,
                "body": {"ok": False, "error": "memory_processing_failed"},
            }
        return {"status_code": 200, "body": {"ok": True}}

    async def reconfigure(**_kwargs):
        events.append("reconfigure")
        if downstream_outcome == "reconfigure_busy":
            return {
                "status_code": 409,
                "body": {
                    "ok": False,
                    "operation": "reconfigure",
                    "state": "busy",
                    "result": "unchanged",
                    "error": "memory_operation_in_progress",
                },
            }
        return {
            "status_code": 500,
            "body": {
                "ok": False,
                "operation": "reconfigure",
                "state": "failed",
                "result": "failed",
                "error": "memory_reconfigure_failed",
            },
        }

    async def unexpected_reconcile():
        pytest.fail("failed settings completion issued reconciliation")

    monkeypatch.setattr(model_service, "ensure_model_access_key", ensure)
    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    monkeypatch.setattr(internal_client, "memory_reconfigure", reconfigure)
    monkeypatch.setattr(internal_client, "reconcile_memory", unexpected_reconcile)
    client = app.test_client()

    response = client.patch(
        "/api/memory/settings",
        json={"acknowledge_transition": True, "confirm_loss": True},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == expected_status
    assert events == expected_events
    persisted = V2Config.load().memory
    assert persisted.mode == "platform"
    assert persisted.cloud.transition_notice_pending is True
    assert persisted.cloud.applied_embedding_identity == "cloud-emb-v1"
    assert persisted.cloud.revision == persisted.cloud.access_key_revision == 2
    assert persisted.cloud.rerank_access_key == "mak_rr_dashscope_opaque"
    assert persisted.cloud.runtime_apply_pending is True
    rerank = persisted.runtime_processing().rerank
    assert rerank is not None
    assert rerank.api_key == "mak_rr_dashscope_opaque"


@pytest.mark.parametrize(
    "apply_outcome",
    ["success", "concurrent_persistence", "failed"],
)
def test_successful_settings_reconcile_clears_only_the_exact_applied_bundle(
    apply_outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    memory = _custom_memory_with_cloud_bundle(access_key_revision=1)
    memory.mode = "platform"
    memory.cloud.applied_embedding_identity = "cloud-emb-v2"
    _save_config(memory)
    reconciled: list[MemoryConfig] = []

    async def reconcile_memory():
        reconciled.append(deepcopy(V2Config.load().memory))
        if apply_outcome == "concurrent_persistence" and len(reconciled) == 1:

            def update(memory: MemoryConfig) -> MemoryConfig:
                memory.cloud.quota_enforced = True
                return memory

            atomic_update_memory(update)
        if apply_outcome == "failed" and len(reconciled) == 1:
            return {
                "status_code": 503,
                "body": {"ok": False, "error": "memory_sidecar_unavailable"},
            }
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(
        model_service,
        "ensure_model_access_key",
        _persist_refreshed_cloud_bundle,
    )
    monkeypatch.setattr(
        model_service,
        "_paired_device_request",
        _current_model_service_status,
    )
    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile_memory)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"mode": "platform"},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == (503 if apply_outcome == "failed" else 200)
    expected_reconciles = 2 if apply_outcome == "failed" else 1
    assert len(reconciled) == expected_reconciles
    assert reconciled[0].cloud.rerank_access_key == "mak_rr_dashscope_opaque"
    assert reconciled[0].cloud.runtime_apply_pending is True
    persisted = V2Config.load().memory
    assert persisted.cloud.runtime_apply_pending is (apply_outcome != "success")

    if apply_outcome == "success":
        result = model_service.sync_model_service_once(_paired_model_service_config())
        assert result == {"ok": True, "configured": True, "changed": False}
        assert len(reconciled) == 1


def test_successful_identity_reconfigure_clears_the_exact_applied_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    memory = _custom_memory_with_cloud_bundle(
        access_key_revision=1,
        transition_notice_pending=True,
    )
    memory.mode = "platform"
    _save_config(memory)
    reconfigured: list[MemoryConfig] = []
    reconciled: list[MemoryConfig] = []

    async def preflight(**_kwargs):
        return {"status_code": 200, "body": {"ok": True}}

    async def reconfigure(*, confirm_loss, memory, expected_memory, user_key):
        assert confirm_loss is True
        assert user_key == "avibe:local"
        saved = api.save_memory_config(
            memory,
            expected=memory_config_from_payload(expected_memory),
        )
        reconfigured.append(deepcopy(saved.memory))
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

    async def unexpected_reconcile():
        reconciled.append(deepcopy(V2Config.load().memory))
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(
        model_service,
        "ensure_model_access_key",
        _persist_refreshed_cloud_bundle,
    )
    monkeypatch.setattr(
        model_service,
        "_paired_device_request",
        _current_model_service_status,
    )
    monkeypatch.setattr(internal_client, "memory_preflight", preflight)
    monkeypatch.setattr(internal_client, "memory_reconfigure", reconfigure)
    monkeypatch.setattr(internal_client, "reconcile_memory", unexpected_reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"acknowledge_transition": True, "confirm_loss": True},
        headers=csrf_headers(client, BASE_URL),
        **_request_options(),
    )

    assert response.status_code == 200
    assert len(reconfigured) == 1
    assert reconfigured[0].cloud.rerank_access_key == "mak_rr_dashscope_opaque"
    assert reconfigured[0].cloud.runtime_apply_pending is True
    persisted = V2Config.load().memory
    assert persisted.cloud.runtime_apply_pending is False

    result = model_service.sync_model_service_once(_paired_model_service_config())
    assert result == {"ok": True, "configured": True, "changed": False}
    assert reconciled == []


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
