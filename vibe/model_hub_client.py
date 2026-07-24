"""UI-process client for the controller-owned Model Hub service."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from core.handlers.model_hub import ModelHubError
from vibe.internal_client import default_socket_path


_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.TimeoutException, OSError)
_RPC_TIMEOUT_SECONDS = 300.0


def _decode(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        raise ModelHubError("engine_down", status=503) from None
    if not isinstance(body, dict):
        raise ModelHubError("engine_down", status=503)
    if response.status_code >= 400 or body.get("ok") is not True:
        code = body.get("error")
        detail = body.get("detail")
        error = ModelHubError(
            code if isinstance(code, str) else "engine_down",
            status=response.status_code if response.status_code >= 400 else 503,
        )
        if isinstance(detail, str):
            error.detail = detail
        raise error
    return body.get("result")


def _rpc_sync(operation: str, payload: Optional[dict[str, Any]] = None) -> Any:
    target = default_socket_path().expanduser().resolve()
    if not target.exists():
        raise ModelHubError("engine_down", status=503)
    transport = httpx.HTTPTransport(uds=str(target))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(_RPC_TIMEOUT_SECONDS, connect=2.0),
        ) as client:
            response = client.post(
                "/internal/model-hub",
                json={"operation": operation, "payload": payload or {}},
            )
    except _TRANSPORT_ERRORS:
        raise ModelHubError("engine_down", status=503) from None
    return _decode(response)


async def _rpc(operation: str, payload: Optional[dict[str, Any]] = None) -> Any:
    target = default_socket_path().expanduser().resolve()
    if not target.exists():
        raise ModelHubError("engine_down", status=503)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(_RPC_TIMEOUT_SECONDS, connect=2.0),
        ) as client:
            response = await client.post(
                "/internal/model-hub",
                json={"operation": operation, "payload": payload or {}},
            )
    except _TRANSPORT_ERRORS:
        raise ModelHubError("engine_down", status=503) from None
    return _decode(response)


class ModelHubRemoteService:
    """Mirror the UI-facing service API without owning config or engine state."""

    def list_sources(self) -> list[dict]:
        return _rpc_sync("list_sources")

    async def create_source(self, payload: dict) -> dict:
        return await _rpc("create_source", {"source": payload})

    async def patch_source(self, source_id: str, payload: dict) -> dict:
        return await _rpc("patch_source", {"source_id": source_id, "patch": payload})

    async def delete_source(self, source_id: str, *, force: bool = False) -> None:
        await _rpc("delete_source", {"source_id": source_id, "force": force})

    async def test_source(self, source_id: str) -> tuple[dict, int]:
        result = await _rpc("test_source", {"source_id": source_id})
        return result["source"], result["discovered"]

    def priority(self) -> dict:
        return _rpc_sync("priority")

    async def set_priority(self, order: object) -> dict:
        return await _rpc("set_priority", {"order": order})

    def list_agents(self) -> list[dict]:
        return _rpc_sync("list_agents")

    async def set_agent_mode(self, backend: str, mode: object) -> dict:
        return await _rpc("set_agent_mode", {"backend": backend, "mode": mode})

    async def set_mappings(self, backend: str, mappings: object) -> dict:
        return await _rpc("set_mappings", {"backend": backend, "mappings": mappings})

    async def set_opencode_menu(self, menu: object) -> dict:
        return await _rpc("set_opencode_menu", {"menu": menu})

    async def add_custom_model(self, payload: dict) -> dict:
        return await _rpc("add_custom_model", {"model": payload})

    async def delete_custom_model(self, source_id: object, model_id: object) -> dict:
        return await _rpc(
            "delete_custom_model",
            {"source_id": source_id, "model_id": model_id},
        )

    def list_events(self, *, limit: int = 20, before: Optional[str] = None) -> list[dict]:
        return _rpc_sync("list_events", {"limit": limit, "before": before})

    async def oauth_start(self, payload: dict) -> dict:
        return await _rpc("oauth_start", {"oauth": payload})

    async def oauth_status(self, flow_id: str) -> dict:
        return await _rpc("oauth_status", {"flow_id": flow_id})

    async def oauth_submit(self, payload: dict) -> dict:
        return await _rpc("oauth_submit", {"oauth": payload})

    async def oauth_cancel(self, flow_id: object) -> None:
        await _rpc("oauth_cancel", {"flow_id": flow_id})

    def migration_scan(self) -> dict:
        return _rpc_sync("migration_scan")

    async def migration_apply(self, item_ids: object) -> dict:
        return await _rpc("migration_apply", {"item_ids": item_ids})

    async def runtime_status(self) -> dict:
        return await _rpc("runtime_status")
