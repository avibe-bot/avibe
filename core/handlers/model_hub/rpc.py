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
    if operation == "replace_credential":
        return await service.replace_credential(
            payload.get("source_id"),
            payload.get("credential"),
        )
    if operation == "reauth_source":
        return await service.reauth_source(
            payload.get("source_id"),
            payload.get("reauth"),
        )
    if operation == "delete_source":
        await service.delete_source(payload.get("source_id"), force=payload.get("force") is True)
        return None
    if operation == "refresh_source":
        source, discovered = await service.refresh_source(payload.get("source_id"))
        return {"source": source, "discovered": discovered}
    if operation == "list_agents":
        return service.list_agents()
    if operation == "get_agent_sources":
        return service.get_agent_sources(payload.get("backend"))
    if operation == "set_agent_sources":
        return await service.set_agent_sources(
            payload.get("backend"),
            payload.get("sources"),
        )
    if operation == "set_agent_mode":
        return await service.set_agent_mode(payload.get("backend"), payload.get("mode"))
    if operation == "set_mappings":
        return await service.set_mappings(payload.get("backend"), payload.get("mappings"))
    if operation == "set_opencode_menu":
        return await service.set_opencode_menu(payload.get("menu"))
    if operation == "add_custom_model":
        return await service.add_custom_model(payload.get("source_id"), payload.get("model"))
    if operation == "update_model_reasoning_efforts":
        return await service.update_model_reasoning_efforts(
            payload.get("source_id"),
            payload.get("model_id"),
            payload.get("model"),
        )
    if operation == "delete_custom_model":
        return await service.delete_custom_model(
            payload.get("source_id"),
            payload.get("model_id"),
            force=payload.get("force") is True,
        )
    if operation == "list_events":
        return service.list_events(limit=payload.get("limit", 20), before=payload.get("before"))
    if operation == "get_agent_chain":
        return service.agent_chain(payload.get("backend"), payload.get("model_id"))
    if operation == "probe_agent":
        return await service.probe_agent(
            payload.get("backend"),
            payload.get("model_id"),
        )
    if operation == "get_turn_provenance":
        return service.get_turn_provenance(payload.get("turn_id"))
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
    if operation == "runtime_start":
        return await service.runtime_start()
    raise ModelHubError("source_not_found", status=404)
