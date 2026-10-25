"""Hop-scoped, pinned transports. Called only inside the disposable worker."""
from __future__ import annotations

import gzip
import io
import ipaddress
import json
import ssl
import sys
import time
from dataclasses import dataclass, field
from importlib.metadata import version
from itertools import zip_longest
from pathlib import Path
from typing import Callable

import certifi
import httpcore
import httpx

from .fetch_policy import CheckedTarget, FetchFailure


ACCEPT = "text/markdown, text/html;q=0.95, application/xhtml+xml;q=0.9, text/plain;q=0.8, application/json;q=0.5, */*;q=0.1"
ENCODING = "gzip, br, zstd"
TEXT_LIMIT = 16 * 1024 * 1024
PDF_LIMIT = 32 * 1024 * 1024
HEADER_LIMIT = 64 * 1024


def browser_profile() -> dict:
    import vibe

    return json.loads((Path(vibe.__file__).parent / "data" / "web_fetch_browser.json").read_text(encoding="utf-8"))


def request_headers(*, degraded: bool, locale: str, previous_origin: tuple | None, target: CheckedTarget) -> dict[str, str]:
    common = {
        "Accept": ACCEPT, "Accept-Encoding": ENCODING,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8" if locale.startswith("zh") else "en-US,en;q=0.9",
    }
    if degraded:
        return {"User-Agent": f"Avibe/{version('avibe-os')}", **common}
    profile = browser_profile()
    platform = profile["platforms"].get(sys.platform, profile["platforms"]["linux"])
    headers = {
        "sec-ch-ua": profile["sec_ch_ua"], "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform["platform"]}"', "Upgrade-Insecure-Requests": "1",
        "User-Agent": f'Mozilla/5.0 ({platform["ua_os"]}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
        **common,
        "Sec-Fetch-Site": "none" if previous_origin is None else "same-origin" if previous_origin == target.address.origin else "cross-site",
        "Sec-Fetch-Mode": "navigate", "Sec-Fetch-User": "?1", "Sec-Fetch-Dest": "document",
    }
    return {key: headers[key] for key in profile["header_order"]}


def native_available() -> tuple[bool, str | None]:
    """Choose once, before sending. A target failure never changes transports."""
    try:
        from curl_cffi import Curl, __version__

        profile = browser_profile()
        if __version__ != profile["version"]:
            return False, "native_profile_version_mismatch"
        curl = Curl()
        try:
            if curl.impersonate(profile["impersonate"]) != 0:
                return False, "native_profile_unavailable"
        finally:
            curl.close()
    except (ImportError, OSError):
        return False, "native_library_unavailable"
    return True, None


@dataclass
class TransferBudget:
    wire: int = 0
    decoded: int = 0


@dataclass
class HopResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str:
        return next((v for k, v in reversed(self.headers) if k.lower() == name.lower()), "")


