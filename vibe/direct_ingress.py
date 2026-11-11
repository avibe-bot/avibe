"""Strict TLS ingress for remote direct paths.

A direct transport (LAN or IPv6 today, a relay later) carries the browser's
connection to the instance without Cloudflare. This module is the only door
such a connection may use. Its contract is ``docs/plans/remote-direct-ingress.md``:

* each transport stream starts with an HTTP/1.1 ``CONNECT`` for exactly this
  instance's hostname and port 443; nothing else is answered;
* the tunnelled bytes go to one fixed loopback TLS listener and nowhere else,
  whatever the client asked for;
* that listener accepts only this hostname as TLS SNI and as HTTP ``Host``,
  drops client-supplied forwarding metadata, and marks the request so the Web
  UI treats it as remote, never as the local owner.

Nothing here authenticates a user. A request that passes still meets the same
remote session, role, CSRF, and Origin checks as one arriving through the
tunnel.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import socket
import ssl
import stat
from typing import Any, Callable, Protocol

import h11

logger = logging.getLogger(__name__)

# ASGI scope key set on every request that entered through the direct ingress.
DIRECT_INGRESS_SCOPE_KEY = "vibe_direct_ingress"
# The client host such a request carries. It is deliberately not an IP address:
# every trust decision keyed on a loopback, private, or proxy peer parses the
# client host as an address, so none of them can match it.
DIRECT_INGRESS_PEER_HOST = "direct-ingress"

DIRECT_INGRESS_PORT = 443
_UNKNOWN_PEER = "unknown"

_CONNECT_HEAD_LIMIT_BYTES = 4096
_CONNECT_TIMEOUT_SECONDS = 5.0
_RELAY_CHUNK_BYTES = 64 * 1024
# How long a tunnel may outlive the client finishing its side; the listener
# closes as soon as it sees that, so this only bounds a stuck connection.
_HALF_CLOSE_LINGER_SECONDS = 30.0
_MAX_CONCURRENT_STREAMS = 256
# A browser opens a handful of connections per origin plus its WebSockets.
_MAX_CONCURRENT_STREAMS_PER_PEER = 64
# The main listener's parser takes larger request heads than h11's default.
_REQUEST_HEAD_LIMIT_BYTES = 64 * 1024
# Matches the main listener: long enough that the client, not the server, ends
# an idle connection (see ``_UI_KEEPALIVE_TIMEOUT_SECONDS`` in ui_server).
_KEEPALIVE_TIMEOUT_SECONDS = 120
_GRACEFUL_SHUTDOWN_SECONDS = 5

_HOSTNAME_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PEER_LABEL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# Request headers a client could use to claim a proxy hop, a scheme, a host, or
# an address. The ingress is the first hop, so every one of them is a forgery.
_STRIPPED_HEADER_NAMES = frozenset(
    {
        b"forwarded",
        b"x-real-ip",
        b"x-original-forwarded-for",
        b"true-client-ip",
        b"x-client-ip",
        b"x-cluster-client-ip",
        b"cdn-loop",
        b"x-vibe-test-remote-addr",
    }
)
_STRIPPED_HEADER_PREFIXES = (b"x-forwarded-", b"cf-")


class DirectIngressError(Exception):
    """The ingress cannot be configured or started as requested."""


class ByteStream(Protocol):
    """One bidirectional transport stream, as the ingress needs it."""

    async def read(self, max_bytes: int) -> bytes:
        """Return up to ``max_bytes``; ``b""`` once the peer has finished sending."""

    async def write(self, data: bytes) -> None:
        """Send ``data``, waiting while the peer cannot take more."""

    async def close_write(self) -> None:
        """Finish sending; the peer may still send."""

    async def aclose(self) -> None:
        """Release the stream in both directions."""


class AsyncioByteStream:
    """A :class:`ByteStream` over an asyncio reader/writer pair."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int) -> bytes:
        return await self._reader.read(max_bytes)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def close_write(self) -> None:
        if self._writer.can_write_eof():
            self._writer.write_eof()

    async def aclose(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


def normalize_ingress_hostname(value: str) -> str:
    """Return ``value`` as the one hostname an ingress may serve, or raise.

    The ingress serves a DNS name it holds a public certificate for. An IP
    literal, ``localhost``, a wildcard, a port, or a trailing dot is refused
    here so no later comparison has to reason about them.
    """

    hostname = (value or "").strip().lower()
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or len(labels) < 2
        or any(_HOSTNAME_LABEL_RE.fullmatch(label) is None for label in labels)
        or labels[-1].isdigit()
    ):
        raise DirectIngressError(f"not a servable hostname: {value!r}")
    return hostname


