"""UI-process client for the controller-owned Model Hub service."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from core.handlers.model_hub import ModelHubError
from core.handlers.model_hub.usage import USAGE_DEFAULT_WINDOW_DAYS
from vibe.internal_client import default_socket_path


_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.TimeoutException, OSError)
MODEL_HUB_RPC_TIMEOUT_SECONDS = 300.0
_REORDER_ORDER_UNSET = object()


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
        data = {key: value for key, value in body.items() if key not in {"ok", "error", "detail", "contract_version"}}
        error = ModelHubError(
            code if isinstance(code, str) else "engine_down",
            status=response.status_code if response.status_code >= 400 else 503,
            data=data,
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
            timeout=httpx.Timeout(MODEL_HUB_RPC_TIMEOUT_SECONDS, connect=2.0),
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
            timeout=httpx.Timeout(MODEL_HUB_RPC_TIMEOUT_SECONDS, connect=2.0),
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

    async def observe_source(self, payload: dict) -> dict:
        return await _rpc("observe_source", {"observation": payload})

    async def create_source(self, payload: dict) -> dict:
        return await _rpc("create_source", {"source": payload})

    async def patch_source(self, source_id: str, payload: dict) -> dict:
        return await _rpc("patch_source", {"source_id": source_id, "patch": payload})

    async def replace_credential(self, source_id: str, payload: object) -> dict:
        return await _rpc(
            "replace_credential",
            {"source_id": source_id, "credential": payload},
        )

    async def reauth_source(self, source_id: str, payload: object) -> dict:
        return await _rpc(
            "reauth_source",
            {"source_id": source_id, "reauth": payload},
        )

    async def delete_source(
        self,
        source_id: str,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        return await _rpc(
            "delete_source",
            {
                "source_id": source_id,
                "force": force,
                "would_remove_hops": confirmed_remove_hops,
                "would_interrupt": confirmed_interruptions,
            },
        )

    async def refresh_source(
        self,
        source_id: str,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        return await _rpc(
            "refresh_source",
            {
                "source_id": source_id,
                "force": force,
                "would_remove_hops": confirmed_remove_hops,
                "would_interrupt": confirmed_interruptions,
            },
        )

    def list_agents(self, *, refresh_cli_presence: bool = False) -> list[dict]:
        return _rpc_sync(
            "list_agents",
            {"refresh_cli_presence": True} if refresh_cli_presence else None,
        )

    def get_agent_sources(self, backend: str) -> dict:
        return _rpc_sync("get_agent_sources", {"backend": backend})

    def agent_model_candidates(self, backend: str) -> dict:
        return _rpc_sync("agent_model_candidates", {"backend": backend})

    async def set_agent_sources(self, backend: str, sources: object) -> dict:
        return await _rpc(
            "set_agent_sources",
            {"backend": backend, "sources": sources},
        )

    async def reorder_agent_chains(
        self,
        backend: str,
        order: object = _REORDER_ORDER_UNSET,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        payload = {"backend": backend}
        if order is not _REORDER_ORDER_UNSET:
            payload["order"] = order
            if force:
                payload["force"] = force
            if confirmed_remove_hops is not None:
                payload["would_remove_hops"] = confirmed_remove_hops
            if confirmed_interruptions is not None:
                payload["would_interrupt"] = confirmed_interruptions
        return await _rpc("reorder_agent_chains", payload)

    async def set_agent_mode(self, backend: str, mode: object) -> dict:
        return await _rpc("set_agent_mode", {"backend": backend, "mode": mode})

    async def set_agent_models(
        self,
        backend: str,
        baseline: object,
        models: object,
        *,
        expected_suppliers: object = None,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        return await _rpc(
            "set_agent_models",
            {
                "backend": backend,
                "baseline": baseline,
                "models": models,
                "expected_suppliers": expected_suppliers,
                "force": force,
                "would_remove_hops": confirmed_remove_hops,
                "would_interrupt": confirmed_interruptions,
            },
        )

    def models_dev_matches(self, query: object) -> list[dict]:
        return _rpc_sync("models_dev_matches", {"query": query})

    async def set_agent_chain(self, backend: str, model_id: str, chain: object) -> dict:
        return await _rpc(
            "set_agent_chain",
            {"backend": backend, "model_id": model_id, "chain": chain},
        )

    async def delete_agent_chain(self, backend: str, model_id: str, chain: object = None) -> dict:
        return await _rpc(
            "delete_agent_chain", {"backend": backend, "model_id": model_id, "chain": chain},
        )

    def preview_agent_chain(self, backend: str, model_id: str, chain: object) -> dict:
        return _rpc_sync(
            "preview_agent_chain", {"backend": backend, "model_id": model_id, "chain": chain},
        )

    async def add_custom_model(self, source_id: object, payload: dict) -> dict:
        return await _rpc(
            "add_custom_model",
            {"source_id": source_id, "model": payload},
        )

    async def update_model_reasoning_efforts(
        self,
        source_id: object,
        model_id: object,
        payload: dict,
    ) -> dict:
        return await _rpc(
            "update_model_reasoning_efforts",
            {"source_id": source_id, "model_id": model_id, "model": payload},
        )

    async def delete_custom_model(
        self,
        source_id: object,
        model_id: object,
        *,
        force: bool = False,
        confirmed_remove_hops: object = None,
        confirmed_interruptions: object = None,
    ) -> dict:
        return await _rpc(
            "delete_custom_model",
            {
                "source_id": source_id,
                "model_id": model_id,
                "force": force,
                "would_remove_hops": confirmed_remove_hops,
                "would_interrupt": confirmed_interruptions,
            },
        )

    def list_events(self, *, limit: int = 20, before: Optional[str] = None) -> list[dict]:
        return _rpc_sync("list_events", {"limit": limit, "before": before})

    async def usage_summary(self, *, days: int = USAGE_DEFAULT_WINDOW_DAYS) -> dict:
        # Async, unlike the other reads here: this one blocks on the lock the
        # ledger's writers hold across an fsync, so a sync call would hold a UI
        # worker for as long as the disk takes. See `usage.BoundedUsageLedger`.
        return await _rpc("usage_summary", {"days": days})

    def agent_chain(self, backend: str, model_id: str) -> dict:
        return _rpc_sync(
            "get_agent_chain",
            {"backend": backend, "model_id": model_id},
        )

    def agent_chains(self, backend: str) -> list[dict]:
        return _rpc_sync("get_agent_chains", {"backend": backend})

    def opencode_public_models(self) -> dict[str, dict[str, Any]]:
        return _rpc_sync("get_opencode_public_models")

    async def probe_agent(
        self,
        backend: str,
        model_id: Optional[str] = None,
    ) -> dict:
        return await _rpc(
            "probe_agent",
            {"backend": backend, "model_id": model_id},
        )

    def get_turn_provenance(self, turn_id: str) -> dict:
        return _rpc_sync("get_turn_provenance", {"turn_id": turn_id})

    def get_model_provenance(self, backend: str, model_id: str) -> dict | None:
        return _rpc_sync("get_model_provenance", {"backend": backend, "model_id": model_id})

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

    async def runtime_install(self) -> dict:
        return await _rpc("runtime_install")

    def ensure_runtime_dependency(
        self,
        *,
        force: bool = False,
        offline: bool = False,
    ) -> dict:
        return _rpc_sync(
            "runtime_ensure_dependency",
            {"force": force, "offline": offline},
        )

    async def runtime_start(self) -> dict:
        return await _rpc("runtime_start")

    async def runtime_stop(self) -> dict:
        return await _rpc("runtime_stop")
