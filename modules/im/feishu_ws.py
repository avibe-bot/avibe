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
# (connect, read) for the sockets of that request; the 30 seconds are for all of it.
_ENDPOINT_SOCKET_TIMEOUT = (5.0, 10.0)
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
    # Fields this client does not know, as the bytes they arrived in, sent back as they came:
    # the frame's own, and each header's (by position in ``headers``).
    unknown: List[bytes] = field(default_factory=list)
    header_unknown: Dict[int, bytes] = field(default_factory=dict)
    # A required field was absent on arrival. Such a frame is read, and cannot be sent back.
    incomplete: bool = False

    def header(self, key: str) -> Optional[str]:
        for name, value in self.headers:
            if name == key:
                return value
        return None


class FrameError(ValueError):
    """Bytes that are not a frame, or a frame that cannot be written."""


_UINT64 = 0xFFFFFFFFFFFFFFFF
_UNKNOWN_FIELD = -1


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
            return value & _UINT64, at
        shift += 7


def _skip(data: bytes, at: int, number: int, wire: int) -> int:
    """The position after the value of a field with this wire type that starts at ``at``."""
    if wire == 0:
        return _read_varint(data, at)[1]
    if wire == 2:
        length, at = _read_varint(data, at)
        end = at + length
    elif wire == 1:
        end = at + 8
    elif wire == 5:
        end = at + 4
    elif wire == 3:  # a group: fields up to the end-group tag of the same number
        while True:
            key, at = _read_varint(data, at)
            inner_number, inner_wire = key >> 3, key & 7
            if inner_number == 0:
                raise FrameError("field number 0")
            if inner_wire == 4:
                if inner_number != number:
                    raise FrameError("mismatched end of group")
                return at
            at = _skip(data, at, inner_number, inner_wire)
    else:
        raise FrameError(f"unexpected wire type {wire}")
    if end > len(data):
        raise FrameError("truncated field")
    return end


def _fields(data: bytes) -> List[Tuple[int, int, Any, bytes]]:
    """Each field of one protobuf message: number, wire type, value, and the bytes it arrived in.

    The value is an integer for a varint and the content for a length-delimited
    field; for the other wire types only the bytes are of use.
    """
    out: List[Tuple[int, int, Any, bytes]] = []
    at = 0
    while at < len(data):
        start = at
        key, at = _read_varint(data, at)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise FrameError("field number 0")
        end = _skip(data, at, number, wire)
        value: Any = None
        if wire == 0:
            value = _read_varint(data, at)[0]
        elif wire == 2:
            value = data[_read_varint(data, at)[1] : end]
        if at - start != len(_varint(key)):
            # A tag written longer than it need be is not the field it spells: protobuf
            # looks a field up by the tag's bytes, and keeps this one as unknown.
            number = _UNKNOWN_FIELD
        out.append((number, wire, value, data[start:end]))
        at = end
    return out


def _int32(value: int) -> int:
    # The low 32 bits, signed: however many bytes the varint took.
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >> 31 else value


def decode_frame(data: bytes) -> Frame:
    frame = Frame()
    seen = set()
    try:
        for number, wire, value, raw in _fields(bytes(data)):
            if number == 1 and wire == 0:
                frame.seq_id = value
            elif number == 2 and wire == 0:
                frame.log_id = value
            elif number == 3 and wire == 0:
                frame.service = _int32(value)
            elif number == 4 and wire == 0:
                frame.method = _int32(value)
            elif number == 5 and wire == 2:
                key = text = None
                extra = b""
                for inner_number, inner_wire, inner_value, inner_raw in _fields(value):
                    if inner_number == 1 and inner_wire == 2:
                        key = inner_value.decode("utf-8")
                    elif inner_number == 2 and inner_wire == 2:
                        text = inner_value.decode("utf-8")
                    else:
                        extra += inner_raw
                if key is None or text is None:
                    frame.incomplete = True
                if extra:
                    frame.header_unknown[len(frame.headers)] = extra
                frame.headers.append((key or "", text or ""))
                continue
            elif number == 6 and wire == 2:
                frame.payload_encoding = value.decode("utf-8")
            elif number == 7 and wire == 2:
                frame.payload_type = value.decode("utf-8")
            elif number == 8 and wire == 2:
                frame.payload = value
            elif number == 9 and wire == 2:
                frame.log_id_new = value.decode("utf-8")
            else:
                frame.unknown.append(raw)
                continue
            seen.add(number)
    except UnicodeDecodeError as exc:
        raise FrameError(f"text field is not UTF-8: {exc}") from None
    if not {1, 2, 3, 4} <= seen:
        frame.incomplete = True
    return frame


