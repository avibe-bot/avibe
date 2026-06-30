import base64
import asyncio
import gzip
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

import psutil
from aiohttp import ClientSession, WSMsgType
from fastapi import Request as FastAPIRequest, WebSocket, WebSocketDisconnect
from fastapi.responses import Response as FastAPIResponse

from vibe.ui_compat import CompatApp, Response, TEST_REMOTE_ADDR_HEADER, g, jsonify, redirect, request, send_file

from config import paths
from config.v2_config import CONFIG_LOCK, V2Config
from core.show_pages import (
    SHOW_CLI_EVENT_TOKEN_HEADER,
    SHOW_EVENT_WRITE_TOKEN_COOKIE,
    SHOW_EVENT_WRITE_TOKEN_HEADER,
    show_cli_event_token,
    show_event_write_token,
)
from core.show_session_events import show_event_payload_session_mismatch
from core.terminal_service import TERMINAL_SUPPORTED, TerminalService, TerminalServiceError, sanitize_session_id
from modules.agents.catalog import AGENT_BACKENDS, supports_runtime_refresh
from vibe.i18n import get_supported_languages, t
from vibe.runtime import get_ui_dist_path, get_working_dir
from vibe.sentry_integration import init_sentry

logger = logging.getLogger(__name__)

# Python's mimetypes map omits .webmanifest; register it so the PWA manifest is
# served as a type browsers accept (an octet-stream manifest is rejected).
mimetypes.add_type("application/manifest+json", ".webmanifest")

app = CompatApp(title="avibe UI", docs_url=None, redoc_url=None, openapi_url=None)

# Global server instance for graceful shutdown on reload
_server = None
SLOW_API_REQUEST_MS = float(os.environ.get("VIBE_UI_SLOW_API_MS", "2000"))
SHOW_RUNTIME_SLOW_REQUEST_MS = float(os.environ.get("VIBE_SHOW_RUNTIME_SLOW_REQUEST_MS", "1000"))
_SHOW_RUNTIME_REQUEST_HEADER_ALLOWLIST = {
    "accept",
    "accept-language",
    "cache-control",
    "content-type",
    "if-modified-since",
    "if-none-match",
    "last-event-id",
    "pragma",
    "range",
    "user-agent",
    SHOW_EVENT_WRITE_TOKEN_HEADER.lower(),
}
_SHOW_RUNTIME_RESPONSE_HEADER_ALLOWLIST = {
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-language",
    "content-range",
    "content-type",
    "etag",
    "expires",
    "last-modified",
    "location",
    "sourcemap",
    "vary",
    "x-sourcemap",
}
_SHOW_RUNTIME_MODULE_SCRIPT_RE = re.compile(
    r"<script\b(?=[^>]*\btype\s*=\s*['\"]module['\"])[^>]*>",
    re.IGNORECASE,
)
_SHOW_RUNTIME_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Shared, content-hashed vendor bundle. The runtime serves this at a
# session-independent path (`/_show-runtime/vendor/<hash>/<file>`) and injects the
# matching `<script type="importmap">` + vendor CSS `<link>` into every Show Page it
# serves, so the avibe proxy only has to forward this prefix verbatim (never under a
# per-session base) and mark 2xx responses immutable.
_SHOW_RUNTIME_VENDOR_PREFIX = "/_show-runtime/vendor"
# HMR-neutralizing shims for the public `/p/` surface. Anonymous viewers must not open
# a live Vite HMR websocket or run React Fast Refresh, so the runtime's `@vite/client`
# and `@react-refresh` references are rewritten to these inert modules. Independent of
# the vendor bundle; the version only busts the shim cache when the shim source changes.
_SHOW_RUNTIME_PUBLIC_SHIM_VERSION = "v1"
_SHOW_RUNTIME_PUBLIC_CLIENT_SHIM_PATH = f"/_show-runtime/client-shim-{_SHOW_RUNTIME_PUBLIC_SHIM_VERSION}.js"
_SHOW_RUNTIME_PUBLIC_REACT_REFRESH_SHIM_PATH = (
    f"/_show-runtime/react-refresh-shim-{_SHOW_RUNTIME_PUBLIC_SHIM_VERSION}.js"
)
_SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES = 1024
TERMINAL_ENABLED_ENV = "VIBE_UI_ENABLE_TERMINAL"
TERMINAL_IDLE_TIMEOUT_ENV = "VIBE_UI_TERMINAL_IDLE_TIMEOUT_SECONDS"
TERMINAL_MAX_SESSIONS_ENV = "VIBE_UI_TERMINAL_MAX_SESSIONS"
_TRUE_BOOL_STRINGS = {"1", "true", "yes", "on"}

STRUCTURED_LOG_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+([\w.]+)\s+-\s+(\w+)\s+-\s+(.*)$")
LEVEL_HINT_PATTERN = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
TRACEBACK_EXCEPTION_PATTERN = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|Failure|Fault|Group)(?:[:(]|$)"
)
CSRF_COOKIE_NAME = "vibe_csrf_token"
CSRF_HEADER_NAME = "X-Vibe-CSRF-Token"
REMOTE_OAUTH_COOKIE_NAME = "__Host-vibe_remote_oauth"
REMOTE_OAUTH_RETRY_PARAM = "__vibe_oauth_retry"
# Lifetime of the short-lived OAuth handshake (signed state + PKCE cookie). The
# cookie MUST carry an explicit Max-Age: iOS standalone PWAs drop session-scoped
# cookies (no Max-Age) across the cross-origin authorize excursion / app
# backgrounding, which silently breaks the callback. A persistent cookie with a
# short TTL survives. The signed payload's own `exp` enforces the real validity.
REMOTE_OAUTH_HANDSHAKE_TTL_SECONDS = 300
# Stable, per-browser binding id. Unlike the per-flow handshake state (which the
# iOS standalone PWA desyncs), this cookie is set once and reused, so it stays
# consistent across the cross-origin authorize excursion. The callback's
# server-side store-fallback is bound to hmac(secret, device_id), which an
# attacker cannot supply for a victim's browser — this closes the login-CSRF that
# a bare code+state callback URL would otherwise allow.
REMOTE_OAUTH_DEVICE_COOKIE_NAME = "__Host-vibe_oauth_device"
REMOTE_OAUTH_DEVICE_TTL_SECONDS = 180 * 24 * 60 * 60
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOG_SOURCES = (
    ("service", "vibe_remote.log", lambda: paths.get_logs_dir() / "vibe_remote.log"),
    ("service_stdout", "service_stdout.log", lambda: paths.get_runtime_dir() / "service_stdout.log"),
    ("service_stderr", "service_stderr.log", lambda: paths.get_runtime_dir() / "service_stderr.log"),
    ("ui_stdout", "ui_stdout.log", lambda: paths.get_runtime_dir() / "ui_stdout.log"),
    ("ui_stderr", "ui_stderr.log", lambda: paths.get_runtime_dir() / "ui_stderr.log"),
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid integer env var %s", name)
        return default


def _parse_explicit_bool(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_BOOL_STRINGS
    return False


def _recover_stale_session_status(session_id: str) -> bool:
    """Clear a persisted ``running`` dot after the controller proves idle.

    The UI process owns the browser-facing API, while the controller owns the
    in-memory turn registry. If the controller reports no in-flight turn (or the
    user Stop reaches a stale turn), a ``running`` row in SQLite is only a stale
    projection and must be repaired so reloads/sidebar state stop showing a
    phantom run.
    """

    from core.services import sessions as workbench_sessions_service
    from storage.db import create_sqlite_engine
    from vibe.sse_broker import broker

    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            try:
                session = workbench_sessions_service.get_session(conn, session_id)
            except LookupError:
                return False
            if not session or session.get("agent_status") != "running":
                return False
            changed = workbench_sessions_service.set_agent_status(conn, session_id, "idle")
    finally:
        engine.dispose()
    if changed:
        broker.publish("session.status", {"session_id": session_id, "agent_status": "idle"})
    return changed


def _is_continuation_line(line: str, previous_message: str | None = None) -> bool:
    stripped = line.lstrip()
    return (
        line[:1].isspace()
        or stripped.startswith("Traceback ")
        or stripped.startswith("During handling of the above exception")
        or stripped.startswith("File ")
        or stripped.startswith("task:")
        or stripped.startswith("^")
        or (
            previous_message is not None
            and "Traceback " in previous_message
            and bool(TRACEBACK_EXCEPTION_PATTERN.match(stripped))
        )
    )


def _fallback_log_entry(line: str, source_key: str) -> dict[str, str]:
    level_match = LEVEL_HINT_PATTERN.search(line)
    level = level_match.group(1) if level_match else "INFO"
    if level == "CRITICAL":
        level = "ERROR"
    return {
        "timestamp": "",
        "logger": source_key,
        "level": level,
        "message": line,
        "source": source_key,
    }


def _timestamp_to_sort_ns(timestamp: str) -> int | None:
    try:
        return int(datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f").timestamp() * 1_000_000_000)
    except ValueError:
        return None


def _serialize_log_entries(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "timestamp": str(entry.get("timestamp", "")),
            "logger": str(entry.get("logger", "")),
            "level": str(entry.get("level", "INFO")),
            "message": str(entry.get("message", "")),
            "source": str(entry.get("source", "")),
        }
        for entry in entries
    ]


def _runtime_pid_file_points_to_live_process(pid_path: Path) -> bool:
    from vibe import runtime

    try:
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw_pid)
    except (OSError, ValueError):
        return False
    return runtime.pid_alive(pid)


def _stop_runtime_process_or_error(pid_path: Path, label: str) -> tuple[bool, str | None]:
    from vibe import runtime

    was_running = _runtime_pid_file_points_to_live_process(pid_path)
    if pid_path == paths.get_runtime_pid_path():
        stopped = runtime.stop_service()
    else:
        stopped = runtime.stop_process(pid_path)
    if was_running and stopped is False:
        return False, f"{label} did not stop"
    return True, None


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _request_origin(value: str | None) -> str | None:
    if not value:
        return None

    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _current_origin() -> str:
    parsed = urlparse(request.host_url)
    scheme = parsed.scheme
    netloc = parsed.netloc

    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()

    if forwarded_proto:
        scheme = forwarded_proto
    if forwarded_host:
        netloc = forwarded_host

    return f"{scheme}://{netloc}"


def _is_mutation_guard_exempt() -> bool:
    if request.path in {"/auth/callback"}:
        return True
    if _is_cli_show_event_request() or _is_cli_session_activity_request():
        return True
    return (
        request.path == "/e2e/simulate-interaction"
        and os.environ.get("E2E_TEST_MODE", "").lower() in ("true", "1", "yes")
    )


def _cli_local_event_token_ok() -> bool:
    """The local CLI proves it's co-located with this service by signing the shared
    local secret. Same trust model as the show-event channel."""
    token = request.headers.get(SHOW_CLI_EVENT_TOKEN_HEADER)
    return (
        request.method == "POST"
        and request.headers.get("X-Vibe-Show-Client") == "cli"
        and bool(token)
        and hmac.compare_digest(token, show_cli_event_token())
    )


def _is_cli_show_event_request() -> bool:
    return (
        _cli_local_event_token_ok()
        and re.fullmatch(r"/api/show/sessions/[^/]+/(events|prewarm)", request.path or "") is not None
    )


def _is_cli_session_activity_request() -> bool:
    return (
        _cli_local_event_token_ok()
        and re.fullmatch(r"/api/sessions/[^/]+/cli-activity", request.path or "") is not None
    )


def _is_show_api_mutation() -> bool:
    if not (request.path.startswith("/show/") or request.path.startswith("/p/")):
        return False
    return "/api/" in request.path or "/__show/" in request.path


def _ensure_csrf_cookie(response: Response) -> Response:
    if _is_current_show_runtime_immutable_asset_request():
        return response
    if response.headers.getlist("Set-Cookie"):
        for cookie_header in response.headers.getlist("Set-Cookie"):
            if cookie_header.startswith(f"{CSRF_COOKIE_NAME}="):
                return response

    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            _new_csrf_token(),
            httponly=False,
            secure=request.is_secure,
            samesite="Strict",
            path="/",
        )
    return response


def _load_remote_access_config() -> V2Config | None:
    try:
        from core.services import settings as settings_service

        return settings_service.load_config()
    except Exception:
        logger.warning("Failed to load remote access config", exc_info=True)
        return None


def _has_cloudflare_forwarded_metadata() -> bool:
    return any(
        request.headers.get(header)
        for header in (
            "CF-Connecting-IP",
            "CF-Ray",
            "CF-Visitor",
            "CF-IPCountry",
        )
    )


def _has_forwarded_metadata() -> bool:
    """Detect any sign that the request traversed a reverse proxy.

    When any forwarded header is set, request.remote_addr no longer reliably
    identifies the actual client (a same-host proxy makes external attackers
    look like loopback / private peers), so authorization paths that lean on a
    private/loopback peer must refuse the request unless we have an explicit
    trusted-proxy chain.
    """
    forwarded_headers = (
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Port",
        "X-Real-IP",
        "X-Original-Forwarded-For",
        "True-Client-IP",
    )
    if any(request.headers.get(header) for header in forwarded_headers):
        return True
    return _has_cloudflare_forwarded_metadata()


def _is_loopback_origin_proxy_request() -> bool:
    if not _is_loopback_peer() or not _is_loopback_host(request.host):
        return False
    if request.headers.get("Forwarded") or request.headers.get("X-Forwarded-For"):
        return False
    client_ip_headers = (
        "X-Real-IP",
        "X-Original-Forwarded-For",
        "True-Client-IP",
    )
    if any(request.headers.get(header) for header in client_ip_headers):
        return False
    if _has_cloudflare_forwarded_metadata():
        return False
    return bool(request.headers.get("X-Forwarded-Host") or request.headers.get("X-Forwarded-Proto"))


def _is_loopback_peer() -> bool:
    remote_addr = (request.remote_addr or "").strip()
    if remote_addr == "localhost":
        return True
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _is_loopback_host(value: str | None) -> bool:
    host = _normalized_host(value)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# RFC 6598 shared address space (CGNAT). Python's ipaddress module classifies
# this range as neither private nor global, but in practice overlay networks
# such as Tailscale assign 100.x.y.z addresses that should be trusted as local
# setup-host peers when the request's Host header otherwise matches.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6_ADDRESS_SPACE = ipaddress.ip_network("fd7a:115c:a1e0::/48")

# Networks that are scoped by the overlay/link itself rather than by the
# kernel's interface routing, so peers anywhere in the block are trusted
# in lieu of a tighter same-subnet check:
#   * 100.64.0.0/10 — Tailscale CGNAT. Tailscale assigns each peer a /32 in
#     this range and routes peers via its overlay; legitimate peers can be
#     anywhere in the /10 even though they share the same logical network.
#   * fd7a:115c:a1e0::/48 — Tailscale IPv6 ULA. Like the IPv4 CGNAT
#     range, Tailscale can assign interface addresses as host routes while
#     legitimate peers live elsewhere in the overlay prefix.
#   * 169.254.0.0/16 / fe80::/10 — link-local. Confined to the same L2
#     segment by the kernel.
_OVERLAY_TRUST_NETWORKS_V4 = (
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)
_OVERLAY_TRUST_NETWORKS_V6 = (
    _TAILSCALE_IPV6_ADDRESS_SPACE,
    ipaddress.IPv6Network("fe80::/10"),
)
_WILDCARD_TRUST_LAN_INTERFACE_PREFIXES = (
    "en",
    "eth",
    "ethernet",
    "local area connection",
    "wi-fi",
    "wifi",
    "wl",
    "wwan",
)
_WILDCARD_TRUST_OVERLAY_INTERFACE_PREFIXES = (
    "tailscale",
)
_TAILSCALE_UTUN_INTERFACE_PREFIXES = ("utun",)
_TAILSCALE_IP_CACHE_TTL_SECONDS = 30.0
_TAILSCALE_IP_CACHE: tuple[float, frozenset[ipaddress._BaseAddress]] | None = None
_TAILSCALE_PEER_CACHE_TTL_SECONDS = 30.0
_TAILSCALE_PEER_CACHE: dict[ipaddress._BaseAddress, tuple[float, bool]] = {}
_CONTAINER_CGROUP_MARKERS = ("docker", "kubepods", "containerd", "libpod", "podman")


def _is_private_address(address: ipaddress._BaseAddress) -> bool:
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in _SHARED_ADDRESS_SPACE


def _is_containerized_runtime() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    for cgroup_path in (Path("/proc/self/cgroup"), Path("/proc/1/cgroup")):
        try:
            cgroup = cgroup_path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if any(marker in cgroup for marker in _CONTAINER_CGROUP_MARKERS):
            return True
    return False


def _is_private_peer() -> bool:
    address = _request_peer_address()
    return address is not None and _is_private_address(address)


def _request_peer_address() -> ipaddress._BaseAddress | None:
    remote_addr = (request.remote_addr or "").strip()
    if not remote_addr or remote_addr == "localhost":
        return None
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _local_interface_network(
    setup_address: ipaddress._BaseAddress,
    interface_filter: Callable[[str, ipaddress._BaseAddress], bool] | None = None,
) -> ipaddress._BaseNetwork | None:
    """Return the network ``setup_host`` is configured on locally.

    Reads the interface's actual netmask via ``psutil.net_if_addrs`` so
    the trust scope mirrors the kernel's pre-wildcard interface filtering
    exactly — a /16 LAN, a /20 corporate network, and a non-/64 IPv6
    network all get their real prefix instead of a fixed estimate.

    Returns None when ``setup_host`` is not configured on any local
    interface or psutil cannot enumerate them; the caller denies trust
    in that case so we never widen the application-layer scope beyond
    what the kernel would have permitted.
    """
    try:
        interfaces = psutil.net_if_addrs()
    except Exception:
        return None
    target_family = socket.AF_INET if setup_address.version == 4 else socket.AF_INET6
    for interface_name, addrs in interfaces.items():
        for snic in addrs:
            if snic.family != target_family:
                continue
            address_str = (snic.address or "").split("%", 1)[0]
            try:
                addr = ipaddress.ip_address(address_str)
            except ValueError:
                continue
            if addr != setup_address:
                continue
            if interface_filter is not None and not interface_filter(interface_name, addr):
                continue
            netmask = snic.netmask
            if not netmask:
                continue
            prefix = _netmask_to_prefix(netmask, addr.version)
            if prefix is None:
                continue
            try:
                return ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
            except ValueError:
                continue
    return None


def _netmask_to_prefix(netmask: str, version: int) -> int | None:
    """Convert ``psutil``'s netmask string to a prefix length.

    psutil returns IPv4 netmasks as dotted strings (``255.255.255.0``)
    and IPv6 netmasks as hex strings (``ffff:ffff:ffff:ff00::``).
    ``ipaddress.ip_network`` only accepts the dotted form for IPv4 and
    requires an integer prefix for IPv6, so we normalize to a prefix
    length here. Returns None for malformed or non-contiguous masks.
    """
    try:
        if version == 4:
            mask_int = int(ipaddress.IPv4Address(netmask))
            width = 32
        else:
            mask_int = int(ipaddress.IPv6Address(netmask))
            width = 128
    except (ipaddress.AddressValueError, ValueError):
        return None
    if mask_int == 0:
        return 0
    inverted = (~mask_int) & ((1 << width) - 1)
    if inverted & (inverted + 1):
        # Non-contiguous mask — refuse rather than guess.
        return None
    prefix = width - inverted.bit_length()
    return prefix


def _setup_host_trust_network(setup_address: ipaddress._BaseAddress) -> ipaddress._BaseNetwork | None:
    """Return the network setup-host trust should extend to, or None to deny.

    Overlay networks (Tailscale CGNAT, link-local) trust the entire block
    because the overlay routing or kernel link-local scoping handles peer
    isolation; legitimate peers can be anywhere in the block. RFC1918 and
    ULA setup hosts derive the network from the actual interface netmask
    via :func:`_local_interface_network` so the application-layer scope
    matches the kernel's pre-wildcard interface filtering. Returning None
    means the scope cannot be determined and the caller must deny trust.
    """
    if setup_address.version == 4:
        for overlay in _OVERLAY_TRUST_NETWORKS_V4:
            if setup_address in overlay:
                return overlay
    elif setup_address.version == 6:
        for overlay in _OVERLAY_TRUST_NETWORKS_V6:
            if setup_address in overlay:
                return overlay
    return _local_interface_network(setup_address)


def _peer_shares_setup_host_network(setup_address: ipaddress._BaseAddress) -> bool:
    """Require the peer to share setup_host's interface-level subnet.

    Compensates for the wildcard bind in the tunnel-on path. Without this,
    a 192.168/16 LAN peer could spoof ``Host=<tailscale_setup_host>`` on
    a different interface and inherit setup-host trust. Subnet size comes
    from :func:`_setup_host_trust_network`, which keeps overlay networks
    (Tailscale, link-local) broad and otherwise mirrors the actual
    interface netmask via :func:`_local_interface_network`.
    """
    remote_addr = (request.remote_addr or "").strip()
    if not remote_addr or remote_addr == "localhost":
        return False
    try:
        peer = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    if peer.version != setup_address.version:
        mapped = getattr(peer, "ipv4_mapped", None)
        if mapped is None or mapped.version != setup_address.version:
            return False
        peer = mapped
    network = _setup_host_trust_network(setup_address)
    if network is None:
        return False
    return peer in network


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def _terminal_enabled() -> bool:
    # The Web Terminal is ON by default; set VIBE_UI_ENABLE_TERMINAL to a falsy
    # value (0/false/no/off) to turn it off. The WebSocket auth gate still
    # authorizes every connection regardless of this flag.
    raw = os.environ.get(TERMINAL_ENABLED_ENV)
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _has_loopback_only_docker_port_binding() -> bool:
    bind_host = os.environ.get("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST")
    if not bind_host:
        return False
    return _is_loopback_host(bind_host)


