"""Transport an already-authorized SSE response without holding an HTTP slot."""

from collections.abc import Awaitable, Callable

import anyio
from fastapi import WebSocket, WebSocketDisconnect
from starlette.responses import Response, StreamingResponse


async def stream_event_response(
    websocket: WebSocket,
    response_factory: Callable[[], Awaitable[Response]],
) -> None:
    """Keep HTTP admission, filtering and replay in the original route owner."""
    if (await websocket.receive())["type"] != "websocket.connect":
        return

    async def send_response() -> None:
        response = await response_factory()
        if (
            response.status_code != 200
            or not isinstance(response, StreamingResponse)
            or response.media_type != "text/event-stream"
        ):
            await websocket.close(code=1008 if response.status_code < 500 else 1011)
            return
        try:
            # Preserve cookie renewal performed by the shared HTTP hooks.
            await websocket.accept(headers=[
                (key, value) for key, value in response.raw_headers if key.lower() == b"set-cookie"
            ])
            async for chunk in response.body_iterator:
                await websocket.send_text(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
            await websocket.close()
        finally:
            with anyio.CancelScope(shield=True):
                await response.body_iterator.aclose()

    async def wait_for_disconnect() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            # This is a read-only subscription, never a publish channel.
            await websocket.close(code=1008)
            return

    # Structured cancellation also covers disconnect during HTTP admission:
    # neither a pending authorization read nor a broker subscriber outlives it.
    async with anyio.create_task_group() as tasks:
        async def run(operation):
            try:
                await operation()
            except WebSocketDisconnect:
                pass
            finally:
                tasks.cancel_scope.cancel()

        tasks.start_soon(run, send_response)
        await run(wait_for_disconnect)