@dataclass
class _Receiver:
    budget: TransferBudget
    headers: list[tuple[str, str]] = field(default_factory=list)
    raw: bytearray = field(default_factory=bytearray)
    limit: int = TEXT_LIMIT
    status: int | None = None
    headers_complete: bool = False
    header_bytes: int = 0

    def header(self, data: bytes) -> int:
        self.header_bytes += len(data)
        if self.header_bytes > HEADER_LIMIT:
            raise FetchFailure("fetch_target_unreadable")
        if self.headers_complete:
            return len(data)  # Trailers cannot rewrite MIME, encoding, cookies or limits.
        if data.startswith(b"HTTP/"):
            self.headers.clear()
            self.limit = TEXT_LIMIT
            try:
                self.status = int(data.split()[1])
            except (ValueError, IndexError):
                raise FetchFailure("fetch_target_unreadable") from None
        elif b":" in data:
            k, v = data.decode("latin-1").split(":", 1)
            self.headers.append((k.lower(), v.strip()))
            if k.lower() == "content-type":
                self.limit = PDF_LIMIT if v.strip().split(";", 1)[0].lower() == "application/pdf" else TEXT_LIMIT
            if k.lower() == "content-length" and self.status in {None, 200}:
                try:
                    length = int(v.strip())
                except ValueError:
                    raise FetchFailure("fetch_target_unreadable") from None
                # Permit a PDF's length before its Content-Type header is received;
                # the final header terminator enforces the actual format's limit.
                if length < 0:
                    raise FetchFailure("fetch_target_unreadable")
                if self.budget.wire + length > PDF_LIMIT:
                    raise FetchFailure("fetch_target_unreadable")
        if data in (b"\r\n", b"\n"):
            self.headers_complete = self.status is None or self.status >= 200
            if self.status in {None, 200}:
                lengths = [int(v) for k, v in self.headers if k == "content-length"]
                if len(set(lengths)) > 1 or (lengths and self.budget.wire + lengths[0] > self.limit):
                    raise FetchFailure("fetch_target_unreadable")
        return len(data)

    def body(self, data: bytes) -> int:
        self.budget.wire += len(data)
        if self.budget.wire > self.limit:
            raise FetchFailure("fetch_target_unreadable")
        self.raw.extend(data)
        return len(data)

    def finish(self, status: int) -> HopResponse:
        if status != 200:
            # Refusals, redirects and Retry-After are header facts. Never let
            # a broken/oversized compressed error body overwrite them.
            return HopResponse(status, self.headers, b"")
        encoding = next((v.lower().strip() for k, v in self.headers if k == "content-encoding"), "identity")
        remaining = self.limit - self.budget.decoded
        if remaining < 0:
            # A preceding PDF hop may have spent more than this text cap.
            # Never pass a negative size (read-all) to a decoder.
            raise FetchFailure("fetch_target_unreadable")
        raw = bytes(self.raw)
        try:
            if encoding in ("identity", ""):
                body = raw
            elif encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    body = stream.read(remaining + 1)
            elif encoding == "zstd":
                import zstandard

                with zstandard.ZstdDecompressor(max_window_size=8 * 1024 * 1024).stream_reader(io.BytesIO(raw), read_across_frames=True) as stream:
                    body = stream.read(remaining + 1)
            elif encoding == "br":
                import brotli

                decoder = brotli.Decompressor()
                chunks = bytearray()
                for start in range(0, len(raw), 1024):
                    pending = raw[start:start + 1024]
                    while True:
                        chunks.extend(decoder.process(pending, output_buffer_limit=min(65536, remaining - len(chunks) + 1)))
                        if len(chunks) > remaining:
                            raise FetchFailure("fetch_target_unreadable")
                        if decoder.can_accept_more_data():
                            break
                        pending = b""
                if not decoder.is_finished():
                    raise FetchFailure("fetch_target_unreadable")
                body = bytes(chunks)
            else:
                raise FetchFailure("fetch_unsupported_content")
        except FetchFailure:
            raise
        except Exception:
            raise FetchFailure("fetch_target_unreadable") from None
        self.budget.decoded += len(body)
        if self.budget.decoded > self.limit:
            raise FetchFailure("fetch_target_unreadable")
        return HopResponse(status, self.headers, body)


def _peer(ip: str) -> str:
    value = ipaddress.ip_address(ip)
    return str(value.ipv4_mapped or value) if isinstance(value, ipaddress.IPv6Address) else str(value)


class _PinnedStream(httpcore.NetworkStream):
    def __init__(self, stream, backend):
        self.stream, self.backend = stream, backend

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout)

    def close(self):
        self.stream.close()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if server_hostname != self.backend.target.address.host:
            self.close()
            raise FetchFailure("fetch_url_blocked", terminal=True)
        remaining = self.backend.deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise httpcore.ConnectTimeout()
        return _PinnedStream(self.stream.start_tls(ssl_context, server_hostname, min(timeout or remaining, remaining)), self.backend)


class PinnedBackend(httpcore.NetworkBackend):
    """Only numeric connects; preserve original hostname for Host and TLS/SNI."""
    def __init__(self, target: CheckedTarget):
        self.target = target
        self.deadline = 0.0

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.target.address.host or port != self.target.address.port or local_address is not None:
            raise FetchFailure("fetch_url_blocked", terminal=True)
        self.deadline = time.monotonic() + min(timeout or 5.0, 5.0)
        # Interleave address families, reserving time for every checked address.
        v4 = [ip for ip in self.target.ips if ":" not in ip]
        v6 = [ip for ip in self.target.ips if ":" in ip]
        ips = [ip for pair in zip_longest(v6, v4) for ip in pair if ip]
        for index, ip in enumerate(ips):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                stream = httpcore.SyncBackend().connect_tcp(ip, port, remaining / (len(ips) - index), socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
            peer = stream.get_extra_info("server_addr")
            if not peer or _peer(peer[0]) not in {_peer(ip) for ip in self.target.ips}:
                stream.close()
                raise FetchFailure("fetch_url_blocked", terminal=True)
            return _PinnedStream(stream, self)
        raise httpcore.ConnectTimeout()

    def connect_unix_socket(self, *args, **kwargs):
        raise FetchFailure("fetch_url_blocked", terminal=True)


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self):
        self.stream.close()


