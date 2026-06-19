"""Resolve the concrete execution target for an agent turn.

This module is the shared boundary between product surfaces (IM, Workbench,
scheduled follow-ups) and backend agents.  The key rule is that an existing
``agent_sessions`` row owns its execution cwd; scope settings only seed new
sessions.
"""

from __future__ import annotations

import logging
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import Engine, select

from core.message_context import build_context_session_key, resolve_context_settings_key
from modules.im import MessageContext
from storage.agent_session_rows import create_agent_session_row
from storage.models import agent_sessions, scope_settings, scopes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRunTarget:
    platform: str
    settings_key: str
    session_key: str
    session_anchor: str
    workdir: str
    source: str
    scope_id: Optional[str] = None
    scope_type: Optional[str] = None
    agent_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_backend: Optional[str] = None
    agent_variant: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    native_session_id: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "settings_key": self.settings_key,
            "session_key": self.session_key,
            "session_anchor": self.session_anchor,
            "workdir": self.workdir,
            "source": self.source,
            "scope_id": self.scope_id,
            "scope_type": self.scope_type,
            "agent_session_id": self.agent_session_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "agent_backend": self.agent_backend,
            "agent_variant": self.agent_variant,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "native_session_id": self.native_session_id,
        }


@dataclass(frozen=True)
class ResolvedAgentTarget:
    agent_id: Optional[str]
    agent_name: Optional[str]
    agent_backend: str
    agent_variant: str
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None


def resolve_agent_run_target(
    context: MessageContext,
    *,
    controller: Any,
    base_session_id: Optional[str] = None,
    source: str = "human",
    create_session: bool = True,
) -> AgentRunTarget:
    """Resolve cwd/session/scope metadata for one agent turn.

    Resolution order:
    1. Reserved/explicit ``agent_sessions`` row carried by the context.
    2. Existing IM-style row for ``(scope, anchor)``.
    3. Current scope defaults.
    4. Global/process cwd fallback with a warning.
    """

    cached = _cached_target(
        context,
        base_session_id=base_session_id,
        source=source,
        create_session=create_session,
    )
    if cached is not None:
        return cached

    platform = _platform_for(context, controller)
    settings_key = _settings_key_for(context)
    session_key = build_context_session_key(context, platform=platform, settings_key=settings_key)
    anchor = str(base_session_id or _fallback_anchor(context, platform))
    engine = _engine_for(controller)

    with engine.begin() as conn:
        target_payload = _target_payload(context)
        target_id = _target_id(context, target_payload)
        if target_id:
            row = conn.execute(
                select(
                    agent_sessions,
                    scopes.c.scope_type.label("_scope_type"),
                )
                .select_from(agent_sessions.outerjoin(scopes, scopes.c.id == agent_sessions.c.scope_id))
                .where(agent_sessions.c.id == target_id)
            ).mappings().first()
            # An archived target is terminal — treat it as gone so no turn resumes it.
            if row is not None and row["status"] != "archived":
                return _cache_target(
                    context,
                    _target_from_session_row(
                        row,
                        platform=platform,
                        settings_key=settings_key,
                        session_key=session_key,
                        fallback_anchor=anchor,
                        workdir=_authoritative_session_workdir(
                            conn,
                            row,
                            controller=controller,
                            platform=platform,
                            settings_key=settings_key,
                            session_key=session_key,
                            fallback_anchor=anchor,
                            source="agent_session",
                        ),
                        source=source,
                    ),
                )
            raise LookupError(f"agent session target does not exist: {target_id}")

        scope_row = _scope_for_context(conn, context, platform, settings_key)
        scope_id = scope_row.get("scope_id") if scope_row else None

        if scope_id:
            existing = conn.execute(
                select(
                    agent_sessions,
                    scopes.c.scope_type.label("_scope_type"),
                )
                .select_from(agent_sessions.outerjoin(scopes, scopes.c.id == agent_sessions.c.scope_id))
                .where(agent_sessions.c.scope_id == scope_id)
                .where(agent_sessions.c.session_anchor == anchor)
                # Never resolve a turn onto an archived row. The archived row's
                # anchor is vacated on archive (so it won't match a live thread
                # anyway); this is the explicit guard, matching the bind path.
                .where(agent_sessions.c.status != "archived")
                .order_by(agent_sessions.c.last_active_at.desc(), agent_sessions.c.id.desc())
                .limit(1)
            ).mappings().first()
            if existing is not None:
                return _cache_target(
                    context,
                    _target_from_session_row(
                        existing,
                        platform=platform,
                        settings_key=settings_key,
                        session_key=session_key,
                        fallback_anchor=anchor,
                        workdir=_authoritative_session_workdir(
                            conn,
                            existing,
                            controller=controller,
                            platform=platform,
                            settings_key=settings_key,
                            session_key=session_key,
                            fallback_anchor=anchor,
                            source="existing_session",
                        ),
                        source=source,
                    ),
                )

        if not scope_id:
            agent_target = _resolve_agent_target(
                context,
                controller=controller,
                scope_row=None,
                platform=platform,
                settings_key=settings_key,
            )
            return _cache_target(
                context,
                _unpersisted_target(
                    None,
                    controller=controller,
                    platform=platform,
                    settings_key=settings_key,
                    session_key=session_key,
                    anchor=anchor,
                    source=source,
                    workdir_source="no_scope",
                    agent_target=agent_target,
                ),
            )

        agent_target = _resolve_agent_target(
            context,
            controller=controller,
            scope_row=scope_row,
            platform=platform,
            settings_key=settings_key,
        )
        resolved_new_workdir = _resolve_workdir(
            _normalize_workdir(scope_row.get("workdir")) if scope_row else None,
            controller=controller,
            platform=platform,
            settings_key=settings_key,
            session_key=session_key,
            source="new_session",
        )
        if not create_session:
            return _cache_target(
                context,
                _unpersisted_target(
                    scope_row,
                    controller=controller,
                    platform=platform,
                    settings_key=settings_key,
                    session_key=session_key,
                    anchor=anchor,
                    source=source,
                    workdir=resolved_new_workdir,
                    workdir_source="scope_read",
                    agent_target=agent_target,
                ),
            )

        new_session_id = create_agent_session_row(
            conn,
            scope_id=str(scope_id),
            session_anchor=anchor,
            agent_id=agent_target.agent_id,
            agent_backend=agent_target.agent_backend,
            agent_variant=agent_target.agent_variant,
            agent_name=agent_target.agent_name,
            model=agent_target.model,
            reasoning_effort=agent_target.reasoning_effort,
            workdir=resolved_new_workdir,
            metadata={"created_via": "agent_run_target", "source": source, "legacy_scope_key": session_key},
        )
        created = conn.execute(
            select(
                agent_sessions,
                scopes.c.scope_type.label("_scope_type"),
            )
            .select_from(agent_sessions.outerjoin(scopes, scopes.c.id == agent_sessions.c.scope_id))
            .where(agent_sessions.c.id == new_session_id)
        ).mappings().one()
        return _cache_target(
            context,
            _target_from_session_row(
                created,
                platform=platform,
                settings_key=settings_key,
                session_key=session_key,
                fallback_anchor=anchor,
                workdir=resolved_new_workdir,
                source=source,
            ),
        )