def _trusted_docker_loopback_peer_addresses() -> set[ipaddress._BaseAddress]:
    addresses: set[ipaddress._BaseAddress] = set()
    for raw_address in os.environ.get("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "").split(","):
        raw_address = raw_address.strip()
        if not raw_address:
            continue
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        addresses.add(getattr(address, "ipv4_mapped", None) or address)
    addresses.update(_docker_default_gateway_addresses())
    return addresses


def _docker_default_gateway_addresses() -> set[ipaddress._BaseAddress]:
    addresses: set[ipaddress._BaseAddress] = set()
    for line in _docker_route_table_lines()[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            gateway_int = int(fields[2], 16)
            gateway = ipaddress.ip_address(gateway_int.to_bytes(4, byteorder="little"))
        except (ValueError, OverflowError):
            continue
        if gateway.is_unspecified:
            continue
        addresses.add(gateway)
    return addresses


def _docker_route_table_lines() -> list[str]:
    try:
        return Path("/proc/net/route").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _is_trusted_docker_peer() -> bool:
    if not _env_flag_enabled("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS"):
        return False
    if not _has_loopback_only_docker_port_binding():
        return False

    remote_addr = (request.remote_addr or "").strip()
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address

    return address in _trusted_docker_loopback_peer_addresses()


def _is_trusted_docker_loopback_request() -> bool:
    if _has_forwarded_metadata():
        return False
    if not _is_loopback_host(request.host):
        return False
    return _is_trusted_docker_peer()


def _is_trusted_docker_loopback_probe() -> bool:
    if request.method not in {"GET", "HEAD"}:
        return False
    if request.path not in {"/health", "/status"}:
        return False
    return _is_trusted_docker_loopback_request()


def _has_docker_loopback_probe_shape() -> bool:
    return (
        request.method in {"GET", "HEAD"}
        and request.path in {"/health", "/status"}
        and not _has_forwarded_metadata()
        and _is_loopback_host(request.host)
        and not _is_loopback_peer()
    )


def _is_wildcard_setup_host(setup_host: str) -> bool:
    return setup_host in {"0.0.0.0", "::", "*"}


def _is_tailscale_overlay_address(address: ipaddress._BaseAddress) -> bool:
    return (
        isinstance(address, ipaddress.IPv4Address)
        and address in _SHARED_ADDRESS_SPACE
        or isinstance(address, ipaddress.IPv6Address)
        and address in _TAILSCALE_IPV6_ADDRESS_SPACE
    )


def _tailscale_cli_candidates() -> list[str]:
    candidates: list[str] = []
    path = shutil.which("tailscale")
    if path:
        candidates.append(path)
    macos_app_cli = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    if macos_app_cli.exists():
        candidates.append(str(macos_app_cli))
    return list(dict.fromkeys(candidates))


def _tailscale_local_addresses() -> frozenset[ipaddress._BaseAddress]:
    global _TAILSCALE_IP_CACHE

    now = time.monotonic()
    if _TAILSCALE_IP_CACHE is not None:
        cached_at, cached_addresses = _TAILSCALE_IP_CACHE
        if now - cached_at < _TAILSCALE_IP_CACHE_TTL_SECONDS:
            return cached_addresses

    addresses: set[ipaddress._BaseAddress] = set()
    env = {**os.environ, "TAILSCALE_BE_CLI": "1"}
    for candidate in _tailscale_cli_candidates():
        try:
            result = subprocess.run(
                [candidate, "ip"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
                env=env,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            try:
                address = ipaddress.ip_address(line.strip())
            except ValueError:
                continue
            if _is_tailscale_overlay_address(address):
                addresses.add(address)
        if addresses:
            break

    cached = frozenset(addresses)
    _TAILSCALE_IP_CACHE = (now, cached)
    return cached


def _tailscale_whois(peer_address: ipaddress._BaseAddress) -> dict[str, Any] | None:
    env = {**os.environ, "TAILSCALE_BE_CLI": "1"}
    for candidate in _tailscale_cli_candidates():
        try:
            result = subprocess.run(
                [candidate, "whois", "--json", str(peer_address)],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
                env=env,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout)
        except Exception:
            continue
        return payload if isinstance(payload, dict) else None
    return None


def _json_list(payload: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _is_tailscale_host_route(network: ipaddress._BaseNetwork) -> bool:
    if network.prefixlen != network.max_prefixlen:
        return False
    return _is_tailscale_overlay_address(network.network_address)


def _is_trusted_tailscale_peer(peer_address: ipaddress._BaseAddress) -> bool:
    global _TAILSCALE_PEER_CACHE

    if not _is_tailscale_overlay_address(peer_address):
        return False

    now = time.monotonic()
    cached = _TAILSCALE_PEER_CACHE.get(peer_address)
    if cached is not None:
        cached_at, trusted = cached
        if now - cached_at < _TAILSCALE_PEER_CACHE_TTL_SECONDS:
            return trusted

    payload = _tailscale_whois(peer_address)
    trusted = False
    if payload is not None:
        machine = payload.get("Machine") or payload.get("machine") or {}
        if isinstance(machine, dict):
            addresses = set()
            for raw_address in _json_list(machine, "Addresses", "addresses"):
                try:
                    addresses.add(ipaddress.ip_address(str(raw_address)))
                except ValueError:
                    continue
            allowed_networks = []
            for raw_network in _json_list(machine, "AllowedIPs", "allowedIPs", "allowedIps"):
                try:
                    allowed_networks.append(ipaddress.ip_network(str(raw_network), strict=False))
                except ValueError:
                    continue
            trusted = bool(addresses and peer_address in addresses and allowed_networks)
            if trusted:
                trusted = all(_is_tailscale_host_route(network) for network in allowed_networks)

    _TAILSCALE_PEER_CACHE[peer_address] = (now, trusted)
    return trusted


def _allows_wildcard_setup_host_trust(interface_name: str, address: ipaddress._BaseAddress) -> bool:
    normalized_name = interface_name.lower()
    if _is_tailscale_overlay_address(address):
        if normalized_name.startswith(_WILDCARD_TRUST_OVERLAY_INTERFACE_PREFIXES):
            return True
        if normalized_name.startswith(_TAILSCALE_UTUN_INTERFACE_PREFIXES):
            return address in _tailscale_local_addresses()
        return False
    if _is_containerized_runtime():
        return False
    return normalized_name.startswith(_WILDCARD_TRUST_LAN_INTERFACE_PREFIXES)


def _is_wildcard_setup_host_request(config: V2Config | None) -> bool:
    """Treat wildcard binds as local only through an actual private interface.

    ``0.0.0.0``/``::`` is a listen address, not a trusted browser host. For
    compatibility with LAN direct access, accept requests to a concrete local
    private IP on a small allowlist of LAN/overlay interfaces while keeping
    arbitrary private Host spoofing, container bridge networks, and public-IP
    exposure behind the normal remote-access checks.
    """
    if config is None:
        return False
    setup_host = _normalized_host(getattr(config.ui, "setup_host", ""))
    if not _is_wildcard_setup_host(setup_host):
        return False
    if _has_forwarded_metadata():
        return False

    try:
        host_address = ipaddress.ip_address(_normalized_host(request.host))
    except ValueError:
        return False
    if host_address.is_unspecified:
        return False
    if not _is_private_address(host_address):
        return False
    if _local_interface_network(host_address, interface_filter=_allows_wildcard_setup_host_trust) is None:
        return False
    if not _is_private_peer():
        return False
    if _is_tailscale_overlay_address(host_address):
        peer_address = _request_peer_address()
        return peer_address is not None and _is_trusted_tailscale_peer(peer_address)
    return _peer_shares_setup_host_network(host_address)


def _is_setup_host_request(config: V2Config | None) -> bool:
    if config is None:
        return False
    setup_host = _normalized_host(getattr(config.ui, "setup_host", ""))
    if not setup_host:
        return False
    if _is_wildcard_setup_host(setup_host):
        return _is_wildcard_setup_host_request(config)
    if _is_loopback_host(setup_host):
        return False
    # Only trust setup-host requests when setup_host parses to a private/CGNAT
    # IP. Public hostnames or public IPs cannot be assumed safe: a reverse proxy
    # on the same machine would make request.remote_addr look like a private
    # peer even for external attackers, so the host-match + private-peer pair
    # is not sufficient on its own.
    try:
        setup_address = ipaddress.ip_address(setup_host)
    except ValueError:
        return False
    if not _is_private_address(setup_address):
        return False
    if _normalized_host(request.host) != setup_host:
        return False
    # Any forwarded header (including non-Cloudflare proxies like nginx /
    # Caddy / Traefik) means we cannot trust request.remote_addr to identify
    # the actual client, so refuse the setup-host trust path entirely.
    if _has_forwarded_metadata():
        return False
    if not _is_private_peer():
        return False
    # When the Avibe Cloud tunnel is on, the UI binds to a wildcard so the
    # local cloudflared origin can reach setup_host regardless of which
    # interface it lives on. Wildcard means the kernel no longer drops
    # cross-interface traffic, so we have to re-enforce "peer shares the
    # setup_host interface subnet" at the application layer to prevent a
    # peer on a different interface from spoofing Host=<setup_host>. When
    # the tunnel is off, the kernel binds to setup_host directly and that
    # interface filtering is already in force; adding the subnet gate
    # here would just block legitimate routed peers (e.g. a 10.50/16
    # client reaching setup_host=10.1.2.3 across a routed corporate net).
    if _is_tunnel_wildcard_bind(config):
        return _peer_shares_setup_host_network(setup_address)
    return True


def _is_tunnel_wildcard_bind(config: V2Config) -> bool:
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    return bool(cloud is not None and cloud.enabled)


def _is_local_request(config: V2Config | None = None) -> bool:
    if _has_forwarded_metadata():
        return False
    if _is_loopback_peer() and _is_loopback_host(request.host):
        return True
    if _is_trusted_docker_loopback_request():
        return True
    return _is_setup_host_request(config)


def _normalized_host(value: str | None) -> str:
    raw_host = (value or "").lower().strip()
    if raw_host.startswith("[") and "]" in raw_host:
        host = raw_host[1 : raw_host.index("]")]
    elif raw_host.count(":") > 1:
        host = raw_host
    else:
        host = raw_host.split(":", 1)[0]
    return host.rstrip(".")


def _is_remote_access_request(config: V2Config) -> bool:
    public_host = _remote_access_public_host(config)
    if not public_host:
        return False
    return _normalized_host(request.host) == public_host


def _remote_access_public_host(config: V2Config) -> str | None:
    public_url = (config.remote_access.vibe_cloud.public_url or "").strip()
    if not public_url:
        return ""
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    return _normalized_host(parsed.hostname)


def _remote_access_public_origin(config: V2Config) -> str | None:
    public_url = (config.remote_access.vibe_cloud.public_url or "").strip()
    if not public_url:
        return ""
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    return f"{parsed.scheme}://{parsed.netloc.lower().rstrip('.')}"


def _origin_identity(value: str) -> tuple[str, str, int | None] | None:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        return None
    return (parsed.scheme.lower(), _normalized_host(parsed.hostname), _origin_port(parsed.netloc, parsed.scheme))


def _remote_access_public_origin_matches(origin: str, config: V2Config) -> bool:
    trusted_origin = _remote_access_public_origin(config)
    if not trusted_origin:
        return False
    return _origin_identity(origin) == _origin_identity(trusted_origin)


def _remote_access_public_url_invalid(config: V2Config) -> bool:
    cloud = config.remote_access.vibe_cloud
    return bool(cloud.enabled and not _remote_access_public_origin(config))


def _remote_access_snapshot(config: V2Config) -> dict[str, Any]:
    return {
        "provider": config.remote_access.provider,
        "vibe_cloud": config.remote_access.vibe_cloud.__dict__.copy(),
    }


def _remote_access_settings_changed(previous: V2Config | None, current: V2Config, payload: dict) -> bool:
    if "remote_access" not in payload:
        return False
    if previous is None:
        return bool(_remote_access_snapshot(current)["vibe_cloud"].get("enabled"))
    return _remote_access_snapshot(previous) != _remote_access_snapshot(current)


def _should_rotate_remote_session_secret(previous: V2Config | None, current: V2Config, payload: dict) -> bool:
    if "remote_access" not in payload or previous is None:
        return False
    previous_cloud = previous.remote_access.vibe_cloud
    current_cloud = current.remote_access.vibe_cloud
    return bool(previous_cloud.enabled and not current_cloud.enabled and current_cloud.session_secret)


def _platform_runtime_signature(config: V2Config) -> dict[str, tuple[Any, ...]]:
    from config.platform_registry import get_platform_descriptor

    signatures: dict[str, tuple[Any, ...]] = {}
    for platform in config.platforms.enabled:
        descriptor = get_platform_descriptor(platform)
        platform_config = descriptor.get_config(config)
        signatures[platform] = (
            tuple(getattr(platform_config, field, None) for field in descriptor.runtime_reconcile_field_names())
            if platform_config is not None
            else ()
        )
    return signatures


def _platform_runtime_fields_changed(previous: V2Config | None, current: V2Config, payload: dict) -> bool:
    from config.platform_registry import im_platform_descriptors

    if previous is None:
        return False
    platform_config_keys = {descriptor.config_key for descriptor in im_platform_descriptors()}
    if "platforms" not in payload and "platform" not in payload and not any(key in payload for key in platform_config_keys):
        return False
    return (
        set(previous.platforms.enabled) != set(current.platforms.enabled)
        or previous.platforms.primary != current.platforms.primary
        or _platform_runtime_signature(previous) != _platform_runtime_signature(current)
    )


# Static PWA / icon assets must be reachable WITHOUT the remote-access auth
# cookie. iOS "Add to Home Screen" fetches the apple-touch-icon + manifest in a
# context that doesn't carry the session, so gating them makes the installed app
# fall back to a generated letter placeholder ("V") instead of the real icon.
# These are non-sensitive static files (app icon, manifest, brand logo/favicon).
_PWA_PUBLIC_ASSETS = frozenset(
    {
        "/manifest.webmanifest",
        "/apple-touch-icon.png",
        "/icon-192.png",
        "/icon-512.png",
        "/logo.png",
    }
)


def _remote_auth_exempt_path() -> bool:
    path = request.path
    return (
        path == "/health"
        or path == "/auth/callback"
        or path == "/auth/logout"
        or path == "/api/session"
        or path == "/api/cloud/token"
        or path == "/api/csrf-token"
        or path.startswith("/assets/")
        or path.startswith(f"{_SHOW_RUNTIME_VENDOR_PREFIX}/")
        or path
        in {
            _SHOW_RUNTIME_PUBLIC_CLIENT_SHIM_PATH,
            _SHOW_RUNTIME_PUBLIC_REACT_REFRESH_SHIM_PATH,
        }
        or path.startswith("/p/")
        or path == "/favicon.ico"
        or path in _PWA_PUBLIC_ASSETS
    )


def _remote_auth_exempt_before_host_validation() -> bool:
    return (
        request.path in {"/auth/callback", "/auth/logout", "/api/session", "/api/csrf-token"}
        or request.path.startswith("/assets/")
        or request.path.startswith(f"{_SHOW_RUNTIME_VENDOR_PREFIX}/")
        or request.path
        in {
            _SHOW_RUNTIME_PUBLIC_CLIENT_SHIM_PATH,
            _SHOW_RUNTIME_PUBLIC_REACT_REFRESH_SHIM_PATH,
        }
        or request.path == "/favicon.ico"
    )


def _oauth_cookie_signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _oauth_device_hash(secret: str, device_id: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"device:{device_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def _oauth_device_id() -> str:
    """The caller's stable per-browser binding id from its device cookie (or None)."""
    return request.cookies.get(REMOTE_OAUTH_DEVICE_COOKIE_NAME) or ""


def _oauth_store_record_device_bound(secret: str, record: dict[str, Any] | None) -> bool:
    """True when the request's device cookie matches the handshake record's binding.

    The store-fallback (cookie-state desync path) is only safe when we can prove the
    callback comes from the same browser that started the flow. The device cookie is
    that proof: it is stable across the iOS authorize excursion and an attacker
    cannot present a victim's value.
    """
    expected = (record or {}).get("device_hash")
    device_id = _oauth_device_id()
    if not expected or not device_id:
        return False
    return hmac.compare_digest(str(expected), _oauth_device_hash(secret, device_id))


def _make_oauth_cookie(secret: str, payload: dict[str, Any]) -> str:
    payload_text = quote(json.dumps(payload, separators=(",", ":")), safe="")
    signature = _oauth_cookie_signature(secret, payload_text)
    return f"{payload_text}.{signature}"


def _read_oauth_cookie(secret: str, value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    payload_text, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _oauth_cookie_signature(secret, payload_text)):
        return None
    try:
        payload = json.loads(unquote(payload_text))
    except Exception:
        return None
    if int(payload.get("exp", 0)) <= int(datetime.now().timestamp()):
        return None
    return payload if isinstance(payload, dict) else None


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _make_oauth_state(secret: str, *, next_target: str, retry: bool = False, rid: str | None = None) -> str:
    payload = {
        "v": 1,
        "r": rid or secrets.token_urlsafe(18),
        "next": next_target,
        "retry": bool(retry),
        "exp": int(datetime.now().timestamp()) + REMOTE_OAUTH_HANDSHAKE_TTL_SECONDS,
    }
    payload_text = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64url_encode(hmac.new(secret.encode("utf-8"), payload_text.encode("ascii"), hashlib.sha256).digest())
    return f"vr1.{payload_text}.{signature}"


def _read_oauth_state(secret: str, value: str | None) -> dict[str, Any] | None:
    if not value or not value.startswith("vr1."):
        return None
    try:
        _, payload_text, signature = value.split(".", 2)
    except ValueError:
        return None
    expected = _b64url_encode(hmac.new(secret.encode("utf-8"), payload_text.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_text).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    if int(payload.get("exp", 0)) <= int(datetime.now().timestamp()):
        return None
    return payload


def _peek_oauth_state_rid(token: str | None) -> str | None:
    """Best-effort extract a vr1 state token's random id, for diagnostics only.

    Does NOT verify the HMAC — purely to compare which state a request carries.
    The ``r`` field is a single-use random nonce, not a secret.
    """
    if not token or not token.startswith("vr1."):
        return None
    try:
        payload = json.loads(_b64url_decode(token.split(".")[1]).decode("utf-8"))
        return (str(payload.get("r", ""))[:12]) or None
    except Exception:
        return None


def _safe_remote_redirect_target(value: Any) -> str:
    if not isinstance(value, str):
        return "/"
    target = value.strip()
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _strip_oauth_retry_param(value: str) -> str:
    target = _safe_remote_redirect_target(value)
    parsed = urlsplit(target)
    query = urlencode(
        [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key != REMOTE_OAUTH_RETRY_PARAM]
    )
    return urlunsplit(("", "", parsed.path or "/", query, ""))


def _add_oauth_retry_param(value: str) -> str:
    target = _strip_oauth_retry_param(value)
    parsed = urlsplit(target)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params.append((REMOTE_OAUTH_RETRY_PARAM, "1"))
    return urlunsplit(("", "", parsed.path or "/", urlencode(params), ""))


def _oauth_callback_arg(name: str) -> str | None:
    return request.args.get(name) or request.args.get(f"amp;{name}")


def _redirect_to_vibe_cloud_login(config: V2Config):
    from vibe import remote_access

    cloud = config.remote_access.vibe_cloud
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    raw_next = request.full_path if request.query_string else request.path
    next_target = _strip_oauth_retry_param(raw_next)
    rid = secrets.token_urlsafe(18)
    state = _make_oauth_state(
        cloud.session_secret,
        next_target=next_target,
        retry=request.args.get(REMOTE_OAUTH_RETRY_PARAM) == "1",
        rid=rid,
    )
    nonce = secrets.token_urlsafe(24)
    # Stable per-browser binding id: reuse the existing device cookie so it stays
    # consistent across the iOS authorize excursion (it is NOT regenerated per flow,
    # unlike the handshake state), generating one only on first use.
    device_id = _oauth_device_id() or secrets.token_urlsafe(24)
    # Persist the handshake server-side keyed by the state id, so the callback can
    # recover the PKCE secrets by the signed URL state even when the cookie desyncs
    # (iOS standalone PWA runs authorize in a separate in-app-browser context). The
    # device_hash binds that recovery to this browser; the cookie below stays the
    # strong per-browser binding for normal browsers.
    remote_access.store_oauth_handshake(
        rid,
        nonce=nonce,
        code_verifier=code_verifier,
        next_target=next_target,
        device_hash=_oauth_device_hash(cloud.session_secret, device_id),
    )
    oauth_cookie = _make_oauth_cookie(
        cloud.session_secret,
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next": next_target,
            "exp": int(datetime.now().timestamp()) + REMOTE_OAUTH_HANDSHAKE_TTL_SECONDS,
        },
    )
    response = Response(status=302)
    response.headers["Location"] = remote_access.authorization_url(config, state, nonce, code_challenge)
    response.set_cookie(
        REMOTE_OAUTH_COOKIE_NAME,
        oauth_cookie,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=REMOTE_OAUTH_HANDSHAKE_TTL_SECONDS,
    )
    response.set_cookie(
        REMOTE_OAUTH_DEVICE_COOKIE_NAME,
        device_id,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=REMOTE_OAUTH_DEVICE_TTL_SECONDS,
    )
    return response


def _restart_vibe_cloud_login_from_state(config: V2Config, state: str | None):
    cloud = config.remote_access.vibe_cloud
    payload = _read_oauth_state(cloud.session_secret, state)
    if not payload or payload.get("retry"):
        return None
    next_target = _safe_remote_redirect_target(payload.get("next"))
    response = redirect(_add_oauth_retry_param(next_target))
    response.delete_cookie(REMOTE_OAUTH_COOKIE_NAME, path="/", secure=True, samesite="Lax")
    return response


# Error codes with dedicated copy in vibe/i18n (remote_access.oauth_error.*); any
# other code falls back to the generic "default_*" strings so an unexpected failure
# still renders a usable page.
_OAUTH_ERROR_PAGE_CODES = {
    "invalid_oauth_state",
    "oauth_exchange_failed",
    "remote_pairing_mismatch",
    "oauth_time_mismatch",
    "invalid_oauth_nonce",
}

_OAUTH_EXCHANGE_ERROR_PAGE_BY_REASON = {
    "invalid_instance_id": "remote_pairing_mismatch",
    "invalid_issuer": "remote_pairing_mismatch",
    "invalid_audience": "remote_pairing_mismatch",
    "expired_id_token": "oauth_time_mismatch",
    "immature_id_token": "oauth_time_mismatch",
}

_OAUTH_DIAGNOSTIC_DETAIL_MAX_CHARS = 240
_OAUTH_DIAGNOSTIC_SECRET_PATTERN = re.compile(
    r"(?i)(?P<key_quote>['\"]?)\b(?P<key>code_verifier|id_token|access_token|refresh_token|code|state|nonce)\b(?P=key_quote)"
    r"(?P<sep>\s*[=:]\s*|%3d)(?P<value_quote>['\"]?)[^&\s,;}]+(?P=value_quote)"
)


def _oauth_exchange_error_page_code(exc: BaseException) -> str:
    from vibe import remote_access

    if isinstance(exc, remote_access.OAuthCodeExchangeError):
        return _OAUTH_EXCHANGE_ERROR_PAGE_BY_REASON.get(exc.reason, "oauth_exchange_failed")
    return "oauth_exchange_failed"


def _sanitize_oauth_diagnostic_detail(value: str | None) -> str:
    if not value:
        return ""
    compact = " ".join(str(value).split())
    redacted = _OAUTH_DIAGNOSTIC_SECRET_PATTERN.sub(lambda match: f"{match.group('key')}=<redacted>", compact)
    if len(redacted) <= _OAUTH_DIAGNOSTIC_DETAIL_MAX_CHARS:
        return redacted
    return redacted[: _OAUTH_DIAGNOSTIC_DETAIL_MAX_CHARS - 1].rstrip() + "..."


def _oauth_exchange_error_diagnostics(exc: BaseException) -> tuple[str, dict[str, str]]:
    from vibe import remote_access

    error = _oauth_exchange_error_page_code(exc)
    if isinstance(exc, remote_access.OAuthCodeExchangeError):
        return error, {
            "reason": exc.reason,
            "detail": _sanitize_oauth_diagnostic_detail(exc.detail),
        }
    return error, {"reason": exc.__class__.__name__}


def _request_ui_language() -> str:
    """Best-effort UI language for a pre-auth page, from the Accept-Language header.

    The Web UI persists its language only in localStorage (not a server-readable
    cookie), so Accept-Language is the available signal here — and it matches what
    the SPA's own navigator-based detection would pick. Falls back to English.
    """
    supported = set(get_supported_languages())
    for part in (request.headers.get("Accept-Language") or "").split(","):
        tag = part.split(";")[0].strip().lower()
        if not tag:
            continue
        if tag in supported:
            return tag
        primary = tag.split("-")[0]
        if primary in supported:
            return primary
    return "en"


def _oauth_error_diagnostics_text(diagnostics: dict[str, Any] | None) -> str:
    if not diagnostics:
        return ""
    fields = ("error", "reason", "detail", "time_utc", "host", "retry_path", "handshake_cookie_present")
    lines = []
    for key in fields:
        value = diagnostics.get(key)
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _render_oauth_error_html(
    error: str,
    *,
    retry_href: str,
    lang: str = "en",
    diagnostics: dict[str, Any] | None = None,
) -> str:
    """Render a branded, self-contained re-login page for a failed OAuth callback.

    Replaces the old raw-JSON dead-end: the user sees a plain-language reason and a
    single re-login button that navigates to ``retry_href`` (a sanitized same-origin
    path), which re-enters the login flow via the auth gate. Copy is served from
    ``vibe/i18n`` in ``lang``.
    """
    key = error if error in _OAUTH_ERROR_PAGE_CODES else "default"
    safe_lang = html.escape(lang, quote=True)
    safe_title = html.escape(t(f"remote_access.oauth_error.{key}_title", lang))
    safe_message = html.escape(t(f"remote_access.oauth_error.{key}_body", lang))
    safe_button = html.escape(t("remote_access.oauth_error.sign_in_again", lang))
    safe_href = html.escape(retry_href or "/", quote=True)
    safe_code = html.escape(error)
    diagnostics_text = _oauth_error_diagnostics_text(diagnostics)
    diagnostics_block = ""
    if diagnostics_text:
        safe_diagnostics_summary = html.escape(t("remote_access.oauth_error.diagnostics_summary", lang))
        safe_diagnostics_hint = html.escape(t("remote_access.oauth_error.diagnostics_hint", lang))
        safe_diagnostics = html.escape(diagnostics_text)
        diagnostics_block = f"""
        <details class="oauth-error-details">
          <summary>{safe_diagnostics_summary}</summary>
          <pre>{safe_diagnostics}</pre>
          <p>{safe_diagnostics_hint}</p>
        </details>"""
    hint = ""
    if error == "invalid_oauth_state":
        hint = f'<p class="oauth-error-hint">{html.escape(t("remote_access.oauth_error.cookie_hint", lang))}</p>'
    return f"""<!doctype html>
<html lang="{safe_lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex">
    <title>{safe_title}</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f6f7f9;
        color: #172033;
      }}
      body {{ margin: 0; min-height: 100vh; }}
      .oauth-error-shell {{
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 32px 18px;
        box-sizing: border-box;
      }}
      .oauth-error-panel {{
        width: min(460px, 100%);
        border: 1px solid rgba(23, 32, 51, 0.12);
        border-radius: 18px;
        background: rgba(255, 255, 255, 0.96);
        padding: clamp(28px, 6vw, 40px);
        box-shadow: 0 24px 80px rgba(23, 32, 51, 0.10);
        box-sizing: border-box;
        text-align: center;
      }}
      .oauth-error-eyebrow {{
        color: #526078;
        font-size: 13px;
        font-weight: 760;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }}
      .oauth-error-panel h1 {{
        margin: 14px 0 0;
        font-size: clamp(24px, 6vw, 32px);
        line-height: 1.12;
        letter-spacing: 0;
      }}
      .oauth-error-panel p {{
        margin: 14px 0 0;
        line-height: 1.65;
        color: #526078;
      }}
      .oauth-error-hint {{ font-size: 13px; }}
      .oauth-error-actions {{ margin-top: 26px; }}
      .oauth-error-button {{
        display: inline-block;
        height: 44px;
        padding: 0 24px;
        border-radius: 12px;
        background: #0f172a;
        color: #fff;
        font: 700 15px/44px Inter, ui-sans-serif, system-ui;
        text-decoration: none;
      }}
      .oauth-error-button:hover {{ background: #1e293b; }}
      .oauth-error-code {{
        margin-top: 22px;
        font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        color: #94a3b8;
      }}
      .oauth-error-details {{
        margin-top: 22px;
        border: 1px solid rgba(23, 32, 51, 0.10);
        border-radius: 12px;
        background: #f8fafc;
        text-align: left;
      }}
      .oauth-error-details summary {{
        cursor: pointer;
        padding: 12px 14px;
        color: #334155;
        font-size: 13px;
        font-weight: 720;
      }}
      .oauth-error-details pre {{
        margin: 0;
        padding: 0 14px 12px;
        color: #334155;
        font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        user-select: all;
      }}
      .oauth-error-details p {{
        margin: 0;
        padding: 0 14px 14px;
        color: #64748b;
        font-size: 12px;
        line-height: 1.5;
      }}
    </style>
  </head>
  <body>
    <main class="oauth-error-shell">
      <section class="oauth-error-panel">
        <div class="oauth-error-eyebrow">Avibe</div>
        <h1>{safe_title}</h1>
        <p>{safe_message}</p>
        {hint}
        <div class="oauth-error-actions">
          <a class="oauth-error-button" href="{safe_href}">{safe_button}</a>
        </div>
        <div class="oauth-error-code">{safe_code}</div>
        {diagnostics_block}
      </section>
    </main>
  </body>
</html>
"""


_OAUTH_DIAG_LOG_INTERVAL_SECONDS = 60.0
_oauth_diag_log_lock = threading.Lock()
# key -> [window_start_monotonic, suppressed_count]
_oauth_diag_log_state: dict[str, list[float]] = {}


def _log_oauth_diag(key: str, message: str, *args: Any) -> None:
    """Emit an unauthenticated-reachable OAuth diagnostic at WARNING, rate-limited
    per ``key`` (~once / ``_OAUTH_DIAG_LOG_INTERVAL_SECONDS``).

    The OAuth callback is reachable without auth, so a flood of invalid callbacks
    would otherwise grow the (unrotated) service log without bound. Suppressed hits
    are counted and folded into the next emitted line so the signal isn't lost.
    """
    now = time.monotonic()
    with _oauth_diag_log_lock:
        window_start, suppressed = _oauth_diag_log_state.get(key, (0.0, 0))
        if window_start and now - window_start < _OAUTH_DIAG_LOG_INTERVAL_SECONDS:
            _oauth_diag_log_state[key] = [window_start, suppressed + 1]
            return
        _oauth_diag_log_state[key] = [now, 0]
    extra = f" [+{int(suppressed)} suppressed in {int(_OAUTH_DIAG_LOG_INTERVAL_SECONDS)}s]" if suppressed else ""
    logger.warning(message + extra, *args)


def _oauth_callback_error_response(
    error: str,
    *,
    next_target: Any,
    status: int = 400,
    diagnostics: dict[str, Any] | None = None,
):
    """Build the HTML re-login response for a failed OAuth callback.

    Clears any stale handshake cookie so "Sign in again" starts a clean flow, and
    strips the auto-retry marker from ``next_target`` so the retry gets a fresh
    attempt (plus one silent auto-retry) instead of immediately failing again.
    """
    # Diagnostic only: whether the handshake cookie reached us at all is the key
    # signal for cookie-loss cases (e.g. iOS standalone PWA). No token values are
    # logged — only presence and a few non-secret request hints. Rate-limited
    # because this path is unauthenticated and could be flooded.
    _log_oauth_diag(
        "callback_rejected",
        "oauth callback rejected: error=%s handshake_cookie_present=%s ua=%r sec_fetch_site=%s",
        error,
        bool(request.cookies.get(REMOTE_OAUTH_COOKIE_NAME)),
        (request.headers.get("User-Agent") or "")[:140],
        request.headers.get("Sec-Fetch-Site") or "",
    )
    retry_href = _strip_oauth_retry_param(next_target if isinstance(next_target, str) else "/")
    diagnostic_payload: dict[str, Any] = {
        "error": error,
        "time_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "host": request.host,
        "retry_path": retry_href,
        "handshake_cookie_present": str(bool(request.cookies.get(REMOTE_OAUTH_COOKIE_NAME))).lower(),
    }
    if diagnostics:
        diagnostic_payload.update(diagnostics)
    response = Response(
        _render_oauth_error_html(
            error,
            retry_href=retry_href,
            lang=_request_ui_language(),
            diagnostics=diagnostic_payload,
        ),
        status=status,
        mimetype="text/html; charset=utf-8",
    )
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(REMOTE_OAUTH_COOKIE_NAME, path="/", secure=True, samesite="Lax")
    return response


# --- Unauthenticated /auth rate limiting -----------------------------------
#
# The login-start redirect and /auth/callback are reachable without a session, so
# a flood of unauthenticated requests is the *root* of the resource-growth concerns
# on this path. A per-client fixed-window limiter bounds that flood at the door, so
# the downstream handshake store and diagnostics stay bounded without each needing
# its own guard. (The per-store cap and per-log throttles remain as cheap backstops.)
_AUTH_RATELIMIT_WINDOW_SECONDS = 60.0
_AUTH_RATELIMIT_MAX_PER_WINDOW = 60  # a real login spends a handful; this only stops floods
_AUTH_RATELIMIT_MAX_TRACKED_CLIENTS = 4096
_auth_ratelimit_lock = threading.Lock()
# Bounded LRU of client -> [window_start_monotonic, count]; the least-recently-seen
# entry is evicted once the table is full, so the table can't grow without bound.
_auth_ratelimit: OrderedDict[str, list[float]] = OrderedDict()


def _auth_client_id() -> str:
    """Client identity for rate limiting.

    Trust the Cloudflare-forwarded client IP only when the request arrived via the
    local tunnel (loopback peer = cloudflared). A direct peer reaching the origin
    port could otherwise set/rotate ``CF-Connecting-IP`` to dodge the limit, so for
    such peers we key on the real connecting address instead.
    """
    forwarded = (request.headers.get("CF-Connecting-IP") or "").strip()
    if forwarded and _is_loopback_peer():
        return f"cf:{forwarded}"
    return f"peer:{(request.remote_addr or 'unknown').strip()}"


def _auth_rate_limited() -> bool:
    """True when the caller has exceeded the unauthenticated /auth request budget."""
    client = _auth_client_id()
    now = time.monotonic()
    with _auth_ratelimit_lock:
        bucket = _auth_ratelimit.get(client)
        if bucket is None or now - bucket[0] >= _AUTH_RATELIMIT_WINDOW_SECONDS:
            # New or rolled-over window. Hard-bound the table before admitting a
            # genuinely new client (evict the least-recently-seen).
            if client not in _auth_ratelimit:
                while len(_auth_ratelimit) >= _AUTH_RATELIMIT_MAX_TRACKED_CLIENTS:
                    _auth_ratelimit.popitem(last=False)
            _auth_ratelimit[client] = [now, 1]
            _auth_ratelimit.move_to_end(client)
            return False
        if bucket[1] >= _AUTH_RATELIMIT_MAX_PER_WINDOW:
            return True
        bucket[1] += 1
        _auth_ratelimit.move_to_end(client)
        return False


def _auth_rate_limit_response():
    """Minimal 429 for an abusive unauthenticated /auth client (no per-request work)."""
    response = Response("Too Many Requests", status=429, mimetype="text/plain; charset=utf-8")
    response.headers["Retry-After"] = str(int(_AUTH_RATELIMIT_WINDOW_SECONDS))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.before_request
def start_api_request_timer():
    if request.path.startswith("/api/"):
        g.api_request_started_at = time.perf_counter()
    return None


@app.before_request
def enforce_remote_access_cookie():
    config = _load_remote_access_config()
    if _remote_auth_exempt_before_host_validation():
        return None
    local_request = _is_local_request(config)
    docker_probe_request = _is_trusted_docker_loopback_probe()
    if config is None:
        if local_request or docker_probe_request:
            return None
        return jsonify({"ok": False, "error": "remote_access_config_unavailable"}), 503
    if _remote_access_public_url_invalid(config) and not (local_request or docker_probe_request):
        return jsonify({"ok": False, "error": "remote_access_public_url_invalid"}), 503
    remote_request = _is_remote_access_request(config)
    if not remote_request:
        if _is_loopback_origin_proxy_request():
            return None
        if not local_request and not docker_probe_request:
            return jsonify({"ok": False, "error": "remote_access_host_mismatch"}), 503
        return None
    if _remote_auth_exempt_path():
        return None
    from vibe import remote_access

    if not config.remote_access.vibe_cloud.enabled:
        return jsonify({"ok": False, "error": "remote_access_disabled"}), 503
    if not config.remote_access.vibe_cloud.session_secret:
        return jsonify({"ok": False, "error": "remote_access_session_secret_missing"}), 503
    payload = remote_access.parse_session_cookie(config, request.cookies.get(remote_access.SESSION_COOKIE_NAME))
    if payload is not None:
        if remote_access.session_needs_renewal(payload):
            g.remote_session_renew = (str(payload.get("email", "")), str(payload.get("sub", "")))
        return None
    if request.method == "GET":
        # Bound unauthenticated login-start floods at the door (this writes a
        # handshake + sets cookies); a real user spends only a couple per login.
        if _auth_rate_limited():
            return _auth_rate_limit_response()
        return _redirect_to_vibe_cloud_login(config)
    return jsonify({"ok": False, "error": "remote_access_login_required"}), 401


@app.before_request
def protect_mutating_ui_requests():
    if request.method not in MUTATING_METHODS:
        return None
    if _is_mutation_guard_exempt():
        return None

    source = _request_origin(request.headers.get("Origin")) or _request_origin(request.headers.get("Referer"))
    if not source:
        return jsonify({"ok": False, "message": "Forbidden: missing origin header"}), 403

    if source != _current_origin():
        return jsonify({"ok": False, "message": "Forbidden: invalid origin"}), 403

    if _is_show_api_mutation():
        return None

    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    csrf_header = request.headers.get(CSRF_HEADER_NAME, "")
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        return jsonify({"ok": False, "message": "Forbidden: invalid csrf token"}), 403

    return None


@app.after_request
def add_api_timing_headers(response: Response) -> Response:
    started_at = getattr(g, "api_request_started_at", None)
    if started_at is None:
        return response
    elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
    elapsed_text = f"{elapsed_ms:.1f}"
    response.headers["Server-Timing"] = f"app;dur={elapsed_text}"
    response.headers["X-Vibe-Request-Ms"] = elapsed_text
    if elapsed_ms >= SLOW_API_REQUEST_MS:
        payload_size = response.headers.get("Content-Length")
        logger.warning(
            "slow api request path=%s method=%s status=%s duration_ms=%.1f size=%s",
            request.path,
            request.method,
            response.status_code,
            elapsed_ms,
            payload_size or "unknown",
        )
    return response


@app.after_request
def add_csrf_cookie(response: Response) -> Response:
    return _ensure_csrf_cookie(response)


@app.after_request
def renew_remote_access_cookie(response: Response) -> Response:
    # Logout handler explicitly clears the session cookie; never re-issue it.
    if getattr(g, "remote_session_logout", False):
        return response
    if _is_current_show_runtime_immutable_asset_request():
        return response
    renew = getattr(g, "remote_session_renew", None)
    if not renew:
        return response
    # Only slide the session cookie when the request was actually accepted.
    # The renew flag is set in the early `enforce_remote_access_cookie`
    # before-request hook, but later guards (e.g. CSRF/origin checks in
    # `protect_mutating_ui_requests`) may still reject the request. Refreshing
    # the cookie on a rejected response would let repeated failed mutations
    # keep a stolen session alive indefinitely without any successful
    # authenticated action.
    if response.status_code >= 400:
        return response
    config = _load_remote_access_config()
    if config is None or not config.remote_access.vibe_cloud.session_secret:
        return response
    from vibe import remote_access

    email, subject = renew
    response.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, email, subject),
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=remote_access.SESSION_TTL_SECONDS,
    )
    return response


def _read_log_entries(log_path: Path, source_key: str, lines: int) -> tuple[list[dict[str, Any]], int]:
    if not log_path.exists():
        return [], 0

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
    file_sort_ns = log_path.stat().st_mtime_ns
    first_recent_line_index = len(all_lines) - len(recent_lines)

    logs_list: list[dict[str, Any]] = []
    for line_offset, raw_line in enumerate(recent_lines):
        line = raw_line.rstrip("\n")
        match = STRUCTURED_LOG_PATTERN.match(line)
        if match:
            parsed_timestamp = match.group(1)
            logs_list.append(
                {
                    "timestamp": parsed_timestamp,
                    "logger": match.group(2),
                    "level": match.group(3),
                    "message": match.group(4),
                    "source": source_key,
                    "_sort_ns": _timestamp_to_sort_ns(parsed_timestamp) or file_sort_ns,
                    "_sort_index": first_recent_line_index + line_offset,
                }
            )
            continue

        if not line:
            continue

        if logs_list and _is_continuation_line(line, logs_list[-1]["message"]):
            logs_list[-1]["message"] += "\n" + line
            continue

        fallback_entry = _fallback_log_entry(line, source_key)
        fallback_entry["_sort_ns"] = file_sort_ns
        fallback_entry["_sort_index"] = first_recent_line_index + line_offset
        logs_list.append(fallback_entry)

    return logs_list, len(all_lines)


def _resolve_log_sources() -> list[dict[str, Any]]:
    resolved = [
        {
            "key": "all",
            "filename": "*",
            "path": "",
            "exists": True,
        }
    ]
    for key, filename, path_factory in LOG_SOURCES:
        path = path_factory()
        resolved.append(
            {
                "key": key,
                "filename": filename,
                "path": str(path),
                "exists": path.exists(),
            }
        )
    return resolved


# =============================================================================
# Error Handler
# =============================================================================


@app.errorhandler(Exception)
def handle_exception(e):
    """Global exception handler - ensures all errors return JSON."""
    # Preserve HTTP status codes for client errors (4xx)
    status_code = getattr(e, "status_code", None)
    detail = getattr(e, "detail", None)
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return jsonify({"error": detail or str(e)}), status_code

    # Log and return 500 for unexpected server errors
    logger.exception("Unhandled exception in UI server")
    return jsonify({"error": str(e)}), 500


# =============================================================================
# GET Endpoints
# =============================================================================


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/status")
def status():
    from vibe import runtime

    payload = runtime.read_status()
    pid_path = paths.get_runtime_pid_path()
    pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
    try:
        running = bool(pid and pid.isdigit() and runtime.service_pid_recorded(int(pid)))
    except Exception as exc:
        logger.warning("Failed to inspect service pid %s: %s", pid, exc)
        running = False
    payload["running"] = running
    payload["pid"] = int(pid) if pid and pid.isdigit() else None
    if running:
        payload["service_pid"] = payload.get("service_pid") or payload["pid"]
    elif payload.get("state") == "running":
        runtime.write_status("stopped", "process not running", None, payload.get("ui_pid"))
        payload = runtime.read_status()
        payload["running"] = False
        payload["pid"] = None
    return jsonify(payload)


@app.websocket("/ws/echo")
async def websocket_echo(websocket: WebSocket):
    if os.environ.get("VIBE_UI_ENABLE_WS_ECHO", "").lower() not in {"1", "true", "yes", "on"}:
        await websocket.close(code=1008)
        return

    client_host = websocket.client.host if websocket.client else ""
    if client_host != "testclient":
        try:
            client_address = ipaddress.ip_address(client_host)
        except ValueError:
            client_address = None
        if client_address is None or not client_address.is_loopback:
            await websocket.close(code=1008)
            return

    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_text(f"echo: {message}")
    except WebSocketDisconnect:
        return


@app.websocket("/show/{session_id}/__vite_hmr")
async def show_runtime_hmr_websocket(websocket: WebSocket, session_id: str):
    from core.show_pages import ShowPageStore

    if not _show_runtime_websocket_authorized(websocket):
        await websocket.close(code=1008)
        return

    store = ShowPageStore()
    try:
        page = store.get(session_id)
        if page is None or page.visibility != "private":
            await websocket.close(code=1008)
            return
    finally:
        store.close()

    await websocket.accept(subprotocol="vite-hmr")
    try:
        await _proxy_show_runtime_websocket(websocket, session_id)
    except Exception:
        logger.debug("Show runtime HMR websocket unavailable", exc_info=True)
        await websocket.close(code=1011)


@app.websocket("/p/{share_id}/__vite_hmr")
async def public_show_runtime_hmr_websocket(websocket: WebSocket, share_id: str):
    from core.show_pages import ShowPageStore

    store = ShowPageStore()
    try:
        page = store.get_by_share_id(share_id)
        if page is None or page.visibility != "public":
            await websocket.close(code=1008)
            return
        session_id = page.session_id
    finally:
        store.close()

    await websocket.accept(subprotocol="vite-hmr")
    try:
        await _proxy_show_runtime_websocket(
            websocket,
            session_id,
            external_prefix=f"/p/{quote(share_id, safe='')}",
        )
    except Exception:
        logger.debug("Public show runtime HMR websocket unavailable", exc_info=True)
        await websocket.close(code=1011)


@app.websocket("/api/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str):
    if not _terminal_enabled():
        await websocket.accept()
        await websocket.close(code=1008)
        return
    if not TERMINAL_SUPPORTED:
        # POSIX-only — no PTY/tmux on native Windows.
        await websocket.accept()
        await websocket.close(code=1008)
        return
    # CSWSH guard. Local trusted terminal sockets have no cookie gate, so the
    # Origin must match the exact socket. Remote terminals are cookie-authenticated,
    # so pin them to the configured public origin as well.
    if not _terminal_origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    if not _show_runtime_websocket_authorized(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    remote_addr = _websocket_client_host(websocket) or "unknown"
    remote_subject = _remote_access_websocket_subject(websocket)
    effective_session_id = _terminal_effective_session_id(session_id, remote_subject)
    session_ref = _terminal_session_log_ref(effective_session_id)
    logger.info("terminal.session_open session_ref=%s remote_addr=%s", session_ref, remote_addr)
    try:
        service = get_terminal_service()
        service.start_reaper()
        await service.handle_websocket(websocket, effective_session_id)
    except TerminalServiceError as exc:
        # Transient "try again shortly" conditions (not server faults): too_many_sessions (cap
        # full) and session_opening (the id is mid-open or mid-teardown). Close with 1013 so
        # the client can auto-retry instead of surfacing a hard error.
        transient = str(exc) in {"too_many_sessions", "session_opening"}
        await websocket.close(code=1013 if transient else 1011)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.debug("Terminal websocket failed", exc_info=True)
        await websocket.close(code=1011)
    finally:
        logger.info("terminal.session_close session_ref=%s remote_addr=%s", session_ref, remote_addr)


@app.route("/api/terminal/<session_id>", methods=["DELETE"])
async def terminal_session_delete(session_id: str):
    if not _terminal_enabled():
        return jsonify({"ok": False, "error": "terminal_disabled"}), 403
    if not TERMINAL_SUPPORTED:
        return jsonify({"ok": False, "error": "terminal_unsupported"}), 403

    terminal_request = _terminal_http_request_adapter()
    if not _terminal_origin_allowed(terminal_request):
        return jsonify({"ok": False, "error": "terminal_origin_forbidden"}), 403
    if not _show_runtime_websocket_authorized(terminal_request):
        return jsonify({"ok": False, "error": "terminal_unauthorized"}), 403

    remote_subject = _remote_access_websocket_subject(terminal_request)
    effective_session_id = _terminal_effective_session_id(session_id, remote_subject)
    session_ref = _terminal_session_log_ref(effective_session_id)
    terminated = await get_terminal_service().terminate(effective_session_id)
    if not terminated:
        logger.info("terminal.session_delete_absent session_ref=%s", session_ref)
        return jsonify({"ok": False, "error": "terminal_session_not_found"}), 404
    logger.info("terminal.session_delete session_ref=%s", session_ref)
    return Response(status=204)


def _terminal_http_request_adapter() -> SimpleNamespace:
    scheme = "wss" if request.is_secure else "ws"
    return SimpleNamespace(
        headers=request.headers,
        cookies=request.cookies,
        client=SimpleNamespace(host=request.remote_addr or ""),
        url=SimpleNamespace(scheme=scheme),
    )


def _show_runtime_websocket_authorized(websocket: Any) -> bool:
    config = _load_remote_access_config()
    if config is None:
        return _websocket_is_local_request(websocket)
    if _websocket_is_local_request(websocket, config):
        return True
    if _websocket_normalized_host(websocket) != _remote_access_public_host(config):
        return False
    return _remote_access_websocket_session_payload(websocket, config) is not None


def _remote_access_websocket_session_payload(websocket: Any, config: V2Config | None) -> dict[str, Any] | None:
    if config is None or _websocket_is_local_request(websocket, config):
        return None
    if _websocket_normalized_host(websocket) != _remote_access_public_host(config):
        return None
    from vibe import remote_access

    if not config.remote_access.vibe_cloud.enabled or not config.remote_access.vibe_cloud.session_secret:
        return None
    return remote_access.parse_session_cookie(
        config,
        websocket.cookies.get(remote_access.SESSION_COOKIE_NAME),
    )


def _remote_access_websocket_subject(websocket: Any) -> str | None:
    payload = _remote_access_websocket_session_payload(websocket, _load_remote_access_config())
    if not payload:
        return None
    subject = str(payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip()
    return subject or email or None


def _terminal_effective_session_id(client_session_id: str, remote_subject: str | None) -> str:
    safe_client_id = sanitize_session_id(client_session_id)
    if not remote_subject:
        return safe_client_id
    subject_hash = hashlib.sha256(remote_subject.encode("utf-8")).hexdigest()[:16]
    return f"{subject_hash}-{safe_client_id[:63]}"


def _terminal_session_log_ref(effective_session_id: str) -> str:
    return hashlib.sha256(effective_session_id.encode("utf-8")).hexdigest()[:16]


def _terminal_origin_allowed(websocket: Any) -> bool:
    origin = _request_origin(websocket.headers.get("origin"))
    if not origin:
        return False
    parsed_origin = urlparse(origin)
    if _normalized_host(parsed_origin.netloc) != _websocket_normalized_host(websocket):
        return False
    # Mirror _show_runtime_websocket_authorized's local classification: loopback AND
    # private setup-host requests are both accepted without a cookie, so both must clear
    # the exact scheme+port check below. Without the config, _websocket_is_local_request
    # cannot recognize a setup-host request as local and we would fall through to the
    # remote host-only relaxation, letting a same-host page on a different scheme/port
    # open a cross-origin terminal socket. Remote (public-host + cookie) requests only
    # need the host match already verified above.
    config = _load_remote_access_config()
    if not _websocket_is_local_request(websocket, config):
        if config is None:
            return False
        return _remote_access_public_origin_matches(origin, config)
    origin_port = _origin_port(parsed_origin.netloc, parsed_origin.scheme)
    request_port = _origin_port(websocket.headers.get("host"), websocket.url.scheme)
    return origin_port == request_port and _terminal_origin_scheme_matches_socket(
        parsed_origin.scheme,
        websocket.url.scheme,
    )


def _origin_port(netloc: str | None, scheme: str | None) -> int | None:
    if not netloc:
        return None
    parsed = urlparse(f"//{netloc}")
    try:
        explicit_port = parsed.port
    except ValueError:
        return None
    if explicit_port is not None:
        return explicit_port
    normalized_scheme = (scheme or "").lower()
    if normalized_scheme in {"https", "wss"}:
        return 443
    if normalized_scheme in {"http", "ws"}:
        return 80
    return None


def _terminal_origin_scheme_matches_socket(origin_scheme: str | None, socket_scheme: str | None) -> bool:
    origin_secure = (origin_scheme or "").lower() == "https"
    socket_secure = (socket_scheme or "").lower() == "wss"
    return origin_secure == socket_secure


def _websocket_is_local_request(websocket: Any, config: V2Config | None = None) -> bool:
    if _websocket_has_forwarded_metadata(websocket):
        return False
    client_host = _websocket_client_host(websocket)
    if client_host == "testclient":
        return _is_loopback_host(websocket.headers.get("host"))
    try:
        client_address = ipaddress.ip_address(client_host)
    except ValueError:
        client_address = None
    if client_address is not None and client_address.is_loopback and _is_loopback_host(websocket.headers.get("host")):
        return True
    if _websocket_is_trusted_docker_loopback_request(websocket):
        return True
    return _websocket_is_setup_host_request(websocket, config)


def _websocket_has_forwarded_metadata(websocket: Any) -> bool:
    forwarded_headers = (
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Port",
        "X-Real-IP",
        "X-Original-Forwarded-For",
        "True-Client-IP",
        "CF-Connecting-IP",
        "CF-Ray",
        "CF-Visitor",
        "CF-IPCountry",
    )
    return any(websocket.headers.get(header) for header in forwarded_headers)


def _websocket_client_host(websocket: Any) -> str:
    client_host = websocket.client.host if websocket.client else ""
    if client_host == "testclient":
        return websocket.headers.get(TEST_REMOTE_ADDR_HEADER) or client_host
    return client_host


def _websocket_peer_address(websocket: Any) -> ipaddress._BaseAddress | None:
    client_host = _websocket_client_host(websocket).strip()
    if not client_host or client_host in {"localhost", "testclient"}:
        return None
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _websocket_is_trusted_docker_peer(websocket: Any) -> bool:
    if not _env_flag_enabled("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS"):
        return False
    if not _has_loopback_only_docker_port_binding():
        return False
    address = _websocket_peer_address(websocket)
    if address is None:
        return False

    return address in _trusted_docker_loopback_peer_addresses()


def _websocket_is_trusted_docker_loopback_request(websocket: Any) -> bool:
    if _websocket_has_forwarded_metadata(websocket):
        return False
    if not _is_loopback_host(websocket.headers.get("host")):
        return False
    return _websocket_is_trusted_docker_peer(websocket)


def _websocket_is_private_peer(websocket: Any) -> bool:
    address = _websocket_peer_address(websocket)
    return address is not None and _is_private_address(address)


def _websocket_peer_shares_setup_host_network(websocket: Any, setup_address: ipaddress._BaseAddress) -> bool:
    peer = _websocket_peer_address(websocket)
    if peer is None:
        return False
    if peer.version != setup_address.version:
        mapped = getattr(peer, "ipv4_mapped", None)
        if mapped is None or mapped.version != setup_address.version:
            return False
        peer = mapped
    network = _setup_host_trust_network(setup_address)
    if network is None:
        return False
    return peer in network


def _websocket_is_wildcard_setup_host_request(websocket: Any, config: V2Config | None) -> bool:
    if config is None:
        return False
    setup_host = _normalized_host(getattr(config.ui, "setup_host", ""))
    if not _is_wildcard_setup_host(setup_host):
        return False
    if _websocket_has_forwarded_metadata(websocket):
        return False

    try:
        host_address = ipaddress.ip_address(_websocket_normalized_host(websocket))
    except ValueError:
        return False
    if host_address.is_unspecified:
        return False
    if not _is_private_address(host_address):
        return False
    if _local_interface_network(host_address, interface_filter=_allows_wildcard_setup_host_trust) is None:
        return False
    if not _websocket_is_private_peer(websocket):
        return False
    if _is_tailscale_overlay_address(host_address):
        peer_address = _websocket_peer_address(websocket)
        return peer_address is not None and _is_trusted_tailscale_peer(peer_address)
    return _websocket_peer_shares_setup_host_network(websocket, host_address)


def _websocket_is_setup_host_request(websocket: Any, config: V2Config | None) -> bool:
    if config is None:
        return False
    setup_host = _normalized_host(getattr(config.ui, "setup_host", ""))
    if not setup_host:
        return False
    if _is_wildcard_setup_host(setup_host):
        return _websocket_is_wildcard_setup_host_request(websocket, config)
    if _is_loopback_host(setup_host):
        return False
    try:
        setup_address = ipaddress.ip_address(setup_host)
    except ValueError:
        return False
    if not _is_private_address(setup_address):
        return False
    if _websocket_normalized_host(websocket) != setup_host:
        return False
    if _websocket_has_forwarded_metadata(websocket):
        return False
    if not _websocket_is_private_peer(websocket):
        return False
    if _is_tunnel_wildcard_bind(config):
        return _websocket_peer_shares_setup_host_network(websocket, setup_address)
    return True


def _websocket_normalized_host(websocket: Any) -> str:
    return _normalized_host(websocket.headers.get("x-forwarded-host") or websocket.headers.get("host"))


async def _proxy_show_runtime_websocket(
    websocket: WebSocket,
    session_id: str,
    *,
    external_prefix: str | None = None,
) -> None:
    from core.show_runtime import get_show_runtime_manager

    if external_prefix is None:
        external_prefix = f"/show/{quote(session_id, safe='')}"
    runtime_path = f"{external_prefix.rstrip('/')}/__vite_hmr"
    if websocket.url.query:
        runtime_path = f"{runtime_path}?{websocket.url.query}"
    upstream_url = await get_show_runtime_manager().websocket_url(runtime_path)
    async with ClientSession() as session:
        async with session.ws_connect(upstream_url, protocols=["vite-hmr"], autoping=True) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            await upstream.close()
                            return
                        if "text" in message:
                            await upstream.send_str(message["text"])
                        elif "bytes" in message:
                            await upstream.send_bytes(message["bytes"])
                except WebSocketDisconnect:
                    await upstream.close()

            async def upstream_to_client():
                async for message in upstream:
                    if message.type == WSMsgType.TEXT:
                        await websocket.send_text(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await websocket.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        await websocket.close()
                        return

            await asyncio.gather(client_to_upstream(), upstream_to_client())


@app.route("/api/doctor", methods=["GET"])
def doctor_get():
    payload = {}
    doctor_path = paths.get_runtime_doctor_path()
    if doctor_path.exists():
        payload = json.loads(doctor_path.read_text(encoding="utf-8"))
    return jsonify(payload)


@app.route("/api/config", methods=["GET"])
def config_get():
    from vibe import api
    from core.services import settings as settings_service

    # On a truly fresh install no config file exists yet, but the setup
    # wizard (and the provider-config modal it reuses, which calls
    # ``getConfig()``) must still load. Serve an in-memory default whose
    # ``setup_state.needs_setup`` is True so the wizard shows and a fresh
    # default is never mistaken for a completed setup. The write side
    # (``save_config``) already creates the file on the first real save.
    config = settings_service.load_config_or_default()
    return jsonify(api.config_to_payload(config))


@app.route("/api/platforms", methods=["GET"])
def platforms_get():
    from vibe import api

    return jsonify(api.get_platform_catalog())


@app.route("/api/agent-backends", methods=["GET"])
def agent_backends_get():
    from vibe import api

    return jsonify(api.get_agent_backend_catalog())


def _vibe_agent_error_response(exc: ValueError):
    message = str(exc)
    lowered = message.lower()
    if "not found" in lowered:
        return jsonify({"ok": False, "code": "agent_not_found", "message": message}), 404
    if "already exists" in lowered:
        return jsonify({"ok": False, "code": "agent_already_exists", "message": message}), 409
    return jsonify({"ok": False, "code": "invalid_agent_request", "message": message}), 400


def _vibe_agent_result_response(result: dict):
    status = 200
    if not result.get("ok", True):
        code = result.get("code")
        if code == "agent_in_use":
            status = 409
        elif code in {"agent_not_found", "agent_import_source_not_found"}:
            status = 404
        else:
            status = 400
    return jsonify(result), status


# Vibe Agent CRUD lives under /api/agents/* — same /api/* convention as
# every other V2 endpoint (/api/sessions, /api/projects, /api/harness/*,
# /api/inbox, ...). The earlier /agents URL collided with the React SPA
# route at the same path; moving the API to /api/agents/* is the root-
# cause fix and removes the Accept-sniffing hack that lived here.
@app.route("/api/agents", methods=["GET"])
def vibe_agents_get():
    from vibe import api

    try:
        include_disabled = str(request.args.get("include_disabled") or request.args.get("all") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return jsonify(api.get_vibe_agents(backend=request.args.get("backend") or None, include_disabled=include_disabled))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/running-agents", methods=["GET"])
async def running_agents_get():
    """Proxy the controller's read-only running-agents snapshot to the workbench.

    Every liveness source lives in the controller process, so this awaits the
    internal Unix-socket snapshot and degrades to an explicit ``unreachable``
    payload (never a misleading empty/0 list) when the controller is down, so the
    Running tab can render a distinct "runtime unreachable" state (I1)."""
    from vibe import internal_client

    try:
        result = await internal_client.list_running_agents()
    except internal_client.InternalServerUnavailable:
        return jsonify({"ok": False, "unreachable": True, "agents": [], "counts": {}}), 503
    except internal_client.InternalServerTimeout:
        return jsonify({"ok": False, "unreachable": True, "timeout": True, "agents": [], "counts": {}}), 504
    return jsonify(result.get("body") or {})


@app.route("/api/running-agents/end", methods=["POST"])
async def running_agents_end():
    """Terminate one running agent's live runtime (Stop turn / disconnect / kill
    orphan), dispatched controller-side by backend+state. Proxies the internal
    socket; degrades to 503 when the controller is down."""
    from vibe import internal_client

    payload = request.json or {}
    try:
        result = await internal_client.end_running_agent(payload)
    except internal_client.InternalServerUnavailable:
        return jsonify({"ok": False, "unreachable": True}), 503
    body = result.get("body") or {}
    # Surface the controller's status (409 when the target couldn't be ended).
    return jsonify(body), (result.get("status_code") or 200)


@app.route("/api/agents/<name>", methods=["GET"])
def vibe_agent_get(name):
    from vibe import api

    try:
        return jsonify(api.get_vibe_agent(name))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents", methods=["POST"])
def vibe_agents_post():
    from vibe import api

    try:
        return jsonify(api.create_vibe_agent(request.json or {}))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/import", methods=["POST"])
def vibe_agents_import_post():
    from vibe import api

    try:
        return _vibe_agent_result_response(api.import_vibe_agents(request.json or {}))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/default", methods=["POST"])
def vibe_agents_default_post():
    from vibe import api

    payload = request.json or {}
    try:
        return jsonify(api.set_default_vibe_agent(payload.get("name") or ""))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/<name>", methods=["PATCH"])
def vibe_agent_patch(name):
    from vibe import api

    try:
        return jsonify(api.update_vibe_agent(name, request.json or {}))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/<name>", methods=["DELETE"])
def vibe_agent_delete(name):
    from vibe import api

    try:
        return _vibe_agent_result_response(api.remove_vibe_agent(name))
    except ValueError as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/settings", methods=["GET"])
def settings_get():
    from vibe import api

    return jsonify(api.get_settings(request.args.get("platform") or None))


def _vault_error_response(exc):
    from vibe import api

    if isinstance(exc, api.VaultApiError):
        return jsonify({"ok": False, "code": exc.code, "message": str(exc)}), exc.status
    return jsonify({"ok": False, "code": "vault_error", "message": str(exc)}), 400


@app.route("/api/vault/secrets", methods=["GET"])
def vault_secrets_get():
    from vibe import api

    return jsonify(api.get_vault_secrets(group=request.args.get("group") or None))


@app.route("/api/vault/pubkey", methods=["GET"])
def vault_pubkey_get():
    from vibe import api

    try:
        return jsonify(api.get_vault_pubkey())
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/agent/pubkey", methods=["GET"])
def vault_agent_pubkey_get():
    from vibe import api

    try:
        return jsonify(api.get_vault_agent_pubkey())
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/vmk", methods=["GET"])
def vault_vmk_get():
    from vibe import api

    return jsonify(api.get_vault_vmk())


@app.route("/api/vault/secrets", methods=["POST"])
def vault_secrets_post():
    from vibe import api

    try:
        return jsonify(api.create_vault_secret(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/secrets/<name>", methods=["DELETE"])
def vault_secret_delete(name):
    from vibe import api

    try:
        return jsonify(api.delete_vault_secret(name))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/requests", methods=["GET"])
def vault_requests_get():
    from vibe import api

    raw_status = request.args.get("status")
    status = None if raw_status == "all" else raw_status or "pending"
    req_type = request.args.get("type") or None
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        limit = 100
    return jsonify(api.get_vault_requests(status=status, request_type=req_type, limit=limit))


@app.route("/api/vault/requests/<request_id>", methods=["GET"])
def vault_request_get(request_id):
    from vibe import api
    from storage import vault_service

    try:
        return jsonify(api.get_vault_request(request_id, audience=vault_service.REQUEST_AUDIENCE_UI))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/requests/access", methods=["POST"])
def vault_access_request_post():
    from vibe import api

    try:
        return jsonify(api.request_vault_access(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/requests/sign", methods=["POST"])
def vault_sign_request_post():
    from vibe import api

    try:
        return jsonify(api.request_vault_sign(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/requests/<request_id>/deny", methods=["POST"])
def vault_request_deny_post(request_id):
    from vibe import api

    try:
        return jsonify(api.deny_vault_request(request_id, request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/requests/<request_id>/fulfill-access", methods=["POST"])
def vault_request_fulfill_access_post(request_id):
    from vibe import api

    try:
        return jsonify(api.fulfill_vault_access_request(request_id, request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/grants", methods=["GET"])
def vault_grants_get():
    from vibe import api

    raw_status = request.args.get("status")
    status = None if raw_status == "all" else raw_status or "active"
    return jsonify(api.get_vault_grants(status=status, session_id=request.args.get("session_id") or None))


@app.route("/api/vault/grants", methods=["POST"])
def vault_grants_post():
    from vibe import api

    try:
        return jsonify(api.create_vault_grant(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/grants/<grant_id>", methods=["DELETE"])
def vault_grant_delete(grant_id):
    from vibe import api

    try:
        return jsonify(api.revoke_vault_grant(grant_id))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/sign", methods=["POST"])
def vault_sign_post():
    from vibe import api

    try:
        return jsonify(api.vault_sign(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/pubkey-pin", methods=["POST"])
def vault_pubkey_pin_post():
    from vibe import api

    try:
        return jsonify(api.store_vault_pubkey_pin(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/audit", methods=["GET"])
def vault_audit_get():
    from vibe import api

    secret = request.args.get("secret") or None
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        limit = 100
    return jsonify(api.get_vault_audit(secret_name=secret, limit=limit))


def _show_page_error_response(exc):
    code = getattr(exc, "code", "invalid_show_page_request")
    # A conflict (not a malformed request) when the page is in the wrong state or
    # the chosen suffix is already claimed.
    status = 409 if code in {"not_public", "share_id_taken"} else 400
    message = str(exc)
    # Structured ``error`` so the Web UI's shared handler localizes via
    # ``errors.<code>`` and falls back to the human message (not the raw code)
    # for any code without an i18n key; top-level ``code``/``message`` stay for
    # the CLI/any direct consumer.
    return jsonify({"ok": False, "error": {"code": code, "message": message}, "code": code, "message": message}), status


@app.route("/api/show-pages", methods=["GET"])
def show_pages_list_get():
    from vibe import api

    return jsonify(api.list_show_pages())


@app.route("/api/show-pages/<session_id>/visibility", methods=["POST"])
def show_page_visibility_post(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    payload = request.json or {}
    try:
        return jsonify(api.set_show_page_visibility(session_id, str(payload.get("visibility") or "")))
    except ShowPageError as exc:
        return _show_page_error_response(exc)


@app.route("/api/show-pages/<session_id>/ensure", methods=["POST"])
def show_page_ensure_post(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    try:
        return jsonify(api.ensure_show_page(session_id))
    except ShowPageError as exc:
        return _show_page_error_response(exc)


@app.route("/api/show-pages/<session_id>/rotate-share", methods=["POST"])
def show_page_rotate_share_post(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    try:
        return jsonify(api.rotate_show_page_share(session_id))
    except ShowPageError as exc:
        return _show_page_error_response(exc)


@app.route("/api/show-pages/<session_id>/share-id", methods=["POST"])
def show_page_set_share_id_post(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    payload = request.json or {}
    try:
        return jsonify(api.set_show_page_share_id(session_id, str(payload.get("share_id") or "")))
    except ShowPageError as exc:
        return _show_page_error_response(exc)


@app.route("/api/csrf-token", methods=["GET"])
def csrf_token_get():
    token = request.cookies.get(CSRF_COOKIE_NAME) or _new_csrf_token()
    response = jsonify({"ok": True, "csrf_token": token})
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        secure=request.is_secure,
        samesite="Strict",
        path="/",
    )
    return response


def _web_push_user_key() -> str:
    """Best-effort local user key for browser push subscriptions.

    Remote-access sessions carry a subject claim; purely local UI sessions do
    not yet have a user identity, so they share the local install namespace.
    """

    config = _load_remote_access_config()
    if config is not None:
        try:
            from vibe import remote_access

            payload = remote_access.parse_session_cookie(
                config, request.cookies.get(remote_access.SESSION_COOKIE_NAME)
            )
            if payload and payload.get("sub"):
                return f"remote:{payload['sub']}"
        except Exception:
            logger.debug("web push: could not resolve remote user key", exc_info=True)
    return "local"


@app.route("/api/web-push/status", methods=["GET", "POST"])
def web_push_status():
    from core.web_push import load_or_create_vapid_keys
    from storage import web_push_service

    keys = load_or_create_vapid_keys()
    body = request.json if request.method == "POST" else {}
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    subscription = body.get("subscription") if isinstance(body, dict) and isinstance(body.get("subscription"), dict) else None
    device_id = body.get("device_id") if isinstance(body, dict) and isinstance(body.get("device_id"), str) else None
    device_label = body.get("device_label") if isinstance(body, dict) and isinstance(body.get("device_label"), str) else None
    previous_endpoints = body.get("previous_endpoints") if isinstance(body, dict) and isinstance(body.get("previous_endpoints"), list) else None
    user_key = _web_push_user_key()
    engine = _projects_engine()
    with engine.begin() as conn:
        if subscription is not None:
            try:
                synced = web_push_service.attach_device_to_enabled_subscription(
                    conn,
                    user_key=user_key,
                    payload=subscription,
                    user_agent=request.headers.get("User-Agent"),
                    device_label=device_label,
                    device_id=device_id,
                    previous_endpoints=previous_endpoints,
                )
                if synced is not None:
                    endpoint = synced["endpoint"]
            except ValueError:
                logger.debug("web push: ignoring invalid status subscription payload", exc_info=True)
        subscription_count = web_push_service.count_enabled(conn, user_key=user_key)
        current_subscription = (
            web_push_service.get_enabled_by_endpoint(
                conn,
                endpoint=endpoint,
                user_key=user_key,
            )
            if isinstance(endpoint, str) and endpoint.strip()
            else None
        )
    return jsonify(
        {
            "ok": True,
            "configured": True,
            "public_key": keys.public_key,
            "subscription_count": subscription_count,
            "current_subscription_enabled": current_subscription is not None,
        }
    )


@app.route("/api/web-push/vapid-public-key", methods=["GET"])
def web_push_vapid_public_key():
    from core.web_push import load_or_create_vapid_keys

    keys = load_or_create_vapid_keys()
    return jsonify({"ok": True, "public_key": keys.public_key})


@app.route("/api/web-push/subscriptions", methods=["POST"])
def web_push_subscribe():
    from storage import web_push_service

    payload = request.json or {}
    user_agent = request.headers.get("User-Agent")
    device_label = payload.get("device_label") if isinstance(payload.get("device_label"), str) else None
    device_id = payload.get("device_id") if isinstance(payload.get("device_id"), str) else None
    previous_endpoints = payload.get("previous_endpoints") if isinstance(payload.get("previous_endpoints"), list) else None
    subscription = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else payload
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            row = web_push_service.upsert_subscription(
                conn,
                user_key=_web_push_user_key(),
                payload=subscription,
                user_agent=user_agent,
                device_label=device_label,
                device_id=device_id,
                previous_endpoints=previous_endpoints,
            )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "subscription": row})


@app.route("/api/web-push/subscriptions", methods=["DELETE"])
def web_push_unsubscribe():
    from storage import web_push_service

    payload = request.json or {}
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return jsonify({"ok": False, "error": "endpoint_required"}), 400
    engine = _projects_engine()
    with engine.begin() as conn:
        disabled = web_push_service.disable_subscription(
            conn,
            endpoint=endpoint,
            user_key=_web_push_user_key(),
        )
    return jsonify({"ok": True, "disabled": disabled})


@app.route("/api/web-push/test", methods=["POST"])
def web_push_test():
    from core.web_push import send_web_push
    from storage import web_push_service

    payload = request.json or {}
    notification = {
        "title": payload.get("title") if isinstance(payload.get("title"), str) else "avibe",
        "body": payload.get("body") if isinstance(payload.get("body"), str) else "Test notification",
        "url": payload.get("url") if isinstance(payload.get("url"), str) else "/inbox",
        "tag": "web-push-test",
    }
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return jsonify({"ok": False, "error": "endpoint_required"}), 400
    user_key = _web_push_user_key()
    engine = _projects_engine()
    sent = 0
    failed = 0
    with engine.connect() as conn:
        subscription = web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=endpoint,
            user_key=user_key,
        )
    if not subscription:
        return jsonify({"ok": False, "error": "no_subscription"}), 404
    try:
        send_web_push(subscription=subscription, payload=notification)
        with engine.begin() as conn:
            web_push_service.mark_send_success(conn, endpoint=subscription["endpoint"])
        sent += 1
    except Exception as exc:
        logger.warning("web push: test send failed", exc_info=True)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        with engine.begin() as conn:
            web_push_service.mark_send_failure(
                conn,
                endpoint=subscription["endpoint"],
                disable=status_code in {404, 410},
            )
        failed += 1
    return jsonify({"ok": failed == 0, "sent": sent, "failed": failed})


@app.route("/api/cli/detect")
def cli_detect():
    from vibe import api

    binary = request.args.get("binary", "")
    return jsonify(api.detect_cli(binary))


@app.route("/api/slack/manifest")
def slack_manifest():
    from vibe import api

    return jsonify(api.get_slack_manifest())


@app.route("/api/version")
def version():
    from vibe import api

    return jsonify(api.get_version_info())


# =============================================================================
# POST Endpoints
# =============================================================================


# Serializes the restart in-flight check + scheduling below. The UI server runs
# requests concurrently, so without this two near-simultaneous restart requests
# could both pass the check before either seeds restart_status.json, scheduling
# two supervisors that race on the same pid files + lock.
_RESTART_CONTROL_LOCK = threading.Lock()
# How long a just-seeded, pid-less "scheduled" status is treated as in flight
# (its supervisor is still starting up). Past this, a pid-less status is stale
# (the supervisor died before recording its pid) and must NOT block restarts.
_RESTART_SEED_GRACE_SECONDS = 60.0


def _restart_in_flight() -> bool:
    """True only when a restart is genuinely still running, so a stale status
    can never permanently block Web restarts."""
    from vibe import runtime

    status = runtime.read_json(runtime.get_restart_status_path()) or {}
    if status.get("state") not in ("scheduled", "running"):
        return False
    sup_pid = status.get("supervisor_pid")
    if isinstance(sup_pid, int):
        if not runtime.pid_alive(sup_pid):
            return False
        # Guard against PID reuse: a dead supervisor's pid can be reclaimed by an
        # unrelated process (notably across a reboot), which would otherwise keep
        # blocking restarts until that process exits. The job records its
        # ``supervisor_started_at`` (process create time), so only treat the pid
        # as the live supervisor when the create time still matches.
        started_at = status.get("supervisor_started_at")
        if started_at is not None:
            current = runtime.process_create_time(sup_pid)
            if current is not None and current != started_at:
                return False
        return True
    # No supervisor pid recorded yet: in flight only while the seed is fresh
    # (the child is still starting). An older pid-less status is stale.
    try:
        age = time.time() - runtime.get_restart_status_path().stat().st_mtime
    except OSError:
        return False
    return age < _RESTART_SEED_GRACE_SECONDS


def _schedule_service_restart_for_config_fallback() -> dict[str, Any]:
    from vibe import runtime
    from vibe.restart_supervisor import mark_pending_restart, schedule_restart

    def _schedule_restart() -> dict[str, Any]:
        status = runtime.read_status()
        runtime.write_status("restarting", "restarting", status.get("service_pid"), status.get("ui_pid"))
        return schedule_restart(delay_seconds=0.0, trigger="web-ui-config", scope="service")

    with _RESTART_CONTROL_LOCK:
        if _restart_in_flight():
            restart_status = runtime.read_json(runtime.get_restart_status_path()) or {}
            pending = mark_pending_restart(
                trigger="web-ui-config-pending",
                scope="service",
                reason="restart_in_progress",
                restart_job_id=restart_status.get("job_id"),
            )
            if not _restart_in_flight():
                try:
                    from vibe.restart_supervisor import _pending_restart_path

                    _pending_restart_path().unlink(missing_ok=True)
                except OSError:
                    logger.debug("Failed to remove stale pending restart marker", exc_info=True)
                restart = _schedule_restart()
                return {
                    "ok": True,
                    "restart": restart,
                    "code": "restart_scheduled_after_in_flight_finished",
                }
            return {
                "ok": True,
                "pending_restart": pending,
                "restart": restart_status,
                "code": "restart_pending_after_in_progress",
            }
        restart = _schedule_restart()
    return {"ok": True, "restart": restart}


def _save_config_and_runtime_decisions(payload: dict) -> tuple[V2Config, bool, bool]:
    from vibe import api
    from vibe import remote_access

    with CONFIG_LOCK:
        previous_config = _load_remote_access_config()
        config = api.save_config(payload)
        should_reconcile_remote_access = False
        if _remote_access_settings_changed(previous_config, config, payload):
            if _should_rotate_remote_session_secret(previous_config, config, payload):
                remote_access.rotate_session_secret(config)
                config = V2Config.load()
            should_reconcile_remote_access = True
        should_reconcile_platforms = _platform_runtime_fields_changed(previous_config, config, payload)
        return config, should_reconcile_remote_access, should_reconcile_platforms


@app.route("/api/control", methods=["POST"])
def control():
    from vibe import runtime
    from vibe.cli import _stop_opencode_server
    from vibe.restart_supervisor import schedule_restart

    payload = request.json or {}
    action = payload.get("action")
    status = runtime.read_status()
    status["last_action"] = action
    if action == "start":
        runtime.ensure_config()
        service_pid = runtime.start_service()
        runtime.write_status("running", "started", service_pid, status.get("ui_pid"))
    elif action == "stop":
        runtime.write_status("stopping", "stopping", status.get("service_pid"), status.get("ui_pid"))
        stopped, error = _stop_runtime_process_or_error(paths.get_runtime_pid_path(), "Vibe service")
        if not stopped:
            runtime.write_status("error", error, status.get("service_pid"), status.get("ui_pid"))
            return jsonify({"ok": False, "action": action, "error": error, "status": runtime.read_status()}), 500
        _stop_opencode_server()
        runtime.write_status("stopped", "stopped", None, status.get("ui_pid"))
    elif action == "restart":
        # Scope defaults to "all" (full restart) so the manual Dashboard /
        # Settings → Service restart buttons keep restarting BOTH processes
        # (a UI host/port change needs the UI server itself to come back up).
        # Only the platform-config flow opts into "service" (keep the Web UI up).
        scope = payload.get("scope") if payload.get("scope") in ("all", "service") else "all"
        # Reject overlapping restarts: a service-only restart leaves the Web UI
        # up, so a user (or another tab) could fire a second restart while the
        # first supervisor is still bouncing the service — two jobs would race
        # on the same pid files + lock. The check + schedule are held under one
        # process lock so two concurrent requests can't both slip through.
        with _RESTART_CONTROL_LOCK:
            if _restart_in_flight():
                return (
                    jsonify(
                        {
                            "ok": False,
                            "action": action,
                            "error": "a restart is already in progress",
                            "code": "restart_in_progress",
                            "status": runtime.read_status(),
                        }
                    ),
                    409,
                )
            runtime.write_status("restarting", "restarting", status.get("service_pid"), status.get("ui_pid"))
            result = schedule_restart(delay_seconds=0.0, trigger="web-ui", scope=scope)
        return jsonify({"ok": True, "action": action, "restart": result, "status": runtime.read_status()})
    return jsonify({"ok": True, "action": action, "status": runtime.read_status()})


@app.route("/api/config", methods=["POST"])
async def config_post():
    from vibe import api
    from vibe import internal_client
    from vibe import remote_access

    payload = request.json or {}
    remote_access_runtime = None
    try:
        config, should_reconcile_remote_access, should_reconcile_platforms = await asyncio.to_thread(
            _save_config_and_runtime_decisions,
            payload,
        )
    except ValueError as exc:
        message = str(exc)
        return jsonify({"ok": False, "error": message, "message": message}), 400
    if should_reconcile_remote_access:
        remote_access_runtime = await asyncio.to_thread(remote_access.reconcile)
    platform_runtime = None
    if should_reconcile_platforms:
        try:
            result = await internal_client.reconcile_platforms()
            platform_runtime = {
                "ok": result.get("status_code") == 200 and bool((result.get("body") or {}).get("ok")),
                "hot_reconciled": result.get("status_code") == 200 and bool((result.get("body") or {}).get("ok")),
                "body": result.get("body") or {},
            }
        except internal_client.InternalServerUnavailable as exc:
            platform_runtime = {"ok": False, "hot_reconciled": False, "error": str(exc)}
        if not platform_runtime.get("ok"):
            restart_result = await asyncio.to_thread(_schedule_service_restart_for_config_fallback)
            platform_runtime["restart_scheduled"] = bool(restart_result.get("ok"))
            if restart_result.get("ok"):
                platform_runtime["restart"] = restart_result.get("restart")
            else:
                platform_runtime["restart_error"] = restart_result.get("error")
                platform_runtime["restart_code"] = restart_result.get("code")
    response_payload = api.config_to_payload(config)
    if remote_access_runtime is not None:
        response_payload["remote_access_runtime"] = remote_access_runtime
    if platform_runtime is not None:
        response_payload["platform_runtime"] = platform_runtime
    return jsonify(response_payload)


@app.route("/api/remote-access/status", methods=["GET"])
def remote_access_status():
    from vibe import remote_access

    return jsonify(remote_access.status())


@app.route("/api/remote-access/vibe-cloud/pair", methods=["POST"])
def remote_access_vibe_cloud_pair():
    from vibe import remote_access

    payload = request.json or {}
    result = remote_access.pair(
        payload.get("pairing_key", ""),
        payload.get("backend_url", "https://avibe.bot"),
        payload.get("device_name", "avibe"),
    )
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/remote-access/start", methods=["POST"])
def remote_access_start():
    from vibe import remote_access

    result = remote_access.start()
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/remote-access/stop", methods=["POST"])
def remote_access_stop():
    from vibe import remote_access

    result = remote_access.stop()
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/auth/callback", methods=["GET"])
def remote_access_auth_callback():
    from vibe import remote_access

    config = _load_remote_access_config()
    if config is None or not _is_remote_access_request(config):
        return jsonify({"error": "remote_access_not_enabled"}), 400
    cloud = config.remote_access.vibe_cloud
    if not cloud.enabled:
        return jsonify({"error": "remote_access_disabled"}), 400
    # Unauthenticated endpoint: bound floods before any store lookup / logging.
    if _auth_rate_limited():
        return _auth_rate_limit_response()
    url_state_token = _oauth_callback_arg("state")
    cookie_state = _read_oauth_cookie(cloud.session_secret, request.cookies.get(REMOTE_OAUTH_COOKIE_NAME))
    url_state = _read_oauth_state(cloud.session_secret, url_state_token)
    # Single-use: consume any server-side handshake for this verified state id, even
    # when we ultimately use the cookie, so the store stays clean.
    store_record = remote_access.pop_oauth_handshake(url_state.get("r")) if url_state else None

    # Prefer the cookie when it matches the URL state (strong per-browser binding,
    # the normal-browser path). Fall back to the server-side handshake when the
    # cookie is missing or carries a different state — the iOS standalone PWA case,
    # where the authorize step runs in a separate in-app-browser context and the
    # cookie desyncs from the state the user actually approved.
    if cookie_state and cookie_state.get("state") == url_state_token:
        code_verifier = cookie_state["code_verifier"]
        handshake_nonce = cookie_state.get("nonce")
        next_target = cookie_state.get("next")
    elif store_record is not None and _oauth_store_record_device_bound(cloud.session_secret, store_record):
        # Store-fallback for the iOS standalone PWA case, where the handshake cookie's
        # state desyncs (authorize ran in a separate in-app-browser context). Gated on
        # the stable device cookie matching the handshake record, which proves this is
        # the same browser that started the flow — so a bare code+state callback URL
        # can't be replayed in another browser (closes login-CSRF). The PWA carries the
        # device cookie unchanged across the excursion, so recovery still succeeds.
        logger.debug("oauth callback recovered via server-side handshake (device-bound, desynced cookie context)")
        code_verifier = store_record["code_verifier"]
        handshake_nonce = store_record.get("nonce")
        next_target = store_record.get("next")
    else:
        # Neither the cookie nor the server-side store yielded the handshake.
        # Rate-limited: this branch is unauthenticated-reachable.
        _log_oauth_diag(
            "state_check_failed",
            "oauth state check failed: cookie_parsed=%s cookie_state_rid=%s url_state_rid=%s url_state_valid=%s",
            cookie_state is not None,
            _peek_oauth_state_rid(cookie_state.get("state")) if cookie_state else None,
            _peek_oauth_state_rid(url_state_token),
            url_state is not None,
        )
        retry_response = _restart_vibe_cloud_login_from_state(config, url_state_token)
        if retry_response is not None:
            return retry_response
        # Auto-retry exhausted (or the state is undecodable): show the re-login page,
        # recovering the original destination from the signed state when possible.
        next_target = url_state.get("next") if url_state else "/"
        return _oauth_callback_error_response("invalid_oauth_state", next_target=next_target)
    try:
        result = remote_access.exchange_oauth_code(config, _oauth_callback_arg("code") or "", code_verifier)
        claims = result["claims"]
    except Exception as exc:
        # Unauthenticated-reachable (valid handshake + bad code), so rate-limited.
        reason = exc.reason if isinstance(exc, remote_access.OAuthCodeExchangeError) else exc.__class__.__name__
        _log_oauth_diag("exchange_failed", "vibe cloud oauth code exchange failed: reason=%s", reason)
        error, diagnostics = _oauth_exchange_error_diagnostics(exc)
        return _oauth_callback_error_response(error, next_target=next_target, diagnostics=diagnostics)
    if claims.get("nonce") != handshake_nonce:
        return _oauth_callback_error_response("invalid_oauth_nonce", next_target=next_target)
    response = Response(status=302)
    response.headers["Location"] = _safe_remote_redirect_target(next_target)
    response.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, str(claims.get("email", "")), str(claims.get("sub", ""))),
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=remote_access.SESSION_TTL_SECONDS,
    )
    response.delete_cookie(REMOTE_OAUTH_COOKIE_NAME, path="/", secure=True, samesite="Lax")
    return response


@app.route("/api/session", methods=["GET"])
def api_session():
    from vibe import remote_access

    config = _load_remote_access_config()
    if config is None or not _is_remote_access_request(config):
        response = jsonify({"remote": False})
    else:
        payload = remote_access.parse_session_cookie(
            config, request.cookies.get(remote_access.SESSION_COOKIE_NAME)
        )
        if payload is None:
            response = jsonify({"remote": True, "authenticated": False})
        else:
            response = jsonify(
                {
                    "remote": True,
                    "authenticated": True,
                    "email": str(payload.get("email", "")),
                    "sub": str(payload.get("sub", "")),
                }
            )
    # Identity payload must never be cached by intermediaries (Cloudflare etc.).
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"
    return response


@app.route("/api/cloud/token", methods=["GET"])
def api_cloud_token():
    """Broker a short-lived avibe.bot user token for the workbench frontend so it
    can call the cloud directly (no tunnel relay). Exempt from the auth redirect
    (like ``/api/session``) and self-checks the session: returns 503
    ``cloud_unavailable`` when there's no authenticated user / no pairing / the
    mint fails, so the frontend cleanly falls back to the local relay."""
    from vibe import remote_access

    config = _load_remote_access_config()
    if config is None:
        return jsonify({"error": "cloud_unavailable"}), 503
    result = remote_access.cloud_token_for_request(
        config, request.cookies.get(remote_access.SESSION_COOKIE_NAME)
    )
    if result is None:
        return jsonify({"error": "cloud_unavailable"}), 503
    response = jsonify(result)
    # Bearer material must never be cached by intermediaries (Cloudflare etc.).
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"
    return response


@app.route("/auth/logout", methods=["POST"])
def remote_access_logout():
    from vibe import remote_access

    # Suppress the after-request renewal so we don't re-issue the cookie we're
    # about to clear; flagged so future hook reorderings stay safe.
    g.remote_session_renew = None
    g.remote_session_logout = True
    response = jsonify({"ok": True})
    response.delete_cookie(
        remote_access.SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/ui/reload", methods=["POST"])
def ui_reload():
    from vibe import runtime

    payload = request.json or {}
    host = payload.get("host")
    port = payload.get("port")
    if not host or not port:
        return jsonify({"error": "host_and_port_required"}), 400
    if not isinstance(host, str):
        return jsonify({"error": "invalid_host"}), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_port"}), 400

    status = runtime.read_status()

    try:
        from core.services import settings as settings_service

        current_config = settings_service.load_config()
    except Exception:
        current_config = None
    if current_config is not None:
        bind_host = runtime.effective_ui_bind_host(current_config, requested_host=host)
    else:
        bind_host = host

    def _restart():
        global _server
        import subprocess
        import sys
        import time
        from config import paths as config_paths

        working_dir = get_working_dir()
        command = f"from vibe.ui_server import run_ui_server; run_ui_server('{bind_host}', {port})"
        stdout_path = config_paths.get_runtime_dir() / "ui_stdout.log"
        stderr_path = config_paths.get_runtime_dir() / "ui_stderr.log"
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        process = subprocess.Popen(
            [sys.executable, "-c", command],
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            cwd=str(working_dir),
            close_fds=True,
        )
        stdout.close()
        stderr.close()
        config_paths.get_runtime_ui_pid_path().write_text(str(process.pid), encoding="utf-8")
        runtime.write_status(
            status.get("state", "running"),
            status.get("detail"),
            status.get("service_pid"),
            process.pid,
        )
        time.sleep(0.2)
        # Shutdown the old server to release the port
        if _server:
            if hasattr(_server, "should_exit"):
                _server.should_exit = True
            else:
                shutdown = getattr(_server, "shutdown", None)
                if callable(shutdown):
                    shutdown()

    # Schedule restart after response is sent
    threading.Thread(target=_restart).start()
    return jsonify({"ok": True, "host": host, "port": port})


@app.route("/api/settings", methods=["POST"])
def settings_post():
    from vibe import api

    payload = request.json or {}
    return jsonify(api.save_settings(payload))


@app.route("/api/slack/auth_test", methods=["POST"])
def slack_auth_test():
    from vibe import api

    payload = request.json or {}
    result = api.slack_auth_test(
        payload.get("bot_token", ""),
        proxy_url=payload.get("proxy_url"),
    )
    return jsonify(result)


@app.route("/api/slack/channels", methods=["POST"])
def slack_channels():
    from vibe import api

    payload = request.json or {}
    return jsonify(
        api.list_channels(
            payload.get("bot_token", ""),
            browse_all=payload.get("browse_all", False),
            force=payload.get("force", False) or request.args.get("force") == "1",
            include_not_returned=bool(payload.get("include_not_returned", False)),
        )
    )


@app.route("/api/discord/auth_test", methods=["POST"])
async def discord_auth_test():
    from vibe import api

    payload = request.json or {}
    result = await api.discord_auth_test_async(
        payload.get("bot_token", ""),
        proxy_url=payload.get("proxy_url"),
    )
    return jsonify(result)


@app.route("/api/discord/guilds", methods=["POST"])
async def discord_guilds():
    from vibe import api

    payload = request.json or {}
    return jsonify(await api.discord_list_guilds_async(payload.get("bot_token", "")))


@app.route("/api/discord/channels", methods=["POST"])
def discord_channels():
    from vibe import api

    payload = request.json or {}
    return jsonify(
        api.discord_list_channels(
            payload.get("bot_token", ""),
            payload.get("guild_id", ""),
            force=payload.get("force", False) or request.args.get("force") == "1",
            include_not_returned=bool(payload.get("include_not_returned", False)),
        )
    )


@app.route("/api/channels/delete", methods=["POST"])
def channels_delete():
    from vibe import api

    payload = request.json or {}
    # Channel-only by design: forward the requested scope_type so a non-channel
    # request is explicitly rejected at this boundary (api.delete_channel_scope
    # returns an error for anything other than "channel") rather than silently
    # deleting the channel scope that happens to share the id.
    return jsonify(
        api.delete_channel_scope(
            payload.get("platform", ""),
            payload.get("id", ""),
            scope_type=payload.get("scope_type", "channel"),
        )
    )


@app.route("/api/telegram/auth_test", methods=["POST"])
async def telegram_auth_test():
    from vibe import api

    payload = request.json or {}
    result = await api.telegram_auth_test_async(
        payload.get("bot_token", ""),
        proxy_url=payload.get("proxy_url")
    )
    return jsonify(result)


@app.route("/api/telegram/chats", methods=["POST"])
def telegram_chats():
    from vibe import api

    payload = request.json or {}
    return jsonify(
        api.telegram_list_chats(
            include_private=payload.get("include_private", False),
            include_not_returned=bool(payload.get("include_not_returned", False)),
        )
    )


@app.route("/api/lark/auth_test", methods=["POST"])
async def lark_auth_test():
    from vibe import api

    payload = request.json or {}
    result = await api.lark_auth_test_async(
        payload.get("app_id", ""),
        payload.get("app_secret", ""),
        payload.get("domain", "feishu"),
        proxy_url=payload.get("proxy_url"),
    )
    return jsonify(result)


@app.route("/api/lark/chats", methods=["POST"])
def lark_chats():
    from vibe import api

    payload = request.json or {}
    return jsonify(
        api.lark_list_chats(
            payload.get("app_id", ""),
            payload.get("app_secret", ""),
            payload.get("domain", "feishu"),
            force=payload.get("force", False) or request.args.get("force") == "1",
            include_not_returned=bool(payload.get("include_not_returned", False)),
        )
    )


@app.route("/api/lark/temp_ws/start", methods=["POST"])
def lark_temp_ws_start():
    from vibe import api

    payload = request.json or {}
    return jsonify(
        api.lark_temp_ws_start(
            payload.get("app_id", ""), payload.get("app_secret", ""), payload.get("domain", "feishu")
        )
    )


@app.route("/api/lark/temp_ws/stop", methods=["POST"])
def lark_temp_ws_stop():
    from vibe import api

    return jsonify(api.lark_temp_ws_stop())


# WeChat auth singleton
_wechat_auth_manager = None


def _get_wechat_auth():
    global _wechat_auth_manager
    if _wechat_auth_manager is None:
        from modules.im.wechat_auth import WeChatAuthManager

        _wechat_auth_manager = WeChatAuthManager()
    return _wechat_auth_manager


def _load_wechat_local_tokens() -> list[str]:
    try:
        from core.services import settings as settings_service

        config = settings_service.load_config()
    except Exception:
        logger.warning("Failed to load WeChat local token list for QR login", exc_info=True)
        return []
    token = getattr(getattr(config, "wechat", None), "bot_token", "")
    if isinstance(token, str) and token.strip():
        return [token.strip()]
    return []


def _schedule_wechat_qr_login_restart() -> dict:
    """Schedule a managed restart after QR-login credentials are persisted."""
    from vibe.restart_supervisor import schedule_restart

    return schedule_restart(delay_seconds=2.0, trigger="wechat-qr-login")


def _persist_wechat_qr_credentials(result: dict) -> None:
    token = result.get("bot_token")
    if not isinstance(token, str) or not token.strip():
        return

    from vibe import api as vibe_api
    from core.services import settings as settings_service

    config = settings_service.load_config(default_factory=settings_service.default_config)
    current = vibe_api.config_to_payload(config, include_secrets=True)
    wechat = dict(current.get("wechat") or {})
    wechat["bot_token"] = token.strip()
    if isinstance(result.get("base_url"), str) and result["base_url"].strip():
        wechat["base_url"] = result["base_url"].strip()
    elif not wechat.get("base_url"):
        wechat["base_url"] = "https://ilinkai.weixin.qq.com"
    current["wechat"] = wechat

    platforms = dict(current.get("platforms") or {})
    enabled = list(platforms.get("enabled") or [])
    if "wechat" not in enabled:
        enabled.append("wechat")
    platforms["enabled"] = enabled
    if not platforms.get("primary") or platforms.get("primary") == "avibe":
        platforms["primary"] = "wechat"
    current["platforms"] = platforms

    vibe_api.save_config(current)


WECHAT_QR_LOGIN_BASE_URL = "https://ilinkai.weixin.qq.com"


@app.route("/api/wechat/qr_login/start", methods=["POST"])
async def wechat_qr_login_start():
    """Start WeChat QR code login flow."""
    auth = _get_wechat_auth()

    result = await auth.start_login(
        base_url=WECHAT_QR_LOGIN_BASE_URL,
        local_token_list=_load_wechat_local_tokens(),
    )
    if result.get("ok") is False:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/wechat/qr_login/poll", methods=["POST"])
async def wechat_qr_login_poll():
    """Poll WeChat QR code login status."""
    payload = request.json or {}
    session_key = payload.get("session_key", "")
    if not session_key:
        return jsonify({"error": "session_key required"}), 400
    verify_code = payload.get("verify_code")
    if verify_code is not None and not isinstance(verify_code, str):
        return jsonify({"error": "invalid_verify_code"}), 400

    auth = _get_wechat_auth()
    result = await auth.poll_status(session_key, verify_code=verify_code)
    if result.get("ok") is False:
        return jsonify(result), 500

    # If confirmed, auto-bind the WeChat user
    if result.get("status") == "confirmed" and result.get("bot_token") and result.get("user_id"):
        user_id = result["user_id"]

        try:
            _persist_wechat_qr_credentials(result)
        except Exception as exc:
            logger.error("Failed to persist WeChat QR credentials: %s", exc)
            return jsonify({"ok": False, "error": "failed_to_persist_wechat_credentials"}), 500

        # Auto-bind user
        try:
            from vibe import api as vibe_api

            vibe_api.auto_bind_wechat_user(user_id)
        except Exception as e:
            logger.warning("Failed to auto-bind WeChat user: %s", e)

        try:
            restart = _schedule_wechat_qr_login_restart()
            logger.info("Scheduled service restart after WeChat QR login: %s", restart.get("job_id"))
        except Exception as exc:
            logger.warning("Failed to schedule service restart after WeChat QR login: %s", exc)

    return jsonify(result)


@app.route("/api/doctor", methods=["POST"])
def doctor_post():
    from vibe.cli import _doctor

    result = _doctor()
    return jsonify(result)


@app.route("/api/logs", methods=["POST"])
def logs():
    payload = request.json or {}
    try:
        lines = max(int(payload.get("lines", 500)), 1)
    except (TypeError, ValueError):
        lines = 500
    selected_source = payload.get("source", "service")
    sources = _resolve_log_sources()
    source_map = {source["key"]: source for source in sources}
    active_source = source_map.get(selected_source) or source_map["all"]

    try:
        aggregated_logs: list[dict[str, Any]] = []
        aggregated_total = 0
        for source in sources:
            if source["key"] == "all":
                continue
            source_logs, total = _read_log_entries(Path(source["path"]), source["key"], lines)
            source["total"] = total
            aggregated_logs.extend(source_logs)
            aggregated_total += total
            if source["key"] == active_source["key"]:
                source["logs"] = source_logs
                active_logs = source_logs
                active_total = total
            else:
                source["logs"] = []
        sources[0]["total"] = aggregated_total
        sources[0]["logs"] = []
        if active_source["key"] == "all":
            active_logs = sorted(
                aggregated_logs,
                key=lambda entry: (
                    int(entry.get("_sort_ns", 0)),
                    int(entry.get("_sort_index", 0)),
                    entry.get("source") or "",
                    entry.get("logger") or "",
                ),
            )
            if len(active_logs) > lines:
                active_logs = active_logs[-lines:]
            active_total = aggregated_total
        return jsonify(
            {
                "source": active_source["key"],
                "logs": _serialize_log_entries(active_logs),
                "total": active_total,
                "sources": sources,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/opencode/options", methods=["POST"])
async def opencode_options():
    from vibe import api

    payload = request.json or {}
    result = await api.opencode_options_async(payload.get("cwd", "."))
    return jsonify(result)


@app.route("/api/upgrade", methods=["POST"])
def upgrade():
    from vibe import api

    result = api.do_upgrade()
    return jsonify(result)


@app.route("/api/opencode/setup-permission", methods=["POST"])
def opencode_setup_permission():
    from vibe import api

    return jsonify(api.setup_opencode_permission())


@app.route("/api/opencode/permission-status", methods=["GET"])
def opencode_permission_status():
    # Cheap, read-only check (no OpenCode server start) so the setup wizard can
    # hide the write-allow affordance once opencode.json already grants it —
    # mirroring the Settings provider page, which derives the same flag from the
    # heavier provider probe.
    from vibe import api

    return jsonify(api.opencode_permission_status())


@app.route("/api/claude/agents", methods=["GET"])
def claude_agents():
    from vibe import api

    cwd = request.args.get("cwd")
    if cwd:
        # Expand ~ first, then check if absolute
        expanded = Path(cwd).expanduser()
        if not expanded.is_absolute():
            cwd = str(get_working_dir() / cwd)
        else:
            cwd = str(expanded)

    return jsonify(api.claude_agents(cwd))


@app.route("/api/codex/agents", methods=["GET"])
def codex_agents():
    from vibe import api

    cwd = request.args.get("cwd")
    if cwd:
        expanded = Path(cwd).expanduser()
        if not expanded.is_absolute():
            cwd = str(get_working_dir() / cwd)
        else:
            cwd = str(expanded)

    return jsonify(api.codex_agents(cwd))


@app.route("/api/claude/models", methods=["GET"])
def claude_models():
    from vibe import api

    return jsonify(api.claude_models())


@app.route("/api/codex/models", methods=["GET"])
def codex_models():
    from vibe import api

    return jsonify(api.codex_models())


@app.route("/api/agent/<name>/install", methods=["POST"])
def agent_install(name):
    """Install an agent CLI tool (opencode, claude, codex)."""
    if name not in _ALLOWED_BACKENDS:
        return jsonify({"ok": False, "message": f"Unknown agent: {name}"}), 400

    from vibe import api

    result = api.start_agent_install_job(name)
    return jsonify(result)


@app.route("/api/agent/<name>/install/<job_id>", methods=["GET"])
def agent_install_status(name, job_id):
    """Poll a background agent CLI install/upgrade job."""
    if name not in _ALLOWED_BACKENDS:
        return jsonify({"ok": False, "message": f"Unknown agent: {name}"}), 400

    from vibe import api

    result = api.get_agent_install_job(job_id, backend=name)
    status = 404 if not result.get("ok") and result.get("error") == "job_not_found" else 200
    return jsonify(result), status


_ALLOWED_BACKENDS = set(AGENT_BACKENDS)


@app.route("/api/backend/<name>/runtime")
def backend_runtime(name):
    """Return lifecycle info (version, update, process status) for a backend."""
    if name not in _ALLOWED_BACKENDS:
        return jsonify({"ok": False, "error": f"Unknown backend: {name}"}), 400

    from vibe import api

    return jsonify(api.get_backend_runtime(name))


@app.route("/api/backend/<name>/restart", methods=["POST"])
def backend_restart(name):
    """Refresh a backend's runtime state after settings change."""
    if not supports_runtime_refresh(name):
        return jsonify({"ok": False, "message": f"Restart is not supported for backend: {name}"}), 400

    from vibe import api

    metadata = {
        "reason": "manual_backend_restart",
        "source": "ui_route",
        "route": request.path,
        "method": request.method,
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        "user_agent": (request.headers.get("User-Agent") or "")[:160],
    }
    return jsonify(api.restart_backend(name, metadata=metadata))


_ALLOWED_DEPENDENCIES = {"askill", "avault", "show-runtime", "tmux"}


@app.route("/api/dependencies")
def get_dependencies():
    """Status of required local runtime dependencies (askill, Show runtime, Node)."""
    from vibe import api

    return jsonify(api.dependencies_status())


@app.route("/api/dependencies/<dep>/install", methods=["POST"])
def dependency_install(dep):
    """Install/repair a required local dependency in a background job."""
    if dep not in _ALLOWED_DEPENDENCIES:
        return jsonify({"ok": False, "message": f"Unknown dependency: {dep}"}), 400

    from vibe import api

    return jsonify(api.start_dependency_install_job(dep))


@app.route("/api/dependencies/<dep>/install/<job_id>", methods=["GET"])
def dependency_install_status(dep, job_id):
    """Poll a background dependency install job."""
    if dep not in _ALLOWED_DEPENDENCIES:
        return jsonify({"ok": False, "message": f"Unknown dependency: {dep}"}), 400

    from vibe import api

    result = api.get_agent_install_job(job_id, backend=dep)
    status = 404 if not result.get("ok") and result.get("error") == "job_not_found" else 200
    return jsonify(result), status


@app.route("/api/backend/codex/auth", methods=["GET"])
def backend_codex_auth_get():
    """Read the user-facing Codex auth state (masked secrets)."""
    from vibe import api

    return jsonify(api.get_codex_auth())


@app.route("/api/backend/codex/auth", methods=["POST"])
def backend_codex_auth_post():
    """Persist Codex auth and reload the app-server.

    Body: ``{auth_mode: 'oauth'|'api_key', api_key?: string, base_url?: string}``.
    """
    from vibe import api

    payload = request.json or {}
    return jsonify(api.save_codex_auth(payload))


@app.route("/api/backend/claude/auth", methods=["GET"])
def backend_claude_auth_get():
    """Read the user-facing Claude auth state (masked secrets)."""
    from vibe import api

    return jsonify(api.get_claude_auth())


@app.route("/api/backend/claude/auth", methods=["POST"])
def backend_claude_auth_post():
    """Persist Claude auth into V2Config.

    Body: ``{auth_mode: 'oauth'|'api_key', api_key?: string, base_url?: string}``.
    Claude relaunches per request, so no daemon restart is necessary —
    the next user message picks up the new env injection automatically.
    """
    from vibe import api

    payload = request.json or {}
    return jsonify(api.save_claude_auth(payload))


@app.route("/api/backend/<backend>/auth/oauth/start", methods=["POST"])
async def backend_oauth_web_start(backend: str):
    """Kick off a Settings → Backends OAuth flow for Claude or Codex.

    Body: ``{force_reset?: bool}``. Returns ``{flow_id, state, url?,
    device_code?, awaiting_code?}``. The caller polls ``GET .../status/<flow_id>``
    while the user completes login externally.
    """
    from vibe import api

    payload = request.json or {}
    force_reset = bool(payload.get("force_reset", True))
    return jsonify(await api.start_oauth_web_async(backend, force_reset=force_reset))


@app.route("/api/backend/<backend>/auth/oauth/status/<flow_id>", methods=["GET"])
def backend_oauth_web_status(backend: str, flow_id: str):
    """Poll an in-flight Settings OAuth flow."""
    from vibe import api

    _ = backend  # backend is encoded in the flow itself; path arg kept for symmetry
    return jsonify(api.get_oauth_web_status(flow_id))


@app.route("/api/backend/<backend>/auth/oauth/submit-code", methods=["POST"])
async def backend_oauth_web_submit_code(backend: str):
    """Submit the Claude OAuth callback code (Codex device-auth ignores this)."""
    from vibe import api

    _ = backend
    payload = request.json or {}
    flow_id = str(payload.get("flow_id") or "").strip()
    code = str(payload.get("code") or "")
    return jsonify(await api.submit_oauth_web_code_async(flow_id, code))


@app.route("/api/backend/<backend>/auth/oauth/cancel", methods=["POST"])
async def backend_oauth_web_cancel(backend: str):
    """Cancel an in-flight Settings OAuth flow."""
    from vibe import api

    _ = backend
    payload = request.json or {}
    flow_id = str(payload.get("flow_id") or "").strip()
    return jsonify(await api.cancel_oauth_web_async(flow_id))


@app.route("/api/backend/<backend>/auth/oauth/remove", methods=["POST"])
async def backend_oauth_web_remove(backend: str):
    """Clear stored credentials for a Claude/Codex backend."""
    from vibe import api

    return jsonify(await api.remove_backend_auth_async(backend))


@app.route("/api/backend/claude/auth/oauth/credentials/remove", methods=["POST"])
async def claude_oauth_credentials_remove():
    """Clear Claude OAuth credentials without touching API-key auth."""
    from vibe import api

    return jsonify(await api.remove_claude_oauth_credentials_async())


@app.route("/api/backend/<backend>/auth/api-key/remove", methods=["POST"])
def backend_auth_api_key_remove(backend: str):
    """Clear the stored API key (V2Config + Codex auth.json) without
    touching OAuth credentials. Per-backend symmetry of OpenCode's
    per-provider DELETE."""
    from vibe import api

    return jsonify(api.remove_backend_api_key(backend))


@app.route("/api/backend/<backend>/auth/test", methods=["POST"])
async def backend_auth_test(backend: str):
    """Send a single-token probe through the backend CLI to verify auth."""
    from vibe import api

    payload = request.json or {}
    raw_model = payload.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    return jsonify(await api.test_backend_auth_async(backend, model=model))


@app.route("/api/backend/opencode/providers", methods=["GET"])
async def backend_opencode_providers():
    """Return the merged OpenCode provider catalog for the Settings UI.

    Fans out to the live OpenCode daemon's ``/provider``, ``/provider/auth``,
    and ``/config/providers`` endpoints and merges them into a list of
    ``{id, name, configured, oauth_available, local, models, default_model}``.
    """
    from vibe import api

    return jsonify(await api.get_opencode_providers_async())


@app.route("/api/backend/opencode/custom-provider", methods=["POST"])
async def backend_opencode_custom_provider_post():
    """Create or update a user-defined OpenCode compatible provider."""
    from vibe import api

    payload = request.json or {}
    return jsonify(await api.save_opencode_custom_provider_async(payload))


@app.route("/api/backend/opencode/custom-provider/<provider_id>", methods=["DELETE"])
async def backend_opencode_custom_provider_delete(provider_id: str):
    """Remove one user-defined OpenCode compatible provider."""
    from vibe import api

    return jsonify(await api.delete_opencode_custom_provider_async(provider_id))


@app.route(
    "/api/backend/opencode/provider/<provider_id>/auth/oauth/start",
    methods=["POST"],
)
async def backend_opencode_provider_oauth_start(provider_id: str):
    """Kick off a Settings → Backends OAuth flow for a single OpenCode provider.

    Body: ``{force_reset?: bool}``. Returns ``{flow_id, state, url?,
    device_code?}``. The status/cancel endpoints are the same generic
    ``/api/backend/opencode/auth/oauth/status/<flow_id>`` etc.
    """
    from vibe import api

    payload = request.json or {}
    force_reset = bool(payload.get("force_reset", True))
    return jsonify(await api.start_oauth_web_async("opencode", force_reset=force_reset, provider_id=provider_id))


@app.route("/api/backend/opencode/provider/<provider_id>/auth", methods=["POST"])
async def backend_opencode_provider_auth_post(provider_id: str):
    """Persist an API key for a single OpenCode provider.

    Body: ``{api_key: string}``. avibe writes API keys to
    ``opencode.json`` provider options so provider config and runtime
    invocation share the same source of truth. OpenCode's auth endpoint
    is used only when clearing conflicting auth.json entries.
    """
    from vibe import api

    payload = request.json or {}
    return jsonify(await api.save_opencode_provider_auth_async(provider_id, payload))


@app.route("/api/backend/opencode/provider/<provider_id>/auth", methods=["DELETE"])
async def backend_opencode_provider_auth_delete(provider_id: str):
    """Drop the stored API key for a single OpenCode provider."""
    from vibe import api

    return jsonify(await api.delete_opencode_provider_auth_async(provider_id))


@app.route("/api/backend/opencode/provider/<provider_id>/test", methods=["POST"])
async def backend_opencode_provider_test(provider_id: str):
    """Run a per-provider connectivity probe through OpenCode's HTTP API.

    Body: ``{model?: string}``. The model id is wrapped server-side
    into the ``{providerID, modelID}`` shape OpenCode expects.
    """
    from vibe import api

    payload = request.json or {}
    raw_model = payload.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    return jsonify(await api.test_opencode_provider_async(provider_id, model=model))


@app.route("/api/backend/opencode/default-provider", methods=["POST"])
def backend_opencode_default_provider():
    """Persist the user's default OpenCode provider into V2Config.

    Body: ``{provider_id: string}``. No daemon contact — the default
    is consulted at session-routing time, not by OpenCode itself.
    """
    from vibe import api

    payload = request.json or {}
    return jsonify(api.set_opencode_default_provider(payload))


@app.route("/api/backend/opencode/provider/<provider_id>/models", methods=["POST"])
async def backend_opencode_provider_model_post(provider_id: str):
    """Add or update one user-managed model for an OpenCode provider."""
    from vibe import api

    payload = request.json or {}
    return jsonify(await api.save_opencode_provider_model_async(provider_id, payload))


@app.route("/api/backend/opencode/provider/<provider_id>/models/<path:model_id>", methods=["DELETE"])
async def backend_opencode_provider_model_delete(provider_id: str, model_id: str):
    """Remove one user-managed model for an OpenCode provider."""
    from vibe import api

    return jsonify(await api.delete_opencode_provider_model_async(provider_id, model_id))


@app.route("/api/browse", methods=["POST"])
def browse_directory():
    """List sub-directories of a given path for the directory picker UI."""
    from vibe import api

    payload = request.json or {}
    return jsonify(
        api.browse_directory(
            payload.get("path", "~"),
            show_hidden=bool(payload.get("show_hidden", False)),
        )
    )


@app.route("/api/browse/favorites", methods=["GET"])
def browse_favorites():
    """OS-appropriate quick-access directories for the directory picker."""
    from vibe import api

    return jsonify(api.browse_favorites())


# =============================================================================
# Workbench: Projects + folder-picker helpers
# =============================================================================
# Projects are stored as avibe scopes (platform='avibe', scope_type='project')
# with the local folder path on ``scope_settings.workdir``. See
# ``storage/projects_service.py`` for the CRUD semantics; the routes below
# are a thin REST surface over the same service so the workbench UI and any
# future CLI both round-trip the same shape.


def _projects_engine():
    from storage.db import create_sqlite_engine

    return create_sqlite_engine()


@app.route("/api/projects", methods=["GET"])
def projects_list():
    from storage import projects_service

    include_archived = request.args.get("include_archived") in {"1", "true", "yes"}
    engine = _projects_engine()
    with engine.connect() as conn:
        return jsonify({"projects": projects_service.list_projects(conn, include_archived=include_archived)})


@app.route("/api/projects", methods=["POST"])
def projects_create():
    from storage import projects_service

    payload = request.json or {}
    folder_path = (payload.get("folder_path") or "").strip()
    if not folder_path:
        return jsonify({"error": "folder_path is required"}), 400
    display_name = payload.get("display_name")
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            project = projects_service.create_project(conn, folder_path, display_name=display_name)
    except (FileNotFoundError, NotADirectoryError) as err:
        return jsonify({"error": str(err)}), 400
    return jsonify(project), 201


@app.route("/api/projects/<project_id>", methods=["GET"])
def projects_get(project_id: str):
    from storage import projects_service

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            return jsonify(projects_service.get_project(conn, project_id))
    except LookupError as err:
        return jsonify({"error": str(err)}), 404


@app.route("/api/projects/<project_id>", methods=["PATCH"])
def projects_update(project_id: str):
    from storage import projects_service

    payload = request.json or {}
    display_name = payload.get("display_name")
    folder_path = payload.get("folder_path")
    # Default-Agent fields are only forwarded when present in the body, so an
    # omitted field is left untouched while a present ``null`` clears the default
    # (see ``projects_service.update_project`` and its ``_UNSET`` sentinel).
    agent_kwargs = {
        field: payload[field]
        for field in ("agent_name", "agent_variant", "model", "reasoning_effort")
        if field in payload
    }
    if display_name is None and folder_path is None and not agent_kwargs:
        return jsonify({"error": "no updatable fields provided"}), 400
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            project = projects_service.update_project(
                conn,
                project_id,
                display_name=display_name,
                folder_path=folder_path,
                **agent_kwargs,
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except (FileNotFoundError, NotADirectoryError) as err:
        return jsonify({"error": str(err)}), 400
    return jsonify(project)


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def projects_archive(project_id: str):
    """Soft-delete a project by marking ``scope_settings.enabled = 0``.

    The scope row itself sticks around so any related agent_sessions /
    messages keep their foreign-key target. Pass ``include_archived=1``
    on the list endpoint to surface archived projects in the UI.
    """

    from storage import projects_service

    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            project = projects_service.archive_project(conn, project_id)
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    return jsonify(project)


class _ProjectNoFolder(Exception):
    """A project exists but has no folder configured. Project-scoped skills are
    impossible (askill needs a real cwd), so routes degrade to global or return
    a clear error instead of feeding an empty cwd into the CLI."""


def _resolve_project_dir(project_id):
    """Map a workbench project id to its folder path for project-scoped skills.

    Returns None when no project is given (global scope). Raises LookupError for
    an unknown id (→ 404) and _ProjectNoFolder when the project's folder is
    unset/blank, so callers can degrade gracefully rather than passing an empty
    cwd to askill (which would surface as a raw ``project folder not found:``).
    """
    if not project_id:
        return None
    from storage import projects_service

    engine = _projects_engine()
    with engine.connect() as conn:
        project = projects_service.get_project(conn, project_id)
    folder = (project.get("folder_path") or "").strip()
    if not folder:
        raise _ProjectNoFolder(project_id)
    return folder


def _project_not_found(err):
    return jsonify({"ok": False, "error": {"code": "project_not_found", "message": str(err)}}), 404


def _project_no_folder_error():
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": "project_no_folder",
                    "message": "This project has no folder configured, so it has no project-scoped skills.",
                },
            }
        ),
        400,
    )


@app.route("/api/projects/<project_id>/agents-md", methods=["GET"])
def project_agents_md_get(project_id: str):
    """Read the project's AGENTS.md (falling back to CLAUDE.md) for the editor."""
    from vibe.project_agents_md import read_agents_md

    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    folder = Path(project_dir)
    if not folder.is_dir():
        return jsonify({"error": f"project folder not found: {folder}"}), 400
    return jsonify(read_agents_md(folder))


@app.route("/api/projects/<project_id>/agents-md", methods=["PUT"])
def project_agents_md_save(project_id: str):
    """Write the project's AGENTS.md and reconcile the optional CLAUDE.md symlink."""
    from vibe.project_agents_md import save_agents_md

    payload = request.json or {}
    content = payload.get("content")
    if content is None:
        return jsonify({"error": "content is required"}), 400
    symlink = bool(payload.get("symlink", True))
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    folder = Path(project_dir)
    if not folder.is_dir():
        return jsonify({"error": f"project folder not found: {folder}"}), 400
    return jsonify({"ok": True, **save_agents_md(folder, str(content), symlink)})


@app.route("/api/global-prompts", methods=["GET"])
def global_prompts_get():
    """Read every backend's *global* instructions file for the editor.

    The global twin of the per-project AGENTS.md editor: each backend's
    user-level prompt file (claude→~/.claude/CLAUDE.md, codex→~/.codex/AGENTS.md,
    opencode→~/.config/opencode/AGENTS.md) that the CLI prepends to every
    session's system prompt.
    """
    from vibe.global_agents_md import read_all_global_agents_md

    return jsonify({"backends": read_all_global_agents_md()})


@app.route("/api/global-prompts", methods=["PUT"])
def global_prompts_save():
    """Write content to one or more backends' global instructions files.

    Body ``{"content": str, "backends": ["claude", ...]}``: a single id backs
    per-backend Save, the full set backs one-click Sync. Unknown ids are
    rejected before any write so a bad request can't half-apply.
    """
    from vibe.global_agents_md import write_many_global_agents_md

    payload = request.json or {}
    content = payload.get("content")
    if content is None:
        return jsonify({"error": "content is required"}), 400
    backends = payload.get("backends")
    if not isinstance(backends, list) or not backends:
        return jsonify({"error": "backends must be a non-empty list"}), 400
    try:
        result = write_many_global_agents_md(backends, str(content))
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify({"ok": True, "backends": result})


# Agent Skills — thin shells over api.* (which wraps the askill CLI). Pure
# data CRUD, so it stays in the UI-server process via core/services (no
# dispatch-socket round-trip). See docs/plans/workbench-skills-page.md.
@app.route("/api/skills", methods=["GET"])
async def skills_list():
    from vibe import api

    scope = request.args.get("scope") or "all"
    backends = [b for b in (request.args.get("backends") or "").split(",") if b]
    try:
        project_dir = _resolve_project_dir(request.args.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        # Folderless project: no project-scoped skills are possible — show
        # global skills (with a flag) instead of erroring the whole page.
        result = await api.list_skills(scope="global", backends=backends or None)
        if isinstance(result, dict) and result.get("ok"):
            result = {**result, "project_no_folder": True}
        return jsonify(result)
    return jsonify(await api.list_skills(scope=scope, project_dir=project_dir, backends=backends or None))


@app.route("/api/skills/preview", methods=["POST"])
async def skills_preview():
    from vibe import api

    payload = request.json or {}
    try:
        project_dir = _resolve_project_dir(payload.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        project_dir = None  # preview doesn't need the project folder (gh/zip sources)
    return jsonify(await api.preview_skill_source(str(payload.get("source") or ""), project_dir=project_dir))


@app.route("/api/skills", methods=["POST"])
async def skills_add():
    from vibe import api

    payload = request.json or {}
    try:
        project_dir = _resolve_project_dir(payload.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    return jsonify(
        await api.add_skill(
            str(payload.get("source") or ""),
            scope=payload.get("scope") or "project",
            project_dir=project_dir,
            backends=payload.get("backends") or None,
            all_skills=bool(payload.get("all")),
            skill=payload.get("skill") or None,
            copy=bool(payload.get("copy")),
        )
    )


@app.route("/api/skills/<name>", methods=["DELETE"])
async def skills_remove(name):
    from vibe import api

    backends = [b for b in (request.args.get("backends") or "").split(",") if b]
    try:
        project_dir = _resolve_project_dir(request.args.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    return jsonify(
        await api.remove_skill(
            name,
            scope=request.args.get("scope") or "project",
            project_dir=project_dir,
            backends=backends or None,
        )
    )


@app.route("/api/skills/find", methods=["GET"])
async def skills_find():
    from vibe import api

    return jsonify(await api.find_skills(request.args.get("q") or ""))


@app.route("/api/skills/check", methods=["GET"])
async def skills_check():
    from vibe import api

    scope = request.args.get("scope") or "project"
    try:
        project_dir = _resolve_project_dir(request.args.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        # Folderless project has no project-local skills, so nothing to check.
        return jsonify({"ok": True, "skills": []})
    return jsonify(await api.check_skills(scope=scope, project_dir=project_dir))


@app.route("/api/skills/update", methods=["POST"])
async def skills_update():
    from vibe import api

    payload = request.json or {}
    try:
        project_dir = _resolve_project_dir(payload.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    return jsonify(
        await api.update_skill(
            str(payload.get("name") or ""),
            scope=payload.get("scope") or "project",
            project_dir=project_dir,
        )
    )


@app.route("/api/skills/upload", methods=["POST"])
async def skills_upload():
    from vibe import api

    payload = request.json or {}
    try:
        project_dir = _resolve_project_dir(payload.get("project_id"))
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        # The zip is unpacked to a temp dir (project-independent); the install
        # step picks the scope. Drop the cwd like preview rather than erroring.
        project_dir = None
    return jsonify(await api.upload_skill_zip(payload, project_dir=project_dir))


@app.route("/api/browse/mkdir", methods=["POST"])
def browse_mkdir():
    """Create a new folder for the directory picker.

    Used by the workbench folder picker's "New Folder" button. Errors
    when the target already exists so the UI never silently selects
    someone else's data dir.
    """

    from storage import projects_service

    payload = request.json or {}
    path = (payload.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = projects_service.make_directory(path)
    except FileExistsError:
        return jsonify({"error": f"Folder already exists: {path}"}), 409
    except OSError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify({"path": resolved}), 201


# =============================================================================
# Workbench: Sessions + Messages + Inbox
# =============================================================================
# All endpoints below talk directly to the SQLite store via the workbench
# service modules — ORM all the way down, no CLI shell-outs.
# ``project_id`` (short ``proj_<hex>`` form) is the public id; we expand to
# the full scope_id ``avibe::project::proj_xxx`` inside.


def _project_to_scope_id(project_id: str) -> str:
    return f"avibe::project::{project_id}"


@app.route("/api/sessions", methods=["GET"])
def sessions_list():
    from core.services import sessions as workbench_sessions_service

    project_id = request.args.get("project_id")
    scope_id = _project_to_scope_id(project_id) if project_id else None
    status = request.args.get("status") or "active"
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    before_id = request.args.get("before_id") or None
    # ``q`` powers the chat composer ``#``-mention global title search.
    title_query = request.args.get("q") or None

    engine = _projects_engine()
    with engine.connect() as conn:
        result = workbench_sessions_service.list_sessions(
            conn,
            scope_id=scope_id,
            status=status,
            limit=limit,
            before_id=before_id,
            title_query=title_query,
        )
    return jsonify(result)


@app.route("/api/workbench/projects-bootstrap", methods=["GET"])
def workbench_projects_bootstrap():
    """Projects tree payload with optional first/restored session pages.

    The sidebar and mobile projects page share one provider. This endpoint lets
    that provider refresh projects and any already-expanded project windows with
    one tunnel round-trip, while preserving the dedicated `/api/sessions`
    endpoint for normal pagination.
    """
    from core.services import sessions as workbench_sessions_service
    from storage import projects_service

    include_archived = request.args.get("include_archived") in {"1", "true", "yes"}
    status = request.args.get("status") or "active"
    try:
        limit = int(request.args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    project_ids = [value.strip() for value in request.args.getlist("project_id") if value.strip()]

    engine = _projects_engine()
    with engine.connect() as conn:
        projects = projects_service.list_projects(conn, include_archived=include_archived)
        project_id_set = {project["id"] for project in projects}
        sessions: dict[str, Any] = {}
        for project_id in project_ids:
            if project_id not in project_id_set:
                continue
            sessions[project_id] = workbench_sessions_service.list_sessions(
                conn,
                scope_id=_project_to_scope_id(project_id),
                status=status,
                limit=limit,
            )
    return jsonify({"projects": projects, "sessions": sessions})


@app.route("/api/sessions", methods=["POST"])
def sessions_create():
    from core.services import sessions as workbench_sessions_service
    from vibe.sse_broker import broker

    payload = request.json or {}
    project_id = (payload.get("project_id") or "").strip()
    agent_backend = (payload.get("agent_backend") or "").strip()
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400
    # When the caller doesn't pin a backend/agent (a plain "new chat"), leave
    # agent_backend empty rather than stamping a concrete backend onto the
    # session. A stamped backend is treated by message_handler as an explicit
    # legacy override and bypasses resolve_vibe_agent_for_context(), so the
    # user's configured default Vibe Agent (and its model/system prompt) would
    # be ignored. Leaving it empty lets the shared resolver pick the default
    # Vibe Agent — including default_agent_name — at dispatch time.

    scope_id = _project_to_scope_id(project_id)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            session = workbench_sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend=agent_backend,
                agent_id=payload.get("agent_id"),
                agent_name=payload.get("agent_name"),
                agent_variant=payload.get("agent_variant"),
                model=payload.get("model"),
                reasoning_effort=payload.get("reasoning_effort"),
                title=payload.get("title"),
                metadata=metadata,
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except PermissionError as err:
        return jsonify({"error": str(err)}), 403
    broker.publish("session.activity", {"session_id": session["id"], "scope_id": session["scope_id"], "event": "created"})
    return jsonify(session), 201


def _session_fork_error_response(err: Exception):
    message = str(err)
    if "id not found" in message:
        return jsonify({"error": message, "code": "session_not_found"}), 404
    if "is archived" in message:
        return jsonify({"error": message, "code": "session_archived"}), 409
    if "no native session id" in message:
        return jsonify({"error": message, "code": "session_not_bound"}), 409
    if "backend cannot be forked" in message:
        return jsonify({"error": message, "code": "session_backend_unsupported"}), 409
    if "backend does not match" in message:
        return jsonify({"error": message, "code": "session_backend_mismatch"}), 409
    return jsonify({"error": message, "code": "session_fork_failed"}), 400


async def _session_turn_state_for_fork(session_id: str) -> dict[str, bool]:
    """Authoritative best-effort live turn check for fork trimming.

    ``agent_status`` can be stale after crashes/restarts. Treat any live
    in-flight turn as a trim candidate; backend-specific reservation code then
    verifies whether a safe native boundary exists before it persists trim
    metadata.
    """

    from vibe import internal_client

    try:
        turn_result = await internal_client.turn_state(session_id)
    except (internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout):
        return {"in_flight": False, "native_turn_started": False}
    body = turn_result.get("body") or {}
    in_flight = bool(body.get("in_flight"))
    native_turn_started = bool(body.get("native_turn_started"))
    return {
        "in_flight": in_flight,
        "native_turn_started": native_turn_started,
        "trim_latest_running_turn": in_flight,
    }


def _reserve_forked_session_for_ui(
    session_id: str,
    *,
    trim_latest_running_turn: bool,
    native_turn_started: bool,
) -> dict:
    from core.services import sessions as workbench_sessions_service
    from core.services import settings as settings_service
    from core.services.session_fork import reserve_forked_session

    title_lang = settings_service.load_config_or_default().language
    result = reserve_forked_session(
        source_session_id=session_id,
        title_lang=title_lang,
        trim_latest_running_turn=trim_latest_running_turn,
        native_turn_started=native_turn_started,
    )
    engine = _projects_engine()
    with engine.connect() as conn:
        return workbench_sessions_service.get_session(conn, result.session_id)


@app.route("/api/sessions/<session_id>/fork", methods=["POST"])
async def sessions_fork(session_id: str):
    from core.services.session_fork import SessionForkError
    from vibe.sse_broker import broker

    try:
        fork_turn_state = await _session_turn_state_for_fork(session_id)
        session = await asyncio.to_thread(
            _reserve_forked_session_for_ui,
            session_id,
            trim_latest_running_turn=bool(fork_turn_state.get("trim_latest_running_turn")),
            native_turn_started=bool(fork_turn_state.get("native_turn_started")),
        )
    except SessionForkError as err:
        return _session_fork_error_response(err)
    except LookupError as err:
        return jsonify({"error": str(err), "code": "session_not_found"}), 404

    broker.publish("session.activity", {"session_id": session["id"], "scope_id": session["scope_id"], "event": "created"})
    return jsonify(session), 201


@app.route("/api/sessions/<session_id>", methods=["GET"])
def sessions_get(session_id: str):
    from core.services import sessions as workbench_sessions_service

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            return jsonify(workbench_sessions_service.get_session(conn, session_id))
    except LookupError as err:
        return jsonify({"error": str(err)}), 404


@app.route("/api/sessions/<session_id>/bootstrap", methods=["GET"])
async def sessions_bootstrap(session_id: str):
    """First-screen payload for the Workbench Chat page.

    This combines the read-only resources ChatPage needs on initial load so a
    remote UI does not pay one tunnel round-trip per independent widget.
    Reconnect/gap recovery still uses the smaller dedicated endpoints so those
    reads can bypass cache precisely.
    """
    from core.services import sessions as workbench_sessions_service
    from core.services import settings as settings_service
    from storage import messages_service
    from vibe import api as vibe_api
    from vibe import internal_client

    engine = _projects_engine()
    with engine.connect() as conn:
        try:
            session = workbench_sessions_service.get_session(conn, session_id)
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        messages_result = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            include_metadata_sources=("show_page",),
            tail=True,
        )
        queued = messages_service.list_queued(conn, session_id)
        draft = messages_service.get_draft(conn, session_id)

    try:
        agents_payload = vibe_api.get_vibe_agents(include_disabled=False)
    except Exception:
        logger.exception("sessions_bootstrap: failed to load Vibe Agents")
        agents_payload = {"agents": [], "default_agent_name": None}

    try:
        config_payload = vibe_api.config_to_payload(settings_service.load_config_or_default())
    except Exception:
        logger.exception("sessions_bootstrap: failed to load config")
        config_payload = None

    try:
        turn_result = await internal_client.turn_state(session_id)
        turn_body = turn_result.get("body") or {}
        turn_state = {"in_flight": bool(turn_body.get("in_flight"))}
    except internal_client.InternalServerUnavailable:
        turn_state = {"in_flight": False}
    except internal_client.InternalServerTimeout:
        turn_state = {"in_flight": None}

    return jsonify(
        {
            "session": session,
            "agents": agents_payload.get("agents") or [],
            "default_agent_name": agents_payload.get("default_agent_name"),
            "config": config_payload,
            "messages": messages_result["messages"],
            "next_after_id": messages_result.get("next_after_id"),
            "next_before_id": messages_result.get("next_before_id"),
            "queued": queued,
            "draft": {"text": (draft or {}).get("text") or ""},
            "turn_state": turn_state,
        }
    )


def _backend_locked_response(err):
    """Shared 409 payload for a rejected cross-backend session change."""
    return (
        jsonify(
            {
                "error": str(err),
                "code": "backend_locked",
                "current_backend": err.current_backend,
                "requested_backend": err.requested_backend,
            }
        ),
        409,
    )


@app.route("/api/sessions/<session_id>", methods=["PATCH"])
async def sessions_update(session_id: str):
    from core.services import sessions as workbench_sessions_service
    from vibe import internal_client
    from vibe.sse_broker import broker

    payload = request.json or {}
    updatable = {
        key: payload[key]
        for key in (
            "title",
            "agent_id",
            "agent_name",
            "agent_backend",
            "agent_variant",
            "model",
            "reasoning_effort",
        )
        if key in payload
    }
    if not updatable:
        return jsonify({"error": "no updatable fields supplied"}), 400

    engine = _projects_engine()
    should_check_backend_lock = "agent_backend" in updatable
    requested_backend = updatable.get("agent_backend")
    if "agent_name" in updatable and "agent_backend" not in updatable:
        try:
            with engine.connect() as conn:
                requested_backend = workbench_sessions_service.derive_backend_for_agent_name(
                    conn,
                    str(updatable.get("agent_name") or ""),
                )
            should_check_backend_lock = True
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
    # The row's ``agent_status`` lags turn acceptance: ``SessionTurnManager.submit``
    # registers the in-flight gate synchronously, but ``running`` is only written
    # once dispatch starts — so a cross-backend switch landing in that startup
    # window would pass the row-status guard and then be silently undone by the
    # bind-time backend backfill. Consult the controller's authoritative in-flight
    # registry first; an unreachable/slow controller falls through to the
    # row-status guard inside ``update_session`` (best effort).
    if should_check_backend_lock:
        try:
            with engine.connect() as conn:
                current = workbench_sessions_service.get_session(conn, session_id)
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        if str(requested_backend or "") != str(current.get("agent_backend") or ""):
            try:
                turn_result = await internal_client.turn_state(session_id)
                in_flight = bool((turn_result.get("body") or {}).get("in_flight"))
            except (internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout):
                in_flight = False
            if in_flight:
                return _backend_locked_response(
                    workbench_sessions_service.SessionBackendLockedError(
                        session_id=session_id,
                        current_backend=current.get("agent_backend"),
                        requested_backend=requested_backend,
                    )
                )

    try:
        with engine.begin() as conn:
            session = workbench_sessions_service.update_session(conn, session_id, **updatable)
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except workbench_sessions_service.SessionBackendLockedError as err:
        # A session is pinned to its backend once it has a conversation (or a
        # running turn); the UI may switch the agent within the same backend,
        # but not across backends.
        return _backend_locked_response(err)
    # Broadcast so other surfaces (e.g. the sidebar session list) reflect the
    # edit live — renaming a session in the chat header should rename its
    # sidebar row without a manual refresh.
    broker.publish(
        "session.activity",
        {
            "session_id": session_id,
            "scope_id": session.get("scope_id"),
            "event": "updated",
            "title": session.get("title"),
        },
    )
    return jsonify(session)


@app.route("/api/sessions/<session_id>/cli-activity", methods=["POST"])
def sessions_cli_activity(session_id: str):
    """Internal: a local CLI (e.g. ``vibe session update``) already wrote the DB in
    its own process, so it can't reach this in-process SSE broker. It pings here and
    we re-read the row and broadcast the SAME ``session.activity`` `updated` event the
    Web PATCH emits, so open surfaces (sidebar title, etc.) reflect the change live
    without a refresh. Authed by the local CLI token (see _is_cli_session_activity_request);
    publish-only — never writes — and never exposed to browsers."""
    if not _is_cli_session_activity_request():
        return jsonify({"error": "forbidden"}), 403
    from core.services import sessions as workbench_sessions_service
    from vibe.sse_broker import broker

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            session = workbench_sessions_service.get_session(conn, session_id)
    except LookupError:
        return jsonify({"error": "not found"}), 404
    broker.publish(
        "session.activity",
        {
            "session_id": session_id,
            "scope_id": session.get("scope_id"),
            "event": "updated",
            "title": session.get("title"),
        },
    )
    return jsonify({"ok": True})


@app.route("/api/sessions/<session_id>/archive-preview", methods=["GET"])
def sessions_archive_preview(session_id: str):
    """Counts of resources archiving this session will permanently reclaim
    (bound tasks/watches + active runs) — powers the irreversible-confirm dialog."""
    from core.services import sessions as workbench_sessions_service

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            workbench_sessions_service.get_session(conn, session_id)  # 404 if missing
            counts = workbench_sessions_service.count_bound_resources(conn, session_id)
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    return jsonify(counts)


async def _archive_cancel_turn(session_id: str) -> None:
    """Best-effort, background cancel of an in-flight turn for a just-archived
    session — kept off the archive request path so a slow/refused backend
    interrupt never delays the response or broadcast."""
    from vibe import internal_client

    try:
        await internal_client.cancel_dispatch(session_id)
    except internal_client.InternalServerUnavailable:
        pass
    except Exception:
        logger.debug("archive: cancel in-flight turn failed for %s", session_id, exc_info=True)


async def _archive_release_vault_scopes(session_id: str, revoked_vault_scopes: list[dict[str, str]]) -> None:
    from vibe import api

    try:
        await asyncio.to_thread(
            api.release_vault_agent_scopes,
            revoked_vault_scopes,
            reason=f"archive_session:{session_id}",
        )
    except Exception:
        logger.debug("archive: resident-agent grant release failed for %s", session_id, exc_info=True)


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
async def sessions_archive(session_id: str):
    """Permanently archive a session and reclaim its bound resources.

    The DB-level teardown (status, tasks/watches, runs, Show Page) is atomic in
    ``archive_session``. Cancelling an in-flight chat turn lives in the controller
    process, so we fire it best-effort in the BACKGROUND after the commit — the
    session is already archived + guarded, so a turn that slips through just
    writes into hidden history rather than re-surfacing the session.
    """
    from core.services import sessions as workbench_sessions_service
    from vibe.sse_broker import broker

    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            session = workbench_sessions_service.archive_session(conn, session_id)
    except LookupError as err:
        return jsonify({"error": str(err)}), 404

    revoked_vault_scopes = session.pop("revoked_vault_grant_scopes", [])

    # Broadcast + return immediately — the archive is already committed. Other
    # mounted clients (sidebars, tabs) drop the row live and leave the chat if
    # they're viewing it (mirrors the rename 'updated' event).
    broker.publish(
        "session.activity",
        {"session_id": session_id, "scope_id": session.get("scope_id"), "event": "archived"},
    )

    # Fire-and-forget the in-flight-turn cancel: the cancel client waits up to 30s
    # for the backend interrupt, so awaiting it here would hang the confirm dialog
    # and delay the broadcast for a teardown that has already committed.
    loop = asyncio.get_running_loop()
    if revoked_vault_scopes:
        loop.create_task(_archive_release_vault_scopes(session_id, revoked_vault_scopes))
    loop.create_task(_archive_cancel_turn(session_id))

    return jsonify(session)


@app.route("/api/sessions/<session_id>/messages", methods=["GET"])
def sessions_messages_list(session_id: str):
    from core.services import sessions as workbench_sessions_service
    from storage import messages_service

    after_id = request.args.get("after_id") or None
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    before_id = request.args.get("before_id") or None
    # ``around_id`` centers the window on a specific message (search deep-link
    # jump); it takes precedence over after/before/tail in the service.
    around_id = request.args.get("around_id") or None
    # ``tail=1`` returns the most-recent window (for the Chat page's gap recovery)
    # instead of the oldest page.
    tail = request.args.get("tail") == "1"

    engine = _projects_engine()
    with engine.connect() as conn:
        try:
            workbench_sessions_service.get_session(conn, session_id)
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        # Chat transcript = the dialogue + turn-terminal markers. avibe turns
        # persist intermediate assistant / tool_call rows (unified store) that we
        # keep OUT of the conversation view, but ``notify`` rows are kept: a
        # terminal notify (e.g. an agent run that failed and stopped without a
        # result) marks the end of that turn and must stay visible. Show-Page
        # transcript marks (metadata.source='show_page') are kept regardless of
        # type.
        result = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            after_id=after_id,
            before_id=before_id,
            around_id=around_id,
            limit=limit,
            types=messages_service.TRANSCRIPT_TYPES,
            include_metadata_sources=("show_page",),
            tail=tail,
        )
    return jsonify(result)


@app.route("/api/search/messages", methods=["GET"])
def search_messages_list():
    """Global message-content search across Workbench sessions, grouped by session.

    Substring (case-insensitive) search over ``content_text`` for ``platform
    ='avibe'`` user prompts + agent ``result`` replies, excluding archived
    sessions. ``q`` is the query, ``limit`` caps the matched-message scan. The
    remote-access host guard + auth run in the global ``before_request`` hooks
    (same as the messages list), so this handler just delegates to the service.
    """
    from storage import messages_service

    query = request.args.get("q") or ""
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50

    engine = _projects_engine()
    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query=query, limit=limit)
    return jsonify(result)


# =============================================================================
# Workbench: File Browser
# =============================================================================


def _file_browser_error_response(exc: Exception):
    from core.file_browser_service import FileBrowserError

    if isinstance(exc, FileBrowserError):
        return jsonify({"ok": False, "error": {"code": exc.code, "message": exc.message}}), exc.status_code
    logger.exception("file browser request failed")
    return jsonify({"ok": False, "error": {"code": "internal_error", "message": "Internal server error"}}), 500


async def _dispatch_native_ui_request(starlette_request: FastAPIRequest, handler: Callable[[], Any]):
    return await app.dispatch_native_request(starlette_request, handler)


@app.get("/api/files/list", include_in_schema=False)
async def files_list(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.list_directory,
                    request.args.get("path") or "",
                    show_hidden=request.args.get("show_hidden") == "1",
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.get("/api/files/meta", include_in_schema=False)
async def files_meta(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        try:
            return jsonify(await asyncio.to_thread(file_browser_service.metadata, request.args.get("path") or ""))
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.get("/api/files/content", include_in_schema=False)
async def files_content(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        try:
            content = await asyncio.to_thread(
                file_browser_service.file_content,
                request.args.get("path") or "",
                download=request.args.get("download") == "1",
            )
        except Exception as exc:
            return _file_browser_error_response(exc)
        return FastAPIResponse(
            content=content.data,
            media_type=content.mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "private, no-store",
                "Content-Disposition": f"{content.disposition}; filename*=UTF-8''{quote(content.path.name)}",
            },
        )

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.put("/api/files/write", include_in_schema=False)
async def files_write(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.write_file,
                    payload.get("path") or "",
                    payload.get("content"),
                    expected_mtime=payload.get("expected_mtime"),
                    create_only=_parse_explicit_bool(payload.get("create_only")),
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.post("/api/files/mkdir", include_in_schema=False)
async def files_mkdir(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(await asyncio.to_thread(file_browser_service.make_directory, payload.get("path") or ""))
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.post("/api/files/rename", include_in_schema=False)
async def files_rename(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.rename_path,
                    payload.get("path") or "",
                    payload.get("new_name") or "",
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.post("/api/files/move", include_in_schema=False)
async def files_move(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.move_path,
                    payload.get("src") or "",
                    payload.get("dst") or "",
                    overwrite=_parse_explicit_bool(payload.get("overwrite")),
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.post("/api/files/delete", include_in_schema=False)
async def files_delete(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.delete_path,
                    payload.get("path") or "",
                    recursive=_parse_explicit_bool(payload.get("recursive")),
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.get("/api/files/search", include_in_schema=False)
async def files_search(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        args = request.args
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.search,
                    args.get("root") or "",
                    args.get("query") or "",
                    regex=args.get("regex") == "1",
                    case_sensitive=args.get("case") == "1",
                    whole_word=args.get("word") == "1",
                    include=args.get("include") or "",
                    exclude=args.get("exclude") or "",
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.post("/api/files/search/replace", include_in_schema=False)
async def files_search_replace(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.replace,
                    payload.get("root") or "",
                    payload.get("query") or "",
                    payload.get("replacement") or "",
                    regex=_parse_explicit_bool(payload.get("regex")),
                    case_sensitive=_parse_explicit_bool(payload.get("case")),
                    whole_word=_parse_explicit_bool(payload.get("word")),
                    include=payload.get("include") or "",
                    exclude=payload.get("exclude") or "",
                    paths=payload.get("paths"),
                    expected_mtimes=payload.get("expected_mtimes"),
                )
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.post("/api/files/search/undo", include_in_schema=False)
async def files_search_undo(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(await asyncio.to_thread(file_browser_service.undo_replace, payload.get("token") or ""))
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


# Content types the media proxy is willing to serve ``inline``. Anything else —
# text/html, image/svg+xml, xml, application/octet-stream, unknown — is forced to
# ``attachment`` so a preview-open of agent-produced ACTIVE content can't execute
# script on the UI origin (``nosniff`` doesn't help when the type IS active).
# ``<img>`` ignores Content-Disposition, so inline image rendering still works.
_INLINE_SAFE_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/bmp",
    "image/x-icon",
    "image/heic",
    "image/heif",
    "application/pdf",
    "text/plain",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/flac",
    "audio/x-m4a",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
}


@app.route("/api/media/<token>", methods=["GET"])
def media_get(token: str):
    """Serve a registered chat-media file (agent reply / upload) by opaque token.

    The token — not a path, not a session — is the capability: only files we
    minted into ``media_objects`` are reachable, and the same token resolves to
    one stable URL the browser can cache across messages/sessions. Lives under
    ``/api/*`` so the remote-access auth middleware already gates it, and a
    same-origin ``<img>`` / anchor GET carries the session cookie. Defaults to
    ``inline`` (so images render in ``<img>`` and PDFs preview); ``?download=1``
    forces an attachment download.
    """
    from urllib.parse import quote

    from storage import media_service

    engine = _projects_engine()
    with engine.connect() as conn:
        row = media_service.get_by_token(conn, token)
    if not row or row.get("revoked_at"):
        return jsonify({"error": "not_found"}), 404
    stored = row["local_path"]
    try:
        candidate = Path(stored).resolve(strict=True)
    except (OSError, ValueError):
        return jsonify({"error": "not_found"}), 404
    # Re-validate at serve time: ``stored`` is the canonical (symlink-free) path
    # captured at registration. If it now resolves elsewhere — a symlink swapped
    # in to escape to e.g. ~/.vibe_remote/config.json — or is no longer a regular
    # file, refuse (closes the mint→click TOCTOU window).
    if str(candidate) != stored or not candidate.is_file():
        return jsonify({"error": "not_found"}), 404
    mime_type = row.get("content_type") or mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
    response = send_file(candidate, mimetype=mime_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Cache for a bounded window so re-renders / scrolling / re-opening the chat
    # reuse the bytes instead of re-fetching — but do NOT promise immutability: a
    # token maps to a MUTABLE ``local_path`` (an agent can overwrite a file in
    # place), so an eternal ``immutable`` cache could pin stale bytes in one
    # client while another reads new bytes from disk. Cheap revalidation isn't an
    # option here — Starlette's FileResponse doesn't emit 304s (verified; it
    # re-sends 200), so ``must-revalidate`` would force a full re-download every
    # time and reintroduce the very re-fetch this avoids. A short max-age is the
    # balance: no re-fetch during active use, and any stale/split window is
    # bounded to an hour, after which the next fetch re-reads the current file.
    # ``private`` because the file is auth-gated (served only to its user).
    response.headers["Cache-Control"] = "private, max-age=3600"
    filename = row.get("file_name") or candidate.name
    # Force download for non-allowlisted (active) types even without ?download=1,
    # so previewing an agent-produced HTML/SVG can't run script on this origin.
    base_ct = mime_type.split(";", 1)[0].strip().lower()
    force_download = request.args.get("download") == "1" or base_ct not in _INLINE_SAFE_MEDIA_TYPES
    disposition = "attachment" if force_download else "inline"
    response.headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename)}"
    return response


@app.route("/api/media/<token>/meta", methods=["GET"])
def media_meta(token: str):
    """Lightweight metadata for a media token so the UI file card can show the
    name / type / size without downloading the file. Same token gate as the
    file route."""
    from storage import media_service

    engine = _projects_engine()
    with engine.connect() as conn:
        row = media_service.get_by_token(conn, token)
    if not row or row.get("revoked_at"):
        return jsonify({"error": "not_found"}), 404
    return jsonify(
        {
            "kind": row.get("kind"),
            "name": row.get("file_name"),
            "content_type": row.get("content_type"),
            "ext": row.get("file_ext"),
            "size": row.get("size_bytes"),
            "width": row.get("width_px"),
            "height": row.get("height_px"),
        }
    )


@app.route("/api/sessions/<session_id>/attachments", methods=["POST"])
def sessions_attachments_create(session_id: str):
    """Persist a user-uploaded file (base64 JSON) and register it for the media
    proxy. Returns an opaque token + proxy URL; the browser never holds a path.
    base64-over-JSON keeps uploads on the existing auth + CSRF-guarded compat
    route (the compat layer parses JSON, not multipart)."""
    import base64
    import re
    import uuid

    from config import paths
    from core.services import sessions as workbench_sessions_service
    from storage import media_service

    payload = request.json or {}
    name = (payload.get("name") or "upload").strip() or "upload"
    mime = (payload.get("mime") or payload.get("content_type") or "application/octet-stream").strip()
    data_b64 = payload.get("data") or ""
    if not isinstance(data_b64, str) or not data_b64:
        return jsonify({"error": "data is required"}), 400
    if data_b64.startswith("data:") and "," in data_b64:
        data_b64 = data_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return jsonify({"error": "invalid base64"}), 400
    if not raw:
        return jsonify({"error": "empty file"}), 400
    if len(raw) > 25 * 1024 * 1024:
        return jsonify({"error": "file too large"}), 413

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            session = workbench_sessions_service.get_session(conn, session_id)
    except LookupError:
        return jsonify({"error": "session_not_found"}), 404

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.rsplit("/", 1)[-1]).strip("_") or "upload"
    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    local_path = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    local_path.write_bytes(raw)

    kind = "image" if mime.startswith("image/") else "file"
    with engine.begin() as conn:
        token = media_service.register(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            kind=kind,
            source="user_upload",
            local_path=str(local_path.resolve()),
            file_name=name,
            content_type=mime,
        )
        # Read back the pixel dimensions register() captured (NULL for non-images
        # / unreadable) so the client can reserve the image box and never shift the
        # transcript when the upload renders.
        row = media_service.get_by_token(conn, token)
    return (
        jsonify(
            {
                "token": token,
                "name": name,
                "mime": mime,
                "size": len(raw),
                "kind": kind,
                "url": f"/api/media/{token}",
                "width": row.get("width_px") if row else None,
                "height": row.get("height_px") if row else None,
            }
        ),
        201,
    )


@app.route("/api/asr/transcribe", methods=["POST"])
async def asr_transcribe():
    """Transcribe recorded audio (base64 JSON) via the avibe.bot ASR client and
    return the text for the composer to fill in. Reuses ``AudioAsrService`` — the
    same client the IM voice-note path uses — so it needs only a V2Config."""
    import base64
    import tempfile
    import uuid

    from core.audio_asr import AudioAsrService
    from core.services import settings as settings_service
    from modules.im.base import FileAttachment

    payload = request.json or {}
    data_b64 = payload.get("data") or ""
    if not isinstance(data_b64, str) or not data_b64:
        return jsonify({"error": "data is required"}), 400
    if data_b64.startswith("data:") and "," in data_b64:
        data_b64 = data_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return jsonify({"error": "invalid base64"}), 400
    if not raw:
        return jsonify({"error": "empty audio"}), 400
    if len(raw) > 25 * 1024 * 1024:
        return jsonify({"error": "file too large"}), 413

    name = (payload.get("name") or "voice.webm").strip() or "voice.webm"
    mime = (payload.get("mime") or "audio/webm").strip()

    try:
        config = settings_service.load_config()
    except Exception:
        logger.warning("asr_transcribe: failed to load config", exc_info=True)
        return jsonify({"error": "config_unavailable"}), 503
    service = AudioAsrService(config)
    if not service.is_available():
        return jsonify({"error": "asr_unavailable"}), 400

    suffix = Path(name).suffix or ".webm"
    tmp_path = Path(tempfile.gettempdir()) / f"vibe_asr_{uuid.uuid4().hex[:8]}{suffix}"
    tmp_path.write_bytes(raw)
    try:
        attachment = FileAttachment(name=name, mimetype=mime, local_path=str(tmp_path), size=len(raw))
        transcripts = await service.transcribe_attachments([attachment])
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    if not transcripts:
        return jsonify({"error": "transcription_failed"}), 502
    return jsonify({"text": transcripts[0].text})


@app.route("/api/asr/status", methods=["GET"])
def asr_status():
    """Whether voice transcription is available (Vibe Cloud paired + enabled) so
    the composer can show/hide the mic button instead of guessing."""
    from core.audio_asr import AudioAsrService
    from core.services import settings as settings_service

    try:
        config = settings_service.load_config()
        return jsonify({"available": bool(AudioAsrService(config).is_available())})
    except Exception:
        return jsonify({"available": False})


@app.route("/api/sessions/<session_id>/messages", methods=["POST"])
async def sessions_messages_create(session_id: str):
    """Persist a user message and fire-and-forget the agent turn.

    Reserves the user's row, then asks the controller to start the turn
    (``/internal/dispatch_async``, 202). The agent's reply — and any
    notify/result — arrives over the persistent ``message.new`` session
    stream, not this response, so the HTTP request returns immediately and a
    closed browser tab can't cancel an in-flight turn. The controller
    atomically either starts the turn (we then promote the row to ``user``)
    or, when a turn is already running, promotes it to ``queued`` itself
    (send-while-busy). The legacy per-turn ``?stream=1`` SSE proxy was retired
    in Step 6 — the session-scoped stream replaced it.
    """

    from core.services import sessions as workbench_sessions_service
    from storage import messages_service
    from vibe import internal_client
    from vibe.sse_broker import broker

    payload = request.json or {}
    text = payload.get("text")
    content = payload.get("content")
    if text is None and not content:
        return jsonify({"error": "text or content is required"}), 400
    # A quick-reply click tags the row with the agent message it answers.
    quick_reply_for = (payload.get("metadata") or {}).get("quick_reply_for")
    web_push_user_key = _web_push_user_key()

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            session = workbench_sessions_service.get_session(conn, session_id)
            # Archived sessions are terminal + inert: refuse to start a turn on one
            # even via a stale/direct request (the workbench hides them from the
            # list, so this only fires on a leftover tab or a hand-crafted call).
            if session.get("status") == "archived":
                return jsonify({"error": "session is archived", "code": "session_archived"}), 409
            # Idempotency: a stale or duplicate quick-reply submit (a second tab, or
            # one that missed the message.new event) must not start a second turn
            # for an already-answered group. The answer lives on the agent message.
            if (
                quick_reply_for
                and messages_service.get_quick_reply_chosen(conn, session_id, quick_reply_for) is not None
            ):
                return jsonify({"already_answered": True}), 200
    except LookupError as err:
        return jsonify({"error": str(err)}), 404

    dispatch_text = (
        (text if isinstance(text, str) else None)
        or (content.get("text") if isinstance(content, dict) else None)
        or ""
    )

    # Resolve uploaded-attachment refs (media tokens the browser holds) to local
    # file specs the agent turn can read. Done here (not in the browser) so a
    # filesystem path never leaves the server.
    attachment_specs: list = []
    raw_attachments = content.get("attachments") if isinstance(content, dict) else None
    if raw_attachments:
        from core.workbench_media import resolve_attachment_specs

        with engine.connect() as conn:
            attachment_specs = resolve_attachment_specs(
                conn, session_id=session_id, attachments=raw_attachments
            )

    def _persist_user_row() -> dict | None:
        """Reserve the user's row as ``pending`` (hidden from transcript/queue/
        inbox) + clear any saved draft, WITHOUT publishing. This locks the row's
        ``(created_at, id)`` BEFORE the turn dispatches (so a fast reply can't
        sort ahead of its prompt) yet keeps it invisible during the dispatch
        window, so another tab can't briefly see it as a sent prompt (Codex P2).
        The caller promotes it (→ user / queued) once the outcome is known.
        Returns ``None`` if the session was archived in the meantime."""
        with engine.begin() as conn:
            # Re-check archive ATOMICALLY with the reservation: a concurrent archive
            # may have committed since the pre-flight check above, and the session
            # must stay terminal — no new row, no turn.
            if workbench_sessions_service.is_session_archived(conn, session_id):
                return None
            row = messages_service.append(
                conn,
                scope_id=session["scope_id"],
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                message_type=messages_service.PENDING_TYPE,
                text=text if isinstance(text, str) else None,
                content=content if isinstance(content, dict) else None,
                metadata={
                    **(payload.get("metadata") or {}),
                    "_web_push_user_key": web_push_user_key,
                },
                author_id=web_push_user_key,
                author_name=payload.get("author_name"),
            )
            # A quick-reply click is a side action, not the user submitting their
            # composer text — keep any saved draft intact for it.
            if not quick_reply_for:
                messages_service.clear_draft(conn, session_id)
            workbench_sessions_service.touch_session(conn, session_id)
        return row

    def _promote_and_publish(row: dict) -> dict:
        """Promote the reserved pending row to a transcript-visible ``user`` row
        and fan it out (message.new + activity + inbox bump). Returns the row
        with its type corrected. The agent-reply side rides the controller→
        browser bridge, but the user row is persisted in this UI process so the
        controller bus never sees it."""
        with engine.begin() as conn:
            promoted = messages_service.promote_pending(conn, row["id"], "user")
        if not promoted:
            # The row wasn't pending anymore: the controller already promoted it
            # (e.g. enqueued as 'queued' via the busy-session path) before our
            # dispatch call failed/returned. Don't publish a phantom 'user'
            # transcript row alongside the still-queued item — nudge the queue view
            # and report it as queued instead (Codex P2).
            broker.publish("queue.updated", {"session_id": session_id, "scope_id": session["scope_id"]})
            return {**row, "type": "queued"}
        row = {**row, "type": "user"}
        broker.publish("message.new", row)
        broker.publish(
            "session.activity",
            {"session_id": session_id, "scope_id": session["scope_id"], "event": "user_message"},
        )
        try:
            with engine.connect() as conn:
                inbox_row = messages_service.get_inbox_session(conn, session_id, platform="avibe")
            if inbox_row is not None:
                broker.publish("inbox.session.updated", inbox_row)
        except Exception:
            logger.debug("inbox.session.updated publish (user message) failed", exc_info=True)
        return row

    # Reserve the row FIRST (pending), then decide by the dispatch outcome.
    message = _persist_user_row()
    if message is None:
        # Archived between the pre-flight check and the reservation — stay terminal.
        return jsonify({"error": "session is archived", "code": "session_archived"}), 409
    # No text AND no attachments: nothing for the agent to act on, so just
    # promote + publish the row, no turn. Attachments WITHOUT text still run a
    # turn (the agent reads the files), so they aren't caught here.
    if not dispatch_text.strip() and not attachment_specs:
        return jsonify(_promote_and_publish(message)), 201
    # Session/page-scoped model (the web Chat): fire-and-forget the turn; the
    # reply arrives over ``message.new``. The controller atomically either lets
    # the turn start (we then promote the row to user) or — if a turn is already
    # running — promotes this row to queued itself (send-while-busy), so we never
    # write a second row and there's no enqueue/flush race and no transcript flash.
    dispatch_payload = {
        "session_id": session_id,
        "text": dispatch_text,
        "scope_id": session["scope_id"],
        "user_message_id": message.get("id"),
        "files": attachment_specs,
    }
    try:
        result = await internal_client.dispatch_async(dispatch_payload)
    except internal_client.InternalServerUnavailable as exc:
        # Couldn't reach the controller — promote + surface the row so the
        # user still sees their message, plus the failure.
        published = _promote_and_publish(message)
        return jsonify({**published, "dispatch_error": "internal_unavailable", "detail": str(exc)}), 502
    except Exception as exc:
        # The socket existed but the call failed another way (ReadTimeout, a
        # non-JSON / 500 response, etc.). The row is still reserved as hidden
        # ``pending`` and the draft was cleared, so WITHOUT this the user's text
        # would vanish from both transcript and queue behind an error. Promote +
        # publish it with the error, same as the unavailable branch (Codex P2).
        logger.warning("dispatch_async call failed for session %s: %s", session_id, exc, exc_info=True)
        published = _promote_and_publish(message)
        return jsonify({**published, "dispatch_error": "dispatch_failed", "detail": str(exc)}), 502
    status = result.get("status_code", 500)
    body = result.get("body") or {}
    # Quick-reply accepted (turn started OR queued) → record the choice on the
    # AGENT message as the single source of truth for the locked/answered state.
    # Only on success, so a failed click stays retriable; ``set_quick_reply_chosen``
    # is set-once, so a rare double-dispatch still records one consistent answer.
    if status == 202 and quick_reply_for:
        with engine.begin() as conn:
            messages_service.set_quick_reply_chosen(conn, session_id, quick_reply_for, dispatch_text)
    if status == 202 and body.get("queued"):
        # Enqueued behind a running turn: the controller already promoted the
        # row pending→queued, so it stays OUT of the transcript (no
        # message.new); show it above the composer via queue.updated.
        broker.publish("queue.updated", {"session_id": session_id, "scope_id": session["scope_id"]})
        return jsonify({**message, "type": "queued", "queued": True}), 202
    if status == 202:
        # Turn started — promote + publish the prompt.
        return jsonify(_promote_and_publish(message)), 201
    # Dispatch failed: still promote + show the row + the error.
    published = _promote_and_publish(message)
    return jsonify({**published, "dispatch_error": "dispatch_failed", "detail": body}), 502


@app.route("/api/sessions/<session_id>/cancel", methods=["POST"])
async def sessions_cancel(session_id: str):
    """Stop an in-flight ``dispatch_turn`` for this session.

    Proxies to ``POST /internal/cancel/<session_id>`` on the controller's
    Unix socket. Falls back to a 503 if the socket is unreachable so
    the UI can show a sensible "cannot stop right now" state instead
    of pretending the cancel succeeded.
    """

    from vibe import internal_client

    try:
        result = await internal_client.cancel_dispatch(session_id)
    except internal_client.InternalServerUnavailable as exc:
        return jsonify({"ok": False, "code": "internal_unavailable", "detail": str(exc)}), 503
    status = result.get("status_code", 500)
    body = result.get("body") or {}
    body.setdefault("ok", status == 200)
    if status == 404 and body.get("code") == "not_in_flight":
        body["recovered_agent_status"] = _recover_stale_session_status(session_id)
    elif status == 200 and body.get("status") == "stale_released":
        body["recovered_agent_status"] = _recover_stale_session_status(session_id)
    return jsonify(body), status


@app.route("/api/sessions/<session_id>/mark-read", methods=["POST"])
def sessions_mark_read(session_id: str):
    from core.services import sessions as workbench_sessions_service
    from storage import messages_service
    from vibe.sse_broker import broker

    payload = request.json or {}
    until_message_id = payload.get("until_message_id")

    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            session = workbench_sessions_service.get_session(conn, session_id)
            updated = messages_service.mark_session_read(
                conn, session_id, until_message_id=until_message_id
            )
            unread_counts = messages_service.unread_counts(conn, platform="avibe")
            unread_by_session = messages_service.unread_counts_by_session(conn, platform="avibe")
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    if updated:
        broker.publish(
            "inbox.unread.changed",
            {
                "session_id": session_id,
                "scope_id": session["scope_id"],
                "delta": -updated,
                "unread_counts": unread_counts,
                "unread_by_session": unread_by_session,
            },
        )
    return jsonify(
        {
            "updated": updated,
            "unread_counts": unread_counts,
            "unread_by_session": unread_by_session,
        }
    )


@app.route("/api/sessions/<session_id>/turn-state", methods=["GET"])
async def sessions_turn_state(session_id: str):
    """Whether a turn is currently in flight (so a freshly loaded / reconnected
    Chat page can restore its Stop/working state). Degrades to idle if the
    controller socket is unreachable."""
    from vibe import internal_client

    try:
        result = await internal_client.turn_state(session_id)
    except internal_client.InternalServerUnavailable:
        return jsonify({"in_flight": False})
    except internal_client.InternalServerTimeout:
        return (
            jsonify(
                {
                    "error": {
                        "code": "turn_state_timeout",
                        "message": "Turn state probe timed out",
                    },
                }
            ),
            504,
        )
    body = result.get("body") or {}
    in_flight = bool(body.get("in_flight"))
    recovered = False
    if not in_flight:
        recovered = _recover_stale_session_status(session_id)
    return jsonify({"in_flight": in_flight, "recovered_agent_status": recovered})


@app.route("/api/sessions/<session_id>/queue", methods=["GET"])
def sessions_queue_list(session_id: str):
    """Pending send-while-busy messages for a session (shown above the composer)."""
    from storage import messages_service

    engine = _projects_engine()
    with engine.connect() as conn:
        queued = messages_service.list_queued(conn, session_id)
    return jsonify({"queued": queued})


@app.route("/api/sessions/<session_id>/queue/<message_id>", methods=["DELETE"])
def sessions_queue_remove(session_id: str, message_id: str):
    """Drop one queued message (the per-item delete in the queue strip)."""
    from storage import messages_service
    from vibe.sse_broker import broker

    engine = _projects_engine()
    with engine.begin() as conn:
        removed = messages_service.remove_queued(conn, session_id, message_id)
    if removed:
        broker.publish("queue.updated", {"session_id": session_id})
    return jsonify({"removed": bool(removed)})


@app.route("/api/sessions/<session_id>/queue/<message_id>/send-now", methods=["POST"])
async def sessions_queue_send_now(session_id: str, message_id: str):
    """Run the queue now ("立即发送"): interrupt the running turn + flush. The
    queue flushes as one merged turn, so ``message_id`` identifies the button's
    item but the whole queue runs (the merge is the user's chosen behavior)."""
    from vibe import internal_client

    try:
        result = await internal_client.send_now(session_id)
    except internal_client.InternalServerUnavailable as exc:
        return jsonify({"ok": False, "code": "internal_unavailable", "detail": str(exc)}), 503
    status = result.get("status_code", 500)
    body = result.get("body") or {}
    body.setdefault("ok", status < 400)
    return jsonify(body), status


@app.route("/api/sessions/<session_id>/draft", methods=["GET"])
def sessions_draft_get(session_id: str):
    """The session's saved unsent compose text (restored on open / device switch)."""
    from storage import messages_service

    engine = _projects_engine()
    with engine.connect() as conn:
        draft = messages_service.get_draft(conn, session_id)
    return jsonify({"text": (draft or {}).get("text") or ""})


@app.route("/api/sessions/<session_id>/draft", methods=["PUT"])
def sessions_draft_set(session_id: str):
    """Upsert the session's draft (debounced from the composer). Blank clears it."""
    from core.services import sessions as workbench_sessions_service
    from storage import messages_service

    payload = request.json or {}
    text = payload.get("text")
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            session = workbench_sessions_service.get_session(conn, session_id)
            # Archive is terminal: drop a late/debounced draft save (e.g. the
            # composer flushing as it unmounts right after archive) so it can't
            # recreate a draft on a session whose drafts were just reclaimed.
            if session.get("status") == "archived":
                return jsonify({"ok": True})
            messages_service.set_draft(
                conn, scope_id=session["scope_id"], session_id=session_id, text=text if isinstance(text, str) else None
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    return jsonify({"ok": True})


@app.route("/api/events", methods=["GET"])
async def workbench_events():
    """Server-Sent Events stream for the workbench.

    Browsers open this once and keep it open; the route streams JSON
    events (message.new, session.activity, inbox.unread.changed) as
    they happen elsewhere in the app, plus a 15-second keep-alive
    comment line so Cloudflare-style proxies don't kill the idle TCP
    connection.

    Native FastAPI ``StreamingResponse`` so the loop stays async and
    each browser only costs one task, not one OS thread.
    """

    import asyncio

    from fastapi.responses import StreamingResponse

    from vibe.sse_broker import broker

    async def generate():
        sub_id, queue = broker.subscribe()
        try:
            # First chunk = handshake + sub_id so the client can include it in
            # subsequent debug logs / cancel calls if we ever need them.
            yield ": stream connected\n\n"
            yield f"event: connected\ndata: {{\"sub_id\": {sub_id}}}\n\n"
            while True:
                try:
                    event_type, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    # 15s keep-alive — Cloudflare Tunnel default idle is well
                    # below 100s but this still keeps mid-tier proxies happy.
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            broker.unsubscribe(sub_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            # Disable nginx/cloudflare body buffering on the response side
            # so chunks reach the client immediately.
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/inbox", methods=["GET"])
def inbox_list():
    """Per-session ("Slack-like") inbox feed: one row per conversation, newest
    activity first. Defaults to avibe-only per workbench scope."""

    from storage import messages_service

    platform = request.args.get("platform") or "avibe"
    scope_filter = platform if platform != "all" else None
    unread_only = request.args.get("unread_only") in {"1", "true", "yes"}
    try:
        limit = int(request.args.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    before = request.args.get("before") or None

    engine = _projects_engine()
    with engine.connect() as conn:
        result = messages_service.list_inbox_sessions(
            conn,
            platform=scope_filter,
            unread_only=unread_only,
            limit=limit,
            before=before,
        )
        # Pagination-independent unread map for the sidebar badges (a session
        # with unread may sit past the first inbox page) + header totals.
        per_session = messages_service.unread_counts_by_session(conn, platform=scope_filter)
        result["unread_by_session"] = per_session
        result["unread_total"] = sum(per_session.values())
        result["unread_sessions"] = len(per_session)
    return jsonify(result)


# =============================================================================
# Harness Endpoints (read-only v1)
# =============================================================================
#
# Workbench Harness page reads scheduled tasks, watches, and agent runs out
# of the same SQLite store the scheduler writes to. Mutations (delete /
# cancel / pause-resume) need to talk to the live ScheduledTaskService and
# WatchSupervisor so the in-memory schedule stays consistent — that wiring
# lands in a follow-up commit.


@contextmanager
def _harness_store():
    # ``SQLiteBackgroundTaskStore`` opens a dedicated ``SqliteInvalidationProbe``
    # connection in __init__ that only closes when ``store.close()`` is
    # called. Harness routes are polled frequently from the workbench UI,
    # so leaking a connection per request exhausts the SQLite pool. The
    # context manager makes ownership explicit at every call site.
    from storage.background import SQLiteBackgroundTaskStore

    store = SQLiteBackgroundTaskStore()
    try:
        yield store
    finally:
        store.close()


def _harness_page_request(default_limit: int = 30):
    from storage.pagination import make_page_request

    try:
        limit = int(request.args.get("limit") or default_limit)
        page = int(request.args.get("page") or 1)
        return make_page_request(page=page, limit=limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _harness_status_filter() -> str:
    status = request.args.get("status") or "all"
    if status not in {"all", "enabled", "disabled"}:
        raise ValueError("status must be one of: all, enabled, disabled")
    return status


def _harness_query_filter() -> str | None:
    query = (request.args.get("query") or "").strip()
    return query or None


def _harness_has_list_params() -> bool:
    return any(key in request.args for key in ("page", "limit", "status", "query"))


def _harness_page_payload(page_result, *, items_key: str, counts: dict[str, int]) -> dict[str, Any]:
    return _harness_page_payload_for_status(
        page_result,
        items_key=items_key,
        counts=counts,
        status=request.args.get("status") or "all",
    )


def _harness_page_payload_for_status(page_result, *, items_key: str, counts: dict[str, int], status: str) -> dict[str, Any]:
    total = int(counts.get(status or "all", 0))
    return {
        items_key: page_result.items,
        "counts": counts,
        "total": total,
        "page": page_result.page,
        "limit": page_result.limit,
        "has_more": page_result.has_more,
    }


@app.route("/api/harness/counts", methods=["GET"])
def harness_counts():
    with _harness_store() as store:
        return jsonify(
            {
                "tasks": store.count_scheduled_tasks(),
                "watches": store.count_watches(),
                "runs": store.count_runs_by_status(),
            }
        )


@app.route("/api/harness/tasks", methods=["GET"])
def harness_tasks_list():
    if not _harness_has_list_params():
        with _harness_store() as store:
            tasks = store.list_scheduled_tasks()
            counts = store.count_scheduled_tasks()
        return jsonify(
            {
                "tasks": tasks,
                "counts": counts,
                "total": counts["all"],
                "page": 1,
                "limit": len(tasks),
                "has_more": False,
            }
        )
    try:
        page_request = _harness_page_request()
        status = _harness_status_filter()
        query = _harness_query_filter()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400
    with _harness_store() as store:
        page_result = store.list_scheduled_tasks_page(
            status=status,
            query=query,
            page_request=page_request,
            newest_first=True,
        )
        counts = store.count_scheduled_tasks(query=query)
    return jsonify(_harness_page_payload(page_result, items_key="tasks", counts=counts))


@app.route("/api/harness/tasks/<task_id>", methods=["PATCH"])
def harness_task_patch(task_id: str):
    payload = request.json or {}
    if "enabled" not in payload:
        return jsonify({"ok": False, "code": "invalid_payload", "message": "missing 'enabled'"}), 400
    enabled = bool(payload["enabled"])
    with _harness_store() as store:
        if not store.get_scheduled_task(task_id):
            return jsonify({"ok": False, "code": "task_not_found"}), 404
        store.set_definition_enabled(task_id, enabled, definition_type="scheduled")
        task = store.get_scheduled_task(task_id)
    return jsonify({"ok": True, "task": task})


@app.route("/api/harness/tasks/<task_id>", methods=["DELETE"])
def harness_task_delete(task_id: str):
    with _harness_store() as store:
        if not store.get_scheduled_task(task_id):
            return jsonify({"ok": False, "code": "task_not_found"}), 404
        store.remove_task(task_id)
    return jsonify({"ok": True, "id": task_id})


@app.route("/api/harness/watches", methods=["GET"])
def harness_watches_list():
    if not _harness_has_list_params():
        with _harness_store() as store:
            watches = store.list_watches()
            counts = store.count_watches()
            runtime = store.load_watch_runtime().get("watches") or {}
        for watch in watches:
            watch["runtime"] = runtime.get(watch["id"]) or {"running": False}
        return jsonify(
            {
                "watches": watches,
                "counts": counts,
                "total": counts["all"],
                "page": 1,
                "limit": len(watches),
                "has_more": False,
            }
        )
    try:
        page_request = _harness_page_request()
        status = _harness_status_filter()
        query = _harness_query_filter()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400
    with _harness_store() as store:
        page_result = store.list_watches_page(
            status=status,
            query=query,
            page_request=page_request,
            newest_first=True,
        )
        counts = store.count_watches(query=query)
        runtime = store.load_watch_runtime().get("watches") or {}
    for watch in page_result.items:
        watch["runtime"] = runtime.get(watch["id"]) or {"running": False}
    return jsonify(_harness_page_payload(page_result, items_key="watches", counts=counts))


@app.route("/api/harness/watches/<watch_id>", methods=["PATCH"])
def harness_watch_patch(watch_id: str):
    payload = request.json or {}
    if "enabled" not in payload:
        return jsonify({"ok": False, "code": "invalid_payload", "message": "missing 'enabled'"}), 400
    enabled = bool(payload["enabled"])
    with _harness_store() as store:
        if not store.get_watch(watch_id):
            return jsonify({"ok": False, "code": "watch_not_found"}), 404
        store.set_definition_enabled(watch_id, enabled, definition_type="watch")
        watch = store.get_watch(watch_id)
        runtime = store.load_watch_runtime().get("watches") or {}
        if watch:
            watch["runtime"] = runtime.get(watch_id) or {"running": False}
    return jsonify({"ok": True, "watch": watch})


@app.route("/api/harness/watches/<watch_id>", methods=["DELETE"])
def harness_watch_delete(watch_id: str):
    with _harness_store() as store:
        if not store.get_watch(watch_id):
            return jsonify({"ok": False, "code": "watch_not_found"}), 404
        store.remove_task(watch_id)
    return jsonify({"ok": True, "id": watch_id})


@app.route("/api/harness/runs", methods=["GET"])
def harness_runs_list():
    try:
        page_request = _harness_page_request()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400
    status = request.args.get("status") or None
    run_type = request.args.get("run_type") or None
    agent_name = request.args.get("agent_name") or None
    definition_id = request.args.get("definition_id") or None
    query = _harness_query_filter()

    with _harness_store() as store:
        page_result = store.list_runs_page(
            status=status,
            run_type=run_type,
            agent_name=agent_name,
            definition_id=definition_id,
            query=query,
            page_request=page_request,
            newest_first=True,
        )
        total = store.count_runs(
            status=status,
            run_type=run_type,
            agent_name=agent_name,
            definition_id=definition_id,
            query=query,
        )
        counts = store.count_runs_by_status(
            run_type=run_type,
            agent_name=agent_name,
            definition_id=definition_id,
            query=query,
        )
    return jsonify(
        {
            "runs": page_result.items,
            "counts": counts,
            "total": total,
            "page": page_result.page,
            "limit": page_result.limit,
            "has_more": page_result.has_more,
        }
    )


@app.route("/api/harness/bootstrap", methods=["GET"])
def harness_bootstrap():
    """Initial Harness page payload.

    Counts are global for tab badges; ``page`` mirrors the selected tab's
    existing endpoint shape so follow-up pagination and refreshes can keep using
    the dedicated routes.
    """
    tab = request.args.get("tab") or "tasks"
    if tab not in {"tasks", "watches", "runs"}:
        return jsonify({"ok": False, "code": "invalid_tab", "message": "tab must be one of: tasks, watches, runs"}), 400
    try:
        page_request = _harness_page_request()
        definition_status = _harness_status_filter() if tab in {"tasks", "watches"} else "all"
        query = _harness_query_filter()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400

    with _harness_store() as store:
        counts_payload = {
            "tasks": store.count_scheduled_tasks(),
            "watches": store.count_watches(),
            "runs": store.count_runs_by_status(),
        }
        if tab == "tasks":
            page_result = store.list_scheduled_tasks_page(
                status=definition_status,
                query=query,
                page_request=page_request,
                newest_first=True,
            )
            page_payload = _harness_page_payload_for_status(
                page_result,
                items_key="tasks",
                counts=store.count_scheduled_tasks(query=query),
                status=definition_status,
            )
        elif tab == "watches":
            page_result = store.list_watches_page(
                status=definition_status,
                query=query,
                page_request=page_request,
                newest_first=True,
            )
            runtime = store.load_watch_runtime().get("watches") or {}
            for watch in page_result.items:
                watch["runtime"] = runtime.get(watch["id"]) or {"running": False}
            page_payload = _harness_page_payload_for_status(
                page_result,
                items_key="watches",
                counts=store.count_watches(query=query),
                status=definition_status,
            )
        else:
            run_status = request.args.get("status") or None
            run_type = request.args.get("run_type") or None
            agent_name = request.args.get("agent_name") or None
            definition_id = request.args.get("definition_id") or None
            page_result = store.list_runs_page(
                status=run_status,
                run_type=run_type,
                agent_name=agent_name,
                definition_id=definition_id,
                query=query,
                page_request=page_request,
                newest_first=True,
            )
            page_payload = {
                "runs": page_result.items,
                "counts": store.count_runs_by_status(
                    run_type=run_type,
                    agent_name=agent_name,
                    definition_id=definition_id,
                    query=query,
                ),
                "total": store.count_runs(
                    status=run_status,
                    run_type=run_type,
                    agent_name=agent_name,
                    definition_id=definition_id,
                    query=query,
                ),
                "page": page_result.page,
                "limit": page_result.limit,
                "has_more": page_result.has_more,
            }
    return jsonify({"counts": counts_payload, "tab": tab, "page": page_payload})


@app.route("/api/harness/runs/<run_id>", methods=["GET"])
def harness_run_detail(run_id: str):
    with _harness_store() as store:
        run = store.get_run(run_id)
    if not run:
        return jsonify({"ok": False, "code": "run_not_found"}), 404
    return jsonify({"ok": True, "run": run})


# =============================================================================
# User & Bind Code Endpoints
# =============================================================================


@app.route("/api/users", methods=["GET"])
def users_get():
    from vibe import api

    return jsonify(api.get_users(request.args.get("platform") or None))


@app.route("/api/users", methods=["POST"])
def users_post():
    from vibe import api

    payload = request.json or {}
    return jsonify(api.save_users(payload))


@app.route("/api/users/<user_id>/admin", methods=["POST"])
def users_toggle_admin(user_id):
    from vibe import api

    payload = request.json or {}
    return jsonify(api.toggle_admin(user_id, payload.get("is_admin", False), payload.get("platform") or None))


@app.route("/api/users/<user_id>", methods=["DELETE"])
def users_delete(user_id):
    from vibe import api

    result = api.remove_user(user_id, request.args.get("platform") or None)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/bind-codes", methods=["GET"])
def bind_codes_get():
    from vibe import api

    return jsonify(api.get_bind_codes())


@app.route("/api/bind-codes", methods=["POST"])
def bind_codes_post():
    from vibe import api

    payload = request.json or {}
    result = api.create_bind_code(
        code_type=payload.get("type", "one_time"),
        expires_at=payload.get("expires_at"),
    )
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/bind-codes/<code>", methods=["DELETE"])
def bind_codes_delete(code):
    from vibe import api

    result = api.delete_bind_code(code)
    if not result.get("ok"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/setup/first-bind-code", methods=["GET"])
def setup_first_bind_code():
    from vibe import api

    return jsonify(api.get_first_bind_code())


# =============================================================================
# E2E Test-Only Endpoints (gated by E2E_TEST_MODE env var)
# =============================================================================

if os.environ.get("E2E_TEST_MODE", "").lower() in ("true", "1", "yes"):
    logger.warning(
        "E2E_TEST_MODE is ENABLED. /e2e/* endpoints are registered. "
        "These endpoints allow unauthenticated config mutation. "
        "Do NOT enable in production."
    )

    @app.route("/e2e/simulate-interaction", methods=["POST"])
    def e2e_simulate_interaction():
        """Simulate a modal submission via the settings/config APIs.

        Only registered when E2E_TEST_MODE=true.

        NOTE: Button clicks (cmd_settings, cmd_routing, etc.) should be
        triggered by sending text commands via Bot B (/settings, /routing, etc.).
        This endpoint handles modal *submissions* that Bot B cannot trigger
        because they require UI interaction (select dropdowns, click Save).

        The UI server and the service process are separate processes, so this
        endpoint operates through the SettingsStore (shared JSON file) rather
        than invoking the controller directly.

        JSON fields:
            action (str):       "settings_submit" | "routing_submit" | "cwd_submit"
            modal_values (dict): the values to submit
        """
        payload = request.json or {}
        action = payload.get("action", "")
        modal_values = payload.get("modal_values", {})

        if not action:
            return jsonify({"ok": False, "error": "action required"}), 400

        try:
            if action == "settings_submit":
                # Merge settings into existing store (not wholesale replace)
                from config.v2_settings import ChannelSettings, normalize_show_message_types
                from core.services import settings as settings_service
                from vibe.api import _parse_routing
                from vibe.api import _current_platform

                settings_key = modal_values.get("settings_key") or modal_values.get("channel_id")
                if not settings_key:
                    return jsonify({"ok": False, "error": "settings_key or channel_id required in modal_values"}), 400

                store = settings_service.reload_settings_store()
                platform = _current_platform()
                ch = store.find_channel(settings_key, platform=platform)
                if not ch:
                    ch = ChannelSettings(enabled=True)
                    store.update_channel(settings_key, ch, platform=platform)

                if "show_message_types" in modal_values:
                    ch.show_message_types = normalize_show_message_types(modal_values["show_message_types"])
                if "custom_cwd" in modal_values:
                    ch.custom_cwd = modal_values["custom_cwd"]
                if "require_mention" in modal_values:
                    ch.require_mention = modal_values["require_mention"]
                if "routing" in modal_values:
                    ch.routing = _parse_routing(modal_values["routing"])

                store.save()
                return jsonify({"ok": True, "action": action})

            elif action == "routing_submit":
                # Write routing config for a specific channel/user
                channel_id = modal_values.get("channel_id") or modal_values.get("settings_key")
                if not channel_id:
                    return jsonify({"ok": False, "error": "channel_id required in modal_values"}), 400

                from core.services import settings as settings_service

                store = settings_service.reload_settings_store()
                from vibe.api import _current_platform

                platform = _current_platform()
                ch = store.find_channel(channel_id, platform=platform)
                if ch:
                    from config.v2_settings import RoutingSettings

                    ch.routing = RoutingSettings(
                        agent_name=modal_values.get("backend", "opencode"),
                        model=(
                            modal_values.get("opencode_model")
                            or modal_values.get("claude_model")
                            or modal_values.get("codex_model")
                        ),
                        reasoning_effort=(
                            modal_values.get("opencode_reasoning_effort")
                            or modal_values.get("claude_reasoning_effort")
                            or modal_values.get("codex_reasoning_effort")
                        ),
                        opencode_agent=modal_values.get("opencode_agent"),
                        claude_agent=modal_values.get("claude_agent"),
                        codex_agent=modal_values.get("codex_agent"),
                    )
                    store.save()
                    return jsonify({"ok": True, "action": action})
                else:
                    return jsonify({"ok": False, "error": f"channel {channel_id} not found in settings"}), 404

            elif action == "cwd_submit":
                # Merge CWD into existing config (load → modify → save)
                from vibe import api as vibe_api

                current = vibe_api.config_to_payload(vibe_api.load_config())
                current.setdefault("runtime", {})
                current["runtime"]["default_cwd"] = modal_values.get("cwd", "/tmp")
                result = vibe_api.save_config(current)
                return jsonify({"ok": True, "action": action})

            elif action == "routing_submit":
                # Write routing config for a specific channel/user
                channel_id = modal_values.get("channel_id") or modal_values.get("settings_key")
                if not channel_id:
                    return jsonify({"ok": False, "error": "channel_id required in modal_values"}), 400

                from core.services import settings as settings_service

                store = settings_service.reload_settings_store()
                from vibe.api import _current_platform

                platform = _current_platform()
                ch = store.find_channel(channel_id, platform=platform)
                if ch:
                    from config.v2_settings import RoutingSettings

                    ch.routing = RoutingSettings(
                        agent_name=modal_values.get("backend", "opencode"),
                        model=(
                            modal_values.get("opencode_model")
                            or modal_values.get("claude_model")
                            or modal_values.get("codex_model")
                        ),
                        reasoning_effort=(
                            modal_values.get("opencode_reasoning_effort")
                            or modal_values.get("claude_reasoning_effort")
                            or modal_values.get("codex_reasoning_effort")
                        ),
                        opencode_agent=modal_values.get("opencode_agent"),
                        claude_agent=modal_values.get("claude_agent"),
                        codex_agent=modal_values.get("codex_agent"),
                    )
                    store.save()
                    return jsonify({"ok": True, "action": action})
                else:
                    return jsonify({"ok": False, "error": f"channel {channel_id} not found in settings"}), 404

            elif action == "cwd_submit":
                # Update CWD via config API
                new_cwd = modal_values.get("cwd", "/tmp")
                result = vibe_api.save_config({"runtime": {"default_cwd": new_cwd}})
                return jsonify({"ok": True, "action": action, "result": result})

            else:
                return jsonify({"ok": False, "error": f"unknown action: {action}"}), 400

        except Exception as e:
            logger.exception("E2E simulate-interaction failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/e2e/ping", methods=["GET"])
    def e2e_ping():
        """Simple check that E2E test mode is active."""
        return jsonify({"ok": True, "e2e_test_mode": True})

    logger.info("E2E_TEST_MODE enabled: /e2e/* endpoints registered")


# =============================================================================
# Static Files (SPA)
# =============================================================================


def _show_page_offline_response():
    html = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Show Page Offline</title>
    <style>
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fb; color: #172033; }
      main { width: min(560px, 100%); border: 1px solid rgba(23, 32, 51, 0.12); border-radius: 12px; background: white; padding: 32px; box-shadow: 0 20px 60px rgba(23, 32, 51, 0.10); }
      h1 { margin: 0; font-size: clamp(28px, 7vw, 42px); line-height: 1.05; letter-spacing: 0; }
      p { margin: 14px 0 0; line-height: 1.65; color: #526078; }
    </style>
  </head>
  <body>
    <main>
      <h1>This Show Page is offline</h1>
      <p>The page owner has taken this page offline. The link is no longer available.</p>
    </main>
  </body>
</html>
"""
    return Response(html, status=401, mimetype="text/html; charset=utf-8")


def _show_page_not_found_response():
    return jsonify({"error": "not_found"}), 404


def _show_page_runtime_unavailable_response():
    return jsonify({"error": "show_runtime_unavailable"}), 503


def _is_show_api_asset(asset_path: str) -> bool:
    relative = (asset_path or "").strip("/")
    return relative == "api" or relative.startswith("api/") or relative == "__show" or relative.startswith("__show/")


def _is_show_page_entry_asset(asset_path: str) -> bool:
    relative = (asset_path or "").strip("/")
    return relative in {"", "index.html"}


def _show_page_recovery_response(session_id: str):
    from core.show_pages import show_page_runtime_recovery_html

    return Response(show_page_runtime_recovery_html(session_id), status=200, mimetype="text/html; charset=utf-8")


def _show_page_file_response(root: Path, asset_path: str):
    relative = (asset_path or "").strip("/")
    if not relative:
        relative = "index.html"
    candidate = (root / unquote(relative)).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        return jsonify({"error": "not_found"}), 404
    if not candidate.exists() or not candidate.is_file():
        return _show_page_not_found_response()
    mime_type, _ = mimetypes.guess_type(str(candidate))
    response = send_file(candidate, mimetype=mime_type or "application/octet-stream")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _show_session_event_error_response(exc: Exception):
    code = getattr(exc, "code", "show_session_event_failed")
    status = 404 if code == "session_not_found" else 400
    return jsonify({"ok": False, "code": code, "error": str(exc)}), status


def _show_session_event_store():
    from core.show_session_events import ShowSessionEventStore

    return ShowSessionEventStore()


def _show_events_payload_from_request() -> dict[str, Any]:
    payload = request.json
    return payload if isinstance(payload, dict) else {}


def _last_event_id_from_request() -> str | None:
    value = request.headers.get("Last-Event-ID")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _show_event_write_authorized(session_id: str) -> bool:
    token = request.headers.get(SHOW_EVENT_WRITE_TOKEN_HEADER)
    if not token:
        return False
    try:
        expected = show_event_write_token(session_id)
    except Exception:
        return False
    return hmac.compare_digest(token, expected)


def _show_event_response_from_payload(session_id: str, payload: dict[str, Any]):
    if show_event_payload_session_mismatch(session_id, payload):
        return (
            jsonify(
                {
                    "ok": False,
                    "code": "session_mismatch",
                    "error": "Show event sessionId must match the URL session.",
                }
            ),
            400,
        )
    store = _show_session_event_store()
    try:
        event_payload = store.append(session_id, payload)
    except Exception as exc:
        return _show_session_event_error_response(exc)
    finally:
        store.close()

    _publish_show_session_event(event_payload)
    _dispatch_show_event_if_requested(event_payload)
    return jsonify({"ok": True, "event": event_payload}), 201


def record_local_show_event(session_id: str, payload: dict[str, Any], *, dispatch_sync: bool = False) -> dict[str, Any]:
    store = _show_session_event_store()
    try:
        event_payload = store.append(session_id, payload)
    finally:
        store.close()
    _publish_show_session_event(event_payload)
    if dispatch_sync and _show_event_requests_dispatch(event_payload):
        try:
            asyncio.run(_run_show_event_dispatch(event_payload))
        except RuntimeError:
            _dispatch_show_event_if_requested(event_payload)
    else:
        _dispatch_show_event_if_requested(event_payload)
    return event_payload


def _publish_show_session_event(event_payload: dict[str, Any]) -> None:
    from vibe.sse_broker import broker

    broker.publish("show.event", event_payload)
    message = event_payload.get("message")
    if isinstance(message, dict):
        broker.publish("message.new", message)
    broker.publish(
        "session.activity",
        {
            "session_id": event_payload.get("session_id"),
            "scope_id": event_payload.get("scope_id"),
            "event": "show_event",
        },
    )


def _dispatch_show_event_if_requested(event_payload: dict[str, Any]) -> None:
    if not _show_event_requests_dispatch(event_payload):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        thread = threading.Thread(
            target=lambda: asyncio.run(_run_show_event_dispatch(event_payload)),
            name="show-event-dispatch",
            daemon=True,
        )
        thread.start()
        return
    loop.create_task(_run_show_event_dispatch(event_payload))


def _show_event_requests_dispatch(event_payload: dict[str, Any]) -> bool:
    if event_payload.get("actor") != "human":
        return False
    if event_payload.get("type") not in {"human.intent.submitted", "human.annotation.created"}:
        return False
    payload = event_payload.get("payload")
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("dispatch"))


async def _run_show_event_dispatch(event_payload: dict[str, Any]) -> None:
    from vibe import internal_client

    session_id = event_payload.get("session_id")
    scope_id = event_payload.get("scope_id")
    transcript_text = event_payload.get("transcript_text")
    if not isinstance(session_id, str) or not session_id or not isinstance(transcript_text, str) or not transcript_text.strip():
        return
    dispatch_payload = {
        "session_id": session_id,
        "text": transcript_text,
        "scope_id": scope_id,
        "user_message_id": event_payload.get("message_id"),
        "message_id": event_payload.get("message_id"),
        "platform": "avibe",
        "channel_id": session_id,
    }
    try:
        async for event_name, data in internal_client.stream_dispatch(dispatch_payload):
            _publish_show_dispatch_event(event_payload, event_name, data)
    except internal_client.InternalServerUnavailable as exc:
        _publish_show_dispatch_event(
            event_payload,
            "stream.error",
            {"reason": "internal_server_unavailable", "detail": str(exc)},
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("show event dispatch failed")
        _publish_show_dispatch_event(event_payload, "stream.error", {"reason": "dispatch_failed", "detail": str(exc)})


def _publish_show_dispatch_event(event_payload: dict[str, Any], event_name: str, data: Any) -> None:
    from vibe.sse_broker import broker

    broker.publish(
        "show.dispatch",
        {
            "show_event_id": event_payload.get("id"),
            "session_id": event_payload.get("session_id"),
            "scope_id": event_payload.get("scope_id"),
            "event": event_name,
            "data": data,
        },
    )


def _show_event_response_payload(event_payload: dict[str, Any], *, public: bool = False) -> dict[str, Any]:
    if not public:
        return event_payload
    return {
        key: value
        for key, value in event_payload.items()
        if key not in {"session_id", "scope_id", "message_id", "message"}
    }


def _show_dispatch_response_payload(event_payload: dict[str, Any], *, public: bool = False) -> dict[str, Any]:
    if not public:
        return event_payload
    return {
        key: _redact_public_dispatch_value(value)
        for key, value in event_payload.items()
        if key not in {"session_id", "scope_id", "message_id", "message", "user_message_id"}
    }


def _redact_public_dispatch_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_public_dispatch_value(nested)
            for key, nested in value.items()
            if key not in {"session_id", "scope_id", "message_id", "message", "user_message_id"}
        }
    if isinstance(value, list):
        return [_redact_public_dispatch_value(item) for item in value]
    return value


def _show_events_list_payload(payload: dict[str, Any], *, public: bool = False) -> dict[str, Any]:
    if not public:
        return payload
    return {
        **payload,
        "events": [
            _show_event_response_payload(event_payload, public=True)
            for event_payload in payload.get("events", [])
            if isinstance(event_payload, dict)
        ],
    }


async def _show_events_stream(session_id: str, *, after_id: str | None = None, public: bool = False):
    import asyncio

    from fastapi.responses import StreamingResponse

    from vibe.sse_broker import broker

    def _event_visible(event_payload: dict[str, Any]) -> bool:
        return event_payload.get("session_id") == session_id

    async def generate():
        sub_id, queue = broker.subscribe()
        replayed_ids: set[str] = set()
        try:
            store = _show_session_event_store()
            try:
                cursor = after_id
                yield ": show events connected\n\n"
                while True:
                    batch = store.list(session_id, after_id=cursor, limit=500)
                    events = batch["events"]
                    if not events:
                        break
                    for event_payload in events:
                        if isinstance(event_payload.get("id"), str):
                            replayed_ids.add(event_payload["id"])
                        yield _sse_frame("show.event", _show_event_response_payload(event_payload, public=public))
                    cursor = batch.get("next_after_id")
                    if not cursor:
                        break
            finally:
                store.close()

            while True:
                try:
                    event_type, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    decoded = json.loads(payload)
                    event_payload = decoded.get("data") if isinstance(decoded, dict) else None
                    if event_type == "show.event" and isinstance(event_payload, dict) and _event_visible(event_payload):
                        event_id = event_payload.get("id")
                        if isinstance(event_id, str) and event_id in replayed_ids:
                            continue
                        if isinstance(event_id, str):
                            replayed_ids.add(event_id)
                        yield _sse_frame("show.event", _show_event_response_payload(event_payload, public=public))
                    elif event_type == "show.dispatch" and isinstance(event_payload, dict) and _event_visible(event_payload):
                        yield _sse_frame("show.dispatch", _show_dispatch_response_payload(event_payload, public=public))
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            broker.unsubscribe(sub_id)

    def _sse_frame(event_type: str, data: Any) -> str:
        event_id = data.get("id") if isinstance(data, dict) else None
        prefix = f"id: {event_id}\n" if isinstance(event_id, str) and event_id else ""
        return f"{prefix}event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _show_events_response(session_id: str, *, public: bool = False):
    if request.method == "GET":
        if request.args.get("stream") == "1":
            return await _show_events_stream(
                session_id,
                after_id=request.args.get("after_id") or _last_event_id_from_request(),
                public=public,
            )
        store = _show_session_event_store()
        try:
            try:
                limit = int(request.args.get("limit") or 100)
            except (TypeError, ValueError):
                limit = 100
            payload = store.list(session_id, after_id=request.args.get("after_id") or None, limit=limit)
            return jsonify(_show_events_list_payload(payload, public=public))
        finally:
            store.close()

    if request.method != "POST":
        return jsonify({"ok": False, "code": "method_not_allowed"}), 405
    if not _show_event_write_authorized(session_id):
        return jsonify({"ok": False, "code": "show_event_write_forbidden"}), 403

    return _show_event_response_from_payload(session_id, _show_events_payload_from_request())


@app.route("/api/show/sessions/<session_id>/events", methods=["POST"])
def show_session_events_create(session_id: str):
    if not _is_cli_show_event_request():
        return jsonify({"ok": False, "code": "forbidden"}), 403
    return _show_event_response_from_payload(session_id, _show_events_payload_from_request())


@app.route("/api/show/sessions/<session_id>/prewarm", methods=["POST"])
async def show_session_prewarm(session_id: str):
    if not _is_cli_show_event_request():
        return jsonify({"ok": False, "code": "forbidden"}), 403
    payload = _show_events_payload_from_request()
    base_path = payload.get("base_path")
    if base_path is not None and not isinstance(base_path, str):
        return jsonify({"ok": False, "code": "invalid_base_path"}), 400
    from core.show_runtime import prewarm_show_page_session

    result = await prewarm_show_page_session(session_id, base_path=base_path)
    status_code = 200 if result.available else 202
    return jsonify({"ok": result.available, "reason": result.reason, "base_url": result.base_url}), status_code


@app.route(f"{_SHOW_RUNTIME_VENDOR_PREFIX}/<path:vendor_path>", methods=["GET", "HEAD"])
async def show_runtime_vendor_asset(vendor_path: str):
    """Proxy the runtime's shared, content-hashed vendor bundle.

    The runtime serves the vendor at the session-independent path
    `/_show-runtime/vendor/<hash>/<file>` and references it from the import map it
    injects into every Show Page, so the same URL is requested by both the authed
    `/show/<id>/` surface and the anonymous public `/p/<share>/` surface. We forward
    this prefix verbatim (never under a per-session base) and, because the content
    hash is in the path, mark successful responses immutable for a year.
    """
    runtime_path = f"{_SHOW_RUNTIME_VENDOR_PREFIX}/{quote(vendor_path, safe='/@:-._~')}"
    if request._request.url.query:
        runtime_path = f"{runtime_path}?{request._request.url.query}"
    from core.show_runtime import get_show_runtime_manager

    forwarded_headers = {
        key: value
        for key, value in request._request.headers.items()
        if key.lower() in _SHOW_RUNTIME_REQUEST_HEADER_ALLOWLIST
    }
    try:
        proxied = await get_show_runtime_manager().request(
            request.method,
            runtime_path,
            headers=forwarded_headers,
            body=None,
        )
    except Exception:
        return _show_page_runtime_unavailable_response()
    response_headers = {
        key: value
        for key, value in proxied.headers.items()
        if key.lower() in _SHOW_RUNTIME_RESPONSE_HEADER_ALLOWLIST
    }
    response_headers["X-Content-Type-Options"] = "nosniff"
    response_headers["Referrer-Policy"] = "no-referrer"
    if 200 <= proxied.status_code < 300:
        _remove_response_header(response_headers, "cache-control")
        _remove_response_header(response_headers, "set-cookie")
        response_headers["Cache-Control"] = _SHOW_RUNTIME_IMMUTABLE_CACHE_CONTROL
    content = _compress_show_runtime_response(proxied.content, response_headers, request._request)
    return FastAPIResponse(content=content, status_code=proxied.status_code, headers=response_headers)


@app.route(_SHOW_RUNTIME_PUBLIC_CLIENT_SHIM_PATH, methods=["GET", "HEAD"])
def show_runtime_public_client_shim():
    content = b"""
const styles = new Map();

export function createHotContext() {
  return {
    data: {},
    accept() {},
    decline() {},
    dispose() {},
    invalidate() {},
    on() {},
    prune() {},
    send() {},
  };
}

export function updateStyle(id, css) {
  let style = styles.get(id);
  if (!style) {
    style = document.createElement("style");
    style.setAttribute("type", "text/css");
    style.setAttribute("data-vite-dev-id", id);
    document.head.appendChild(style);
    styles.set(id, style);
  }
  style.textContent = css;
}

export function removeStyle(id) {
  const style = styles.get(id);
  if (style) {
    style.remove();
    styles.delete(id);
  }
}
"""
    return FastAPIResponse(
        content=content.strip(),
        media_type="text/javascript",
        headers={
            "Cache-Control": _SHOW_RUNTIME_IMMUTABLE_CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.route(_SHOW_RUNTIME_PUBLIC_REACT_REFRESH_SHIM_PATH, methods=["GET", "HEAD"])
def show_runtime_public_react_refresh_shim():
    content = b"""
function identity(type) {
  return type;
}

function noop() {}

export function injectIntoGlobalHook(target) {
  const scope = target || globalThis;
  scope.$RefreshReg$ = scope.$RefreshReg$ || noop;
  scope.$RefreshSig$ = scope.$RefreshSig$ || (() => identity);
}

export const register = noop;
export const performReactRefresh = noop;
export const createSignatureFunctionForTransform = () => identity;
export const isLikelyComponentType = () => false;
export const getFamilyByType = () => undefined;
export const __hmr_import = () => Promise.resolve({});
export const registerExportsForReactRefresh = noop;
export const validateRefreshBoundaryAndEnqueueUpdate = () => undefined;
"""
    return FastAPIResponse(
        content=content.strip(),
        media_type="text/javascript",
        headers={
            "Cache-Control": _SHOW_RUNTIME_IMMUTABLE_CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


def _show_runtime_public_client_shim_response(asset_path: str):
    normalized = (asset_path or "").strip("/")
    if normalized == "@vite/client":
        return show_runtime_public_client_shim()
    if normalized == "@react-refresh":
        return show_runtime_public_react_refresh_shim()
    return None


async def _show_page_runtime_response(
    session_id: str,
    asset_path: str,
    starlette_request: FastAPIRequest,
    *,
    external_prefix: str | None = None,
    inject_private_config: bool = False,
):
    from core.show_runtime import get_show_runtime_manager

    session_part = quote(session_id, safe="")
    asset_part = quote(asset_path.lstrip("/"), safe="/@:-._~")
    runtime_path = f"/sessions/{session_part}/app/"
    if asset_part:
        runtime_path = f"{runtime_path}{asset_part}"
    if starlette_request.url.query:
        runtime_path = f"{runtime_path}?{starlette_request.url.query}"
    forwarded_headers = {
        key: value
        for key, value in starlette_request.headers.items()
        if key.lower() in _SHOW_RUNTIME_REQUEST_HEADER_ALLOWLIST
    }
    if external_prefix:
        forwarded_headers["x-vibe-show-base"] = f"{external_prefix.rstrip('/')}/"
    body = await starlette_request.body()
    request_started = time.monotonic()
    proxied = await get_show_runtime_manager().request(
        starlette_request.method,
        runtime_path,
        headers=forwarded_headers,
        body=body or None,
    )
    proxy_duration_ms = int((time.monotonic() - request_started) * 1000)
    if proxy_duration_ms >= SHOW_RUNTIME_SLOW_REQUEST_MS or _is_show_page_entry_asset(asset_path):
        logger.info(
            "Show Runtime proxy %s %s session=%s asset=%s status=%s duration_ms=%s",
            starlette_request.method,
            runtime_path.split("?", 1)[0],
            session_id,
            asset_path or "<entry>",
            proxied.status_code,
            proxy_duration_ms,
        )
    response_headers = {
        key: value
        for key, value in proxied.headers.items()
        if key.lower() in _SHOW_RUNTIME_RESPONSE_HEADER_ALLOWLIST
    }
    if location := response_headers.get("location"):
        response_headers["location"] = _rewrite_show_runtime_location(
            session_id,
            location,
            external_prefix=external_prefix,
        )
    response_headers["X-Content-Type-Options"] = "nosniff"
    response_headers["Referrer-Policy"] = "no-referrer"
    content = proxied.content
    if proxied.status_code == 200 and external_prefix:
        # Public `/p/<share>/` surface only: rewrite the runtime's internal
        # `/show/<id>/` paths to the public base and neutralize Vite's HMR client /
        # React Fast Refresh so anonymous viewers don't open a live dev socket. The
        # shared vendor bundle is referenced via the runtime's import map at the
        # session-independent `/_show-runtime/vendor/...` path, so it is untouched here.
        content = _rewrite_public_show_runtime_private_paths(
            content,
            response_headers,
            session_id=session_id,
            external_prefix=external_prefix,
        )
        content = _rewrite_public_show_runtime_client(content, response_headers, external_prefix=external_prefix)
    if _should_inject_show_runtime_config(proxied.status_code, response_headers, inject_private_config=inject_private_config):
        content = _inject_show_runtime_config(content, session_id)
        _mark_show_runtime_document_no_store(response_headers)
    elif _is_show_page_entry_asset(asset_path) and 200 <= proxied.status_code < 300:
        # The entry document is per-session/per-share dynamic (it embeds the import map
        # and base path); never let it be cached. App modules and per-session deps keep
        # the runtime's own cache headers (Vite marks optimized deps immutable).
        _mark_show_runtime_document_no_store(response_headers)
    content = _compress_show_runtime_response(content, response_headers, starlette_request)
    return FastAPIResponse(content=content, status_code=proxied.status_code, headers=response_headers)


def _should_inject_show_runtime_config(
    status_code: int,
    headers: dict[str, str],
    *,
    inject_private_config: bool,
) -> bool:
    if not inject_private_config or status_code != 200:
        return False
    if _show_response_is_attachment(_response_header(headers, "content-disposition")):
        return False
    return _show_response_is_html(_response_header(headers, "content-type"))


def _mark_show_runtime_document_no_store(headers: dict[str, str]) -> None:
    for name in ("cache-control", "etag", "expires", "last-modified", "content-length"):
        _remove_response_header(headers, name)
    headers["Cache-Control"] = "no-store"


def _compress_show_runtime_response(content: bytes, headers: dict[str, str], starlette_request: FastAPIRequest) -> bytes:
    if len(content) < _SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES:
        return content
    if _response_header(headers, "content-encoding"):
        return content
    if "gzip" not in (starlette_request.headers.get("accept-encoding") or "").lower():
        return content
    content_type = _response_header(headers, "content-type") or ""
    if not _show_response_is_compressible(content_type):
        return content
    compressed = gzip.compress(content, compresslevel=6)
    if len(compressed) >= len(content):
        return content
    _remove_response_header(headers, "content-length")
    _remove_response_header(headers, "etag")
    headers["Content-Encoding"] = "gzip"
    headers["Vary"] = _append_vary_header(_response_header(headers, "vary"), "Accept-Encoding")
    return compressed


def _append_vary_header(existing: str | None, value: str) -> str:
    values = [item.strip() for item in (existing or "").split(",") if item.strip()]
    if not any(item.lower() == value.lower() for item in values):
        values.append(value)
    return ", ".join(values)


def _show_response_is_compressible(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return any(
        marker in lowered
        for marker in (
            "javascript",
            "ecmascript",
            "text/",
            "json",
            "css",
            "svg",
            "xml",
        )
    )


def _show_response_is_rewritable_show_runtime_source(content_type: str | None) -> bool:
    return _show_response_is_javascript(content_type) or _show_response_is_html(content_type)


def _rewrite_public_show_runtime_client(
    content: bytes,
    headers: dict[str, str],
    *,
    external_prefix: str | None,
) -> bytes:
    if not external_prefix:
        return content
    content_type = _response_header(headers, "content-type") or ""
    if not _show_response_is_rewritable_show_runtime_source(content_type):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    public_prefix = f"{external_prefix.rstrip('/')}/"
    rewritten = text.replace(f"{public_prefix}@vite/client", _SHOW_RUNTIME_PUBLIC_CLIENT_SHIM_PATH)
    rewritten = rewritten.replace(f"{public_prefix}@react-refresh", _SHOW_RUNTIME_PUBLIC_REACT_REFRESH_SHIM_PATH)
    if rewritten == text:
        return content
    _mark_show_runtime_document_no_store(headers)
    return rewritten.encode("utf-8")


def _rewrite_public_show_runtime_private_paths(
    content: bytes,
    headers: dict[str, str],
    *,
    session_id: str,
    external_prefix: str | None,
) -> bytes:
    if not external_prefix:
        return content
    content_type = _response_header(headers, "content-type") or ""
    if not _show_response_is_rewritable_show_runtime_source(content_type):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    private_prefix = f"/show/{quote(session_id, safe='')}/"
    public_prefix = f"{external_prefix.rstrip('/')}/"
    # Only rewrite the private prefix where it is a genuine URL reference, not
    # where the same "/show/<session>/" substring is embedded inside an absolute
    # Vite /@fs/<realpath> filesystem path (e.g.
    # /@fs/<home>/.avibe/show/<session>/src/App.tsx). A blind str.replace would
    # also corrupt that fs path -> the module 404s and is served as index.html
    # -> MIME error -> the app never mounts (blank public Show Page). A genuine
    # URL reference of the prefix is preceded by a quote / paren / = / comma /
    # whitespace / start, whereas the embedded fs occurrence is preceded by an
    # alphanumeric path-component char (the "e" in ".avibe"); the negative
    # lookbehind (note: "/" is deliberately NOT excluded) skips only the latter.
    rewritten = re.sub(
        r"(?<![A-Za-z0-9._~-])" + re.escape(private_prefix),
        public_prefix,
        text,
    )
    if rewritten == text:
        return content
    _mark_show_runtime_document_no_store(headers)
    return rewritten.encode("utf-8")


def _show_response_is_javascript(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return "javascript" in lowered or "ecmascript" in lowered


def _is_show_runtime_immutable_asset(relative_asset_path: str) -> bool:
    if relative_asset_path.startswith(".vite/deps/"):
        return True
    if relative_asset_path.startswith("node_modules/.vite/deps/"):
        return True
    if relative_asset_path.startswith("@fs/") and _is_relocated_vite_dep_path(relative_asset_path):
        return True
    return False


def _is_relocated_vite_dep_path(relative_asset_path: str) -> bool:
    return (
        "/deps/" in relative_asset_path
        and (
            "/vite-cache/" in relative_asset_path
            or "/.vite-cache/" in relative_asset_path
        )
    )


def _is_show_runtime_immutable_asset_path(asset_path: str) -> bool:
    return _is_show_runtime_immutable_asset((asset_path or "").strip("/"))


def _is_current_show_runtime_immutable_asset_request() -> bool:
    if (request.path or "").startswith(f"{_SHOW_RUNTIME_VENDOR_PREFIX}/"):
        return True
    path = (request.path or "").strip("/")
    parts = path.split("/", 2)
    if len(parts) < 3 or parts[0] not in {"show", "p"}:
        return False
    return _is_show_runtime_immutable_asset_path(parts[2])


def _remove_response_header(headers: dict[str, str], name: str) -> None:
    normalized = name.lower()
    for key in list(headers):
        if key.lower() == normalized:
            headers.pop(key, None)


def _response_header(headers: dict[str, str], name: str) -> str | None:
    normalized = name.lower()
    for key, value in headers.items():
        if key.lower() == normalized:
            return value
    return None


def _show_response_is_html(content_type: str | None) -> bool:
    return bool(content_type and "text/html" in content_type.lower())


def _show_response_is_attachment(content_disposition: str | None) -> bool:
    return bool(content_disposition and content_disposition.lstrip().lower().startswith("attachment"))


def _show_runtime_config_payload(session_id: str) -> dict[str, str]:
    session_path = quote(session_id, safe="")
    events_path = f"/show/{session_path}/__show/events"
    return {
        "sessionId": session_id,
        "basePath": f"/show/{session_path}/",
        "eventsPath": events_path,
        "streamPath": f"{events_path}?stream=1",
        "writeToken": show_event_write_token(session_id),
    }


def _show_runtime_config_script(session_id: str) -> str:
    payload = json.dumps(_show_runtime_config_payload(session_id), ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        "<script>"
        "(function(){"
        f"var next={payload};"
        "globalThis.__AVIBE_SHOW__=Object.assign({},globalThis.__AVIBE_SHOW__||{},next);"
        "}());"
        "</script>"
    )


def _inject_show_runtime_config(content: bytes, session_id: str) -> bytes:
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    script = _show_runtime_config_script(session_id)
    module_match = _SHOW_RUNTIME_MODULE_SCRIPT_RE.search(html)
    if module_match:
        html = f"{html[: module_match.start()]}{script}\n    {html[module_match.start() :]}"
    elif "</head>" in html:
        html = html.replace("</head>", f"{script}\n  </head>", 1)
    elif "</body>" in html:
        html = html.replace("</body>", f"{script}\n  </body>", 1)
    else:
        html = f"{script}\n{html}"
    return html.encode("utf-8")


def _rewrite_show_runtime_location(session_id: str, location: str, *, external_prefix: str | None = None) -> str:
    parsed = urlsplit(location)
    internal_prefix = f"/sessions/{quote(session_id, safe='')}/app"
    external_prefix = (external_prefix or f"/show/{quote(session_id, safe='')}").rstrip("/")
    if parsed.path == internal_prefix:
        public_path = f"{external_prefix}/"
    elif parsed.path.startswith(f"{internal_prefix}/"):
        suffix = parsed.path[len(internal_prefix) :].lstrip("/")
        public_path = f"{external_prefix}/{suffix}"
    else:
        return location
    return urlunsplit(("", "", public_path, parsed.query, parsed.fragment))


def _with_show_event_write_cookie(response: Response, session_id: str, *, enabled: bool) -> Response:
    if enabled:
        # 'self' (not 'none') so the workbench can frame a private Show Page in the
        # chat view — same origin as the page — while cross-origin clickjacking
        # stays blocked. Direct navigation is unaffected (frame-ancestors only
        # governs framing).
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        response.set_cookie(
            SHOW_EVENT_WRITE_TOKEN_COOKIE,
            show_event_write_token(session_id),
            httponly=False,
            secure=request.is_secure,
            samesite="Strict",
            path=f"/show/{quote(session_id, safe='')}/",
        )
    else:
        response.delete_cookie(SHOW_EVENT_WRITE_TOKEN_COOKIE, path=f"/show/{quote(session_id, safe='')}/")
    return response


def stop_show_runtime_on_shutdown() -> None:
    from core.show_runtime import stop_show_runtime_manager

    stop_show_runtime_manager()


@app.route("/show/<session_id>")
def redirect_private_show_page_to_canonical_path(session_id):
    from core.show_pages import ShowPageStore

    store = ShowPageStore()
    try:
        page = store.get(session_id)
        if page is None:
            return _show_page_not_found_response()
        if page.visibility not in {"private", "offline"}:
            return _show_page_not_found_response()
        return redirect(f"/show/{quote(session_id, safe='')}/")
    finally:
        store.close()


@app.route("/show/<session_id>/", defaults={"asset_path": ""})
@app.route(
    "/show/<session_id>/",
    defaults={"asset_path": ""},
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@app.route(
    "/show/<session_id>/<path:asset_path>",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def serve_private_show_page(session_id, asset_path):
    from core.show_pages import ShowPageStore, ensure_show_page_dir

    store = ShowPageStore()
    try:
        page = store.get(session_id)
        if page is None:
            return _show_page_not_found_response()
        if page.visibility == "offline":
            return _show_page_offline_response()
        if page.visibility != "private":
            return _show_page_not_found_response()
        if asset_path.strip("/") in {"__show/events", "__events"}:
            return await _show_events_response(page.session_id)
        page_dir = ensure_show_page_dir(page.session_id)
        response = None
        if request.method in {"GET", "HEAD"} or _is_show_api_asset(asset_path):
            try:
                starlette_request = request._request
                response = await _show_page_runtime_response(
                    page.session_id,
                    asset_path,
                    starlette_request,
                    inject_private_config=request.method == "GET" and not _is_show_api_asset(asset_path),
                )
            except Exception:
                if _is_show_api_asset(asset_path):
                    return _show_page_runtime_unavailable_response()
                if _is_show_page_entry_asset(asset_path):
                    response = _show_page_recovery_response(page.session_id)
                    logger.debug("Show runtime unavailable; serving recovery Show Page", exc_info=True)
                else:
                    logger.debug("Show runtime unavailable; serving static Show Page", exc_info=True)
        if response is None:
            response = _show_page_file_response(page_dir, asset_path)
        if request.method in {"GET", "HEAD"}:
            if _is_show_runtime_immutable_asset_path(asset_path):
                return response
            return _with_show_event_write_cookie(response, page.session_id, enabled=True)
        return response
    finally:
        store.close()


@app.route("/p/<share_id>")
def redirect_public_show_page_to_canonical_path(share_id):
    from core.show_pages import ShowPageStore

    store = ShowPageStore()
    try:
        page = store.get_by_share_id(share_id)
        if page is None:
            return _show_page_not_found_response()
        if page.visibility not in {"public", "offline"}:
            return _show_page_not_found_response()
        return redirect(f"/p/{quote(share_id, safe='')}/")
    finally:
        store.close()


@app.route(
    "/p/<share_id>/",
    defaults={"asset_path": ""},
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@app.route(
    "/p/<share_id>/<path:asset_path>",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def serve_public_show_page(share_id, asset_path):
    from core.show_pages import ShowPageStore, ensure_show_page_dir

    store = ShowPageStore()
    try:
        page = store.get_by_share_id(share_id)
        if page is None:
            return _show_page_not_found_response()
        if page.visibility == "offline":
            return _show_page_offline_response()
        if page.visibility != "public":
            return _show_page_not_found_response()
        if asset_path.strip("/") in {"__show/events", "__events"}:
            if request.method != "GET":
                return jsonify({"ok": False, "code": "public_show_events_read_only"}), 403
            return await _show_events_response(page.session_id, public=True)
        if request.method in {"GET", "HEAD"}:
            if shim_response := _show_runtime_public_client_shim_response(asset_path):
                return shim_response
        page_dir = ensure_show_page_dir(page.session_id)
        response = None
        if request.method in {"GET", "HEAD"} or _is_show_api_asset(asset_path):
            try:
                starlette_request = request._request
                response = await _show_page_runtime_response(
                    page.session_id,
                    asset_path,
                    starlette_request,
                    external_prefix=f"/p/{quote(share_id, safe='')}",
                )
            except Exception:
                if _is_show_api_asset(asset_path):
                    return _show_page_runtime_unavailable_response()
                if _is_show_page_entry_asset(asset_path):
                    response = _show_page_recovery_response(page.session_id)
                    logger.debug("Show runtime unavailable; serving recovery public Show Page", exc_info=True)
                else:
                    logger.debug("Show runtime unavailable; serving static public Show Page", exc_info=True)
        if response is None:
            response = _show_page_file_response(page_dir, asset_path)
        if request.method in {"GET", "HEAD"}:
            if _is_show_runtime_immutable_asset_path(asset_path):
                return response
            return _with_show_event_write_cookie(response, page.session_id, enabled=False)
        return response
    finally:
        store.close()


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_static(path):
    """Serve static files from ui/dist, with SPA fallback to index.html."""
    ui_dist = get_ui_dist_path()

    if path.startswith("assets/"):
        file_path = ui_dist / path
    elif not path or path == "index.html":
        file_path = ui_dist / "index.html"
    else:
        file_path = ui_dist / path

    resolved_path = file_path.resolve()

    # Security check: ensure path is within ui_dist
    if ui_dist.resolve() not in resolved_path.parents and resolved_path != ui_dist.resolve():
        return jsonify({"error": "not_found"}), 404

    if resolved_path.exists() and resolved_path.is_file():
        mime_type, _ = mimetypes.guess_type(str(resolved_path))
        response = send_file(resolved_path, mimetype=mime_type or "application/octet-stream")
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif resolved_path.name == "index.html":
            response.headers["Cache-Control"] = "no-store, private"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # SPA fallback: serve index.html for routes without file extension
    if "." not in path:
        index_path = ui_dist / "index.html"
        if index_path.exists():
            response = send_file(index_path, mimetype="text/html")
            response.headers["Cache-Control"] = "no-store, private"
            return response

    return jsonify({"error": "not_found"}), 404


# =============================================================================
# Server Entry Point
# =============================================================================


def _reconcile_remote_access_for_ui_start(config: V2Config | None) -> None:
    if config is None:
        return
    try:
        from vibe import remote_access

        result = remote_access.reconcile(config)
        if isinstance(result, dict) and result.get("ok") is False:
            logger.warning("Remote access reconcile after UI start failed: %s", result.get("error"))
    except Exception:
        logger.warning("Failed to reconcile remote access after UI start", exc_info=True)


# --- Realtime inbox bridge --------------------------------------------------
# Relays the controller's cross-process inbox events into the local SSE broker
# (see vibe/inbox_bridge.py). One task per UI-server process, owned by the ASGI
# lifecycle so it starts after the loop is alive and is cancelled cleanly on
# shutdown/reload instead of leaking a pending task.

_inbox_bridge_task: "asyncio.Task | None" = None
_startup_dependency_reconcile_task: "asyncio.Task | None" = None
_terminal_service: TerminalService | None = None


async def _start_inbox_bridge() -> None:
    global _inbox_bridge_task
    from vibe.inbox_bridge import run_inbox_bridge

    if _inbox_bridge_task is None or _inbox_bridge_task.done():
        _inbox_bridge_task = asyncio.create_task(run_inbox_bridge(), name="inbox-events-bridge")


async def _stop_inbox_bridge() -> None:
    global _inbox_bridge_task
    task, _inbox_bridge_task = _inbox_bridge_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("inbox bridge shutdown raised", exc_info=True)


app.add_event_handler("startup", _start_inbox_bridge)
app.add_event_handler("shutdown", _stop_inbox_bridge)


def get_terminal_service() -> TerminalService:
    global _terminal_service
    if _terminal_service is None:
        _terminal_service = TerminalService(
            idle_timeout_seconds=_env_int(TERMINAL_IDLE_TIMEOUT_ENV, 3600),
            max_sessions=_env_int(TERMINAL_MAX_SESSIONS_ENV, 8),
        )
    return _terminal_service


async def _start_terminal_service() -> None:
    if _terminal_enabled():
        get_terminal_service().start_reaper()


async def _stop_terminal_service() -> None:
    global _terminal_service
    service, _terminal_service = _terminal_service, None
    if service is not None:
        await service.shutdown()


app.add_event_handler("startup", _start_terminal_service)
app.add_event_handler("shutdown", _stop_terminal_service)


async def _reconcile_startup_dependencies_task() -> None:
    start = time.monotonic()
    try:
        from vibe import api

        result = await asyncio.to_thread(api.reconcile_startup_dependencies)
        show_runtime = result.get("show_runtime") if isinstance(result.get("show_runtime"), dict) else {}
        if show_runtime.get("ok"):
            from core.show_runtime import prewarm_show_page_session, prewarm_show_runtime

            prewarm = await prewarm_show_runtime()
            show_runtime["prewarmed"] = prewarm.available
            if not prewarm.available:
                show_runtime["reason"] = prewarm.reason or show_runtime.get("reason")
                result["ok"] = False
            else:
                targets = api.startup_show_page_prewarm_targets()
                page_results = []
                for page in targets.get("pages") or []:
                    session_id = str(page.get("session_id") or "")
                    if not session_id:
                        continue
                    session_prewarm = await prewarm_show_page_session(
                        session_id,
                        base_path=page.get("base_path") if isinstance(page.get("base_path"), str) else None,
                    )
                    page_results.append(
                        {
                            "session_id": session_id,
                            "ok": session_prewarm.available,
                            "reason": session_prewarm.reason,
                        }
                    )
                show_runtime["session_prewarm"] = {
                    "limit": targets.get("limit"),
                    "count": len(page_results),
                    "ok": sum(1 for item in page_results if item.get("ok")),
                    "failed": sum(1 for item in page_results if not item.get("ok")),
                }
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.get("skipped"):
            logger.info(
                "Startup dependency reconcile skipped in %sms: %s",
                duration_ms,
                result.get("reason") or "skipped",
            )
        elif result.get("ok"):
            logger.info("Startup dependencies reconciled in %sms", duration_ms)
        else:
            askill = result.get("askill") if isinstance(result.get("askill"), dict) else {}
            logger.warning(
                "Startup dependency reconcile completed with issues in %sms: askill=%s show_runtime=%s",
                duration_ms,
                askill.get("message") or askill.get("status") or askill.get("ok"),
                show_runtime.get("reason") or show_runtime.get("status") or show_runtime.get("ok"),
            )
    except Exception:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning("Startup dependency reconcile raised after %sms", duration_ms, exc_info=True)


async def _start_startup_dependency_reconcile() -> None:
    global _startup_dependency_reconcile_task
    if _startup_dependency_reconcile_task is None or _startup_dependency_reconcile_task.done():
        _startup_dependency_reconcile_task = asyncio.create_task(
            _reconcile_startup_dependencies_task(),
            name="startup-dependency-reconcile",
        )


async def _stop_startup_dependency_reconcile() -> None:
    global _startup_dependency_reconcile_task
    task, _startup_dependency_reconcile_task = _startup_dependency_reconcile_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("startup dependency reconcile shutdown raised", exc_info=True)


app.add_event_handler("startup", _start_startup_dependency_reconcile)
app.add_event_handler("shutdown", _stop_startup_dependency_reconcile)
app.add_event_handler("shutdown", stop_show_runtime_on_shutdown)


def _bind_ui_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if host and ":" in host else socket.AF_INET
    sock = socket.socket(family)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def run_ui_server(host: str, port: int) -> None:
    """Start the FastAPI UI server."""
    global _server
    import time
    import uvicorn

    paths.ensure_data_dirs()
    try:
        from core.services import settings as settings_service

        config = settings_service.load_config()
    except FileNotFoundError:
        config = None
    except Exception as exc:
        logger.warning("Skipping UI Sentry init because config load failed: %s", exc)
        config = None
    if config is not None:
        init_sentry(config, component="ui", enable_fastapi=True)
        try:
            from vibe import remote_access

            remote_access.start_status_heartbeat(config)
        except Exception:
            logger.warning("Failed to start remote access status heartbeat", exc_info=True)
    print(f"UI Server running at http://{host}:{port}")

    # Retry binding in case of TIME_WAIT or port still held by old server during reload
    for attempt in range(10):
        bound_socket: socket.socket | None = None
        try:
            uvicorn_config = uvicorn.Config(
                app,
                host=host,
                port=port,
                log_config=None,
                access_log=False,
                loop="asyncio",
                lifespan="on",
                workers=1,
            )
            bound_socket = _bind_ui_socket(host, port)
            _server = uvicorn.Server(uvicorn_config)
            # Reconcile remote_access in the background so cloudflared download/
            # connector start does not block /health and the rest of the UI
            # from coming up after restart/reload.
            threading.Thread(
                target=_reconcile_remote_access_for_ui_start,
                args=(config,),
                daemon=True,
                name="remote-access-reconcile-on-start",
            ).start()
            _server.run(sockets=[bound_socket])
            break
        except OSError as e:
            if bound_socket is not None:
                bound_socket.close()
            if e.errno == 48 and attempt < 9:  # Address already in use (macOS)
                print(f"Port {port} in use, retrying in 1s... (attempt {attempt + 1})")
                time.sleep(1)
            elif e.errno == 98 and attempt < 9:  # Address already in use (Linux)
                print(f"Port {port} in use, retrying in 1s... (attempt {attempt + 1})")
                time.sleep(1)
            else:
                raise
