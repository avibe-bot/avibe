"""Standalone real Host/Unix HTTP cancellation probe for supported Python versions.

Run with a test-only HOME and dependencies; no running Avibe instance is used.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
import sys
import tempfile

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[1] / "packaging" / "avibe-device"))

from avibe_device.cli import serve  # noqa: E402
from avibe_device.lifecycle import transition  # noqa: E402
from avibe_device.pairing import Pairing  # noqa: E402
from avibe_device.server import Conversation  # noqa: E402
from core.control_requests import result  # noqa: E402


async def probe():
    with tempfile.TemporaryDirectory(prefix="avdv-ipc-", dir="/tmp") as root:
        os.environ["HOME"] = root
        os.environ["AVIBE_HOME"] = root
        path = Path(root) / "host.sock"
        os.environ["VIBE_INTERNAL_DISPATCH_SOCKET"] = str(path)
        reached, settled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def endpoint(request):
            try:
                payload = await request.json()
            except ConnectionResetError:
                return web.Response(status=499)
            calls.append(payload["arguments"]["action"])
            return web.json_response(result({"ok": True}))

        app = web.Application()
        app.router.add_post("/internal/control", endpoint)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.UnixSite(runner, str(path)).start()
        path.chmod(0o600)
        original = aiohttp.ClientRequest.write_bytes

        async def delayed_write(self, *args, **kwargs):
            if b'"transcribe"' not in getattr(self.body, "_value", b""):
                return await original(self, *args, **kwargs)
            reached.set()
            try:
                await release.wait()
                return await original(self, *args, **kwargs)
            finally:
                # Model real library cancellation cleanup with an async tail.
                # A second cancel must not make the test's tail disappear.
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                settled.set()

        pairing = Pairing(Path(root) / "state")
        token = pairing.pair("board")
        pairing.enable(True)
        original_run = Conversation.run

        async def transcribe(conversation):
            await conversation.host.call("transcribe", audio="synthetic")

        Conversation.run = transcribe
        aiohttp.ClientRequest.write_bytes = delayed_write
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        stopped = asyncio.Event()
        serving = asyncio.create_task(serve(pairing, bind="127.0.0.1", port=port,
                                            inactivity=20, threshold=600, stopped=stopped))
        stopping = None
        try:
            for _ in range(200):
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{port}/device/ws", headers={
                    "Device-Id": "board", "Authorization": "Bearer " + token,
                }):
                    await asyncio.wait_for(reached.wait(), 3)
                    stopping = asyncio.create_task(asyncio.to_thread(transition, pairing, "stop"))
                    await asyncio.sleep(0.4)
                    assert not stopping.done(), "stop returned while its request writer still existed"
                    assert pairing.read().get("generation", 0) == 0
                    release.set()
                    await asyncio.wait_for(stopping, 5)
                    assert settled.is_set() and pairing.read()["generation"] == 1
                    assert calls == ["close_device"], calls  # Startup recovery only.
        finally:
            release.set()
            stopped.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await serving
            Conversation.run = original_run
            aiohttp.ClientRequest.write_bytes = original
            await runner.cleanup()
        print(json.dumps({"python": sys.version.split()[0], "aiohttp": aiohttp.__version__,
                          "writer_joined_before_return": True, "transcribe_requests_after_stop": calls.count("transcribe")}))


if __name__ == "__main__":
    asyncio.run(probe())
