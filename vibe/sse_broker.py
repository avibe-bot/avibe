"""In-process pub/sub for workbench Server-Sent Events.

The workbench UI opens a long-lived ``GET /api/events`` request and the
handler subscribes here. The REST routes that mutate messages / sessions /
unread counts publish events back through ``broker.publish``; the broker fans
them out to every subscriber.

Why SSE over WebSocket (per Cloudflare Tunnel research, 2026-05-24):
    SSE rides on plain HTTP, has automatic browser reconnect via
    ``EventSource``, and Cloudflare proxies + tunnels handle it without
    the WS upgrade handshake. A 15-second keep-alive comment line keeps
    intermediaries from killing idle streams.

Threading model:
    - Subscribers hold ``asyncio.Queue``; the SSE generator awaits them.
    - ``publish`` is safe to call from any thread (Flask-style sync
      routes, Agent worker threads, etc.) — it schedules
      ``call_soon_threadsafe`` on the event loop captured at first
      subscribe time. Sync callers don't need to know about the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class _Subscription:
    """One subscriber's queue, plus what this broker failed to hand it.

    A bounded queue means "slow subscriber" and "lost events" are the same
    condition, and the loss is invisible from both ends: the publisher's
    ``put_nowait`` raised somewhere it cannot report, and the subscriber's socket
    stays perfectly healthy. Counting the discards here is what lets whoever owns
    that subscriber's stream tell it that its view has a hole in it.
    """

    __slots__ = ("queue", "dropped")

    def __init__(self, queue: asyncio.Queue) -> None:
        self.queue = queue
        self.dropped = 0


class SSEBroker:
    def __init__(self) -> None:
        self._subscribers: dict[int, _Subscription] = {}
        self._next_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        # ``subscribe`` / ``unsubscribe`` run on the event loop thread while
        # ``publish`` runs from any thread (sync REST routes, IM threads).
        # Plain ``list(dict.values())`` mid-mutation can raise
        # ``RuntimeError: dictionary changed size during iteration``, so we
        # guard the read with a short-held lock.
        self._lock = threading.Lock()

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        """Register a new subscriber. Must be called from an event-loop coroutine."""

        self._loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subscribers[sub_id] = _Subscription(queue)
            total = len(self._subscribers)
        logger.debug("SSE subscriber %s connected (total=%s)", sub_id, total)
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            removed = self._subscribers.pop(sub_id, None) is not None
            total = len(self._subscribers)
        if removed:
            logger.debug("SSE subscriber %s disconnected (total=%s)", sub_id, total)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def dropped_count(self, sub_id: int) -> int:
        """How many events this broker discarded for one subscriber, ever.

        Monotonic for the life of the subscription, so a caller compares it
        against its own last reading rather than resetting it: two readers of the
        same subscription must not be able to consume each other's evidence.
        Returns 0 for an unknown ``sub_id`` -- a subscription that no longer
        exists has no continuity left to speak for.
        """

        with self._lock:
            subscription = self._subscribers.get(sub_id)
        return subscription.dropped if subscription is not None else 0

    def publish(self, event_type: str, data: Any) -> None:
        """Fan a JSON event out to every subscriber.

        Safe from any thread. No-op when there are no subscribers (the
        common case during boot / headless setups).
        """

        loop = self._loop
        if loop is None:
            return
        # Snapshot under the lock so a concurrent subscribe/unsubscribe on
        # the event loop thread cannot mutate the dict mid-iteration.
        payload = json.dumps({"type": event_type, "data": data}, sort_keys=True, separators=(",", ":"))
        with self._lock:
            if not self._subscribers:
                return
            subscriptions = list(self._subscribers.values())
        for subscription in subscriptions:
            try:
                loop.call_soon_threadsafe(self._put_nowait, subscription, event_type, payload)
            except RuntimeError:
                # Loop was closed; skip silently — next subscribe will
                # capture a fresh loop.
                pass

    @staticmethod
    def _put_nowait(subscription: _Subscription, event_type: str, payload: str) -> None:
        """Hand one event to one subscriber, and record it if that fails.

        Every path here that does not enqueue is a lost event and must count as
        one: the subscriber has no other way to find out, and a discard nobody
        counted is a hole in its view that reads exactly like quiet. Runs on the
        event loop thread via ``call_soon_threadsafe``, same as the generator
        that reads the count, so the counter needs no lock of its own.
        """

        try:
            subscription.queue.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            subscription.dropped += 1
            logger.warning("SSE subscriber queue full; dropping %s event", event_type)


# Module-level singleton — ui_server.py imports this and Avibe / REST
# routes call ``broker.publish`` from their write paths.
broker = SSEBroker()
