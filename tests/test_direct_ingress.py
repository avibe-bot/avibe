"""Wire-level contract of the strict direct ingress (docs/plans/remote-direct-ingress.md).

Every test that claims something about the Web UI drives the real application
under real uvicorn listeners over real sockets: a CONNECT acceptor standing in
for the transport, the ingress TLS listener, and the ordinary loopback listener
as the control. An in-process ASGI client cannot see uvicorn's own proxy-header
handling or the socket peer, which is where this boundary lives.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import http.client
import json
import os
import socket
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.requests import Request as StarletteRequest
from websockets.sync.client import connect as websocket_connect

from tests.ui_server_test_helpers import _save_config, remote_session_cookie
from vibe import direct_ingress, remote_access, ui_compat, ui_server
from vibe.direct_ingress import (
    DIRECT_INGRESS_PEER_HOST,
    DIRECT_INGRESS_SCOPE_KEY,
    AsyncioByteStream,
    DirectIngress,
    DirectIngressError,
    DirectIngressGuard,
)

HOSTNAME = "alex.avibe.bot"
PEER = "peer-a"


# --- fixtures ---------------------------------------------------------------


@dataclass
class Certificate:
    certfile: str
    keyfile: str
    cafile: str
    serial: int


def _issue_certificate(directory: Path, hostname: str, name: str = "leaf") -> Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "direct ingress test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    certfile, keyfile, cafile = (directory / f"{name}.{kind}.pem" for kind in ("cert", "key", "ca"))
    certfile.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    cafile.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    keyfile.touch(mode=0o600)
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Certificate(str(certfile), str(keyfile), str(cafile), leaf.serial_number)


@dataclass
class Harness:
    ingress: DirectIngress
    transport_port: int
    certificate: Certificate
    main_port: int | None = None


@contextlib.asynccontextmanager
async def _running(app, tmp_path: Path, *, with_main_listener: bool = False, **ingress_options):
    """The ingress behind a TCP stand-in for the transport, plus optionally the
    application's ordinary loopback listener."""

    certificate = _issue_certificate(tmp_path, HOSTNAME)
    ingress = DirectIngress(
        app,
        hostname=HOSTNAME,
        certfile=certificate.certfile,
        keyfile=certificate.keyfile,
        **ingress_options,
    )
    await ingress.start()

    async def on_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await ingress.handle_stream(AsyncioByteStream(reader, writer), peer=PEER)

    transport = await asyncio.start_server(on_stream, "127.0.0.1", 0)
    harness = Harness(ingress, transport.sockets[0].getsockname()[1], certificate)
    main_server = main_task = None
    try:
        if with_main_listener:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            harness.main_port = listener.getsockname()[1]
            main_server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
            main_task = asyncio.create_task(main_server.serve(sockets=[listener]))
            while not main_server.started:
                await asyncio.sleep(0.01)
        yield harness
    finally:
        transport.close()
        await transport.wait_closed()
        await ingress.stop()
        if main_server is not None:
            main_server.should_exit = True
            await main_task


