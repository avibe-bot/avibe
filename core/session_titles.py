"""Backfill Vibe session titles from backend-native session metadata."""

from __future__ import annotations

import logging
from typing import Any

from core.inbox_events import bus
from core.services import sessions as workbench_sessions_service
from modules.agents.catalog import NATIVE_CLI_BACKENDS
from modules.agents.native_sessions.base import derive_first_prompt_title
from modules.agents.native_sessions.service import AgentNativeSessionService
from storage import messages_service
from storage.db import create_sqlite_engine

logger = logging.getLogger(__name__)


def backfill_agent_session_title(
    *,
    agent_session_id: str,
    backend: str,
    native_session_id: str,
    working_path: str,
    fallback_first_user_message: str = "",
) -> dict[str, Any] | None:
    """Backfill one empty ``agent_sessions.title`` from backend metadata.

    Best-effort and write-once: returns the updated session payload when a title
    was written, otherwise ``None``. User-owned or already non-empty titles are
    preserved by the storage-layer writer.
    """

    if not (agent_session_id and backend and native_session_id and working_path):
        return None

    inbox_row: dict[str, Any] | None = None
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            try:
                first_user_message = messages_service.first_user_text(conn, agent_session_id)
            except Exception:
                logger.debug("session-title: failed to read first user text", exc_info=True)
                first_user_message = ""
            candidate = AgentNativeSessionService().get_title(
                working_path=working_path,
                agent=backend,
                native_session_id=native_session_id,
                first_user_message=first_user_message or fallback_first_user_message,
            )
            if candidate is None and backend not in NATIVE_CLI_BACKENDS:
                # A backend with no native session store (the Avibe Agent) is titled the way
                # the native providers title a session without metadata: from its first prompt.
                candidate = derive_first_prompt_title(first_user_message or fallback_first_user_message)
            if candidate is None:
                return None
            updated = workbench_sessions_service.backfill_session_title(
                conn,
                agent_session_id,
                title=candidate.title,
                backend=backend,
                source=candidate.source,
                confidence=candidate.confidence,
                native_session_id=native_session_id,
            )
            if updated is not None:
                try:
                    inbox_row = messages_service.get_inbox_session(conn, agent_session_id, platform="avibe")
                except Exception:
                    logger.debug("session-title: failed to build inbox row", exc_info=True)
    finally:
        engine.dispose()

    if updated is None:
        return None
    bus.publish(
        "session.activity",
        {
            "session_id": updated["id"],
            "scope_id": updated.get("scope_id"),
            "event": "updated",
            "title": updated.get("title"),
        },
    )
    if inbox_row is not None:
        bus.publish("inbox.session.updated", inbox_row)
    return updated