def _cached_target(
    context: MessageContext,
    *,
    base_session_id: Optional[str],
    source: str,
    create_session: bool,
) -> Optional[AgentRunTarget]:
    payload = context.platform_specific or {}
    cached = payload.get("agent_run_target")
    if not isinstance(cached, dict):
        return None
    if base_session_id and cached.get("session_anchor") != base_session_id:
        return None
    if cached.get("source") != source:
        return None
    if create_session and cached.get("scope_id") and not cached.get("agent_session_id"):
        return None
    workdir = _normalize_workdir(cached.get("workdir"))
    if not workdir:
        return None
    return AgentRunTarget(
        platform=str(cached.get("platform") or ""),
        settings_key=str(cached.get("settings_key") or ""),
        session_key=str(cached.get("session_key") or ""),
        session_anchor=str(cached.get("session_anchor") or base_session_id or ""),
        workdir=workdir,
        source=str(cached.get("source") or source),
        scope_id=_optional_str(cached.get("scope_id")),
        scope_type=_optional_str(cached.get("scope_type")),
        agent_session_id=_optional_str(cached.get("agent_session_id")),
        agent_id=_optional_str(cached.get("agent_id")),
        agent_name=_optional_str(cached.get("agent_name")),
        agent_backend=_optional_str(cached.get("agent_backend")),
        agent_variant=_optional_str(cached.get("agent_variant")),
        model=_optional_str(cached.get("model")),
        reasoning_effort=_optional_str(cached.get("reasoning_effort")),
        native_session_id=_optional_str(cached.get("native_session_id")),
    )


def _cache_target(context: MessageContext, target: AgentRunTarget) -> AgentRunTarget:
    payload = dict(context.platform_specific or {})
    payload["agent_run_target"] = target.to_payload()
    context.platform_specific = payload
    return target


