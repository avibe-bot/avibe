"""Disposable web_fetch worker: all DNS, sockets and parsers die with this process.

The parent supervises a 30s wall deadline and RSS ceiling. POSIX adds kernel
CPU/address-space limits; Windows adds a process-memory Job limit. IPC carries
only bounded facts (never cookies, headers, or credentials).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from .policy import FetchFailure, parse_url, resolve_target


MEMORY_LIMIT = 768 * 1024 * 1024
_JOB = None


def resource_limits() -> None:
    global _JOB
    if os.name == "posix":
        import resource

        def cap(kind, soft, hard):
            _, inherited = resource.getrlimit(kind)
            if inherited != resource.RLIM_INFINITY:
                soft, hard = min(soft, inherited), min(hard, inherited)
            resource.setrlimit(kind, (soft, hard))

        cap(resource.RLIMIT_CPU, 30, 31)
        # macOS has no enforceable per-process address-space limit here; the
        # parent supervises RSS. Linux additionally enforces this in the kernel.
        if sys.platform != "darwin":
            cap(resource.RLIMIT_AS, MEMORY_LIMIT, MEMORY_LIMIT)
        cap(resource.RLIMIT_FSIZE, 1024 * 1024, 1024 * 1024)
    elif os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class Limits(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _JOB = kernel.CreateJobObjectW(None, None)
        limits = Limits()
        limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        limits.process_memory = MEMORY_LIMIT
        if (not _JOB or not kernel.SetInformationJobObject(_JOB, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(_JOB, kernel.GetCurrentProcess())):
            raise FetchFailure("fetch_unavailable")


class OriginCookies:
    def __init__(self):
        self.values = {}

    def receive(self, address, headers):
        for name, value in headers:
            if name.lower() != "set-cookie" or len(value.encode("utf-8")) > 4096:
                continue
            # RFC 6265's response parser has ONE name=value pair. SimpleCookie
            # parses a Cookie header instead and mistakes unknown attributes for
            # more cookies (or drops modern Partitioned/Priority cookies).
            pair, *pieces = value.split(";")
            if "=" not in pair:
                continue
            key, content = (part.strip() for part in pair.split("=", 1))
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(ord(c) < 32 or ord(c) > 126 for c in content):
                continue
            attributes = {}
            for piece in pieces:
                name, _, attribute = piece.strip().partition("=")
                attributes[name.lower()] = attribute.strip()
            secure = "secure" in attributes
            domain = attributes.get("domain", "").lstrip(".").lower()
            if (domain and not (address.host == domain or (not address.literal and "." in domain and address.host.endswith("." + domain)))):
                continue
            if secure and address.scheme != "https":
                continue
            if key.startswith(("__Secure-", "__Host-")) and not secure:
                continue
            if key.startswith("__Host-") and (domain or attributes.get("path") != "/"):
                continue
            default_path = urlsplit(address.url).path.rpartition("/")[0] or "/"
            path = attributes.get("path", default_path)
            if not path.startswith("/"):
                path = default_path
            identity = (address.origin, path, key)
            expiry = float("inf")
            try:
                if attributes.get("expires"):
                    expiry = parsedate_to_datetime(attributes["expires"]).timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
            try:
                if re.fullmatch(r"-?[0-9]+", attributes.get("max-age", "")):
                    expiry = time.time() + int(attributes["max-age"])
            except (ValueError, OverflowError):
                pass
            if expiry <= time.time():
                self.values.pop(identity, None)
            elif identity in self.values or len(self.values) < 64:
                # Domain influences acceptance only, never where a cookie goes.
                self.values[identity] = (f"{key}={content}", secure, expiry)

    def header(self, address):
        request_path = urlsplit(address.url).path
        parts, size = [], 0
        for (origin, path, key), (value, secure, expiry) in self.values.items():
            if (origin != address.origin or not (request_path == path or request_path.startswith(path.rstrip("/") + "/"))
                    or (secure and address.scheme != "https") or expiry <= time.time()):
                continue
            # Reserve 16 KiB for the URL and fixed headers under the 64 KiB
            # outgoing-header ceiling. Excess cookies are simply not sent.
            cost = len(value.encode("utf-8")) + 2
            if size + cost <= 48 * 1024:
                parts.append(value)
                size += cost
        return "; ".join(parts)


def fetch_document(url: str, locale: str, exchange, *, remaining_seconds: float = 30) -> dict:
    deadline = time.monotonic() + max(0, min(remaining_seconds, 30))
    # The independent URL/DNS gate must not pay HTTP/parser startup cost or
    # depend on those packages loading. Imports stay inside the bounded fetch.
    from .extract import extract_page
    from .transport import TransferBudget, curl_hop, httpx_hop, native_available, request_headers

    def resolve(value):
        # A genuine DNS failure/full DNS allowance is terminal. Exhausting the
        # attempt's smaller remaining budget is admission/timeout, not a refusal;
        # any next candidate must gate the original URL again. The resolver
        # thread is confined to this disposable process and dies with it.
        import queue
        import threading

        exchange({"event": "phase", "phase": "policy"})
        if parse_url(value).literal:
            result = resolve_target(value)
        else:
            replies = queue.Queue(maxsize=1)
            def lookup():
                try:
                    replies.put((resolve_target(value), None))
                except Exception as error:
                    replies.put((None, error))
            threading.Thread(target=lookup, daemon=True).start()
            window = max(0, min(5, deadline - time.monotonic()))
            try:
                result, error = replies.get(timeout=window)
            except queue.Empty:
                if window < 5:
                    raise FetchFailure("fetch_timeout" if sent else "service_busy", cooldown_suppressed=True) from None
                raise FetchFailure("fetch_dns_unverified", terminal=True) from None
            if error:
                raise error
        exchange({"event": "phase", "phase": "fetch"})
        return result

    budget = TransferBudget(on_response=lambda status: exchange({"event": "response", "http_status": status}))
    cookies, seen = OriginCookies(), set()
    native, reason = native_available()
    exchange({"event": "transport", "transport_degraded": not native, "transport_degraded_reason": reason})
    hop = curl_hop if native else httpx_hop
    original = parse_url(url).url
    current, previous = original, None
    used_follow = set()
    retained = None
    sent = False

    def on_sent():
        nonlocal sent
        if not sent:
            exchange({"event": "sent"})
            sent = True

    for index in range(6):
        # Release/acquire is parent-owned and counted in the attempt deadline.
        # Re-resolve AFTER the lock wait, immediately before each numeric connect.
        address = parse_url(current)
        resolve(current)  # Policy precedes even a host-cooldown skip.
        exchange({"event": "host", "host": address.host})
        target = resolve(current)
        cookie = cookies.header(address)
        # Only effective cookies for this URL may justify a revisit. Unrelated
        # origins/paths and refreshed expiry timestamps cannot disguise a loop.
        visit = (target.address.url, hashlib.sha256(cookie.encode("utf-8")).digest())
        if visit in seen:
            exchange({"event": "phase", "phase": "loop"})
            raise FetchFailure("fetch_target_unreadable")
        seen.add(visit)
        headers = request_headers(degraded=not native, locale=locale, previous_origin=previous, target=target)
        if cookie:
            headers["Cookie"] = cookie
        stage = "fetch"
        allowance = max(0.01, deadline - time.monotonic())
        hop_sent = False

        def mark_hop_sent():
            nonlocal hop_sent
            if not hop_sent:
                on_sent()
                exchange({"event": "hop"})
                hop_sent = True

        try:
            response = hop(target, headers, budget, allowance, mark_hop_sent)
            cookies.receive(address, response.headers)
            # Explicit WAF headers are authoritative on any status (including
            # AWS's 202/405). Do not broaden status-only detection or scan prose.
            if (response.header("cf-mitigated").strip().lower() == "challenge"
                    or response.header("x-amzn-waf-action").strip().lower() in {"challenge", "captcha"}):
                raise FetchFailure("fetch_challenge")
            if response.status == 401:
                raise FetchFailure("fetch_refused", terminal=True)
            if response.status == 429:
                raise FetchFailure("service_rate_limited", retry_after=response.header("retry-after") or None)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.header("location")
                if not location:
                    raise FetchFailure("fetch_target_unreadable")
                previous, current = address.origin, urljoin(address.url, location)
                # Always gate the selected redirect, even at the hop bound.
                resolve(current)
                continue
            if response.status in {403, 404, 410, 408}:
                raise FetchFailure("fetch_target_unreadable")
            if response.status != 200:
                raise FetchFailure("fetch_unavailable" if response.status >= 500 else "fetch_target_unreadable",
                                   retry_after=response.header("retry-after") or None)
            stage = "extract"
            exchange({"event": "phase", "phase": "extract"})
            page = extract_page(response.body, response.header("content-type"), address.url)
            if page.follow_url and page.follow_kind not in used_follow:
                # A forbidden selected alternate/refresh is terminal, not a reason
                # to return retained HTML or disclose the original to a provider.
                resolve(page.follow_url)
                if index < 5:
                    used_follow.add(page.follow_kind)
                    if page.follow_kind == "alternate" and page.content:
                        retained = (page, address.url, len(response.body))
                        exchange({"event": "fallback", "result": {
                            **page.payload(), "final_url": address.url, "source_size_bytes": len(response.body),
                            "transport_degraded": not native, "transport_degraded_reason": reason,
                        }})
                    previous, current = address.origin, page.follow_url
                    continue
            if not page.content.strip():
                raise FetchFailure("fetch_target_unreadable")
            if retained is not None:
                original_page = retained[0]
                page.title = page.title or original_page.title
                page.canonical_url = page.canonical_url or original_page.canonical_url
            result = {**page.payload(), "final_url": address.url, "source_size_bytes": len(response.body)}
            break
        except FetchFailure as error:
            if error.code == "fetch_timeout" and (hop_sent or allowance < 5):
                error.cooldown_suppressed = True
            if error.terminal or retained is None:
                raise
            page, final_url, size = retained
            result = {**page.payload(), "final_url": final_url, "source_size_bytes": size,
                      "cooldown_code": error.code, "retry_after": error.retry_after,
                      "cooldown_suppressed": error.cooldown_suppressed}
            break
        except Exception as error:
            # No target error retries on the other transport. Native-load failure
            # was the only fallback, chosen before the first socket existed.
            code = "fetch_target_unreadable" if stage == "extract" else "fetch_timeout" if "timeout" in type(error).__name__.lower() else "fetch_unavailable"
            # Connect/TLS has an independent 5 s bound. After send, the only
            # timeout is the remaining attempt budget; a shortened connect
            # allowance likewise cannot prove this new host is unhealthy.
            suppressed = code == "fetch_timeout" and (hop_sent or allowance < 5)
            if retained is not None:
                page, final_url, size = retained
                result = {**page.payload(), "final_url": final_url, "source_size_bytes": size,
                          "cooldown_code": code, "cooldown_suppressed": suppressed}
                break
            raise FetchFailure(code, cooldown_suppressed=suppressed) from None
    else:
        exchange({"event": "phase", "phase": "loop"})
        raise FetchFailure("fetch_target_unreadable")
    for field in ("follow_url", "follow_kind"):
        result.pop(field, None)
    return {**result, "transport_degraded": not native, "transport_degraded_reason": reason}


def main() -> None:
    def emit(payload):
        sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
        sys.stdout.flush()

    def exchange(payload):
        emit(payload)
        if sys.stdin.readline().strip() != "ok":
            # Never return control to a native callback after denied admission.
            os._exit(1)

    try:
        resource_limits()
        request = json.loads(sys.stdin.readline(65536))
        if request["operation"] == "gate":
            target = resolve_target(request["url"])
            result = {"url": target.address.url}
        else:
            result = fetch_document(request["url"], request.get("locale", "en"), exchange,
                                    remaining_seconds=request.get("remaining_seconds", 30))
        emit({"event": "result", "result": result})
    except FetchFailure as error:
        emit({"event": "failure", "error_code": error.code, "terminal": error.terminal,
              "retry_after": error.retry_after, "cooldown_suppressed": error.cooldown_suppressed})
    except Exception:
        emit({"event": "failure", "error_code": "fetch_unavailable", "terminal": False})


if __name__ == "__main__":
    main()