def _host_header_matches(value: bytes, hostname: str) -> bool:
    host = value.decode("latin-1").lower()
    return host == hostname or host == f"{hostname}:{DIRECT_INGRESS_PORT}"


def _is_stripped_header(name: bytes) -> bool:
    lowered = name.lower()
    return lowered in _STRIPPED_HEADER_NAMES or lowered.startswith(_STRIPPED_HEADER_PREFIXES)


class DirectIngressGuard:
    """ASGI boundary in front of the Web UI for the direct-ingress listener.

    It refuses a request whose ``Host`` is not this instance's hostname, whose
    target is not origin-form, or that did not arrive over TLS. For the rest it
    removes forwarding metadata, replaces the socket peer (always this
    process's own loopback relay) with :data:`DIRECT_INGRESS_PEER_HOST`, and
    sets :data:`DIRECT_INGRESS_SCOPE_KEY` to the transport peer's label.
    """

    def __init__(
        self,
        app: Any,
        *,
        hostname: str,
        peer_label: Callable[[Any], str] | None = None,
    ) -> None:
        self.app = app
        self.hostname = normalize_ingress_hostname(hostname)
        self._host_header = self.hostname.encode("ascii")
        self._peer_label = peer_label or (lambda client: _UNKNOWN_PEER)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            # The main listener owns the application lifespan.
            return
        reason = self._refusal(scope)
        if reason is not None:
            await self._refuse(scope_type, reason, send)
            return
        guarded = dict(scope)
        # One canonical Host reaches the application: the bare hostname.
        guarded["headers"] = [
            (name, self._host_header if name.lower() == b"host" else value)
            for name, value in scope.get("headers") or ()
            if not _is_stripped_header(name)
        ]
        guarded[DIRECT_INGRESS_SCOPE_KEY] = {"peer": self._peer_label(scope.get("client"))}
        guarded["client"] = (DIRECT_INGRESS_PEER_HOST, 0)
        await self.app(guarded, receive, send)

    def _refusal(self, scope: Any) -> str | None:
        if scope.get("scheme") not in {"https", "wss"}:
            return "direct_ingress_requires_tls"
        hosts = [value for name, value in scope.get("headers") or () if name.lower() == b"host"]
        if len(hosts) != 1 or not _host_header_matches(hosts[0], self.hostname):
            return "direct_ingress_host_mismatch"
        raw_path = scope.get("raw_path")
        if not isinstance(raw_path, (bytes, bytearray)):
            raw_path = str(scope.get("path") or "").encode("latin-1", "replace")
        if not raw_path.startswith(b"/"):
            # Absolute-form and asterisk-form targets name an authority of
            # their own; only origin-form is unambiguous behind a pinned Host.
            return "direct_ingress_target_not_origin_form"
        return None

    @staticmethod
    async def _refuse(scope_type: str, reason: str, send: Any) -> None:
        if scope_type == "websocket":
            # Closing before accept answers the handshake with HTTP 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = ('{"ok":false,"error":"%s"}' % reason).encode("ascii")
        await send(
            {
                "type": "http.response.start",
                # 421 Misdirected Request: this listener is not the origin for
                # the authority the request names.
                "status": 421,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _require_private_key_file(keyfile: str) -> None:
    """Refuse a TLS key other local users can read.

    The key is as sensitive as the tunnel credential. POSIX only: Windows has
    no equivalent mode bits, and its per-user profile directory is the bound.
    """

    if os.name != "posix":
        return
    try:
        mode = os.stat(keyfile).st_mode
    except OSError as exc:
        raise DirectIngressError("TLS private key is not readable") from exc
    if not stat.S_ISREG(mode) or mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DirectIngressError("TLS private key must be a regular file readable by its owner only")


def _load_certificate_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    _require_private_key_file(keyfile)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # HTTP/1.1 only: the Web UI server has no HTTP/2 authority handling.
    context.set_alpn_protocols(["http/1.1"])
    try:
        context.load_cert_chain(certfile, keyfile)
    except (OSError, ssl.SSLError) as exc:
        raise DirectIngressError("TLS certificate or private key could not be loaded") from exc
    return context


class DirectIngress:
    """The instance's direct-ingress boundary: CONNECT acceptor plus TLS listener.

    ``start`` binds the TLS listener on loopback and serves ``app`` behind
    :class:`DirectIngressGuard`. A transport then passes every accepted stream
    to :meth:`handle_stream`. The object dials nothing but its own listener.
    """

    def __init__(
        self,
        app: Any,
        *,
        hostname: str,
        certfile: str,
        keyfile: str,
        connect_timeout: float = _CONNECT_TIMEOUT_SECONDS,
        max_streams: int = _MAX_CONCURRENT_STREAMS,
        max_streams_per_peer: int = _MAX_CONCURRENT_STREAMS_PER_PEER,
    ) -> None:
        self.hostname = normalize_ingress_hostname(hostname)
        self._authority = f"{self.hostname}:{DIRECT_INGRESS_PORT}".encode("ascii")
        self._app = app
        self._certfile = certfile
        self._keyfile = keyfile
        self._connect_timeout = connect_timeout
        self._max_streams = max_streams
        self._max_streams_per_peer = max_streams_per_peer
        # Open streams per transport peer; the total is the sum.
        self._active_streams: dict[str, int] = {}
        self._certificate_context: ssl.SSLContext | None = None
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._address: tuple[str, int] | None = None
        # Local port of each relay connection to the listener -> transport peer.
        self._relay_peers: dict[int, str] = {}

    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise DirectIngressError("direct ingress is not running")
        return self._address

    @property
    def running(self) -> bool:
        return self._serve_task is not None and not self._serve_task.done()

    async def start(self) -> None:
        if self.running:
            return
        import uvicorn

        self._certificate_context = _load_certificate_context(self._certfile, self._keyfile)
        listener_context = _load_certificate_context(self._certfile, self._keyfile)
        listener_context.sni_callback = self._select_certificate

        class _EmbeddedServer(uvicorn.Server):
            # The main UI server owns process signals; this listener is
            # stopped through ``DirectIngress.stop``.
            @contextlib.contextmanager
            def capture_signals(self):
                yield

            def install_signal_handlers(self) -> None:  # uvicorn < 0.29
                return None

        config = uvicorn.Config(
            DirectIngressGuard(self._app, hostname=self.hostname, peer_label=self._relay_peer_label),
            # h11 reports the request target as sent, so the guard can refuse
            # absolute-form targets instead of having them silently rewritten.
            http="h11",
            h11_max_incomplete_event_size=_REQUEST_HEAD_LIMIT_BYTES,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            # No forwarding header is trusted on this listener, at any layer.
            proxy_headers=False,
            server_header=False,
            log_config=None,
            access_log=False,
            timeout_keep_alive=_KEEPALIVE_TIMEOUT_SECONDS,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
        )
        config.load()
        config.ssl = listener_context

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
        except OSError:
            sock.close()
            raise
        self._address = ("127.0.0.1", sock.getsockname()[1])
        self._server = _EmbeddedServer(config)
        self._serve_task = asyncio.create_task(self._server.serve(sockets=[sock]), name="direct-ingress-listener")
        while not self._server.started:
            if self._serve_task.done():
                self._address = None
                sock.close()
                self._serve_task.result()
                raise DirectIngressError("direct ingress listener exited during startup")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        task, self._serve_task = self._serve_task, None
        server, self._server = self._server, None
        self._address = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def reload_certificate(self, certfile: str | None = None, keyfile: str | None = None) -> None:
        """Present a renewed certificate to handshakes that start from now on."""

        certfile = certfile or self._certfile
        keyfile = keyfile or self._keyfile
        self._certificate_context = _load_certificate_context(certfile, keyfile)
        self._certfile = certfile
        self._keyfile = keyfile

    def _select_certificate(self, ssl_object: Any, server_name: str | None, _context: Any) -> int | None:
        if server_name is None or server_name.lower() != self.hostname:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        ssl_object.context = self._certificate_context
        return None

    def _relay_peer_label(self, client: Any) -> str:
        if isinstance(client, (tuple, list)) and len(client) == 2 and client[0] == "127.0.0.1":
            return self._relay_peers.get(client[1], _UNKNOWN_PEER)
        return _UNKNOWN_PEER

    async def handle_stream(self, stream: ByteStream, *, peer: str) -> None:
        """Serve one transport stream until either side ends it.

        ``peer`` names the transport peer (for example its endpoint ID). It is
        metadata for rate limiting and logs only, never an authorization input.
        """

        peer = peer if _PEER_LABEL_RE.fullmatch(peer or "") else _UNKNOWN_PEER
        if (
            not self.running
            or sum(self._active_streams.values()) >= self._max_streams
            or self._active_streams.get(peer, 0) >= self._max_streams_per_peer
        ):
            with contextlib.suppress(Exception):
                await stream.write(_refusal_response(503))
            await stream.aclose()
            return
        self._active_streams[peer] = self._active_streams.get(peer, 0) + 1
        try:
            try:
                early_data = await asyncio.wait_for(self._accept_connect(stream), self._connect_timeout)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return
            if early_data is None:
                return
            await self._relay(stream, early_data, peer)
        finally:
            remaining = self._active_streams[peer] - 1
            if remaining:
                self._active_streams[peer] = remaining
            else:
                del self._active_streams[peer]
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def _accept_connect(self, stream: ByteStream) -> bytes | None:
        """Answer the stream's CONNECT; return bytes that followed it, or None if refused."""

        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=_CONNECT_HEAD_LIMIT_BYTES)
        request: h11.Request | None = None
        try:
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    data = await stream.read(_CONNECT_HEAD_LIMIT_BYTES)
                    if not data:
                        return None
                    connection.receive_data(data)
                elif isinstance(event, h11.Request) and request is None:
                    request = event
                elif isinstance(event, h11.EndOfMessage) and request is not None:
                    break
                else:
                    # A body, a second request, or an early close.
                    await stream.write(_refusal_response(400))
                    return None
        except h11.RemoteProtocolError:
            await stream.write(_refusal_response(400))
            return None

        status = self._connect_refusal(request)
        if status is not None:
            await stream.write(_refusal_response(status))
            return None
        await stream.write(connection.send(h11.Response(status_code=200, headers=[], reason=b"Connection Established")))
        early_data, _closed = connection.trailing_data
        return bytes(early_data)

    def _connect_refusal(self, request: h11.Request) -> int | None:
        if request.method != b"CONNECT":
            return 405
        if request.http_version != b"1.1":
            return 400
        headers = dict(request.headers)
        if b"content-length" in headers or b"transfer-encoding" in headers:
            return 400
        # h11 has already required exactly one Host header.
        if request.target.lower() != self._authority or headers.get(b"host", b"").lower() != self._authority:
            return 403
        return None

    async def _relay(self, stream: ByteStream, early_data: bytes, peer: str) -> None:
        host, port = self.address
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            logger.warning("direct ingress listener refused its own relay connection")
            return
        relay_port = writer.get_extra_info("sockname")[1]
        self._relay_peers[relay_port] = peer

        async def client_to_listener() -> None:
            data = early_data
            while True:
                if data:
                    writer.write(data)
                    await writer.drain()
                data = await stream.read(_RELAY_CHUNK_BYTES)
                if not data:
                    break
            if writer.can_write_eof():
                writer.write_eof()

        async def listener_to_client() -> None:
            while True:
                data = await reader.read(_RELAY_CHUNK_BYTES)
                if not data:
                    break
                await stream.write(data)
            await stream.close_write()

        upload = asyncio.create_task(client_to_listener())
        download = asyncio.create_task(listener_to_client())
        try:
            done, _pending = await asyncio.wait({upload, download}, return_when=asyncio.FIRST_COMPLETED)
            if download not in done and upload.exception() is None:
                # The client finished its side cleanly. Responses still in
                # flight must reach it; the listener then closes.
                await asyncio.wait({download}, timeout=_HALF_CLOSE_LINGER_SECONDS)
        finally:
            for task in (upload, download):
                task.cancel()
            await asyncio.gather(upload, download, return_exceptions=True)
            self._relay_peers.pop(relay_port, None)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _refusal_response(status: int) -> bytes:
    reason = {400: b"Bad Request", 403: b"Forbidden", 405: b"Method Not Allowed", 503: b"Service Unavailable"}[status]
    return b"HTTP/1.1 %d %s\r\nConnection: close\r\nContent-Length: 0\r\n\r\n" % (status, reason)

