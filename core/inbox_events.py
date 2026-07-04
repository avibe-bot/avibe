"""Controller-side fan-out bus for inbox change events.

The Controller process persists agent messages (``message_mirror``), but the
browser SSE broker lives in the UI server process. This bus lets the Controller
publish ``inbox.session.updated`` events; ``core/internal_server.py`` exposes
them over ``GET /internal/events`` (a long-lived SSE on the dispatch socket),
and the UI server re-broadcasts them to browsers via its own ``SSEBroker``.

Thread-safe like ``vibe/sse_broker.py``: ``publish`` may be called from any
thread/loop and lands on each subscriber's loop via ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

RUNS_UPDATED_EVENT = "runs.updated"
VAULTS_UPDATED_EVENT = "vaults.updated"
WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT = "workbench.events.bridge.status"
_CONTROLLER_PROCESS = False


def mark_controller_process() -> None:
    global _CONTROLLER_PROCESS
    _CONTROLLER_PROCESS = True


def is_controller_process() -> bool:
    return _CONTROLLER_PROCESS


class InboxEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subscribers[sub_id] = (loop, queue)
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event_type: str, data: Any) -> None:
        """Fan ``(event_type, data)`` out to every subscriber. No-op when none."""
        with self._lock:
            subs = list(self._subscribers.values())
        if not subs:
            return
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(self._put_nowait, queue, event_type, data)
            except RuntimeError:
                # Loop closed mid-publish; drop silently.
                pass

    @staticmethod
    def _put_nowait(queue: asyncio.Queue, event_type: str, data: Any) -> None:
        try:
            queue.put_nowait((event_type, data))
        except asyncio.QueueFull:
            logger.warning("inbox event bus subscriber queue full; dropping %s", event_type)


# Process-wide singleton (Controller process). ``message_mirror`` publishes;
# ``internal_server`` subscribes.
bus = InboxEventBus()


def run_updated_payload(
    *,
    run_id: str,
    status: str,
    run_type: str | None = None,
    session_id: str | None = None,
    definition_id: str | None = None,
    updated_at: str | None = None,
    cancel_requested: bool | None = None,
) -> dict[str, Any]:
    """Minimal run-lifecycle payload for browser refetch-on-event consumers."""

    payload: dict[str, Any] = {"run_id": run_id, "status": status}
    if run_type:
        payload["run_type"] = run_type
    if session_id:
        payload["session_id"] = session_id
    if definition_id:
        payload["definition_id"] = definition_id
    if updated_at:
        payload["updated_at"] = updated_at
    if cancel_requested is not None:
        payload["cancel_requested"] = cancel_requested
    return payload


def vaults_updated_payload(
    *,
    scope: str,
    request_id: str | None = None,
    request_status: str | None = None,
    grant_id: str | None = None,
    grant_status: str | None = None,
    secret_name: str | None = None,
) -> dict[str, Any]:
    """Minimal vault-state payload for browser refetch-on-event consumers."""

    payload: dict[str, Any] = {"scope": scope}
    if request_id:
        payload["request_id"] = request_id
    if request_status:
        payload["request_status"] = request_status
    if grant_id:
        payload["grant_id"] = grant_id
    if grant_status:
        payload["grant_status"] = grant_status
    if secret_name:
        payload["secret_name"] = secret_name
    return payload


def publish_run_updated(**kwargs: Any) -> None:
    bus.publish(RUNS_UPDATED_EVENT, run_updated_payload(**kwargs))