def _engine_for(controller: Any) -> Engine:
    engine = getattr(controller, "sqlite_engine", None)
    if engine is not None:
        return engine
    from storage.db import create_sqlite_engine

    engine = create_sqlite_engine()
    try:
        setattr(controller, "sqlite_engine", engine)
    except Exception:
        pass
    return engine


def _platform_for(context: MessageContext, controller: Any) -> str:
    return (
        context.platform
        or (context.platform_specific or {}).get("platform")
        or getattr(controller, "primary_platform", None)
        or getattr(getattr(controller, "config", None), "platform", None)
        or "slack"
    )


def _settings_key_for(context: MessageContext) -> str:
    return resolve_context_settings_key(context)


def _fallback_anchor(context: MessageContext, platform: str) -> str:
    payload = context.platform_specific or {}
    target = payload.get("agent_session_target")
    if isinstance(target, dict) and target.get("session_anchor"):
        return str(target["session_anchor"])
    return f"{platform}_{context.thread_id or context.message_id or context.channel_id or context.user_id}"


def _target_payload(context: MessageContext) -> dict[str, Any]:
    payload = context.platform_specific or {}
    target = payload.get("agent_session_target")
    return target if isinstance(target, dict) else {}


def _target_id(context: MessageContext, target_payload: dict[str, Any]) -> Optional[str]:
    payload = context.platform_specific or {}
    value = target_payload.get("id") or payload.get("agent_session_id") or payload.get("workbench_session_id")
    return _optional_str(value)


def _resolve_agent_target(
    context: MessageContext,
    *,
    controller: Any,
    scope_row: Optional[dict[str, Any]],
    platform: str,
    settings_key: str,
) -> ResolvedAgentTarget:
    """Resolve the concrete Vibe Agent/backend before a Session row exists."""

    scope_agent_name = _optional_str(scope_row.get("agent_name")) if scope_row else None
    resolver = getattr(controller, "resolve_vibe_agent_for_context", None)
    if scope_agent_name and callable(resolver):
        try:
            agent = resolver(context, override_agent_name=scope_agent_name, required=False)
        except TypeError:
            agent = resolver(context, required=False)
        except Exception:
            logger.debug("Failed to resolve scoped Vibe Agent for new session", exc_info=True)
            agent = None
        if agent is not None:
            target = _agent_target_from_vibe_agent(agent, scope_row=scope_row)
            if target is not None:
                return target

    if callable(resolver):
        try:
            agent = resolver(context, required=False)
        except Exception:
            logger.debug("Failed to resolve Vibe Agent for new session", exc_info=True)
            agent = None
        if agent is not None:
            target = _agent_target_from_vibe_agent(agent, scope_row=scope_row)
            if target is not None:
                return target

    backend = _fallback_registered_backend(controller)
    if backend is None:
        from modules.agents.catalog import DEFAULT_AGENT_BACKEND

        backend = DEFAULT_AGENT_BACKEND
    return ResolvedAgentTarget(
        agent_id=None,
        agent_name=None,
        agent_backend=backend,
        agent_variant=backend,
        model=_optional_str(scope_row.get("model")) if scope_row else None,
        reasoning_effort=_optional_str(scope_row.get("reasoning_effort")) if scope_row else None,
    )


def _fallback_registered_backend(controller: Any) -> Optional[str]:
    agent_service = getattr(controller, "agent_service", None)
    registered = getattr(agent_service, "agents", {}) if agent_service is not None else {}
    default_agent = _supported_backend(getattr(agent_service, "default_agent", None))
    if default_agent and default_agent in registered:
        return default_agent
    for backend in registered:
        supported = _supported_backend(backend)
        if supported:
            return supported
    return None


def _agent_target_from_vibe_agent(agent: Any, *, scope_row: Optional[dict[str, Any]]) -> Optional[ResolvedAgentTarget]:
    backend = _supported_backend(getattr(agent, "backend", None))
    if not backend:
        return None
    scope_model = _optional_str(scope_row.get("model")) if scope_row else None
    scope_effort = _optional_str(scope_row.get("reasoning_effort")) if scope_row else None
    return ResolvedAgentTarget(
        agent_id=_optional_str(getattr(agent, "id", None)),
        agent_name=_optional_str(getattr(agent, "name", None)),
        agent_backend=backend,
        agent_variant=_agent_variant_for_backend(backend, scope_row),
        model=scope_model or _optional_str(getattr(agent, "model", None)),
        reasoning_effort=scope_effort or _optional_str(getattr(agent, "reasoning_effort", None)),
    )


def _supported_backend(value: Any) -> Optional[str]:
    backend = _optional_str(value)
    if backend in {"opencode", "claude", "codex"}:
        return backend
    return None