@pytest.fixture
def avibe_config(monkeypatch, tmp_path):
    """A cloud-enabled instance whose managed hostname is HOSTNAME."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    return _save_config(tmp_path)


# --- blocking wire client (run with asyncio.to_thread) ------------------------


def _connect(port: int, head: bytes | None = None) -> tuple[int, socket.socket]:
    """Send a CONNECT head to the transport stand-in; return its status and the socket."""

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(head if head is not None else _connect_head(f"{HOSTNAME}:443"))
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    status = int(response.split(b" ", 2)[1]) if response.startswith(b"HTTP/1.1 ") else 0
    return status, sock


def _connect_head(authority: str, host: str | None = None, method: str = "CONNECT") -> bytes:
    return f"{method} {authority} HTTP/1.1\r\nHost: {host or authority}\r\n\r\n".encode("ascii")


def _client_context(cafile: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.set_alpn_protocols(["h2", "http/1.1"])
    return context


def _tunnel(harness: Harness, *, sni: str | None = HOSTNAME) -> ssl.SSLSocket:
    status, sock = _connect(harness.transport_port)
    assert status == 200
    context = _client_context(harness.certificate.cafile)
    if sni is None:
        context.check_hostname = False
    return context.wrap_socket(sock, server_hostname=sni)


def _exchange(sock: socket.socket, request: bytes) -> tuple[int, http.client.HTTPMessage, bytes]:
    sock.sendall(request)
    response = http.client.HTTPResponse(sock)
    response.begin()
    return response.status, response.headers, response.read()


def _request(path: str, host: str, *headers: str, method: str = "GET") -> bytes:
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Accept: application/json", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _through_ingress(harness: Harness, request: bytes) -> tuple[int, http.client.HTTPMessage, bytes]:
    with _tunnel(harness) as tls:
        return _exchange(tls, request)


def _through_main_listener(harness: Harness, request: bytes) -> tuple[int, http.client.HTTPMessage, bytes]:
    with socket.create_connection(("127.0.0.1", harness.main_port), timeout=10) as sock:
        return _exchange(sock, request)


def _as_cloudflared(path: str, *headers: str) -> bytes:
    """The request cloudflared sends the loopback listener for a public client."""

    return _request(
        path,
        HOSTNAME,
        "X-Forwarded-For: 203.0.113.10",
        "X-Forwarded-Proto: https",
        "CF-Connecting-IP: 203.0.113.10",
        "CF-Ray: 8f0000000000abcd-SIN",
        "CF-Visitor: {\"scheme\":\"https\"}",
        "Cdn-Loop: cloudflare",
        *headers,
    )


# --- a minimal application for what the tunnel must carry ----------------------


class _Probe:
    """ASGI app: echoes the scope it was given, a WebSocket, and a gated stream."""

    def __init__(self) -> None:
        self.release_stream = threading.Event()
        self.calls = 0

    async def __call__(self, scope, receive, send) -> None:
        self.calls += 1
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            while (message := await receive())["type"] != "websocket.disconnect":
                await send({"type": "websocket.send", "text": message["text"]})
            return
        if scope["path"] == "/stream":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
            # The second event exists only after the client has read the first.
            assert await asyncio.to_thread(self.release_stream.wait, 10)
            await send({"type": "http.response.body", "body": b"data: two\n\n"})
            return
        body = json.dumps(
            {
                "headers": sorted(name.decode("latin-1") for name, _ in scope["headers"]),
                "host": dict(scope["headers"]).get(b"host", b"").decode("latin-1"),
                "client": list(scope["client"]),
                "scheme": scope["scheme"],
                "marker": scope.get(DIRECT_INGRESS_SCOPE_KEY),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --- the guard, at its own boundary ---------------------------------------------


def _scope(**overrides) -> dict:
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/api/config",
        "raw_path": b"/api/config",
        "headers": [(b"host", HOSTNAME.encode())],
        "client": ("127.0.0.1", 50000),
    }
    scope.update(overrides)
    return scope


async def _call_guard(scope: dict) -> tuple[list[dict], list[dict]]:
    seen: list[dict] = []
    sent: list[dict] = []

    async def inner(inner_scope, receive, send) -> None:
        seen.append(inner_scope)

    async def send(message: dict) -> None:
        sent.append(message)

    guard = DirectIngressGuard(inner, hostname=HOSTNAME, peer_label=lambda client: PEER)
    await guard(scope, None, send)
    return seen, sent


async def test_guard_removes_forwarding_metadata_and_replaces_the_peer():
    forged = [
        (b"host", f"{HOSTNAME}:443".upper().encode()),
        (b"forwarded", b"for=127.0.0.1;host=localhost;proto=http"),
        (b"x-forwarded-for", b"127.0.0.1"),
        (b"x-forwarded-host", b"localhost"),
        (b"x-forwarded-proto", b"http"),
        (b"x-forwarded-port", b"5123"),
        (b"x-forwarded-anything", b"1"),
        (b"x-real-ip", b"127.0.0.1"),
        (b"x-original-forwarded-for", b"127.0.0.1"),
        (b"true-client-ip", b"127.0.0.1"),
        (b"cf-connecting-ip", b"127.0.0.1"),
        (b"cf-ray", b"1"),
        (b"cf-visitor", b"{}"),
        (b"cf-ipcountry", b"XX"),
        (b"cf-access-jwt-assertion", b"x"),
        (b"cdn-loop", b"cloudflare"),
        (b"x-vibe-test-remote-addr", b"127.0.0.1"),
        (b"cookie", b"a=b"),
        (b"origin", b"https://alex.avibe.bot"),
    ]

    seen, sent = await _call_guard(_scope(headers=forged))

    assert sent == []
    (scope,) = seen
    assert scope["headers"] == [
        (b"host", HOSTNAME.encode()),
        (b"cookie", b"a=b"),
        (b"origin", b"https://alex.avibe.bot"),
    ]
    assert scope["client"] == (DIRECT_INGRESS_PEER_HOST, 0)
    assert scope[DIRECT_INGRESS_SCOPE_KEY] == {"peer": PEER}


@pytest.mark.parametrize(
    "overrides",
    [
        {"headers": [(b"host", b"localhost")]},
        {"headers": [(b"host", b"localhost:5123")]},
        {"headers": [(b"host", b"127.0.0.1")]},
        {"headers": [(b"host", b"[::1]")]},
        {"headers": [(b"host", b"evil.example")]},
        {"headers": [(b"host", b"alex.avibe.bot.")]},
        {"headers": [(b"host", b"alex.avibe.bot:8443")]},
        {"headers": [(b"host", b"sub.alex.avibe.bot")]},
        {"headers": [(b"host", b"")]},
        {"headers": []},
        {"headers": [(b"host", HOSTNAME.encode()), (b"host", b"localhost")]},
        {"headers": [(b"host", HOSTNAME.encode()), (b"host", HOSTNAME.encode())]},
        {"scheme": "http"},
        {"raw_path": b"http://localhost/api/config"},
        {"raw_path": b"*"},
    ],
)
@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_guard_refuses_without_calling_the_application(overrides, scope_type):
    scope = _scope(type=scope_type, **overrides)
    if scope_type == "websocket" and "scheme" not in overrides:
        scope["scheme"] = "wss"

    seen, sent = await _call_guard(scope)

    assert seen == []
    if scope_type == "websocket":
        assert sent == [{"type": "websocket.close", "code": 1008}]
    else:
        assert sent[0]["status"] == 421


# --- the CONNECT acceptor and the TLS listener ----------------------------------


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (_connect_head("localhost:443"), 403),
        (_connect_head("127.0.0.1:443"), 403),
        (_connect_head("other.avibe.bot:443"), 403),
        (_connect_head(f"{HOSTNAME}:5123"), 403),
        (_connect_head(f"{HOSTNAME}:80"), 403),
        (_connect_head(HOSTNAME), 403),
        (_connect_head(f"user@{HOSTNAME}:443"), 403),
        (_connect_head(f"{HOSTNAME}:443", host="localhost:443"), 403),
        (_connect_head(f"http://{HOSTNAME}/", host=HOSTNAME, method="GET"), 405),
        (_connect_head("/", host=HOSTNAME, method="GET"), 405),
        (f"CONNECT {HOSTNAME}:443 HTTP/1.0\r\n\r\n".encode(), 400),
        (f"CONNECT {HOSTNAME}:443 HTTP/1.1\r\n\r\n".encode(), 400),
        (f"CONNECT {HOSTNAME}:443 HTTP/1.1\r\nHost: {HOSTNAME}:443\r\nContent-Length: 4\r\n\r\nbody".encode(), 400),
        (f"CONNECT {HOSTNAME}:443 HTTP/1.1\r\nHost: {HOSTNAME}:443\r\nHost: localhost\r\n\r\n".encode(), 400),
        (b"CONNECT " + b"a" * 8192, 400),
        (b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\x00" * 64, 400),
    ],
)
async def test_connect_is_answered_only_for_this_instance_on_port_443(tmp_path, head, status):
    probe = _Probe()
    async with _running(probe, tmp_path) as harness:
        answered, sock = await asyncio.to_thread(_connect, harness.transport_port, head)
        closed = await asyncio.to_thread(sock.recv, 1)
        sock.close()

    assert answered == status
    assert closed == b""
    assert probe.calls == 0


async def test_connect_that_never_completes_is_dropped(tmp_path):
    async with _running(_Probe(), tmp_path, connect_timeout=0.2) as harness:
        answered, sock = await asyncio.to_thread(_connect, harness.transport_port, b"CONNECT alex")
        sock.close()

    assert answered == 0


@pytest.mark.parametrize("limits", [{"max_streams": 1}, {"max_streams_per_peer": 1}])
async def test_streams_past_the_limit_are_refused_and_the_slot_is_returned(tmp_path, limits):
    async with _running(_Probe(), tmp_path, **limits) as harness:
        first_status, first = await asyncio.to_thread(_connect, harness.transport_port)
        second_status, second = await asyncio.to_thread(_connect, harness.transport_port)
        second.close()
        first.close()
        # The slot comes back once the first stream's relay has unwound.
        for _ in range(200):
            third_status, third = await asyncio.to_thread(_connect, harness.transport_port)
            third.close()
            if third_status == 200:
                break
            await asyncio.sleep(0.01)

    assert (first_status, second_status, third_status) == (200, 503, 200)


@pytest.mark.parametrize("sni", ["localhost", "other.avibe.bot", f"sub.{HOSTNAME}", None])
async def test_tls_handshake_fails_without_this_instances_sni(tmp_path, sni):
    probe = _Probe()
    async with _running(probe, tmp_path) as harness:

        def handshake() -> None:
            status, sock = _connect(harness.transport_port)
            assert status == 200
            context = _client_context(harness.certificate.cafile)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with contextlib.closing(sock):
                context.wrap_socket(sock, server_hostname=sni)

        with pytest.raises((ssl.SSLError, ConnectionError)):
            await asyncio.to_thread(handshake)

    assert probe.calls == 0


async def test_tunnel_presents_trusted_metadata_only(tmp_path):
    async with _running(_Probe(), tmp_path) as harness:

        def fetch() -> tuple[str | None, dict]:
            with _tunnel(harness) as tls:
                protocol = tls.selected_alpn_protocol()
                status, _headers, body = _exchange(
                    tls, _request("/", f"{HOSTNAME}:443", "X-Forwarded-For: 127.0.0.1", "CF-Connecting-IP: 127.0.0.1")
                )
                assert status == 200
                return protocol, json.loads(body)

        protocol, seen = await asyncio.to_thread(fetch)

    assert protocol == "http/1.1"
    assert seen == {
        "headers": ["accept", "host"],
        "host": HOSTNAME,
        "client": [DIRECT_INGRESS_PEER_HOST, 0],
        "scheme": "https",
        "marker": {"peer": PEER},
    }


async def test_tunnel_carries_websockets_and_unbuffered_streams(tmp_path):
    probe = _Probe()
    async with _running(probe, tmp_path) as harness:

        def websocket_echo() -> str:
            status, sock = _connect(harness.transport_port)
            assert status == 200
            with websocket_connect(
                f"wss://{HOSTNAME}/ws",
                sock=sock,
                ssl=_client_context(harness.certificate.cafile),
                server_hostname=HOSTNAME,
                proxy=None,
            ) as websocket:
                websocket.send("ping")
                return websocket.recv()

        def stream() -> tuple[bytes, bytes]:
            with _tunnel(harness) as tls:
                tls.sendall(_request("/stream", HOSTNAME))
                response = http.client.HTTPResponse(tls)
                response.begin()
                first = response.read1(1024)
                probe.release_stream.set()
                return first, response.read()

        assert await asyncio.to_thread(websocket_echo) == "ping"
        first, rest = await asyncio.to_thread(stream)

    assert first == b"data: one\n\n"
    assert rest == b"data: two\n\n"


async def test_reloaded_certificate_serves_the_next_handshake(tmp_path):
    async with _running(_Probe(), tmp_path) as harness:
        renewed = _issue_certificate(tmp_path, HOSTNAME, name="renewed")

        def presented_serial(cafile: str) -> int:
            status, sock = _connect(harness.transport_port)
            assert status == 200
            with _client_context(cafile).wrap_socket(sock, server_hostname=HOSTNAME) as tls:
                return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True)).serial_number

        before = await asyncio.to_thread(presented_serial, harness.certificate.cafile)
        harness.ingress.reload_certificate(renewed.certfile, renewed.keyfile)
        after = await asyncio.to_thread(presented_serial, renewed.cafile)

    assert before == harness.certificate.serial
    assert after == renewed.serial


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
async def test_private_key_readable_by_others_is_refused(tmp_path):
    certificate = _issue_certificate(tmp_path, HOSTNAME)
    os.chmod(certificate.keyfile, 0o640)
    ingress = DirectIngress(
        _Probe(), hostname=HOSTNAME, certfile=certificate.certfile, keyfile=certificate.keyfile
    )

    with pytest.raises(DirectIngressError):
        await ingress.start()

    assert not ingress.running


@pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1", "[::1]", "*.avibe.bot", "alex.avibe.bot.", "alex.avibe.bot:443"])
def test_ingress_cannot_be_built_for_a_local_or_wildcard_name(tmp_path, hostname):
    with pytest.raises(DirectIngressError):
        DirectIngress(_Probe(), hostname=hostname, certfile="unused", keyfile="unused")


# --- the Web UI behind the ingress ------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:5123", "127.0.0.1", "127.0.0.1:5123", "[::1]", "[::1]:5123"],
)
async def test_local_host_inside_a_valid_connect_is_refused(avibe_config, tmp_path, host):
    """The naive bridge's bypass: a valid CONNECT and SNI, then a loopback Host."""

    async with _running(ui_server.app, tmp_path, with_main_listener=True) as harness:
        # Control: the same request on the ordinary loopback listener is the
        # local owner. Without it, a refusal here would prove nothing.
        owner_status, _headers, owner_body = await asyncio.to_thread(
            _through_main_listener, harness, _request("/api/config", host)
        )
        status, _headers, body = await asyncio.to_thread(_through_ingress, harness, _request("/api/config", host))
        session_status, _headers, session_body = await asyncio.to_thread(
            _through_ingress, harness, _request("/api/session", host)
        )

    assert owner_status == 200
    assert "platforms" in json.loads(owner_body)
    assert status == 421
    assert json.loads(body) == {"ok": False, "error": "direct_ingress_host_mismatch"}
    assert session_status == 421
    assert b"owner" not in session_body


