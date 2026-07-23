from __future__ import annotations

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
from vibe import internal_client, ui_server
from vibe.ui_server import app


def _save_config(tmp_path) -> None:
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()


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


def test_memory_status_proxies_controller_over_uds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def status():
        return {"status_code": 200, "body": {"state": "disabled", "data_exists": False}}

    monkeypatch.setattr(internal_client, "memory_status", status)
    response = app.test_client().get(
        "/api/memory/status",
        headers=_local_headers(),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"state": "disabled", "data_exists": False}
    assert response.headers["cache-control"] == "no-store"


def test_memory_failures_proxy_is_direct_loopback_only_and_no_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def failures():
        return {
            "status_code": 200,
            "body": {
                "items": [
                    {
                        "kind": "delivery_abandoned",
                        "occurred_at": "2026-01-01T00:00:00.000Z",
                        "error_code": "memory_provider_timeout",
                        "request_id": None,
                        "attempts": 3,
                    }
                ],
                "retention_days": 90,
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
    assert response.get_json()["items"][0]["kind"] == "delivery_abandoned"
    assert response.headers["cache-control"] == "no-store"
    assert forwarded.status_code == 403


def test_memory_search_requires_csrf_and_only_forwards_query_and_limit(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    calls: list[tuple[str, int]] = []

    async def search(query: str, limit: int):
        calls.append((query, limit))
        return {"status_code": 200, "body": {"status": "ok", "items": [], "warnings": []}}

    monkeypatch.setattr(internal_client, "memory_search", search)
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    response = client.post(
        "/api/memory/search",
        json={"query": "find this", "limit": 3},
        headers=headers,
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert calls == [("find this", 3)]
    assert response.headers["cache-control"] == "no-store"


def test_memory_settings_enable_reconciles_through_controller(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

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


def test_memory_enable_rolls_back_when_live_sidecar_reconciliation_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

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


def test_memory_embedding_change_rolls_back_when_controller_rejects_it(monkeypatch, tmp_path) -> None:
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
    current.save()
    calls: list[bool] = []

    async def reconcile():
        calls.append(True)
        if len(calls) == 1:
            return {"status_code": 200, "body": {"ok": False, "error": "memory_clear_failed"}}
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"embedding": {"model": "embed-v2"}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 409
    assert response.get_json() == {"status": "failed", "error": "memory_clear_failed"}
    assert calls == [True, True]
    assert V2Config.load().memory.processing.embedding.model == "embed-v1"


def test_memory_embedding_change_delegates_marker_settlement_to_controller(monkeypatch, tmp_path) -> None:
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
    current.save()
    observed: list[tuple[str | None, bool]] = []

    async def reconcile():
        persisted = V2Config.load().memory
        observed.append((persisted.processing.embedding.model, persisted.embedding_change_pending))
        persisted.embedding_change_pending = False
        controller_config = V2Config.load()
        controller_config.memory = persisted
        controller_config.save()
        return {"status_code": 200, "body": {"ok": True, "state": "disabled"}}

    monkeypatch.setattr(internal_client, "reconcile_memory", reconcile)
    client = app.test_client()
    response = client.patch(
        "/api/memory/settings",
        json={"processing": {"embedding": {"model": "embed-v2"}}},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert observed == [("embed-v2", True)]
    persisted = V2Config.load().memory
    assert persisted.processing.embedding.model == "embed-v2"
    assert persisted.embedding_change_pending is False


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