def _agent_variant_for_backend(backend: str, scope_row: Optional[dict[str, Any]]) -> str:
    routing_payload = _scope_routing_payload(scope_row)
    if routing_payload:
        variant = _optional_str(routing_payload.get(f"{backend}_agent"))
        if variant:
            return variant
    stored_variant = _optional_str(scope_row.get("agent_variant")) if _scope_variant_applies(backend, scope_row) else None
    if stored_variant:
        return stored_variant
    return backend


def _scope_variant_applies(backend: str, scope_row: Optional[dict[str, Any]]) -> bool:
    if not scope_row:
        return False
    if _optional_str(scope_row.get("agent_name")):
        return True
    return _optional_str(scope_row.get("agent_backend")) == backend


def _scope_routing_payload(scope_row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not scope_row:
        return {}
    raw = scope_row.get("settings_json")
    if not raw:
        return {}
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    routing = payload.get("routing") if isinstance(payload, dict) else None
    return routing if isinstance(routing, dict) else {}


def _scope_for_context(conn, context: MessageContext, platform: str, settings_key: str) -> Optional[dict[str, Any]]:
    payload = context.platform_specific or {}
    target = payload.get("agent_session_target")
    if isinstance(target, dict) and target.get("scope_id"):
        row = _scope_row(conn, str(target["scope_id"]))
        if row is not None:
            return row

    project_id = payload.get("project_id")
    if platform == "avibe" and project_id:
        row = _scope_row(conn, f"avibe::project::{project_id}")
        if row is not None:
            return row

    candidates: list[tuple[str, str, str]] = []
    if (payload.get("is_dm", False)):
        candidates.append((platform, "user", str(context.user_id)))
    else:
        candidates.append((platform, "channel", str(settings_key)))
        candidates.append((platform, "user", str(settings_key)))
    if platform == "avibe":
        candidates.append(("avibe", "project", str(settings_key)))

    for candidate_platform, scope_type, native_id in candidates:
        scope_id = f"{candidate_platform}::{scope_type}::{native_id}"
        row = _scope_row(conn, scope_id)
        if row is not None:
            return row
    return None


def _scope_row(conn, scope_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        select(
            scopes.c.id.label("scope_id"),
            scopes.c.scope_type,
            scopes.c.platform,
            scopes.c.native_id,
            scope_settings.c.workdir,
            scope_settings.c.agent_name,
            scope_settings.c.agent_variant,
            scope_settings.c.model,
            scope_settings.c.reasoning_effort,
            scope_settings.c.settings_json,
        )
        .select_from(scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id))
        .where(scopes.c.id == scope_id)
    ).mappings().first()
    return dict(row) if row is not None else None


def _scope_workdir(conn, scope_id: Any) -> Optional[str]:
    if not scope_id:
        return None
    value = conn.execute(
        select(scope_settings.c.workdir).where(scope_settings.c.scope_id == str(scope_id))
    ).scalar_one_or_none()
    return _normalize_workdir(value)


def _authoritative_session_workdir(
    conn,
    row: dict[str, Any],
    *,
    controller: Any,
    platform: str,
    settings_key: str,
    session_key: str,
    fallback_anchor: str,
    source: str,
) -> str:
    """Return ``agent_sessions.workdir`` and repair legacy blanks once.

    New sessions must be created with a workdir snapshot. This function exists
    for old rows only: if the stored workdir is missing or is a legacy
    anchor-derived placeholder, choose a migration value and write it back.
    """

    session_id = _optional_str(row.get("id"))
    stored = _session_row_workdir(row, fallback_anchor=fallback_anchor)
    if stored:
        return _resolve_workdir(
            stored,
            controller=controller,
            platform=platform,
            settings_key=settings_key,
            session_key=session_key,
            source=source,
        )

    repaired = _resolve_workdir(
        _scope_workdir(conn, row.get("scope_id")),
        controller=controller,
        platform=platform,
        settings_key=settings_key,
        session_key=session_key,
        source=f"{source}_legacy_repair",
    )
    if session_id:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == session_id)
            .values(workdir=repaired)
        )
        logger.warning("Repaired legacy agent session workdir session_id=%s workdir=%s", session_id, repaired)
    return repaired


