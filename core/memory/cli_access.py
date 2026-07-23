"""Ephemeral grants for Agent-initiated Memory CLI reads."""

from __future__ import annotations

import hmac
import secrets
import threading


CALLER_SESSION_HEADER = "X-Avibe-Caller-Session"
MEMORY_CAPABILITY_HEADER = "X-Avibe-Memory-Capability"


class MemoryCliAccessRegistry:
    """Issue process-local capabilities bound to an admitted Agent session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, str] = {}

    def grant(self, session_id: str) -> str:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        with self._lock:
            token = self._tokens.get(session_id)
            if token is None:
                token = secrets.token_urlsafe(32)
                self._tokens[session_id] = token
            return token

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._tokens.pop(str(session_id or "").strip(), None)

    def validate(self, session_id: str, capability: str) -> bool:
        session_id = str(session_id or "").strip()
        capability = str(capability or "").strip()
        if not session_id or not capability:
            return False
        with self._lock:
            expected = self._tokens.get(session_id)
        return expected is not None and hmac.compare_digest(expected, capability)