class PinnedTransport(httpx.BaseTransport):
    """A new pool for exactly one checked hop. No proxy, retry, or coalescing."""
    def __init__(self, target: CheckedTarget):
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
            network_backend=PinnedBackend(target), http1=True, http2=True,
            max_connections=1, max_keepalive_connections=0, retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.pool.handle_request(httpcore.Request(
            method=request.method, url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                                                    port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        ))
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    def close(self):
        self.pool.close()


def httpx_hop(target: CheckedTarget, headers: dict, budget: TransferBudget, timeout: float, on_sent: Callable) -> HopResponse:
    receiver = _Receiver(budget)

    def trace(name, info):
        if name in {"http11.send_request_headers.started", "http2.send_request_headers.started"}:
            on_sent()

    with httpx.Client(transport=PinnedTransport(target), trust_env=False, follow_redirects=False,
                      timeout=httpx.Timeout(timeout, connect=min(5, timeout)), headers=headers) as client:
        with client.stream("GET", target.address.url, extensions={"trace": trace}) as response:
            receiver.status = response.status_code
            for k, v in response.headers.raw:
                receiver.header(k + b":" + v + b"\r\n")
            receiver.header(b"\r\n")
            if response.status_code != 200:
                return receiver.finish(response.status_code)
            for chunk in response.iter_raw(chunk_size=65536):
                receiver.body(chunk)
            return receiver.finish(response.status_code)


def curl_hop(target: CheckedTarget, headers: dict, budget: TransferBudget, timeout: float, on_sent: Callable) -> HopResponse:
    from curl_cffi import CurlHttpVersion, CurlOpt
    from curl_cffi.curl import CURL_WRITEFUNC_ERROR
    from curl_cffi.requests import Session

    receiver = _Receiver(budget)
    pins = ",".join(f"[{ip}]" if ":" in ip else ip for ip in target.ips)
    errors = []

    def guarded(callback):
        def call(data):
            try:
                return callback(data)
            except FetchFailure as error:
                errors.append(error)
                return CURL_WRITEFUNC_ERROR  # Returning zero is NOT an abort in curl_cffi.
        return call

    def header(data):
        receiver.header(data)
        return CURL_WRITEFUNC_ERROR if receiver.headers_complete and receiver.status != 200 else len(data)

    def debug(kind, data):
        if kind == 2:  # CURLINFO_HEADER_OUT: after connect/TLS, before write.
            on_sent()
        # Deliberately discard all bytes: never log cookies or headers.

    options = {
        CurlOpt.PROXY: "", CurlOpt.NETRC: 0, CurlOpt.FRESH_CONNECT: 1, CurlOpt.FORBID_REUSE: 1,
        CurlOpt.HTTP_VERSION: CurlHttpVersion.V2TLS, CurlOpt.HTTP_CONTENT_DECODING: 0,
        CurlOpt.CONNECTTIMEOUT_MS: max(1, int(min(5, timeout) * 1000)),
        CurlOpt.HEADERFUNCTION: guarded(header), CurlOpt.DEBUGFUNCTION: debug, CurlOpt.VERBOSE: 1,
        # A fresh handle with no ALTSVC/HSTS storage never learns a later-hop
        # destination. HTTP/3 is excluded above; no shared cache is attached.
        CurlOpt.HSTS_CTRL: 0,
    }
    if not target.address.literal:
        options[CurlOpt.RESOLVE] = [f"{target.address.host}:{target.address.port}:{pins}"]
    try:
        with Session(trust_env=False, curl_options=options, retry=0) as session:
            try:
                response = session.get(
                    target.address.url, headers=headers, impersonate=browser_profile()["impersonate"],
                    allow_redirects=False, verify=certifi.where(), timeout=timeout,
                    content_callback=guarded(receiver.body), accept_encoding=ENCODING,
                )
            except Exception as error:
                # A deliberate header-only abort still carries libcurl's peer
                # and status snapshot; validate the peer exactly as on success.
                response = getattr(error, "response", None)
                if not receiver.headers_complete or receiver.status == 200 or response is None:
                    raise
            if errors:
                raise errors[0]
            if _peer(response.primary_ip) not in {_peer(ip) for ip in target.ips}:
                raise FetchFailure("fetch_url_blocked", terminal=True)
            return receiver.finish(response.status_code)
    except Exception:
        if errors:
            raise errors[0] from None
        raise