def _remove_guard_layer(monkeypatch) -> None:
    """Let every Host through to the application, unchanged."""

    guarded_call = DirectIngressGuard.__call__

    async def keep_client_host(self, scope, receive, send) -> None:
        self._host_header = dict(scope["headers"]).get(b"host", b"")
        await guarded_call(self, scope, receive, send)

    monkeypatch.setattr(DirectIngressGuard, "_refusal", lambda self, scope: None)
    monkeypatch.setattr(DirectIngressGuard, "__call__", keep_client_host)


def _remove_marker_layer(monkeypatch) -> None:
    """Have the guard mark requests under a key no reader looks at."""

    monkeypatch.setattr(direct_ingress, "DIRECT_INGRESS_SCOPE_KEY", "unread")


def _remove_peer_layer(monkeypatch) -> None:
    """Show the application the relay's real loopback peer."""

    monkeypatch.setattr(direct_ingress, "DIRECT_INGRESS_PEER_HOST", "127.0.0.1")


_LAYER_REMOVERS = {"guard": _remove_guard_layer, "marker": _remove_marker_layer, "peer": _remove_peer_layer}


@pytest.mark.parametrize("kept", ["guard", "marker", "peer", None])
async def test_each_layer_alone_keeps_a_loopback_host_off_the_owner_path(avibe_config, tmp_path, monkeypatch, kept):
    """Defence in depth is real only if each layer holds without the others.

    ``kept=None`` removes all three and must reach the owner path: it proves the
    removals work and that the hazard is the one these layers exist for.
    """

    for layer, remove in _LAYER_REMOVERS.items():
        if layer != kept:
            remove(monkeypatch)
    async with _running(ui_server.app, tmp_path) as harness:
        status, _headers, body = await asyncio.to_thread(
            _through_ingress, harness, _request("/api/config", "localhost")
        )

    if kept is None:
        assert status == 200
        assert "platforms" in json.loads(body)
    else:
        assert status in {421, 503}
        assert json.loads(body)["ok"] is False


