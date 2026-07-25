"""Ephemeral grants for Agent-initiated Memory CLI reads."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable

from core.memory.store import is_principal_id


CALLER_SESSION_HEADER = "X-Avibe-Caller-Session"
MEMORY_CAPABILITY_HEADER = "X-Avibe-Memory-Capability"
MEMORY_USER_KEY_HEADER = "X-Avibe-Memory-User-Key"

# A capability outlives the turn that issued it by at most this long. Every
# admitted turn re-grants it, so an ordinary agent turn — however many CLI reads
# it makes — keeps working. What expires is a capability inherited from the
# environment by a background child process that outlived the session's last
# eligible turn: without a bound, such a process kept read access until some
# later ineligible turn for the SAME session happened to reach
# ``configure_memory_cli_access()``, which never happens once the user's binding
# is disabled or the user can no longer start turns. Four hours is well beyond
# the duration of agent turns in practice while making revocation take effect in
# hours rather than never.
CAPABILITY_TTL_SECONDS = 4 * 60 * 60


class MemoryCliAccessRegistry:
    """Issue process-local, expiring capabilities bound to an admitted Agent session."""

    def __init__(
        self,
        *,
        ttl_seconds: float = CAPABILITY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        # session_id -> (capability, principal_id, expires_at)
        self._tokens: dict[str, tuple[str, str, float]] = {}
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock

    def grant(self, session_id: str, principal_id: str) -> str:
        session_id = str(session_id or "").strip()
        if not session_id or not is_principal_id(principal_id):
            raise ValueError("session_id and principal_id are required")
        with self._lock:
            now = self._clock()
            self._drop_expired(now)
            expires_at = now + self._ttl_seconds
            current = self._tokens.get(session_id)
            if current is None or current[1] != principal_id:
                token = secrets.token_urlsafe(32)
                self._tokens[session_id] = (token, principal_id, expires_at)
                return token
            # Only a freshly admitted turn reaches this method, so re-admission
            # is a trusted event and may extend the window. Use never does.
            self._tokens[session_id] = (current[0], principal_id, expires_at)
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
            if grant is not None and grant[2] <= self._clock():
                self._tokens.pop(session_id, None)
                grant = None
        if grant is None or not hmac.compare_digest(grant[0], capability):
            return None
        return grant[1]

    def _drop_expired(self, now: float) -> None:
        """Evict lapsed sessions. The caller holds the lock."""

        for session_id in [key for key, grant in self._tokens.items() if grant[2] <= now]:
            self._tokens.pop(session_id, None)
