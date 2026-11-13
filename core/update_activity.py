"""Live controller inactivity for native automatic updates, never a cached idle grant."""
from __future__ import annotations

import asyncio
import threading
from time import monotonic
from typing import Any

from core.inbox_events import RUNTIME_ACTIVITY_EVENT, bus


class UpdateActivity:
    """Observe short turns between polls and probe the shared runtime owners.

    The clock is process-local and starts cold on every controller start. Event
    callbacks only advance it; they do not persist a file for every token. A
    read is not an installation authorization and must be repeated at that edge.
    """

    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self._lock = threading.Lock()
        self._last_activity = monotonic()
        self._subscription: int | None = None

    def start(self) -> None:
        if self._subscription is None:
            self.record()
            self._subscription = bus.subscribe_callback(self._event)

    def stop(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            bus.unsubscribe(subscription)
        self.record()

    def record(self) -> None:
        with self._lock:
            self._last_activity = monotonic()

    def _event(self, event_type: str, _data: Any) -> None:
        if event_type in {RUNTIME_ACTIVITY_EVENT, "turn.start", "turn.end", "session.activity", "message.new", "queue.updated"}:
            self.record()

    async def idle_seconds(self) -> int:
        """Fail closed on missing lifecycle owners, active work or probe errors."""
        try:
            if self._subscription is None:
                return 0
            turns = self.controller.session_turns
            service = self.controller.agent_service
            if turns.in_flight or turns.active_turn_sinks or service.has_retained_runtime_work():
                self.record()
                return 0
            backends = tuple(service.agents)
            for backend in backends:
                if service.runtime_turn_tokens_for_backend(backend) or await service.backend_runtime_active(backend):
                    self.record()
                    return 0
            # An ownership probe may yield. New activity and backend replacement
            # during it cannot inherit the preceding quiet period.
            if (
                self._subscription is None or turns.in_flight or turns.active_turn_sinks
                or service.has_retained_runtime_work() or tuple(service.agents) != backends
            ):
                self.record()
                return 0
            with self._lock:
                return max(0, int(monotonic() - self._last_activity))
        except asyncio.CancelledError:
            self.record()
            raise
        except Exception:
            self.record()
            return 0