def _delimited(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def encode_frame(frame: Frame) -> bytes:
    """Fields in number order, as protobuf writes them; unknown fields last, as they arrived."""
    if frame.incomplete:
        raise FrameError("a required field is missing")
    out = bytearray()
    for number, value in ((1, frame.seq_id), (2, frame.log_id), (3, frame.service), (4, frame.method)):
        out += _varint(number << 3) + _varint(value & _UINT64)
    for index, (key, value) in enumerate(frame.headers):
        header = _delimited(1, key.encode("utf-8")) + _delimited(2, value.encode("utf-8"))
        out += _delimited(5, header + frame.header_unknown.get(index, b""))
    if frame.payload_encoding is not None:
        out += _delimited(6, frame.payload_encoding.encode("utf-8"))
    if frame.payload_type is not None:
        out += _delimited(7, frame.payload_type.encode("utf-8"))
    if frame.payload is not None:
        out += _delimited(8, frame.payload)
    if frame.log_id_new is not None:
        out += _delimited(9, frame.log_id_new.encode("utf-8"))
    for raw in frame.unknown:
        out += raw
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
        header_unknown=dict(received.header_unknown),
        incomplete=received.incomplete,
    )


def event_piece(frame: Frame) -> Tuple[str, str, int, int]:
    """What a data frame says of itself: its type, its message id, how many pieces, and which one this is.

    All five headers the SDK's client reads must be there, ``trace_id``
    included; as there, ``seq`` is only read as a number when there are several
    pieces.
    """
    kind, message_id, total, index = (frame.header(key) for key in ("type", "message_id", "sum", "seq"))
    if None in (kind, message_id, total, index, frame.header("trace_id")):
        raise FrameError("a data frame without one of its five headers")
    try:
        pieces = int(total)
        return kind, message_id, pieces, int(index) if pieces > 1 else 0
    except ValueError:
        raise FrameError("a data frame whose piece numbers are not numbers") from None


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


# The WebSocket library logs each handshake's request line at DEBUG, ticket and all. Ours do not.
_handshake_log = logging.getLogger(__name__ + ".handshake")
_handshake_log.setLevel(logging.INFO)

async def close_socket(socket: Any) -> None:
    """Close a WebSocket politely, and in any case have its TCP connection gone on return."""
    try:
        await asyncio.wait_for(socket.close(), _CLOSE_SECONDS + 1)
    except (Exception, asyncio.CancelledError):
        logger.debug("Feishu long connection: the peer did not complete the close", exc_info=True)
    transport = getattr(socket, "transport", None)
    if transport is not None and not transport.is_closing():
        transport.abort()
    try:
        # The loop has to run for the transport to be released.
        await asyncio.wait_for(socket.wait_closed(), 1.0)
    except (Exception, asyncio.CancelledError):
        logger.debug("Feishu long connection: the connection was not released in time", exc_info=True)


