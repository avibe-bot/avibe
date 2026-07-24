"""Allowlisted RPC surface for the controller-owned Model Hub service."""

from __future__ import annotations

from typing import Any

from .service import ModelHubError, ModelHubService


async def dispatch_model_hub_rpc(
    service: ModelHubService,
    operation: str,
    payload: dict[str, Any],
) -> Any:
    if operation == "list_sources":
        return service.list_sources()
    if operation == "create_source":
        return await service.create_source(payload.get("source"))
    if operation == "patch_source":
        return await service.patch_source(payload.get("source_id"), payload.get("patch"))
    if operation == "delete_source":
        await service.delete_source(payload.get("source_id"), force=payload.get("force") is True)
        return None
    if operation == "test_source":
        source, discovered = await service.test_source(payload.get("source_id"))
        return {"source": source, "discovered": discovered}
    if operation == "priority":
        return service.priority()
    if operation == "set_priority":
        return await service.set_priority(payload.get("order"))
    if operation == "list_agents":
        return service.list_agents()
    if operation == "set_agent_mode":
        return await service.set_agent_mode(payload.get("backend"), payload.get("mode"))
    if operation == "set_mappings":
        return await service.set_mappings(payload.get("backend"), payload.get("mappings"))
    if operation == "set_opencode_menu":
        return await service.set_opencode_menu(payload.get("menu"))
    if operation == "add_custom_model":
        return await service.add_custom_model(payload.get("model"))
    if operation == "delete_custom_model":
        return await service.delete_custom_model(payload.get("source_id"), payload.get("model_id"))
    if operation == "list_events":
        return service.list_events(limit=payload.get("limit", 20), before=payload.get("before"))
    if operation == "oauth_start":
        return await service.oauth_start(payload.get("oauth"))
    if operation == "oauth_status":
        return await service.oauth_status(payload.get("flow_id"))
    if operation == "oauth_submit":
        return await service.oauth_submit(payload.get("oauth"))
    if operation == "oauth_cancel":
        await service.oauth_cancel(payload.get("flow_id"))
        return None
    if operation == "migration_scan":
        return service.migration_scan()
    if operation == "migration_apply":
        return await service.migration_apply(payload.get("item_ids"))
    if operation == "runtime_status":
        return await service.runtime_status()
    raise ModelHubError("source_not_found", status=404)
