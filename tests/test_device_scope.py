"""Consumers cannot initiate effects from an invalidated device connection."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import os
from pathlib import Path
import struct
import socket
import wave

import aiohttp
from aiohttp import web
import pytest

from tests.test_device_voice import short_device_dir  # noqa: F401

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The optional device scope uses POSIX state locks")


@pytest.fixture(autouse=True)
def plugin(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "packaging" / "avibe-device"))
    pytest.importorskip("opuslib_next")
    pytest.importorskip("audioop")


def response_wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0" * (24000 * 2))
    return base64.b64encode(out.getvalue()).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["stop_enable", "disable", "revoke"])
@pytest.mark.parametrize("boundary", [
    "handshake_before_101", "handshake_after_101", "hello_wake", "before_open",
    "transcribe_chat", "chat_speech", "before_tts", "playback", "writer", "open_response",
])
async def test_scope_cuts_off_host_and_device_effects_at_async_boundaries(tmp_path, monkeypatch, boundary, transition, short_device_dir):
    tmp_path = short_device_dir
    from avibe_device.cli import serve, main
    from avibe_device.host import Host
    from core.control_requests import result
    from avibe_device.codec import Codec
    from avibe_device.pairing import Pairing
    from avibe_device.server import AUDIO

    pairing = Pairing(tmp_path / "state")
    token = pairing.pair("board")
    pairing.enable(True)
    reached, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects, cancellations = [], []
    generation_at_start = pairing.read().get("generation", 0)
    own_id = "this-scope-only"

    def record(op):
        effects.append((op, pairing.read().get("generation", 0)))

    async def barrier(name):
        if boundary == name:
            reached.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append(name)
                raise

    original_call = Host.call

    async def host_call(self, action, **arguments):
        if action == "open":
            await barrier("before_open")
        record("host:" + action)
        return await original_call(self, action, **arguments)

    monkeypatch.setattr(Host, "call", host_call)
    active = False

    async def ipc(request):
        nonlocal active
        body = await request.json()
        args = body["arguments"]
        action = args["action"]
        if action == "open":
            active = True
            payload = {"conversation_id": own_id}
        elif action in {"close", "close_device"}:
            active = False
            closed.set()
            payload = {}
        elif action == "transcribe":
            await barrier("transcribe_chat")
            payload = {"text": "你好"}
        elif action == "chat":
            await barrier("chat_speech")
            payload = {"text": "你好。", "request_id": args["request_id"]}
        elif action == "speech":
            await barrier("before_tts")
            payload = {"audio": response_wav()}
        else:
            raise AssertionError(action)
        document = result({"ok": True, **payload})
        if action == "open" and boundary == "open_response":
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(json.dumps(document).encode())
            await barrier("open_response")
            return response
        return web.json_response(document)

    ipc_app = web.Application()
    ipc_app.router.add_post("/internal/control", ipc)
    ipc_runner = web.AppRunner(ipc_app, access_log=None, shutdown_timeout=0.1)
    await ipc_runner.setup()
    ipc_path = tmp_path / "host.sock"
    await web.UnixSite(ipc_runner, str(ipc_path)).start()
    ipc_path.chmod(0o600)
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(ipc_path))
    original_writer = aiohttp.ClientRequest.write_bytes

    async def write_bytes(request, *args, **kwargs):
        if boundary == "writer" and b'"transcribe"' in getattr(request.body, "_value", b""):
            await barrier("writer")
        return await original_writer(request, *args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientRequest, "write_bytes", write_bytes)

    original_prepare = web.WebSocketResponse.prepare
    original_json = web.WebSocketResponse.send_json
    original_bytes = web.WebSocketResponse.send_bytes
    prepared_once = False

    async def prepare(ws, request):
        nonlocal prepared_once
        if not prepared_once:
            prepared_once = True
            await barrier("handshake_before_101")
            record("send:handshake")
            result = await original_prepare(ws, request)
            await barrier("handshake_after_101")
            return result
        return await original_prepare(ws, request)

    async def send_json(ws, payload, *args, **kwargs):
        record("send:" + payload["type"] + ":" + payload.get("state", ""))
        return await original_json(ws, payload, *args, **kwargs)

    async def send_bytes(ws, payload, *args, **kwargs):
        record("send:audio")
        return await original_bytes(ws, payload, *args, **kwargs)

    monkeypatch.setattr(web.WebSocketResponse, "prepare", prepare)
    monkeypatch.setattr(web.WebSocketResponse, "send_json", send_json)
    monkeypatch.setattr(web.WebSocketResponse, "send_bytes", send_bytes)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    stopped = asyncio.Event()
    serving = asyncio.create_task(serve(pairing, bind="127.0.0.1", port=port, inactivity=20,
                                        threshold=600, stopped=stopped))
    for _ in range(200):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            break
        except OSError:
            if serving.done():
                await serving
            await asyncio.sleep(0.01)
    else:
        pytest.fail("listener did not start")
    effects.clear()  # Startup close_device is part of recovery, not this WS.
    closed.clear()
    client_task = None
    cut = None
    client_ready, continue_client = asyncio.Event(), asyncio.Event()

    async def device(client):
        async with client.ws_connect(f"http://127.0.0.1:{port}/device/ws", headers={
            "Device-Id": "board", "Authorization": "Bearer " + token,
        }) as ws:
            await ws.send_json({"type": "hello", "version": 2, "audio_params": AUDIO,
                                "features": {"avibe_voice": 1}})
            first = await ws.receive(timeout=3)
            if first.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                return
            hello = json.loads(first.data)
            assert hello["type"] == "hello"
            if boundary == "hello_wake":
                client_ready.set()
                await continue_client.wait()
            await ws.send_json({"type": "listen", "state": "detect", "session_id": hello["session_id"],
                                "protocol_version": 1, "route_revision": 4, "wakeword_id": "xiaowai-xiaowai"})
            codec = Codec()
            while True:
                event = await ws.receive(timeout=3)
                if event.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                    return
                if event.type == aiohttp.WSMsgType.BINARY:
                    if boundary == "playback":
                        client_ready.set()
                        await continue_client.wait()
                    continue
                assert event.type == aiohttp.WSMsgType.TEXT
                control = json.loads(event.data)
                if control["type"] == "listen":
                    await ws.send_json(control)
                    if control["state"] == "start":
                        voiced = struct.pack("<960h", *[int(6000 * math.sin(2 * math.pi * 440 * i / 16000))
                                                        for i in range(960)])
                        for pcm in [voiced] * 3 + [b"\0" * 1920] * 14:
                            await ws.send_bytes(codec.encode(pcm))
                            await asyncio.sleep(0.003)
                elif control["type"] == "tts":
                    assert control["state"] == "start"
                    await ws.send_json({**control, "type": "playback", "state": "started"})

    try:
        async with aiohttp.ClientSession() as client:
            client_task = asyncio.create_task(device(client))
            await asyncio.wait_for((client_ready if boundary in {"hello_wake", "playback"} else reached).wait(), 5)
            command = "revoke" if transition == "revoke" else "stop" if transition == "stop_enable" else "disable"
            assert await asyncio.to_thread(main, ["--state-dir", str(pairing.root), command]) == 0
            if transition == "stop_enable":
                assert main(["--state-dir", str(pairing.root), "enable"]) == 0
            cut = len(effects)
            assert pairing.read()["generation"] != generation_at_start
            assert not active
            release.set()
            continue_client.set()
            if boundary == "handshake_before_101":
                with pytest.raises((aiohttp.WSServerHandshakeError, aiohttp.ClientConnectionError)):
                    await asyncio.wait_for(client_task, 5)
            else:
                with contextlib.suppress(aiohttp.ClientConnectionError):
                    await asyncio.wait_for(client_task, 5)
            if any(op == "host:open" for op, _ in effects):
                await asyncio.wait_for(closed.wait(), 2)
            assert effects[cut:] == [], effects
            assert all(gen == generation_at_start for op, gen in effects), effects
            assert sum(op == "host:close" for op, _ in effects) <= 1
            if boundary in {"before_open", "handshake_before_101", "handshake_after_101", "hello_wake"}:
                assert not any(op == "host:open" for op, _ in effects)
            if boundary in {"writer", "before_open", "handshake_before_101", "handshake_after_101"}:
                assert cancellations == [boundary]
    finally:
        release.set()
        continue_client.set()
        if client_task is not None:
            client_task.cancel()
            await asyncio.gather(client_task, return_exceptions=True)
        stopped.set()
        await serving
        await ipc_runner.cleanup()


def test_production_host_writer_is_joined_before_lifecycle_publication():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, str(Path(__file__).with_name("device_transport_probe.py"))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["writer_joined_before_return"]
    assert evidence["transcribe_requests_after_stop"] == 0
