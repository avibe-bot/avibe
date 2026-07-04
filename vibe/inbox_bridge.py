"""UI-server side relay: Controller inbox events → browser SSE broker.

The Controller process persists agent messages (``core.message_mirror``) and
publishes ``inbox.session.updated`` onto its in-process
``core.inbox_events.bus``, which ``core.internal_server`` exposes over the
dispatch Unix socket at ``GET /internal/events``. The UI server runs in a
*separate* process, so its ``vibe.sse_broker.broker`` never sees those events
directly. This module bridges the gap: one long-lived background task
subscribes to the controller feed and re-publishes every event to the local
broker, which fans it out to each browser's ``GET /api/events``.

One task per UI-server process (started from the ASGI ``startup`` hook). It
reconnects with backoff whenever the socket is missing (controller still
booting), the stream drops, or the controller restarts — so realtime inbox
updates self-heal without a UI reload.
"""

from __future__ import annotations

import asyncio
import logging

from core.inbox_events import WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT
from vibe import internal_client
from vibe.sse_broker import broker

logger = logging.getLogger(__name__)

# Reconnect backoff. The controller socket is often absent for a beat during
# co-startup / controller restart, so start tight (fast first connect) and cap
# it so a long-down controller doesn't busy-spin.
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 15.0
_bridge_connected = False


def is_bridge_connected() -> bool:
    return _bridge_connected


def _set_bridge_connected(connected: bool) -> None:
    global _bridge_connected
    if _bridge_connected == connected:
        return
    _bridge_connected = connected
    broker.publish(WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT, {"connected": connected})


async def run_inbox_bridge() -> None:
    """Relay controller inbox events to the local SSE broker until cancelled.

    Never raises out (except ``CancelledError`` on shutdown): every failure
    path loops back into a reconnect so a transient controller outage can't
    kill the bridge.
    """

    backoff = _BACKOFF_INITIAL
    while True:
        try:
            async for event_type, data in internal_client.stream_events():
                # A flowing event proves the link is healthy → reset backoff so
                # the next genuine drop reconnects promptly.
                backoff = _BACKOFF_INITIAL
                if event_type == "connected":
                    _set_bridge_connected(True)
                broker.publish(event_type, data)
        except asyncio.CancelledError:
            _set_bridge_connected(False)
            raise
        except internal_client.InternalServerUnavailable:
            logger.debug("inbox bridge: controller socket unavailable; retry in %.1fs", backoff)
        except Exception:
            logger.warning("inbox bridge: stream error; retry in %.1fs", backoff, exc_info=True)
        else:
            logger.debug("inbox bridge: controller feed closed; reconnect in %.1fs", backoff)
        _set_bridge_connected(False)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)
