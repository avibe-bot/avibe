"""Feishu/Lark long connection: the WebSocket that delivers an app's events.

Feishu documents the limits (handle an event within 3 seconds, at most 50
connections per app, each event to one connection) but not the wire protocol.
This is that protocol as the official SDKs implement it, with the choices of
the Python SDK the adapter used before, wherever the SDKs differ
(``docs/plans/feishu-own-client.md`` has the full account):

1. ``POST {base}/callback/ws/endpoint`` with the app's id and secret returns a
   ``wss://`` URL carrying a one-time ticket, and the client configuration.
2. Every WebSocket message is one protobuf ``Frame``. A data frame carries an
   event, possibly one piece of it; it is answered by sending the same frame
   back with the outcome as its payload. A control frame is a ping or a pong;
   the pong may carry a new client configuration.
3. When the connection ends, wait a random part of ``ReconnectNonce`` seconds,
   fetch a new endpoint and connect; then try every ``ReconnectInterval``
   seconds. The connection has no token to refresh.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import aiohttp
import websockets
from websockets.exceptions import InvalidStatus

logger = logging.getLogger(__name__)

ENDPOINT_PATH = "/callback/ws/endpoint"

CONTROL = 0
DATA = 1

# Endpoint codes. Any other non-zero code is final: asking again cannot help.
_SYSTEM_BUSY = 1
_INTERNAL_ERROR = 1000040343
# Handshake refusals that are final.
_FORBIDDEN = 403
_AUTH_FAILED = 514
_EXCEED_CONNECTION_LIMIT = 1000040350

# How long the pieces of an event are kept while the rest is awaited.
_PIECES_KEPT_SECONDS = 5.0
# Feishu sends an event again when it is not answered within this.
ANSWER_WINDOW_SECONDS = 3.0
_ENDPOINT_TIMEOUT_SECONDS = 30.0
_CLOSE_SECONDS = 3.0


# --- frame ---------------------------------------------------------------------


@dataclass
class Frame:
    """``pbbp2.Frame`` (proto2): four required integers, headers, and an optional payload."""

    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = 0
    headers: List[Tuple[str, str]] = field(default_factory=list)
    payload_encoding: Optional[str] = None
    payload_type: Optional[str] = None
    payload: Optional[bytes] = None
    log_id_new: Optional[str] = None
    # Fields this client does not know, kept as received and sent back as they came.
    unknown: List[Tuple[int, int, Any]] = field(default_factory=list)

    def header(self, key: str) -> Optional[str]:
        for name, value in self.headers:
            if name == key:
                return value
        return None


class FrameError(ValueError):
    """Bytes that are not a frame."""


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        out.append(low | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(data: bytes, at: int) -> Tuple[int, int]:
    shift = value = 0
    while True:
        if at >= len(data) or shift > 63:
            raise FrameError("truncated or oversized varint")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value & 0xFFFFFFFFFFFFFFFF, at
        shift += 7


def _fields(data: bytes) -> List[Tuple[int, int, Any]]:
    """The (field number, wire type, value) triples of one protobuf message."""
    out: List[Tuple[int, int, Any]] = []
    at = 0
    while at < len(data):
        key, at = _read_varint(data, at)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, at = _read_varint(data, at)
        elif wire == 2:
            length, at = _read_varint(data, at)
            value, at = data[at : at + length], at + length
            if len(value) != length:
                raise FrameError("truncated field")
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, at = data[at : at + size], at + size
            if len(value) != size:
                raise FrameError("truncated field")
        else:
            raise FrameError(f"unsupported wire type {wire}")
        out.append((number, wire, value))
    return out


def _signed32(value: int) -> int:
    # A negative int32 travels as its 64-bit two's complement.
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >> 63 else value


def decode_frame(data: bytes) -> Frame:
    frame = Frame()
    try:
        for number, wire, value in _fields(bytes(data)):
            if number == 1 and wire == 0:
                frame.seq_id = value
            elif number == 2 and wire == 0:
                frame.log_id = value
            elif number == 3 and wire == 0:
                frame.service = _signed32(value)
            elif number == 4 and wire == 0:
                frame.method = _signed32(value)
            elif number == 5 and wire == 2:
                pair = {n: v.decode("utf-8") for n, w, v in _fields(value) if w == 2 and n in (1, 2)}
                frame.headers.append((pair.get(1, ""), pair.get(2, "")))
            elif number == 6 and wire == 2:
                frame.payload_encoding = value.decode("utf-8")
            elif number == 7 and wire == 2:
                frame.payload_type = value.decode("utf-8")
            elif number == 8 and wire == 2:
                frame.payload = value
            elif number == 9 and wire == 2:
                frame.log_id_new = value.decode("utf-8")
            else:
                frame.unknown.append((number, wire, value))
    except UnicodeDecodeError as exc:
        raise FrameError(f"text field is not UTF-8: {exc}") from None
    return frame


def _delimited(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def encode_frame(frame: Frame) -> bytes:
    """Fields in number order, as protobuf writes them; unknown fields last."""
    out = bytearray()
    for number, value in ((1, frame.seq_id), (2, frame.log_id), (3, frame.service), (4, frame.method)):
        out += _varint(number << 3) + _varint(value & 0xFFFFFFFFFFFFFFFF)
    for key, value in frame.headers:
        out += _delimited(5, _delimited(1, key.encode("utf-8")) + _delimited(2, value.encode("utf-8")))
    if frame.payload_encoding is not None:
        out += _delimited(6, frame.payload_encoding.encode("utf-8"))
    if frame.payload_type is not None:
        out += _delimited(7, frame.payload_type.encode("utf-8"))
    if frame.payload is not None:
        out += _delimited(8, frame.payload)
    if frame.log_id_new is not None:
        out += _delimited(9, frame.log_id_new.encode("utf-8"))
    for number, wire, value in frame.unknown:
        out += _varint(number << 3 | wire)
        out += _varint(value) if wire == 0 else (_varint(len(value)) + value if wire == 2 else value)
    return bytes(out)


def ping_frame(service: int) -> Frame:
    return Frame(service=service, method=CONTROL, headers=[("type", "ping")])


def answer_frame(
    received: Frame, *, code: int, result: Optional[Dict[str, Any]], handled_ms: Optional[int]
) -> Frame:
    """The received frame, sent back with what came of it.

    ``handled_ms`` is how long handling took; a failed event is answered
    without it (``None``), as the SDK answered one.
    """
    answer: Dict[str, Any] = {"code": code}
    if result is not None:
        answer["data"] = base64.b64encode(json.dumps(result, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return Frame(
        seq_id=received.seq_id,
        log_id=received.log_id,
        service=received.service,
        method=received.method,
        headers=[*received.headers] if handled_ms is None else [*received.headers, ("biz_rt", str(handled_ms))],
        payload_encoding=received.payload_encoding,
        payload_type=received.payload_type,
        payload=json.dumps(answer, ensure_ascii=False).encode("utf-8"),
        log_id_new=received.log_id_new,
        unknown=list(received.unknown),
    )


class EventPieces:
    """An event that arrives as several frames, put back together.

    Pieces share a ``message_id``; ``sum`` is how many there are and ``seq``
    which one this is. Pieces of an event that never completes are dropped
    after five seconds, and the event is then Feishu's to send again.
    """

    def __init__(self) -> None:
        self._waiting: Dict[str, Tuple[float, List[Optional[bytes]]]] = {}

    def add(self, message_id: str, total: int, index: int, piece: bytes) -> Optional[bytes]:
        """Return the whole payload once its last piece is in, ``None`` until then."""
        now = time.monotonic()
        for key in [key for key, (kept_until, _) in self._waiting.items() if kept_until <= now]:
            del self._waiting[key]
        if total <= 1:
            return piece
        if not 0 <= index < total:
            raise FrameError(f"piece {index} of {total}")
        kept = self._waiting.get(message_id)
        pieces = kept[1] if kept is not None and len(kept[1]) == total else [None] * total
        pieces[index] = piece
        if any(part is None or part == b"" for part in pieces):
            self._waiting[message_id] = (now + _PIECES_KEPT_SECONDS, pieces)
            return None
        self._waiting.pop(message_id, None)
        return b"".join(part for part in pieces if part is not None)


# --- connection ------------------------------------------------------------------


class ConnectionRefused(Exception):
    """Feishu refused the app or the connection for a reason that trying again will not change."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _Retry(Exception):
    """A failed attempt that is worth another."""


