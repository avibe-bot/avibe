"""UI-process client for the controller-owned Model Hub service."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from core.handlers.model_hub import ModelHubError
from vibe import internal_client
from core.handlers.model_hub.usage import USAGE_DEFAULT_WINDOW_DAYS


_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    OSError,
    internal_client.InternalServerUnavailable,
)
# How long the UI process waits for any Model Hub RPC; the Web UI's request deadline stays beyond it.
MODEL_HUB_RPC_TIMEOUT_SECONDS = 300.0
_MODEL_HUB_RPC_PATH = "/internal/model-hub"
_RESET_CARD_USE_PATH = "/internal/model-hub-ui/reset-card"
# What the controller's use route answers by itself besides a result.
_RESET_CARD_USE_ANSWERS = frozenset(
    {
        (400, "invalid_parameter"),
        (404, "feature_disabled"),
        (409, "reset_card_locked"),
        (409, "reset_card_unavailable"),
        (409, "reset_card_count_changed"),
    }
)
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
    try:
        endpoint = internal_client._resolve_endpoint(None)
        transport = internal_client._sync_transport(endpoint)
        with httpx.Client(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(MODEL_HUB_RPC_TIMEOUT_SECONDS, connect=2.0),
        ) as client:
            response = client.post(
                "/internal/model-hub",
                json={"operation": operation, "payload": payload or {}},
            )
            internal_client._validate_response(response, endpoint)
    except _TRANSPORT_ERRORS:
        raise ModelHubError("engine_down", status=503) from None
    return _decode(response)


def engine_control_sync(action: str, *, operation_id: str | None = None) -> dict:
    """Local controller only, never direct install or restart on IPC failure."""
    if action not in {"status", "check", "update", "job"}:
        raise ValueError("Unknown engine operation")
    # The new verb cannot accidentally run a legacy controller's update before
    # we discover it lacks durable job polling. Keep its old verbs intact.
    if action in {"check", "update"}:
        operation, payload = "engine_control", {"action": action}
    else:
        operation, payload = "engine_" + action, {"operation_id": operation_id} if action == "job" else {}
    try:
        result = _rpc_sync(operation, payload)
    except ModelHubError as exc:
        if exc.status == 404 and exc.code != "job_not_found":
            raise ModelHubError("incompatible_controller", status=503) from exc
        raise
    if not isinstance(result, dict) or (action == "status" and "update_state" not in result) or (
        action in {"check", "update"} and not result.get("job_id")
    ):
        raise ModelHubError("incompatible_controller", status=503)
    return result


async def _rpc(operation: str, payload: Optional[dict[str, Any]] = None) -> Any:
    return await _post(_MODEL_HUB_RPC_PATH, {"operation": operation, "payload": payload or {}})


class _ControllerUnreached(ModelHubError):
    """The request was never handed to the controller, so nothing it asks for has begun."""

    def __init__(self) -> None:
        super().__init__("engine_down", status=503)


async def _post(path: str, body: dict[str, Any]) -> Any:
    try:
        endpoint = await internal_client._resolve_endpoint_async(None)
        client = httpx.AsyncClient(
            transport=internal_client._async_transport(endpoint),
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(MODEL_HUB_RPC_TIMEOUT_SECONDS, connect=2.0),
        )
    except _TRANSPORT_ERRORS:
        raise _ControllerUnreached() from None
    try:
        async with client:
            response = await client.post(path, json=body)
            internal_client._validate_response(response, endpoint)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise _ControllerUnreached() from None
    except _TRANSPORT_ERRORS:
        # Sent, and then lost: the controller may be doing what was asked.
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

    async def probe_source(self, source_id: str, payload: object) -> dict:
        return await _rpc("probe_source", {"source_id": source_id, "probe": payload})

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

    async def reenable_search_source(self, source_id: str) -> dict:
        return await _rpc(
            "reenable_search_source",
            {"source_id": source_id},
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
        renames: object = ...,
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
                **({"renames": renames} if renames is not ... else {}),
                "expected_suppliers": expected_suppliers,
                "force": force,
                "would_remove_hops": confirmed_remove_hops,
                "would_interrupt": confirmed_interruptions,
            },
        )

    def models_dev_matches(
        self,
        query: object,
        *,
        refresh_if_stale: bool = False,
    ) -> list[dict]:
        return _rpc_sync(
            "models_dev_matches",
            {"query": query, "refresh_if_stale": refresh_if_stale},
        )

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

    async def usage_summary(
        self,
        *,
        days: Optional[int] = None,
        window: Optional[str] = None,
    ) -> dict:
        # Async, unlike the other reads here: this one blocks on the lock the
        # ledger's writers hold across an fsync, so a sync call would hold a UI
        # worker for as long as the disk takes. See `usage.BoundedUsageLedger`.
        payload: dict[str, Any] = {}
        if days is not None:
            payload["days"] = days
        if window is not None:
            payload["window"] = window
        if not payload:
            payload["days"] = USAGE_DEFAULT_WINDOW_DAYS
        return await _rpc("usage_summary", payload)

    async def use_reset_card(self, source_id: str, payload: object) -> dict:
        """Ask the controller to use one reset card; only the Web route calls this.

        Checked here, in the Web process, before anything is sent: the controller
        receives a Source id and exactly ``{"kind": <kind>, "count": <count>}``,
        never the page's body. Its own route, not an RPC operation: the CLI's
        dispatcher cannot reach it.
        """

        from core.handlers.model_hub.card_action import (
            checked_selection,
            checked_source_id,
            empty_summary,
            unconfirmed_use,
        )

        kind, count = checked_selection(payload)
        body = {"source_id": checked_source_id(source_id), "selection": {"kind": kind, "count": count}}
        try:
            result = await _post(_RESET_CARD_USE_PATH, body)
            if isinstance(result, dict) and set(result) == {"quota", "use"}:
                return result
        except _ControllerUnreached:
            raise
        except ModelHubError as exc:
            if (exc.status, exc.code) in _RESET_CARD_USE_ANSWERS:
                raise
        except Exception:  # noqa: BLE001 - a lost answer is handled below, whatever lost it
            pass
        # The controller was given the request and no answer of its own came back:
        # the connection or the controller went away, or it failed. A card may have
        # been spent, and that is the closed "unconfirmed" result: not a generic
        # error, and not a second request. The fence holds the account meanwhile.
        try:
            quota = await self.quota_summary()
        except Exception:  # noqa: BLE001
            quota = empty_summary()
        return {"quota": quota, "use": unconfirmed_use(kind)}

    async def quota_summary(self, *, force: bool = False) -> dict:
        return await _rpc("quota_summary", {"force": force})

    async def hosted_tools_status(self) -> dict:
        return await _rpc("hosted_tools_status", {})

    async def set_hosted_tool(self, tool: str, setting: object) -> dict:
        return await _rpc("set_hosted_tool", {"tool": tool, "setting": setting})

    async def clear_hosted_refusals(self, tool: str, target: object) -> dict:
        return await _rpc("clear_hosted_refusals", {"tool": tool, "target": target})

    async def clear_service_cooldown(self, source_id: str, service_id: str | None = None, tool: str = "web_search") -> dict:
        payload = {"source_id": source_id, "service_id": service_id}
        if tool != "web_search":
            payload["tool"] = tool
        return await _rpc("clear_service_cooldown", payload)

    async def clear_source_evidence(self, source_id: str, target: object) -> dict:
        return await _rpc("clear_source_evidence", {"source_id": source_id, "target": target})

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

    async def migration_apply(self, item_ids: object, clean_api_keys: object = False) -> dict:
        return await _rpc("migration_apply", {"item_ids": item_ids, "clean_api_keys": clean_api_keys})

    async def engine_status(self) -> dict:
        return await _rpc("engine_status")

    async def engine_check(self) -> dict:
        return await _rpc("engine_check")

    async def engine_update(self) -> dict:
        return await _rpc("engine_update")

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
