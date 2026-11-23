"""One connection, explicit half-duplex gates, no background speech replay."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from uuid import uuid4

from aiohttp import WSMsgType, web

from .codec import Codec, Utterance
from .host import Host
from .pairing import Pairing

WAKE_IDS = {"xiaowai-xiaowai", "hi-xiaowai", "xiaowai-tongxue"}
AUDIO = {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}


class Conversation:
    def __init__(self, ws, host, *, inactivity: float = 20, threshold: int = 600, settling: float = 0.2, device_id: str = ""):
        self.ws = ws
        self.host = host
        self.inactivity = inactivity
        self.threshold = threshold
        self.settling = settling
        self.device_id = device_id
        self.conversation_id = None
        self.session_id = str(uuid4())
        self.request_id = ""
        self.playback_id = ""
        self.codec = Codec()
        self.incoming: asyncio.Queue = asyncio.Queue(maxsize=12)

    async def receive(self):
        async for message in self.ws:
            if message.type == WSMsgType.TEXT:
                payload = json.loads(message.data)
                if not isinstance(payload, dict):
                    raise ValueError("invalid_control")
                if payload.get("type") == "abort":
                    raise ValueError("device_aborted")
            elif message.type == WSMsgType.BINARY:
                payload = message.data
            else:
                break
            # Overflow terminates the connection; dropping control could reopen a stale gate.
            self.incoming.put_nowait(payload)
        raise ValueError("device_disconnected")

    async def event(self, timeout: float):
        return await asyncio.wait_for(self.incoming.get(), timeout)

    async def send(self, kind: str, state: str, *, playback=False):
        await self.ws.send_json({"type": kind, "state": state, "session_id": self.session_id,
                                 "request_id": self.request_id,
                                 **({"playback_id": self.playback_id} if playback else {})})

    async def ack(self, kind: str, state: str, *, playback=False, discard_audio=False):
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            event = await self.event(remaining)
            if isinstance(event, bytes) and discard_audio:
                continue
            expected = {"type": kind, "state": state, "session_id": self.session_id,
                        "request_id": self.request_id,
                        **({"playback_id": self.playback_id} if playback else {})}
            if not isinstance(event, dict) or any(event.get(k) != v for k, v in expected.items()):
                raise ValueError("control_mismatch")
            if state == "drained" and (
                event.get("boundary") != "queues_empty_after_output_write"
                or type(event.get("tail_guard_ms")) is not int or event["tail_guard_ms"] < 80
            ):
                raise ValueError("invalid_drain_evidence")
            return

    async def run(self):
        hello = await self.event(10)
        if (
            not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("version") != 2
            or hello.get("audio_params") != AUDIO
            or not isinstance(hello.get("features"), dict)
            or type(hello["features"].get("avibe_voice")) is not int
            or hello["features"]["avibe_voice"] != 1
        ):
            raise ValueError("voice_capability_required")
        await self.ws.send_json({"type": "hello", "version": 2, "transport": "websocket",
                                 "session_id": self.session_id, "audio_params": AUDIO,
                                 "avibe_voice": {"version": 1, "route_revision": 4}})
        wake = await self.event(self.inactivity)
        if (
            not isinstance(wake, dict) or wake.get("type") != "listen" or wake.get("state") != "detect"
            or type(wake.get("protocol_version")) is not int or wake["protocol_version"] != 1
            or type(wake.get("route_revision")) is not int or wake["route_revision"] != 4
            or wake.get("wakeword_id") not in WAKE_IDS
            or wake.get("session_id") != self.session_id
        ):
            raise ValueError("invalid_wake")
        opened = await self.host.call("open", device_id=self.device_id)
        self.conversation_id = opened["conversation_id"]
        for _ in range(128):
            self.request_id = str(uuid4())
            self.codec.decoder.reset_state()
            await self.send("listen", "start")
            await self.ack("listen", "start")
            utterance = Utterance(self.threshold)
            deadline = time.monotonic() + self.inactivity
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                event = await self.event(remaining)
                if not isinstance(event, bytes):
                    raise ValueError("unexpected_control")
                if utterance.feed(self.codec.decode(event)):
                    break
            await self.send("listen", "stop")
            await self.ack("listen", "stop", discard_audio=True)
            frames = await asyncio.wait_for(self.reply_frames(utterance), 55)
            if frames is None:
                return
            self.playback_id = str(uuid4())
            await self.send("tts", "start", playback=True)
            await self.ack("playback", "started", playback=True)
            for frame in frames:
                await self.ws.send_bytes(frame)
                await asyncio.sleep(0.06)
            await self.send("tts", "stop", playback=True)
            await self.ack("playback", "drained", playback=True)
            await asyncio.sleep(self.settling)

    async def reply_frames(self, utterance: Utterance) -> list[bytes] | None:
        transcript = await self.host.call("transcribe", audio=base64.b64encode(utterance.wav()).decode("ascii"))
        text = transcript["text"]
        if re.sub(r"[\s。！？,.!?，]", "", text) == "先不聊了":
            return None
        speech = await self.host.answer(self.conversation_id, self.request_id, text)
        response = await self.host.call("speech", text=speech)
        return self.codec.output_frames(base64.b64decode(response["audio"], validate=True))


def create_app(pairing: Pairing, host=None, *, inactivity=20, threshold=600, settling=0.2) -> web.Application:
    app = web.Application()
    connections: set[web.WebSocketResponse] = set()
    selected_host = host or Host()

    async def websocket(request):
        device_id = request.headers.get("Device-Id", "")
        authorization = request.headers.get("Authorization", "")
        if not pairing.accepts(device_id, authorization):
            raise web.HTTPUnauthorized()
        if connections:
            raise web.HTTPConflict()
        ws = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
        connections.add(ws)  # Reserve before the first await.
        tasks = []
        conversation = None
        try:
            await ws.prepare(request)
            conversation = Conversation(ws, selected_host, inactivity=inactivity, threshold=threshold, settling=settling, device_id=device_id)

            async def revoked():
                while pairing.accepts(device_id, authorization):
                    await asyncio.sleep(0.25)
                raise ValueError("device_revoked")

            tasks = [asyncio.create_task(conversation.receive()), asyncio.create_task(conversation.run()),
                     asyncio.create_task(revoked())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError, asyncio.QueueFull, ConnectionError):
            # Payload-free close, no transcripts, credentials or protocol bodies in logs.
            pass
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if conversation is not None and conversation.conversation_id:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(selected_host.call("close", conversation_id=conversation.conversation_id), 2)
            await ws.close()
            connections.discard(ws)
        return ws

    async def shutdown(_app):
        for ws in tuple(connections):
            await ws.close(code=1001)

    app.router.add_get("/device/ws", websocket)
    app.on_shutdown.append(shutdown)
    return app
