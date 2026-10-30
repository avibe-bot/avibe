"""WF-LOCAL-002: actual hop transports must connect only to checked pins.

Fixtures own their loopback pins; production has no policy bypass. These tests
exercise real libcurl/HTTPcore sockets, not a mock implementing the pin policy.
"""
import gzip
import io
import logging
import time
import zlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import pytest

from core.handlers.model_hub import local_fetch
from core.web_fetch_worker import transport, worker
from core.web_fetch_worker.policy import CheckedTarget, FetchFailure, FetchURL, parse_url


# Static codec fixtures keep optional Python codecs out of every install/test
# environment. Both br/zstd payloads expand to exactly b"x" * 4096.
_COMPRESSED = {
    "gzip": gzip.compress(b"x" * 4096),
    "deflate": zlib.compress(b"x" * 4096),
    "deflate-raw": zlib.compress(b"x" * 4096)[2:-4],
    "br": bytes.fromhex("1bff0ff825f0e2b14020f70000"),
    "zstd": bytes.fromhex("28b52ffd60000f4d00001078780100fbf70116"),
}
_STREAMED_ZSTD = bytes.fromhex(
    "28b52ffd0048940100840241207265616461626c652064796e616d6963616c6c7920636f6d7072657373656420706167652e0a"
    "0100a8fe5fb92a034c000008640100fcff3910024d0000086f0100fc611d0801"
)


