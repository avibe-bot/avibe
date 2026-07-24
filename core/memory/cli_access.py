"""Ephemeral grants for Agent-initiated Memory CLI reads."""

from __future__ import annotations

import hmac
import secrets
import threading

from core.memory.store import is_principal_id


CALLER_SESSION_HEADER = "X-Avibe-Caller-Session"
MEMORY_CAPABILITY_HEADER = "X-Avibe-Memory-Capability"
MEMORY_USER_KEY_HEADER = "X-Avibe-Memory-User-Key"


class MemoryCliAccessRegistry:
    """Issue process-local capabilities bound to an admitted Agent session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, str]] = {}

    def grant(self, session_id: str, principal_id: str) -> str:
        session_id = str(session_id or "").strip()
        if not session_id or not is_principal_id(principal_id):
            raise ValueError("session_id and principal_id are required")
        with self._lock:
            current = self._tokens.get(session_id)
            if current is None or current[1] != principal_id:
                token = secrets.token_urlsafe(32)
                self._tokens[session_id] = (token, principal_id)
                return token
            return current[0]

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._tokens.pop(str(session_id or "").strip(), None)

    def validate(self, session_id: str, capability: str) -> str | None:
        session_id = str(session_id or "").strip()
        capability = str(capability or "").strip()
        if not session_id or not capability:
            return None
        with self._lock:
            grant = self._tokens.get(session_id)
        if grant is None or not hmac.compare_digest(grant[0], capability):
            return None
        return grant[1]
