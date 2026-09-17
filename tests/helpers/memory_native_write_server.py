"""Isolated native HTTP/memorize contract fixture, never a production launcher.

Run only via test_memory_native_write_contract.py. The real route and memorize
lock/deadline execute; the costly pipeline is replaced by a synthetic local write.
"""

import asyncio
import importlib
import json
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from everos.config.settings import load_settings
from everos.entrypoints.api.routes.memorize import router

native = importlib.import_module("everos.service.memorize")
root = Path(sys.argv[1])
assert load_settings().memorize.session_lock_timeout_seconds == 360.0


async def synthetic_pipeline(payload, **kwargs):
    text = payload["messages"][0]["content"] if payload["messages"] else "flush"
    if text == "slow":
        await asyncio.sleep(31)
    # Prove a representative write is confined to the disposable root.
    (root / f"{text}.receipt").write_text("synthetic committed")
    return native.MemorizeResult(message_count=len(payload["messages"]), status="accumulated")


native._memorize_locked = synthetic_pipeline
app = FastAPI()
app.include_router(router, prefix="/api/v2")


class LoseResponse:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        request = await receive()
        body = json.loads(request.get("body", b"{}"))
        lose = any(m.get("content") == "lose" for m in body.get("messages", []))
        first = True

        async def replay():
            nonlocal first
            if first:
                first = False
                return request
            return await receive()

        async def delayed_send(message):
            if lose and message["type"] == "http.response.start":
                await asyncio.sleep(1)
            await send(message)

        await app(scope, replay, delayed_send)


uvicorn.run(LoseResponse(), uds=str(root / "native.sock"), access_log=False, log_level="error")