@pytest.fixture
def stub():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path.startswith(("/gemini-api/", "/oauth2", "/o/oauth2/", "/loop")):
                port = self.server.server_port
                routes = {
                    "/gemini-api/docs/thought-signatures": "/oauth2authorize",
                    "/oauth2authorize": f"http://accounts.example:{port}/o/oauth2/v2/auth",
                    "/o/oauth2/v2/auth": f"http://public.example:{port}/oauth2callback",
                    "/oauth2callback": "/gemini-api/docs/thought-signatures",
                    "/loop": "/loop",
                }
                if self.path == "/gemini-api/docs/thought-signatures" and "signin=autosignin" in self.headers.get("Cookie", ""):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown")
                    self.end_headers()
                    self.wfile.write(b"# Thought signatures | Gemini API\n\nA readable public document.")
                else:
                    self.send_response(302)
                    self.send_header("Location", routes[self.path])
                    if self.path == "/oauth2callback":
                        self.send_header("Set-Cookie", "signin=autosignin; Path=/; HttpOnly")
                    self.end_headers()
                return
            if self.path == "/challenge":
                self.send_response(202)
                self.send_header("x-amzn-waf-action", "challenge")
                self.end_headers()
                return
            if self.path == "/empty":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                return
            if self.path == "/slow-body":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "10")
                self.end_headers()
                time.sleep(2)
                return
            if self.path.startswith("/diagnostic"):
                self.send_response(302)
                self.send_header("Location", "/status/404")
                self.send_header("Set-Cookie", "private-cookie=never-log-this; Path=/")
                self.end_headers()
                return
            if self.path.startswith("/encoded/") or self.path == "/wire-padding":
                if self.path == "/wire-padding":
                    stream = io.BytesIO()
                    with gzip.GzipFile(filename="x" * 4096, mode="wb", fileobj=stream) as compressed:
                        compressed.write(b"tiny")
                    raw, encoding = stream.getvalue(), "gzip"
                else:
                    encoding = self.path.rsplit("/", 1)[-1]
                    raw = _STREAMED_ZSTD if encoding == "zstd-stream" else _COMPRESSED[encoding]
                    encoding = encoding.removesuffix("-stream").removesuffix("-raw")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Encoding", encoding)
                if self.path != "/wire-padding":
                    self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path.startswith("/status/"):
                self.send_response(int(self.path.rsplit("/", 1)[-1]))
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", "8")
                self.send_header("Retry-After", "3600")
                self.end_headers()
                try:
                    self.wfile.write(b"not gzip")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/trailer":
                self.protocol_version = "HTTP/1.1"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"4\r\nbody\r\n0\r\nContent-Type: application/pdf\r\nContent-Encoding: gzip\r\n\r\n")
                self.close_connection = True
                return
            if self.path.startswith("/oversized"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                if "announced" in self.path:
                    self.send_header("Content-Length", str(17 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(17 * 16):
                        self.wfile.write(b"x" * 65536)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            body = gzip.compress(b"# Pinned document\n\nThe bounded body.")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("native", [True, False])
def test_cookie_setting_signin_redirect_revisits_original_url(stub, monkeypatch, native):
    """WF-LOCAL-002: real sockets reproduce four redirects, then a cookie-only 200.

    The URL-only loop guard rejects the last request; ordinary redirect tests
    never revisit a URL after the origin's outgoing cookie header changes.
    """
    if native:
        pytest.importorskip("curl_cffi")
    port, requests = stub
    gates, events = [], []

    def resolve(url):
        gates.append(url)
        return CheckedTarget(parse_url(url), ("127.0.0.1",))

    monkeypatch.setattr(worker, "resolve_target", resolve)
    monkeypatch.setattr(transport, "native_available", lambda: (native, None if native else "native_library_unavailable"))
    url = f"http://public.example:{port}/gemini-api/docs/thought-signatures"
    result = worker.fetch_document(url, "en", events.append)
    assert result["final_url"] == url and "Thought signatures" in result["content"]
    assert len(requests) == 5  # Four redirects plus final fetch fits six-fetch bound.
    assert requests[-1][1]["Cookie"] == "signin=autosignin"
    assert all("Cookie" not in headers for _, headers in requests[:-1])
    assert len([event for event in events if event["event"] == "sent"]) == 1
    assert all(f"http://{headers['Host']}{path}" in gates for path, headers in requests)
    requests.clear()
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        worker.fetch_document(f"http://public.example:{port}/loop", "en", events.append)
    assert len(requests) == 1  # No cookie change: a genuine loop is still rejected.


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("path,status,hops,cause,code", [
    ("/diagnostic?private-query=do-not-log", 404, 2, "http_status", "fetch_target_unreadable"),
    ("/loop", 302, 1, "loop", "fetch_target_unreadable"),
    ("/challenge", 202, 1, "challenge", "fetch_challenge"),
    ("/empty", 200, 1, "extract_empty", "fetch_target_unreadable"),
    ("/slow-body", 200, 1, "timeout", "fetch_timeout"),
])
async def test_local_failure_logs_one_redacted_warning_from_real_worker(stub, monkeypatch, caplog, native, path, status, hops, cause, code):
    """The worker/parent IPC must preserve facts without persisting URL or headers."""
    if native:
        pytest.importorskip("curl_cffi")
    port, _ = stub
    script = '''import sys
sys.path.insert(0, sys.argv.pop(1))
from core.web_fetch_worker import worker, transport
from core.web_fetch_worker.policy import CheckedTarget, parse_url
worker.resolve_target = lambda url: CheckedTarget(parse_url(url), ("127.0.0.1",))
transport.native_available = lambda: (NATIVE, None if NATIVE else "native_library_unavailable")
original_hop = transport.curl_hop if NATIVE else transport.httpx_hop
def bounded_hop(target, headers, budget, timeout, on_sent):
    if target.address.url.endswith("/slow-body"):
        timeout = 1  # Transport timeout starts after worker startup, not before it.
    return original_hop(target, headers, budget, timeout, on_sent)
if NATIVE:
    transport.curl_hop = bounded_hop
else:
    transport.httpx_hop = bounded_hop
worker.main()
'''.replace("NATIVE", repr(native))
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    caplog.set_level(logging.WARNING, logger="core.handlers.model_hub.service_diagnostics")
    url = f"http://public.example:{port}{path}"
    result = await local_fetch.LocalFetchEngine().fetch(url, deadline=time.monotonic() + 10)
    assert result["error_code"] == code
    records = [r for r in caplog.records if r.name == "core.handlers.model_hub.service_diagnostics"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    for field in ("tool=web_fetch", "source_id=src_localfetch", f"error_code={code}",
                  f"http_status={status}", f"hop_count={hops}", f"cause={cause}",
                  f"transport={'native' if native else 'degraded'}", "elapsed_ms="):
        assert field in message
    assert int(message.split("elapsed_ms=")[1].split()[0]) >= 0
    for private in (url, "public.example", "accounts.example", "private-query", "private-cookie", "never-log-this", "not gzip"):
        assert private not in message
    assert "http_status" not in result and "hop_count" not in result and "cause" not in result


@pytest.mark.parametrize("native", [True, False])
def test_real_hop_uses_numeric_pin_original_host_and_no_ambient_credentials(stub, monkeypatch, tmp_path, native):
    if native:
        pytest.importorskip("curl_cffi")
    port, requests = stub
    # Test-owned transport fixture, never a production policy flag.
    address = FetchURL(f"http://public.example:{port}/doc?q=%2F", "public.example", port, "http", False)
    target = CheckedTarget(address, ("127.0.0.1",))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".netrc").write_text("machine public.example login secret-user password secret-password\n")
    original = socket.getaddrinfo
    def numeric_only(host, *args, **kwargs):
        assert host == "127.0.0.1", "HTTPcore attempted hostname DNS instead of a numeric pin"
        return original(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    headers = transport.request_headers(degraded=not native, locale="zh", previous_origin=None, target=target)
    sent = []
    result = (transport.curl_hop if native else transport.httpx_hop)(target, headers, transport.TransferBudget(), 3, lambda: sent.append(1))
    assert result.status == 200
    assert result.body == b"# Pinned document\n\nThe bounded body."
    assert sent == [1]
    path, got = requests[0]
    assert path == "/doc?q=%2F"
    assert got["Host"] == f"public.example:{port}"
    lower = {key.lower(): value for key, value in got.items()}
    assert not ({"authorization", "proxy-authorization", "cookie", "referer"} & lower.keys())
    assert lower["accept"] == transport.ACCEPT
    assert lower["accept-encoding"] == ("gzip, deflate, br, zstd" if native else "gzip, deflate")
    if native:
        assert "Chrome/150.0.0.0" in lower["user-agent"]
        assert '"Chromium";v="150"' in lower["sec-ch-ua"]
    else:
        assert lower["user-agent"].startswith("Avibe/")
        assert not any(key.startswith(("sec-ch-", "sec-fetch-")) for key in lower)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("announced", [True, False])
def test_real_transfer_aborts_oversize_instead_of_returning_partial_success(stub, native, announced):
    if native:
        pytest.importorskip("curl_cffi")
    port, requests = stub
    suffix = "-announced" if announced else ""
    address = FetchURL(f"http://public.example:{port}/oversized{suffix}", "public.example", port, "http", False)
    target = CheckedTarget(address, ("127.0.0.1",))
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        (transport.curl_hop if native else transport.httpx_hop)(target, {}, transport.TransferBudget(), 4, lambda: None)
    assert len(requests) == 1


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("status", [401, 429, 302, 403, 503])
def test_status_and_retry_after_survive_broken_compressed_error_body(stub, native, status):
    if native:
        pytest.importorskip("curl_cffi")
    port, requests = stub
    target = CheckedTarget(FetchURL(f"http://public.example:{port}/status/{status}", "public.example", port, "http", False), ("127.0.0.1",))
    response = (transport.curl_hop if native else transport.httpx_hop)(target, {}, transport.TransferBudget(), 3, lambda: None)
    assert response.status == status and response.header("retry-after") == "3600" and response.body == b""
    assert len(requests) == 1


@pytest.mark.parametrize("native", [True, False])
def test_real_chunked_trailers_cannot_change_representation(stub, native):
    if native:
        pytest.importorskip("curl_cffi")
    port, _ = stub
    target = CheckedTarget(FetchURL(f"http://public.example:{port}/trailer", "public.example", port, "http", False), ("127.0.0.1",))
    response = (transport.curl_hop if native else transport.httpx_hop)(target, {}, transport.TransferBudget(), 3, lambda: None)
    assert response.body == b"body" and response.header("content-type") == "text/plain"
    assert not response.header("content-encoding")


def test_header_budget_is_per_hop_and_conflicting_length_is_target_scoped():
    budget = transport.TransferBudget()
    for _ in range(6):
        receiver = transport._Receiver(budget)
        receiver.header(b"X-CSP: " + b"a" * 14000 + b"\r\n")
        receiver.header(b"\r\n")
        assert receiver.finish(302).status == 302
    receiver = transport._Receiver(budget)
    receiver.header(b"Content-Length: 1\r\n")
    receiver.header(b"Content-Length: 2\r\n")
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        receiver.header(b"\r\n")


@pytest.mark.parametrize("overage", [1, 2, 512])
def test_format_downgrade_rejects_spent_decode_budget_before_decoder(monkeypatch, overage):
    # A PDF on the preceding hop may consume more than the next text hop's cap.
    # In particular remaining+1 == -1 means an unbounded read to gzip/zstd.
    reads, original = [], gzip.GzipFile.read
    def observed(self, size=-1):
        reads.append(size)
        return original(self, size)
    monkeypatch.setattr(gzip.GzipFile, "read", observed)
    receiver = transport._Receiver(transport.TransferBudget(decoded=1024 + overage), limit=1024)
    receiver.header(b"Content-Encoding: gzip\r\n")
    receiver.body(gzip.compress(b"x" * 65536))
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        receiver.finish(200)
    assert reads == []


@pytest.mark.parametrize("headers", [
    [b"HTTP/1.1 103 Early Hints\r\n", b"Content-Type: application/pdf\r\n", b"\r\n", b"HTTP/1.1 200 OK\r\n"],
    [b"Content-Type: application/pdf\r\n"],
])
def test_final_content_type_owns_size_limit_not_interim_headers(headers):
    receiver = transport._Receiver(transport.TransferBudget())
    for line in [*headers, b"Content-Type: text/plain\r\n", b"\r\n"]:
        receiver.header(line)
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        receiver.body(b"x" * (transport.TEXT_LIMIT + 1))


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "deflate-raw", "br", "zstd"])
def test_real_codec_contract_counts_wire_and_decoded_bytes_separately(stub, native, encoding):
    if native:
        pytest.importorskip("curl_cffi")
    port, _ = stub
    target = CheckedTarget(FetchURL(f"http://public.example:{port}/encoded/{encoding}", "public.example", port, "http", False), ("127.0.0.1",))
    budget = transport.TransferBudget()
    if not native and encoding in {"br", "zstd"}:
        with pytest.raises(FetchFailure, match="fetch_unsupported_content"):
            transport.httpx_hop(target, {}, budget, 3, lambda: None)
        return
    response = (transport.curl_hop if native else transport.httpx_hop)(target, {}, budget, 3, lambda: None)
    assert response.body == b"x" * 4096
    assert budget.wire == len(_COMPRESSED[encoding]) and budget.decoded == 4096


def test_native_streamed_zstd_without_content_size(stub):
    pytest.importorskip("curl_cffi")
    port, _ = stub
    target = CheckedTarget(FetchURL(f"http://public.example:{port}/encoded/zstd-stream", "public.example", port, "http", False), ("127.0.0.1",))
    budget = transport.TransferBudget()
    response = transport.curl_hop(target, {}, budget, 3, lambda: None)
    assert response.body == b"A readable dynamically compressed page.\n" * 8000
    assert budget.wire == len(_STREAMED_ZSTD) and budget.decoded == len(response.body)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd"])
def test_native_decode_bomb_aborts_bounded_callback_before_retaining_excess(stub, monkeypatch, encoding):
    pytest.importorskip("curl_cffi")
    port, _ = stub
    target = CheckedTarget(FetchURL(f"http://public.example:{port}/encoded/{encoding}", "public.example", port, "http", False), ("127.0.0.1",))
    monkeypatch.setattr(transport, "TEXT_LIMIT", 1024)
    original, calls = transport._Receiver.body, []
    def observed(receiver, data):
        try:
            return original(receiver, data)
        finally:
            calls.append((len(data), len(receiver.raw)))
    monkeypatch.setattr(transport._Receiver, "body", observed)
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        transport.curl_hop(target, {}, transport.TransferBudget(), 3, lambda: None)
    assert calls and all(size <= 65536 and retained <= 1024 for size, retained in calls)


@pytest.mark.parametrize("native", [True, False])
def test_wire_budget_aborts_compressed_stream_with_tiny_decoded_body(stub, monkeypatch, native):
    if native:
        pytest.importorskip("curl_cffi")
    port, _ = stub
    target = CheckedTarget(FetchURL(f"http://public.example:{port}/wire-padding", "public.example", port, "http", False), ("127.0.0.1",))
    monkeypatch.setattr(transport, "TEXT_LIMIT", 1024)
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        (transport.curl_hop if native else transport.httpx_hop)(target, {}, transport.TransferBudget(), 3, lambda: None)


def test_backend_reserves_connect_budget_for_both_families_and_checks_peer(monkeypatch):
    target = CheckedTarget(FetchURL("https://example.com/", "example.com", 443, "https", False),
                           ("2001:4860::1", "8.8.8.8"))
    calls = []
    class Stream:
        def get_extra_info(self, info):
            return ("8.8.8.8", 443)
        def close(self):
            pass
    def connect(self, host, port, timeout=None, **kwargs):
        calls.append((host, timeout))
        if ":" in host:
            raise httpcore.ConnectTimeout()
        return Stream()
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = transport.PinnedBackend(target)
    stream = backend.connect_tcp("example.com", 443, timeout=5)
    assert [host for host, _ in calls] == ["2001:4860::1", "8.8.8.8"]
    assert 0 < calls[0][1] <= 2.5 and calls[0][1] < calls[1][1] <= 5
    with pytest.raises(FetchFailure, match="fetch_url_blocked"):
        stream.start_tls(None, "wrong.example", timeout=5)
    for host, port in (("other.example", 443), ("example.com", 80)):
        with pytest.raises(FetchFailure):
            backend.connect_tcp(host, port)
    with pytest.raises(FetchFailure):
        backend.connect_unix_socket("/tmp/socket")
    monkeypatch.setattr(Stream, "get_extra_info", lambda *a: ("127.0.0.1", 443))
    with pytest.raises(FetchFailure, match="fetch_url_blocked"):
        backend.connect_tcp("example.com", 443)


@pytest.mark.parametrize("encoding", ["gzip", "br", "zstd", "deflate"])
def test_bounded_decoders_reject_bombs_and_unadvertised_encoding(encoding):
    if encoding in {"gzip", "deflate"}:
        raw = gzip.compress(b"x" * 2048) if encoding == "gzip" else zlib.compress(b"x" * 2048)
    else:
        raw = _COMPRESSED[encoding]
    receiver = transport._Receiver(transport.TransferBudget(), limit=1024)
    receiver.header(f"Content-Encoding: {encoding}\r\n".encode())
    receiver.body(raw)
    with pytest.raises(FetchFailure):
        receiver.finish(200)


def test_announced_unknown_length_and_cumulative_limits():
    receiver = transport._Receiver(transport.TransferBudget(), limit=10)
    receiver.header(b"Content-Length: 11\r\n")
    with pytest.raises(FetchFailure):
        receiver.header(b"\r\n")
    budget = transport.TransferBudget()
    first = transport._Receiver(budget, limit=10)
    first.body(b"123456")
    first.finish(200)
    second = transport._Receiver(budget, limit=10)
    with pytest.raises(FetchFailure):
        second.body(b"12345")
    with pytest.raises(FetchFailure):
        transport._Receiver(transport.TransferBudget()).header(b"x:" + b"a" * 65536)


def test_browser_catalog_native_preset_and_degraded_profile():
    pytest.importorskip("curl_cffi")
    assert transport.native_available() == (True, None)
    target = CheckedTarget(FetchURL("https://example.com/", "example.com", 443, "https", False), ("8.8.8.8",))
    headers = transport.request_headers(degraded=False, locale="en", previous_origin=target.address.origin, target=target)
    assert list(headers) == transport.browser_profile()["header_order"]
    assert headers["Sec-Fetch-Site"] == "same-origin"
