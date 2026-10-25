"""Dormant fetch settings and catalog projections (WF-1); no request executor."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from config.v2_config import (
    ModelHubConfig, ModelHubFetchServiceSettings, ModelHubHostedToolConfig,
    hosted_tool_setting, is_model_hub_source_id, model_hub_builtin_sources,
)
from vibe.model_hub_runtime.fetch_catalog import fetch_service, fetch_service_catalog


def fetch_service_settings(setting: ModelHubHostedToolConfig, source: Any) -> dict | None:
    service = fetch_service(source)
    return setting.services.get(source.id, ModelHubFetchServiceSettings()).effective(service) if service else None


def fetch_candidates(config: ModelHubConfig) -> tuple:
    """Independent fetch order: local, creation-ordered services, model Sources, Exa, Jina."""
    from .hosted_tools import ExecutorCandidate, _vibey_order

    setting = hosted_tool_setting(config, "web_fetch")
    by_id = {source.id: source for source in config.sources}
    service_order = hosted_tool_setting(config, "web_search").service_order or ()
    first = [by_id[sid] for sid in service_order if sid in by_id and by_id[sid].protocol == "search"]
    sources = first + [s for s in config.sources if s.protocol == "search" and s not in first]
    sources += [s for s in _vibey_order(config) if s.protocol != "search"]
    builtins = model_hub_builtin_sources()
    sources = [s for s in builtins if fetch_service(s)["kind"] == "local"] + sources
    sources += [s for s in builtins if fetch_service(s)["kind"] != "local"]
    automatic = {}
    disabled = []
    for source in sources:
        service = fetch_service(source)
        if service is None or not service["available"]:
            continue
        settings = fetch_service_settings(setting, source)
        candidate = ExecutorCandidate(source, service["service_id"], {}, (), kind=service["kind"], settings=settings)
        if settings["enabled"]:
            automatic[source.id] = candidate
        else:
            disabled.append(candidate)
    kinds = setting.executor_order_kinds or {}
    named = [automatic[sid] for sid in (setting.executor_order or ())
             if sid in automatic and kinds.get(sid) == automatic[sid].kind]
    named_ids = {c.source.id for c in named}
    additions = [c for sid, c in automatic.items() if sid not in named_ids]
    local = [c for c in additions if c.kind == "local"]
    configured = [c for c in additions if c.kind != "local" and not fetch_service(c.source).get("source_id")]
    builtins = [c for c in additions if c.kind != "local" and fetch_service(c.source).get("source_id")]
    position = next((i for i, c in enumerate(named) if c.kind == "service" and fetch_service(c.source).get("source_id")), len(named))
    return tuple(local + named[:position] + configured + named[position:] + builtins + disabled)


def fetch_status(config: ModelHubConfig, *, status: dict, cooldown_until) -> dict:
    setting = hosted_tool_setting(config, "web_fetch")
    sources = {s.id: s for s in [*config.sources, *model_hub_builtin_sources()]}
    chain = fetch_candidates(config)
    vendors = []
    for service in fetch_service_catalog():
        if not service["available"]:
            continue
        vendors.append({
            "id": service["vendor"], "name": service["label"], "price": service["price"],
            "capabilities": ["web_fetch"], "content_types": service["content_types"],
            "source_ids": [s.id for s in sources.values() if fetch_service(s) == service],
            "facts": {"built_in": bool(service.get("source_id")), "requires_setup": not bool(service.get("source_id")),
                      "auto_enroll": service["auto_enroll"]},
        })
    return {
        "name": "web_fetch", "enabled": setting.enabled, "status": status,
        "executor_order": list(setting.executor_order) if setting.executor_order is not None else None,
        "services": {sid: saved.to_payload() for sid, saved in setting.services.items()
                     if sid in sources and fetch_service(sources[sid]) is not None},
        "effective_order": [c.source.id for c in chain],
        "executor_candidates": [{
            "kind": c.kind, "source_id": c.source.id, "model_id": c.model_id, "billing": "metered", "relay": False,
            "price": fetch_service(c.source)["price"], "settings": c.settings, "state": c.source.state.to_payload(),
            "trial_pending": False,
            "cooldown_until": cooldown_until(c.source.id, c.model_id) if c.kind != "local" else None,
        } for c in chain],
        "service_vendors": vendors,
    }


def patch_fetch_services(config: ModelHubConfig, payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("services must be a Source-keyed patch")
    saved = dict(hosted_tool_setting(config, "web_fetch").services)
    sources = {s.id: s for s in [*config.sources, *model_hub_builtin_sources()]}
    for sid, patch in payload.items():
        if not is_model_hub_source_id(sid):
            raise ValueError("services keys must be Source ids")
        if patch is None:
            saved.pop(sid, None)
            continue
        if sid not in sources or fetch_service(sources[sid]) is None or not isinstance(patch, dict) or set(patch) - {"enabled"}:
            raise ValueError("services must name a fetch-capable Source and known fields")
        merged = saved.get(sid, ModelHubFetchServiceSettings()).to_payload()
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        parsed = ModelHubFetchServiceSettings.from_payload(merged)
        if parsed.to_payload():
            saved[sid] = parsed
        else:
            saved.pop(sid, None)
    return saved


def fetch_order_roles(config: ModelHubConfig, setting: ModelHubHostedToolConfig) -> ModelHubHostedToolConfig:
    sources = {s.id: s for s in [*config.sources, *model_hub_builtin_sources()]}
    roles = {sid: service["kind"] for sid in (setting.executor_order or ())
             if sid in sources and (service := fetch_service(sources[sid])) is not None}
    return replace(setting, executor_order_kinds=roles if setting.executor_order is not None else None)
