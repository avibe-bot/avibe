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
from typing import Any, Callable

logger = logging.getLogger(__name__)

RUNS_UPDATED_EVENT = "runs.updated"
VAULTS_UPDATED_EVENT = "vaults.updated"
QUEUE_UPDATED_EVENT = "queue.updated"
DEFINITIONS_UPDATED_EVENT = "definitions.updated"
WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT = "workbench.events.bridge.status"
_CONTROLLER_PROCESS = False


def mark_controller_process() -> None:
    global _CONTROLLER_PROCESS
    _CONTROLLER_PROCESS = True


def is_controller_process() -> bool:
    return _CONTROLLER_PROCESS


class _Subscription:
    """One subscriber's queue, plus what this bus failed to hand it.

    Same shape and same reason as ``vibe/sse_broker.py``: a bounded queue makes
    "slow subscriber" and "lost events" one condition, and the loss is invisible
    from both ends. This is the first of the two queues on the controller →
    browser path, so a discard here never reaches the UI server's broker at all:
    ids stay contiguous, the browser socket keeps heartbeating, and nothing
    downstream can tell that a gap exists. Counting it is what lets the feed's
    owner end the subscription and make the reconnect announce the gap.
    """

    __slots__ = ("loop", "queue", "dropped")

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self.loop = loop
        self.queue = queue
        self.dropped = 0


class InboxEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, _Subscription] = {}
        self._callbacks: dict[int, Callable[[str, Any], None]] = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subscribers[sub_id] = _Subscription(loop, queue)
        return sub_id, queue

    def dropped_count(self, sub_id: int) -> int:
        """How many events this bus discarded for one subscriber, ever.

        Monotonic for the life of the subscription so a caller compares it
        against its own last reading, never resetting it out from under another
        reader. Returns 0 for an unknown ``sub_id``: a subscription that no
        longer exists has no continuity left to speak for.
        """

        with self._lock:
            subscription = self._subscribers.get(sub_id)
        return subscription.dropped if subscription is not None else 0

    def subscribe_callback(self, callback: Callable[[str, Any], None]) -> int:
        """Register an in-process callback run synchronously before queue fan-out.

        Turn-boundary owners use this path when the work triggered by an event
        must finish before the publisher continues. Exceptions are isolated so a
        diagnostic/checkpoint subscriber can never break message delivery.
        """

        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._callbacks[sub_id] = callback
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)
            self._callbacks.pop(sub_id, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event_type: str, data: Any) -> None:
        """Fan ``(event_type, data)`` out to every subscriber. No-op when none."""
        with self._lock:
            subs = list(self._subscribers.values())
            callbacks = list(self._callbacks.values())
        for callback in callbacks:
            try:
                callback(event_type, data)
            except Exception:
                logger.exception("inbox event callback failed for %s", event_type)
        for subscription in subs:
            try:
                subscription.loop.call_soon_threadsafe(
                    self._put_nowait, subscription, event_type, data
                )
            except RuntimeError:
                # Loop closed mid-publish; drop silently.
                pass

    @staticmethod
    def _put_nowait(subscription: _Subscription, event_type: str, data: Any) -> None:
        """Hand one event to one subscriber, and record it if that fails.

        Every path here that does not enqueue is a lost event and must count as
        one. Runs on the subscriber's loop thread via ``call_soon_threadsafe``,
        same as the feed that reads the count, so the counter needs no lock.
        """

        try:
            subscription.queue.put_nowait((event_type, data))
        except asyncio.QueueFull:
            subscription.dropped += 1
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


def publish_definitions_updated(*, definition_type: str) -> None:
    """Publish a payload-free scheduling hint after a definition commit."""

    payload = {"definition_type": str(definition_type or "")}
    bus.publish(DEFINITIONS_UPDATED_EVENT, payload)
    if is_controller_process():
        return
    try:
        from vibe import internal_client

        internal_client.publish_event_sync(
            DEFINITIONS_UPDATED_EVENT,
            payload,
            timeout=1.5,
        )
    except Exception:
        logger.debug("definitions.updated bridge publish failed", exc_info=True)
