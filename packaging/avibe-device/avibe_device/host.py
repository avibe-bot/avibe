"""The plugin sees only the narrow host proxy, never instance credentials."""
from __future__ import annotations

import asyncio
import os

import aiohttp


class Host:
    async def call(self, action: str, **arguments) -> dict:
        from core.control_requests import new_request, validate_result
        from vibe.internal_client import _verified_socket_path, InternalServerUnavailable

        # V1 uses owner-only Unix IPC. Never read the Windows descriptor's broad
        # host bearer credential into an optional plugin.
        if os.name == "nt":
            raise ValueError("unix_host_required")
        request = new_request("device.voice", {"action": action, **arguments})
        writers = set()

        class OwnedRequest(aiohttp.ClientRequest):
            def write_bytes(self, *args, **kwargs):
                # aiohttp creates its own writer task (also on Python 3.10/3.11).
                # Register the actual I/O before handing its awaitable to aiohttp,
                # so cancellation of the caller joins all request publication.
                task = asyncio.create_task(super().write_bytes(*args, **kwargs))
                writers.add(task)

                async def wait():
                    return await task
                return wait()

        try:
            path = await asyncio.to_thread(_verified_socket_path, None)
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=str(path)),
                request_class=OwnedRequest,
                timeout=aiohttp.ClientTimeout(total=65),
            ) as client:
                async with client.post("http://localhost/internal/control", json=request.to_payload()) as response:
                    if response.status != 200:
                        raise ValueError("host_voice_unavailable")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > 3_000_000:
                            raise ValueError("host_response_too_large")
                    import json

                    response = validate_result(json.loads(data))
        except (InternalServerUnavailable, aiohttp.ClientError, asyncio.TimeoutError):
            raise ValueError("host_voice_unavailable") from None
        finally:
            for writer in writers:
                if not writer.done():
                    writer.cancel()
            joined = asyncio.gather(*writers, return_exceptions=True)
            cancelled = False
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    cancelled = True
            joined.result()
            if cancelled:
                raise asyncio.CancelledError
        payload = response.get("payload") or {}
        if not payload.get("ok"):
            raise ValueError("host_voice_unavailable")
        return payload

    async def answer(self, conversation_id: str, request_id: str, text: str) -> str:
        response = await self.call("chat", conversation_id=conversation_id, request_id=request_id, text=text)
        if response.get("request_id") != request_id:
            raise ValueError("request_mismatch")
        return response["text"]
