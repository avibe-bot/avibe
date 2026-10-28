"""One settings change to one channel, Telegram topic, or bound DM user.

Every surface that edits a scope writes through ``apply_scope_change``: the Web
settings pages (``PATCH /api/settings/scope``) and the IM settings handlers
(through ``SettingsManager``). It writes only the fields a change names, on the
rows read under the settings write lock, and stores a route through the one
``normalize_scope_routing``, so the same choice is stored the same way whichever
surface made it. See ``docs/plans/scope-settings-patch.md``.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import fields
from typing import Any, Optional

from config.platform_registry import get_platform_descriptor
from config.v2_settings import (
    ChannelSettings,
    RoutingSettings,
    SettingsState,
    SettingsStore,
    UserSettings,
    _UNSET_AGENT_BINDING,
    _make_scoped_key,
    _parse_routing,
    _routing_to_dict,
    make_thread_native_id,
    normalize_routing_settings,
    normalize_show_message_types,
    routing_to_compat_dict,
)

logger = logging.getLogger(__name__)

SCOPE_KINDS = ("channel", "thread", "user")
CHANNEL_FIELDS = frozenset({"enabled", "show_message_types", "custom_cwd", "require_mention", "require_bind", "routing"})
SCOPE_FIELDS = {
    "channel": CHANNEL_FIELDS,
    "thread": CHANNEL_FIELDS,
    "user": frozenset({"display_name", "enabled", "show_message_types", "custom_cwd", "routing"}),
}
ROUTING_FIELDS = frozenset(field.name for field in fields(RoutingSettings))
MISSING: Any = object()


class ScopeNotFoundError(LookupError):
    """A change names a DM user that is not bound."""

    def __init__(self, scope_id: str) -> None:
        super().__init__(scope_id)
        self.scope_id = scope_id


def normalize_scope_message_types(show_message_types: Optional[list], platform: str) -> list[str]:
    """The message types a scope on ``platform`` can show."""
    normalized = normalize_show_message_types(show_message_types)
    if not get_platform_descriptor(platform).capabilities.supports_toolcall_delivery:
        return [msg_type for msg_type in normalized if msg_type != "toolcall"]
    return normalized


def _route_agent_backend_and_model(agent_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    from core.vibe_agents import VibeAgentStore
    from modules.agents.catalog import is_agent_backend

    name = str(agent_name or "").strip()
    if not name:
        return None, None
    store = VibeAgentStore()
    try:
        agent = store.get(name)
        if agent is not None:
            return agent.backend, agent.model
    except Exception:
        logger.debug("Failed to resolve Agent backend while normalizing routing payload", exc_info=True)
    finally:
        store.close()
    return (name, None) if is_agent_backend(name) else (None, None)


def normalize_scope_routing(routing_payload: dict) -> dict:
    """The route a scope stores for ``routing_payload``.

    Claude Code rejects an effort its model does not support, so a Claude
    route keeps an effort only when that model offers it: from the Model Hub
    catalog when Claude runs through the Hub, else from the native catalog.
    """
    from config.v2_config import is_model_hub_enabled
    from core.handlers.model_hub.service import V2ModelHubConfigStore
    from modules.agents.opencode.utils import normalize_claude_reasoning_effort
    from vibe import backend_model_catalog

    routing = _parse_routing(routing_payload or {})
    backend, agent_model = _route_agent_backend_and_model(routing.agent_name)
    if backend == "claude" and routing.reasoning_effort:
        model = routing.model or agent_model
        catalog_efforts = None
        agent_catalog = V2ModelHubConfigStore().load().agents["claude"] if is_model_hub_enabled() else None
        if agent_catalog is not None and agent_catalog.mode == "hub":
            row = next((row for row in agent_catalog.models if row.id == model), None)
            if row is not None:
                # Match the picker: a present empty list (or non-reasoning
                # model) is authoritative, not a request for backend tiers.
                catalog_efforts = [] if row.supports_reasoning is False else row.reasoning_efforts
            # Unknown Hub rows retain the existing generic normalization;
            # a same-named native model must not grant them Off support.
        else:
            catalog_efforts = backend_model_catalog.catalog_reasoning_efforts_for_model("claude", model)
        routing.reasoning_effort = normalize_claude_reasoning_effort(
            model,
            routing.reasoning_effort,
            catalog_efforts,
        )
    return _routing_to_dict(routing)


def _stored_agent_name(settings: ChannelSettings | UserSettings) -> Optional[str]:
    stored = settings._agent_name_at_load
    return settings.routing.agent_name if stored is _UNSET_AGENT_BINDING else stored


def apply_scope_change(
    store: SettingsStore,
    *,
    platform: str,
    kind: str,
    native_id: str,
    thread_id: str = "",
    changes: dict,
    expected_agent_name: Any = MISSING,
    new_topic_require_mention: Optional[bool] = None,
    user_context: Any = None,
) -> ChannelSettings | UserSettings:
    """Write ``changes`` to one scope and return its stored row.

    Only the named fields are written, so a caller holding an old copy of the
    scope cannot revert what another writer changed since. ``routing`` merges
    key by key into the stored route. A routing change names the Agent the
    caller's view of the route was based on (``expected_agent_name``): when the
    stored Agent differs, a model or effort chosen for the old Agent must not
    land, so the change is refused unless it sets the stored Agent itself.

    A channel or topic without a row is created; a topic starts from its parent
    channel and keeps the mention gate it inherited, the parent's or else
    ``new_topic_require_mention``. A DM user must already be bound.
    """
    from storage.settings_service import StaleScopeAgentBindingError, make_scope_id

    if kind not in SCOPE_KINDS:
        raise ValueError(f"unknown scope kind: {kind}")
    unknown = sorted(set(changes) - SCOPE_FIELDS[kind])
    if unknown:
        raise ValueError(f"unknown {kind} fields: {', '.join(unknown)}")
    routing_patch = changes.get("routing")
    if routing_patch is not None and expected_agent_name is MISSING:
        raise ValueError("a routing change names the Agent it was based on")
    stored_native_id = make_thread_native_id(native_id, thread_id) if kind == "thread" else native_id
    key = _make_scoped_key(platform, stored_native_id)
    scope_id = make_scope_id(platform, kind, stored_native_id)
    parent_key = _make_scoped_key(platform, native_id)

    def rows(state: SettingsState) -> dict:
        return {"user": state.users, "channel": state.channels, "thread": state.threads}[kind]

    def source_of(state: SettingsState) -> ChannelSettings | UserSettings:
        """The scope's stored settings, or what a new row starts from."""
        existing = rows(state).get(key)
        if existing is not None:
            return existing
        if kind == "user":
            raise ScopeNotFoundError(scope_id)
        if kind == "thread":
            parent = state.channels.get(parent_key)
            if parent is not None:
                return parent
        return ChannelSettings()

    # The route is resolved before the write lock: normalizing an effort reads
    # Agent and model catalogs, which must not hold other writers up.
    routing_changes: Optional[dict] = None
    if routing_patch is not None:
        stored_routing = routing_to_compat_dict(source_of(store.settings).routing)
        normalized = normalize_scope_routing({**stored_routing, **routing_patch})
        routing_changes = {
            name: value
            for name, value in normalized.items()
            if name in routing_patch or value != stored_routing.get(name)
        }

    def mutate(state: SettingsState) -> None:
        existing = rows(state).get(key)
        source = source_of(state)
        stored_agent = _stored_agent_name(source)
        if routing_changes is not None and expected_agent_name != stored_agent:
            if routing_patch.get("agent_name", MISSING) != stored_agent:
                raise StaleScopeAgentBindingError(scope_id=scope_id)
        if existing is not None:
            updated = existing
        else:
            updated = ChannelSettings(
                enabled=source.enabled,
                show_message_types=list(source.show_message_types),
                custom_cwd=source.custom_cwd,
                routing=copy.deepcopy(source.routing),
                require_mention=source.require_mention,
                require_bind=source.require_bind,
                # A new row's Agent is a new binding: the save validates it.
                _agent_name_at_load=None,
            )
        for name, value in changes.items():
            if name == "routing":
                continue
            if name == "show_message_types":
                value = normalize_scope_message_types(value, platform)
            setattr(updated, name, value)
        if routing_changes:
            merged = {**_routing_to_dict(updated.routing), **routing_changes}
            updated.routing = normalize_routing_settings(RoutingSettings(**merged))
        if kind == "thread" and existing is None and updated.require_mention is None:
            # An inheriting topic's first override keeps the gate it showed.
            inherited = source.require_mention
            updated.require_mention = inherited if inherited is not None else new_topic_require_mention
        rows(state)[key] = updated

    store.apply(mutate, user_context=user_context)
    return rows(store.settings)[key]
