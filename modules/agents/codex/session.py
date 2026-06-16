"""Maps vibe-remote session keys to Codex thread/turn state."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class CodexSessionManager:
    """Track Codex thread and turn IDs per vibe-remote base session.

    A *base_session_id* corresponds to a Slack thread (channel + thread_ts).
    Each base session maps to exactly one Codex ``threadId``.
    """

    def __init__(self) -> None:
        # base_session_id → Codex threadId
        self._threads: dict[str, str] = {}
        # base_session_id → session_key (for scoped clear)
        self._session_keys: dict[str, str] = {}
        # base_session_id → working directory (for cwd-scoped invalidation)
        self._cwds: dict[str, str] = {}

    # -- Thread mapping ---------------------------------------------------

    def get_thread_id(self, base_session_id: str) -> Optional[str]:
        return self._threads.get(base_session_id)

    def set_thread_id(self, base_session_id: str, thread_id: str) -> None:
        self._threads[base_session_id] = thread_id
        logger.info("Session %s → Codex thread %s", base_session_id, thread_id)

    def invalidate_thread(self, base_session_id: str) -> None:
        """Remove only the thread_id, preserving session_key and cwd metadata."""
        self._threads.pop(base_session_id, None)

    # -- Session-key tracking ---------------------------------------------

    def set_session_key(self, base_session_id: str, session_key: str) -> None:
        self._session_keys[base_session_id] = session_key

    def get_session_key(self, base_session_id: str) -> Optional[str]:
        return self._session_keys.get(base_session_id)

    # -- Cwd tracking -----------------------------------------------------

    def set_cwd(self, base_session_id: str, cwd: str) -> None:
        self._cwds[base_session_id] = cwd

    def sessions_for_cwd(self, cwd: str) -> list[str]:
        """Return base_session_ids associated with a given working directory."""
        return [bid for bid, stored_cwd in self._cwds.items() if stored_cwd == cwd]

    def get_cwd(self, base_session_id: str) -> Optional[str]:
        """Return the working directory tracked for a base session."""
        return self._cwds.get(base_session_id)

    def get_sessions_by_session_key(self, session_key: str) -> list[str]:
        """Return base_session_ids associated with a given session_key."""
        return [bid for bid, sk in self._session_keys.items() if sk == session_key]

    def clear_by_session_key(self, session_key: str) -> int:
        """Remove all sessions associated with a given session_key. Returns count cleared."""
        to_remove = [bid for bid, sk in self._session_keys.items() if sk == session_key]
        for bid in to_remove:
            self._threads.pop(bid, None)
            self._session_keys.pop(bid, None)
            self._cwds.pop(bid, None)
        return len(to_remove)

    # -- Cleanup ----------------------------------------------------------

    def clear(self, base_session_id: str) -> None:
        """Remove all state for a session."""
        self._threads.pop(base_session_id, None)
        self._session_keys.pop(base_session_id, None)
        self._cwds.pop(base_session_id, None)

    def clear_all(self) -> int:
        """Remove all tracked sessions. Returns count cleared."""
        count = len(set(self._threads) | set(self._session_keys) | set(self._cwds))
        self._threads.clear()
        self._session_keys.clear()
        self._cwds.clear()
        return count

    def all_thread_ids(self) -> list[str]:
        """Return all known Codex thread IDs (for archiving on shutdown)."""
        return list(self._threads.values())

    def all_base_sessions(self) -> list[str]:
        """Return all base session IDs being tracked."""
        return list(set(self._threads) | set(self._session_keys) | set(self._cwds))

    def find_base_session_id_for_thread(self, thread_id: str) -> Optional[str]:
        for base_session_id, stored_thread_id in self._threads.items():
            if stored_thread_id == thread_id:
                return base_session_id
        return None
