"""End an anonymous setup-network stream when its persisted authority ends."""

from __future__ import annotations

import asyncio

from vibe.network_position import BindingState, position_snapshot

CHECK_INTERVAL_SECONDS = 1.0


async def serve_setup_connection(app, scope, receive, send, still_admitted):
    """Ordinary finite HTTP responses need no watcher. WS and SSE outlive admission."""
    owner = asyncio.current_task()
    websocket = scope["type"] == "websocket"
    streaming = False
    revoked = False
    started = False
    finished = False
    monitor = None

    def lease_valid():
        try:
            config, state = position_snapshot()
            return state is BindingState.UNBOUND and still_admitted(config)
        except Exception:
            # A failed admission read must close the lease, not just its watcher.
            return False

    async def check():
        nonlocal revoked
        if revoked or not await asyncio.to_thread(lease_valid):
            if not revoked:
                revoked = True
                if asyncio.current_task() is not owner:
                    owner.cancel()
            # A send/receive pump may be a child of the handler. Never hand it
            # one more frame while cancellation travels through the task tree.
            raise asyncio.CancelledError

    async def watch():
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            await check()

    async def protected_receive():
        message = await receive()
        if message["type"] == "websocket.receive":
            await check()
        return message

    async def protected_send(message):
        nonlocal streaming, started, finished, monitor
        kind = message["type"]
        if kind == "http.response.start":
            streaming = any(
                k == b"content-type" and v.lower().split(b";", 1)[0] == b"text/event-stream"
                for k, v in message.get("headers", ())
            )
        if websocket and kind in ("websocket.accept", "websocket.send") or streaming:
            await check()
            if monitor is None:
                monitor = asyncio.create_task(watch())
        await send(message)
        if kind in ("http.response.start", "websocket.accept"):
            started = True
        if kind == "websocket.close" or kind == "http.response.body" and not message.get("more_body"):
            finished = True

    try:
        await app(scope, protected_receive, protected_send)
    except asyncio.CancelledError:
        if not revoked:
            raise
        # This cancellation belongs to the lease. Keep incidental server or
        # peer cancellations untouched.
        uncancel = getattr(owner, "uncancel", None)
        if uncancel is not None:
            uncancel()
    finally:
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
    if revoked and not finished:
        if websocket:
            await send({"type": "websocket.close", "code": 1008})
        else:
            if not started:
                await send({"type": "http.response.start", "status": 503, "headers": [(b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