def _kind(failure: BaseException) -> str:
    """A failure as it may be logged: what kind it is, and its number if it has one.

    Never its text. What Feishu, a proxy or a library puts into a message can
    carry the app secret or the connection's ticket, in spellings no filter
    can list.
    """
    number = getattr(failure, "errno", None)
    return f"{type(failure).__name__} {number}" if isinstance(number, int) else type(failure).__name__


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
        # A socket that was opened and is not yet known to be closed.
        self._held: Any = None
        # The thread of the last endpoint request, which may outlast the wait for it.
        self._endpoint_request: Optional[threading.Thread] = None

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
        """Run the connection on its own daemon thread until ``stop``; after a ``stop``, do nothing."""

        def serve() -> None:
            loop = asyncio.new_event_loop()
            try:
                self._loop = loop
                self._runner = loop.create_task(self.run())
                loop.run_until_complete(self._runner)
            except ConnectionRefused:
                pass  # logged where it was raised; the thread ends, as the SDK's did
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Feishu long connection: thread crashed")
            finally:
                self._end(loop)

        with self._stop_lock:
            if self._stopping or self._thread is not None:
                return
            thread = threading.Thread(target=serve, daemon=True, name=thread_name)
            thread.start()
            # Published only once it runs: a stop must find a thread it can wait for.
            self._thread = thread

    def _end(self, loop: asyncio.AbstractEventLoop) -> None:
        """Outside any task that a stop could cancel: no socket, no task and no loop are left behind."""
        try:
            held, self._held = self._held, None
            self._socket = None
            if held is not None:
                loop.run_until_complete(close_socket(held))
            tasks = list(asyncio.all_tasks(loop))
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except (Exception, asyncio.CancelledError):
            logger.debug("Feishu long connection: ending the loop failed", exc_info=True)
        finally:
            loop.close()

    def stop(self, timeout: float = 5.0) -> bool:
        """End the connection and its thread; returns whether the thread is gone."""
        with self._stop_lock:
            self._stopping = True
            thread, loop, runner = self._thread, self._loop, self._runner
            # Cancelled once: a second cancellation would land in the closing of the socket.
            cancel = not self._cancel_sent and loop is not None and runner is not None
            self._cancel_sent = self._cancel_sent or cancel
        if thread is None:
            return True
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
        try:
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            self._service = int(query["service_id"][0])
            device = query["device_id"][0]
            label = f"{parts.scheme}://{parts.netloc}{parts.path} [conn_id={device}]"
        except (KeyError, IndexError, ValueError):
            # Not said with the URL: it is the ticket. Feishu may do better next time.
            raise _Retry("the endpoint URL is unusable or names no device or service") from None
        try:
            # ``proxy=None``: straight to Feishu, as the adapter has always connected.
            # ``close_timeout``: a peer that does not complete a close is cut off after it.
            socket = await websockets.connect(url, proxy=None, close_timeout=_CLOSE_SECONDS, logger=_handshake_log)
        except InvalidStatus as refusal:
            raise self._handshake_refusal(refusal) from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _Retry(f"the handshake failed ({_kind(exc)})") from None
        # Ours from this line on, before anything can be awaited or cancelled.
        self._held = self._socket = socket
        if self._stopping:
            await self._disconnect()
            return
        self._label = label
        self.counts.connects += 1
        # The URL's query is the ticket; only where and which connection is logged.
        logger.info("Feishu long connection: connected to %s", self._label)

    async def _fetch_endpoint(self) -> str:
        """Ask Feishu where to connect.

        Sent with ``requests``, as the SDK's client sends it: which proxy it
        goes through, when it goes past one, and which certificates are
        trusted are then decided by the same library, from the same
        environment, as before. It runs beside the loop, so that a stop does
        not wait for it, and one at a time, so that requests that do not end
        cannot pile up.
        """
        if self._endpoint_request is not None and self._endpoint_request.is_alive():
            raise _Retry("the previous endpoint request has not ended")
        loop = asyncio.get_running_loop()
        answered: "asyncio.Future[Any]" = loop.create_future()
        url = self._base_url + ENDPOINT_PATH
        body = {"AppID": self._app_id, "AppSecret": self._app_secret}

        def deliver(outcome: Any) -> None:
            if not answered.done():
                answered.set_result(outcome)

        def send() -> None:
            try:
                import requests

                # The app secret goes to Feishu and nowhere a redirect points.
                reply = requests.post(
                    url, headers={"locale": "zh"}, json=body, timeout=_ENDPOINT_SOCKET_TIMEOUT, allow_redirects=False
                )
                outcome: Any = (reply.status_code, reply.content)
            except BaseException as exc:
                outcome = exc
            try:
                loop.call_soon_threadsafe(deliver, outcome)
            except RuntimeError:
                pass  # the connection ended meanwhile and took its loop with it

        self._endpoint_request = threading.Thread(target=send, daemon=True, name="feishu-ws-endpoint")
        self._endpoint_request.start()
        try:
            outcome = await asyncio.wait_for(answered, _ENDPOINT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise _Retry(f"no endpoint within {_ENDPOINT_TIMEOUT_SECONDS:g} s") from None
        if isinstance(outcome, BaseException):
            raise _Retry(f"the endpoint request failed ({_kind(outcome)})")
        status, raw = outcome
        try:
            answer = json.loads(raw.decode("utf-8"))
        except ValueError:
            answer = None
        if status != 200:
            raise _Retry(f"the endpoint answered HTTP {status}")
        if not isinstance(answer, dict):
            # Not Feishu's answer (a gateway's page, say): nothing final can be read from it.
            raise _Retry("the endpoint reply is not a JSON object")
        code = answer.get("code")
        if code == _SYSTEM_BUSY:
            raise _Retry("the endpoint is busy (code 1)")
        if code == _INTERNAL_ERROR:
            raise _Retry(f"the endpoint failed (code {_INTERNAL_ERROR})")
        if code != 0:
            # The code says why; Feishu's sentence about it is not repeated (see ``_kind``).
            raise ConnectionRefused(code if isinstance(code, int) else -1, "the endpoint refused the app")
        data = answer.get("data")
        url = data.get("URL") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise _Retry("the endpoint reply names no URL")
        self.config.update(data.get("ClientConfig"))
        return url

    def _handshake_refusal(self, refusal: InvalidStatus) -> Exception:
        headers = refusal.response.headers
        try:
            code = int(headers["Handshake-Status"])
            headers["Handshake-Msg"]  # the SDK's client reads the refusal only when both are there
            auth_code = int(headers.get("Handshake-Autherrcode") or 0)
        except (KeyError, ValueError):
            return _Retry(f"the handshake was refused (HTTP {refusal.response.status_code})")
        if code == _FORBIDDEN or (code == _AUTH_FAILED and auth_code == _EXCEED_CONNECTION_LIMIT):
            return ConnectionRefused(code, f"the handshake was refused for good (auth code {auth_code})")
        return _Retry(f"the handshake was refused (status {code}, auth code {auth_code})")

    async def _reconnect(self) -> None:
        if self.config.reconnect_nonce > 0:
            await asyncio.sleep(random.random() * self.config.reconnect_nonce)
        # How many attempts is settled here, as in the SDK; how long between them may still change.
        allowed = self.config.reconnect_count
        attempts = 0
        while not self._stopping:
            if 0 <= allowed <= attempts:
                raise ConnectionRefused(-1, f"unable to connect to the server after trying {allowed} times")
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
        await close_socket(socket)
        if self._held is socket:
            self._held = None
        logger.info("Feishu long connection: disconnected from %s", self._label)

    # --- frames ---------------------------------------------------------------

    async def _ping_forever(self) -> None:
        while True:
            if self._socket is not None:
                try:
                    await self._send(ping_frame(self._service))
                    self.counts.pings += 1
                except Exception as exc:
                    logger.warning("Feishu long connection: ping failed, err: %s", _kind(exc))
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
            closed = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
            reason = _kind(exc) + (f", close code {closed.code}" if closed is not None else "")
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
            logger.error("Feishu long connection: handle message failed, err: %s", _kind(exc))

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
        kind, message_id, total, index = event_piece(frame)
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
                exc if isinstance(exc, ValueError) else _kind(exc),  # the dispatcher's own words, or a kind
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

# An event this module joined that the other client has not handed on after this long never will be.
_TAP_PENDING_SECONDS = 30.0
_TAP_PENDING_LIMIT = 256


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
    hands to the dispatcher with what the dispatcher made of it, and each raw
    frame it sends. It works out from the received frames alone what this
    module would have done, and holds the other client to it:

    - a received frame must decode and re-encode as the other client's
      protobuf class does it (``reference_frame``);
    - an event this module would hand to the dispatcher must be handed on by
      the other client, with the same bytes, and no other;
    - the answer must be the bytes this module would send for the same frame
      and the same outcome of the dispatcher, and come within 30 seconds. Only
      the number in its timing header is taken from the other client's answer.

    A mismatch is counted and logged with the message id, never with content.
    Nothing here can reach the other client: every entry point contains its
    own failures.
    """

    def __init__(self, reference_frame: Any) -> None:
        self._reference_frame = reference_frame
        self._pieces = EventPieces()
        # Oldest first. What this module joined and expects to be handed on:
        # (when, message id, the payload, the frame that completed it).
        self._joined: List[Tuple[float, str, bytes, Frame]] = []
        # What was handed on and is to be answered:
        # (when, message id, the frame to answer, what the dispatcher returned, whether it raised).
        self._handled: List[Tuple[float, str, Frame, Any, bool]] = []
        self.counts = TapCounts()
        # The other client's thread reports frames; the adapter's loop asks for a check.
        self._lock = threading.RLock()

    def received(self, raw: bytes) -> None:
        """A frame the other client is about to handle."""
        with self._lock:
            self._received(raw)

    def handled(self, payload: bytes, result: Any, failed: bool) -> None:
        """A whole event payload the other client handed to the dispatcher, and what came of it."""
        with self._lock:
            self._was_handled(payload, result, failed)

    def sent(self, raw: bytes) -> None:
        """A frame the other client sent: a ping or an answer."""
        with self._lock:
            self._sent(raw)

    def _received(self, raw: bytes) -> None:
        try:
            self.counts.frames_in += 1
            self.check()
            reference = self._reference_frame()
            try:
                reference.ParseFromString(raw)
            except Exception:
                reference = None
            try:
                frame = decode_frame(raw)
            except FrameError:
                if reference is not None:
                    self._mismatch("a frame the other client reads is refused here", self._id_of(raw))
                return  # refused by both: agreement
            if reference is None:
                self._mismatch("a frame the other client refuses is read here", frame.header("message_id"))
                return
            try:
                theirs: Optional[bytes] = reference.SerializeToString()
            except Exception:
                theirs = None  # a required field is missing: their class cannot write it back
            try:
                ours: Optional[bytes] = encode_frame(frame)
            except FrameError:
                ours = None
            if ours != theirs or frame.headers != [(header.key, header.value) for header in reference.headers]:
                self._mismatch("a received frame decodes differently", frame.header("message_id"))
                return
            if frame.method != DATA:
                return
            try:
                kind, message_id, total, index = event_piece(frame)
                payload = self._pieces.add(message_id, total, index, frame.payload or b"")
            except FrameError:
                return  # this module would not hand it on; an answer to it will count as a mismatch
            if payload is not None and kind == "event":
                self._joined.append((time.monotonic(), message_id, payload, frame))
        except Exception as exc:
            self._mismatch(f"a received frame could not be read here ({type(exc).__name__})", self._id_of(raw))

    def _was_handled(self, payload: bytes, result: Any, failed: bool) -> None:
        try:
            self.counts.events += 1
            self.check()
            for position, (_, message_id, joined, frame) in enumerate(self._joined):
                if joined == payload:
                    del self._joined[position]
                    if not frame.incomplete:  # one that cannot be written back gets no answer from either
                        self._handled.append((time.monotonic(), message_id, frame, result, failed))
                    return
            self._mismatch("an event was handed on that this codec did not join, or joined differently", None)
        except Exception as exc:
            self._mismatch(f"a handed-on event could not be followed here ({type(exc).__name__})", None)

    def _sent(self, raw: bytes) -> None:
        try:
            self.counts.frames_out += 1
            self.check()
            frame = decode_frame(raw)
            if frame.method == CONTROL:
                if encode_frame(ping_frame(frame.service)) != raw:
                    self._mismatch("a ping is built differently", None)
                return
            message_id = frame.header("message_id") or ""
            position = next((at for at, entry in enumerate(self._handled) if entry[1] == message_id), None)
            if position is None:
                self._mismatch("an answer to an event this codec would not have handed on", message_id)
                return
            _, _, received, result, failed = self._handled.pop(position)
            timing = frame.header("biz_rt")
            if failed:
                ours = answer_frame(received, code=500, result=None, handled_ms=None)
            else:
                # How long handling took is the other client's to say; that it says so is not.
                ours = answer_frame(received, code=200, result=result, handled_ms=int(timing) if timing else 0)
            if (not failed and timing is None) or encode_frame(ours) != raw:
                self._mismatch("an answer is built differently", message_id)
        except Exception as exc:
            self._mismatch(f"a sent frame could not be read here ({type(exc).__name__})", self._id_of(raw))

    def check(self) -> None:
        """What was expected of the other client and has not come in time is a mismatch, not forgotten.

        Called on every frame, and by the adapter's periodic report, so that a
        quiet connection is judged too.
        """
        with self._lock:
            now = time.monotonic()
            for table, what in (
                (self._joined, "an event this codec joined was not handed on"),
                (self._handled, "an event that was handled was not answered"),
            ):
                while table and (now - table[0][0] > _TAP_PENDING_SECONDS or len(table) > _TAP_PENDING_LIMIT):
                    self._mismatch(what, table.pop(0)[1])

    def _id_of(self, raw: bytes) -> Optional[str]:
        """The message id as the other client's class reads it, for a frame this codec could not read."""
        try:
            reference = self._reference_frame()
            reference.ParseFromString(raw)
            return next((header.value for header in reference.headers if header.key == "message_id"), None)
        except Exception:
            return None

    def _mismatch(self, what: str, message_id: Optional[str]) -> None:
        self.counts.mismatches += 1
        logger.error("Feishu long connection tap: %s (message_id: %s)", what, message_id)
