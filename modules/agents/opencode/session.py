"""OpenCode session bookkeeping.

This module owns per-thread locks and mapping from Slack thread (base_session_id)
to OpenCode session IDs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

from core.services.session_fork import pending_native_fork_source
from modules.agents.base import AgentRequest, BaseAgent

from .server import OpenCodeServerManager


class OpenCodeResumeUnavailableError(RuntimeError):
    """The OpenCode session associated with this conversation can no longer be
    validated on the server. Raised instead of silently creating a fresh session,
    so the user is told the context is gone (product decision: no silent
    fallbacks)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"Could not resume the previous OpenCode session ({session_id}); it may have expired. "
            "Not creating a new one to avoid silently losing context — start a new session to continue."
        )

logger = logging.getLogger(__name__)


RequestSessionTuple = Tuple[str, str, str]


class OpenCodeSessionManager:
    """Manage OpenCode session ids and concurrency guards."""

    def __init__(self, settings_manager, agent_name: str):
        self._settings_manager = settings_manager
        self._agent_name = agent_name

        self._request_sessions: Dict[str, RequestSessionTuple] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._initialized_sessions: set[str] = set()

    def get_request_session(self, base_session_id: str) -> Optional[RequestSessionTuple]:
        return self._request_sessions.get(base_session_id)

    def set_request_session(
        self,
        base_session_id: str,
        opencode_session_id: str,
        working_path: str,
        session_key: str,
    ) -> None:
        self._request_sessions[base_session_id] = (
            opencode_session_id,
            working_path,
            session_key,
        )

    def pop_request_session(self, base_session_id: str) -> Optional[RequestSessionTuple]:
        return self._request_sessions.pop(base_session_id, None)

    def pop_all_for_session_key(self, session_key: str) -> Dict[str, RequestSessionTuple]:
        matches: Dict[str, RequestSessionTuple] = {}
        for base_id, info in list(self._request_sessions.items()):
            if len(info) >= 3 and info[2] == session_key:
                matches[base_id] = info
        return matches

    def list_for_session_key(self, session_key: str) -> Dict[str, RequestSessionTuple]:
        return {
            base_id: info
            for base_id, info in self._request_sessions.items()
            if len(info) >= 3 and info[2] == session_key
        }

    def list_all(self) -> Dict[str, RequestSessionTuple]:
        return dict(self._request_sessions)

    def _set_request_agent_session_id(self, request: AgentRequest, agent_session_id: Optional[str]) -> None:
        if not agent_session_id:
            return
        payload = dict(request.context.platform_specific or {})
        payload["agent_session_id"] = agent_session_id
        request.context.platform_specific = payload

    def _reserved_agent_session_id(self, request: AgentRequest) -> Optional[str]:
        payload = request.context.platform_specific or {}
        session_target = payload.get("agent_session_target")
        if isinstance(session_target, dict):
            target_id = str(session_target.get("id") or "").strip()
            if target_id:
                return target_id
        return None

    def ensure_agent_session_id(self, request: AgentRequest, session_anchor: str) -> Optional[str]:
        reserved_target_id = self._reserved_agent_session_id(request)
        use_backend_anchor = BaseAgent._uses_namespaced_backend_session(request.context)
        if reserved_target_id and not use_backend_anchor:
            self._set_request_agent_session_id(request, reserved_target_id)
            return reserved_target_id
        sessions = getattr(self._settings_manager, "sessions", self._settings_manager)
        ensure = getattr(sessions, "ensure_agent_session_id", None)
        if callable(ensure):
            agent_session_id = ensure(request.session_key, self._agent_name, session_anchor)
        else:
            getter = getattr(sessions, "get_agent_session_row_id", None)
            agent_session_id = (
                getter(request.session_key, session_anchor, self._agent_name)
                if callable(getter)
                else None
            )
        self._set_request_agent_session_id(
            request,
            reserved_target_id if use_backend_anchor and reserved_target_id else agent_session_id,
        )
        return agent_session_id

    def bind_agent_session_id(
        self,
        request: AgentRequest,
        session_anchor: str,
        opencode_session_id: str,
    ) -> Optional[str]:
        sessions = getattr(self._settings_manager, "sessions", self._settings_manager)
        reserved_id = self._reserved_agent_session_id(request)
        use_backend_anchor = BaseAgent._uses_namespaced_backend_session(request.context)
        if reserved_id and not use_backend_anchor:
            bind_by_id = getattr(sessions, "bind_agent_session_by_id", None)
            if callable(bind_by_id):
                agent_session_id = bind_by_id(
                    reserved_id,
                    opencode_session_id,
                    workdir=request.working_path,
                    vibe_agent_id=request.vibe_agent_id,
                    vibe_agent_name=request.vibe_agent_name,
                    vibe_agent_backend=request.vibe_agent_backend,
                )
                if agent_session_id:
                    self._set_request_agent_session_id(request, agent_session_id)
                    return agent_session_id
            self._set_request_agent_session_id(request, reserved_id)
            return reserved_id
        binder = getattr(sessions, "bind_agent_session", None)
        if callable(binder):
            agent_session_id = binder(
                request.session_key,
                self._agent_name,
                session_anchor,
                opencode_session_id,
                workdir=request.working_path,
            )
        else:
            sessions.set_agent_session_mapping(
                request.session_key,
                self._agent_name,
                session_anchor,
                opencode_session_id,
            )
            agent_session_id = None
        if not agent_session_id:
            agent_session_id = self.ensure_agent_session_id(request, session_anchor)
        else:
            self._set_request_agent_session_id(
                request,
                reserved_id if use_backend_anchor and reserved_id else agent_session_id,
            )
        return agent_session_id

    def mark_initialized(self, opencode_session_id: str) -> bool:
        """Return True if this session was newly marked initialized."""

        if opencode_session_id in self._initialized_sessions:
            return False
        self._initialized_sessions.add(opencode_session_id)
        return True

    def get_session_lock(self, base_session_id: str) -> asyncio.Lock:
        if base_session_id not in self._session_locks:
            self._session_locks[base_session_id] = asyncio.Lock()
        return self._session_locks[base_session_id]

    async def wait_for_session_idle(
        self,
        server: OpenCodeServerManager,
        session_id: str,
        directory: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                messages = await server.list_messages(session_id, directory)
            except Exception as err:
                logger.debug(f"Failed to poll OpenCode session {session_id} for idle: {err}")
                await asyncio.sleep(1.0)
                continue

            in_progress = False
            for message in messages:
                info = message.get("info", {})
                if info.get("role") != "assistant":
                    continue
                time_info = info.get("time") or {}
                if not time_info.get("completed"):
                    in_progress = True
                    break

            if not in_progress:
                return

            await asyncio.sleep(1.0)

        logger.warning(
            "OpenCode session %s did not reach idle state within %.1fs",
            session_id,
            timeout_seconds,
        )

    async def ensure_working_dir(self, working_path: str) -> None:
        if not os.path.exists(working_path):
            os.makedirs(working_path, exist_ok=True)

    async def get_or_create_session_id(self, request: AgentRequest, server: OpenCodeServerManager) -> Optional[str]:
        """Get a cached OpenCode session id, or create a new session.

        The session anchor is the bare base (the thread's identity), independent
        of the working directory: OpenCode takes the directory as a PER-REQUEST
        param (``x-opencode-directory``), so ONE session per ``(scope, anchor)`` is
        reused across cwds. (New session model: one thread → one session → one
        backend; the cwd is metadata on the ``workdir`` column, never part of the
        key.)
        """

        sessions = getattr(self._settings_manager, "sessions", self._settings_manager)

        anchor = request.base_session_id
        self.ensure_agent_session_id(request, anchor)

        # Prefer the native bound to the RESERVED workbench row (by PK): the by-PK
        # bind WRITE and this resume READ must agree, else avibe forks a fresh
        # session after a restart (context loss). IM/CLI turns (no reserved target)
        # fall back to the (scope, anchor) projection. The server-validation below
        # still handles a reserved native the server no longer knows.
        use_backend_anchor = BaseAgent._uses_namespaced_backend_session(request.context)
        session_id = (
            None if use_backend_anchor else BaseAgent._reserved_native_session_id(request.context, self._agent_name)
        ) or sessions.get_agent_session_id(
            request.session_key,
            anchor,
            agent_name=self._agent_name,
        )

        if not session_id:
            fork_source = pending_native_fork_source(request.context, self._agent_name)
            try:
                if fork_source:
                    session_data = await server.fork_session(
                        fork_source,
                        directory=request.working_path,
                    )
                else:
                    session_data = await server.create_session(
                        directory=request.working_path,
                    )
                session_id = session_data.get("id")
                if session_id:
                    self.bind_agent_session_id(request, anchor, session_id)
                    if fork_source:
                        logger.info(
                            "Forked OpenCode session %s from %s for %s",
                            session_id,
                            fork_source,
                            request.base_session_id,
                        )
                    else:
                        logger.info(f"Created OpenCode session {session_id} for {request.base_session_id}")
            except Exception as e:
                logger.error(f"Failed to create OpenCode session: {e}", exc_info=True)
                return None
            return session_id

        # raise_on_error=True so a transport/connection failure propagates as a
        # transient server error (handled by the normal error path) rather than
        # being mislabeled as expiry — only a genuine "not found" (None) is
        # treated as context loss below.
        existing = await server.get_session(session_id, request.working_path, raise_on_error=True)
        if existing:
            self.bind_agent_session_id(request, anchor, session_id)
            return session_id

        # FAIL LOUD: an existing mapped session the server says is gone is context
        # loss — surface it rather than silently creating a fresh session (product
        # decision: no silent fallbacks). A fresh session is only created when
        # there was NO prior mapping (handled above).
        raise OpenCodeResumeUnavailableError(session_id)