async def test_forged_forwarding_headers_do_not_change_the_answer(avibe_config, tmp_path):
    forged = (
        "Forwarded: for=127.0.0.1;host=localhost;proto=http",
        "X-Forwarded-For: 127.0.0.1",
        "X-Forwarded-Host: localhost",
        "X-Forwarded-Proto: http",
        "X-Real-IP: 127.0.0.1",
        "True-Client-IP: 127.0.0.1",
        "CF-Connecting-IP: 127.0.0.1",
    )
    async with _running(ui_server.app, tmp_path) as harness:
        plain = await asyncio.to_thread(_through_ingress, harness, _request("/api/config", HOSTNAME))
        results = [
            await asyncio.to_thread(_through_ingress, harness, _request("/api/config", HOSTNAME, header))
            for header in (*forged, "\r\n".join(forged))
        ]

    assert plain[0] == 401
    assert json.loads(plain[2]) == {"ok": False, "error": "remote_access_login_required"}
    assert [(status, body) for status, _headers, body in results] == [(plain[0], plain[2])] * len(results)


async def test_remote_authentication_matches_the_tunnel(avibe_config, tmp_path):
    cookie = "Cookie: {}={}".format(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(avibe_config, "alex@example.com", "sub_alex", role="viewer", access_source="email"),
    )
    cases = {
        "anonymous api": ("/api/config", ()),
        "anonymous session": ("/api/session", ()),
        "viewer api": ("/api/config", (cookie,)),
        "viewer session": ("/api/session", (cookie,)),
        "forged cookie": ("/api/config", (cookie + "x",)),
    }
    direct: dict[str, tuple[int, object]] = {}
    tunnel: dict[str, tuple[int, object]] = {}
    async with _running(ui_server.app, tmp_path, with_main_listener=True) as harness:
        for name, (path, headers) in cases.items():
            status, _headers, body = await asyncio.to_thread(
                _through_ingress, harness, _request(path, HOSTNAME, *headers)
            )
            direct[name] = (status, json.loads(body))
            status, _headers, body = await asyncio.to_thread(
                _through_main_listener, harness, _as_cloudflared(path, *headers)
            )
            tunnel[name] = (status, json.loads(body))

    assert direct == tunnel
    assert direct["anonymous api"] == (401, {"ok": False, "error": "remote_access_login_required"})
    assert direct["forged cookie"][0] == 401
    assert direct["viewer api"][0] == 200
    assert direct["viewer session"][1]["remote"] is True
    assert direct["viewer session"][1]["instance_role"] == "viewer"


