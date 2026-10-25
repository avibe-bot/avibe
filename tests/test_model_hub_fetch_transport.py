"""WF-LOCAL-002: actual hop transports must connect only to checked pins.

Fixtures own their loopback pins; production has no policy bypass. These tests
exercise real libcurl/HTTPcore sockets, not a mock implementing the pin policy.
"""
import gzip
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import pytest

from core.handlers.model_hub import fetch_transport as transport
from core.handlers.model_hub.fetch_policy import CheckedTarget, FetchFailure, FetchURL


@pytest.fixture
def stub():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
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
    assert lower["accept-encoding"] == "gzip, br, zstd"
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


def test_streamed_zstd_without_content_size_uses_http_window():
    import zstandard
    body = b"A readable dynamically compressed page.\n" * 8000
    raw = zstandard.ZstdCompressor(write_content_size=False).compress(body)
    receiver = transport._Receiver(transport.TransferBudget())
    receiver.header(b"Content-Encoding: zstd\r\n")
    receiver.body(raw)
    assert receiver.finish(200).body == body


def test_brotli_caps_each_sdk_allocation_before_checking_aggregate(monkeypatch):
    import brotli
    raw = brotli.compress(b"x" * (4 * 1024 * 1024))
    actual, chunks = brotli.Decompressor, []
    class ObservedDecoder:
        def __init__(self):
            self.decoder = actual()
        def process(self, data, **kwargs):
            output = self.decoder.process(data, **kwargs)
            chunks.append((kwargs.get("output_buffer_limit"), len(output)))
            return output
        def can_accept_more_data(self):
            return self.decoder.can_accept_more_data()
        def is_finished(self):
            return self.decoder.is_finished()
    monkeypatch.setattr(brotli, "Decompressor", ObservedDecoder)
    receiver = transport._Receiver(transport.TransferBudget(), limit=2048)
    receiver.header(b"Content-Encoding: br\r\n")
    receiver.body(raw)
    with pytest.raises(FetchFailure):
        receiver.finish(200)
    assert chunks and all(limit is not None and limit <= 2049 and size <= 65536 for limit, size in chunks)


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
    if encoding == "gzip":
        raw = gzip.compress(b"x" * 2048)
    elif encoding == "br":
        import brotli
        raw = brotli.compress(b"x" * 2048)
    elif encoding == "zstd":
        import zstandard
        raw = zstandard.ZstdCompressor().compress(b"x" * 2048)
    else:
        raw = b"unadvertised"
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