def _target_from_session_row(
    row: dict[str, Any],
    *,
    platform: str,
    settings_key: str,
    session_key: str,
    fallback_anchor: str,
    workdir: str,
    source: str,
) -> AgentRunTarget:
    return AgentRunTarget(
        platform=platform,
        settings_key=settings_key,
        session_key=session_key,
        session_anchor=str(row.get("session_anchor") or fallback_anchor),
        workdir=workdir,
        source=source,
        scope_id=_optional_str(row.get("scope_id")),
        scope_type=_optional_str(row.get("_scope_type")),
        agent_session_id=_optional_str(row.get("id")),
        agent_id=_optional_str(row.get("agent_id")),
        agent_name=_optional_str(row.get("agent_name")),
        agent_backend=_optional_str(row.get("agent_backend")),
        agent_variant=_optional_str(row.get("agent_variant")),
        model=_optional_str(row.get("model")),
        reasoning_effort=_optional_str(row.get("reasoning_effort")),
        native_session_id=_optional_str(row.get("native_session_id")),
    )


def _unpersisted_target(
    scope_row: Optional[dict[str, Any]],
    *,
    controller: Any,
    platform: str,
    settings_key: str,
    session_key: str,
    anchor: str,
    source: str,
    workdir_source: str,
    workdir: Optional[str] = None,
    agent_target: Optional[ResolvedAgentTarget] = None,
) -> AgentRunTarget:
    resolved_workdir = workdir or _resolve_workdir(
        _normalize_workdir(scope_row.get("workdir")) if scope_row else None,
        controller=controller,
        platform=platform,
        settings_key=settings_key,
        session_key=session_key,
        source=workdir_source,
    )
    return AgentRunTarget(
        platform=platform,
        settings_key=settings_key,
        session_key=session_key,
        session_anchor=anchor,
        workdir=resolved_workdir,
        source=source,
        scope_id=_optional_str(scope_row.get("scope_id")) if scope_row else None,
        scope_type=_optional_str(scope_row.get("scope_type")) if scope_row else None,
        agent_id=agent_target.agent_id if agent_target else None,
        agent_name=agent_target.agent_name if agent_target else None,
        agent_backend=agent_target.agent_backend if agent_target else None,
        agent_variant=agent_target.agent_variant if agent_target else None,
        model=agent_target.model if agent_target else None,
        reasoning_effort=agent_target.reasoning_effort if agent_target else None,
    )


def _session_row_workdir(row: dict[str, Any], *, fallback_anchor: str) -> Optional[str]:
    workdir = _normalize_workdir(row.get("workdir"))
    if not workdir:
        return None
    native_session_id = _optional_str(row.get("native_session_id"))
    anchor = str(row.get("session_anchor") or fallback_anchor)
    placeholder = _normalize_workdir(_workdir_from_anchor(anchor))
    if not native_session_id and placeholder and workdir == placeholder:
        return None
    return workdir


def _workdir_from_anchor(anchor: str) -> Optional[str]:
    if ":" not in anchor:
        return None
    suffix = anchor.rsplit(":", 1)[1]
    return suffix or None


def _normalize_workdir(value: Any) -> Optional[str]:
    text = _optional_str(value)
    if not text:
        return None
    return os.path.abspath(os.path.expanduser(text))


def _resolve_workdir(
    candidate: Optional[str],
    *,
    controller: Any,
    platform: str,
    settings_key: str,
    session_key: str,
    source: str,
) -> str:
    if candidate:
        ensured_candidate = _ensure_workdir(
            candidate,
            platform=platform,
            settings_key=settings_key,
            session_key=session_key,
            source=source,
        )
        if ensured_candidate:
            return ensured_candidate
    config = getattr(controller, "config", None)
    default_cwd = getattr(getattr(config, "claude", None), "cwd", None)
    if default_cwd:
        resolved_default = os.path.abspath(os.path.expanduser(str(default_cwd)))
        ensured_default = _ensure_workdir(
            resolved_default,
            platform=platform,
            settings_key=settings_key,
            session_key=session_key,
            source="config_default",
        )
        if ensured_default:
            return ensured_default
    fallback = str(Path.cwd())
    logger.warning(
        "Agent run target missing workdir; falling back to process cwd=%s platform=%s settings_key=%s session_key=%s source=%s",
        fallback,
        platform,
        settings_key,
        session_key,
        source,
    )
    return _ensure_workdir(
        fallback,
        platform=platform,
        settings_key=settings_key,
        session_key=session_key,
        source=source,
    )


def _ensure_workdir(
    path: str,
    *,
    platform: str,
    settings_key: str,
    session_key: str,
    source: str,
) -> Optional[str]:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Failed to create agent run target workdir=%s platform=%s settings_key=%s session_key=%s source=%s error=%s",
            path,
            platform,
            settings_key,
            session_key,
            source,
            exc,
        )
        return None
    return path


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