async def test_cookies_set_through_the_ingress_are_secure(avibe_config, tmp_path):
    async with _running(ui_server.app, tmp_path) as harness:
        status, headers, _body = await asyncio.to_thread(
            _through_ingress, harness, _request("/api/csrf-token", HOSTNAME)
        )

    assert status == 200
    (cookie,) = headers.get_all("Set-Cookie")
    assert cookie.startswith("vibe_csrf_token=")
    assert "Secure" in cookie.split("; ")


async def test_loopback_only_websocket_is_closed_to_the_ingress(avibe_config, tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_UI_ENABLE_WS_ECHO", "1")
    upgrade = (
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
    )
    async with _running(ui_server.app, tmp_path, with_main_listener=True) as harness:

        def local_echo() -> str:
            with websocket_connect(f"ws://127.0.0.1:{harness.main_port}/ws/echo", proxy=None) as websocket:
                websocket.send("ping")
                return websocket.recv()

        # Control: the route answers a loopback client on the ordinary listener.
        assert "ping" in await asyncio.to_thread(local_echo)
        statuses = [
            (await asyncio.to_thread(_through_ingress, harness, _request("/ws/echo", host, *upgrade)))[0]
            for host in (HOSTNAME, "localhost", "127.0.0.1:5123")
        ]

    assert statuses == [403, 403, 403]


async def test_ingress_stops_answering_once_its_hostname_is_not_the_instances(avibe_config, tmp_path):
    async with _running(ui_server.app, tmp_path) as harness:
        before = await asyncio.to_thread(_through_ingress, harness, _request("/api/session", HOSTNAME))
        avibe_config.remote_access.vibe_cloud.public_url = "https://moved.avibe.bot"
        avibe_config.save()
        moved = await asyncio.to_thread(_through_ingress, harness, _request("/api/session", HOSTNAME))
        avibe_config.remote_access.vibe_cloud.public_url = f"https://{HOSTNAME}"
        avibe_config.remote_access.vibe_cloud.enabled = False
        avibe_config.save()
        disabled = await asyncio.to_thread(_through_ingress, harness, _request("/api/session", HOSTNAME))

    assert before[0] == 200
    assert json.loads(before[2])["remote"] is True
    for status, _headers, body in (moved, disabled):
        assert status == 421
        assert json.loads(body) == {"ok": False, "error": "direct_ingress_host_mismatch"}


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_no_route_answers_for_a_stale_ingress_hostname(avibe_config, scope_type):
    """Request hooks do not wrap WebSocket handlers, so the rule sits outside every route."""

    reached: list[str] = []

    async def inner(scope, receive, send) -> None:
        reached.append(scope["type"])

    async def call(marked: bool) -> list[dict]:
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        scope = _scope(type=scope_type)
        if marked:
            scope[DIRECT_INGRESS_SCOPE_KEY] = {"peer": PEER}
        await ui_server._DirectIngressLiveHostMiddleware(inner)(scope, None, send)
        return sent

    current = await call(marked=True)
    avibe_config.remote_access.vibe_cloud.enabled = False
    avibe_config.save()
    disabled = await call(marked=True)
    # Control: a request from any other listener is not this rule's business.
    unmarked = await call(marked=False)

    assert current == [] and unmarked == []
    assert reached == [scope_type, scope_type]
    if scope_type == "websocket":
        assert disabled == [{"type": "websocket.close", "code": 1008}]
    else:
        assert disabled[0]["status"] == 421


def test_auth_rate_limit_is_keyed_by_the_transport_peer():
    """The socket peer is always this process's relay; one noisy device must not
    spend every direct client's sign-in budget."""

    def client_id(scope_extra: dict) -> str:
        scope = _scope(headers=[(b"host", HOSTNAME.encode()), (b"cf-connecting-ip", b"198.51.100.7")], **scope_extra)
        token = ui_compat._request_var.set(ui_compat.CompatRequest(StarletteRequest(scope)))
        try:
            return ui_server._auth_client_id()
        finally:
            ui_compat._request_var.reset(token)

    assert client_id({"client": (DIRECT_INGRESS_PEER_HOST, 0), DIRECT_INGRESS_SCOPE_KEY: {"peer": "peer-a"}}) == "direct:peer-a"
    assert client_id({"client": (DIRECT_INGRESS_PEER_HOST, 0), DIRECT_INGRESS_SCOPE_KEY: {"peer": "peer-b"}}) == "direct:peer-b"
    # Control: without the marker, the same headers take the tunnel's key.
    assert client_id({"client": ("127.0.0.1", 50000)}) == "cf:198.51.100.7"
