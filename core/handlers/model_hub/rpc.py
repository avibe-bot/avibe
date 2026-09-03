"""Allowlisted RPC surface for the controller-owned Model Hub service."""

from __future__ import annotations

import asyncio
from typing import Any
from weakref import WeakKeyDictionary

from .service import BackendName, ModelHubError, ModelHubService
from .usage import USAGE_DEFAULT_WINDOW_DAYS

_cli_presence_refresh_tasks: WeakKeyDictionary[
    ModelHubService,
    dict[tuple[BackendName, ...] | None, asyncio.Task[None]],
] = WeakKeyDictionary()


async def _refresh_agent_presence(
    service: ModelHubService,
    backends: tuple[BackendName, ...] | None = None,
) -> None:
    """Refresh host CLI facts without blocking the controller event loop."""

    tasks = _cli_presence_refresh_tasks.setdefault(service, {})
    current = tasks.get(backends)
    if current is not None and not current.done():
        await asyncio.shield(current)
        return

    task = _start_agent_presence_refresh(service, backends)
    await asyncio.shield(task)


async def _run_agent_presence_refresh(
    service: ModelHubService,
    backends: tuple[BackendName, ...] | None,
) -> None:
    try:
        await asyncio.to_thread(
            service.refresh_cli_presence,
            include_npm_global=True,
            backends=backends,
        )
    finally:
        current = asyncio.current_task()
        tasks = _cli_presence_refresh_tasks.get(service)
        if tasks is not None and tasks.get(backends) is current:
            tasks.pop(backends, None)
            if not tasks:
                _cli_presence_refresh_tasks.pop(service, None)


def _start_agent_presence_refresh(
    service: ModelHubService,
    backends: tuple[BackendName, ...] | None,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _run_agent_presence_refresh(service, backends),
        name="model-hub-cli-presence-refresh",
    )
    _cli_presence_refresh_tasks.setdefault(service, {})[backends] = task
    return task


async def _refresh_payload_backend(
    service: ModelHubService,
    backend: object,
) -> None:
    if backend in ("claude", "codex", "opencode"):
        await _refresh_agent_presence(service, (backend,))
        if backend in ("claude", "codex"):
            await service.reconcile_builtin_models((backend,))


async def _reconcile_payload_backend(
    service: ModelHubService,
    backend: object,
) -> None:
    if backend in ("claude", "codex"):
        await service.reconcile_builtin_models((backend,))


async def dispatch_model_hub_rpc(
    service: ModelHubService,
    operation: str,
    payload: dict[str, Any],
) -> Any:
    if operation == "list_sources":
        return service.list_sources()
    if operation == "observe_source":
        return await service.observe_source(payload.get("observation"))
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
        return await service.delete_source(
            payload.get("source_id"),
            force=payload.get("force") is True,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
    if operation == "refresh_source":
        return await service.refresh_source(
            payload.get("source_id"),
            force=payload.get("force") is True,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
    if operation == "list_agents":
        if payload.get("refresh_cli_presence") is True:
            await _refresh_agent_presence(service)
        await service.reconcile_builtin_models()
        return await asyncio.to_thread(service.list_agents)
    if operation == "get_agent_sources":
        backend = payload.get("backend")
        await _reconcile_payload_backend(service, backend)
        return await asyncio.to_thread(
            service.get_agent_sources,
            backend,
        )
    if operation == "agent_model_candidates":
        backend = payload.get("backend")
        await _reconcile_payload_backend(service, backend)
        return await asyncio.to_thread(service.agent_model_candidates, backend)
    if operation == "set_agent_sources":
        await _refresh_payload_backend(service, payload.get("backend"))
        return await service.set_agent_sources(
            payload.get("backend"),
            payload.get("sources"),
        )
    if operation == "reorder_agent_chains":
        await _refresh_payload_backend(service, payload.get("backend"))
        if "order" in payload:
            return await service.reorder_agent_chains(
                payload.get("backend"),
                payload.get("order"),
            )
        return await service.reorder_agent_chains(payload.get("backend"))
    if operation == "set_agent_mode":
        await _refresh_payload_backend(service, payload.get("backend"))
        return await service.set_agent_mode(payload.get("backend"), payload.get("mode"))
    if operation == "set_agent_models":
        await _refresh_payload_backend(service, payload.get("backend"))
        backend = payload.get("backend")
        return await service.set_agent_models(
            backend,
            payload.get("baseline"),
            payload.get("models"),
            expected_suppliers=payload.get("expected_suppliers"),
            force=payload.get("force") is True,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
    if operation == "models_dev_matches":
        return await asyncio.to_thread(
            service.models_dev_matches,
            payload.get("query"),
        )
    if operation == "set_agent_chain":
        await _reconcile_payload_backend(service, payload.get("backend"))
        return await service.set_agent_chain(
            payload.get("backend"),
            payload.get("model_id"),
            payload.get("chain"),
        )
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
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
    if operation == "list_events":
        return service.list_events(limit=payload.get("limit", 20), before=payload.get("before"))
    if operation == "usage_summary":
        # Reads the ledger file and the config store, and takes the same lock a
        # concurrent `record()` holds across `fsync()`. On the controller loop
        # that is every turn on this machine waiting on one settings page.
        return await asyncio.to_thread(
            service.usage_summary,
            days=payload.get("days", USAGE_DEFAULT_WINDOW_DAYS),
        )
    if operation == "get_agent_chain":
        await _reconcile_payload_backend(service, payload.get("backend"))
        return service.agent_chain(payload.get("backend"), payload.get("model_id"))
    if operation == "get_agent_chains":
        await _reconcile_payload_backend(service, payload.get("backend"))
        return service.agent_chains(payload.get("backend"))
    if operation == "get_opencode_public_models":
        return service.opencode_public_models()
    if operation == "probe_agent":
        await _reconcile_payload_backend(service, payload.get("backend"))
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
    if operation == "runtime_install":
        return await service.runtime_install()
    if operation == "runtime_start":
        return await service.runtime_start()
    if operation == "runtime_stop":
        return await service.runtime_stop()
    raise ModelHubError("source_not_found", status=404)