@dataclass
class ClientConfig:
    """Set by Feishu: with the endpoint, and again with any pong."""

    ping_interval: float = 120.0
    reconnect_count: int = -1
    reconnect_interval: float = 120.0
    reconnect_nonce: float = 30.0

    def update(self, values: Any) -> None:
        if not isinstance(values, dict):
            return
        for key, name in (
            ("PingInterval", "ping_interval"),
            ("ReconnectCount", "reconnect_count"),
            ("ReconnectInterval", "reconnect_interval"),
            ("ReconnectNonce", "reconnect_nonce"),
        ):
            value = values.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(self, name, value)


@dataclass
class ConnectionCounts:
    """What one connection has seen; read by the shadow comparison and the log."""

    connects: int = 0
    frames: int = 0
    events: int = 0
    answered: int = 0
    answered_failed: int = 0
    answered_late: int = 0
    unreadable_frames: int = 0
    pings: int = 0
    pongs: int = 0


class FeishuLongConnection:
    """One long connection, kept up until it is stopped or finally refused.

    ``handle_event`` receives one whole event payload on the connection's own
    thread. It returns what is sent back to Feishu with the answer (an object
    for a card click, otherwise ``None``) and raises for an event it could not
    read, which is answered as failed. It must not wait: Feishu allows 3 seconds.

    ``start`` runs the connection on a thread and loop of its own, so that
    answers and keepalives do not wait behind whatever the adapter's loop is
    doing. ``stop`` ends it for good: the connection is one task, and stopping
    cancels it. No event is taken after ``stop`` is called. One being handled
    at that moment is handled to its end, but its answer may not reach Feishu,
    which then sends it again.
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        handle_event: Callable[[bytes], Optional[Dict[str, Any]]],
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._handle_event = handle_event
        self.config = ClientConfig()
        self.counts = ConnectionCounts()
        self._socket: Any = None
        self._service = 0
        self._label = ""
        self._pieces = EventPieces()
        self._stopping = False
        self._pinger: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[asyncio.Task] = None
        self._stop_lock = threading.Lock()
        self._cancel_sent = False

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def run(self) -> None:
        """Connect and stay connected. Returns after ``stop``; raises ``ConnectionRefused`` when finally refused."""
        try:
            try:
                await self._connect()
            except _Retry as failure:
                logger.error("Feishu long connection: connect failed, err: %s", failure)
                await self._reconnect()
            while not self._stopping:
                if self._pinger is None:
                    self._pinger = asyncio.create_task(self._ping_forever())
                await self._receive()
                if self._stopping:
                    break
                await self._reconnect()
        except asyncio.CancelledError:
            if not self._stopping:
                raise
        except ConnectionRefused as refusal:
            if not self._stopping:
                logger.error("Feishu long connection refused, not trying again: %s", refusal)
                raise
        finally:
            if self._pinger is not None:
                self._pinger.cancel()
                await asyncio.gather(self._pinger, return_exceptions=True)
            await self._disconnect()

    def start(self, thread_name: str = "feishu-ws") -> None:
        """Run the connection on its own daemon thread until ``stop``."""

        def serve() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._runner = loop.create_task(self.run())
            try:
                loop.run_until_complete(self._runner)
            except ConnectionRefused:
                pass  # logged where it was raised; the thread ends, as the SDK's did
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Feishu long connection: thread crashed")
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._thread = threading.Thread(target=serve, daemon=True, name=thread_name)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """End the connection and its thread; returns whether the thread is gone."""
        self._stopping = True
        thread, loop = self._thread, self._loop
        if thread is None:
            return True
        runner = self._runner
        with self._stop_lock:
            # Cancelled once: a second cancellation would land in the closing of the socket.
            cancel = not self._cancel_sent and loop is not None and runner is not None and thread.is_alive()
            self._cancel_sent = self._cancel_sent or cancel
        if cancel:
            try:
                loop.call_soon_threadsafe(runner.cancel)
            except RuntimeError:
                pass  # the loop closed between the check and the call
        if thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return not thread.is_alive()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- connecting -----------------------------------------------------------

    async def _connect(self) -> None:
        if self._stopping:
            return
        url = await self._fetch_endpoint()
        query = parse_qs(urlsplit(url).query)
        try:
            self._service = int(query["service_id"][0])
            device = query["device_id"][0]
        except (KeyError, IndexError, ValueError):
            raise _Retry("the endpoint URL names no device or service") from None
        try:
            # ``proxy=None``: straight to Feishu, as the adapter has always connected.
            # ``close_timeout``: a peer that does not complete a close is cut off after it.
            socket = await websockets.connect(url, proxy=None, close_timeout=_CLOSE_SECONDS)
        except InvalidStatus as refusal:
            raise self._handshake_refusal(refusal) from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _Retry(str(exc) or type(exc).__name__) from None
        if self._stopping:
            await socket.close()
            return
        self._socket = socket
        self._label = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}{urlsplit(url).path} [conn_id={device}]"
        self.counts.connects += 1
        # The URL's query is the ticket; only where and which connection is logged.
        logger.info("Feishu long connection: connected to %s", self._label)

    async def _fetch_endpoint(self) -> str:
        body = json.dumps({"AppID": self._app_id, "AppSecret": self._app_secret}).encode("utf-8")
        headers = {"Content-Type": "application/json", "locale": "zh"}
        timeout = aiohttp.ClientTimeout(total=_ENDPOINT_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # The app secret goes to Feishu and nowhere a redirect points.
                async with session.post(
                    self._base_url + ENDPOINT_PATH, data=body, headers=headers, allow_redirects=False
                ) as reply:
                    status = reply.status
                    raw = await reply.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _Retry(str(exc) or type(exc).__name__) from None
        try:
            answer = json.loads(raw.decode("utf-8"))
        except ValueError:
            answer = None
        if status != 200:
            message = answer.get("msg") if isinstance(answer, dict) else None
            raise _Retry(f"{status}: {message or 'system busy'}")
        if not isinstance(answer, dict):
            # Not Feishu's answer (a gateway's page, say): nothing final can be read from it.
            raise _Retry("the endpoint reply is not a JSON object")
        code = answer.get("code")
        if code == _SYSTEM_BUSY:
            raise _Retry(f"{code}: system busy")
        if code == _INTERNAL_ERROR:
            raise _Retry(f"{code}: {answer.get('msg')}")
        if code != 0:
            raise ConnectionRefused(code if isinstance(code, int) else -1, self._without_secret(answer.get("msg")))
        data = answer.get("data")
        url = data.get("URL") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise _Retry("the endpoint reply names no URL")
        self.config.update(data.get("ClientConfig"))
        return url

    def _handshake_refusal(self, refusal: InvalidStatus) -> Exception:
        headers = refusal.response.headers
        status = headers.get("Handshake-Status")
        message = headers.get("Handshake-Msg")
        if status is None or message is None:
            return _Retry(str(refusal))
        try:
            code = int(status)
            auth_code = int(headers.get("Handshake-Autherrcode") or 0)
        except ValueError:
            return _Retry(f"{status}: {message}")
        if code == _FORBIDDEN or (code == _AUTH_FAILED and auth_code == _EXCEED_CONNECTION_LIMIT):
            return ConnectionRefused(code, message)
        return _Retry(f"{code}: {message}")

    def _without_secret(self, text: Any) -> str:
        text = str(text)
        return text.replace(self._app_secret, "***") if self._app_secret else text

    async def _reconnect(self) -> None:
        if self.config.reconnect_nonce > 0:
            await asyncio.sleep(random.random() * self.config.reconnect_nonce)
        attempts = 0
        while not self._stopping:
            if 0 <= self.config.reconnect_count <= attempts:
                raise ConnectionRefused(
                    -1, f"unable to connect to the server after trying {self.config.reconnect_count} times"
                )
            attempts += 1
            logger.info("Feishu long connection: trying to reconnect, attempt %d", attempts)
            try:
                await self._connect()
                return
            except _Retry as failure:
                logger.error("Feishu long connection: connect failed, err: %s", failure)
            await asyncio.sleep(self.config.reconnect_interval)

    async def _disconnect(self) -> None:
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            await socket.close()
        except Exception:
            logger.debug("Feishu long connection: close failed", exc_info=True)
        logger.info("Feishu long connection: disconnected from %s", self._label)

    # --- frames ---------------------------------------------------------------

    async def _ping_forever(self) -> None:
        while True:
            if self._socket is not None:
                try:
                    await self._send(ping_frame(self._service))
                    self.counts.pings += 1
                except Exception as exc:
                    logger.warning("Feishu long connection: ping failed, err: %s", exc)
            await asyncio.sleep(self.config.ping_interval)

    async def _receive(self) -> None:
        socket = self._socket
        if socket is None:
            return
        try:
            async for message in socket:
                await self._on_message(message)
            reason = "the connection was closed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
        if not self._stopping:
            logger.error("Feishu long connection: receive loop exit, err: %s", reason)
        await self._disconnect()

    async def _on_message(self, message: Any) -> None:
        self.counts.frames += 1
        try:
            if not isinstance(message, (bytes, bytearray)):
                raise FrameError("a text message")
            frame = decode_frame(bytes(message))
            if frame.method == CONTROL:
                self._on_control(frame)
            elif frame.method == DATA:
                await self._on_data(frame)
            else:
                raise FrameError(f"unknown frame method {frame.method}")
        except FrameError as exc:
            self.counts.unreadable_frames += 1
            logger.error("Feishu long connection: unreadable frame: %s", exc)
        except Exception as exc:
            logger.error("Feishu long connection: handle message failed, err: %s", exc)

    def _on_control(self, frame: Frame) -> None:
        if frame.header("type") != "pong":
            return
        self.counts.pongs += 1
        if frame.payload:
            try:
                self.config.update(json.loads(frame.payload.decode("utf-8")))
            except ValueError:
                logger.warning("Feishu long connection: unreadable client configuration in a pong")

    async def _on_data(self, frame: Frame) -> None:
        kind = frame.header("type")
        message_id = frame.header("message_id")
        try:
            total = int(frame.header("sum") or "")
            index = int(frame.header("seq") or "")
        except ValueError:
            raise FrameError("a data frame without piece numbers") from None
        if message_id is None or kind is None:
            raise FrameError("a data frame without a message id or a type")
        arrived = time.monotonic()
        payload = self._pieces.add(message_id, total, index, frame.payload or b"")
        if payload is None or kind != "event":
            return
        if self._stopping:
            # Not taken and not answered: Feishu sends it again, to the connection that replaces this one.
            return
        self.counts.events += 1
        started = time.monotonic()
        code, result = 200, None
        try:
            result = self._handle_event(payload)
        except Exception as exc:
            code = 500
            logger.error(
                "Feishu long connection: handle message failed, message_id: %s, trace_id: %s, err: %s",
                message_id,
                frame.header("trace_id"),
                exc,
            )
        elapsed = time.monotonic() - started
        handled_ms = int(round(elapsed * 1000)) if code == 200 else None
        await self._send(answer_frame(frame, code=code, result=result, handled_ms=handled_ms))
        self.counts.answered += 1
        if code != 200:
            self.counts.answered_failed += 1
        took = time.monotonic() - arrived
        if took > ANSWER_WINDOW_SECONDS:
            self.counts.answered_late += 1
            logger.warning(
                "Feishu long connection: event answered after %.1f s, Feishu will send it again (message_id: %s)",
                took,
                message_id,
            )

    async def _send(self, frame: Frame) -> None:
        socket = self._socket
        if socket is None:
            raise ConnectionError("connection is closed, write message failed")
        await socket.send(encode_frame(frame))


# --- listening in on another client's connection ---------------------------------


@dataclass
class TapCounts:
    frames_in: int = 0
    frames_out: int = 0
    events: int = 0
    mismatches: int = 0


class FrameTap:
    """Runs this module's codec beside another client's, on that client's own frames.

    The other client (``lark-oapi``'s) stays in charge of the connection and of
    every event. The tap is shown each raw frame it receives, each payload it
    hands to the dispatcher and each raw frame it sends, and checks that this
    module would have decoded, joined and answered the same, byte for byte.
    ``reference_frame`` is the other client's protobuf class.

    A mismatch is counted and logged with the message id, never with content.
    """

    def __init__(self, reference_frame: Any) -> None:
        self._reference_frame = reference_frame
        self._pieces = EventPieces()
        self._joined: Dict[str, bytes] = {}
        self._received: Dict[Tuple[str, str], Frame] = {}
        self.counts = TapCounts()

    def received(self, raw: bytes) -> None:
        """A frame the other client is about to handle."""
        self.counts.frames_in += 1
        try:
            reference = self._reference_frame()
            reference.ParseFromString(raw)
            frame = decode_frame(raw)
            if encode_frame(frame) != reference.SerializeToString() or frame.headers != [
                (header.key, header.value) for header in reference.headers
            ]:
                self._mismatch("a received frame decodes differently", frame.header("message_id"))
                return
            if frame.method != DATA or frame.header("type") != "event":
                return
            message_id = frame.header("message_id") or ""
            self._received[(message_id, frame.header("seq") or "")] = frame
            payload = self._pieces.add(
                message_id, int(frame.header("sum") or "1"), int(frame.header("seq") or "0"), frame.payload or b""
            )
            if payload is not None:
                self._joined[message_id] = payload
            self._forget_old()
        except Exception as exc:
            self._mismatch(f"a received frame could not be read here ({type(exc).__name__})", None)

    def handled(self, payload: bytes) -> None:
        """A whole event payload the other client handed to the dispatcher."""
        self.counts.events += 1
        for message_id, joined in list(self._joined.items()):
            if joined == payload:
                del self._joined[message_id]
                return
        self._mismatch("an event was joined differently", None)

    def sent(self, raw: bytes) -> None:
        """A frame the other client sent: a ping or an answer."""
        self.counts.frames_out += 1
        try:
            frame = decode_frame(raw)
            if frame.method == CONTROL:
                if encode_frame(ping_frame(frame.service)) != raw:
                    self._mismatch("a ping is built differently", None)
                return
            message_id = frame.header("message_id") or ""
            received = self._received.pop((message_id, frame.header("seq") or ""), None)
            if received is None:
                self._mismatch("an answer to a frame that was not seen", message_id)
                return
            answer = json.loads((frame.payload or b"").decode("utf-8"))
            data = answer.get("data")
            result = None if data is None else json.loads(base64.b64decode(data))
            timing = frame.header("biz_rt")
            ours = answer_frame(
                received, code=answer["code"], result=result, handled_ms=None if timing is None else int(timing)
            )
            if encode_frame(ours) != raw:
                self._mismatch("an answer is built differently", message_id)
        except Exception as exc:
            self._mismatch(f"a sent frame could not be read here ({type(exc).__name__})", None)

    def _forget_old(self) -> None:
        # Frames that are never answered (an event type the other client drops) must not pile up.
        for table in (self._received, self._joined):
            while len(table) > 256:
                del table[next(iter(table))]

    def _mismatch(self, what: str, message_id: Optional[str]) -> None:
        self.counts.mismatches += 1
        logger.error("Feishu long connection tap: %s (message_id: %s)", what, message_id)
