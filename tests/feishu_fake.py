"""A loopback stand-in for Feishu's long connection.

It serves what a long-connection client needs: the endpoint request
(``POST /callback/ws/endpoint``), the WebSocket itself, and the two calls the
adapter makes at start (tenant token, bot info). A test pushes events down the
socket and reads back what the client sent: acknowledgements and pings.

Frames are encoded and decoded with ``lark-oapi``'s generated protobuf class, so
the fake does not share a codec with any client of ours that it tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import patch

from aiohttp import WSMsgType, web

def _frame_class() -> Any:
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    return Frame


@dataclass
class ReceivedFrame:
    """One frame a client sent, decoded."""

    method: int
    service: int
    headers: Dict[str, str]
    payload: bytes
    connection: int
    raw: bytes

    @property
    def type(self) -> str:
        return self.headers.get("type", "")

    def json(self) -> Any:
        return json.loads(self.payload.decode("utf-8"))

    def bytes_without_timing(self) -> bytes:
        """The frame as sent, with the handling time (the one field two runs cannot share) set to zero."""
        frame = _frame_class()()
        frame.ParseFromString(self.raw)
        for header in frame.headers:
            if header.key == "biz_rt":
                header.value = "0"
        return frame.SerializeToString()


@dataclass
class FakeFeishu:
    """Start with ``start()``, stop with ``close()``; every other method is safe from any thread."""

    client_config: Dict[str, int] = field(
        default_factory=lambda: {"PingInterval": 120, "ReconnectCount": -1, "ReconnectInterval": 1, "ReconnectNonce": 0}
    )
    endpoint_code: int = 0
    endpoint_msg: str = "ok"
    bot_open_id: str = "ou_bot"

    def __post_init__(self) -> None:
        self.received: List[ReceivedFrame] = []
        self.endpoint_requests: List[Dict[str, Any]] = []
        # Answers for the next endpoint requests, before the normal one: a body, or (HTTP status, body).
        self.endpoint_replies: List[Any] = []
        # Refusals for the next handshakes, before the normal upgrade: (HTTP status, headers).
        self.handshake_refusals: List[Any] = []
        self.handshakes_refused = 0
        self.closes_withheld = 0
        # A peer that never completes a close the client begins.
        self.withhold_close_reply = False
        # Events sent down every socket the moment it opens.
        self.events_on_open: List[Dict[str, Any]] = []
        # Seconds to withhold the next endpoint replies, before answering as usual.
        self.endpoint_delays: List[float] = []
        # Seconds over which to send the next endpoint replies, a few bytes at a time.
        self.endpoint_trickles: List[float] = []
        self.connections_opened = 0
        self.close_codes: List[Optional[int]] = []
        self.history: Dict[str, List[Any]] = {}
        self.history_refused: set = set()
        self.history_delay = 0.0
        self.history_requests: List[Dict[str, str]] = []
        # The configuration sent with each pong; ``None`` repeats the endpoint's.
        self.pong_config: Optional[Dict[str, Any]] = None
        self.url = ""
        self._sockets: List[web.WebSocketResponse] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[web.AppRunner] = None
        self._port = 0

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeFeishu":
        ready = threading.Event()

        def serve() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_until_complete(self._serve())
            ready.set()
            loop.run_forever()
            loop.run_until_complete(self._runner.cleanup())
            loop.close()

        self._thread = threading.Thread(target=serve, daemon=True, name="fake-feishu")
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("fake Feishu did not start")
        return self

    def close(self) -> None:
        if self._loop is None or self._thread is None:
            return
        self._call(self._close_sockets())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_post("/callback/ws/endpoint", self._endpoint)
        app.router.add_get("/ws/v2", self._socket)
        app.router.add_post("/open-apis/auth/v3/tenant_access_token/internal", self._token)
        app.router.add_get("/open-apis/bot/v3/info", self._bot_info)
        app.router.add_get("/open-apis/im/v1/messages", self._history)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, shutdown_timeout=1)
        await site.start()
        self._port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{self._port}"

    # --- what Feishu serves ---------------------------------------------------

    async def _endpoint(self, request: web.Request) -> web.Response:
        self.endpoint_requests.append({"body": await request.json(), "locale": request.headers.get("locale")})
        if self.endpoint_delays:
            await asyncio.sleep(self.endpoint_delays.pop(0))
        if self.endpoint_replies:
            reply = self.endpoint_replies.pop(0)
            status, body = reply if isinstance(reply, tuple) else (200, reply)
            return web.json_response(body, status=status)
        if self.endpoint_code != 0:
            return web.json_response({"code": self.endpoint_code, "msg": self.endpoint_msg})
        socket_url = (
            f"ws://127.0.0.1:{self._port}/ws/v2"
            f"?fpid=1&aid=2&device_id={7000 + self.connections_opened}&access_key=key&service_id=33554678&ticket=once"
        )
        answer = {"code": 0, "msg": "ok", "data": {"URL": socket_url, "ClientConfig": dict(self.client_config)}}
        if self.endpoint_trickles:
            # A reply that keeps coming, never silent for long, and takes this long in all.
            body = json.dumps(answer).encode()
            pause = self.endpoint_trickles.pop(0) / 20
            reply = web.StreamResponse(headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
            await reply.prepare(request)
            step = -(-len(body) // 20)
            for start in range(0, len(body), step):
                await reply.write(body[start : start + step])
                await asyncio.sleep(pause)
            return reply
        return web.json_response(answer)

    async def _token(self, _request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "msg": "ok", "tenant_access_token": "t-fake", "expire": 7200})

    async def _history(self, request: web.Request) -> web.Response:
        """A chat's history: ``self.history[chat_id]`` is a list of (message id, sender type, seconds)."""
        query = request.query
        self.history_requests.append(dict(query))
        if self.history_delay:
            await asyncio.sleep(self.history_delay)
        chat_id = query["container_id"]
        if chat_id in self.history_refused:
            return web.json_response({"code": 230027, "msg": "Lack of necessary permissions"}, status=400)
        start, end = int(query["start_time"]), int(query["end_time"])
        items = [
            {"message_id": message_id, "sender": {"sender_type": sender_type}, "create_time": str(at * 1000)}
            for message_id, sender_type, at in self.history.get(chat_id, [])
            if start <= at <= end
        ]
        offset = int(query.get("page_token") or 0)
        page = items[offset : offset + 2]  # small pages, so that paging is exercised
        more = offset + 2 < len(items)
        return web.json_response(
            {"code": 0, "msg": "ok", "data": {"items": page, "has_more": more, "page_token": str(offset + 2) if more else ""}}
        )

    async def _bot_info(self, _request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "msg": "ok", "bot": {"open_id": self.bot_open_id}})

    async def _socket(self, request: web.Request) -> web.StreamResponse:
        if self.handshake_refusals:
            status, headers = self.handshake_refusals.pop(0)
            self.handshakes_refused += 1
            return web.Response(status=status, headers=headers)
        socket = web.WebSocketResponse(autoclose=False)
        await socket.prepare(request)
        index = self.connections_opened
        self.connections_opened += 1
        self._sockets.append(socket)
        for number, body in enumerate(self.events_on_open):
            for frame in self.event_frames(body, message_id=f"on-open-{index}-{number}"):
                await socket.send_bytes(frame)
        while True:
            message = await socket.receive()
            if message.type == WSMsgType.CLOSE:
                if self.withhold_close_reply:
                    self.closes_withheld += 1
                    await asyncio.sleep(30)  # says nothing, keeps the TCP connection
                await socket.close()
                break
            if message.type in (WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
            if message.type != WSMsgType.BINARY:
                continue
            frame = _frame_class()()
            frame.ParseFromString(message.data)
            received = ReceivedFrame(
                method=frame.method,
                service=frame.service,
                headers={header.key: header.value for header in frame.headers},
                payload=frame.payload,
                connection=index,
                raw=message.data,
            )
            self.received.append(received)
            if received.method == 0 and received.type == "ping":
                config = self.client_config if self.pong_config is None else self.pong_config
                await socket.send_bytes(self._control("pong", json.dumps(config).encode()))
        self.close_codes.append(socket.close_code)
        return socket

    async def _close_sockets(self) -> None:
        for socket in self._sockets:
            if not socket.closed:
                await socket.close()

    # --- what a test does -----------------------------------------------------

    def push_event(
        self, body: Dict[str, Any], *, pieces: int = 1, message_id: Optional[str] = None, connection: int = -1
    ) -> str:
        """Send one event down a connection (the newest by default), in ``pieces`` frames; returns its message id."""
        message_id = message_id or uuid.uuid4().hex
        self.push_frames(self.event_frames(body, pieces=pieces, message_id=message_id), connection=connection)
        return message_id

    def event_frames(self, body: Dict[str, Any], *, pieces: int = 1, message_id: str) -> List[bytes]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        size = max(1, -(-len(payload) // pieces))
        chunks = [payload[start : start + size] for start in range(0, len(payload), size)] or [b""]
        frames = []
        for seq, chunk in enumerate(chunks):
            frame = _frame_class()()
            frame.SeqID = 100 + seq
            frame.LogID = 200
            frame.service = 33554678
            frame.method = 1
            for key, value in (
                ("type", "event"),
                ("message_id", message_id),
                ("sum", str(len(chunks))),
                ("seq", str(seq)),
                ("trace_id", f"trace-{message_id}"),
            ):
                header = frame.headers.add()
                header.key = key
                header.value = value
            frame.payload = chunk
            frames.append(frame.SerializeToString())
        return frames

    def push_frames(self, frames: List[bytes], *, connection: int = -1) -> None:
        self._call(self._send(frames, connection))

    def drop_connection(self, connection: int = -1) -> None:
        """Close a connection (the newest by default) from the server side."""
        self._call(self._sockets[connection].close())

    def acknowledgements(self, message_id: Optional[str] = None) -> List[ReceivedFrame]:
        return [
            frame
            for frame in list(self.received)
            if frame.method == 1 and (message_id is None or frame.headers.get("message_id") == message_id)
        ]

    def pings(self) -> List[ReceivedFrame]:
        return [frame for frame in list(self.received) if frame.method == 0 and frame.type == "ping"]

    def wait_until(self, condition: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError(f"fake Feishu: timed out waiting for {what}")

    def wait_connected(self, connections: int = 1, timeout: float = 10.0) -> None:
        self.wait_until(lambda: self.connections_opened >= connections, timeout, f"{connections} connection(s)")

    def wait_acknowledged(self, message_id: str, timeout: float = 10.0) -> ReceivedFrame:
        return self.wait_until(lambda: self.acknowledgements(message_id), timeout, f"an acknowledgement of {message_id}")[
            0
        ]

    # --- internals ------------------------------------------------------------

    def _control(self, kind: str, payload: bytes = b"") -> bytes:
        frame = _frame_class()()
        frame.SeqID = 0
        frame.LogID = 0
        frame.service = 33554678
        frame.method = 0
        header = frame.headers.add()
        header.key = "type"
        header.value = kind
        if payload:
            frame.payload = payload
        return frame.SerializeToString()

    async def _send(self, frames: List[bytes], connection: int = -1) -> None:
        socket = self._sockets[connection]
        for frame in frames:
            await socket.send_bytes(frame)

    def _call(self, coroutine: Any) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=10)


def message_event(event_id: str = "evt_1", text: str = "hello", **message: Any) -> Dict[str, Any]:
    """A received-message event as Feishu delivers it."""
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "token": "verification-token",
            "create_time": "1608725989000",
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant",
            "app_id": "cli_app",
        },
        "event": {
            "sender": {
                "sender_id": {"union_id": "on_user", "user_id": "u1", "open_id": "ou_user"},
                "sender_type": "user",
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": "om_message",
                "create_time": "1609073151345",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                **message,
            },
        },
    }


def card_action_event(**action: Any) -> Dict[str, Any]:
    """A card click as Feishu delivers it."""
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_card",
            "token": "verification-token",
            "create_time": "1608725989000",
            "event_type": "card.action.trigger",
            "tenant_key": "tenant",
            "app_id": "cli_app",
        },
        "event": {
            "operator": {"tenant_key": "tenant", "user_id": "u1", "open_id": "ou_user", "union_id": "on_user"},
            "token": "c-token",
            "action": {"value": {"key": "stop", "thread_id": ""}, "tag": "button", **action},
            "host": "im_message",
            "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
        },
    }


class RunningAdapter:
    """A Feishu adapter connected to the fake, with its two event entry points recorded."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.messages: List[Dict[str, Any]] = []
        self.clicks: List[Dict[str, Any]] = []
        bot._handle_message_event = self.messages.append
        bot._handle_card_action = self.clicks.append


@contextmanager
def running_adapter(
    fake: FakeFeishu, app_id: str = "cli_app", connections: int = 1, **config: Any
) -> Iterator[RunningAdapter]:
    """Run the adapter against ``fake`` until the block ends; ``config`` adds fields to its ``LarkConfig``."""
    from config.v2_config import LarkConfig
    from modules.im.feishu import FeishuBot

    with patch.object(LarkConfig, "api_base_url", property(lambda _self: fake.url)):
        adapter = RunningAdapter(FeishuBot(LarkConfig(app_id=app_id, app_secret="socket-secret", **config)))
        already = fake.connections_opened
        thread = threading.Thread(target=adapter.bot.run, daemon=True)
        thread.start()
        try:
            fake.wait_connected(already + connections)
            yield adapter
        finally:
            adapter.bot.stop()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert adapter.bot.verify_stopped()
