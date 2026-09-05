import base64
import asyncio
import gzip
import hashlib
import hmac
import html
import inspect
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
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit

import psutil
from aiohttp import ClientConnectionError, ClientSession, WSMsgType
from fastapi import Request as FastAPIRequest, WebSocket, WebSocketDisconnect
from fastapi.responses import Response as FastAPIResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from vibe.ui_compat import (
    CompatApp,
    Response,
    TEST_REMOTE_ADDR_HEADER,
    g,
    is_json_content_type,
    jsonify,
    redirect,
    request,
    send_file,
)

from config import paths
from config.v2_config import CONFIG_LOCK, V2Config
from core.show_pages import (
    SHOW_CLI_EVENT_TOKEN_HEADER,
    SHOW_EVENT_WRITE_TOKEN_COOKIE,
    SHOW_EVENT_WRITE_TOKEN_HEADER,
    SHOW_PAGE_ICON_MAX_UPLOAD_BYTES,
    VISIBILITY_LIMITED,
    VISIBILITY_OFFLINE,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    ShowPage,
    show_cli_event_token,
    show_event_write_token,
    show_public_event_write_token,
)
from core.show_session_events import (
    HUMAN_EVENT_TYPES,
    ShowSessionEventError,
    localized_show_event_error,
    show_event_payload_session_mismatch,
    show_event_request_requests_dispatch,
    show_event_requests_dispatch,
)
from core.terminal_service import TERMINAL_SUPPORTED, TerminalService, TerminalServiceError, sanitize_session_id
from modules.agents.catalog import AGENT_BACKENDS, supports_runtime_refresh
from vibe.i18n import get_supported_languages, t
from vibe.logging_config import application_log_paths
from vibe.message_types import types_with
from vibe.model_service import MODEL_SERVICE_REFRESH_PATH
from vibe.runtime import get_ui_dist_path, get_working_dir
from vibe.sentry_integration import init_sentry
from storage.delivery_states import ADMITTED_DELIVERY_STATES
from vibe.ui_memory_routes import register_memory_routes

if TYPE_CHECKING:
    from core.show_runtime import ShowRuntimeUnavailableError

logger = logging.getLogger(__name__)


class _ShowEventDispatchOutcome(str, Enum):
    ACCEPTED = "accepted"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"


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


def _show_runtime_forwarded_headers(headers: Mapping[str, str]) -> dict[str, str]:
    from core.show_runtime import SHOW_RUNTIME_CONTEXT_HEADER, SHOW_RUNTIME_PROTOCOL_HEADER

    blocked = {SHOW_RUNTIME_PROTOCOL_HEADER.lower(), SHOW_RUNTIME_CONTEXT_HEADER.lower()}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in _SHOW_RUNTIME_REQUEST_HEADER_ALLOWLIST and key.lower() not in blocked
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
    "x-avibe-render-cache",
    "x-sourcemap",
}
_SHOW_RUNTIME_MODULE_SCRIPT_RE = re.compile(
    r"<script\b(?=[^>]*\btype\s*=\s*['\"]module['\"])[^>]*>",
    re.IGNORECASE,
)
_SHOW_RUNTIME_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_SHOW_PAGE_ASSET_SUFFIXES = frozenset(
    {
        ".avif",
        ".br",
        ".cjs",
        ".css",
        ".eot",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".jsx",
        ".map",
        ".mjs",
        ".mp3",
        ".mp4",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".svg",
        ".ts",
        ".tsx",
        ".ttf",
        ".wasm",
        ".wav",
        ".webm",
        ".webmanifest",
        ".webp",
        ".woff",
        ".woff2",
        ".xml",
        ".zip",
    }
)
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
_AUTHORIZATION_LOGIN_REQUIRED_WEBSOCKET_CLOSE_CODE = 4401
_AUTHORIZATION_REVOKED_WEBSOCKET_CLOSE_CODE = 4403
_AUTHORIZATION_UNAVAILABLE_WEBSOCKET_CLOSE_CODE = 4503
_AUTHORIZATION_CHANGED_WEBSOCKET_CLOSE_CODE = 1012
_AUTHORIZATION_REVISION_RECHECK_SECONDS = 1.0
# How often ``GET /api/events`` proves the stream is alive. It doubles as the
# proxy keep-alive -- Cloudflare Tunnel's default idle is well below 100s, and
# mid-tier proxies are happier still with something this short.
WORKBENCH_EVENT_HEARTBEAT_INTERVAL_S = 15.0
_TRUE_BOOL_STRINGS = {"1", "true", "yes", "on"}

STRUCTURED_LOG_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+-\s+([\w.]+)\s+-\s+(\w+)\s+-\s+(.*)$")
LEVEL_HINT_PATTERN = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
TRACEBACK_EXCEPTION_PATTERN = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|Failure|Fault|Group)(?:[:(]|$)"
)
CSRF_COOKIE_NAME = "vibe_csrf_token"
CSRF_HEADER_NAME = "X-Vibe-CSRF-Token"
VAULT_SANDBOX_ORIGIN = "https://sandbox.avibe.bot"
# WebAuthn is delegated to the cross-origin sandbox iframe via Permissions-Policy. Chrome only
# honors the delegation when the allowlist includes `self`: an origin-only allowlist
# (`("origin")`, no self) does NOT reach the cross-origin child — verified empirically against
# the sandbox iframe's DevTools "Allowed Features". So get + create both include self.
#
# Accepted trade-off (product decision): with `self`, the parent origin can also invoke the
# get/create ceremonies, so a parent-app XSS could — via a user-approved passkey prompt — derive
# the VMK. We intentionally do NOT defend against that. The sandbox's purpose is that the VMK /
# PRF output / private keys / plaintext are only ever handled *inside* the sandbox origin, and the
# agent (a backend process, never in the browser) can never reach them — those guarantees hold
# regardless of self. Forcing a top-level popup on every unlock to close a
# browser-XSS-social-engineering gap would wreck UX for a strictly secondary threat.
VAULT_SANDBOX_PERMISSIONS_POLICY = (
    f'publickey-credentials-get=(self "{VAULT_SANDBOX_ORIGIN}"), '
    f'publickey-credentials-create=(self "{VAULT_SANDBOX_ORIGIN}"), '
    f'clipboard-write=(self "{VAULT_SANDBOX_ORIGIN}")'
)
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
TRUSTED_PROXY_IPS_ENV = "VIBE_UI_TRUSTED_PROXY_IPS"
TRUSTED_PUBLIC_ORIGINS_ENV = "VIBE_UI_TRUSTED_PUBLIC_ORIGINS"
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

    if pid_path == paths.get_runtime_pid_path():
        was_running = runtime.resolve_service_owner_pid() is not None or bool(runtime.extra_service_process_pids())
        stopped = runtime.stop_service()
    else:
        was_running = _runtime_pid_file_points_to_live_process(pid_path)
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

    config = _load_remote_access_config()
    trusted_forwarded_origin = _trusted_forwarded_origin(default_scheme=scheme)
    if trusted_forwarded_origin and _trusted_public_origin_local_request(config):
        return trusted_forwarded_origin

    if config is not None and _is_remote_access_request(config):
        remote_origin = _remote_access_request_origin(config)
        if remote_origin:
            return remote_origin

    if trusted_forwarded_origin is None:
        return f"{scheme}://{netloc}"

    return trusted_forwarded_origin


def _trusted_forwarded_origin(*, default_scheme: str | None = None) -> str | None:
    trusted_forwarded_host = _trusted_forwarded_host()
    if trusted_forwarded_host is None:
        return None
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if forwarded_proto not in {"http", "https"}:
        forwarded_proto = (default_scheme or "").lower()
    if forwarded_proto in {"http", "https"}:
        return f"{forwarded_proto}://{trusted_forwarded_host}"
    return None


def _effective_request_host() -> str:
    return _trusted_forwarded_host() or request.host


def _trusted_forwarded_host() -> str | None:
    if not _is_explicitly_trusted_proxy_peer():
        return None
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if not _forwarded_host_is_safe(forwarded_host):
        return None
    if _forwarded_host_has_explicit_port(forwarded_host):
        return forwarded_host
    forwarded_port = _trusted_forwarded_port()
    if forwarded_port is None:
        return forwarded_host
    return f"{forwarded_host}:{forwarded_port}"


def _trusted_forwarded_port() -> int | None:
    raw_port = request.headers.get("X-Forwarded-Port", "").split(",")[0].strip()
    if not raw_port:
        return None
    if not raw_port.isdigit():
        return None
    port = int(raw_port)
    if port < 1 or port > 65535:
        return None
    return port


def _has_trusted_forwarded_metadata() -> bool:
    return _is_explicitly_trusted_proxy_peer() and _has_forwarded_metadata()


def _has_untrusted_forwarded_metadata() -> bool:
    return _has_forwarded_metadata() and not _is_explicitly_trusted_proxy_peer()


def _effective_loopback_host() -> bool:
    return _is_loopback_host(_effective_request_host())


def _effective_normalized_host() -> str:
    return _normalized_host(_effective_request_host())


def _trusted_forwarded_origin_identity() -> tuple[str, str, int | None] | None:
    trusted_forwarded_host = _trusted_forwarded_host()
    if trusted_forwarded_host is None:
        return None
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if forwarded_proto not in {"http", "https"}:
        return None
    return (
        forwarded_proto,
        _normalized_host(trusted_forwarded_host),
        _origin_port(trusted_forwarded_host, forwarded_proto),
    )


def _trusted_forwarded_client_address() -> ipaddress._BaseAddress | None:
    if not _is_explicitly_trusted_proxy_peer():
        return None
    raw_client = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not raw_client:
        return None
    try:
        address = ipaddress.ip_address(raw_client)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _local_trust_peer_address() -> ipaddress._BaseAddress | None:
    if not _has_trusted_forwarded_metadata():
        return _request_peer_address()
    if _trusted_forwarded_host() is None:
        return None
    return _trusted_forwarded_client_address()


def _is_explicitly_trusted_proxy_peer() -> bool:
    configured = os.environ.get(TRUSTED_PROXY_IPS_ENV, "")
    if not configured.strip():
        return False
    peer = _request_peer_address()
    if peer is None:
        return False
    for raw_entry in configured.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("Ignoring invalid %s entry: %s", TRUSTED_PROXY_IPS_ENV, entry)
            continue
        if peer in network:
            return True
    return False


def _forwarded_host_is_safe(value: str) -> bool:
    if not value or any(ch.isspace() for ch in value):
        return False
    if "/" in value or "\\" in value or "@" in value:
        return False
    try:
        parsed = urlparse(f"//{value}")
        parsed.port
    except ValueError:
        return False
    return bool(parsed.netloc and parsed.hostname and not parsed.username and not parsed.password)


def _forwarded_host_has_explicit_port(value: str) -> bool:
    try:
        return urlparse(f"//{value}").port is not None
    except ValueError:
        return False


def _trusted_public_origin_entries(config: V2Config | None) -> list[str]:
    values: list[str] = []
    configured = getattr(getattr(config, "ui", None), "trusted_public_origins", None)
    if isinstance(configured, str):
        values.extend(configured.split(","))
    elif isinstance(configured, (list, tuple, set)):
        values.extend(str(value) for value in configured)
    values.extend(os.environ.get(TRUSTED_PUBLIC_ORIGINS_ENV, "").split(","))
    return [value.strip() for value in values if value and value.strip()]


def _trusted_public_origin_entry_matches(
    value: str,
    *,
    scheme: str,
    host: str,
    port: int | None,
) -> bool:
    try:
        parsed = urlparse(value)
        explicit_port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        logger.warning("Ignoring invalid %s entry: %s", TRUSTED_PUBLIC_ORIGINS_ENV, value)
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        logger.warning("Ignoring invalid %s entry: %s", TRUSTED_PUBLIC_ORIGINS_ENV, value)
        return False
    entry_host = _normalized_host(hostname)
    if entry_host != host:
        return False
    entry_scheme = parsed.scheme.lower()
    return scheme == entry_scheme and port == (explicit_port or _origin_port(parsed.netloc, entry_scheme))


def _trusted_public_origin_matches(
    config: V2Config | None,
    identity: tuple[str, str, int | None] | None,
) -> bool:
    if identity is None:
        return False
    scheme, host, port = identity
    if not host:
        return False
    return any(
        _trusted_public_origin_entry_matches(value, scheme=scheme, host=host, port=port)
        for value in _trusted_public_origin_entries(config)
    )


def _trusted_public_origin_allowed_for_peer(
    config: V2Config | None,
    peer_address: ipaddress._BaseAddress | None,
) -> bool:
    if config is None:
        return True
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    if not getattr(cloud, "enabled", False):
        return True
    if peer_address is not None and not peer_address.is_loopback:
        return True
    logger.warning(
        "Ignoring %s for loopback trusted proxy while remote access is enabled; "
        "use a non-loopback proxy peer, disable remote access, or enforce authentication before the proxy.",
        TRUSTED_PUBLIC_ORIGINS_ENV,
    )
    return False


def _trusted_public_origin_local_request(config: V2Config | None) -> bool:
    if not _has_trusted_forwarded_metadata():
        return False
    if not _trusted_public_origin_matches(config, _trusted_forwarded_origin_identity()):
        return False
    return _trusted_public_origin_allowed_for_peer(
        config,
        _request_peer_address(),
    )


def _is_mutation_guard_exempt() -> bool:
    if request.path in {
        "/auth/callback",
        "/auth/show-identity/callback",
    }:
        return True
    if (
        _is_cli_show_event_request()
        or _is_cli_session_activity_request()
        or _is_cli_model_service_refresh_request()
    ):
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


def _is_cli_model_service_refresh_request() -> bool:
    return _cli_local_event_token_ok() and request.path == MODEL_SERVICE_REFRESH_PATH


def _is_show_api_mutation() -> bool:
    if not (request.path.startswith("/show/") or request.path.startswith("/p/")):
        return False
    return "/api/" in request.path or "/__show/" in request.path


def _ensure_csrf_cookie(response: Response) -> Response:
    if _is_current_immutable_static_asset_request():
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
    if _has_trusted_forwarded_metadata():
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
    return _is_private_peer_address(_request_peer_address())


def _is_private_peer_address(address: ipaddress._BaseAddress | None) -> bool:
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


def _peer_shares_setup_host_network(
    setup_address: ipaddress._BaseAddress,
    peer: ipaddress._BaseAddress | None = None,
) -> bool:
    """Require the peer to share setup_host's interface-level subnet.

    Compensates for the wildcard bind in the tunnel-on path. Without this,
    a 192.168/16 LAN peer could spoof ``Host=<tailscale_setup_host>`` on
    a different interface and inherit setup-host trust. Subnet size comes
    from :func:`_setup_host_trust_network`, which keeps overlay networks
    (Tailscale, link-local) broad and otherwise mirrors the actual
    interface netmask via :func:`_local_interface_network`.
    """
    peer = peer or _request_peer_address()
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
        # Modern `tailscale whois --json` nests the peer record under "Node"
        # (where "Machine" is just the machine-key string); older builds used a
        # top-level "Machine" object. Accept a dict from either shape.
        machine = payload.get("Machine") or payload.get("machine")
        if not isinstance(machine, dict):
            machine = payload.get("Node") or payload.get("node") or {}
        if isinstance(machine, dict):
            addresses = set()
            for raw_address in _json_list(machine, "Addresses", "addresses"):
                # "Node" payloads list addresses as host CIDRs ("100.64.0.2/32");
                # older "Machine" payloads used bare IPs.
                try:
                    interface = ipaddress.ip_interface(str(raw_address))
                except ValueError:
                    continue
                if interface.network.prefixlen == interface.network.max_prefixlen:
                    addresses.add(interface.ip)
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
    if _effective_normalized_host() != setup_host:
        return False
    # Any forwarded header (including non-Cloudflare proxies like nginx /
    # Caddy / Traefik) means we cannot trust request.remote_addr to identify
    # the actual client, so refuse the setup-host trust path entirely.
    if _has_untrusted_forwarded_metadata():
        return False
    peer_address = _local_trust_peer_address()
    if not _is_private_peer_address(peer_address):
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
        return _peer_shares_setup_host_network(setup_address, peer_address)
    return True


def _is_tunnel_wildcard_bind(config: V2Config) -> bool:
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    return bool(cloud is not None and cloud.enabled)


def _is_local_request(config: V2Config | None = None) -> bool:
    if _has_untrusted_forwarded_metadata():
        return False
    if _has_trusted_forwarded_metadata() and _trusted_forwarded_host() is None:
        return False
    if _trusted_public_origin_local_request(config):
        return True
    if not _has_trusted_forwarded_metadata() and _is_loopback_peer() and _effective_loopback_host():
        return True
    if _is_trusted_docker_loopback_request():
        return True
    return _is_setup_host_request(config)


def is_direct_loopback_memory_request() -> bool:
    """Strict Memory-only browser admission, intentionally narrower than UI local.

    Memory content and settings never accept proxy forwarding, Docker bridge
    allowances, LAN setup hosts, or remote-access cookies. The browser must be
    directly connected over loopback and present a same-origin header.
    """

    if _has_forwarded_metadata() or not _is_loopback_peer() or not _is_loopback_host(request.host):
        return False
    origin = _request_origin(request.headers.get("Origin")) or _request_origin(request.headers.get("Referer"))
    return bool(origin and _same_origin(origin, request.host_url.rstrip("/")))


def memory_ui_user_key() -> str | None:
    """Resolve the Memory principal for a trusted browser request.

    Direct loopback keeps the install-local identity. Remote browser access is
    admitted only through the configured Avibe Cloud origin with a valid signed
    session cookie; LAN and arbitrary proxy routes remain closed. Reads require
    the same origin evidence as mutations so a remote session cookie cannot be
    used as a cross-origin Memory oracle.
    """

    if is_direct_loopback_memory_request():
        return "avibe:local"
    config = _load_remote_access_config()
    if config is None or not _is_remote_access_request(config):
        return None
    source = _request_origin(request.headers.get("Origin")) or _request_origin(
        request.headers.get("Referer")
    )
    if not source or not _same_origin(source, _current_origin()):
        return None
    try:
        payload = _resolved_remote_session_payload(config)
    except Exception:
        return None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(subject, str) or not subject.strip():
        return None
    return f"avibe:remote:{subject.strip()}"


def _resolved_remote_session_payload(config: V2Config) -> dict[str, Any] | None:
    existing = getattr(g, "remote_session_payload", None)
    if isinstance(existing, dict):
        return existing
    from vibe import remote_access

    identity = remote_access.parse_session_identity(
        config,
        request.cookies.get(remote_access.SESSION_COOKIE_NAME),
    )
    if identity is None:
        return None
    resolution = remote_access.resolve_current_authorization(config, identity)
    return resolution.payload if resolution.current else None


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
    return _remote_access_host_allowed(config, _effective_normalized_host())


_REMOTE_ACCESS_STATUS_PUBLIC_FIELDS = (
    "ok",
    "provider",
    "enabled",
    "public_url",
    "paired",
    "running",
    "pid_state",
    "transport_protocol",
    "settings",
    "tunnel_quality",
    "network_path",
)


def _remote_access_allowed_hosts(config: V2Config) -> frozenset[str]:
    public_host = _remote_access_public_host(config)
    if not public_host:
        return frozenset()
    from vibe import remote_access

    return frozenset({public_host, *remote_access.active_hostnames(config)})


def _remote_access_host_allowed(config: V2Config, host: str | None) -> bool:
    normalized = _normalized_host(host)
    return bool(normalized and normalized in _remote_access_allowed_hosts(config))


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


def _remote_access_request_origin(config: V2Config) -> str | None:
    host = _effective_normalized_host()
    if not _remote_access_host_allowed(config, host):
        return None
    if host == _remote_access_public_host(config):
        return _remote_access_public_origin(config)
    return f"https://{host}"


def _remote_access_oauth_redirect_uri(config: V2Config) -> str:
    host = _effective_normalized_host()
    if _remote_access_host_allowed(config, host):
        return f"https://{host}/auth/callback"
    return config.remote_access.vibe_cloud.redirect_uri


def _origin_identity(value: str) -> tuple[str, str, int | None] | None:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        return None
    return (parsed.scheme.lower(), _normalized_host(parsed.hostname), _origin_port(parsed.netloc, parsed.scheme))


def _same_origin(left: str, right: str) -> bool:
    left_identity = _origin_identity(left)
    right_identity = _origin_identity(right)
    return left_identity is not None and left_identity == right_identity


def _remote_access_public_origin_matches(origin: str, config: V2Config) -> bool:
    identity = _origin_identity(origin)
    if identity is None:
        return False
    scheme, host, _ = identity
    if scheme != "https" or not _remote_access_host_allowed(config, host):
        return False
    if host == _remote_access_public_host(config):
        trusted_origin = _remote_access_public_origin(config)
    else:
        trusted_origin = f"https://{host}"
    return bool(trusted_origin and _same_origin(origin, trusted_origin))


def _remote_access_public_url_invalid(config: V2Config) -> bool:
    cloud = config.remote_access.vibe_cloud
    return bool(cloud.enabled and not _remote_access_public_origin(config))


def _remote_access_settings_changed(previous: V2Config | None, current: V2Config, payload: dict) -> bool:
    if "remote_access" not in payload:
        return False
    from vibe import api

    return api.remote_access_runtime_changed(previous, current)


def _should_rotate_remote_session_secret(previous: V2Config | None, current: V2Config, payload: dict) -> bool:
    if "remote_access" not in payload or previous is None:
        return False
    previous_cloud = previous.remote_access.vibe_cloud
    current_cloud = current.remote_access.vibe_cloud
    return bool(previous_cloud.enabled and not current_cloud.enabled and current_cloud.session_secret)


def _activity_streaming_flag_touched(payload: dict) -> bool:
    ui_payload = payload.get("ui")
    return isinstance(ui_payload, dict) and "show_agent_activity" in ui_payload


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
    # The list-operations verb mutates the enabled list without carrying a
    # literal ``platforms`` section. Treat it as a platforms edit so
    # enable/disable toggles still reach the comparison below, but do not
    # infer a runtime change from the verb alone: Finish may replay an
    # already-applied operation.
    from vibe.api import _LIST_OPS_PAYLOAD_KEY

    has_list_ops = _LIST_OPS_PAYLOAD_KEY in payload
    if (
        not has_list_ops
        and "platforms" not in payload
        and "platform" not in payload
        and not any(key in payload for key in platform_config_keys)
    ):
        return False
    return (
        set(previous.platforms.enabled) != set(current.platforms.enabled)
        or previous.platforms.primary != current.platforms.primary
        or _platform_runtime_signature(previous) != _platform_runtime_signature(current)
    )


def _changed_agent_backend_runtimes(
    previous: V2Config | None,
    current: V2Config,
    payload: dict,
) -> list[str]:
    """Return backends whose persisted runtime projection changed."""
    if previous is None or "agents" not in payload:
        return []

    from config.v2_compat import to_app_config
    from modules.agents.catalog import AGENT_BACKENDS

    previous_runtime = to_app_config(previous)
    current_runtime = to_app_config(current)
    return [
        backend
        for backend in AGENT_BACKENDS
        if getattr(previous_runtime, backend, None) != getattr(current_runtime, backend, None)
    ]


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
        or path == "/auth/login"
        or path == "/auth/callback"
        or path == "/auth/show-identity/callback"
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
        request.path
        in {
            "/auth/callback",
            "/auth/logout",
            "/api/session",
            "/api/csrf-token",
        }
        or request.path.startswith("/assets/")
        or request.path.startswith(f"{_SHOW_RUNTIME_VENDOR_PREFIX}/")
        or request.path
        in {
            _SHOW_RUNTIME_PUBLIC_CLIENT_SHIM_PATH,
            _SHOW_RUNTIME_PUBLIC_REACT_REFRESH_SHIM_PATH,
        }
        or request.path == "/favicon.ico"
    )


def _is_ui_static_request() -> bool:
    endpoint = request._request.scope.get("endpoint")
    return getattr(endpoint, "__name__", "") == "serve_static_compat_endpoint"


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


def _oauth_retry_requested(value: Any) -> bool:
    target = _safe_remote_redirect_target(value)
    return any(
        key == REMOTE_OAUTH_RETRY_PARAM and val == "1"
        for key, val in parse_qsl(urlsplit(target).query, keep_blank_values=True)
    )


def _add_oauth_retry_param(value: str) -> str:
    target = _strip_oauth_retry_param(value)
    parsed = urlsplit(target)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params.append((REMOTE_OAUTH_RETRY_PARAM, "1"))
    return urlunsplit(("", "", parsed.path or "/", urlencode(params), ""))


def _oauth_callback_arg(name: str) -> str | None:
    return request.args.get(name) or request.args.get(f"amp;{name}")


def _redirect_to_vibe_cloud_login(
    config: V2Config,
    *,
    next_target: Any | None = None,
):
    from vibe import remote_access

    cloud = config.remote_access.vibe_cloud
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    raw_next = (
        next_target
        if next_target is not None
        else (request.full_path if request.query_string else request.path)
    )
    next_target = _strip_oauth_retry_param(raw_next)
    rid = secrets.token_urlsafe(18)
    state = _make_oauth_state(
        cloud.session_secret,
        next_target=next_target,
        retry=_oauth_retry_requested(raw_next),
        rid=rid,
    )
    nonce = secrets.token_urlsafe(24)
    redirect_uri = _remote_access_oauth_redirect_uri(config)
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
        redirect_uri=redirect_uri,
    )
    oauth_cookie = _make_oauth_cookie(
        cloud.session_secret,
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next": next_target,
            "redirect_uri": redirect_uri,
            "exp": int(datetime.now().timestamp()) + REMOTE_OAUTH_HANDSHAKE_TTL_SECONDS,
        },
    )
    response = Response(status=302)
    response.headers["Location"] = remote_access.authorization_url(
        config,
        state,
        nonce,
        code_challenge,
        redirect_uri=redirect_uri,
    )
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


def _log_oauth_diag(key: str, message: str, *args: Any, exc_info: BaseException | None = None) -> None:
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
    logger.warning(message + extra, *args, exc_info=exc_info)


def _log_oauth_callback_failure(stage: str, exc: BaseException) -> None:
    """Log one failed OAuth callback stage so the cause stays attributable.

    ``OAuthCodeExchangeError`` is the expected shape: it carries its own reason
    and detail, and any unauthenticated caller can produce one at will. Anything
    else is a bug or an environment fault (a locked, full, or read-only database
    all arrive as a bare ``OperationalError``) whose only remaining description
    is the traceback. Those get one — in the service log, never in the response,
    which still exposes just the exception class name — plus a rate-limit budget
    of their own, so a flood of bad codes cannot suppress the line that matters.
    """

    from vibe import remote_access

    expected = isinstance(exc, remote_access.OAuthCodeExchangeError)
    reason = exc.reason if expected else exc.__class__.__name__
    _log_oauth_diag(
        f"{stage}_{'rejected' if expected else 'error'}",
        "vibe cloud oauth %s failed: reason=%s",
        stage,
        reason,
        # The exception object, not ``True``: ``True`` reads ambient
        # ``sys.exc_info()`` and silently logs nothing outside a live handler.
        exc_info=None if expected else exc,
    )


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

    Trust forwarded client IPs only on proxy paths we explicitly trust: the
    configured trusted proxy chain with an accepted forwarded host, or the local
    Cloudflare tunnel peer. A direct peer reaching the origin port could
    otherwise set/rotate forwarded headers to dodge the limit, so for such peers
    we key on the real connecting address instead.
    """
    if _has_trusted_forwarded_metadata() and _trusted_forwarded_host() is not None:
        forwarded_client = _trusted_forwarded_client_address()
        if forwarded_client is not None:
            return f"xff:{forwarded_client.compressed}"

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
def reject_disabled_model_hub_api():
    """Keep the unreleased Model Hub REST surface dormant by default."""

    from config.v2_config import is_model_hub_enabled
    from core.handlers.model_hub.service import CONTRACT_VERSION

    if request.path.startswith("/api/models/") and not is_model_hub_enabled():
        return jsonify(
            {
                "ok": False,
                "contract_version": CONTRACT_VERSION,
                "error": "feature_disabled",
            }
        ), 404
    return None


@app.before_request
def enforce_remote_access_cookie():
    config = _load_remote_access_config()
    markdown_show_request = _is_private_show_page_markdown_request()
    if _remote_auth_exempt_before_host_validation():
        return None
    from vibe.authorization import context_from_session_payload, instance_owner_context

    local_request = _is_local_request(config)
    docker_probe_request = _is_trusted_docker_loopback_probe()
    if config is None:
        if local_request or docker_probe_request:
            g.authorization_context = instance_owner_context()
            return None
        return jsonify({"ok": False, "error": "remote_access_config_unavailable"}), 503
    if _remote_access_public_url_invalid(config) and not (local_request or docker_probe_request):
        return jsonify({"ok": False, "error": "remote_access_public_url_invalid"}), 503
    remote_request = _is_remote_access_request(config)
    if not remote_request:
        if _is_loopback_origin_proxy_request():
            g.authorization_context = instance_owner_context()
            return None
        if not local_request and not docker_probe_request:
            return jsonify({"ok": False, "error": "remote_access_host_mismatch"}), 503
        g.authorization_context = instance_owner_context()
        return None
    if _trusted_public_origin_local_request(config):
        g.authorization_context = instance_owner_context()
        return None
    if _remote_auth_exempt_path():
        return None
    from vibe import remote_access

    if not config.remote_access.vibe_cloud.enabled:
        return jsonify({"ok": False, "error": "remote_access_disabled"}), 503
    if not config.remote_access.vibe_cloud.session_secret:
        return jsonify({"ok": False, "error": "remote_access_session_secret_missing"}), 503
    identity = remote_access.parse_session_identity(
        config,
        request.cookies.get(remote_access.SESSION_COOKIE_NAME),
    )
    if identity is not None:
        resolution = remote_access.resolve_current_authorization(config, identity)
        if resolution.state == "revoked":
            if _is_ui_static_request():
                g.remote_session_identity = identity
                g.remote_authorization_resolution = resolution
                return None
            if markdown_show_request:
                return _show_page_markdown_error_response("forbidden", 403)
            return jsonify({"ok": False, "error": "remote_access_revoked"}), 403
        if resolution.state == "unavailable":
            if _is_ui_static_request():
                g.remote_session_identity = identity
                g.remote_authorization_resolution = resolution
                return None
            return jsonify(
                {"ok": False, "error": "remote_access_authorization_unavailable"}
            ), 503
        payload = resolution.payload if resolution.current else None
    else:
        resolution = None
        payload = None
    if payload is not None:
        context = context_from_session_payload(payload)
        g.authorization_context = context
        g.remote_session_identity = identity
        g.remote_session_payload = payload
        g.remote_authorization_resolution = resolution
        if remote_access.session_needs_renewal(payload):
            g.remote_session_renew = payload
        return None
    # The SPA shell is non-sensitive and its APIs remain protected. Serving it
    # lets AuthGuard keep an iOS Home-Screen cold launch on the installed app's
    # origin instead of automatically crossing into an OAuth browser sheet.
    if _is_ui_static_request():
        return None
    if markdown_show_request:
        return _show_page_markdown_error_response("authentication_required", 401)
    if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
        target = request.full_path if request.query_string else request.path
        return redirect(f"/auth/login?{urlencode({'next': _safe_remote_redirect_target(target)})}")
    return jsonify({"ok": False, "error": "remote_access_login_required"}), 401


def _request_authorization_context(context: Any = None):
    if context is not None:
        return context
    try:
        resolved = getattr(g, "authorization_context", None)
    except (LookupError, RuntimeError):
        resolved = None
    return resolved


def _has_runtime_owner_access(context: Any) -> bool:
    return bool(context is not None and context.is_instance_owner)


def _access_administration_forbidden(context: Any = None):
    """Return a 403 response unless the caller may administer instance access.

    The route policy table (``authorization._ACCESS_ADMINISTRATION_HTTP_RULES``)
    is one layer; this is the one that travels with the handler, so a route
    re-registered under a different path keeps the gate. Both answer the same
    question: may this caller change who reaches the instance? The member set is
    cloud allowlist entries *and* multi-platform IM bound users, so IM bind codes
    and bound-user mutation are member management.
    """

    resolved = _request_authorization_context(context)
    if resolved is not None and resolved.can_manage_access_members:
        return None
    return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403


def _runtime_record_session_id(record: Any) -> str | None:
    if not isinstance(record, Mapping):
        return None
    value = str(record.get("session_id") or "").strip()
    return value or None


def _runtime_record_agent_refs(record: Any) -> tuple[str | None, str | None]:
    if not isinstance(record, Mapping):
        return None, None
    agent_id = str(record.get("agent_id") or "").strip() or None
    agent_name = str(record.get("agent_name") or "").strip() or None
    return agent_id, agent_name


def _runtime_record_visible(context: Any, record: Any, *, connection: Any | None = None) -> bool:
    """Return whether a Project-bound Agent runtime record is authorized.

    Owners see every record. Everyone else must pass both the Project ACL for
    the bound session (when one exists) and the Agent ACL for the selected
    Agent (when one exists). Harness definitions and runs intentionally do not
    use this helper because Harness has no additional resource ACL in this MVP.
    """

    if context is None:
        return False
    if _has_runtime_owner_access(context):
        return True
    session_id = _runtime_record_session_id(record)
    agent_id, agent_name = _runtime_record_agent_refs(record)
    if session_id is None and agent_id is None and agent_name is None:
        return False
    if session_id is not None and not _project_session_access_allowed(context, session_id, "editor"):
        return False
    if agent_id is None and agent_name is None:
        return True
    from core.vibe_agents import VibeAgentAccessError, ensure_agent_selection_access

    def _check(conn: Any) -> bool:
        try:
            ensure_agent_selection_access(
                conn,
                agent_name=agent_name,
                agent_id=agent_id,
                user_context=context,
            )
        except VibeAgentAccessError:
            return False
        return True

    if connection is not None:
        return _check(connection)
    engine = _projects_engine()
    with engine.connect() as conn:
        return _check(conn)


def _filter_runtime_records(context: Any, records: list[Any] | tuple[Any, ...] | None) -> list[Any]:
    return [record for record in (records or []) if _runtime_record_visible(context, record)]


def _running_agent_counts(agents: list[Any] | tuple[Any, ...] | None) -> dict[str, Any]:
    """Recompute the frozen RunningAgentCounts shape from authorized rows."""

    states = {"active": 0, "idle": 0, "orphan": 0}
    by_backend: dict[str, int] = {}
    rows = [row for row in (agents or []) if isinstance(row, Mapping)]
    for row in rows:
        state = str(row.get("state") or "")
        if state in states:
            states[state] += 1
        backend = str(row.get("backend") or "").strip()
        if backend:
            by_backend[backend] = by_backend.get(backend, 0) + 1
    return {
        "total": len(rows),
        "active": states["active"],
        "idle": states["idle"],
        "orphan": states["orphan"],
        "by_backend": by_backend,
    }


def _authorized_graph_payload(context: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if _has_runtime_owner_access(context):
        return payload
    from core.services.agent_graph import _counts as graph_counts

    visible_nodes = _filter_runtime_records(context, payload.get("nodes") or [])
    visible_ids = {
        str(node.get("session_id") or "")
        for node in visible_nodes
        if node.get("session_id")
    }
    payload["nodes"] = visible_nodes
    payload["edges"] = [
        edge
        for edge in (payload.get("edges") or [])
        if (
            edge.get("kind") == "trigger"
            and str(edge.get("to") or "") in visible_ids
        )
        or (
            edge.get("kind") != "trigger"
            and str(edge.get("from") or "") in visible_ids
            and str(edge.get("to") or "") in visible_ids
        )
    ]
    visible_trigger_ids = {
        str(edge.get("from") or "").removeprefix("def:")
        for edge in payload["edges"]
        if edge.get("kind") == "trigger"
    }
    payload["trigger_nodes"] = [
        node
        for node in (payload.get("trigger_nodes") or [])
        if str(node.get("definition_id") or "") in visible_trigger_ids
    ]
    payload["counts"] = graph_counts(visible_nodes)
    return payload


def _require_runtime_record(context: Any, record: Any, *, not_found: tuple[dict[str, Any], int]):
    if record is None:
        return not_found
    if not _runtime_record_visible(context, record):
        return not_found
    return None


@app.before_request
def enforce_instance_role_capabilities():
    if _remote_auth_exempt_path():
        return None
    from vibe.authorization import (
        InstanceAuthorizationError,
        http_authorization_policy,
        require_instance_role,
    )

    policy = http_authorization_policy(
        request.method,
        request.path,
    )
    g.http_authorization_policy = policy
    minimum_role = policy.minimum_role
    if minimum_role is None:
        return None
    try:
        context = getattr(g, "authorization_context", None)
    except (LookupError, RuntimeError):
        # This helper is also exercised by pure policy tests outside a request.
        context = None
    try:
        if context is None:
            raise InstanceAuthorizationError(minimum_role)
        require_instance_role(context, minimum_role)
    except InstanceAuthorizationError:
        return jsonify(
            {
                "ok": False,
                "error": "instance_access_forbidden",
                "required_role": minimum_role,
            }
        ), 403
    return None


def _permissions_error_response(error: Exception):
    from vibe import permissions

    if isinstance(error, permissions.PermissionsNotPairedError):
        return jsonify({"ok": False, "error": "permissions_not_paired"}), 409
    if isinstance(error, permissions.PermissionsPairingChangedError):
        return jsonify({"ok": False, "error": "permissions_pairing_changed"}), 409
    if isinstance(error, permissions.PermissionsUnavailableError):
        return jsonify(
            {"ok": False, "error": "permissions_unavailable", "offline": True}
        ), 503
    if isinstance(error, permissions.PermissionsBackendError):
        return jsonify({"ok": False, **error.payload}), error.status
    if isinstance(error, permissions.PermissionsInvalidResponseError):
        return jsonify({"ok": False, "error": str(error)}), 502
    if isinstance(error, permissions.PermissionsInvalidRequestError):
        return jsonify({"ok": False, "error": str(error)}), 422
    logger.warning("Permissions request failed: %s", error.__class__.__name__)
    return jsonify({"ok": False, "error": "permissions_unavailable"}), 503


def _permissions_mutation_payload(
    payload: Any,
    allowed_keys: set[str],
    item_shapes: dict[str, frozenset[str]],
):
    if not isinstance(payload, dict) or set(payload) != allowed_keys:
        return None
    expected_instance_id = payload.get("if_match_instance_id")
    if not isinstance(expected_instance_id, str) or not expected_instance_id:
        return None
    for field, allowed_item_keys in item_shapes.items():
        items = payload.get(field)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or set(item) != allowed_item_keys
            for item in items
        ):
            return None
    return payload


def _resource_access_mutation_payload(payload: Any):
    if not isinstance(payload, dict) or set(payload) != {
        "access_level",
        "group_ids",
        "if_match_revision",
        "if_match_instance_id",
    }:
        return None
    access_level = payload.get("access_level")
    group_ids = payload.get("group_ids")
    revision = payload.get("if_match_revision")
    instance_id = payload.get("if_match_instance_id")
    if (
        access_level not in {"private", "public", "scope"}
        or not isinstance(group_ids, list)
        or any(not isinstance(group_id, str) or not group_id.strip() for group_id in group_ids)
        or len(group_ids) != len(set(group_ids))
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(instance_id, str)
        or not instance_id
    ):
        return None
    if (access_level == "scope") != bool(group_ids):
        return None
    return payload


@app.get("/api/permissions", include_in_schema=False)
async def current_instance_permissions_get(starlette_request: FastAPIRequest):
    async def handler():
        from vibe import permissions

        authorization_context = getattr(g, "authorization_context", None)
        if authorization_context is None or not authorization_context.can_read_instance:
            return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
        try:
            result = await asyncio.to_thread(permissions.get_current_permissions)
            response = jsonify(permissions.response_payload(result))
            response.headers["Cache-Control"] = "private, no-store"
            return response
        except Exception as error:
            return _permissions_error_response(error)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.put("/api/permissions/authorized-users", include_in_schema=False)
async def current_instance_permissions_authorized_users_put(
    starlette_request: FastAPIRequest,
):
    async def handler():
        from vibe import permissions

        authorization_context = getattr(g, "authorization_context", None)
        if authorization_context is None or not authorization_context.can_manage_access_members:
            return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
        try:
            body = await starlette_request.body()
            raw_payload = await starlette_request.json() if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw_payload = None
        payload = _permissions_mutation_payload(
            raw_payload,
            {"entries", "if_match_revision", "if_match_instance_id"},
            {"entries": frozenset({"kind", "value", "role"})},
        )
        if payload is None:
            return jsonify({"ok": False, "error": "invalid_request"}), 422
        try:
            result = await asyncio.to_thread(permissions.replace_authorized_users, payload)
            return jsonify(result)
        except Exception as error:
            return _permissions_error_response(error)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.put("/api/permissions/projects/{project_id}/access", include_in_schema=False)
async def current_instance_permissions_project_access_put(
    project_id: str,
    starlette_request: FastAPIRequest,
):
    async def handler():
        from vibe import permissions

        authorization_context = getattr(g, "authorization_context", None)
        if authorization_context is None or not authorization_context.can_manage_instance:
            return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
        try:
            body = await starlette_request.body()
            raw_payload = await starlette_request.json() if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw_payload = None
        payload = _permissions_mutation_payload(
            raw_payload,
            {"mode", "bindings", "if_match_revision", "if_match_instance_id"},
            {
                "bindings": frozenset(
                    {"principal_kind", "principal_value", "access_role"}
                )
            },
        )
        if payload is None:
            return jsonify({"ok": False, "error": "invalid_request"}), 422
        try:
            result = await asyncio.to_thread(
                permissions.update_project_access,
                project_id,
                payload,
            )
            return jsonify(result)
        except Exception as error:
            return _permissions_error_response(error)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.get(
    "/api/permissions/resources/{resource_kind}/{resource_id}/access",
    include_in_schema=False,
)
async def current_instance_permissions_resource_access_get(
    resource_kind: str,
    resource_id: str,
    starlette_request: FastAPIRequest,
):
    async def handler():
        from vibe import permissions

        authorization_context = getattr(g, "authorization_context", None)
        if authorization_context is None or not authorization_context.can_read_instance:
            return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
        try:
            result = await asyncio.to_thread(
                permissions.get_resource_access,
                resource_kind,
                resource_id,
            )
            response = jsonify(result)
            response.headers["Cache-Control"] = "private, no-store"
            return response
        except Exception as error:
            return _permissions_error_response(error)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.put(
    "/api/permissions/resources/{resource_kind}/{resource_id}/access",
    include_in_schema=False,
)
async def current_instance_permissions_resource_access_put(
    resource_kind: str,
    resource_id: str,
    starlette_request: FastAPIRequest,
):
    async def handler():
        from vibe import permissions

        authorization_context = getattr(g, "authorization_context", None)
        if authorization_context is None or not authorization_context.can_manage_instance:
            return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
        try:
            body = await starlette_request.body()
            raw_payload = await starlette_request.json() if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw_payload = None
        payload = _resource_access_mutation_payload(raw_payload)
        if payload is None:
            return jsonify({"ok": False, "error": "invalid_request"}), 422
        try:
            result = await asyncio.to_thread(
                permissions.update_resource_access,
                resource_kind,
                resource_id,
                payload,
            )
            return jsonify(result)
        except Exception as error:
            return _permissions_error_response(error)

    return await _dispatch_native_ui_request(starlette_request, handler)


_PROJECT_RESOURCE_PATHS = (
    ("project", re.compile(r"^/api/projects/([^/]+)(?:/agents-md)?$")),
    ("session", re.compile(r"^/api/sessions/([^/]+)(?:/.*)?$")),
    ("show_page", re.compile(r"^/api/show-pages/([^/]+)(?:/.*)?$")),
    ("show_page", re.compile(r"^/api/show/sessions/([^/]+)(?:/.*)?$")),
    ("show_page", re.compile(r"^/show/([^/]+)(?:/.*)?$")),
)


def _project_access_resource(path: str) -> tuple[str, str] | None:
    for kind, pattern in _PROJECT_RESOURCE_PATHS:
        match = pattern.fullmatch(path)
        if match is not None:
            return kind, unquote(match.group(1))
    return None


@app.before_request
def enforce_project_role_capabilities():
    """Narrow remote non-owner Project/session routes through applied Project ACLs."""
    if _remote_auth_exempt_path():
        return None
    context = getattr(g, "authorization_context", None)
    if context is None or _has_runtime_owner_access(context):
        return None

    from storage import project_access_service
    from storage.db import create_sqlite_engine
    from vibe.authorization import required_instance_role

    minimum_instance_role = getattr(
        g,
        "http_authorization_policy",
        None,
    )
    minimum_instance_role = (
        minimum_instance_role.minimum_role
        if minimum_instance_role is not None
        else required_instance_role(request.method, request.path)
    )
    if minimum_instance_role not in {"viewer", "editor", "member"}:
        return None
    # A ``member`` route is instance-wide Project administration, but it still
    # names one Project, and the instance role does not say *which* Projects the
    # caller may touch. Ceiling this at "editor" is what let a member mutate a
    # restricted Project by id that ``list_projects`` hides from them.
    #
    # The floor for those routes is the Project ACL's *visibility* floor, not
    # "member": a member holding an explicit editor binding on a restricted
    # Project has an effective Project role of editor, so demanding a "member"
    # Project role would refuse the very Projects the list shows them. The
    # instance-role half of the authorization is already enforced by the HTTP
    # policy before this hook runs; this half only asks whether the Project is
    # theirs to see.
    required_project_role = "viewer" if minimum_instance_role == "member" else minimum_instance_role
    resource = _project_access_resource(request.path)
    if resource is None:
        return None

    kind, resource_id = resource
    if kind == "show_page":
        # Show Page ``/show`` admission is the §3.2 Instance Viewer role alone.
        # Project ACL is required by ShowPageStore for create/edit operations,
        # but applying the generic project middleware here treats a session id
        # as a project id and rejects valid pages (including pages without a
        # live session).
        return None
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        role = (
            project_access_service.get_effective_project_role(conn, context, resource_id)
            if kind == "project"
            else project_access_service.get_effective_session_role(conn, context, resource_id)
        )
    if not project_access_service.role_allows(role, required_project_role):
        return jsonify({"ok": False, "error": "not_found"}), 404
    return None


@app.before_request
def protect_mutating_ui_requests():
    if request.method not in MUTATING_METHODS:
        return None
    if _is_mutation_guard_exempt():
        return None

    source = _request_origin(request.headers.get("Origin")) or _request_origin(request.headers.get("Referer"))
    if not source:
        return jsonify({"ok": False, "message": "Forbidden: missing origin header"}), 403

    if not _same_origin(source, _current_origin()):
        return jsonify({"ok": False, "message": "Forbidden: invalid origin"}), 403

    if _is_show_api_mutation():
        return None

    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    csrf_header = request.headers.get(CSRF_HEADER_NAME, "")
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        return jsonify({"ok": False, "message": "Forbidden: invalid csrf token"}), 403

    return None


@app.after_request
def compress_materialized_api_response(response: Response) -> Response:
    return _compress_materialized_api_response(response)


@app.after_request
def add_public_show_representation_vary(response: Response) -> Response:
    if not getattr(request._request.state, "public_show_representation_varies", False):
        return response
    for header in _PUBLIC_SHOW_REPRESENTATION_HEADERS:
        response.headers["Vary"] = _append_vary_header(
            response.headers.get("Vary"),
            header,
        )
    if getattr(request._request.state, "public_show_representation_varies_cookie", False):
        response.headers["Vary"] = _append_vary_header(
            response.headers.get("Vary"),
            "Cookie",
        )
    return response


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


def _merge_csp_frame_src(existing: str | None) -> str:
    required = ["'self'", VAULT_SANDBOX_ORIGIN]
    if not existing:
        return f"frame-src {' '.join(required)}"
    directives = [part.strip() for part in existing.split(";") if part.strip()]
    for index, directive in enumerate(directives):
        name, _, rest = directive.partition(" ")
        if name.lower() != "frame-src":
            continue
        tokens = rest.split()
        for token in required:
            if token not in tokens:
                tokens.append(token)
        directives[index] = f"frame-src {' '.join(tokens)}"
        return "; ".join(directives)
    directives.append(f"frame-src {' '.join(required)}")
    return "; ".join(directives)


def _is_show_page_response_path(path: str) -> bool:
    return path == "/show" or path.startswith("/show/") or path == "/p" or path.startswith("/p/")


def _apply_vault_sandbox_security_headers(response: FastAPIResponse, path: str) -> FastAPIResponse:
    if _is_show_page_response_path(path):
        return response
    response.headers["Permissions-Policy"] = VAULT_SANDBOX_PERMISSIONS_POLICY
    response.headers["Content-Security-Policy"] = _merge_csp_frame_src(
        response.headers.get("Content-Security-Policy")
    )
    return response


@app.middleware("http")
async def add_vault_sandbox_security_headers(starlette_request: FastAPIRequest, call_next):
    response = await call_next(starlette_request)
    return _apply_vault_sandbox_security_headers(response, starlette_request.url.path)


@app.after_request
def renew_remote_access_cookie(response: Response) -> Response:
    # Logout handler explicitly clears the session cookie; never re-issue it.
    if getattr(g, "remote_session_logout", False):
        return response
    if _is_current_immutable_static_asset_request():
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

    if not isinstance(renew, dict):
        return response
    try:
        session_cookie = remote_access.renew_session_cookie(config, renew)
    except (remote_access.OAuthCodeExchangeError, TypeError, ValueError):
        return response
    response.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        session_cookie,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
        max_age=remote_access.SESSION_TTL_SECONDS,
    )
    return response


def _read_log_entries(log_path: Path, source_key: str, lines: int) -> tuple[list[dict[str, Any]], int]:
    log_paths = application_log_paths(log_path) if source_key == "service" else [log_path]
    existing_paths = [path for path in log_paths if path.exists()]
    if not existing_paths:
        return [], 0

    recent_lines: deque[tuple[str, int, int]] = deque(maxlen=lines)
    total_lines = 0
    for path in existing_paths:
        try:
            file_sort_ns = path.stat().st_mtime_ns
            with path.open("r", encoding="utf-8", errors="replace") as log_file:
                for raw_line in log_file:
                    recent_lines.append((raw_line, file_sort_ns, total_lines))
                    total_lines += 1
        except OSError:
            continue

    logs_list: list[dict[str, Any]] = []
    for raw_line, file_sort_ns, line_index in recent_lines:
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
                    "_sort_index": line_index,
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
        fallback_entry["_sort_index"] = line_index
        logs_list.append(fallback_entry)

    return logs_list, total_lines


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
        exists = any(candidate.exists() for candidate in application_log_paths(path)) if key == "service" else path.exists()
        resolved.append(
            {
                "key": key,
                "filename": filename,
                "path": str(path),
                "exists": exists,
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

    payload = json.loads(runtime.render_status(detect_extra_processes=False))
    if not payload.get("running") and runtime.read_status().get("state") == "running":
        runtime.write_status("stopped", "process not running", None, payload.get("ui_pid"))
        payload = json.loads(runtime.render_status(detect_extra_processes=False))
    return jsonify(payload)


@app.route(MODEL_SERVICE_REFRESH_PATH, methods=["POST"])
def model_service_refresh():
    if not _is_cli_model_service_refresh_request():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    from vibe.model_service import request_model_service_refresh

    request_model_service_refresh()
    return jsonify({"ok": True})


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
    from core.show_pages import ShowPageError, ShowPageStore

    if not _show_runtime_hmr_origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    remote_config = _load_remote_access_config()
    local_request = _websocket_is_local_request(websocket, remote_config)
    remote_identity = None
    remote_payload = None
    remote_session_cookie = None
    remote_request_host = None
    authorization_context = None
    if not local_request:
        from vibe import remote_access

        remote_session_cookie = getattr(websocket, "cookies", {}).get(
            remote_access.SESSION_COOKIE_NAME
        )
        remote_request_host = _websocket_normalized_host(websocket)
        remote_identity, resolution = await _remote_access_websocket_authorization(
            websocket,
            remote_config,
        )
        if resolution is None or not resolution.current:
            await _close_websocket_for_authorization(
                websocket,
                resolution.state if resolution is not None else "invalid_identity",
                subprotocol="vite-hmr",
            )
            return
        remote_payload = resolution.payload
        from vibe.authorization import context_from_session_payload

        authorization_context = context_from_session_payload(remote_payload)
        # §3.2: HMR drives live mutation of the page, so it is an Editor surface.
        # A Viewer may read /show but must not open HMR (nor POST/PUT/PATCH/DELETE).
        if not _show_page_mutation_allowed(authorization_context):
            await websocket.close(code=1008)
            return

    store = ShowPageStore()
    try:
        try:
            page = store.require_access(
                session_id,
                user_context=_show_runtime_websocket_resource_context(
                    websocket,
                    payload=remote_payload,
                ),
            )
        except ShowPageError:
            await websocket.close(code=1008)
            return
        # The authenticated /show/ surface is the editor path for every online
        # audience mode. Limited /p admission remains a separate shared-runtime
        # boundary, but choosing Limited must not break the owner's live preview.
        if page is None or page.visibility not in {"private", "limited", "public"}:
            await websocket.close(code=1008)
            return
    finally:
        store.close()

    access_sub_id = None
    access_queue = None
    if authorization_context is not None:
        from vibe.sse_broker import broker

        access_sub_id, access_queue = broker.subscribe()
        if not _show_page_mutation_allowed(authorization_context):
            broker.unsubscribe(access_sub_id)
            await websocket.close(code=1008)
            return

    await websocket.accept(subprotocol="vite-hmr")
    proxy_task = asyncio.create_task(_proxy_show_runtime_websocket(websocket, session_id))
    revocation_task = (
        asyncio.create_task(
            _wait_for_show_page_access_loss(
                access_queue,
                authorization_context,
                session_id,
            )
        )
        if access_queue is not None and authorization_context is not None
        else None
    )
    authorization_revision_task = (
        asyncio.create_task(
            _wait_for_remote_session_authorization_loss(
                remote_config,
                remote_identity,
                remote_payload,
                session_cookie=remote_session_cookie,
                request_host=remote_request_host,
            )
        )
        if remote_config is not None and remote_identity is not None and remote_payload is not None
        else None
    )
    try:
        waiters = {proxy_task}
        if revocation_task is not None:
            waiters.add(revocation_task)
        if authorization_revision_task is not None:
            waiters.add(authorization_revision_task)
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if proxy_task in done:
            await proxy_task
        elif authorization_revision_task is not None and authorization_revision_task in done:
            outcome = await authorization_revision_task
            logger.info("show_runtime.authorization_%s session=%s", outcome, session_id)
            await websocket.close(code=_authorization_websocket_close_code(outcome))
        else:
            await revocation_task
            logger.info("show_runtime.authorization_revoked session=%s", session_id)
            await websocket.close(code=_AUTHORIZATION_REVOKED_WEBSOCKET_CLOSE_CODE)
    except Exception:
        logger.debug("Show runtime HMR websocket unavailable", exc_info=True)
        await websocket.close(code=1011)
    finally:
        tasks = (proxy_task, revocation_task, authorization_revision_task)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
        if access_sub_id is not None:
            from vibe.sse_broker import broker

            broker.unsubscribe(access_sub_id)


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
    config = _load_remote_access_config()
    local_request = _websocket_is_local_request(websocket, config)
    remote_identity = None
    remote_payload = None
    remote_session_cookie = None
    remote_request_host = None
    if not local_request:
        from vibe import remote_access

        remote_session_cookie = getattr(websocket, "cookies", {}).get(
            remote_access.SESSION_COOKIE_NAME
        )
        remote_request_host = _websocket_normalized_host(websocket)
        remote_identity, resolution = await _remote_access_websocket_authorization(
            websocket,
            config,
        )
        if resolution is None or not resolution.current:
            await _close_websocket_for_authorization(
                websocket,
                resolution.state if resolution is not None else "invalid_identity",
            )
            return
        remote_payload = resolution.payload
        from vibe.authorization import context_from_session_payload

        if not _websocket_context_authorized(
            context_from_session_payload(remote_payload),
            minimum_role="editor",
        ):
            await websocket.close(code=1008)
            return
    await websocket.accept()
    remote_addr = _websocket_client_host(websocket) or "unknown"
    remote_subject = None
    if remote_payload is not None:
        subject = str(remote_payload.get("sub") or "").strip()
        email = str(remote_payload.get("email") or "").strip()
        remote_subject = subject or email or None
    effective_session_id = _terminal_effective_session_id(session_id, remote_subject)
    session_ref = _terminal_session_log_ref(effective_session_id)
    logger.info("terminal.session_open session_ref=%s remote_addr=%s", session_ref, remote_addr)
    # Optional start directory for a brand-new session ("Open Terminal Here" from the Files
    # app). Validated server-side in the terminal service; an invalid/absent value silently
    # falls back to the default cwd and reattaching an existing session ignores it entirely.
    initial_cwd = websocket.query_params.get("cwd") or None
    handler_task = None
    authorization_revision_task = None
    try:
        service = get_terminal_service()
        service.start_reaper()
        handler_task = asyncio.create_task(
            service.handle_websocket(
                websocket,
                effective_session_id,
                initial_cwd=initial_cwd,
            )
        )
        authorization_revision_task = (
            asyncio.create_task(
                _wait_for_remote_session_authorization_loss(
                    config,
                    remote_identity,
                    remote_payload,
                    session_cookie=remote_session_cookie,
                    request_host=remote_request_host,
                )
            )
            if config is not None and remote_identity is not None and remote_payload is not None
            else None
        )
        waiters = {handler_task}
        if authorization_revision_task is not None:
            waiters.add(authorization_revision_task)
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if authorization_revision_task is not None and authorization_revision_task in done:
            outcome = await authorization_revision_task
            logger.info("terminal.authorization_%s session_ref=%s", outcome, session_ref)
            await websocket.close(code=_authorization_websocket_close_code(outcome))
        else:
            await handler_task
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
        tasks = (handler_task, authorization_revision_task)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
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
    context = getattr(g, "authorization_context", None)
    # Closing a terminal session is allowed for any authenticated runtime
    # viewer; the subject-scoped effective ID below prevents terminating a
    # different user's session. Opening remains Editor-only.
    if context is None or not context.has_role("viewer"):
        return jsonify({"ok": False, "error": "terminal_unauthorized"}), 403

    remote_payload = getattr(g, "remote_session_payload", None)
    remote_subject = None
    if isinstance(remote_payload, Mapping):
        subject = str(remote_payload.get("sub") or "").strip()
        email = str(remote_payload.get("email") or "").strip()
        remote_subject = subject or email or None
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


def _show_runtime_websocket_authorized(
    websocket: Any,
    *,
    minimum_role: str = "viewer",
    project_session_id: str | None = None,
) -> bool:
    config = _load_remote_access_config()
    if config is None:
        return _websocket_is_local_request(websocket)
    if _websocket_is_local_request(websocket, config):
        return True
    if not _remote_access_host_allowed(config, _websocket_normalized_host(websocket)):
        return False
    payload = _remote_access_websocket_session_payload(websocket, config)
    if payload is None:
        return False
    from vibe.authorization import context_from_session_payload

    context = context_from_session_payload(payload)
    return _websocket_context_authorized(
        context,
        minimum_role=minimum_role,
        project_session_id=project_session_id,
    )


def _websocket_context_authorized(
    context: Any,
    *,
    minimum_role: str,
    project_session_id: str | None = None,
) -> bool:
    if not context.has_role(minimum_role):
        return False
    if project_session_id is None or _has_runtime_owner_access(context):
        return True
    if minimum_role == "viewer":
        return context.has_role("viewer")
    return _project_session_access_allowed(context, project_session_id, minimum_role)


def _project_session_access_allowed(context: Any, session_id: str, minimum_role: str) -> bool:
    from storage import project_access_service

    if context is None:
        return False
    if _has_runtime_owner_access(context):
        return True
    if not context.has_role(minimum_role):
        return False
    engine = _projects_engine()
    with engine.connect() as conn:
        role = project_access_service.get_effective_session_role(
            conn,
            context,
            session_id,
        )
    return project_access_service.role_allows(role, minimum_role)


async def _wait_for_project_session_access_loss(
    queue: Any,
    context: Any,
    session_id: str,
    minimum_role: str,
) -> None:
    while True:
        event_type, _payload = await queue.get()
        if event_type != "authorization.changed":
            continue
        if not _project_session_access_allowed(context, session_id, minimum_role):
            return


async def _wait_for_show_page_access_loss(
    queue: Any,
    context: Any,
    session_id: str,
) -> None:
    """Close a Show Page socket when its independent ACL is revoked."""

    while True:
        event_type, _payload = await queue.get()
        if event_type != "authorization.changed":
            continue
        if not _show_page_mutation_allowed(context):
            return


def _show_page_resource_access_allowed(context: Any, session_id: str) -> bool:
    """§3.2 ``/show`` admission: the Instance Viewer role alone."""

    return context is not None and context.has_role("viewer")


def _show_page_mutation_allowed(context: Any) -> bool:
    """§3.2 mutation boundary: only an Instance Editor/owner may drive ``/show``."""

    return context is not None and context.has_role("editor")


async def _wait_for_remote_session_authorization_loss(
    config: V2Config,
    identity: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
    *,
    session_cookie: str | None,
    request_host: str | None,
) -> str:
    """Return the terminal state for one accepted remote socket."""

    from vibe import remote_access
    from vibe.authorization import context_from_session_payload

    if payload is None:
        payload = identity
    initial_context = context_from_session_payload(payload)
    while True:
        await asyncio.sleep(_AUTHORIZATION_REVISION_RECHECK_SECONDS)
        resolution = await _live_remote_authorization_resolution(
            config,
            identity,
            session_cookie=session_cookie,
            request_host=request_host,
        )
        if resolution.state != "current" or resolution.payload is None:
            return resolution.state
        if context_from_session_payload(resolution.payload) != initial_context:
            return "changed"


def _show_runtime_websocket_resource_context(
    websocket: Any,
    *,
    payload: Mapping[str, Any] | None = None,
):
    """Build the ACL context from the same signed session used by the socket gate."""

    from storage import resource_access_service

    config = _load_remote_access_config()
    if config is None or _websocket_is_local_request(websocket, config):
        return resource_access_service.ResourceUserContext(instance_role="owner")
    if payload is None:
        payload = _remote_access_websocket_session_payload(websocket, config)
    if payload is None:
        return resource_access_service.ResourceUserContext()
    return _request_authorization_context(
        resource_access_service.current_resource_context(
            payload,
            is_remote=True,
        )
    )


def _show_runtime_hmr_origin_allowed(websocket: Any) -> bool:
    config = _load_remote_access_config()
    if not _websocket_trusted_public_origin_local_request(websocket, config):
        return True
    return _websocket_origin_matches_effective_request(websocket)


def _authorization_websocket_close_code(state: str) -> int:
    if state == "revoked":
        return _AUTHORIZATION_REVOKED_WEBSOCKET_CLOSE_CODE
    if state == "unavailable":
        return _AUTHORIZATION_UNAVAILABLE_WEBSOCKET_CLOSE_CODE
    if state == "changed":
        return _AUTHORIZATION_CHANGED_WEBSOCKET_CLOSE_CODE
    return _AUTHORIZATION_LOGIN_REQUIRED_WEBSOCKET_CLOSE_CODE


async def _close_websocket_for_authorization(
    websocket: Any,
    state: str,
    *,
    subprotocol: str | None = None,
) -> None:
    if subprotocol is None:
        await websocket.accept()
    else:
        await websocket.accept(subprotocol=subprotocol)
    await websocket.close(code=_authorization_websocket_close_code(state))


def _remote_authorization_sse_frame(state: str) -> str:
    error = {
        "revoked": "remote_access_revoked",
        "unavailable": "remote_access_authorization_unavailable",
        "changed": "remote_access_authorization_changed",
    }.get(state, "remote_access_login_required")
    return (
        "event: remote.authorization\n"
        f"data: {json.dumps({'state': state, 'error': error}, separators=(',', ':'))}\n\n"
    )


async def _remote_stream_authorization_state(
    config: V2Config,
    identity: Mapping[str, Any],
    initial_payload: Mapping[str, Any],
    *,
    session_cookie: str | None,
    request_host: str | None,
) -> str:
    from vibe.authorization import context_from_session_payload

    resolution = await _live_remote_authorization_resolution(
        config,
        identity,
        session_cookie=session_cookie,
        request_host=request_host,
    )
    if not resolution.current or resolution.payload is None:
        return resolution.state
    if context_from_session_payload(resolution.payload) != context_from_session_payload(initial_payload):
        return "changed"
    return "current"


async def _live_remote_authorization_resolution(
    config: V2Config,
    identity: Mapping[str, Any],
    *,
    session_cookie: str | None,
    request_host: str | None,
):
    """Revalidate the accepted remote session before refreshing its authority."""

    from vibe import remote_access

    try:
        live_config = await asyncio.to_thread(V2Config.load)
    except Exception:
        logger.warning("live remote authorization config reload failed", exc_info=True)
        return remote_access.AuthorizationResolution(
            "unavailable",
            reason="remote_access_config_unavailable",
        )

    cloud = live_config.remote_access.vibe_cloud
    if not cloud.enabled or not cloud.session_secret:
        return remote_access.AuthorizationResolution(
            "invalid_identity",
            reason=(
                "remote_access_disabled"
                if not cloud.enabled
                else "remote_access_session_secret_missing"
            ),
        )
    if not request_host or not _remote_access_host_allowed(live_config, request_host):
        return remote_access.AuthorizationResolution(
            "invalid_identity",
            reason="remote_access_host_mismatch",
        )
    live_identity = remote_access.parse_session_identity(live_config, session_cookie)
    if live_identity is None or live_identity != dict(identity):
        return remote_access.AuthorizationResolution(
            "invalid_identity",
            reason="identity_invalid",
        )
    return await remote_access.resolve_current_authorization_async(
        live_config,
        live_identity,
    )


async def _remote_access_websocket_authorization(
    websocket: Any,
    config: V2Config | None,
) -> tuple[dict[str, Any] | None, Any]:
    from vibe import remote_access

    if config is None or _websocket_is_local_request(websocket, config):
        return None, None
    if not _remote_access_host_allowed(config, _websocket_normalized_host(websocket)):
        return None, remote_access.AuthorizationResolution(
            "invalid_identity",
            reason="remote_access_host_mismatch",
        )
    cloud = config.remote_access.vibe_cloud
    if not cloud.enabled or not cloud.session_secret:
        return None, remote_access.AuthorizationResolution(
            "invalid_identity",
            reason=(
                "remote_access_disabled"
                if not cloud.enabled
                else "remote_access_session_secret_missing"
            ),
        )
    cookie_value = websocket.cookies.get(remote_access.SESSION_COOKIE_NAME)
    identity = remote_access.parse_session_identity(
        config,
        cookie_value,
    )
    if identity is None:
        return None, remote_access.AuthorizationResolution(
            "invalid_identity",
            reason="identity_invalid",
        )
    resolution = await remote_access.resolve_current_authorization_async(config, identity)
    return identity, resolution


def _remote_access_websocket_session_payload(websocket: Any, config: V2Config | None) -> dict[str, Any] | None:
    """Compatibility sync helper for tests and non-ASGI adapters."""

    return _remote_access_websocket_session_claims(websocket, config)


def _remote_access_websocket_session_claims(websocket: Any, config: V2Config | None) -> dict[str, Any] | None:
    """Return current remote claims for synchronous compatibility callers."""

    if config is None or _websocket_is_local_request(websocket, config):
        return None
    if not _remote_access_host_allowed(config, _websocket_normalized_host(websocket)):
        return None
    from vibe import remote_access

    if not config.remote_access.vibe_cloud.enabled or not config.remote_access.vibe_cloud.session_secret:
        return None
    identity = remote_access.parse_session_identity(
        config,
        websocket.cookies.get(remote_access.SESSION_COOKIE_NAME),
    )
    if identity is None:
        return None
    resolution = remote_access.resolve_current_authorization(config, identity)
    return resolution.payload if resolution.current else None


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
    websocket_scheme = _websocket_effective_scheme(websocket)
    request_port = _origin_port(_websocket_effective_request_host(websocket), websocket_scheme)
    return origin_port == request_port and _terminal_origin_scheme_matches_socket(
        parsed_origin.scheme,
        websocket_scheme,
    )


def _websocket_origin_matches_effective_request(websocket: Any) -> bool:
    origin = _request_origin(websocket.headers.get("origin"))
    if not origin:
        return False
    parsed_origin = urlparse(origin)
    if _normalized_host(parsed_origin.netloc) != _websocket_normalized_host(websocket):
        return False
    origin_port = _origin_port(parsed_origin.netloc, parsed_origin.scheme)
    websocket_scheme = _websocket_effective_scheme(websocket)
    request_port = _origin_port(_websocket_effective_request_host(websocket), websocket_scheme)
    return origin_port == request_port and _terminal_origin_scheme_matches_socket(
        parsed_origin.scheme,
        websocket_scheme,
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
    if _websocket_has_untrusted_forwarded_metadata(websocket):
        return False
    if _websocket_has_trusted_forwarded_metadata(websocket) and _websocket_trusted_forwarded_host(websocket) is None:
        return False
    if _websocket_trusted_public_origin_local_request(websocket, config):
        return True
    client_host = _websocket_client_host(websocket)
    if not _websocket_has_trusted_forwarded_metadata(websocket) and client_host == "testclient":
        return _is_loopback_host(websocket.headers.get("host"))
    try:
        client_address = ipaddress.ip_address(client_host)
    except ValueError:
        client_address = None
    if (
        not _websocket_has_trusted_forwarded_metadata(websocket)
        and client_address is not None
        and client_address.is_loopback
        and _is_loopback_host(_websocket_effective_request_host(websocket))
    ):
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


def _websocket_has_trusted_forwarded_metadata(websocket: Any) -> bool:
    return _websocket_is_explicitly_trusted_proxy_peer(websocket) and _websocket_has_forwarded_metadata(websocket)


def _websocket_has_untrusted_forwarded_metadata(websocket: Any) -> bool:
    return _websocket_has_forwarded_metadata(websocket) and not _websocket_is_explicitly_trusted_proxy_peer(websocket)


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


def _websocket_is_explicitly_trusted_proxy_peer(websocket: Any) -> bool:
    configured = os.environ.get(TRUSTED_PROXY_IPS_ENV, "")
    if not configured.strip():
        return False
    peer = _websocket_peer_address(websocket)
    if peer is None:
        return False
    for raw_entry in configured.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("Ignoring invalid %s entry: %s", TRUSTED_PROXY_IPS_ENV, entry)
            continue
        if peer in network:
            return True
    return False


def _websocket_trusted_forwarded_host(websocket: Any) -> str | None:
    if not _websocket_is_explicitly_trusted_proxy_peer(websocket):
        return None
    forwarded_host = websocket.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if not _forwarded_host_is_safe(forwarded_host):
        return None
    if _forwarded_host_has_explicit_port(forwarded_host):
        return forwarded_host
    forwarded_port = _websocket_trusted_forwarded_port(websocket)
    if forwarded_port is None:
        return forwarded_host
    return f"{forwarded_host}:{forwarded_port}"


def _websocket_trusted_forwarded_port(websocket: Any) -> int | None:
    raw_port = websocket.headers.get("X-Forwarded-Port", "").split(",")[0].strip()
    if not raw_port:
        return None
    if not raw_port.isdigit():
        return None
    port = int(raw_port)
    if port < 1 or port > 65535:
        return None
    return port


def _websocket_effective_request_host(websocket: Any) -> str:
    return _websocket_trusted_forwarded_host(websocket) or websocket.headers.get("host")


def _websocket_effective_scheme(websocket: Any) -> str:
    if _websocket_is_explicitly_trusted_proxy_peer(websocket):
        forwarded_proto = websocket.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        if forwarded_proto == "https":
            return "wss"
        if forwarded_proto == "http":
            return "ws"
        if forwarded_proto in {"ws", "wss"}:
            return forwarded_proto
    return websocket.url.scheme


def _websocket_trusted_forwarded_origin_identity(websocket: Any) -> tuple[str, str, int | None] | None:
    trusted_forwarded_host = _websocket_trusted_forwarded_host(websocket)
    if trusted_forwarded_host is None:
        return None
    forwarded_proto = websocket.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if forwarded_proto == "wss":
        forwarded_proto = "https"
    elif forwarded_proto == "ws":
        forwarded_proto = "http"
    if forwarded_proto not in {"http", "https"}:
        return None
    return (
        forwarded_proto,
        _normalized_host(trusted_forwarded_host),
        _origin_port(trusted_forwarded_host, forwarded_proto),
    )


def _websocket_trusted_forwarded_client_address(websocket: Any) -> ipaddress._BaseAddress | None:
    if not _websocket_is_explicitly_trusted_proxy_peer(websocket):
        return None
    raw_client = websocket.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not raw_client:
        return None
    try:
        address = ipaddress.ip_address(raw_client)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _websocket_local_trust_peer_address(websocket: Any) -> ipaddress._BaseAddress | None:
    if not _websocket_has_trusted_forwarded_metadata(websocket):
        return _websocket_peer_address(websocket)
    if _websocket_trusted_forwarded_host(websocket) is None:
        return None
    return _websocket_trusted_forwarded_client_address(websocket)


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


def _websocket_is_private_peer_address(address: ipaddress._BaseAddress | None) -> bool:
    return address is not None and _is_private_address(address)


def _websocket_trusted_public_origin_local_request(websocket: Any, config: V2Config | None) -> bool:
    if not _websocket_has_trusted_forwarded_metadata(websocket):
        return False
    if not _trusted_public_origin_matches(config, _websocket_trusted_forwarded_origin_identity(websocket)):
        return False
    return _trusted_public_origin_allowed_for_peer(
        config,
        _websocket_peer_address(websocket),
    )


def _websocket_peer_shares_setup_host_network(
    websocket: Any,
    setup_address: ipaddress._BaseAddress,
    peer: ipaddress._BaseAddress | None = None,
) -> bool:
    peer = peer or _websocket_peer_address(websocket)
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
    if _websocket_has_untrusted_forwarded_metadata(websocket):
        return False
    peer_address = _websocket_local_trust_peer_address(websocket)
    if not _websocket_is_private_peer_address(peer_address):
        return False
    if _is_tunnel_wildcard_bind(config):
        return _websocket_peer_shares_setup_host_network(websocket, setup_address, peer_address)
    return True


def _websocket_normalized_host(websocket: Any) -> str:
    return _normalized_host(_websocket_effective_request_host(websocket))


async def _proxy_show_runtime_websocket(
    websocket: WebSocket,
    session_id: str,
    *,
    external_prefix: str | None = None,
) -> None:
    from core.show_runtime import (
        ShowRuntimeContext,
        ShowRuntimeProtocolEnvelope,
        get_show_runtime_manager,
    )

    if external_prefix is None:
        external_prefix = f"/show/{quote(session_id, safe='')}"
        context = ShowRuntimeContext.PRIVATE
    else:
        context = ShowRuntimeContext.SHARED
    runtime_path = f"{external_prefix.rstrip('/')}/__vite_hmr"
    if websocket.url.query:
        runtime_path = f"{runtime_path}?{websocket.url.query}"
    manager = get_show_runtime_manager()
    target = await manager.websocket_target(
        runtime_path,
        envelope=ShowRuntimeProtocolEnvelope(context),
    )
    async with ClientSession() as session:
        try:
            upstream = await session.ws_connect(
                target.url,
                headers=target.headers,
                protocols=["vite-hmr"],
                autoping=True,
            )
        except (asyncio.TimeoutError, ClientConnectionError):
            await manager.invalidate_websocket_target(target)
            raise
        async with upstream:
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
    from core.services import settings as settings_service

    # On a truly fresh install no config file exists yet, but the setup
    # wizard (and the provider-config modal it reuses, which calls
    # ``getConfig()``) must still load. Serve an in-memory default whose
    # ``setup_state.needs_setup`` is True so the wizard shows and a fresh
    # default is never mistaken for a completed setup. The write side
    # (``save_config``) already creates the file on the first real save.
    config = settings_service.load_config_or_default()
    authorization_context = getattr(g, "authorization_context", None)
    payload = _config_api_payload_for_context(config, authorization_context)
    return jsonify(payload)


def _config_payload_for_context(config: Any, authorization_context: Any) -> dict[str, Any]:
    """Project configuration by Instance role, independent of request origin."""

    from vibe import api

    if authorization_context is None or authorization_context.can_manage_instance:
        return api.client_config_payload(config)
    return api.non_owner_config_payload(config)


def _config_api_payload_for_context(config: Any, authorization_context: Any) -> dict[str, Any]:
    """Return the complete payload exposed by the config API."""

    from config.v2_config import is_model_hub_enabled

    payload = _config_payload_for_context(config, authorization_context)
    payload["capabilities"] = {"model_hub": {"enabled": is_model_hub_enabled()}}
    return payload


_MODEL_HUB_SERVICE = None


def _model_hub_service():
    from vibe.model_hub_client import ModelHubRemoteService

    global _MODEL_HUB_SERVICE
    if _MODEL_HUB_SERVICE is None:
        _MODEL_HUB_SERVICE = ModelHubRemoteService()
    return _MODEL_HUB_SERVICE


def _model_hub_success(**payload):
    from core.handlers.model_hub.service import CONTRACT_VERSION

    return jsonify({"ok": True, "contract_version": CONTRACT_VERSION, **payload})


def _model_hub_error(exc):
    from core.handlers.model_hub.service import CONTRACT_VERSION

    body = {"ok": False, "contract_version": CONTRACT_VERSION, "error": exc.code}
    if exc.detail:
        body["detail"] = exc.detail
    body.update(exc.data)
    return jsonify(body), exc.status


def _model_hub_json_object(error: str = "discovery_failed", *, status: int = 400):
    from core.handlers.model_hub import ModelHubError

    payload = request.json
    if not isinstance(payload, dict):
        raise ModelHubError(error, status=status)
    return payload


@app.route("/api/models/sources", methods=["GET"])
def model_hub_sources_get():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(sources=_model_hub_service().list_sources())
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/observe", methods=["POST"])
async def model_hub_sources_observe_post():
    from core.handlers.model_hub import ModelHubError

    try:
        result = await _model_hub_service().observe_source(
            _model_hub_json_object("discovery_failed")
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources", methods=["POST"])
async def model_hub_sources_post():
    from core.handlers.model_hub import ModelHubError

    try:
        result = await _model_hub_service().create_source(_model_hub_json_object())
        return _model_hub_success(**result), 201
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/<source_id>", methods=["PATCH"])
async def model_hub_sources_patch(source_id):
    from core.handlers.model_hub import ModelHubError

    try:
        result = await _model_hub_service().patch_source(source_id, _model_hub_json_object())
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/<source_id>/credential", methods=["PUT"])
async def model_hub_source_credential_put(source_id):
    from core.handlers.model_hub import ModelHubError

    try:
        result = await _model_hub_service().replace_credential(
            source_id,
            _model_hub_json_object(),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/<source_id>/reauth", methods=["POST"])
async def model_hub_source_reauth_post(source_id):
    from core.handlers.model_hub import ModelHubError

    try:
        result = await _model_hub_service().reauth_source(
            source_id,
            _model_hub_json_object(),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/<source_id>", methods=["DELETE"])
async def model_hub_sources_delete(source_id):
    from core.handlers.model_hub import ModelHubError

    try:
        force = str(request.args.get("force") or "").lower() in _TRUE_BOOL_STRINGS
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {
            "would_remove_hops",
            "would_interrupt",
        }:
            raise ModelHubError("invalid_source_order")
        result = await _model_hub_service().delete_source(
            source_id,
            force=force,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/<source_id>/refresh", methods=["POST"])
async def model_hub_sources_refresh(source_id):
    from core.handlers.model_hub import ModelHubError

    try:
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {
            "force",
            "would_remove_hops",
            "would_interrupt",
        }:
            raise ModelHubError("invalid_source_order")
        result = await _model_hub_service().refresh_source(
            source_id,
            force=payload.get("force") is True,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents", methods=["GET"])
def model_hub_agents_get():
    from core.handlers.model_hub import ModelHubError

    try:
        service = _model_hub_service()
        refresh_cli_presence = request.args.get("refresh_cli_presence") == "1"
        return _model_hub_success(
            agents=(
                service.list_agents(refresh_cli_presence=True)
                if refresh_cli_presence
                else service.list_agents()
            )
        )
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/sources", methods=["GET"])
def model_hub_agent_sources_get(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(agent=_model_hub_service().get_agent_sources(backend))
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/sources", methods=["PUT"])
async def model_hub_agent_sources_put(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        agent = await _model_hub_service().set_agent_sources(
            backend,
            _model_hub_json_object("invalid_source_order"),
        )
        return _model_hub_success(agent=agent)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route(
    "/api/models/agents/<backend>/chains/reorder",
    methods=["POST"],
    allow_malformed_json=True,
)
async def model_hub_agent_chains_reorder_post(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        payload = request.json
        if payload is None:
            if request.has_body:
                raise ModelHubError("invalid_source_order")
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {"order"}:
            raise ModelHubError("invalid_source_order")
        if "order" in payload:
            agent = await _model_hub_service().reorder_agent_chains(
                backend,
                payload["order"],
            )
        else:
            agent = await _model_hub_service().reorder_agent_chains(backend)
        return _model_hub_success(agent=agent)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/mode", methods=["PATCH"])
async def model_hub_agent_mode_patch(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        agent = await _model_hub_service().set_agent_mode(
            backend,
            _model_hub_json_object("mode_switch_blocked").get("mode"),
        )
        return _model_hub_success(agent=agent)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/models", methods=["GET"])
def model_hub_agent_models_get(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        agent = _model_hub_service().get_agent_sources(backend)
        picker_agent = {
            "backend": agent["backend"],
            "mode": agent["mode"],
        }
        if "catalog_models" in agent:
            picker_agent["catalog_models"] = agent["catalog_models"]
        return _model_hub_success(agent=picker_agent)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/models", methods=["PUT"])
async def model_hub_agent_models_put(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        payload = _model_hub_json_object("mapping_target_unavailable")
        if set(payload) - {
            "baseline",
            "models",
            "expected_suppliers",
            "force",
            "would_remove_hops",
            "would_interrupt",
        }:
            raise ModelHubError("mapping_target_unavailable")
        result = await _model_hub_service().set_agent_models(
            backend,
            payload.get("baseline"),
            payload.get("models"),
            expected_suppliers=payload.get("expected_suppliers"),
            force=payload.get("force") is True,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/models/candidates", methods=["GET"])
def model_hub_agent_model_candidates_get(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        candidates = _model_hub_service().agent_model_candidates(backend)
        return _model_hub_success(candidates=candidates)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/catalog/models-dev", methods=["GET"])
def model_hub_models_dev_get():
    from core.handlers.model_hub import ModelHubError

    try:
        matches = _model_hub_service().models_dev_matches(
            request.args.get("query"),
        )
        return _model_hub_success(matches=matches)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/sources/<source_id>/models", methods=["POST"])
async def model_hub_source_models_post(source_id):
    from core.handlers.model_hub import ModelHubError

    try:
        source = await _model_hub_service().add_custom_model(
            source_id,
            _model_hub_json_object("mapping_target_unavailable")
        )
        return _model_hub_success(source=source), 201
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route(
    "/api/models/sources/<source_id>/models/<path:model_id>",
    methods=["PATCH"],
)
async def model_hub_source_models_patch(source_id, model_id):
    from core.handlers.model_hub import ModelHubError

    try:
        source = await _model_hub_service().update_model_reasoning_efforts(
            source_id,
            model_id,
            _model_hub_json_object("mapping_target_unavailable"),
        )
        return _model_hub_success(source=source)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route(
    "/api/models/sources/<source_id>/models/<path:model_id>",
    methods=["DELETE"],
)
async def model_hub_source_models_delete(source_id, model_id):
    from core.handlers.model_hub import ModelHubError

    try:
        payload = _model_hub_json_object("mapping_target_unavailable")
        result = await _model_hub_service().delete_custom_model(
            source_id,
            model_id,
            force=payload.get("force") is True,
            confirmed_remove_hops=payload.get("would_remove_hops"),
            confirmed_interruptions=payload.get("would_interrupt"),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/events", methods=["GET"])
def model_hub_events_get():
    from core.handlers.model_hub import ModelHubError

    try:
        limit = int(request.args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    try:
        events = _model_hub_service().list_events(limit=limit, before=request.args.get("before") or None)
        return _model_hub_success(events=events)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.get("/api/models/usage", include_in_schema=False)
async def model_hub_usage_get(starlette_request: FastAPIRequest):
    # Native rather than on the compat surface, and awaited rather than called:
    # this read blocks on the lock the usage ledger's writers hold across an
    # fsync, so reaching it from a threadpool worker would occupy that worker for
    # as long as the disk takes. The controller side of the same rule is in
    # `rpc.py`, which keeps the read off the event loop there.
    async def handler():
        from core.handlers.model_hub import ModelHubError
        from core.handlers.model_hub.usage import USAGE_DEFAULT_WINDOW_DAYS

        try:
            days = int(starlette_request.query_params.get("days") or USAGE_DEFAULT_WINDOW_DAYS)
        except (TypeError, ValueError):
            days = USAGE_DEFAULT_WINDOW_DAYS
        try:
            usage = await _model_hub_service().usage_summary(days=days)
            return _model_hub_success(usage=usage)
        except ModelHubError as exc:
            return _model_hub_error(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.route("/api/models/agents/<backend>/chains", methods=["GET"])
def model_hub_agent_chains_get(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        chains = _model_hub_service().agent_chains(backend)
        return _model_hub_success(chains=chains)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/chain", methods=["GET"])
def model_hub_agent_chain_get(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        model_id = str(request.args.get("model") or "").strip()
        if not model_id:
            raise ModelHubError("mapping_target_unavailable", status=409)
        chain = _model_hub_service().agent_chain(backend, model_id)
        return _model_hub_success(chain=chain)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/chain", methods=["PUT"])
async def model_hub_agent_chain_put(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        model_id = str(request.args.get("model") or "").strip()
        if not model_id:
            raise ModelHubError("mapping_target_unavailable", status=409)
        result = await _model_hub_service().set_agent_chain(
            backend,
            model_id,
            _model_hub_json_object("mapping_target_unavailable"),
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/agents/<backend>/probe", methods=["POST"])
async def model_hub_agent_probe_post(backend):
    from core.handlers.model_hub import ModelHubError

    try:
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {"model"}:
            raise ModelHubError("mapping_target_unavailable")
        model_id = payload.get("model")
        if model_id is not None and (
            not isinstance(model_id, str) or not model_id.strip()
        ):
            raise ModelHubError("mapping_target_unavailable")
        probe = await _model_hub_service().probe_agent(backend, model_id)
        return _model_hub_success(probe=probe)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/turns/<turn_id>/provenance", methods=["GET"])
def model_hub_turn_provenance_get(turn_id):
    from core.handlers.model_hub import ModelHubError

    try:
        provenance = _model_hub_service().get_turn_provenance(turn_id)
        return _model_hub_success(provenance=provenance)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/oauth/start", methods=["POST"])
async def model_hub_oauth_start():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(
            **await _model_hub_service().oauth_start(_model_hub_json_object("flow_not_found"))
        )
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/oauth/status/<flow_id>", methods=["GET"])
async def model_hub_oauth_status(flow_id):
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(**await _model_hub_service().oauth_status(flow_id))
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/oauth/submit", methods=["POST"])
async def model_hub_oauth_submit():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(
            **await _model_hub_service().oauth_submit(
                _model_hub_json_object("flow_not_found", status=404)
            )
        )
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/oauth/cancel", methods=["POST"])
async def model_hub_oauth_cancel():
    from core.handlers.model_hub import ModelHubError

    try:
        await _model_hub_service().oauth_cancel(
            _model_hub_json_object("flow_not_found", status=404).get("flow_id")
        )
        return _model_hub_success()
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/migration/scan", methods=["POST"])
def model_hub_migration_scan():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(scan=_model_hub_service().migration_scan())
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/migration/apply", methods=["POST"])
async def model_hub_migration_apply():
    from core.handlers.model_hub import ModelHubError

    try:
        result = await _model_hub_service().migration_apply(
            _model_hub_json_object("migration_item_conflict", status=409).get("item_ids")
        )
        return _model_hub_success(**result)
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/runtime/status", methods=["GET"])
async def model_hub_runtime_status():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(runtime=await _model_hub_service().runtime_status())
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/runtime/install", methods=["POST"])
async def model_hub_runtime_install():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(runtime=await _model_hub_service().runtime_install())
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/runtime/start", methods=["POST"])
async def model_hub_runtime_start():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(runtime=await _model_hub_service().runtime_start())
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/models/runtime/stop", methods=["POST"])
async def model_hub_runtime_stop():
    from core.handlers.model_hub import ModelHubError

    try:
        return _model_hub_success(runtime=await _model_hub_service().runtime_stop())
    except ModelHubError as exc:
        return _model_hub_error(exc)


@app.route("/api/platforms", methods=["GET"])
def platforms_get():
    from vibe import api

    return jsonify(api.get_platform_catalog())


@app.route("/api/agent-backends", methods=["GET"])
def agent_backends_get():
    from vibe import api

    return jsonify(api.get_agent_backend_catalog())


def _vibe_agent_error_response(exc: Exception):
    message = str(exc)
    if isinstance(exc, PermissionError):
        return jsonify({"ok": False, "code": "agent_access_forbidden", "message": message}), 403
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
        if code in {"agent_in_use", "agent_archived_read_only"}:
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
        user_context = getattr(g, "authorization_context", None)
        include_disabled = str(request.args.get("include_disabled") or request.args.get("all") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        include_archived = str(request.args.get("include_archived") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return jsonify(
            api.get_vibe_agents(
                backend=request.args.get("backend") or None,
                include_disabled=include_disabled,
                include_archived=include_archived,
                user_context=user_context,
            )
        )
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agent-onboarding", methods=["GET", "POST"])
def vibe_agent_onboarding():
    """Inventory or explicitly register existing Agents with Organization ACL."""

    from vibe import api

    try:
        user_context = getattr(g, "authorization_context", None)
        # Owner identity rather than can_manage_access_members: this is an
        # instance-wide one-way Agent migration, not member management. The store
        # repeats the check in ``_require_agent_onboarding_access`` so non-HTTP
        # callers are gated too; both layers ask the same question.
        if not _has_runtime_owner_access(user_context):
            return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
        if request.method == "POST":
            return jsonify(api.onboard_vibe_agents(user_context=user_context))
        return jsonify(api.get_vibe_agent_onboarding(user_context=user_context))
    except (ValueError, PermissionError) as exc:
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
    body = result.get("body") or {}
    context = _request_authorization_context()
    agents = _filter_runtime_records(context, body.get("agents") or [])
    counts = body.get("counts") if _has_runtime_owner_access(context) else _running_agent_counts(agents)
    return jsonify({**body, "agents": agents, "counts": counts})


@app.route("/api/running-agents/end", methods=["POST"])
async def running_agents_end():
    """Terminate one running agent's live runtime (Stop turn / disconnect / kill
    orphan), dispatched controller-side by backend+state. Proxies the internal
    socket; degrades to 503 when the controller is down."""
    from vibe import internal_client

    payload = request.json or {}
    context = _request_authorization_context()
    denied = _require_runtime_record(
        context,
        payload,
        not_found=({"ok": False, "error": "running_agent_not_found"}, 404),
    )
    if denied is not None:
        return jsonify(denied[0]), denied[1]
    try:
        result = await internal_client.end_running_agent(payload)
    except internal_client.InternalServerUnavailable:
        return jsonify({"ok": False, "unreachable": True}), 503
    body = result.get("body") or {}
    # Surface the controller's status (409 when the target couldn't be ended).
    return jsonify(body), (result.get("status_code") or 200)


# Contract A7: the run-graph endpoint lives OUTSIDE the ``/api/agents/<name>``
# namespace (``/api/agents-graph``). ``<name>`` is a user-creatable agent slug,
# so a ``/api/agents/graph`` path would be shadowed by — or shadow — an agent
# literally named ``graph``; a distinct top-level path avoids the collision.
@app.route("/api/agents-graph", methods=["GET"])
async def agents_graph_get():
    """Read-only run-graph payload for the Agents → 运行 tab.

    Assembles ``agent_sessions`` + ``agent_runs`` + ``scopes`` into the frozen
    contract §3 shape (``docs/plans/agents-run-graph-contract.md``). Liveness is
    controller-owned, so it is fetched from the internal running-agents snapshot
    and merged in; when the controller is unreachable the graph still renders
    from the DB (all nodes non-live) with a ``live_unreachable`` hint so the tab
    can show a "runtime unreachable — history only" state instead of a
    misleading empty graph."""
    from core.services import agent_graph
    from vibe import internal_client

    def _flag(value, default: bool) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    window = request.args.get("window") or agent_graph.DEFAULT_WINDOW
    project = request.args.get("project") or "all"
    include_ended = _flag(request.args.get("include_ended"), True)
    include_background = _flag(request.args.get("include_background"), True)

    live_agents: list = []
    live_unreachable = False
    try:
        result = await internal_client.list_running_agents()
        live_agents = (result.get("body") or {}).get("agents") or []
    except (internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout):
        # Controller down: fall back to a DB-only graph (history stays visible).
        live_unreachable = True

    payload = await asyncio.to_thread(
        agent_graph.build_graph,
        live_agents=live_agents,
        window=window,
        project=project,
        include_ended=include_ended,
        include_background=include_background,
        live_unreachable=live_unreachable,
    )
    context = _request_authorization_context()
    return jsonify(_authorized_graph_payload(context, payload))


@app.route("/api/agents/<name>", methods=["GET"])
def vibe_agent_get(name):
    from vibe import api

    try:
        return jsonify(api.get_vibe_agent(name, user_context=getattr(g, "authorization_context", None)))
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents", methods=["POST"])
def vibe_agents_post():
    from vibe import api

    try:
        return _vibe_agent_result_response(
            api.create_vibe_agent(
                request.json or {},
                user_context=getattr(g, "authorization_context", None),
            )
        )
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/import", methods=["POST"])
def vibe_agents_import_post():
    from vibe import api

    try:
        return _vibe_agent_result_response(
            api.import_vibe_agents(
                request.json or {},
                user_context=getattr(g, "authorization_context", None),
            )
        )
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/default", methods=["POST"])
def vibe_agents_default_post():
    from vibe import api

    payload = request.json or {}
    try:
        return jsonify(
            api.set_default_vibe_agent(
                payload.get("name") or "",
                user_context=getattr(g, "authorization_context", None),
            )
        )
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/<name>", methods=["PATCH"])
def vibe_agent_patch(name):
    from vibe import api

    try:
        return _vibe_agent_result_response(
            api.update_vibe_agent(
                name,
                request.json or {},
                user_context=getattr(g, "authorization_context", None),
            )
        )
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/agents/<name>", methods=["DELETE"])
def vibe_agent_delete(name):
    from vibe import api

    try:
        return _vibe_agent_result_response(
            api.remove_vibe_agent(
                name,
                user_context=getattr(g, "authorization_context", None),
            )
        )
    except (ValueError, PermissionError) as exc:
        return _vibe_agent_error_response(exc)


@app.route("/api/settings", methods=["GET"])
def settings_get():
    from vibe import api

    return jsonify(
        api.get_settings(
            request.args.get("platform") or None,
            user_context=getattr(g, "authorization_context", None),
        )
    )


def _vault_error_response(exc):
    from vibe import api

    if isinstance(exc, api.VaultApiError):
        return jsonify({"ok": False, "code": exc.code, "message": str(exc)}), exc.status
    return jsonify({"ok": False, "code": "vault_error", "message": str(exc)}), 400


def _webauthn_request_origin() -> str:
    origin = request.headers.get("Origin")
    if origin:
        return origin
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{scheme}://{host}"


def _vault_sandbox_webauthn_origin() -> str:
    return VAULT_SANDBOX_ORIGIN


@app.route("/api/vault/secrets", methods=["GET"])
def vault_secrets_get():
    from vibe import api

    try:
        return jsonify(
            api.get_vault_secrets(
                tag=request.args.get("tag") or None,
                tags=request.args.getlist("tag"),
                query=request.args.get("q") or None,
                kind=request.args.get("kind") or None,
                protection=request.args.get("protection") or None,
            )
        )
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/tags", methods=["GET"])
def vault_tags_get():
    from vibe import api

    try:
        return jsonify(api.get_vault_tags(query=request.args.get("q") or None, tag_type=request.args.get("type") or None))
    except ValueError as exc:
        return _vault_error_response(exc)


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


@app.route("/api/vault/sandbox/root-metadata", methods=["GET"])
def vault_sandbox_root_metadata_get():
    from vibe import api

    try:
        return jsonify(api.get_vault_sandbox_root_metadata())
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/agent-bindings:batch", methods=["POST"])
def vault_agent_bindings_batch_post():
    from vibe import api

    try:
        return jsonify(api.create_vault_agent_bindings_batch(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/agent-binding", methods=["POST"])
def vault_agent_binding_post():
    from vibe import api

    try:
        return jsonify(api.create_vault_agent_binding(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/settings", methods=["GET"])
def vault_settings_get():
    from vibe import api

    try:
        return jsonify(api.get_vault_settings())
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/settings", methods=["PATCH"])
def vault_settings_patch():
    from vibe import api

    try:
        return jsonify(api.save_vault_settings(request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/vmk", methods=["GET"])
def vault_vmk_get():
    from vibe import api

    return jsonify(api.get_vault_vmk())


@app.route("/api/vault/authz/factors/webauthn/options", methods=["POST"])
def vault_authz_webauthn_options_post():
    from vibe import api

    try:
        return jsonify(api.create_vault_authz_webauthn_options(origin=_vault_sandbox_webauthn_origin()))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/authz/factors/webauthn", methods=["POST"])
def vault_authz_webauthn_register_post():
    from vibe import api

    try:
        return jsonify(api.register_vault_authz_webauthn_factor(request.json or {}, origin=_vault_sandbox_webauthn_origin()))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/signing-addresses", methods=["POST"])
def vault_signing_addresses_post():
    from vibe import api

    try:
        return jsonify(api.derive_vault_signing_addresses(str((request.json or {}).get("public_key") or "")))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/secrets", methods=["POST"])
def vault_secrets_post():
    from vibe import api

    try:
        return jsonify(api.create_vault_secret(request.json or {}, origin=_vault_sandbox_webauthn_origin()))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/secrets/<name>", methods=["PATCH"])
def vault_secret_patch(name):
    from vibe import api

    try:
        return jsonify(api.update_vault_secret(name, request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/secrets/<name>", methods=["DELETE"])
def vault_secret_delete(name):
    from vibe import api

    try:
        return jsonify(api.delete_vault_secret(name))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/secrets/<name>/reveal-context", methods=["POST"])
def vault_secret_reveal_context_post(name):
    from vibe import api

    try:
        return jsonify(api.create_vault_reveal_context(name, request.json or {}))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/requests", methods=["GET"])
def vault_requests_get():
    from vibe import api

    raw_status = request.args.get("status")
    status = None if raw_status == "all" else raw_status or "pending"
    req_type = request.args.get("type") or None
    session = request.args.get("session") or None
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        limit = 100
    return jsonify(api.get_vault_requests(status=status, request_type=req_type, limit=limit, session=session))


@app.route("/api/vault/requests/<request_id>", methods=["GET"])
def vault_request_get(request_id):
    from vibe import api
    from storage import vault_service

    try:
        return jsonify(api.get_vault_request(request_id, audience=vault_service.REQUEST_AUDIENCE_UI))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/provision-requests/<name>", methods=["GET"])
def vault_provision_request_by_name_get(name):
    from vibe import api

    try:
        return jsonify(api.get_vault_provision_request_by_name(name))
    except ValueError as exc:
        return _vault_error_response(exc)


@app.route("/api/vault/provision-requests/by-id/<request_id>", methods=["GET"])
def vault_provision_request_get(request_id):
    from vibe import api

    try:
        return jsonify(api.get_vault_provision_request(request_id))
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


def _coded_error_response(code: str, message: str, status: int, **extra: Any):
    """THE error body for any route whose failure carries a machine-readable code.

    Nested ``error`` object, because the Web UI's shared parser
    (``selectApiErrorFields`` in ``ui/src/context/ApiContext.tsx``) reads ``data.error``
    FIRST and treats a *string* ``error`` as the code itself. So the flat
    ``{"error": "<sentence>", "code": "<code>"}`` shape silently DESTROYS the code:
    callers get ``ApiError.code == "<sentence>"``, ``errors.<code>`` never resolves,
    the sentence is rendered verbatim under every locale, and any client branch keyed
    on the code (e.g. the archived-session convergence subscription) never fires.

    The flat top-level ``code``/``message`` are kept alongside for the CLI and any
    direct consumer that reads them. ``extra`` carries route-specific detail fields.

    One builder rather than per-route dict literals so a new coded route inherits the
    right shape; ``tests/test_ui_server_fastapi.py`` guards both directions (every
    builder survives the parser, and no route hand-rolls the flat coded shape).
    """
    return (
        jsonify({"ok": False, "error": {"code": code, "message": message}, "code": code, "message": message, **extra}),
        status,
    )


def _settings_conflict_response(exc):
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    key = "error.settingsConflict"
    return _coded_error_response(
        exc.code,
        t(f"{key}.message", lang),
        409,
        hint=t(f"{key}.hint", lang),
        details={"scope_id": exc.scope_id},
    )


def _scope_agent_unavailable_response(exc):
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    key = "error.scopeAgentUnavailable"
    return _coded_error_response(
        exc.code,
        t(f"{key}.message", lang, agent=exc.agent_name),
        400,
        hint=t(f"{key}.hint", lang),
        details={"agent_name": exc.agent_name},
    )


def _project_agent_conflict_response(exc):
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    key = "error.projectAgentConflict"
    return _coded_error_response(
        exc.code,
        t(f"{key}.message", lang),
        409,
        hint=t(f"{key}.hint", lang),
        details=exc.details,
    )


def _project_agent_unavailable_response(exc):
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    key = "error.projectAgentUnavailable"
    return _coded_error_response(
        exc.code,
        t(f"{key}.message", lang, agent=exc.agent_name),
        400,
        hint=t(f"{key}.hint", lang),
        details={"agent_name": exc.agent_name},
    )


def _task_resume_blocked_response(exc):
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    return _coded_error_response(
        exc.code,
        t("error.taskOwnerUnavailable.message", lang),
        409,
        hint=t("error.taskOwnerUnavailable.hint", lang, id=exc.definition_id),
        details={
            "task_id": exc.definition_id,
            "owner_session_id": exc.owner_session_id,
        },
    )


def _task_schedule_retired_response(exc):
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    return _coded_error_response(
        exc.code,
        t("error.taskScheduleRetired.message", lang),
        409,
        hint=t("error.taskScheduleRetired.hint", lang, id=exc.definition_id),
        details={"task_id": exc.definition_id},
    )


def _show_page_error_response(exc):
    code = getattr(exc, "code", "invalid_show_page_request")
    if code == "resource_access_forbidden":
        status = 403
    elif code == "show_page_not_found":
        status = 404
    # A conflict (not a malformed request) when the page is in the wrong state or
    # the chosen suffix is already claimed.
    elif code in {"not_public", "not_shared", "share_id_taken", "show_access_conflict"}:
        status = 409
    else:
        status = 400
    return _coded_error_response(code, str(exc), status)


def _is_remote_show_page_request() -> bool:
    context = getattr(g, "authorization_context", None)
    return bool(
        (context is not None and context.is_remote)
        or getattr(g, "remote_session_payload", None) is not None
        or _is_remote_access_request(_load_remote_access_config())
    )


def _show_page_payload_for_request(payload: dict, context: Any = None) -> dict:
    context = _request_authorization_context(context)
    if context is None or _has_runtime_owner_access(context):
        return payload
    from storage import project_access_service

    engine = _projects_engine()
    with engine.connect() as conn:
        return _show_page_payload_for_connection(payload, context, conn)


def _show_page_payload_for_connection(payload: dict, context: Any, conn: Any) -> dict:
    from storage import project_access_service

    session_id = str(payload.get("session_id") or "")
    if not project_access_service.session_exists(conn, session_id):
        return {key: value for key, value in payload.items() if key != "path"}
    project_id = project_access_service.get_session_project_id(conn, session_id)
    effective_role = project_access_service.get_effective_session_role(
        conn,
        context,
        session_id,
    )
    if project_access_service.role_allows(effective_role, "editor"):
        return payload
    # Legacy and IM-scoped pages have no project role. §3.2 makes the Instance
    # Editor role their authority for the page owner/editor, but it must not
    # override an effective project Viewer downgrade on project-attached sessions.
    if project_id is None and context.has_role("editor"):
        return payload
    return {key: value for key, value in payload.items() if key != "path"}


def _show_page_payloads_for_request(payloads: list[dict], context: Any = None) -> list[dict]:
    context = _request_authorization_context(context)
    if context is None or _has_runtime_owner_access(context):
        return payloads
    engine = _projects_engine()
    with engine.connect() as conn:
        return [
            _show_page_payload_for_connection(payload, context, conn)
            for payload in payloads
        ]


def _show_page_response_for_request(response: Any, context: Any = None) -> Any:
    """Project every Show Page payload embedded in a mutation response."""

    if not isinstance(response, dict):
        return response
    if isinstance(response.get("session_id"), str):
        return _show_page_payload_for_request(response, context)
    projected = dict(response)
    for key in ("page", "show_page"):
        payload = response.get(key)
        if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
            projected[key] = _show_page_payload_for_request(payload, context)
    return projected


@app.route("/api/show-pages", methods=["GET"])
def show_pages_list_get():
    from vibe import api

    context = getattr(g, "authorization_context", None)
    resource_context = _request_authorization_context(context)
    payload = api.list_show_pages(user_context=resource_context)
    payload = {
        **payload,
        "pages": _show_page_payloads_for_request(payload.get("pages", []), context),
    }
    return jsonify(payload)


@app.route("/api/show-pages/<session_id>/availability", methods=["POST"])
def show_page_availability_post(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    payload = request.json if isinstance(request.json, dict) else {}
    if set(payload) != {"offline"} or not isinstance(payload.get("offline"), bool):
        return _show_page_error_response(
            ShowPageError("Invalid Show Page availability.", code="invalid_availability")
        )
    try:
        context = _request_authorization_context()
        return jsonify(
            _show_page_response_for_request(
                api.set_show_page_availability(
                    session_id,
                    payload["offline"],
                    user_context=context,
                ),
                context,
            )
        )
    except ShowPageError as exc:
        return _show_page_error_response(exc)


@app.route("/api/show-pages/<session_id>", methods=["GET"])
def show_page_get(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    status = 200
    try:
        context = _request_authorization_context()
        response = jsonify(
            _show_page_payload_for_request(
                api.get_show_page(session_id, user_context=context),
                context,
            )
        )
    except ShowPageError as exc:
        response, status = _show_page_error_response(exc)
    # Same per-caller page data the ensure POST returned, now over a method caches
    # are allowed to store by default — so EVERY outcome of this route is marked,
    # not just the success. A 404 is heuristically cacheable, and a cached "no page
    # here" would survive the page's creation and leave the share panel empty until
    # it expired.
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"
    return response, status


@app.route("/api/show-pages/<session_id>/ensure", methods=["POST"])
def show_page_ensure_post(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    try:
        context = _request_authorization_context()
        return jsonify(
            _show_page_payload_for_request(
                api.ensure_show_page(
                    session_id,
                    user_context=context,
                ),
                context,
            )
        )
    except ShowPageError as exc:
        return _show_page_error_response(exc)


@app.route("/api/show-pages/<session_id>/access", methods=["GET"])
def show_page_access_get(session_id):
    from core.show_pages import ShowPageError
    from vibe import api

    try:
        response = jsonify(
            api.get_show_page_access(
                session_id,
                user_context=_request_authorization_context(),
            )
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Vary"] = "Cookie"
        return response
    except ShowPageError as exc:
        return _show_page_error_response(exc)


def _show_access_http_response(body: dict[str, Any], status: int = 200):
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return response


def _show_access_page_identity_matches(body: Any, page_id: str) -> bool:
    return bool(
        isinstance(body, dict)
        and isinstance(body.get("show_access"), dict)
        and body["show_access"].get("page_id") == page_id
    )


def _valid_show_access_apply_payload(payload: dict[str, Any]) -> bool:
    from core.show_pages import parse_show_access_apply_request

    return parse_show_access_apply_request(payload) is not None


@app.route("/api/show-pages/<session_id>/access-settings/read", methods=["POST"])
async def show_page_access_settings_read(session_id):
    from core.show_pages import ShowPageError
    from vibe import api, internal_client

    payload = request.json if isinstance(request.json, dict) else {}
    if set(payload) != {"page_id"} or payload.get("page_id") != session_id:
        return _show_access_http_response(
            {"ok": False, "error": "show_access_page_identity_mismatch"},
            400,
        )
    try:
        api.require_show_access_settings_control(
            session_id,
            user_context=_request_authorization_context(),
        )
    except ShowPageError as exc:
        return _show_page_error_response(exc)
    try:
        result = await internal_client.show_access_settings_read(payload)
    except internal_client.InternalServerUnavailable:
        return _show_access_http_response(
            {"ok": False, "error": "show_access_controller_unavailable"},
            503,
        )
    except internal_client.InternalServerTimeout:
        return _show_access_http_response(
            {"ok": False, "error": "show_access_controller_timeout"},
            504,
        )
    body = result.get("body") or {}
    status = int(result.get("status_code") or 500)
    if status == 200 and not _show_access_page_identity_matches(body, session_id):
        return _show_access_http_response(
            {"ok": False, "error": "show_access_internal_protocol_error"},
            502,
        )
    return _show_access_http_response(body, status)


@app.route("/api/show-pages/<session_id>/access-settings/apply", methods=["POST"])
async def show_page_access_settings_apply(session_id):
    from core.show_pages import ShowPageError
    from vibe import api, internal_client

    payload = request.json if isinstance(request.json, dict) else {}
    if payload.get("page_id") != session_id:
        return _show_access_http_response(
            {"ok": False, "error": "show_access_page_identity_mismatch"},
            400,
        )
    if not _valid_show_access_apply_payload(payload):
        return _show_access_http_response(
            {"ok": False, "error": "invalid_show_access_apply_request"},
            400,
        )
    try:
        api.require_show_access_settings_control(
            session_id,
            user_context=_request_authorization_context(),
        )
    except ShowPageError as exc:
        return _show_page_error_response(exc)
    try:
        result = await internal_client.show_access_apply(payload)
    except internal_client.InternalServerUnavailable:
        return _show_access_http_response(
            {"ok": False, "error": "show_access_controller_unavailable"},
            503,
        )
    except internal_client.InternalServerTimeout:
        return _show_access_http_response(
            {"ok": False, "error": "show_access_controller_timeout"},
            504,
        )
    body = result.get("body") or {}
    status = int(result.get("status_code") or 500)
    if status == 200 and not _show_access_page_identity_matches(body, session_id):
        return _show_access_http_response(
            {"ok": False, "error": "show_access_internal_protocol_error"},
            502,
        )
    return _show_access_http_response(body, status)


def _show_page_icon_not_found():
    # 404 on any missing page / no icon / policy rejection — the frontend's
    # onerror -> letter-avatar fallback covers it. No body needed. `no-store` so a
    # heuristically-cached 404 can't strand the letter fallback on the stable
    # sid-only URL after the page later adds the icon.
    response = Response("", status=404, mimetype="text/plain")
    response.headers["Cache-Control"] = "no-store"
    return response


def _show_page_access_forbidden_response():
    response = Response("", status=403, mimetype="text/plain")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/show-pages/<session_id>/icon", methods=["GET", "HEAD"])
def show_page_icon_get(session_id):
    # The page's own HTML icon, served as the single chokepoint (§7.1f): ALL href
    # resolution + policy (document semantics incl. <base>; reject api / traversal /
    # absolute / root-relative / external / non-image / stock) lives server-side in
    # resolve_show_page_icon. The `?v=` token NEVER selects the file — resolution is
    # sid + workspace only — it is validated as a read-time CONTENT ASSERTION so the
    # stable URL's `immutable` cache is honest. Auth rides the global /api hooks.
    #
    # Contract: bytes-or-404, NEVER a 500 — the icon is decorative. A malformed
    # session id (validate_session_id -> ShowPageError), a page-authored href that
    # resolves to a filesystem-invalid path (ValueError/OSError), a live-edit swap,
    # or a token mismatch all degrade to the letter-avatar fallback, never erroring.
    from core.show_pages import ShowPageError, ShowPageStore, read_show_page_icon

    try:
        store = ShowPageStore()
        try:
            page = store.require_access(
                session_id,
                user_context=_request_authorization_context(),
            )
            # Any of the user's own pages — private, public, OR offline — may serve
            # its static icon: the payload advertises an icon token for all of them
            # and the inventory lists them, so gating by visibility would strand
            # offline rows / pinned offline apps on the letter avatar despite an icon.
            if page is None:
                return _show_page_icon_not_found()
            # read_show_page_icon does the race-safe read (O_NOFOLLOW + fstat cap on
            # the descriptor) and enforces `?v=` as a content assertion — resolution
            # stays sid-only; the query can never pick a different file.
            result = read_show_page_icon(page.session_id, request.args.get("v", ""))
        finally:
            store.close()
        if result is None:
            return _show_page_icon_not_found()
        data, content_type = result
        response = Response(data, mimetype=content_type)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # A directly-navigated SVG must not execute scripts in the API origin.
        response.headers["Content-Security-Policy"] = "sandbox"
        # Local URLs may cache immutably because `?v=` is enforced against the served
        # bytes. Remote responses must revalidate the ACL on every request so a revoked
        # user or a different account in the same browser cannot reuse cached bytes.
        # A plain Response also never honors `Range` (no 206/416).
        response.headers["Cache-Control"] = (
            "private, no-store"
            if _is_remote_show_page_request()
            else "private, max-age=604800, immutable"
        )
        return response
    except ShowPageError as exc:
        if exc.code == "resource_access_forbidden":
            return _show_page_access_forbidden_response()
        return _show_page_icon_not_found()
    except (ValueError, OSError):
        # Enforce the bytes-or-404 contract at the boundary: a bad session id, a bad
        # page-authored icon, or a file that vanished mid-race must fall back, not 500.
        return _show_page_icon_not_found()


def _dock_error_response(exc):
    code = getattr(exc, "code", "invalid_dock_request")
    # A missing Show Page (nothing to pin) is a 404; a malformed id or a bad
    # order is a 400. Structured ``error`` so the Web UI's shared handler can
    # localize via ``errors.<code>`` and fall back to the human message.
    if code == "resource_access_forbidden":
        status = 403
    elif code in {"show_page_not_found", "session_not_found"}:
        status = 404
    else:
        status = 400
    return _coded_error_response(code, str(exc), status)


@app.route("/api/dock", methods=["GET"])
def dock_get():
    from vibe import api

    return jsonify(api.get_dock(user_context=_request_authorization_context()))


@app.route("/api/dock/pins", methods=["POST"])
def dock_pin_post():
    from core.dock_store import DockError
    from core.show_pages import ShowPageError
    from vibe import api

    payload = request.json or {}
    try:
        return jsonify(
            api.pin_dock_show_page(
                str(payload.get("session_id") or ""),
                user_context=_request_authorization_context(),
            )
        )
    except (DockError, ShowPageError) as exc:
        return _dock_error_response(exc)


@app.route("/api/dock/pins/<session_id>", methods=["DELETE"])
def dock_unpin_delete(session_id):
    from core.dock_store import DockError
    from core.show_pages import ShowPageError
    from vibe import api

    try:
        return jsonify(
            api.unpin_dock_show_page(
                session_id,
                user_context=_request_authorization_context(),
            )
        )
    except (DockError, ShowPageError) as exc:
        return _dock_error_response(exc)


@app.route("/api/dock/order", methods=["PUT"])
def dock_order_put():
    from core.dock_store import DockError
    from core.show_pages import ShowPageError
    from vibe import api

    payload = request.json or {}
    try:
        # ``known`` (optional) is the client's baseline id set for optimistic
        # concurrency — set_dock_order rejects the write as stale when it no
        # longer matches the server's, so a stale tab can't silently undock a pin
        # another tab installed.
        return jsonify(
            api.set_dock_order(
                payload.get("order"),
                known=payload.get("known"),
                user_context=_request_authorization_context(),
            )
        )
    except (DockError, ShowPageError) as exc:
        return _dock_error_response(exc)


@app.route("/api/workbench/prefs", methods=["GET"])
def workbench_prefs_get():
    from vibe import api

    return jsonify(api.get_workbench_prefs())


@app.route("/api/workbench/prefs", methods=["PUT"])
def workbench_prefs_put():
    from vibe import api

    payload = request.json or {}
    raw = payload.get("background_work_banner_enabled")
    enabled = bool(raw) if raw is not None else None
    return jsonify(api.set_workbench_prefs(background_work_banner_enabled=enabled))


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
    Subscription identity is durable (#1434): a still-valid session cookie
    keeps attributing subscriptions and test sends to its subject across
    sliding-session renewal. The request's shared authorization resolver
    enforces confirmed revocation, so no separate interactive-refresh cutoff
    is applied here.
    """

    config = _load_remote_access_config()
    if config is not None:
        try:
            payload = _resolved_remote_session_payload(config)
            if payload and payload.get("sub"):
                return f"remote:{payload['sub']}"
        except Exception:
            logger.debug("web push: could not resolve remote user key", exc_info=True)
    return "local"


def _workbench_author_id() -> str | None:
    """Return an author only when the browser passes strict Memory admission."""

    memory_user_key = memory_ui_user_key()
    prefix = "avibe:"
    if not isinstance(memory_user_key, str) or not memory_user_key.startswith(prefix):
        return None
    author_id = memory_user_key[len(prefix) :].strip()
    return author_id or None


def _web_push_normal_delivery_diagnostics() -> dict:
    """Explain the normal-path authorization gates for the calling owner.

    The Web Push test send skips the authorization gates that normal inbox
    delivery applies. Reporting the same evaluation here lets a user see why a
    test notification arrives while a normal one does not, without exposing
    protected content or credentials.
    """

    from core import web_push_notifications

    user_key = _web_push_user_key()
    try:
        evaluation = web_push_notifications.evaluate_delivery_authorization_for_context(
            user_key,
            getattr(g, "authorization_context", None),
        )
    except Exception:
        logger.debug("web push: normal delivery evaluation failed", exc_info=True)
        evaluation = {
            "user_key": user_key,
            "policy": "unknown",
            "authorized": None,
            "disposition": None,
            "reason": "evaluation_unavailable",
        }
    try:
        evaluation["recent_deliveries"] = web_push_notifications.recent_delivery_dispositions(
            user_key=user_key,
        )
    except Exception:
        logger.debug("web push: recent delivery lookup failed", exc_info=True)
        evaluation["recent_deliveries"] = []
    return evaluation


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
            "normal_delivery": _web_push_normal_delivery_diagnostics(),
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
    return jsonify(
        {
            "ok": failed == 0,
            "sent": sent,
            "failed": failed,
            "normal_delivery": _web_push_normal_delivery_diagnostics(),
        }
    )


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


def _save_config_and_runtime_decisions(payload: dict) -> tuple[V2Config, bool, bool, bool, list[str]]:
    from vibe import api
    from vibe import remote_access

    with CONFIG_LOCK:
        previous_config = _load_remote_access_config()
        config = api.save_config(payload, generic_remote_access=True)
        previous_cloud = previous_config.remote_access.vibe_cloud if previous_config is not None else None
        current_cloud = config.remote_access.vibe_cloud
        old_instance_id = str(previous_cloud.instance_id or "") if previous_cloud is not None else ""
        instance_changed = bool(old_instance_id and old_instance_id != str(current_cloud.instance_id or ""))
        pairing_disabled = bool(
            previous_cloud is not None
            and previous_cloud.enabled
            and not current_cloud.enabled
        )
        if instance_changed or pairing_disabled:
            try:
                from storage import remote_access_authorization_service

                remote_access_authorization_service.delete_for_instance(old_instance_id)
            except Exception:
                logger.warning("Old remote authorization cleanup failed after config save", exc_info=True)
        should_reconcile_remote_access = False
        if _remote_access_settings_changed(previous_config, config, payload):
            if _should_rotate_remote_session_secret(previous_config, config, payload):
                remote_access.rotate_session_secret(config)
                config = V2Config.load()
            should_reconcile_remote_access = True
        should_reconcile_platforms = _platform_runtime_fields_changed(previous_config, config, payload)
        should_reconcile_activity_streaming = _activity_streaming_flag_touched(payload)
        changed_agent_backends = _changed_agent_backend_runtimes(previous_config, config, payload)
        return (
            config,
            should_reconcile_remote_access,
            should_reconcile_platforms,
            should_reconcile_activity_streaming,
            changed_agent_backends,
        )


_UI_RUNTIME_ACTIVE = False


def _ensure_remote_access_monitoring(config: V2Config | None = None) -> None:
    if not _UI_RUNTIME_ACTIVE:
        return
    from vibe import remote_access

    remote_access.start_runtime_monitoring(config)


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

    # The decoded body, not the usual ``request.json or {}``: this route's two
    # validators already require a JSON object — ``editor_config_write_payload``
    # for an Editor, ``api.save_config`` for everyone — and that coercion turns
    # every falsy body (``null``, ``[]``, ``false``, ``0``, ``""``, or none at
    # all) into an empty patch before either of them sees it, so a malformed
    # write saved nothing and answered 200. Passing the value through keeps one
    # property — a config write is an object — instead of an enumeration of the
    # falsy shapes that happen to exist today.
    payload = request.json
    authorization_context = getattr(g, "authorization_context", None)
    # Persisting a credential is an Owner act, so the write schema is selected
    # by ownership and by nothing else. ``can_manage_instance`` used to pick it,
    # which stopped being an owner test the moment a member acquired that
    # capability: a member fell past this branch into a filter that removed only
    # ``remote_access``, and every other section — ``slack.bot_token``,
    # ``discord.bot_token``, ``lark.app_secret``, gateway secrets — reached the
    # save path and was reconciled onto the live platform.
    #
    # There is one non-owner write schema and it is ``_EDITOR_CONFIG_WRITE_FIELDS``,
    # a closed allowlist of non-secret preferences. Closed is the whole point:
    # no credential-bearing section has to be enumerated here, and a secret
    # added to ``api._PLATFORM_SECRET_FIELDS`` (or to any config section) is
    # unreachable below Owner by construction rather than by remembering to add
    # it to a strip list. Pairing identity is covered by the same rule —
    # ``remote_access`` is not on the allowlist, so it is refused outright
    # instead of silently dropped.
    non_owner_write = (
        authorization_context is not None and not authorization_context.can_manage_access_members
    )
    if non_owner_write:
        try:
            payload = api.editor_config_write_payload(payload)
        except ValueError as exc:
            code = api.editor_config_write_error_code(exc)
            return jsonify({"ok": False, "error": {"code": code, "message": code}}), 400
    remote_access_runtime = None
    try:
        (
            config,
            should_reconcile_remote_access,
            should_reconcile_platforms,
            should_reconcile_activity_streaming,
            changed_agent_backends,
        ) = await asyncio.to_thread(
            _save_config_and_runtime_decisions,
            payload,
        )
    except ValueError as exc:
        # Same chokepoint as the allowlist rejection above: a non-owner write
        # answers with a stable code whichever layer refused it, including
        # value validation raised deep inside ``V2Config.from_payload``. Owner
        # saves keep the descriptive message the Settings pages already show.
        if non_owner_write:
            code = api.editor_config_write_error_code(exc)
            return jsonify({"ok": False, "error": {"code": code, "message": code}}), 400
        message = str(exc)
        return jsonify({"ok": False, "error": message, "message": message}), 400
    if should_reconcile_remote_access:
        remote_access_runtime = await asyncio.to_thread(remote_access.reconcile)
    await asyncio.to_thread(_ensure_remote_access_monitoring, config)
    activity_streaming_runtime = None
    if should_reconcile_activity_streaming:
        try:
            result = await internal_client.invalidate_activity_streaming()
            body = result.get("body") or {}
            hot_reconciled = result.get("status_code") == 200 and bool(body.get("ok"))
            activity_streaming_runtime = {
                "ok": hot_reconciled,
                "hot_reconciled": hot_reconciled,
                "body": body,
            }
        except internal_client.InternalServerUnavailable as exc:
            # The controller's bounded cache remains the degradation path; the
            # persisted setting is still authoritative and self-heals within its TTL.
            activity_streaming_runtime = {
                "ok": False,
                "hot_reconciled": False,
                "error": str(exc),
            }
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
    agent_backend_runtime = None
    if changed_agent_backends:
        try:
            result = await internal_client.reconcile_agent_backends(changed_agent_backends)
            body = result.get("body") or {}
            hot_reconciled = result.get("status_code") == 200 and bool(body.get("ok"))
            agent_backend_runtime = {
                "ok": hot_reconciled,
                "hot_reconciled": hot_reconciled,
                "backends": changed_agent_backends,
                "body": body,
            }
        except internal_client.InternalServerUnavailable as exc:
            agent_backend_runtime = {
                "ok": False,
                "hot_reconciled": False,
                "backends": changed_agent_backends,
                "error": str(exc),
            }

        if not agent_backend_runtime.get("ok"):
            from vibe import runtime

            service_running = await asyncio.to_thread(runtime.service_process_running)
            if service_running:
                if platform_runtime and platform_runtime.get("restart_scheduled"):
                    agent_backend_runtime["restart_scheduled"] = True
                    if platform_runtime.get("restart"):
                        agent_backend_runtime["restart"] = platform_runtime["restart"]
                else:
                    restart_result = await asyncio.to_thread(_schedule_service_restart_for_config_fallback)
                    agent_backend_runtime["restart_scheduled"] = bool(restart_result.get("ok"))
                    if restart_result.get("ok"):
                        agent_backend_runtime["restart"] = restart_result.get("restart")
                    else:
                        agent_backend_runtime["restart_error"] = restart_result.get("error")
                        agent_backend_runtime["restart_code"] = restart_result.get("code")
            else:
                agent_backend_runtime["apply_on_next_start"] = True
    authorization_context = getattr(g, "authorization_context", None)
    response_payload = _config_api_payload_for_context(config, authorization_context)
    if remote_access_runtime is not None:
        response_payload["remote_access_runtime"] = remote_access_runtime
    if platform_runtime is not None:
        response_payload["platform_runtime"] = platform_runtime
    if activity_streaming_runtime is not None:
        response_payload["activity_streaming_runtime"] = activity_streaming_runtime
    if agent_backend_runtime is not None:
        response_payload["agent_backend_runtime"] = agent_backend_runtime
    return jsonify(response_payload)


@app.route("/api/remote-access/status", methods=["GET"])
def remote_access_status():
    from vibe import remote_access

    config = _load_remote_access_config()
    client_colo = None
    remote_request = bool(
        config is not None
        and _is_remote_access_request(config)
    )
    if remote_request:
        from vibe import cloudflare_network

        # CF-Ray is diagnostic-only input. It never participates in access
        # control or route recovery, so a locally spoofed value can at most
        # change the caller's own displayed ingress location.
        client_colo = cloudflare_network.parse_cf_ray_colo(request.headers.get("CF-Ray"))
    status_payload = remote_access.status(
        config,
        client_colo=client_colo,
        client_access="remote" if remote_request else "local",
        include_network_path=True,
    )
    if remote_request:
        # Keep host internals (cloudflared PID, absolute binary path/version)
        # local-only. The Remote Access page itself is used across the tunnel,
        # so the projection must still carry the fields that page renders:
        # connector health, saved tunnel controls, quality, and the network
        # path that owns the "Technical details" disclosure.
        status_payload = {
            key: status_payload[key]
            for key in _REMOTE_ACCESS_STATUS_PUBLIC_FIELDS
            if key in status_payload
        }
    return jsonify(status_payload)


@app.route("/api/remote-access/vibe-cloud/pair", methods=["POST"])
def remote_access_vibe_cloud_pair():
    from vibe import remote_access

    authorization_context = getattr(g, "authorization_context", None)
    if authorization_context is None or not authorization_context.can_manage_access_members:
        return jsonify({"ok": False, "error": "instance_access_forbidden"}), 403
    payload = request.json or {}
    result = remote_access.pair(
        payload.get("pairing_key", ""),
        payload.get("backend_url", "https://avibe.bot"),
        payload.get("device_name", "avibe"),
    )
    if result.get("ok"):
        _ensure_remote_access_monitoring()
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/remote-access/start", methods=["POST"])
def remote_access_start():
    from vibe import remote_access

    result = remote_access.start()
    if result.get("ok"):
        _ensure_remote_access_monitoring()
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/remote-access/stop", methods=["POST"])
def remote_access_stop():
    from vibe import remote_access

    result = remote_access.stop()
    return jsonify(result), 200 if result.get("ok") else 400


@app.route("/api/remote-access/optimize-route", methods=["POST"])
def remote_access_optimize_route():
    from vibe import remote_access

    result = remote_access.optimize_route()
    return jsonify(result), 202 if result.get("ok") else 409


@app.route("/api/remote-access/network-interfaces", methods=["GET"])
def remote_access_network_interfaces():
    from vibe import remote_access

    return jsonify(remote_access.network_interfaces())


@app.route("/api/remote-access/settings", methods=["POST"])
async def remote_access_settings():
    from vibe import remote_access

    payload = request.json or {}
    result = await asyncio.to_thread(remote_access.apply_settings, payload)
    if result.get("ok"):
        await asyncio.to_thread(_ensure_remote_access_monitoring)
        return jsonify(result)
    status_code = 400 if result.get("error") == "remote_access_settings_invalid" else 409
    return jsonify(result), status_code


@app.route("/api/remote-access/diagnostics", methods=["POST"])
async def remote_access_diagnostics():
    from vibe import remote_access

    try:
        result = await asyncio.to_thread(remote_access.connectivity_diagnostics)
    except Exception as exc:
        logger.warning("Tunnel connectivity diagnostics failed", exc_info=True)
        result = {
            "ok": False,
            "error": "remote_access_diagnostics_failed",
            "detail": str(exc),
        }
    return jsonify(result), 200 if result.get("ok") else 409


@app.route("/auth/login", methods=["GET"])
def remote_access_login():
    from vibe import remote_access

    config = _load_remote_access_config()
    if config is None or not _is_remote_access_request(config):
        return jsonify({"error": "remote_access_not_enabled"}), 400
    cloud = config.remote_access.vibe_cloud
    if not cloud.enabled:
        return jsonify({"error": "remote_access_disabled"}), 503
    if not cloud.session_secret:
        return jsonify({"error": "remote_access_session_secret_missing"}), 503

    next_target = _safe_remote_redirect_target(request.args.get("next"))
    identity = remote_access.parse_session_identity(
        config,
        request.cookies.get(remote_access.SESSION_COOKIE_NAME),
    )
    if identity is not None:
        resolution = remote_access.resolve_current_authorization(
            config,
            identity,
            refresh_revoked=True,
        )
        if resolution.state == "revoked":
            return jsonify({"error": "remote_access_revoked"}), 403
        if resolution.state == "unavailable":
            return jsonify({"error": "remote_access_authorization_unavailable"}), 503
    else:
        resolution = None
    if resolution is not None and resolution.current:
        return redirect(next_target)
    if _auth_rate_limited():
        return _auth_rate_limit_response()
    return _redirect_to_vibe_cloud_login(config, next_target=next_target)


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
        redirect_uri = str(cookie_state.get("redirect_uri") or cloud.redirect_uri)
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
        redirect_uri = str(store_record.get("redirect_uri") or cloud.redirect_uri)
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
        result = remote_access.exchange_oauth_code(
            config,
            _oauth_callback_arg("code") or "",
            code_verifier,
            redirect_uri=redirect_uri,
        )
        claims = result["claims"]
        session_claims = result.get("session_claims")
        if not isinstance(session_claims, dict):
            raise remote_access.OAuthCodeExchangeError("invalid_session_claims")
    except Exception as exc:
        # Unauthenticated-reachable (valid handshake + bad code), so rate-limited.
        _log_oauth_callback_failure("code_exchange", exc)
        error, diagnostics = _oauth_exchange_error_diagnostics(exc)
        return _oauth_callback_error_response(error, next_target=next_target, diagnostics=diagnostics)
    if claims.get("nonce") != handshake_nonce:
        return _oauth_callback_error_response("invalid_oauth_nonce", next_target=next_target)
    try:
        session_cookie = remote_access.make_session_cookie(
            config,
            str(claims.get("email", "")),
            str(claims.get("sub", "")),
            session_claims=session_claims,
        )
    except Exception as exc:
        _log_oauth_callback_failure("session_cookie", exc)
        error, diagnostics = _oauth_exchange_error_diagnostics(exc)
        return _oauth_callback_error_response(error, next_target=next_target, diagnostics=diagnostics)
    response = Response(status=302)
    response.headers["Location"] = _safe_remote_redirect_target(next_target)
    response.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        session_cookie,
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
    from vibe.authorization import context_from_session_payload, instance_owner_context

    config = _load_remote_access_config()
    instance_kind = None
    if config is not None:
        configured_instance_kind = config.remote_access.vibe_cloud.instance_kind
        if configured_instance_kind in {"personal", "organization"}:
            instance_kind = configured_instance_kind
    if config is None or not _is_remote_access_request(config):
        context = instance_owner_context()
        response = jsonify(
            {
                "remote": False,
                "instance_kind": instance_kind,
                "instance_role": "owner",
                "capabilities": context.capability_projection(),
            }
        )
    else:
        identity = remote_access.parse_session_identity(
            config,
            request.cookies.get(remote_access.SESSION_COOKIE_NAME),
        )
        if identity is None:
            response = jsonify({"remote": True, "authenticated": False})
        else:
            resolution = remote_access.resolve_current_authorization(config, identity)
            if resolution.policy in {"personal", "organization"}:
                instance_kind = resolution.policy
            if resolution.state == "unavailable":
                response = jsonify(
                    {
                        "remote": True,
                        "authenticated": True,
                        "email": str(identity.get("email", "")),
                        "sub": str(identity.get("sub", "")),
                        "instance_kind": instance_kind,
                        "authorization_state": "unavailable",
                    }
                )
            elif resolution.state == "revoked":
                response = jsonify(
                    {
                        "remote": True,
                        "authenticated": True,
                        "email": str(identity.get("email", "")),
                        "sub": str(identity.get("sub", "")),
                        "instance_kind": instance_kind,
                        "authorization_state": "revoked",
                    }
                )
            elif not resolution.current:
                response = jsonify({"remote": True, "authenticated": False})
            else:
                payload = resolution.payload
                context = context_from_session_payload(payload)
                response = jsonify(
                    {
                        "remote": True,
                        "authenticated": True,
                        "email": str(payload.get("email", "")),
                        "sub": str(payload.get("sub", "")),
                        "instance_kind": instance_kind,
                        "instance_role": context.instance_role,
                        "capabilities": context.capability_projection(),
                        "authorization_state": "current",
                        "authorization_policy": resolution.policy,
                    }
                )
    # Identity payload must never be cached by intermediaries (Cloudflare etc.).
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"
    return response


def _remote_resource_access_context():
    """Resolve the signed remote session required by local ACL metadata APIs."""

    from storage import resource_access_service
    from vibe import remote_access

    config = _load_remote_access_config()
    if (
        config is None
        or not config.remote_access.vibe_cloud.enabled
        or not _is_remote_access_request(config)
    ):
        return None, None, None
    payload = _resolved_remote_session_payload(config)
    if payload is None:
        return config, None, None
    return config, payload, resource_access_service.current_resource_context(
        payload,
        is_remote=True,
    )


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
    cookie_value = request.cookies.get(remote_access.SESSION_COOKIE_NAME)
    identity = remote_access.parse_session_identity(config, cookie_value)
    if identity is None:
        # Local-origin requests never carry the avibe.bot session cookie, so a
        # missing identity is the expected state there, not an expired remote
        # session. Emitting the login-required signal would trip the frontend's
        # global auth-recovery redirect to ``/auth/login``, which the local
        # host rejects with ``remote_access_not_enabled`` (issue #1491).
        # Degrade to the documented ``cloud_unavailable`` fallback instead;
        # only genuine remote-access requests get the login signal.
        if not _is_remote_access_request(config):
            return jsonify({"error": "cloud_unavailable"}), 503
        return jsonify({"ok": False, "error": "remote_access_login_required"}), 401
    resolution = remote_access.resolve_current_authorization(config, identity)
    if resolution.state == "revoked":
        return jsonify({"ok": False, "error": "remote_access_revoked"}), 403
    if resolution.state == "unavailable":
        return jsonify(
            {"ok": False, "error": "remote_access_authorization_unavailable"}
        ), 503
    result = (
        remote_access.cloud_token_for_authorization(config, resolution.payload)
        if resolution.current
        else None
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

    config = _load_remote_access_config()
    identity = (
        remote_access.parse_session_identity(
            config,
            request.cookies.get(remote_access.SESSION_COOKIE_NAME),
        )
        if config is not None
        else None
    )
    if identity is not None:
        remote_access.revoke_browser_session(identity)
        body = request.json if isinstance(request.json, dict) else {}
        device_id = body.get("device_id")
        endpoint = body.get("endpoint")
        try:
            from storage import web_push_service

            engine = _projects_engine()
            with engine.begin() as conn:
                web_push_service.disable_device_subscription(
                    conn,
                    user_key=f"remote:{identity['sub']}",
                    device_id=device_id if isinstance(device_id, str) else None,
                    endpoint=endpoint if isinstance(endpoint, str) else None,
                )
        except Exception:
            logger.warning("remote logout could not disable browser Push", exc_info=True)
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
        import sys
        import time
        from config import paths as config_paths
        from vibe.memory_ui_access import process_ui_read_secret

        command = f"from vibe.ui_server import run_ui_server; run_ui_server('{bind_host}', {port})"
        memory_ui_secret = process_ui_read_secret()
        spawn_kwargs = (
            {"memory_ui_secret": memory_ui_secret} if memory_ui_secret is not None else {}
        )
        pid = runtime.spawn_background(
            [sys.executable, "-c", command],
            config_paths.get_runtime_ui_pid_path(),
            "ui_stdout.log",
            "ui_stderr.log",
            **spawn_kwargs,
        )
        runtime.write_status(
            status.get("state", "running"),
            status.get("detail"),
            status.get("service_pid"),
            pid,
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
    from storage.settings_service import ScopeAgentUnavailableError, StaleScopeAgentBindingError

    payload = request.json or {}
    try:
        return jsonify(api.save_settings(payload))
    except StaleScopeAgentBindingError as exc:
        return _settings_conflict_response(exc)
    except ScopeAgentUnavailableError as exc:
        return _scope_agent_unavailable_response(exc)


@app.post("/api/settings/thread", include_in_schema=False)
async def thread_settings_post(starlette_request: FastAPIRequest):
    async def handler():
        from vibe import api
        from storage.settings_service import ScopeAgentUnavailableError, StaleScopeAgentBindingError

        body = await starlette_request.body()
        payload = await starlette_request.json() if body else {}
        try:
            return api.save_thread_settings(payload if isinstance(payload, dict) else {})
        except StaleScopeAgentBindingError as exc:
            return _settings_conflict_response(exc)
        except ScopeAgentUnavailableError as exc:
            return _scope_agent_unavailable_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.delete("/api/settings/thread", include_in_schema=False)
async def thread_settings_delete(starlette_request: FastAPIRequest):
    async def handler():
        from vibe import api

        query = starlette_request.query_params
        return api.delete_thread_settings(
            query.get("platform", ""),
            query.get("channel_id", ""),
            query.get("thread_id", ""),
        )

    return await _dispatch_native_ui_request(starlette_request, handler)


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

    from config.v2_config import config_file_lock
    from vibe import api as vibe_api

    new_bot_token = token.strip()
    new_base_url = None
    if isinstance(result.get("base_url"), str) and result["base_url"].strip():
        new_base_url = result["base_url"].strip()

    # Patch-write shape (#1458 stage ③): the whole compute-and-save runs
    # under the config transaction — the wechat fields and the
    # enabled-list mutation are derived from the lock-fresh snapshot, so
    # a concurrent wechat/platform save between an earlier read and this
    # write can no longer be overwritten by stale section values.
    with config_file_lock():
        try:
            base = vibe_api.load_config()
        except FileNotFoundError:
            # Fresh install: seed the same default the settings loader
            # uses, exactly like the previous default_factory path.
            from core.services import settings as settings_service

            base = settings_service.default_config()
        wechat = {"bot_token": new_bot_token}
        if new_base_url:
            wechat["base_url"] = new_base_url
        elif not base.wechat.base_url:
            wechat["base_url"] = "https://ilinkai.weixin.qq.com"

        vibe_api.save_config(
            {
                "wechat": wechat,
                "__avibe_list_ops": {"platforms.enabled": {"add": ["wechat"]}},
            }
        )


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
            # The persistence helper takes the cross-process config
            # lock and does synchronous file/DB work — keep it off the
            # ASGI event loop so other UI requests don't stall while it
            # waits on the lock.
            await asyncio.to_thread(_persist_wechat_qr_credentials, result)
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

    payload = request.json or {}
    deep = _parse_explicit_bool(payload.get("deep")) or _parse_explicit_bool(request.args.get("deep"))
    result = _doctor(deep=deep)
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


_ALLOWED_DEPENDENCIES = {
    "askill",
    "avault",
    "model-hub-engine",
    "show-runtime",
    "memory-package",
    "memory-runtime",
    "tmux",
}


@app.route("/api/dependencies")
def get_dependencies():
    """Status of local tool, package, and managed-runtime dependencies."""
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
    """Persist Claude auth and refresh cached Claude SDK sessions.

    Body: ``{auth_mode: 'oauth'|'api_key', api_key?: string,
    credential_type?: 'api_key'|'auth_token', base_url?: string}``.
    Secrets live in Claude Code's own settings; V2Config records the selected
    mode, and the controller rolls its Claude runtime state after the write.
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
    """Send an isolated turn through the backend's production Agent transport."""
    from vibe import internal_client

    payload = request.json or {}
    raw_model = payload.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    try:
        result = await internal_client.test_backend_auth(backend, model=model)
    except (internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout) as exc:
        return jsonify({"ok": False, "error": "spawn_failed", "detail": str(exc)})
    return jsonify(result.get("body") or {"ok": False, "error": "test_failed"})


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


def _accessible_project_scope_ids_for_context(conn, context) -> list[str] | None:
    """Return a principal's readable Project scopes; owners need no SQL filter."""
    from storage import project_access_service

    if context is None or _has_runtime_owner_access(context):
        return None
    return sorted(
        project_access_service.project_scope_id(project_id)
        for project_id in project_access_service.accessible_project_ids(conn, context)
    )


def _request_accessible_project_scope_ids(conn) -> list[str] | None:
    return _accessible_project_scope_ids_for_context(
        conn,
        getattr(g, "authorization_context", None),
    )


@app.route("/api/projects", methods=["GET"])
def projects_list():
    from storage import projects_service

    include_archived = request.args.get("include_archived") in {"1", "true", "yes"}
    engine = _projects_engine()
    with engine.connect() as conn:
        return jsonify(
            {
                "projects": projects_service.list_projects(
                    conn,
                    include_archived=include_archived,
                    authorization_context=getattr(g, "authorization_context", None),
                )
            }
        )


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
            project = projects_service.create_project(
                conn,
                folder_path,
                display_name=display_name,
                authorization_context=getattr(g, "authorization_context", None),
            )
    except (FileNotFoundError, NotADirectoryError) as err:
        return jsonify({"error": str(err)}), 400
    except LookupError as err:
        # The folder is already held by a Project this caller cannot see, so
        # create-or-reuse answers exactly as the id-keyed routes do rather than
        # reusing, reviving, or duplicating it.
        return jsonify({"error": str(err)}), 404
    return jsonify(project), 201


@app.route("/api/projects/<project_id>", methods=["GET"])
def projects_get(project_id: str):
    from storage import projects_service

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            return jsonify(
                projects_service.get_project(
                    conn,
                    project_id,
                    authorization_context=getattr(g, "authorization_context", None),
                )
            )
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
        for field in (
            "agent_id",
            "expected_agent_id",
            "agent_name",
            "agent_variant",
            "model",
            "reasoning_effort",
        )
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
                authorization_context=getattr(g, "authorization_context", None),
                **agent_kwargs,
            )
    except projects_service.StaleProjectAgentBindingError as err:
        return _project_agent_conflict_response(err)
    except projects_service.ProjectAgentUnavailableError as err:
        return _project_agent_unavailable_response(err)
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except (FileNotFoundError, NotADirectoryError, ValueError) as err:
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
    from vibe.sse_broker import broker

    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            project = projects_service.archive_project(
                conn,
                project_id,
                authorization_context=getattr(g, "authorization_context", None),
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    broker.publish("authorization.changed", {"project_ids": [project_id]})
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

    Project-scoped skill routes are remote-readable, so the project lookup must
    carry the current request's authorization context; a resource ACL on the
    skill is an additional gate, not a substitute for Project access.
    """
    if not project_id:
        return None
    authorization_context = getattr(g, "authorization_context", None)
    from storage import projects_service

    engine = _projects_engine()
    with engine.connect() as conn:
        folder = projects_service.get_project_workdir(
            conn,
            project_id,
            authorization_context=authorization_context,
        )
    folder = str(folder or "").strip()
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


def _skills_project_id_kwargs(project_dir: str | None, project_id: str | None) -> dict[str, str]:
    """Thread stable project ids only for real project-scoped skill requests."""

    if project_dir is None or project_id is None:
        return {}
    return {"project_id": project_id}


def _skills_user_context_kwargs(context: Any) -> dict[str, Any]:
    """Pass the parsed request authorization context to Skill API calls.

    Local service callers historically invoke the Skill API with its compact
    legacy signature. HTTP requests need the real context for ACL enforcement;
    local HTTP requests already carry an ordinary Owner context.
    """

    if context is not None:
        return {"user_context": context}
    return {}


def _require_project_editor_for_skill_mutation(
    project_id: str | None,
    *,
    scope: str = "project",
):
    """Reject project Skill mutations below the effective Editor role."""

    if not project_id or scope != "project":
        return None
    from core.services import skills as skills_service

    try:
        skills_service.require_project_editor_access(
            getattr(g, "authorization_context", None),
            project_id,
        )
    except skills_service.SkillsError as exc:
        return _coded_error_response(exc.code, exc.message, 403)
    return None


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

    user_context = getattr(g, "authorization_context", None)
    scope = request.args.get("scope") or "all"
    backends = [b for b in (request.args.get("backends") or "").split(",") if b]
    project_id = request.args.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        # Folderless project: no project-scoped skills are possible — show
        # global skills (with a flag) instead of erroring the whole page.
        result = await api.list_skills(
            scope="global",
            backends=backends or None,
            **_skills_user_context_kwargs(user_context),
            **_skills_project_id_kwargs(None, project_id),
        )
        if isinstance(result, dict) and result.get("ok"):
            result = {**result, "project_no_folder": True}
        return jsonify(result)
    return jsonify(
        await api.list_skills(
            scope=scope,
            project_dir=project_dir,
            backends=backends or None,
            **_skills_user_context_kwargs(user_context),
            **_skills_project_id_kwargs(project_dir, project_id),
        )
    )


@app.route("/api/skills/preview", methods=["POST"])
async def skills_preview():
    from vibe import api

    user_context = getattr(g, "authorization_context", None)
    payload = request.json or {}
    project_id = payload.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        project_dir = None  # preview doesn't need the project folder (gh/zip sources)
    return jsonify(
        await api.preview_skill_source(
            str(payload.get("source") or ""),
            project_dir=project_dir,
            **_skills_user_context_kwargs(user_context),
            **_skills_project_id_kwargs(project_dir, project_id),
        )
    )


@app.route("/api/skills", methods=["POST"])
async def skills_add():
    from vibe import api

    user_context = getattr(g, "authorization_context", None)
    payload = request.json or {}
    project_id = payload.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    denied = _require_project_editor_for_skill_mutation(
        project_id,
        scope=str(payload.get("scope") or "project"),
    )
    if denied is not None:
        return denied
    return jsonify(
        await api.add_skill(
            str(payload.get("source") or ""),
            scope=payload.get("scope") or "project",
            project_dir=project_dir,
            project_id=project_id,
            backends=payload.get("backends") or None,
            all_skills=bool(payload.get("all")),
            skill=payload.get("skill") or None,
            copy=bool(payload.get("copy")),
            **_skills_user_context_kwargs(user_context),
        )
    )


@app.route("/api/skills/<name>", methods=["DELETE"])
async def skills_remove(name):
    from vibe import api

    user_context = getattr(g, "authorization_context", None)
    backends = [b for b in (request.args.get("backends") or "").split(",") if b]
    project_id = request.args.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    denied = _require_project_editor_for_skill_mutation(
        project_id,
        scope=request.args.get("scope") or "project",
    )
    if denied is not None:
        return denied
    return jsonify(
        await api.remove_skill(
            name,
            scope=request.args.get("scope") or "project",
            project_dir=project_dir,
            project_id=project_id,
            backends=backends or None,
            **_skills_user_context_kwargs(user_context),
        )
    )


@app.route("/api/skills/find", methods=["GET"])
async def skills_find():
    from vibe import api

    return jsonify(
        await api.find_skills(
            request.args.get("q") or "",
            **_skills_user_context_kwargs(getattr(g, "authorization_context", None)),
        )
    )


@app.route("/api/skills/check", methods=["GET"])
async def skills_check():
    from vibe import api

    user_context = getattr(g, "authorization_context", None)
    scope = request.args.get("scope") or "project"
    project_id = request.args.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        # Folderless project has no project-local skills, so nothing to check.
        return jsonify({"ok": True, "skills": []})
    return jsonify(
        await api.check_skills(
            scope=scope,
            project_dir=project_dir,
            project_id=project_id,
            **_skills_user_context_kwargs(user_context),
        )
    )


@app.route("/api/skills/update", methods=["POST"])
async def skills_update():
    from vibe import api

    user_context = getattr(g, "authorization_context", None)
    payload = request.json or {}
    project_id = payload.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        return _project_no_folder_error()
    denied = _require_project_editor_for_skill_mutation(
        project_id,
        scope=str(payload.get("scope") or "project"),
    )
    if denied is not None:
        return denied
    return jsonify(
        await api.update_skill(
            str(payload.get("name") or ""),
            scope=payload.get("scope") or "project",
            project_dir=project_dir,
            project_id=project_id,
            **_skills_user_context_kwargs(user_context),
        )
    )


@app.route("/api/skills/upload", methods=["POST"])
async def skills_upload():
    from vibe import api

    user_context = getattr(g, "authorization_context", None)
    payload = request.json or {}
    project_id = payload.get("project_id")
    try:
        project_dir = _resolve_project_dir(project_id)
    except LookupError as err:
        return _project_not_found(err)
    except _ProjectNoFolder:
        # The zip is unpacked to a temp dir (project-independent); the install
        # step picks the scope. Drop the cwd like preview rather than erroring.
        project_dir = None
    denied = _require_project_editor_for_skill_mutation(project_id)
    if denied is not None:
        return denied
    return jsonify(
        await api.upload_skill_zip(
            payload,
            project_dir=project_dir,
            **_skills_user_context_kwargs(user_context),
            **_skills_project_id_kwargs(project_dir, project_id),
        )
    )


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
            authorization_context=getattr(g, "authorization_context", None),
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
        authorization_context = getattr(g, "authorization_context", None)
        projects = projects_service.list_projects(
            conn,
            include_archived=include_archived,
            authorization_context=authorization_context,
        )
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
                authorization_context=authorization_context,
            )
    return jsonify({"projects": projects, "sessions": sessions})


@app.route("/api/sessions", methods=["POST"])
def sessions_create():
    from core.services import sessions as workbench_sessions_service
    from vibe.sse_broker import broker

    payload = request.json or {}
    project_id = (payload.get("project_id") or "").strip()
    agent_backend = (payload.get("agent_backend") or "").strip()
    agent_id = payload.get("agent_id")
    agent_name = payload.get("agent_name")
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
            from storage.agent_session_rows import reserve_write_lock

            reserve_write_lock(conn)
            if agent_id or agent_name:
                identity = workbench_sessions_service.require_enabled_agent_identity(
                    conn,
                    agent_id=agent_id,
                    agent_name=agent_name,
                )
                agent_id = identity["id"]
                agent_name = identity["name"]
                agent_backend = identity["backend"]
            session = workbench_sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend=agent_backend,
                agent_id=agent_id,
                agent_name=agent_name,
                agent_variant=payload.get("agent_variant"),
                model=payload.get("model"),
                reasoning_effort=payload.get("reasoning_effort"),
                title=payload.get("title"),
                metadata=metadata,
                authorization_context=getattr(g, "authorization_context", None),
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except workbench_sessions_service.ProjectAccessDeniedError as err:
        code = err.code
        message = t("error.projectAccessDenied", _request_ui_language())
        return _coded_error_response(code, message, 403)
    except PermissionError as err:
        return jsonify({"error": str(err)}), 403
    broker.publish("session.activity", {"session_id": session["id"], "scope_id": session["scope_id"], "event": "created"})
    return jsonify(session), 201


def _session_fork_error_response(err: Exception):
    """Map a ``SessionForkError`` to its machine code, in the STRUCTURED body.

    ``session_archived`` here is the same terminal fact the PATCH route answers, so it
    must reach the Web UI's shared archived-session convergence the same way: the flat
    shape this used to emit fed the human sentence to callers as the code, which left
    ``archivedConflictSessionId`` blind and every mutating control live after a
    permanent refusal. See ``_coded_error_response`` — every code here needs the same
    treatment, so the whole mapping goes through it rather than one patched branch.
    """
    from core.services import settings as settings_service
    from core.services.session_fork import (
        SESSION_AGENT_UNAVAILABLE_CODE,
        SESSION_AGENT_UNAVAILABLE_I18N_KEY,
    )

    message = str(err)
    if getattr(err, "code", None) == SESSION_AGENT_UNAVAILABLE_CODE:
        lang = settings_service.load_config_or_default().language
        key = SESSION_AGENT_UNAVAILABLE_I18N_KEY
        return _coded_error_response(
            SESSION_AGENT_UNAVAILABLE_CODE,
            t(f"{key}.message", lang),
            409,
            hint=t(f"{key}.hint", lang),
            **getattr(err, "details", {}),
        )
    if "id not found" in message:
        return _coded_error_response("session_not_found", message, 404)
    if "is archived" in message:
        return _coded_error_response("session_archived", message, 409)
    if "no native session id" in message:
        return _coded_error_response("session_not_bound", message, 409)
    if "backend cannot be forked" in message:
        return _coded_error_response("session_backend_unsupported", message, 409)
    if "backend does not match" in message:
        return _coded_error_response("session_backend_mismatch", message, 409)
    return _coded_error_response("session_fork_failed", message, 400)


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
    authorization_context,
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
        authorization_context=authorization_context,
    )
    engine = _projects_engine()
    with engine.connect() as conn:
        return workbench_sessions_service.get_session(
            conn,
            result.session_id,
            authorization_context=authorization_context,
        )


@app.route("/api/sessions/<session_id>/fork", methods=["POST"])
async def sessions_fork(session_id: str):
    from core.services.session_fork import SessionForkError
    from vibe.sse_broker import broker

    try:
        authorization_context = getattr(g, "authorization_context", None)
        fork_turn_state = await _session_turn_state_for_fork(session_id)
        session = await asyncio.to_thread(
            _reserve_forked_session_for_ui,
            session_id,
            trim_latest_running_turn=bool(fork_turn_state.get("trim_latest_running_turn")),
            native_turn_started=bool(fork_turn_state.get("native_turn_started")),
            authorization_context=authorization_context,
        )
    except SessionForkError as err:
        return _session_fork_error_response(err)
    except LookupError as err:
        # Same coded shape as the branch above: this is the same route answering the
        # same ``session_not_found`` code from a different exception type.
        return _coded_error_response("session_not_found", str(err), 404)

    broker.publish("session.activity", {"session_id": session["id"], "scope_id": session["scope_id"], "event": "created"})
    return jsonify(session), 201


@app.route("/api/sessions/<session_id>", methods=["GET"])
def sessions_get(session_id: str):
    from core.services import sessions as workbench_sessions_service

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            return jsonify(
                workbench_sessions_service.get_session(
                    conn,
                    session_id,
                    authorization_context=getattr(g, "authorization_context", None),
                )
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404


def _session_runtime_projection(
    body: dict[str, Any] | None,
    *,
    pending_input_count: int | None = None,
    controller_available: bool | None = True,
    authorization_context=None,
) -> dict[str, Any]:
    """Normalize the controller's orthogonal Session runtime axes for the UI."""

    payload = body if isinstance(body, dict) else {}
    raw_foreground = str(payload.get("foreground") or "").strip()
    if raw_foreground not in {"idle", "running"}:
        if controller_available is None:
            foreground = "unknown"
        else:
            foreground = "running" if bool(payload.get("in_flight")) else "idle"
    else:
        foreground = raw_foreground

    raw_connection = str(payload.get("connection") or "").strip()
    if raw_connection not in {"connected", "reconnecting", "disconnected", "unknown"}:
        raw_connection = "disconnected" if controller_available is False else "unknown"

    if pending_input_count is None:
        try:
            pending_input_count = max(0, int(payload.get("pending_input_count") or 0))
        except (TypeError, ValueError):
            pending_input_count = 0
    try:
        pending_output_count = max(
            0,
            int(payload.get("pending_activity_output_count") or 0),
        )
    except (TypeError, ValueError):
        pending_output_count = 0
    raw_activities = payload.get("background_activities")
    activities = (
        [dict(item) for item in raw_activities if isinstance(item, dict)]
        if isinstance(raw_activities, list)
        else []
    )
    if authorization_context is not None and not authorization_context.has_role("editor"):
        activities = [
            item for item in activities if item.get("item_kind") == "backend_activity"
        ]
    projection: dict[str, Any] = {
        # Retained as a read-only compatibility alias for older clients.
        "in_flight": None if foreground == "unknown" else foreground == "running",
        "foreground": foreground,
        "native_turn_started": bool(payload.get("native_turn_started")),
        "pending_input_count": pending_input_count,
        "background_activities": activities,
        "pending_activity_output_count": pending_output_count,
        "connection": raw_connection,
    }
    backend = str(payload.get("backend") or "").strip()
    if backend:
        projection["backend"] = backend
    return projection


def _active_unmaterialized_input(conn, session_id: str) -> dict[str, Any] | None:
    """Project the active claimed Delivery as a temporary transcript row."""

    from storage import message_deliveries

    turn = message_deliveries.active_turn(conn, session_id)
    if turn is None:
        return None
    payload = message_deliveries.claimed_workbench_message_payload(
        conn,
        str(turn["id"]),
    )
    if payload is None:
        return None
    payload = message_deliveries.public_delivery_payload(payload)
    payload.update(
        projection="claimed_delivery",
        delivered_at=turn.get("started_at") or turn.get("created_at"),
        read_at=None,
    )
    return payload


def _append_active_input(
    messages_result: dict[str, Any],
    active_input: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep a claimed input visible until native acceptance materializes it."""

    if active_input is None:
        return messages_result
    current = list(messages_result.get("messages") or [])
    if any(str(row.get("id") or "") == str(active_input["id"]) for row in current):
        return messages_result
    current.append(active_input)
    current.sort(
        key=lambda row: (
            str(row.get("delivered_at") or row.get("created_at") or ""),
            str(row.get("id") or ""),
        )
    )
    return {**messages_result, "messages": current}


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
    from storage import messages_service, project_access_service
    from vibe import api as vibe_api
    from vibe import internal_client

    authorization_context = getattr(g, "authorization_context", None)
    engine = _projects_engine()
    with engine.connect() as conn:
        try:
            session = workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=authorization_context,
            )
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        effective_role = project_access_service.get_effective_session_role(
            conn,
            authorization_context,
            session_id,
        )
        can_chat = project_access_service.role_allows(effective_role, "editor")
        messages_result = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            authorization_context=authorization_context,
            tail=True,
        )
        messages_result = _append_active_input(
            messages_result,
            _active_unmaterialized_input(conn, session_id),
        )
        from storage import message_deliveries

        queued = (
            [
                message_deliveries.public_delivery_payload(item)
                for item in message_deliveries.list_queued(conn, session_id)
            ]
            if can_chat
            else []
        )
        can_access_draft = can_chat
        draft = (
            message_deliveries.get_draft_state(conn, session_id)
            if can_access_draft
            else None
        )

    agents_payload = {"agents": [], "default_agent_name": None}
    if can_chat:
        try:
            agents_payload = vibe_api.get_vibe_agents(
                include_disabled=False,
                include_archived=True,
            )
        except Exception:
            logger.exception("sessions_bootstrap: failed to load Vibe Agents")

    try:
        config = settings_service.load_config_or_default()
        config_payload = _config_payload_for_context(config, authorization_context)
    except Exception:
        logger.exception("sessions_bootstrap: failed to load config")
        config_payload = None

    visible_queued = queued
    visible_draft = draft

    try:
        turn_result = await internal_client.turn_state(session_id)
        turn_body = turn_result.get("body") or {}
        turn_state = _session_runtime_projection(
            turn_body,
            pending_input_count=len(visible_queued),
            authorization_context=authorization_context,
        )
    except internal_client.InternalServerUnavailable:
        turn_state = _session_runtime_projection(
            None,
            pending_input_count=len(visible_queued),
            controller_available=False,
            authorization_context=authorization_context,
        )
    except internal_client.InternalServerTimeout:
        turn_state = _session_runtime_projection(
            None,
            pending_input_count=len(visible_queued),
            controller_available=None,
            authorization_context=authorization_context,
        )

    return jsonify(
        {
            "session": session,
            "capabilities": {"can_chat": can_chat},
            "agents": agents_payload.get("agents") or [],
            "default_agent_name": agents_payload.get("default_agent_name"),
            "config": config_payload,
            "messages": messages_result["messages"],
            "next_after_id": messages_result.get("next_after_id"),
            "next_before_id": messages_result.get("next_before_id"),
            "queued": visible_queued,
            "draft": (
                _session_draft_payload(visible_draft)
                if can_access_draft
                else {"text": ""}
            ),
            "turn_state": turn_state,
        }
    )


def _session_archived_response():
    """Shared 409 payload for a write refused because the session is archived.

    The shared ``_coded_error_response`` shape: the code MUST survive the Web UI's
    parser or the read-only convergence never fires at all (see that builder for the
    mechanism this refusal depends on).

    The ``message`` comes from ``vibe/i18n`` (AGENTS.md §6) rather than an English
    literal: direct API/CLI consumers read it verbatim, and a Web UI client without
    the ``errors.session_archived`` key falls back to it too.
    """
    from core.services import sessions as workbench_sessions_service

    return _coded_error_response("session_archived", workbench_sessions_service.session_archived_message(), 409)


#: The archive/edit refusal's copy: the row exists to receive notices and cannot be
#: torn down or re-labelled. Used by the DELETE and PATCH guards.
RESERVED_SESSION_PROTECTED_I18N_KEY = "harness.notice.workspaceSessionProtected"
#: The send refusal's copy. A SEPARATE key on purpose: "cannot be archived or modified"
#: is not an answer to "why did my message not send", and the composer's own inert-state
#: notice has to say the same thing this body says.
RESERVED_SESSION_READ_ONLY_I18N_KEY = "harness.notice.workspaceSessionReadOnly"


def _reserved_session_response(i18n_key: str, *, code: str = "reserved_session"):
    """Shared 403 payload for a write refused because the RUNTIME reserves the session.

    Third instance of the same three lines (DELETE teardown, PATCH edit, and now the
    messages POST), so it is extracted rather than copied again. The refusal is 403 and
    not the archive's 409: this is not a lifecycle state the caller could wait out, it is
    a row the caller does not own.

    One ``code`` for all three (``reserved_session``) so a Web UI client resolves one
    ``errors.*`` entry, with the per-verb sentence chosen by ``i18n_key`` — the same split
    the archived refusal uses (global ``errors.session_archived`` plus per-verb
    ``chat.archived.*`` copy). ``storage`` raises the machine ``code`` and carries no
    user-facing text, because the configured language is only resolvable up here.
    """
    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    return _coded_error_response(code, t(i18n_key, lang), 403)


def _backend_locked_response(err):
    """Shared 409 payload for a rejected cross-backend session change.

    Coded shape for the same reason as the archived 409, and specifically BECAUSE it
    shares a route with it: ``sessions_update`` deliberately answers the terminal
    ``session_archived`` ahead of this *retryable* ``backend_locked`` so a client can
    tell a permanent refusal from one worth retrying — which it cannot do while either
    code is being replaced by its own error sentence. The lock's detail fields stay
    top-level, where their existing consumers read them.
    """
    return _coded_error_response(
        "backend_locked",
        str(err),
        409,
        current_backend=err.current_backend,
        requested_backend=err.requested_backend,
    )


def _publish_session_update_activity(
    broker,
    *,
    session_id: str,
    session: dict,
    previous_session: dict | None = None,
) -> None:
    """Publish one canonical title/placement reconciliation sequence."""
    broker.publish(
        "session.activity",
        {
            "session_id": session_id,
            "scope_id": session.get("scope_id"),
            "event": "updated",
            "title": session.get("title"),
            "visibility": session.get("visibility"),
            "pinned": bool(session.get("pinned")),
        },
    )
    if previous_session is None:
        return

    previous_scope_id = previous_session.get("scope_id")
    current_scope_id = session.get("scope_id")
    placement_changed = (
        previous_scope_id != current_scope_id
        or previous_session.get("visibility") != session.get("visibility")
    )
    if not placement_changed:
        return

    # The current sidebar listener patches title-only `updated` events, then
    # reconciles project windows for ordering activity. Reconcile the old scope
    # to remove the row, and treat a foreground row in its new scope as newly
    # visible so an empty loaded project also fetches it.
    if previous_scope_id and (
        previous_scope_id != current_scope_id or session.get("visibility") == "background"
    ):
        broker.publish(
            "session.activity",
            {
                "session_id": session_id,
                "scope_id": previous_scope_id,
                "event": "user_message",
                "reason": "session_placement_changed",
            },
        )
    if current_scope_id and session.get("visibility") == "foreground":
        broker.publish(
            "session.activity",
            {
                "session_id": session_id,
                "scope_id": current_scope_id,
                "event": "created",
                "reason": "session_placement_changed",
            },
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
            "visibility",
            "pinned",
            "scope_id",
        )
        if key in payload
    }
    if not updatable:
        return jsonify({"error": "no updatable fields supplied"}), 400

    engine = _projects_engine()
    # Archive is TERMINAL, so it outranks every transient conflict below — most
    # importantly the cross-backend lock preflight, which consults the controller
    # and would answer a retryable ``backend_locked`` for an archived row whose
    # in-flight turn is still unwinding: ``archive_session`` cannot cancel that turn
    # inside its transaction, so the DELETE route commits the archive first and
    # cancels best-effort afterwards (``_archive_cancel_turn``). A stale
    # cross-backend PATCH landing in that window used to have its terminal state
    # masked, leaving the client unable to recognize ``session_archived`` and
    # converge. Short-circuit here so the archive conflict always wins.
    #
    # Same shared write-guard the messages POST uses. A MISSING session reads
    # ``False``, so the 404s below are unchanged, and every non-archived PATCH pays
    # only one indexed read and keeps its existing preflight ordering.
    with engine.connect() as conn:
        if workbench_sessions_service.is_session_archived(conn, session_id):
            return _session_archived_response()
    should_check_backend_lock = "agent_backend" in updatable
    requested_backend = updatable.get("agent_backend")
    identity_requested = "agent_id" in updatable or "agent_name" in updatable
    if identity_requested and not (updatable.get("agent_id") or updatable.get("agent_name")):
        updatable["agent_id"] = None
        updatable["agent_name"] = None
    if identity_requested and (updatable.get("agent_id") or updatable.get("agent_name")):
        try:
            with engine.begin() as conn:
                from storage.agent_session_rows import reserve_write_lock

                reserve_write_lock(conn)
                identity = workbench_sessions_service.require_enabled_agent_identity(
                    conn,
                    agent_id=updatable.get("agent_id"),
                    agent_name=updatable.get("agent_name"),
                )
                requested_backend = identity["backend"]
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
            from storage.agent_session_rows import reserve_write_lock

            reserve_write_lock(conn)
            if identity_requested and (updatable.get("agent_id") or updatable.get("agent_name")):
                identity = workbench_sessions_service.require_enabled_agent_identity(
                    conn,
                    agent_id=updatable.get("agent_id"),
                    agent_name=updatable.get("agent_name"),
                )
                updatable["agent_id"] = identity["id"]
                updatable["agent_name"] = identity["name"]
                updatable["agent_backend"] = identity["backend"]
                updatable.setdefault("agent_variant", identity["backend"])
            previous_session = (
                workbench_sessions_service.get_session(conn, session_id)
                if {"visibility", "scope_id"}.intersection(updatable)
                else None
            )
            session = workbench_sessions_service.update_session(
                conn,
                session_id,
                authorization_context=getattr(g, "authorization_context", None),
                **updatable,
            )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except workbench_sessions_service.SessionArchivedError:
        # Archive is terminal — the read-only chat UI relies on this backstop. The
        # short-circuit above already answers the common case; this catches a
        # session archived BETWEEN that read and this write, and keeps the service
        # guard authoritative rather than trusting the route's preflight.
        return _session_archived_response()
    except workbench_sessions_service.ReservedSessionError as err:
        # The reserved workspace-notifications session refuses modification the
        # same way it refuses archive: a flipped visibility or title would
        # un-project the system surface until the next heal. Mirror the DELETE
        # route's 403 + localized copy so the two guards read as one contract.
        return _reserved_session_response(
            RESERVED_SESSION_PROTECTED_I18N_KEY,
            code=getattr(err, "code", "reserved_session"),
        )
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    except PermissionError as err:
        return jsonify({"error": str(err)}), 403
    except workbench_sessions_service.SessionBackendLockedError as err:
        # A session is pinned to its backend once it has a conversation (or a
        # running turn); the UI may switch the agent within the same backend,
        # but not across backends.
        return _backend_locked_response(err)
    # Broadcast so other surfaces (e.g. the sidebar session list) reflect the
    # edit live. The local CLI route below uses this same sequence after its
    # out-of-process DB write.
    _publish_session_update_activity(
        broker,
        session_id=session_id,
        session=session,
        previous_session=previous_session,
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

    payload = request.json or {}
    if payload.get("event") == "queue_updated":
        broker.publish("queue.updated", {"session_id": session_id})
        return jsonify({"ok": True})

    previous_session = None
    if "previous_scope_id" in payload and "previous_visibility" in payload:
        previous_session = {
            "scope_id": payload.get("previous_scope_id"),
            "visibility": payload.get("previous_visibility"),
        }

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            session = workbench_sessions_service.get_session(conn, session_id)
    except LookupError:
        return jsonify({"error": "not found"}), 404
    _publish_session_update_activity(
        broker,
        session_id=session_id,
        session=session,
        previous_session=previous_session,
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
            workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=getattr(g, "authorization_context", None),
            )
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


def _session_archive_unavailable_response():
    """Fail closed when the controller cannot own the archive lifecycle."""

    from core.services import settings as settings_service

    lang = settings_service.load_config_or_default().language
    return _coded_error_response(
        "session_archive_unavailable",
        t("error.sessionArchiveUnavailable", lang),
        503,
    )


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


async def _archive_publish_definition_updates(reclaimed: dict[str, Any]) -> None:
    definition_types = [
        definition_type
        for definition_type, key in (("scheduled", "tasks"), ("watch", "watches"))
        if reclaimed.get(key)
    ]
    if not definition_types:
        return
    from core.inbox_events import publish_definitions_updated

    await asyncio.gather(
        *(
            asyncio.to_thread(
                publish_definitions_updated,
                definition_type=definition_type,
            )
            for definition_type in definition_types
        )
    )


async def _archive_publish_run_updates(
    session_id: str,
    reclaimed: dict[str, Any],
) -> None:
    """Wake post-commit Run consumers for archive cancellation writes."""

    if not reclaimed.get("runs"):
        return
    from core.inbox_events import RUNS_UPDATED_EVENT
    from vibe import internal_client

    try:
        await internal_client.publish_event(
            RUNS_UPDATED_EVENT,
            {"session_id": session_id, "reason": "session_archived"},
            timeout=1.5,
        )
    except internal_client.InternalServerUnavailable:
        pass
    except Exception:
        logger.debug(
            "archive: run update publish failed for %s",
            session_id,
            exc_info=True,
        )


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
async def sessions_archive(session_id: str):
    """Permanently archive a session and reclaim its bound resources.

    For an active row, the controller owns the terminal session write. Memory
    a volatile Memory barrier is best-effort after that write and never blocks archive. If the
    controller seam itself is unavailable, archive fails closed.

    The DB-level teardown (status, tasks/watches, runs, Show Page) is atomic in
    ``archive_session``. Cancelling an in-flight chat turn lives in the controller
    process, so we fire it best-effort in the BACKGROUND after the commit — the
    session is already archived + guarded, so a turn that slips through just
    writes into hidden history rather than re-surfacing the session.
    """
    from core.services import sessions as workbench_sessions_service
    from core.show_pages import ShowPageError, require_show_page_management
    from sqlalchemy import select
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.models import show_pages
    from vibe.sse_broker import broker

    engine = _projects_engine()
    if str(session_id) == WORKSPACE_NOTICE_SESSION_ID:
        return _reserved_session_response(
            RESERVED_SESSION_PROTECTED_I18N_KEY,
            code="reserved_session",
        )
    try:
        with engine.connect() as conn:
            existing_session = workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=getattr(g, "authorization_context", None),
            )
            page_exists = conn.execute(
                select(show_pages.c.session_id).where(
                    show_pages.c.session_id == session_id
                )
            ).scalar_one_or_none()
            if page_exists is not None:
                require_show_page_management(
                    conn,
                    session_id,
                    user_context=getattr(g, "authorization_context", None),
                )
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    except ShowPageError as err:
        return _coded_error_response(err.code, str(err), 403)
    if existing_session.get("status") == "archived":
        try:
            with engine.begin() as conn:
                session = workbench_sessions_service.archive_session(
                    conn,
                    session_id,
                    authorization_context=getattr(g, "authorization_context", None),
                )
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        except PermissionError as err:
            code = getattr(err, "code", "forbidden")
            if code == "reserved_session":
                return _reserved_session_response(
                    RESERVED_SESSION_PROTECTED_I18N_KEY,
                    code=code,
                )
            return _coded_error_response(code, str(err), 403)
    else:
        from vibe import internal_client

        try:
            archive_result = await internal_client.memory_archive_session(session_id)
        except (
            internal_client.InternalServerUnavailable,
            internal_client.InternalServerTimeout,
        ):
            return _session_archive_unavailable_response()
        except Exception:
            logger.debug(
                "archive: controller lifecycle failed for %s",
                session_id,
                exc_info=True,
            )
            return _session_archive_unavailable_response()

        status_code = archive_result.get("status_code")
        body = archive_result.get("body")
        body = body if isinstance(body, dict) else {}
        if status_code == 404 and body.get("error") == "session_not_found":
            return jsonify({"error": f"Session not found: {session_id}"}), 404
        if status_code == 403 and body.get("error") == "reserved_session":
            return _reserved_session_response(
                RESERVED_SESSION_PROTECTED_I18N_KEY,
                code="reserved_session",
            )
        candidate = body.get("session")
        if (
            status_code != 200
            or body.get("ok") is not True
            or not isinstance(candidate, dict)
            or candidate.get("id") != session_id
            or candidate.get("status") != "archived"
        ):
            return _session_archive_unavailable_response()
        session = candidate

    revoked_vault_scopes = session.pop("revoked_vault_grant_scopes", [])
    reclaimed = session.get("reclaimed") or {}
    await asyncio.gather(
        _archive_publish_definition_updates(reclaimed),
        _archive_publish_run_updates(session_id, reclaimed),
    )

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
    # Legacy IM caller contexts may carry only the platform-native message id;
    # storage resolves it to the durable row before applying cursor pagination.
    around_native_id = request.args.get("around_native_id") or None
    around_native_platform = request.args.get("around_native_platform") or None
    around_turn_id = request.args.get("around_turn_id") or None
    around_run_id = request.args.get("around_run_id") or None
    # ``tail=1`` returns the most-recent window (for the Chat page's gap recovery)
    # instead of the oldest page.
    tail = request.args.get("tail") == "1"

    engine = _projects_engine()
    authorization_context = getattr(g, "authorization_context", None)
    with engine.connect() as conn:
        try:
            workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=authorization_context,
            )
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        # Chat transcript = the dialogue + turn-terminal markers. avibe turns
        # persist intermediate assistant / tool_call rows (unified store) that we
        # keep OUT of the conversation view, but ``notify`` rows are kept: a
        # terminal notify (e.g. an agent run that failed and stopped without a
        # result) marks the end of that turn and must stay visible.
        result = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            after_id=after_id,
            before_id=before_id,
            around_id=around_id,
            around_native_id=around_native_id,
            around_native_platform=around_native_platform,
            around_turn_id=around_turn_id,
            around_run_id=around_run_id,
            limit=limit,
            types=messages_service.TRANSCRIPT_TYPES,
            authorization_context=authorization_context,
            tail=tail,
        )
        if tail and not any(
            (after_id, before_id, around_id, around_native_id, around_turn_id, around_run_id)
        ):
            result = _append_active_input(
                result,
                _active_unmaterialized_input(conn, session_id),
            )
    return jsonify(result)


@app.route("/api/sessions/<session_id>/activity", methods=["GET"])
def sessions_activity(session_id: str):
    """Turn-grouped agent activity for the Chat Activity panel.

    Two modes on one route (mirrors the ``chat_message_font_size`` /
    ``show_agent_activity`` display feature; the panel is a display-only view of
    already-persisted rows — no gating here, the gate is the UI toggle):

    * no query params → SUMMARY: ``{"groups": [{id, anchor_message_id, status,
      steps, started_at, ended_at, duration_ms}, ...]}`` — one entry per turn that
      produced ≥1 activity row, WITHOUT the row text. Loaded once when the Chat
      opens so past turns can show their collapsed chip.
    * ``?group_id=<id>`` → DETAIL: that one group plus ``"rows": [{id, kind,
      text, created_at}, ...]`` (``kind`` is ``assistant`` | ``tool_call``) — the
      lazy expand. 404 when the group id is unknown.

    ``status`` ∈ ``done`` | ``failed`` | ``interrupted``. See
    ``storage/agent_activity_service.py`` for the grouping contract and the
    bounded-scan window.
    """
    from core.services import sessions as workbench_sessions_service
    from storage import agent_activity_service

    group_id = request.args.get("group_id") or None
    engine = _projects_engine()
    authorization_context = getattr(g, "authorization_context", None)
    with engine.connect() as conn:
        try:
            workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=authorization_context,
            )
        except LookupError as err:
            return jsonify({"error": str(err)}), 404
        if group_id:
            group = agent_activity_service.get_turn_group(
                conn, session_id=session_id, group_id=group_id
            )
            if group is None:
                return jsonify({"error": "activity group not found"}), 404
            return jsonify(group)
        result = agent_activity_service.list_turn_groups(conn, session_id=session_id)
    return jsonify(result)


@app.route("/api/search/messages", methods=["GET"])
def search_messages_list():
    """Global message-content search across Workbench sessions, grouped by session.

    Substring (case-insensitive) search over ``content_text`` for ``platform
    ='avibe'`` user prompts + agent ``result`` replies. Archived sessions are
    excluded by default; ``include_archived=1`` opts them in, and each returned
    session group carries ``archived`` so the client can mark and open them
    read-only. Messages under an archived PROJECT stay excluded either way.
    ``q`` is the query, ``limit`` caps the matched-message scan. The
    remote-access host guard + auth run in the global ``before_request`` hooks
    (same as the messages list), so this handler just delegates to the service.
    """
    from storage import messages_service

    query = request.args.get("q") or ""
    include_archived = request.args.get("include_archived") in {"1", "true", "yes"}
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50

    engine = _projects_engine()
    with engine.connect() as conn:
        result = messages_service.search_messages(
            conn,
            query=query,
            limit=limit,
            include_archived=include_archived,
            scope_ids=_request_accessible_project_scope_ids(conn),
        )
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


_FILE_UPLOAD_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_FILE_UPLOAD_FIELD_MAX_BYTES = 4096


def _validate_file_upload_content_length(headers: Any, max_file_bytes: int) -> None:
    from core.file_browser_service import FileBrowserError

    raw_length = headers.get("content-length")
    if raw_length is None:
        return
    try:
        length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise FileBrowserError("fs_error", "Invalid Content-Length", 400) from exc
    if length < 0:
        raise FileBrowserError("fs_error", "Invalid Content-Length", 400)
    if length > max_file_bytes + _FILE_UPLOAD_MULTIPART_OVERHEAD_BYTES:
        raise FileBrowserError("too_large", "File is too large", 413)


class _FileUploadMultiPartParser(MultiPartParser):
    def __init__(
        self,
        *args: Any,
        max_file_bytes: int,
        max_field_bytes: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_file_bytes = max_file_bytes
        self._max_field_bytes = max_field_bytes
        self._current_file_bytes = 0

    def on_part_begin(self) -> None:
        self._current_file_bytes = 0
        super().on_part_begin()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk_len = end - start
        if self._current_part.file is None:
            if len(self._current_part.data) + chunk_len > self._max_field_bytes:
                raise MultiPartException("Form field is too large.")
        else:
            self._current_file_bytes += chunk_len
            if self._current_file_bytes > self._max_file_bytes:
                raise MultiPartException("File is too large.")
        super().on_part_data(data, start, end)


async def _parse_file_upload_form(starlette_request: FastAPIRequest, *, max_file_bytes: int):
    content_type = starlette_request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        from core.file_browser_service import FileBrowserError

        raise FileBrowserError("invalid_name", "File is required", 400)
    parser_kwargs: dict[str, Any] = {"max_files": 1, "max_fields": 3}
    if "max_part_size" in inspect.signature(MultiPartParser).parameters:
        parser_kwargs["max_part_size"] = _FILE_UPLOAD_FIELD_MAX_BYTES
    parser = _FileUploadMultiPartParser(
        starlette_request.headers,
        starlette_request.stream(),
        max_file_bytes=max_file_bytes,
        max_field_bytes=_FILE_UPLOAD_FIELD_MAX_BYTES,
        **parser_kwargs,
    )
    return await parser.parse()


async def _dispatch_native_ui_request(starlette_request: FastAPIRequest, handler: Callable[[], Any]):
    return await app.dispatch_native_request(starlette_request, handler)


# The Memory routes live in their own module; registered here so their position
# in the app's route table is unchanged.
register_memory_routes(app)


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


@app.post("/api/files/upload", include_in_schema=False)
async def files_upload(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        try:
            _validate_file_upload_content_length(starlette_request.headers, file_browser_service.MAX_FILE_BYTES)
            form = await _parse_file_upload_form(starlette_request, max_file_bytes=file_browser_service.MAX_FILE_BYTES)
            try:
                upload = form.get("file")
                if not isinstance(upload, StarletteUploadFile):
                    raise file_browser_service.FileBrowserError("invalid_name", "File is required", 400)
                name_value = form.get("name")
                dir_value = form.get("dir")
                await upload.seek(0)
                return jsonify(
                    await asyncio.to_thread(
                        file_browser_service.upload_file,
                        str(dir_value or ""),
                        upload.file,
                        filename=upload.filename,
                        name=name_value if isinstance(name_value, str) else None,
                        overwrite=_parse_explicit_bool(form.get("overwrite")),
                    )
                )
            finally:
                await form.close()
        except MultiPartException as exc:
            message = str(exc)
            if "too large" in message.lower():
                return _file_browser_error_response(
                    file_browser_service.FileBrowserError("too_large", "File is too large", 413)
                )
            return _file_browser_error_response(file_browser_service.FileBrowserError("fs_error", message, 400))
        except StarletteHTTPException as exc:
            return _file_browser_error_response(
                file_browser_service.FileBrowserError("fs_error", str(exc.detail), 400)
            )
        except Exception as exc:
            return _file_browser_error_response(exc)

    return await _dispatch_native_ui_request(starlette_request, handler)


def _show_page_icon_upload_error(code: str, message: str):
    # Structured 4xx for the icon upload (§7.1j) — mirrors _dock_error_response so the
    # Web UI's shared handler localizes via errors.<code> and falls back to `message`.
    # Every failure is a client/policy error (bad type/size, missing file, unknown
    # page), so the endpoint answers a clear 4xx — never a 500.
    status = {
        "show_page_not_found": 404,
        "session_not_found": 404,
        "resource_access_forbidden": 403,
        "icon_too_large": 413,
        "invalid_icon_type": 415,
    }.get(code, 400)
    return _coded_error_response(code, message, status)


@app.post("/api/show-pages/{session_id}/icon", include_in_schema=False)
async def show_page_icon_upload(session_id: str, starlette_request: FastAPIRequest):
    # Icon self-serve upload (§7.1j): the multipart sibling of the GET icon endpoint
    # (that one is a compat @app.route; the compat layer parses JSON, not multipart, so
    # the upload is a native FastAPI route reusing the shared file-upload machinery —
    # parser cap + Content-Length guard). Auth/CSRF ride the native dispatch hooks.
    # Contract: a structured 4xx on ANY bad input, never a 500 (the icon is decorative).
    async def handler():
        from core.file_browser_service import FileBrowserError
        from core.show_pages import ShowPageError
        from vibe import api
        from vibe.sse_broker import broker

        try:
            _validate_file_upload_content_length(starlette_request.headers, SHOW_PAGE_ICON_MAX_UPLOAD_BYTES)
            form = await _parse_file_upload_form(
                starlette_request, max_file_bytes=SHOW_PAGE_ICON_MAX_UPLOAD_BYTES
            )
            try:
                upload = form.get("file")
                if not isinstance(upload, StarletteUploadFile):
                    raise ShowPageError("An icon file is required.", code="icon_required")
                await upload.seek(0)
                data = await upload.read()
                result = await asyncio.to_thread(
                    api.upload_show_page_icon,
                    session_id,
                    data,
                    filename=upload.filename,
                    content_type=upload.content_type,
                    user_context=_request_authorization_context(),
                )
                result = _show_page_response_for_request(
                    result,
                    _request_authorization_context(),
                )
                # Broadcast so EVERY already-mounted inventory (Dock, WindowLayer, mobile
                # drawer, app search) reloads and picks up the new icon_version — the
                # optimistic mergePage only updates the Library instance that uploaded
                # (§7.1j review P2). Reuses the existing "show page changed → reload"
                # signal that those surfaces already listen for.
                broker.publish(
                    "session.activity",
                    {"session_id": session_id, "scope_id": None, "event": "show_event"},
                )
                return jsonify(result)
            finally:
                await form.close()
        except MultiPartException as exc:
            message = str(exc)
            code = "icon_too_large" if "too large" in message.lower() else "invalid_icon"
            return _show_page_icon_upload_error(code, message)
        except StarletteHTTPException as exc:
            return _show_page_icon_upload_error("invalid_icon", str(exc.detail))
        except FileBrowserError as exc:
            # _parse_file_upload_form raises this for a non-multipart body; the
            # Content-Length guard raises it with code "too_large" (413) BEFORE parsing a
            # very large body — that must keep the documented icon_too_large/413 path
            # instead of collapsing to a generic 400 (§7.1j review P3).
            code = "icon_too_large" if exc.code == "too_large" else "invalid_icon"
            return _show_page_icon_upload_error(code, exc.message)
        except ShowPageError as exc:
            return _show_page_icon_upload_error(getattr(exc, "code", "invalid_icon"), str(exc))
        except Exception:
            # A genuine server-side write fault (disk full, permission). The icon is
            # decorative, so answer a clear 4xx (never 500 per §7.1j) and log the cause.
            logger.exception("show page icon upload failed")
            return _show_page_icon_upload_error("icon_write_failed", "Could not save the icon; please try again.")

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


@app.post("/api/files/copy", include_in_schema=False)
async def files_copy(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.copy_path,
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


@app.post("/api/files/delete/undo", include_in_schema=False)
async def files_delete_undo(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        payload = request.json or {}
        try:
            return jsonify(await asyncio.to_thread(file_browser_service.undo_delete_path, payload.get("token") or ""))
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


@app.get("/api/files/search_names", include_in_schema=False)
async def files_search_names(starlette_request: FastAPIRequest):
    async def handler():
        from core import file_browser_service

        args = request.args
        try:
            return jsonify(
                await asyncio.to_thread(
                    file_browser_service.search_names,
                    args.get("root") or "",
                    args.get("query") or "",
                    show_hidden=args.get("show_hidden") == "1",
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

    Only files minted into ``media_objects`` are reachable. Tokens stay stable
    within their referencing session, and the row's Project/session scope is
    authorized on every request. Lives under ``/api/*`` so the remote-access
    auth middleware already gates it, and a same-origin ``<img>`` / anchor GET
    carries the session cookie. Defaults to ``inline`` (so images render in
    ``<img>`` and PDFs preview); ``?download=1`` forces an attachment download.
    """
    return _registered_media_response(token)


def _media_row_show_page_access_allowed(context: Any, row: dict[str, Any]) -> bool:
    """Whether *context* may still read a Show annotation's screenshot bytes.

    A `show_annotation` screenshot is part of the page it was drawn on, so it
    inherits the page's ``/show`` admission (the §3.2 Instance Viewer gate)
    rather than only the Project/session role the rest of the media proxy
    checks. Without this the media token outlives the access that produced it:
    a caller who saw the Workbench once keeps its screenshot readable after
    their Instance role is revoked, and an email-grant ``/p`` visitor never
    reaches the annotation bytes at all.

    Media from any other source is unaffected and keeps the Project/session
    authorization below as its only gate.
    """

    if (row.get("source") or "") != "show_annotation":
        return True
    session_id = row.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        # Fail closed: an annotation screenshot with no page to check against
        # cannot be authorized, and serving it would be the exact bypass above.
        return False
    return _show_page_resource_access_allowed(context, session_id)


def _request_can_read_media_row(conn, token: str, row: dict[str, Any]) -> bool:
    from storage import media_service, project_access_service

    context = getattr(g, "authorization_context", None)
    if not _media_row_show_page_access_allowed(context, row):
        return False
    if (row.get("source") or "") == "show_annotation":
        # §3.2: annotation media inherits the /show Instance Viewer gate alone
        # (already applied by _media_row_show_page_access_allowed). The
        # Project/session role below must not stack on top, or an admitted
        # Instance Viewer who is outside the page's Project loses the screenshot.
        return True
    if context is None or _has_runtime_owner_access(context):
        return True
    session_ids = media_service.referenced_session_ids(conn, token)
    if session_ids:
        return any(
            project_access_service.role_allows(
                project_access_service.get_effective_session_role(conn, context, session_id),
                "viewer",
            )
            for session_id in session_ids
        )
    session_id = row.get("session_id")
    project_id = project_access_service.project_id_from_scope_id(row.get("scope_id"))
    role = (
        project_access_service.get_effective_session_role(conn, context, session_id)
        if session_id
        else project_access_service.get_effective_project_role(conn, context, project_id)
        if project_id
        else None
    )
    return project_access_service.role_allows(role, "viewer")


def _registered_media_response(
    token: str,
    *,
    expected_session_id: str | None = None,
    expected_source: str | None = None,
    public_show_page: bool = False,
):
    from urllib.parse import quote

    from storage import media_service

    engine = _projects_engine()
    with engine.connect() as conn:
        row = media_service.get_by_token(conn, token)
        matches_expected_session = (
            expected_session_id is None
            or bool(row)
            and (
                row.get("session_id") == expected_session_id
                or media_service.is_referenced_by_session(conn, token, expected_session_id)
            )
        )
        matches_expected_source = (
            expected_source is None
            or bool(row)
            and row.get("source") == expected_source
        )
        public_show_page_validated = bool(
            public_show_page
            and expected_session_id
            and expected_source == "show_annotation"
            and matches_expected_session
            and matches_expected_source
        )
        if row and not public_show_page_validated and not _request_can_read_media_row(
            conn,
            token,
            row,
        ):
            row = None
    if not row or row.get("revoked_at"):
        return jsonify({"error": "not_found"}), 404
    if not matches_expected_session:
        return jsonify({"error": "not_found"}), 404
    if not matches_expected_source:
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
    # Authorization is checked on every request and can change independently of
    # the opaque token. Browser-profile account switching must not reuse bytes
    # cached under a previous remote identity.
    response.headers["Cache-Control"] = "private, no-store"
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
        if row and not _request_can_read_media_row(conn, token, row):
            row = None
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


_WORKBENCH_ATTACHMENT_ERROR_CODES = {
    "session_not_found",
    "file_required",
    "empty_file",
    "too_large",
    "invalid_upload",
    "upload_failed",
}


async def _workbench_attachment_error(code: str, status: int):
    from core.workbench_media import MAX_WORKBENCH_ATTACHMENT_BYTES
    from core.services import settings as settings_service

    message_code = code if code in _WORKBENCH_ATTACHMENT_ERROR_CODES else "invalid_upload"
    config = await asyncio.to_thread(settings_service.load_config_or_default)
    extra: dict[str, Any] = {}
    if code == "too_large":
        extra["max_file_bytes"] = MAX_WORKBENCH_ATTACHMENT_BYTES
    return _coded_error_response(
        code,
        t(f"error.workbenchAttachment.{message_code}", config.language),
        status,
        **extra,
    )


def _workbench_attachment_response(result: Any):
    return (
        jsonify(
            {
                "token": result.token,
                "name": result.name,
                "mime": result.mime,
                "size": result.size,
                "kind": result.kind,
                "url": f"/api/media/{result.token}",
                "width": result.width,
                "height": result.height,
            }
        ),
        201 if result.created else 200,
    )


@app.post("/api/sessions/{session_id}/attachments", include_in_schema=False)
async def sessions_attachments_create(session_id: str, starlette_request: FastAPIRequest):
    """Stream a user upload into the session's media store.

    Multipart is the current browser contract. Base64 JSON remains accepted so
    a page opened before a service upgrade can finish uploading without a forced
    refresh; it should not be used by new clients.
    """

    async def handler():
        from core import workbench_media
        from core.file_browser_service import FileBrowserError
        from core.services import sessions as workbench_sessions_service

        def load_session():
            engine = _projects_engine()
            with engine.connect() as conn:
                session = workbench_sessions_service.get_session(conn, session_id)
            return engine, session

        try:
            engine, session = await asyncio.to_thread(load_session)
        except LookupError:
            return await _workbench_attachment_error("session_not_found", 404)

        form = None
        try:
            source = None
            legacy_data_b64 = None
            content_type = starlette_request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type == "multipart/form-data":
                _validate_file_upload_content_length(
                    starlette_request.headers,
                    workbench_media.MAX_WORKBENCH_ATTACHMENT_BYTES,
                )
                form = await _parse_file_upload_form(
                    starlette_request,
                    max_file_bytes=workbench_media.MAX_WORKBENCH_ATTACHMENT_BYTES,
                )
                upload = form.get("file")
                if not isinstance(upload, StarletteUploadFile):
                    return await _workbench_attachment_error("file_required", 400)
                await upload.seek(0)
                source = upload.file
                name = upload.filename
                mime = upload.content_type
                upload_id = form.get("upload_id")
            elif is_json_content_type(content_type):
                payload = request.json or {}
                data_b64 = payload.get("data") or ""
                if not isinstance(data_b64, str) or not data_b64:
                    return await _workbench_attachment_error("file_required", 400)
                legacy_data_b64 = data_b64
                name = payload.get("name")
                mime = payload.get("mime") or payload.get("content_type")
                upload_id = payload.get("upload_id")
            else:
                return await _workbench_attachment_error("invalid_upload", 415)

            stable_upload_id = workbench_media.normalize_workbench_upload_id(upload_id)

            def persist_attachment():
                attachment_source = source
                if legacy_data_b64 is not None:
                    attachment_source = workbench_media.decode_legacy_workbench_attachment(
                        legacy_data_b64
                    )
                if attachment_source is None:
                    raise workbench_media.WorkbenchAttachmentUploadError(
                        "file_required",
                        "File is required",
                        400,
                    )
                with workbench_media.workbench_attachment_upload_lock(
                    session_id, stable_upload_id
                ):
                    result = None
                    try:
                        with engine.begin() as conn:
                            result = workbench_media.materialize_workbench_attachment(
                                conn,
                                scope_id=session["scope_id"],
                                session_id=session_id,
                                file_name=name,
                                content_type=mime,
                                source=attachment_source,
                                upload_id=stable_upload_id,
                            )
                        return result
                    except Exception:
                        # Registration and commit are one logical publish. Keep
                        # rollback under the same key lock so a waiting retry
                        # cannot recreate this path before cleanup completes.
                        if result is not None and result.created:
                            Path(result.path).unlink(missing_ok=True)
                        raise

            result = await asyncio.to_thread(persist_attachment)
            return _workbench_attachment_response(result)
        except workbench_media.WorkbenchAttachmentUploadError as exc:
            return await _workbench_attachment_error(exc.code, exc.status)
        except MultiPartException as exc:
            if "too large" in str(exc).lower():
                return await _workbench_attachment_error("too_large", 413)
            return await _workbench_attachment_error("invalid_upload", 400)
        except FileBrowserError as exc:
            code = "too_large" if exc.code == "too_large" else "invalid_upload"
            return await _workbench_attachment_error(code, exc.status_code)
        except StarletteHTTPException:
            return await _workbench_attachment_error("invalid_upload", 400)
        except Exception:
            logger.exception("workbench attachment upload failed for session %s", session_id)
            return await _workbench_attachment_error("upload_failed", 500)
        finally:
            if form is not None:
                await form.close()

    return await _dispatch_native_ui_request(starlette_request, handler)


@app.route("/api/asr/transcribe", methods=["POST"])
async def asr_transcribe():
    """Transcribe recorded audio (base64 JSON) via the avibe.bot ASR client and
    return the text for the composer to fill in. Reuses ``AudioAsrService`` — the
    same client the IM voice-note path uses — so it needs only a V2Config."""
    import base64
    import tempfile
    import uuid

    from core.audio_asr import (
        AudioAsrEmptyTranscriptError,
        AudioAsrInvalidDictationError,
        AudioAsrProtocolError,
        AudioAsrService,
        AudioAsrTimeoutError,
        AudioAsrUnavailableError,
    )
    from core.services import settings as settings_service
    from modules.im.base import FileAttachment

    payload = request.json or {}
    finalize_only = payload.get("finalize_only") is True
    data_b64 = payload.get("data") or ""
    raw = b""
    if not finalize_only:
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
            return jsonify({"error": "file_too_large"}), 413

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
    audio_asr_config = getattr(config, "audio_asr", None)
    max_file_bytes = getattr(audio_asr_config, "max_file_bytes", None)
    if max_file_bytes is not None and raw and len(raw) > max_file_bytes:
        return jsonify({"error": "file_too_large"}), 413

    dictation_id = payload.get("dictation_id")
    if not isinstance(dictation_id, str) or not dictation_id:
        dictation_id = f"legacy-{uuid.uuid4().hex}"
    sequence = payload.get("sequence", 0)
    overlap_ms = payload.get("overlap_ms", 0)
    final = payload.get("final", True)
    receipts = payload.get("receipts", [])
    before = payload.get("before", "")
    after = payload.get("after", "")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not isinstance(overlap_ms, int)
        or isinstance(overlap_ms, bool)
        or not isinstance(final, bool)
        or not isinstance(receipts, list)
        or not all(isinstance(receipt, str) for receipt in receipts)
        or not isinstance(before, str)
        or not isinstance(after, str)
    ):
        return jsonify({"error": "invalid_dictation"}), 422

    tmp_path = None
    attachment = None
    if raw:
        suffix = Path(name).suffix or ".webm"
        tmp_path = Path(tempfile.gettempdir()) / f"vibe_asr_{uuid.uuid4().hex[:8]}{suffix}"
        tmp_path.write_bytes(raw)
        attachment = FileAttachment(name=name, mimetype=mime, local_path=str(tmp_path), size=len(raw))
    try:
        try:
            result = await service.transcribe_voice_segment(
                attachment,
                dictation_id=dictation_id,
                sequence=sequence,
                overlap_ms=overlap_ms,
                final=final,
                finalize_only=finalize_only,
                receipts=receipts,
                before=before,
                after=after,
                timeout_seconds=155.0,
            )
        except AudioAsrEmptyTranscriptError:
            return jsonify({"error": "transcription_empty"}), 422
        except AudioAsrInvalidDictationError:
            return jsonify({"error": "invalid_dictation"}), 422
        except AudioAsrTimeoutError:
            return jsonify({"error": "transcription_timeout"}), 504
        except AudioAsrUnavailableError:
            return jsonify({"error": "asr_unavailable"}), 503
        except AudioAsrProtocolError:
            return jsonify({"error": "transcription_failed"}), 502
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return jsonify(result)


@app.route("/api/asr/telemetry", methods=["POST"])
def asr_telemetry():
    """Persist privacy-safe browser voice metrics in the normal service log."""
    from vibe import __version__

    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_payload"}), 400

    event = payload.get("event")
    if not isinstance(event, str) or event not in {
        "segment_transcription",
        "dictation_finalized",
        "dictation_inserted",
    }:
        return jsonify({"error": "invalid_event"}), 400

    enum_fields = {
        "outcome": {
            "success",
            "fallback",
            "cancelled",
            "empty",
            "failed",
            "timeout",
            "too_large",
            "unavailable",
        },
        "path": {"cloud", "local"},
        "providerStage": {"token", "upload", "refresh", "response", "finalization"},
        "browserFamily": {"chrome", "firefox", "edge", "safari", "other", "unknown"},
    }
    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or outcome not in enum_fields["outcome"]:
        return jsonify({"error": "invalid_outcome"}), 400

    sanitized: dict[str, Any] = {
        "release": __version__,
        "event": event,
        "outcome": outcome,
    }
    dictation_id = payload.get("dictationId")
    if dictation_id is not None:
        if not isinstance(dictation_id, str) or not re.fullmatch(
            r"[a-z0-9_-]{1,80}",
            dictation_id,
            flags=re.IGNORECASE,
        ):
            return jsonify({"error": "invalid_field", "field": "dictationId"}), 400
        sanitized["dictationId"] = dictation_id

    for key, allowed_values in enum_fields.items():
        if key == "outcome" or key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or value not in allowed_values:
            return jsonify({"error": "invalid_field", "field": key}), 400
        sanitized[key] = value

    mime_type = payload.get("mimeType")
    if mime_type is not None:
        if not isinstance(mime_type, str) or not re.fullmatch(
            r"(?:audio|video)/[a-z0-9][a-z0-9.+_-]{0,63}",
            mime_type,
            flags=re.IGNORECASE,
        ):
            return jsonify({"error": "invalid_field", "field": "mimeType"}), 400
        sanitized["mimeType"] = mime_type.lower()

    integer_fields = {
        "sizeBytes",
        "durationMs",
        "elapsedMs",
        "attemptCount",
        "segmentCount",
        "failedSegmentCount",
        "backlogAtStop",
        "totalDurationMs",
        "stopToInsertionMs",
        "firstPreviewMs",
        "stopToFinalMs",
    }
    for key in integer_fields:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10**12:
            return jsonify({"error": "invalid_field", "field": key}), 400
        sanitized[key] = value

    if "httpStatus" in payload:
        http_status = payload["httpStatus"]
        if (
            not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or not 100 <= http_status <= 599
        ):
            return jsonify({"error": "invalid_field", "field": "httpStatus"}), 400
        sanitized["httpStatus"] = http_status

    if "retry" in payload:
        if not isinstance(payload["retry"], bool):
            return jsonify({"error": "invalid_field", "field": "retry"}), 400
        sanitized["retry"] = payload["retry"]

    if "realtime" in payload:
        if not isinstance(payload["realtime"], bool):
            return jsonify({"error": "invalid_field", "field": "realtime"}), 400
        sanitized["realtime"] = payload["realtime"]

    logger.info(
        "voice_reliability %s",
        json.dumps(sanitized, sort_keys=True, separators=(",", ":")),
    )
    return jsonify({"ok": True})


@app.route("/api/asr/status", methods=["GET"])
def asr_status():
    """Whether voice transcription is available (Vibe Cloud paired + enabled) so
    the composer can show/hide the mic button instead of guessing."""
    from core.audio_asr import AudioAsrService
    from core.services import settings as settings_service

    try:
        config = settings_service.load_config()
        audio_asr_config = getattr(config, "audio_asr", None)
        max_file_bytes = getattr(audio_asr_config, "max_file_bytes", None)
        if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
            max_file_bytes = None
        available = bool(AudioAsrService(config).is_available())
        # Browser capture uses 16 kHz mono 16-bit PCM. Smaller limits would
        # create sub-five-second segments and an impractical ASR request rate.
        min_browser_wav_bytes = 44 + (16_000 * 2 * 5)
        if max_file_bytes is not None and max_file_bytes < min_browser_wav_bytes:
            available = False
        return jsonify(
            {
                "available": available,
                "max_file_bytes": max_file_bytes,
            }
        )
    except Exception:
        return jsonify({"available": False, "max_file_bytes": None})


def _publish_visible_input_message(
    row: dict[str, Any],
    *,
    session_id: str,
    scope_id: str | None,
    activity_event: str = "user_message",
) -> dict[str, Any]:
    """Publish one already-visible input row through the shared fan-out."""
    from storage import messages_service
    from vibe.sse_broker import broker

    broker.publish("message.new", row)
    broker.publish(
        "session.activity",
        {"session_id": session_id, "scope_id": scope_id, "event": activity_event},
    )
    try:
        with _projects_engine().connect() as conn:
            inbox_row = messages_service.get_inbox_session(conn, session_id, platform="avibe")
        if inbox_row is not None:
            broker.publish("inbox.session.updated", inbox_row)
    except Exception:
        logger.debug("inbox.session.updated publish (user message) failed", exc_info=True)
    return row


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
    from core.web_push_notifications import (
        WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA,
        web_push_authorization_context_record,
    )
    from modules.im.message_facts import workbench_message_kind
    from storage import messages_service, resource_access_service
    from storage.agent_session_rows import session_is_runtime_owned
    from vibe import internal_client

    payload = request.json or {}
    text = payload.get("text")
    content = payload.get("content")
    if text is None and not content:
        return jsonify({"error": "text or content is required"}), 400
    # A quick-reply click tags the row with the agent message it answers.
    quick_reply_for = (payload.get("metadata") or {}).get("quick_reply_for")
    message_kind = workbench_message_kind(payload, quick_reply_for)
    web_push_user_key = _web_push_user_key()
    workbench_author_id = _workbench_author_id()
    web_push_authorization_context = web_push_authorization_context_record(
        web_push_user_key,
        getattr(g, "authorization_context", None),
    )

    engine = _projects_engine()
    try:
        with engine.connect() as conn:
            session = workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=getattr(g, "authorization_context", None),
            )
            from core.vibe_agents import ensure_session_agent_access

            _config, _session_payload, user_context = _remote_resource_access_context()
            ensure_session_agent_access(conn, session, user_context=user_context)
            # Archived sessions are terminal + inert: refuse to start a turn on one
            # even via a stale/direct request (the workbench hides them from the
            # list, so this only fires on a leftover tab or a hand-crafted call).
            if session.get("status") == "archived":
                return _session_archived_response()
            # A runtime-owned session accepts NO turn. The reserved workspace-notifications
            # row is ``visibility='system'``, which keeps it in the inbox on purpose — so
            # its card links into this chat, and a chat's composer POSTs here. Archive and
            # PATCH already refuse that row; this is the third door, and the only one that
            # could put a real agent turn (against an empty ``agent_backend``) and a user's
            # conversation into the machine's failure-notice transcript. The UI hides the
            # composer for the same fact (``sessionReadOnlyReason``); this guard is what
            # makes it true for a hand-crafted call. Free here — the payload just loaded
            # carries ``visibility``.
            if session_is_runtime_owned(session_id=session_id, visibility=session.get("visibility")):
                return _reserved_session_response(RESERVED_SESSION_READ_ONLY_I18N_KEY)
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
    except PermissionError as err:
        return _coded_error_response("agent_access_forbidden", str(err), 403)

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
        """Atomically persist one unaccepted submission as a Delivery."""
        from storage import message_deliveries
        from storage.agent_session_rows import reserve_write_lock

        with engine.begin() as conn:
            reserve_write_lock(conn)
            if workbench_sessions_service.is_session_archived(conn, session_id):
                return None
            delivery_id = message_deliveries.new_delivery_id()
            message_metadata = resource_access_service.metadata_with_resource_user_context(
                {
                    **(payload.get("metadata") or {}),
                    "_web_push_user_key": web_push_user_key,
                },
                getattr(g, "authorization_context", None),
            )
            message_metadata.pop(WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA, None)
            if web_push_authorization_context is not None:
                message_metadata[WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA] = [
                    web_push_authorization_context
                ]
            row = message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=session_id,
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=session["scope_id"],
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    text=text if isinstance(text, str) else None,
                    content=content if isinstance(content, dict) else None,
                    metadata=message_metadata,
                    author_id=workbench_author_id,
                    author_name=payload.get("author_name"),
                    message_kind=message_kind,
                ),
                dispatch_text=dispatch_text,
                history_event={"kind": "admission", "priority": "p3", "state": "reserved"},
            )
            if not quick_reply_for:
                message_deliveries.set_draft(conn, session_id, None)
            draft = message_deliveries.get_draft_state(conn, session_id)
            workbench_sessions_service.touch_session(conn, session_id)
        result = message_deliveries.public_delivery_payload(row)
        result["draft"] = _session_draft_payload(draft)
        result["draft_advanced"] = not bool(quick_reply_for)
        return result

    # Reserve the row FIRST (pending), then decide by the dispatch outcome.
    message = _persist_user_row()
    if message is None:
        # Archived between the pre-flight check and the reservation — stay terminal.
        return _session_archived_response()
    if not dispatch_text.strip() and not attachment_specs:
        from storage import message_deliveries

        with engine.begin() as conn:
            message_deliveries.retire_reserved(
                conn,
                session_id,
                str(message["id"]),
                reason="empty_submission",
            )
        return jsonify({"error": "empty submission"}), 400
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
        "display_text": message.get("text") or "",
        "content": content if isinstance(content, dict) else None,
        "metadata": payload.get("metadata") or {},
        "author_id": workbench_author_id,
        "author_name": payload.get("author_name"),
        "files": attachment_specs,
        "message_id": message.get("id"),
        "message_kind": message_kind,
    }

    def _current_delivery_response() -> dict:
        from storage import message_deliveries

        with engine.connect() as conn:
            current = message_deliveries.get_delivery(conn, str(message["id"]))
        if current is None:
            return dict(message)
        payload = message_deliveries.public_delivery_payload(current)
        payload["draft"] = message["draft"]
        payload["draft_advanced"] = message["draft_advanced"]
        if current["state"] == "queued":
            payload["type"] = "queued"
            payload["queued"] = True
        return payload

    def _retire_unclaimed_delivery(reason: str) -> dict:
        from storage import message_deliveries
        from storage.agent_session_rows import reserve_write_lock

        with engine.begin() as conn:
            reserve_write_lock(conn)
            message_deliveries.retire_reserved(
                conn,
                session_id,
                str(message["id"]),
                reason=reason,
            )
        return _current_delivery_response()

    try:
        result = await internal_client.dispatch_async(dispatch_payload)
    except internal_client.InternalServerTimeout as exc:
        current = _current_delivery_response()
        return jsonify(
            {
                **current,
                "dispatch_error": "dispatch_pending",
                "detail": str(exc),
            }
        ), 504
    except internal_client.InternalServerUnavailable as exc:
        current = _retire_unclaimed_delivery("internal_dispatch_unavailable")
        return jsonify(
            {
                **current,
                "dispatch_error": "internal_unavailable",
                "detail": str(exc),
            }
        ), 502
    except Exception as exc:
        logger.warning(
            "dispatch_async acceptance is unknown for session %s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        current = _current_delivery_response()
        return jsonify(
            {
                **current,
                "dispatch_error": "dispatch_pending",
                "detail": str(exc),
            }
        ), 502
    status = result.get("status_code", 500)
    body = result.get("body") or {}
    # Quick-reply accepted (turn started OR queued) → record the choice on the
    # AGENT message as the single source of truth for the locked/answered state.
    # Only on success, so a failed click stays retriable; ``set_quick_reply_chosen``
    # is set-once, so a rare double-dispatch still records one consistent answer.
    if status == 202 and quick_reply_for:
        with engine.begin() as conn:
            messages_service.set_quick_reply_chosen(conn, session_id, quick_reply_for, dispatch_text)
    if status == 202:
        delivery_state = str(body.get("delivery_state") or "")
        current = _current_delivery_response()
        if delivery_state == "accepted":
            accepted_message_id = str(
                body.get("message_id")
                or current.get("message_id")
                or message["id"]
            )
            with engine.connect() as conn:
                accepted = messages_service.get_message(conn, accepted_message_id)
            if accepted is None:
                return jsonify(
                    {
                        **current,
                        **body,
                        "dispatch_error": "dispatch_pending",
                    }
                ), 502
            return jsonify(
                {
                    **accepted,
                    **body,
                    "draft": message["draft"],
                    "draft_advanced": message["draft_advanced"],
                }
            ), 201
        return jsonify({**current, **body}), 202
    current = _retire_unclaimed_delivery(f"internal_dispatch_rejected_{status}")
    return jsonify(
        {
            **current,
            "dispatch_error": "dispatch_failed",
            "detail": body,
        }
    ), 502


@app.route("/api/sessions/<session_id>/cancel", methods=["POST"])
async def sessions_cancel(session_id: str):
    """Stop an in-flight ``dispatch_turn`` for this session.

    Proxies to ``POST /internal/cancel/<session_id>`` on the controller's
    Unix socket. Falls back to a 503 if the socket is unreachable so
    the UI can show a sensible "cannot stop right now" state instead
    of pretending the cancel succeeded.
    """

    from vibe import internal_client

    logger.info("Workbench Stop requested for session=%s", session_id)

    try:
        result = await internal_client.cancel_dispatch(session_id)
    except internal_client.InternalServerUnavailable as exc:
        return jsonify({"ok": False, "code": "internal_unavailable", "detail": str(exc)}), 503
    status = result.get("status_code", 500)
    body = result.get("body") or {}
    body.setdefault("ok", status == 200)
    body.setdefault("recovered_agent_status", False)
    logger.info(
        "Workbench Stop settled for session=%s status=%s outcome=%s code=%s",
        session_id,
        status,
        body.get("status"),
        body.get("code"),
    )
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
            authorization_context = getattr(g, "authorization_context", None)
            session = workbench_sessions_service.get_session(
                conn,
                session_id,
                authorization_context=authorization_context,
            )
            updated = messages_service.mark_session_read(
                conn, session_id, until_message_id=until_message_id
            )
            accessible_scope_ids = _request_accessible_project_scope_ids(conn)
            unread_counts = messages_service.unread_counts(
                conn,
                platform="avibe",
                scope_ids=accessible_scope_ids,
            )
            unread_by_session = messages_service.unread_counts_by_session(
                conn,
                platform="avibe",
                scope_ids=accessible_scope_ids,
            )
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
    """Return independent foreground, Inbox, Activity, and connection facts."""
    from vibe import internal_client

    try:
        result = await internal_client.turn_state(session_id)
    except internal_client.InternalServerUnavailable:
        return jsonify(
            _session_runtime_projection(
                None,
                controller_available=False,
                authorization_context=getattr(g, "authorization_context", None),
            )
        )
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
    projection = _session_runtime_projection(
        body,
        authorization_context=getattr(g, "authorization_context", None),
    )
    projection["recovered_agent_status"] = bool(
        body.get("recovered_agent_status", False)
    )
    return jsonify(projection)


@app.route("/api/sessions/<session_id>/queue", methods=["GET"])
def sessions_queue_list(session_id: str):
    """Pending send-while-busy messages for a session (shown above the composer)."""
    from storage import message_deliveries

    engine = _projects_engine()
    with engine.connect() as conn:
        queued = [
            message_deliveries.public_delivery_payload(item)
            for item in message_deliveries.list_queued(conn, session_id)
        ]
    return jsonify({"queued": queued})


@app.route("/api/sessions/<session_id>/queue/<message_id>", methods=["DELETE"])
def sessions_queue_remove(session_id: str, message_id: str):
    """Drop one queued message (the per-item delete in the queue strip)."""
    from storage import message_deliveries
    from storage.agent_session_rows import reserve_write_lock
    from storage.background import run_update_event_transaction
    from vibe.sse_broker import broker

    engine = _projects_engine()
    with run_update_event_transaction(engine) as conn:
        reserve_write_lock(conn)
        removed = message_deliveries.retire_queued_with_run(conn, session_id, message_id)
    if removed:
        broker.publish("queue.updated", {"session_id": session_id})
    return jsonify({"removed": bool(removed)})


@app.route("/api/sessions/<session_id>/queue/<message_id>/send-now", methods=["POST"])
async def sessions_queue_send_now(session_id: str, message_id: str):
    """Promote the exact queue head represented by the clicked row."""
    from vibe import internal_client

    try:
        result = await internal_client.send_now(
            session_id,
            expected_delivery_id=message_id,
        )
    except internal_client.InternalServerUnavailable as exc:
        return jsonify({"ok": False, "code": "internal_unavailable", "detail": str(exc)}), 503
    status = result.get("status_code", 500)
    body = result.get("body") or {}
    body.setdefault("ok", status < 400)
    return jsonify(body), status


def _session_draft_payload(draft: dict | None) -> dict:
    return {
        "text": (draft or {}).get("text") or "",
        "updated_at": (draft or {}).get("updated_at"),
    }


@app.route("/api/sessions/<session_id>/draft", methods=["GET"])
def sessions_draft_get(session_id: str):
    """The session's saved unsent compose text (restored on open / device switch)."""
    from storage import message_deliveries

    engine = _projects_engine()
    with engine.connect() as conn:
        draft = message_deliveries.get_draft_state(conn, session_id)
    return jsonify(_session_draft_payload(draft))


@app.route("/api/sessions/<session_id>/draft", methods=["PUT"])
def sessions_draft_set(session_id: str):
    """Upsert the session's draft (debounced from the composer). Blank clears it."""
    from core.services import sessions as workbench_sessions_service
    from storage import message_deliveries
    from storage.agent_session_rows import reserve_write_lock

    payload = request.json or {}
    text = payload.get("text")
    expected_supplied = "expected_updated_at" in payload
    expected_updated_at = payload.get("expected_updated_at")
    if expected_supplied and expected_updated_at is not None and not isinstance(expected_updated_at, str):
        return jsonify({"ok": False, "code": "invalid_expected_updated_at"}), 400
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            # The version read and its update are one CAS decision. Reserving
            # SQLite's writer slot before either read prevents a concurrent
            # commit from turning this transaction's snapshot into BUSY_SNAPSHOT.
            reserve_write_lock(conn)
            session = workbench_sessions_service.get_session(conn, session_id)
            # Archive is terminal: drop a late/debounced draft save (e.g. the
            # composer flushing as it unmounts right after archive) so it can't
            # recreate a draft on a session whose drafts were just reclaimed.
            if session.get("status") == "archived":
                current = message_deliveries.get_draft_state(conn, session_id)
                return jsonify({"ok": True, "draft": _session_draft_payload(current)})
            current = message_deliveries.get_draft_state(conn, session_id)
            current_updated_at = (current or {}).get("updated_at")
            if (
                (not expected_supplied and current_updated_at is not None)
                or (expected_supplied and current_updated_at != expected_updated_at)
            ):
                return jsonify(
                    {
                        "ok": False,
                        "code": "draft_conflict",
                        "draft": _session_draft_payload(current),
                    }
                ), 409
            message_deliveries.set_draft(
                conn,
                session_id,
                text if isinstance(text, str) else None,
            )
            saved = message_deliveries.get_draft_state(conn, session_id)
    except LookupError as err:
        return jsonify({"error": str(err)}), 404
    return jsonify({"ok": True, "draft": _session_draft_payload(saved)})


def _workbench_event_data(payload: str) -> dict[str, Any] | None:
    try:
        envelope = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    data = envelope.get("data") if isinstance(envelope, dict) else None
    return data if isinstance(data, dict) else None


def _workbench_event_heartbeat_interval_ms() -> int:
    return int(WORKBENCH_EVENT_HEARTBEAT_INTERVAL_S * 1000)


def _workbench_event_connected_frame(sub_id: int) -> str:
    """The handshake: this subscription's id, and the cadence it is owed.

    Carrying the cadence here is what lets a client hold a brand-new stream to a
    deadline. The promise arrives before the first heartbeat does, so a stream
    that opens and then goes silent has something to have broken -- without it,
    the client's only options are to trust an unproven stream for a whole window
    or to watchdog servers that never promised anything.

    A declaration, never proof: a server too old to send this field is simply
    never held to a cadence, and a client must still wait for a heartbeat before
    believing the stream is carrying events.
    """
    interval_ms = _workbench_event_heartbeat_interval_ms()
    return f'event: connected\ndata: {{"sub_id":{sub_id},"interval_ms":{interval_ms}}}\n\n'


def _workbench_event_heartbeat_frame() -> str:
    """A frame whose only job is to be seen.

    An SSE comment keeps proxies awake but never reaches ``EventSource``, so a
    client watching a quiet stream cannot distinguish it from a socket that died
    while the tab was suspended -- iOS in particular leaves such a stream in a
    zombie ``OPEN`` state with no ``error``. Carrying the cadence lets the client
    size its own staleness tolerance from the server that sets it, rather than
    duplicating the interval on both sides -- which is also why the interval
    belongs in the payload even though nothing else here needs a body.
    """
    interval_ms = _workbench_event_heartbeat_interval_ms()
    return f'event: heartbeat\ndata: {{"interval_ms":{interval_ms}}}\n\n'


def _workbench_event_visible_to_context(context, event_type: str, payload: str) -> bool:
    if context is None:
        return True
    if event_type == "show.event":
        # A Show Page's Workbench stream follows §3.2 Instance Viewer admission,
        # so any Instance role sees its page's live events while an email-grant
        # ``/p`` visitor never receives annotation text or attachment metadata.
        # Fail closed when the frame has no session to check.
        data = _workbench_event_data(payload)
        session_id = data.get("session_id") if data else None
        if not isinstance(session_id, str) or not session_id:
            return False
        if not _show_page_resource_access_allowed(context, session_id):
            return False
        # Show Page event visibility is intentionally independent from Project
        # ACL. Project ACL gates page creation/editing, while §3.2 instance
        # admission gates Viewer reads and live event delivery.
        return context.has_role("viewer")
    if _has_runtime_owner_access(context):
        return True
    if event_type in {"authorization.changed", "workbench.events.bridge.status"}:
        return True
    data = _workbench_event_data(payload)
    if data is None:
        return False

    from storage import project_access_service

    engine = _projects_engine()
    with engine.connect() as conn:
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            return project_access_service.role_allows(
                project_access_service.get_effective_session_role(
                    conn,
                    context,
                    session_id,
                ),
                "viewer",
            )
        project_id = project_access_service.project_id_from_scope_id(data.get("scope_id"))
        if project_id is not None:
            return project_access_service.can_read_project(conn, context, project_id)
    return False


def _workbench_event_payload_for_context(context, event_type: str, payload: str) -> str | None:
    """Project-filter aggregate payloads whose values depend on the recipient.

    Returns ``None`` when the event cannot be projected safely for this
    recipient, in which case the caller drops the frame.
    """
    if (
        event_type == "show.event"
        and context is not None
        and context.is_remote
    ):
        # A Show annotation event carries the absolute host path of its
        # materialized screenshot. Every remote recipient reads the image by
        # attachment id, so the path is useless to them and only discloses
        # the host's directory layout — drop it for every remote reader
        # and drop the whole frame
        # if it cannot be projected. Host-path redaction is transport safety,
        # independent of Instance role or Organization membership.
        try:
            envelope = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return None
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            return None
        return json.dumps(
            {**envelope, "data": _remote_safe_show_event_payload(data)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if event_type != "inbox.unread.changed":
        return payload
    try:
        envelope = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return payload
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return payload

    from storage import messages_service

    engine = _projects_engine()
    with engine.connect() as conn:
        scope_ids = _accessible_project_scope_ids_for_context(conn, context)
        unread_counts = messages_service.unread_counts(
            conn,
            platform="avibe",
            scope_ids=scope_ids,
        )
        unread_by_session = messages_service.unread_counts_by_session(
            conn,
            platform="avibe",
            scope_ids=scope_ids,
        )
    return json.dumps(
        {
            **envelope,
            "data": {
                **data,
                "unread_counts": unread_counts,
                "unread_by_session": unread_by_session,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@app.route("/api/events", methods=["GET"])
async def workbench_events():
    """Server-Sent Events stream for the workbench.

    Browsers open this once and keep it open; the route streams JSON
    events (message.new, session.activity, inbox.unread.changed) as
    they happen elsewhere in the app, plus a ``heartbeat`` event every
    ``WORKBENCH_EVENT_HEARTBEAT_INTERVAL_S`` so Cloudflare-style proxies
    don't kill the idle TCP connection and the client can tell a quiet
    stream from a dead one.

    Native FastAPI ``StreamingResponse`` so the loop stays async and
    each browser only costs one task, not one OS thread.
    """

    import asyncio

    from fastapi.responses import StreamingResponse

    from core.inbox_events import WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT
    from vibe.authorization import can_receive_workbench_event
    from vibe.inbox_bridge import is_bridge_connected
    from vibe.sse_broker import broker

    authorization_context = getattr(g, "authorization_context", None)
    remote_session_identity = getattr(g, "remote_session_identity", None)
    remote_session_payload = getattr(g, "remote_session_payload", None)
    remote_config = _load_remote_access_config() if remote_session_payload is not None else None
    remote_session_cookie = None
    remote_request_host = None
    if remote_session_payload is not None:
        from vibe import remote_access

        remote_session_cookie = request.cookies.get(remote_access.SESSION_COOKIE_NAME)
        remote_request_host = _effective_normalized_host()

    async def authorization_state() -> str:
        if remote_session_payload is None:
            return "current"
        if remote_config is None or remote_session_identity is None:
            return "invalid_identity"
        return await _remote_stream_authorization_state(
            remote_config,
            remote_session_identity,
            remote_session_payload,
            session_cookie=remote_session_cookie,
            request_host=remote_request_host,
        )

    async def generate():
        sub_id, queue = broker.subscribe()
        # Baselined here, before anything can suspend or reach the client. A
        # fresh subscription is owed everything from this instant on, so the
        # count is 0 by construction -- reading it late looked equivalent and is
        # not: the authorization await and the handshake below are suspension
        # points, and the client can finish its connect catch-up while this
        # generator is parked between them. A burst discarded in that window
        # would then become the baseline and never be reported.
        last_dropped = broker.dropped_count(sub_id)
        try:
            state = await authorization_state()
            if state != "current":
                yield _remote_authorization_sse_frame(state)
                return
            # First chunk = handshake + sub_id so the client can include it in
            # subsequent debug logs / cancel calls if we ever need them.
            yield ": stream connected\n\n"
            yield _workbench_event_connected_frame(sub_id)
            payload = json.dumps(
                {
                    "type": WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT,
                    "data": {"connected": is_bridge_connected()},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"event: {WORKBENCH_EVENTS_BRIDGE_STATUS_EVENT}\ndata: {payload}\n\n"
            last_heartbeat_at = time.monotonic()
            while True:
                state = await authorization_state()
                if state != "current":
                    yield _remote_authorization_sse_frame(state)
                    return
                # A subscriber that lost an event is not a subscriber any more,
                # so end its stream and let it reconnect: the fresh subscription
                # gets an empty queue and the client's reconnect path already
                # catches consumers up exactly once, with backoff if the load
                # that overflowed the queue is still going. Announcing the hole
                # on this stream instead looks cheaper and is not -- the queue
                # stays full, so the next iteration finds another discard and
                # announces again, starving the payload frames it was warning
                # about. Reopening is the actual repair here, unlike the
                # controller leg, which announces in place because a new socket
                # would inherit the same severed bridge.
                #
                # Checked before the heartbeat: a heartbeat claims this stream is
                # worth trusting, which stopped being true.
                dropped = broker.dropped_count(sub_id)
                if dropped > last_dropped:
                    logger.warning(
                        "workbench events: ending subscriber %s after %s dropped event(s)",
                        sub_id,
                        dropped - last_dropped,
                    )
                    return
                # A fixed cadence, deliberately not "only when the queue went
                # quiet". Data frames are no proof of life to a client that may
                # be filtered out of all of them, and one unconditional clock
                # means each side has exactly one thing to stamp.
                since_heartbeat = time.monotonic() - last_heartbeat_at
                if since_heartbeat >= WORKBENCH_EVENT_HEARTBEAT_INTERVAL_S:
                    yield _workbench_event_heartbeat_frame()
                    last_heartbeat_at = time.monotonic()
                    continue
                try:
                    # Floored so an event arriving just short of the deadline
                    # cannot spin this loop; the heartbeat is at most that late.
                    event_type, payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=max(0.25, WORKBENCH_EVENT_HEARTBEAT_INTERVAL_S - since_heartbeat),
                    )
                    state = await authorization_state()
                    if state != "current":
                        yield _remote_authorization_sse_frame(state)
                        return
                    if not can_receive_workbench_event(authorization_context, event_type):
                        continue
                    visible = await asyncio.to_thread(
                        _workbench_event_visible_to_context,
                        authorization_context,
                        event_type,
                        payload,
                    )
                    if not visible:
                        continue
                    payload = await asyncio.to_thread(
                        _workbench_event_payload_for_context,
                        authorization_context,
                        event_type,
                        payload,
                    )
                    if payload is None:
                        continue
                    if (
                        event_type == "authorization.changed"
                        and authorization_context is not None
                        and not _has_runtime_owner_access(authorization_context)
                    ):
                        payload = json.dumps(
                            {
                                "type": "authorization.changed",
                                "data": {"project_ids": []},
                            },
                            separators=(",", ":"),
                        )
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Nothing to forward. Loop round so the heartbeat is emitted
                    # by the one branch that owns it, after a fresh auth check.
                    continue
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
    # Targeted single-session fetch: lets a client (e.g. the Inbox visibility
    # reconcile) guarantee one specific session's row is (re)loaded even when its
    # activity sorts past the paged window.
    only_session = request.args.get("session") or None

    engine = _projects_engine()
    with engine.connect() as conn:
        accessible_scope_ids = _request_accessible_project_scope_ids(conn)
        result = messages_service.list_inbox_sessions(
            conn,
            platform=scope_filter,
            unread_only=unread_only,
            limit=limit,
            before=before,
            only_session=only_session,
            scope_ids=accessible_scope_ids,
        )
        # Pagination-independent unread map for the sidebar badges (a session
        # with unread may sit past the first inbox page) + header totals.
        per_session = messages_service.unread_counts_by_session(
            conn,
            platform=scope_filter,
            scope_ids=accessible_scope_ids,
        )
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
    """``?status=`` for tasks/watches, validated against the store's own filter
    table so the route cannot accept a value the query would reject (or reject
    one it would accept)."""
    from storage.background import DEFINITION_STATUS_FILTERS

    status = request.args.get("status") or "all"
    if status not in DEFINITION_STATUS_FILTERS:
        raise ValueError("status must be one of: " + ", ".join(DEFINITION_STATUS_FILTERS))
    return status


def _harness_query_filter() -> str | None:
    query = (request.args.get("query") or "").strip()
    return query or None


def _harness_exclude_run_type() -> list[str]:
    """``?exclude_run_type=a,b`` — the one parsing site for the Runs tab's
    "hide watcher heartbeats" default. An exclusion (rather than a hardcoded
    include-list) keeps a future run type visible by default."""
    raw = request.args.get("exclude_run_type") or ""
    return [value for value in (part.strip() for part in raw.split(",")) if value]


def _harness_session_filter() -> str | None:
    # ``?session=<id>`` — the background-work banner navigates here to scope a
    # tab to its originating session (the removable "只看本会话" chip).
    session_id = (request.args.get("session_id") or "").strip()
    return session_id or None


def _harness_has_list_params() -> bool:
    return any(key in request.args for key in ("page", "limit", "status", "query", "session_id"))


def _harness_page_payload(page_result, *, items_key: str, counts: dict[str, int]) -> dict[str, Any]:
    return _harness_page_payload_for_status(
        page_result,
        items_key=items_key,
        counts=counts,
        status=request.args.get("status") or "all",
    )


def _harness_page_payload_for_status(page_result, *, items_key: str, counts: dict[str, int], status: str) -> dict[str, Any]:
    # Tasks/watches only — runs build their payload from their own count call.
    from storage.background import definition_status_total

    total = definition_status_total(counts, status)
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
                "total": counts["total"],
                "page": 1,
                "limit": len(tasks),
                "has_more": False,
            }
        )
    try:
        page_request = _harness_page_request()
        status = _harness_status_filter()
        query = _harness_query_filter()
        session_id = _harness_session_filter()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400
    with _harness_store() as store:
        page_result = store.list_scheduled_tasks_page(
            status=status,
            query=query,
            session_id=session_id,
            page_request=page_request,
            newest_first=True,
        )
        counts = store.count_scheduled_tasks(query=query, session_id=session_id)
    return jsonify(_harness_page_payload(page_result, items_key="tasks", counts=counts))


@app.route("/api/harness/tasks/<task_id>", methods=["PATCH"])
def harness_task_patch(task_id: str):
    payload = request.json or {}
    if "enabled" not in payload:
        return jsonify({"ok": False, "code": "invalid_payload", "message": "missing 'enabled'"}), 400
    enabled = bool(payload["enabled"])
    from storage.background import TaskResumeBlocked, TaskScheduleRetired

    with _harness_store() as store:
        if not store.get_scheduled_task(task_id):
            return jsonify({"ok": False, "code": "task_not_found"}), 404
        try:
            store.set_definition_enabled(task_id, enabled, definition_type="scheduled")
        except TaskResumeBlocked as exc:
            return _task_resume_blocked_response(exc)
        except TaskScheduleRetired as exc:
            return _task_schedule_retired_response(exc)
        task = store.get_scheduled_task(task_id)
    from core.inbox_events import publish_definitions_updated

    publish_definitions_updated(definition_type="scheduled")
    return jsonify({"ok": True, "task": task})


@app.route("/api/harness/tasks/<task_id>", methods=["DELETE"])
def harness_task_delete(task_id: str):
    with _harness_store() as store:
        if not store.get_scheduled_task(task_id):
            return jsonify({"ok": False, "code": "task_not_found"}), 404
        store.remove_task(task_id)
    from core.inbox_events import publish_definitions_updated

    publish_definitions_updated(definition_type="scheduled")
    return jsonify({"ok": True, "id": task_id})


@app.route("/api/harness/watches", methods=["GET"])
def harness_watches_list():
    if not _harness_has_list_params():
        with _harness_store() as store:
            watches = store.list_watches()
            counts = store.count_watches()
        return jsonify(
            {
                "watches": watches,
                "counts": counts,
                "total": counts["total"],
                "page": 1,
                "limit": len(watches),
                "has_more": False,
            }
        )
    try:
        page_request = _harness_page_request()
        status = _harness_status_filter()
        query = _harness_query_filter()
        session_id = _harness_session_filter()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400
    with _harness_store() as store:
        page_result = store.list_watches_page(
            status=status,
            query=query,
            session_id=session_id,
            page_request=page_request,
            newest_first=True,
        )
        counts = store.count_watches(query=query, session_id=session_id)
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
    from core.inbox_events import publish_definitions_updated

    publish_definitions_updated(definition_type="watch")
    return jsonify({"ok": True, "watch": watch})


@app.route("/api/harness/watches/<watch_id>", methods=["DELETE"])
def harness_watch_delete(watch_id: str):
    with _harness_store() as store:
        if not store.get_watch(watch_id):
            return jsonify({"ok": False, "code": "watch_not_found"}), 404
        store.remove_task(watch_id)
    from core.inbox_events import publish_definitions_updated

    publish_definitions_updated(definition_type="watch")
    return jsonify({"ok": True, "id": watch_id})


@app.route("/api/harness/runs", methods=["GET"])
def harness_runs_list():
    try:
        page_request = _harness_page_request()
    except ValueError as exc:
        return jsonify({"ok": False, "code": "invalid_pagination", "message": str(exc)}), 400
    status = request.args.get("status") or None
    run_type = request.args.get("run_type") or None
    exclude_run_type = _harness_exclude_run_type()
    agent_name = request.args.get("agent_name") or None
    definition_id = request.args.get("definition_id") or None
    query = _harness_query_filter()

    with _harness_store() as store:
        page_result = store.list_runs_page(
            status=status,
            run_type=run_type,
            exclude_run_type=exclude_run_type,
            agent_name=agent_name,
            definition_id=definition_id,
            query=query,
            page_request=page_request,
            newest_first=True,
        )
        total = store.count_runs(
            status=status,
            run_type=run_type,
            exclude_run_type=exclude_run_type,
            agent_name=agent_name,
            definition_id=definition_id,
            query=query,
        )
        counts = store.count_runs_by_status(
            run_type=run_type,
            exclude_run_type=exclude_run_type,
            agent_name=agent_name,
            definition_id=definition_id,
            query=query,
        )
        # The types present in the ledger, so the selector can offer one the UI
        # has no built-in name for instead of stranding those rows under All.
        run_types = store.list_run_types()
    return jsonify(
        {
            "runs": page_result.items,
            "counts": counts,
            "run_types": run_types,
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
        # Session scope from the background-work banner (tasks/watches only; a
        # delegated run is anchored by ``?run=`` on the client, not filtered by
        # its execution session here).
        session_id = _harness_session_filter() if tab in {"tasks", "watches"} else None
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
                session_id=session_id,
                page_request=page_request,
                newest_first=True,
            )
            page_payload = _harness_page_payload_for_status(
                page_result,
                items_key="tasks",
                counts=store.count_scheduled_tasks(query=query, session_id=session_id),
                status=definition_status,
            )
        elif tab == "watches":
            page_result = store.list_watches_page(
                status=definition_status,
                query=query,
                session_id=session_id,
                page_request=page_request,
                newest_first=True,
            )
            page_payload = _harness_page_payload_for_status(
                page_result,
                items_key="watches",
                counts=store.count_watches(query=query, session_id=session_id),
                status=definition_status,
            )
        else:
            run_status = request.args.get("status") or None
            run_type = request.args.get("run_type") or None
            exclude_run_type = _harness_exclude_run_type()
            agent_name = request.args.get("agent_name") or None
            definition_id = request.args.get("definition_id") or None
            page_result = store.list_runs_page(
                status=run_status,
                run_type=run_type,
                exclude_run_type=exclude_run_type,
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
                    exclude_run_type=exclude_run_type,
                    agent_name=agent_name,
                    definition_id=definition_id,
                    query=query,
                ),
                # Same facet as /api/harness/runs — the tab loads through
                # whichever of the two the caller reached, so both must carry it.
                "run_types": store.list_run_types(),
                "total": store.count_runs(
                    status=run_status,
                    run_type=run_type,
                    exclude_run_type=exclude_run_type,
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
    from storage.settings_service import ScopeAgentUnavailableError, StaleScopeAgentBindingError

    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
    payload = request.json or {}
    try:
        return jsonify(api.save_users(payload))
    except StaleScopeAgentBindingError as exc:
        return _settings_conflict_response(exc)
    except ScopeAgentUnavailableError as exc:
        return _scope_agent_unavailable_response(exc)


@app.route("/api/users/<user_id>/admin", methods=["POST"])
def users_toggle_admin(user_id):
    from vibe import api

    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
    payload = request.json or {}
    return jsonify(api.toggle_admin(user_id, payload.get("is_admin", False), payload.get("platform") or None))


@app.route("/api/users/<user_id>", methods=["DELETE"])
def users_delete(user_id):
    from vibe import api

    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
    result = api.remove_user(user_id, request.args.get("platform") or None)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/bind-codes", methods=["GET"])
def bind_codes_get():
    from vibe import api

    # The listing carries the codes themselves, so reading it mints access.
    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
    return jsonify(api.get_bind_codes())


@app.route("/api/bind-codes", methods=["POST"])
def bind_codes_post():
    from vibe import api

    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
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

    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
    result = api.delete_bind_code(code)
    if not result.get("ok"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/setup/first-bind-code", methods=["GET"])
def setup_first_bind_code():
    from vibe import api

    # Named "setup", but it mints a live bind code rather than reporting state.
    forbidden = _access_administration_forbidden()
    if forbidden is not None:
        return forbidden
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

                # Patch-write shape (#1458 stage ③): only the field
                # this modal owns.
                result = vibe_api.save_config(
                    {"runtime": {"default_cwd": modal_values.get("cwd", "/tmp")}}
                )
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


def _show_page_accept_quality(accept: str, target: str) -> float:
    """Return the preferred quality for a response media type."""
    target_type, target_subtype = target.split("/", 1)
    best_specificity = -1
    best_quality = 0.0
    for item in accept.split(","):
        parts = [part.strip() for part in item.split(";")]
        media_range = parts[0].lower()
        if "/" not in media_range:
            continue
        range_type, range_subtype = media_range.split("/", 1)
        if range_type not in {target_type, "*"} or range_subtype not in {target_subtype, "*"}:
            continue
        specificity = (range_type != "*") + (range_subtype != "*")
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if name.strip().lower() != "q" or not separator:
                continue
            try:
                quality = float(value.strip().strip('"'))
            except ValueError:
                quality = 0.0
            break
        if not 0.0 <= quality <= 1.0:
            quality = 0.0
        if specificity > best_specificity:
            best_specificity = specificity
            best_quality = quality
    return best_quality


def _show_page_accepts_html() -> bool:
    """Choose HTML only when it is preferred and explicitly acceptable."""
    accept = request.headers.get("Accept", "").strip()
    if not accept:
        return False
    html_quality = _show_page_accept_quality(accept, "text/html")
    json_quality = _show_page_accept_quality(accept, "application/json")
    return html_quality > 0.0 and html_quality > json_quality


def _show_page_accepts_markdown_value(accept: str) -> bool:
    """Choose Markdown only when explicitly requested over HTML."""
    explicitly_requested = any(
        item.split(";", 1)[0].strip().lower() == "text/markdown"
        for item in accept.split(",")
    )
    if not explicitly_requested:
        return False
    markdown_quality = _show_page_accept_quality(accept, "text/markdown")
    html_quality = _show_page_accept_quality(accept, "text/html")
    return markdown_quality > 0.0 and markdown_quality >= html_quality


_PUBLIC_SHOW_REPRESENTATION_HEADERS = (
    "Accept",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Mode",
    "User-Agent",
)
_SHOW_PAGE_CRAWLER_USER_AGENT_RE = re.compile(
    r"(?:bot\b|crawler|spider|slurp|claudebot|perplexitybot|"
    r"anthropic-ai|cohere-ai|google-extended|bytespider)",
    re.IGNORECASE,
)
_SHOW_PAGE_KNOWN_NON_BROWSER_USER_AGENT_RE = re.compile(
    r"(?:chatgpt-user|claude-user|perplexity-user|mistralai-user|powershell/)",
    re.IGNORECASE,
)
_SHOW_PAGE_BROWSER_USER_AGENT_RE = re.compile(
    r"(?:mozilla/|applewebkit/|chrome/|chromium/|firefox/|safari/|edg/|opr/)",
    re.IGNORECASE,
)
_SHOW_PAGE_AGENT_OR_CLI_USER_AGENT_RE = re.compile(
    r"(?:curl/|wget/|httpie/|python-requests/|python-httpx/|go-http-client/|"
    r"libwww-perl/|powershell/|openai|anthropic|claude|perplexity|cohere|agent)",
    re.IGNORECASE,
)


def _public_show_page_explicit_representation(accept: str) -> str | None:
    """Return an explicit Show representation choice, if the header makes one."""
    media_types = {
        item.split(";", 1)[0].strip().lower()
        for item in accept.split(",")
    }
    markdown_quality = _show_page_accept_quality(accept, "text/markdown")
    html_quality = max(
        _show_page_accept_quality(accept, "text/html"),
        _show_page_accept_quality(accept, "application/xhtml+xml"),
    )
    html_explicit = bool({"text/html", "application/xhtml+xml"} & media_types)
    if (
        _show_page_accepts_markdown_value(accept)
        and (not html_explicit or markdown_quality >= html_quality)
    ):
        return "markdown"
    if html_explicit and html_quality > 0.0:
        return "html"
    if "text/markdown" in media_types:
        # An explicit Markdown range that loses quality negotiation (including
        # q=0) must not be turned back into Markdown by implicit client inference.
        return "html"
    return None


def _public_show_page_prefers_markdown(starlette_request: FastAPIRequest) -> bool:
    explicit = _public_show_page_explicit_representation(
        starlette_request.headers.get("accept", "")
    )
    if explicit is not None:
        return explicit == "markdown"

    # Fetch Metadata is a strong browser-shaped signal. Any supplied mode or
    # destination is conservatively kept on the interactive HTML surface,
    # including incomplete or contradictory browser requests.
    if (
        starlette_request.headers.get("sec-fetch-mode", "").strip()
        or starlette_request.headers.get("sec-fetch-dest", "").strip()
    ):
        return False

    user_agent = starlette_request.headers.get("user-agent", "").strip()
    if _SHOW_PAGE_CRAWLER_USER_AGENT_RE.search(user_agent):
        return True
    # User-initiated Agent fetchers and PowerShell deliberately use
    # browser-compatible UA prefixes. Their product tokens are more specific
    # than the surrounding Mozilla/WebKit tokens.
    if _SHOW_PAGE_KNOWN_NON_BROWSER_USER_AGENT_RE.search(user_agent):
        return True
    if _SHOW_PAGE_BROWSER_USER_AGENT_RE.search(user_agent):
        return False
    if _SHOW_PAGE_AGENT_OR_CLI_USER_AGENT_RE.search(user_agent):
        return True
    return True


def _show_page_not_found_html_response():
    language = _request_ui_language()
    title = html.escape(t("show.pageUnavailable.title", language), quote=True)
    heading = html.escape(t("show.pageUnavailable.heading", language))
    message = html.escape(t("show.pageUnavailable.message", language))
    html_body = """<!doctype html>
<html lang="__LANGUAGE__">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>__TITLE__</title>
    <style>
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fb; color: #172033; }
      main { width: min(560px, 100%); border: 1px solid rgba(23, 32, 51, 0.12); border-radius: 12px; background: white; padding: 32px; box-shadow: 0 20px 60px rgba(23, 32, 51, 0.10); }
      h1 { margin: 0; font-size: clamp(28px, 7vw, 42px); line-height: 1.05; letter-spacing: 0; }
      p { margin: 14px 0 0; line-height: 1.65; color: #526078; }
    </style>
  </head>
  <body>
    <main>
      <h1>__HEADING__</h1>
      <p>__MESSAGE__</p>
    </main>
  </body>
</html>
""".replace("__LANGUAGE__", language).replace("__TITLE__", title).replace("__HEADING__", heading).replace("__MESSAGE__", message)
    response = Response(html_body, status=404, mimetype="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _show_page_not_found_response():
    if (
        request.method in {"GET", "HEAD"}
        and _show_page_accepts_html()
    ):
        return _show_page_not_found_html_response()
    return jsonify({"error": "not_found"}), 404


def _show_page_access_denied_html_response(*, include_back_link: bool = True):
    language = _request_ui_language()
    title = html.escape(t("show.pageAccessDenied.title", language), quote=True)
    heading = html.escape(t("show.pageAccessDenied.heading", language))
    message = html.escape(t("show.pageAccessDenied.message", language))
    back = html.escape(t("show.pageAccessDenied.back", language))
    back_link = f'<a href="/">{back}</a>' if include_back_link else ""
    html_body = """<!doctype html>
<html lang="__LANGUAGE__">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>__TITLE__</title>
    <style>
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fb; color: #172033; }
      main { width: min(560px, 100%); border: 1px solid rgba(23, 32, 51, 0.12); border-radius: 12px; background: white; padding: 32px; box-shadow: 0 20px 60px rgba(23, 32, 51, 0.10); }
      h1 { margin: 0; font-size: clamp(28px, 7vw, 42px); line-height: 1.05; letter-spacing: 0; }
      p { margin: 14px 0 0; line-height: 1.65; color: #526078; }
      a { display: inline-block; margin-top: 22px; color: #3157d5; font-weight: 600; text-decoration: none; }
      a:hover { text-decoration: underline; }
    </style>
  </head>
  <body>
    <main>
      <h1>__HEADING__</h1>
      <p>__MESSAGE__</p>
      __BACK_LINK__
    </main>
  </body>
</html>
""".replace("__LANGUAGE__", language).replace("__TITLE__", title).replace("__HEADING__", heading).replace("__MESSAGE__", message).replace("__BACK_LINK__", back_link)
    response = Response(html_body, status=403, mimetype="text/html; charset=utf-8")
    return _with_limited_show_policy(response)


def _show_page_access_denied_response(*, include_back_link: bool = True):
    if (
        request.method in {"GET", "HEAD"}
        and _show_page_accepts_html()
    ):
        return _show_page_access_denied_html_response(include_back_link=include_back_link)
    response = jsonify({"error": "show_access_forbidden"})
    response.status_code = 403
    return _with_limited_show_policy(response)


def _show_page_file_not_found_response():
    response = jsonify({"error": "not_found"})
    response.status_code = 404
    return response


def _show_page_runtime_unavailable_response(reason: str):
    return jsonify({"error": "show_runtime_unavailable", "reason": reason}), 503


def _show_page_runtime_timeout_response():
    return jsonify({"error": "show_runtime_request_timeout"}), 504


def _is_show_page_api_handler_path(asset_path: str) -> bool:
    relative = (asset_path or "").strip("/")
    return relative == "api" or relative.startswith("api/")


def _is_show_api_asset(asset_path: str) -> bool:
    relative = (asset_path or "").strip("/")
    if _is_show_page_api_handler_path(asset_path):
        return True
    if relative == "__show/annotation.js":
        return False
    return relative == "__show" or relative.startswith("__show/")


def _is_show_annotation_asset(asset_path: str) -> bool:
    return (asset_path or "").strip("/") == "__show/annotation.js"


def _show_page_runtime_error_response(asset_path: str, exc: Exception):
    from core.show_runtime import ShowRuntimeRequestTimeoutError, ShowRuntimeUnavailableError

    if _is_show_page_api_handler_path(asset_path) and isinstance(
        exc,
        ShowRuntimeRequestTimeoutError,
    ):
        return _show_page_runtime_timeout_response()
    if not isinstance(exc, ShowRuntimeUnavailableError):
        raise AssertionError("Show Runtime error response requires owner-published evidence")
    return _show_page_runtime_unavailable_response(exc.reason)


def _is_show_page_entry_asset(asset_path: str) -> bool:
    relative = (asset_path or "").strip("/")
    return relative in {"", "index.html"}


def _is_show_page_spa_route_request(asset_path: str, starlette_request: FastAPIRequest) -> bool:
    if starlette_request.method not in {"GET", "HEAD"}:
        return False
    relative = _decode_show_page_asset_path(asset_path)
    if relative in {"", "index.html"}:
        return True
    accept = starlette_request.headers.get("accept", "")
    if "text/html" not in accept.lower():
        if not _show_page_accepts_markdown_value(accept):
            return False
        if _is_show_page_non_document_path(relative):
            return False
    segments = [segment for segment in relative.split("/") if segment]
    if not segments or segments[0] in {"api", "__show"}:
        return False
    return True


def _is_public_show_page_document_candidate(
    asset_path: str,
    starlette_request: FastAPIRequest,
) -> bool:
    if starlette_request.method not in {"GET", "HEAD"}:
        return False
    if not _is_show_page_entry_asset(asset_path) and _is_show_page_non_document_path(asset_path):
        return False
    relative = _decode_show_page_asset_path(asset_path)
    segments = [segment for segment in relative.split("/") if segment]
    return not segments or segments[0] not in {"api", "__show"}


def _is_public_show_page_markdown_request(
    asset_path: str,
    starlette_request: FastAPIRequest,
) -> bool:
    return (
        _is_public_show_page_document_candidate(asset_path, starlette_request)
        and _public_show_page_prefers_markdown(starlette_request)
    )


def _is_show_page_non_document_path(asset_path: str) -> bool:
    relative = _decode_show_page_asset_path(asset_path)
    segments = [segment for segment in relative.split("/") if segment]
    if not segments:
        return False
    if segments[0] in {
        "api",
        "assets",
        "src",
        "node_modules",
        "__show",
        "__events",
        "__vite_hmr",
        "@fs",
        "@id",
        "@vite",
        "@react-refresh",
    }:
        return True
    return Path(segments[-1]).suffix.lower() in _SHOW_PAGE_ASSET_SUFFIXES


def _is_show_page_markdown_request(
    asset_path: str,
    starlette_request: FastAPIRequest,
) -> bool:
    if not _show_page_accepts_markdown_value(starlette_request.headers.get("accept", "")):
        return False
    fetch_destination = starlette_request.headers.get("sec-fetch-dest", "").strip().lower()
    if fetch_destination and fetch_destination not in {"document", "empty", "iframe"}:
        return False
    if not _is_show_page_entry_asset(asset_path) and _is_show_page_non_document_path(asset_path):
        return False
    return _is_show_page_spa_route_request(asset_path, starlette_request)


def _is_private_show_page_markdown_request() -> bool:
    match = re.match(r"^/show/[^/]+(?:/(.*))?$", request.path or "")
    if match is None:
        return False
    return _is_show_page_markdown_request(
        match.group(1) or "",
        request._request,
    )


def _show_page_markdown_target_is_document(session_id: str, asset_path: str) -> bool:
    if _is_show_page_entry_asset(asset_path):
        return True
    return not _show_page_runtime_asset_exists(
        session_id,
        asset_path,
    ) or _show_page_runtime_document_exists(session_id, asset_path)


def _show_page_runtime_asset_exists(session_id: str, asset_path: str) -> bool:
    relative = _decode_show_page_asset_path(asset_path)
    if not relative:
        return False
    workspace = paths.get_show_page_dir(session_id)
    try:
        for candidate in (workspace / relative, workspace / "public" / relative):
            if candidate.is_file() or (candidate.is_dir() and (candidate / "index.html").is_file()):
                return True
    except OSError:
        return False
    return False


def _show_page_runtime_document_exists(session_id: str, asset_path: str) -> bool:
    relative = _decode_show_page_asset_path(asset_path)
    if not relative:
        return False
    workspace = paths.get_show_page_dir(session_id)
    try:
        for candidate in (workspace / relative, workspace / "public" / relative):
            if candidate.is_file() and candidate.suffix.lower() in {".htm", ".html"}:
                return True
            if candidate.is_dir() and (candidate / "index.html").is_file():
                return True
    except OSError:
        return False
    return False


def _decode_show_page_asset_path(asset_path: str) -> str:
    decoded = (asset_path or "").strip("/")
    for _ in range(3):
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    return decoded.replace("\\", "/")


def _is_show_page_dot_path(asset_path: str) -> bool:
    decoded = _decode_show_page_asset_path(asset_path)
    return any(segment.startswith(".") for segment in decoded.split("/") if segment)


def _is_show_runtime_sensitive_file_segment(segment: str) -> bool:
    lowered = segment.lower()
    return (
        lowered == ".git"
        or lowered == ".env"
        or lowered.startswith(".env.")
        or lowered.endswith((".pem", ".crt", ".key"))
    )


def _is_show_page_runtime_denied_path(
    asset_path: str,
    *,
    session_id: str,
    confine_to_workspace: bool = False,
) -> bool:
    decoded = _decode_show_page_asset_path(asset_path)
    segments = [segment for segment in decoded.split("/") if segment]
    if any(_is_show_runtime_sensitive_file_segment(segment) for segment in segments):
        return True
    if any(segment in {".", ".."} for segment in segments):
        return True
    # Vite `@fs/<abs>` paths can be non-dot (e.g. a workspace symlink `evil.txt`),
    # so classify them fully here rather than through the dot-segment fast path.
    if decoded.startswith("@fs/"):
        return _is_denied_show_page_at_fs_path(
            decoded,
            session_id=session_id,
            confine_to_workspace=confine_to_workspace,
        )
    dot_segments = [index for index, segment in enumerate(segments) if segment.startswith(".")]
    if not dot_segments:
        return False
    # A non-@fs dot path is private unless it is an optimized Vite dependency.
    vite_dependency = (
        (len(segments) >= 4 and segments[:3] == ["node_modules", ".vite", "deps"] and dot_segments == [1])
        or (len(segments) >= 3 and segments[:2] == [".vite", "deps"] and dot_segments == [0])
    )
    return not vite_dependency


def _is_denied_show_page_at_fs_path(decoded: str, *, session_id: str, confine_to_workspace: bool) -> bool:
    # Recover the absolute filesystem path from Vite's `/@fs/<abs>` convention,
    # mirroring Vite's own fsPathFromId. This route stripped the URL's single
    # leading slash, so a POSIX request arrives as `@fs/home/...` (restore the
    # slash); `@fs//home/...` (double slash) and `@fs/C:/...` (Windows drive)
    # already carry an absolute path, so leave those intact — prepending a slash
    # to `C:/...` would corrupt the Windows path. Parsing with `removeprefix("@fs/")`
    # alone dropped the single POSIX slash and mis-read a real request as relative,
    # denying legitimate external deps (notably the Vite HMR client's `env.mjs`) so
    # nothing mounted on the private `/show/` surface. Every genuine @fs URL is
    # absolute, so restore the slash unconditionally — a workspace path that merely
    # contains `vite-cache/deps` must still reach the workspace/escape checks below,
    # not slip through the relative allowance.
    fs_remainder = decoded.removeprefix("@fs/")
    if (
        fs_remainder
        and not fs_remainder.startswith("/")
        and not re.match(r"^[A-Za-z]:", fs_remainder)
    ):
        fs_remainder = "/" + fs_remainder
    # Collapse redundant leading POSIX slashes (e.g. an `@fs///<ws>/x` request
    # arrives here as `//<ws>/x`). pathlib keeps a `//` prefix in its parts and
    # os.path.normpath preserves it, so without this the workspace-prefix checks
    # below would miss both spellings while Vite still serves the collapsed path —
    # reopening the escape. A Windows drive/UNC path never starts with `/`.
    if fs_remainder.startswith("//"):
        fs_remainder = "/" + fs_remainder.lstrip("/")
    fs_path = Path(fs_remainder)
    if not fs_path.is_absolute():
        # A synthetic/relative @fs form (prewarming/tests). Allow only relocated
        # Vite cache deps; otherwise deny so a hidden path can't be proxied here.
        return not _is_relocated_vite_dep_path(decoded)

    workspace = paths.get_show_page_dir(session_id).resolve(strict=False)
    target = fs_path.resolve(strict=False)  # follows symlinks, like the runtime will
    try:
        workspace_relative = target.relative_to(workspace)
    except ValueError:
        # The resolved target is outside the workspace. Untrusted viewers must not
        # read through a workspace file that symlinks OUT of the workspace (a
        # symlink escape), so confine them to the workspace. That covers the PUBLIC
        # surface and any REMOTE viewer of the private `/show/` surface: remote
        # collaborators reach the page over the tunnel and must never be able to
        # read out-of-Project disk files through an authored symlink. Local Owner
        # authoring keeps the escape — an agent may legitimately symlink a disk file
        # into its own page — and a genuine dependency path (its parent is literally
        # outside the workspace) is still deferred to the Show Runtime's fs
        # allowlist on every surface.
        if confine_to_workspace:
            # Compare the requested path lexically (symlinks NOT followed here;
            # `..` was already rejected): a request ROOTED in the workspace whose
            # real target escapes it is a symlink escape — via a symlinked file OR
            # a symlinked directory in the path — so deny it. Match against the
            # workspace under both its resolved and unresolved spelling so a
            # symlinked AVIBE_HOME ancestor can't be used to dodge the prefix test.
            # A genuine dependency (rooted outside the workspace under either
            # spelling) still defers to the runtime.
            requested = Path(os.path.normpath(str(fs_path)))
            for ws_spelling in {workspace, paths.get_show_page_dir(session_id)}:
                try:
                    requested.relative_to(ws_spelling)
                    return True
                except ValueError:
                    continue
        # Outside the workspace: defer to the Show Runtime, which owns the
        # authoritative fs allowlist for its dependency/cache roots — the default
        # `~/.avibe/runtime`, a custom `VIBE_SHOW_RUNTIME_BIN` provider (e.g. an
        # nvm/global install), per-session extras — and still denies sensitive
        # filenames and non-allowlisted paths there. avibe cannot enumerate those
        # roots without breaking supported providers, so it enforces only what it
        # can decide authoritatively: the sensitive-filename check (caller) and the
        # workspace-internal dot-path check below.
        return False
    return any(part.startswith(".") for part in workspace_relative.parts)


def _show_page_runtime_failure_evidence(exc: "ShowRuntimeUnavailableError"):
    return (
        exc.reason,
        exc.failure_class,
        exc.recovery_action,
    )


def _log_show_runtime_unavailable(reason: str, *, public: bool, fallback: bool) -> None:
    if fallback:
        target = "fallback public Show Page response" if public else "fallback Show Page response"
    else:
        target = "static public Show Page" if public else "static Show Page"
    message = f"Show runtime unavailable (%s); serving {target}"
    logger.warning(message, reason, exc_info=True)


def _show_page_recovery_response(
    session_id: str,
    *,
    reason: str,
    failure_class,
    recovery_action,
    retry_authorized: bool,
):
    from core.show_pages import show_page_runtime_recovery_html

    response = Response(
        show_page_runtime_recovery_html(
            session_id,
            reason=reason,
            failure_class=failure_class,
            recovery_action=recovery_action,
            retry_authorized=retry_authorized,
            language=_request_ui_language(),
        ),
        status=200,
        mimetype="text/html; charset=utf-8",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Avibe-Show-Recovery"] = "1"
    response.headers["X-Avibe-Show-Recovery-Reason"] = reason
    response.headers["X-Avibe-Show-Recovery-Class"] = failure_class.value
    return response


def _show_page_file_response(root: Path, asset_path: str):
    relative = (asset_path or "").strip("/")
    if not relative:
        relative = "index.html"
    decoded = _decode_show_page_asset_path(relative)
    segments = [segment for segment in decoded.split("/") if segment]
    if _is_show_page_dot_path(decoded) or any(
        _is_show_runtime_sensitive_file_segment(segment) for segment in segments
    ):
        return _show_page_file_not_found_response()
    candidate = (root / decoded).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        return _show_page_file_not_found_response()
    if not candidate.exists() or not candidate.is_file():
        return _show_page_file_not_found_response()
    mime_type, _ = mimetypes.guess_type(str(candidate))
    response = send_file(candidate, mimetype=mime_type or "application/octet-stream")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _show_page_runtime_failure_response(
    page_dir: Path,
    session_id: str,
    asset_path: str,
    starlette_request: FastAPIRequest,
    *,
    reason: str,
    failure_class,
    recovery_action,
    retry_authorized: bool,
):
    if not _is_show_page_spa_route_request(asset_path, starlette_request):
        return None
    if not _is_show_page_entry_asset(asset_path):
        static_response = _show_page_file_response(page_dir, asset_path)
        if static_response.status_code != 404:
            return static_response
    return _show_page_recovery_response(
        session_id,
        reason=reason,
        failure_class=failure_class,
        recovery_action=recovery_action,
        retry_authorized=retry_authorized,
    )


def _show_session_event_error_response(exc: Exception):
    code = getattr(exc, "code", "show_session_event_failed")
    status = 404 if code == "session_not_found" else 409 if code == "event_id_conflict" else 400
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


def _public_show_event_write_authorized(share_id: str, session_id: str) -> bool:
    token = request.headers.get(SHOW_EVENT_WRITE_TOKEN_HEADER)
    if not token:
        return False
    try:
        expected = show_public_event_write_token(share_id, session_id)
    except Exception:
        return False
    return hmac.compare_digest(token, expected)


def _public_show_referer_matches(share_id: str) -> bool:
    referer = request.headers.get("Referer")
    if not referer:
        return False
    expected_path = f"/p/{quote(share_id, safe='')}/"
    return urlsplit(referer).path.startswith(expected_path)


def _sanitize_public_show_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    for key in ("id", "sessionId", "session_id"):
        sanitized.pop(key, None)
    for key in ("payload", "annotation", "mark"):
        nested = sanitized.get(key)
        if isinstance(nested, dict):
            sanitized[key] = {
                nested_key: value
                for nested_key, value in nested.items()
                if nested_key not in {"sessionId", "session_id"}
            }
    return sanitized


def _show_annotation_capability(
    *,
    author: dict[str, str] | None,
    page: ShowPage,
    public_share_id: str | None = None,
) -> bool:
    """Return whether this request may write and dispatch Show annotations.

    The current device-side authorization boundary is the validated Workbench
    session, represented by ``author``. Page
    visibility and the share/session binding remain independent structural
    checks so a future ACL can extend the author decision without changing the
    event pipeline.
    """
    if author is None or page.visibility == VISIBILITY_OFFLINE:
        return False
    if public_share_id is not None:
        return page.visibility == VISIBILITY_PUBLIC and page.share_id == public_share_id
    return page.visibility in {
        VISIBILITY_PRIVATE,
        VISIBILITY_LIMITED,
        VISIBILITY_PUBLIC,
    }


def _show_public_editor_context():
    from vibe.authorization import context_from_session_payload, instance_owner_context

    config = _load_remote_access_config()
    if config is not None:
        session = _resolved_remote_session_payload(config)
        context = context_from_session_payload(session) if session is not None else None
        if context is not None:
            return context if context.has_role("editor") else None
        if config.remote_access.vibe_cloud.enabled:
            return None
    if not (_is_local_request(config) or _is_loopback_origin_proxy_request()):
        return None
    return instance_owner_context()


def _show_public_authenticated_context(config: V2Config | None):
    from vibe.authorization import context_from_session_payload

    session = _resolved_remote_session_payload(config) if config is not None else None
    return context_from_session_payload(session) if session is not None else None


def _show_access_visitor_from_context(context: Any):
    from core.show_pages import ShowAccessVisitor, normalize_show_access_email

    if context is None:
        return None
    normalized_email = ""
    if context.email:
        try:
            normalized_email = normalize_show_access_email(context.email)
        except (TypeError, ValueError):
            normalized_email = ""
    return ShowAccessVisitor(
        normalized_email=normalized_email,
        organization_id=context.organization_id,
        organization_member_id=context.organization_member_id,
        organization_role=context.organization_role,
        group_ids=frozenset(context.group_ids or ()),
    )


def _limited_show_access_grant(access: Any, visitor: Any):
    from core.show_pages import limited_show_access_grant

    if access is None or visitor is None:
        return None
    return limited_show_access_grant(access, visitor)


def _limited_show_access_admits(access: Any, visitor: Any) -> bool:
    return _limited_show_access_grant(access, visitor) is not None


def _limited_show_access_grant_is_current(access: Any, grant: Any) -> bool:
    from core.show_pages import limited_show_access_grant_is_current

    return access is not None and limited_show_access_grant_is_current(access, grant)


def _show_limited_viewer_is_allowed(
    context: Any,
    access: Any,
) -> bool:
    allowlisted = _limited_show_access_admits(
        access, _show_access_visitor_from_context(context)
    )
    return allowlisted


async def _show_public_request_author() -> dict[str, str] | None:
    context = await asyncio.to_thread(_show_public_editor_context)
    if context is None:
        return None
    if context.is_remote:
        return {"kind": "user", "email": context.email} if context.email else None
    return {"kind": "local"}


def _show_request_author() -> dict[str, str] | None:
    from vibe.authorization import context_from_session_payload

    context = getattr(g, "authorization_context", None)
    if context is not None:
        if not context.has_role("editor"):
            return None
        if context.is_remote:
            return {"kind": "user", "email": context.email} if context.email else None
        return {"kind": "local"}

    config = _load_remote_access_config()
    if config is not None:
        session = _resolved_remote_session_payload(config)
        context = (
            context_from_session_payload(session)
            if session is not None
            else None
        )
        if context is not None:
            email = str(session.get("email", "")).strip()
            if email and context.has_role("editor"):
                return {"kind": "user", "email": email}
            return None

    return {"kind": "local"}


def _show_me_response(
    author: dict[str, str] | None,
    *,
    can_annotate: bool | None = None,
    write_token: str | None = None,
):
    authenticated = author is not None
    capability = authenticated if can_annotate is None else bool(can_annotate)
    payload = {"authenticated": authenticated, "canAnnotate": capability}
    if capability and write_token:
        payload["writeToken"] = write_token
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"
    return response


async def _show_event_response_from_payload(
    session_id: str,
    payload: dict[str, Any],
    *,
    author: dict[str, str] | None = None,
    public: bool = False,
    public_share_id: str | None = None,
    allow_dispatch: bool = True,
):
    context = getattr(g, "authorization_context", None)
    is_remote_caller = context is not None and context.is_remote
    if is_remote_caller or public:
        # A remote caller — whether an authenticated editor on the HTML route or
        # a public share visitor — may only author human input: a typed intent or
        # an annotation lifecycle event, plus resolving a mark the Agent drew.
        # Every other supported type (`assistant.mark.*`, `system.*`,
        # `assistant.page.*`) is Agent/system provenance: `ShowSessionEventStore`
        # derives the actor from the type and would persist it as `author="agent"`,
        # so accepting one from across the tunnel lets a collaborator forge Agent
        # or system activity and corrupt the shared transcript. This is the same
        # human-event / mark-resolution allowlist the public route enforces; the
        # local CLI callers keep the full supported set.
        event_type = str(payload.get("type") or "").strip()
        if event_type not in HUMAN_EVENT_TYPES and event_type != "assistant.mark.resolved":
            return _unsupported_show_event_type_response()
    remote = not public and _is_remote_show_page_request()
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
        event_payload = store.append(
            session_id,
            payload,
            author=author,
            reserve_dispatch=allow_dispatch,
        )
    except Exception as exc:
        return _show_session_event_error_response(exc)
    finally:
        store.close()

    _publish_show_session_event(event_payload)
    if allow_dispatch and show_event_requests_dispatch(event_payload):
        # The internal endpoint returns after SessionTurnManager has either
        # started or queued the turn. Settle the pending transcript row before
        # acknowledging the Show event so a successful POST cannot strand it.
        dispatch_outcome = await _run_show_event_dispatch(event_payload)
        if dispatch_outcome is _ShowEventDispatchOutcome.IN_FLIGHT:
            return (
                jsonify(
                    {
                        "ok": True,
                        "dispatch_pending": True,
                        "event": _show_event_response_payload(
                            event_payload,
                            public=public,
                            public_share_id=public_share_id,
                            remote=remote,
                        ),
                    }
                ),
                202,
            )
        if dispatch_outcome is _ShowEventDispatchOutcome.FAILED:
            exc = _show_event_dispatch_error()
            return (
                jsonify(
                    {
                        "ok": False,
                        "code": exc.code,
                        "error": str(exc),
                        "event": _show_event_response_payload(
                            event_payload,
                            public=public,
                            public_share_id=public_share_id,
                            remote=remote,
                        ),
                    }
                ),
                502,
            )
    return (
        jsonify(
            {
                "ok": True,
                "event": _show_event_response_payload(
                    event_payload,
                    public=public,
                    public_share_id=public_share_id,
                    remote=remote,
                ),
            }
        ),
        201,
    )


def record_local_show_event(
    session_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    store = _show_session_event_store()
    try:
        event_payload = store.append(session_id, payload, reserve_dispatch=True)
    finally:
        store.close()
    _publish_show_session_event(event_payload)
    if show_event_requests_dispatch(event_payload):
        # The internal endpoint returns after the manager accepts or queues the
        # turn, so local CLI callers can settle the reservation synchronously
        # without waiting for the agent turn itself.
        dispatch_outcome = asyncio.run(_run_show_event_dispatch(event_payload))
        if dispatch_outcome is _ShowEventDispatchOutcome.IN_FLIGHT:
            raise _show_event_dispatch_pending_error()
        if dispatch_outcome is _ShowEventDispatchOutcome.FAILED:
            raise _show_event_dispatch_error()
    return event_payload


def _publish_show_session_event(event_payload: dict[str, Any]) -> None:
    from storage import messages_service
    from vibe.sse_broker import broker

    broker.publish("show.event", event_payload)
    message = event_payload.get("message")
    if show_event_requests_dispatch(event_payload):
        return
    if (
        isinstance(message, dict)
        and message.get("type") in messages_service.TRANSCRIPT_TYPES
    ):
        broker.publish("message.new", message)
    broker.publish(
        "session.activity",
        {
            "session_id": event_payload.get("session_id"),
            "scope_id": event_payload.get("scope_id"),
            "event": "show_event",
        },
    )


async def _run_show_event_dispatch(
    event_payload: dict[str, Any],
) -> _ShowEventDispatchOutcome:
    from vibe import internal_client

    session_id = event_payload.get("session_id")
    scope_id = event_payload.get("scope_id")
    event_id = event_payload.get("id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(event_id, str)
        or not event_id
    ):
        return _ShowEventDispatchOutcome.FAILED

    delivery = event_payload.get("delivery")
    if not isinstance(delivery, dict):
        return _ShowEventDispatchOutcome.FAILED
    delivery_state = str(delivery.get("state") or "")
    if delivery_state in ADMITTED_DELIVERY_STATES:
        return _ShowEventDispatchOutcome.ACCEPTED
    if delivery_state != "reserved":
        return _ShowEventDispatchOutcome.FAILED

    dispatch_text = _show_event_dispatch_text(event_payload)
    if not dispatch_text.strip():
        _retire_show_event_reservation(event_payload, reason="empty_dispatch")
        return _ShowEventDispatchOutcome.FAILED

    dispatch_payload = {
        "session_id": session_id,
        "text": dispatch_text,
        "scope_id": scope_id,
        "user_message_id": delivery["id"],
        "display_text": delivery.get("text") or "",
        "content": delivery.get("content") or {},
        "metadata": delivery.get("metadata") or {},
        "show_event_id": event_id,
        "files": [],
    }

    try:
        result = await internal_client.dispatch_async(
            dispatch_payload,
            timeout=None,
        )
    # Only acceptance makes the reservation transcript-visible. A timed-out CLI
    # uses that transition as its retry/dedupe signal.
    except internal_client.InternalServerTimeout as exc:
        logger.warning(
            "show event dispatch still pending for session %s: %s",
            session_id,
            exc,
        )
        return _ShowEventDispatchOutcome.IN_FLIGHT
    except internal_client.InternalServerUnavailable as exc:
        logger.warning(
            "show event dispatch unavailable for session %s: %s",
            session_id,
            exc,
        )
        _retire_show_event_reservation(event_payload, reason="dispatch_unavailable")
        return _ShowEventDispatchOutcome.FAILED
    except Exception:  # pragma: no cover - defensive
        logger.exception("show event dispatch acceptance is unknown")
        return _ShowEventDispatchOutcome.IN_FLIGHT

    status = result.get("status_code", 500)
    body = result.get("body") or {}
    if status != 202:
        logger.warning(
            "show event dispatch failed for session %s: status=%s body=%s",
            session_id,
            status,
            body,
        )
        _retire_show_event_reservation(
            event_payload,
            reason=f"dispatch_rejected_{status}",
        )
        return _ShowEventDispatchOutcome.FAILED
    settled = _settle_show_event_message(event_payload)
    state = str((settled or {}).get("state") or body.get("delivery_state") or "")
    if state in ADMITTED_DELIVERY_STATES:
        return _ShowEventDispatchOutcome.ACCEPTED
    return _ShowEventDispatchOutcome.IN_FLIGHT


def _retire_show_event_reservation(
    event_payload: dict[str, Any],
    *,
    reason: str,
) -> bool:
    from storage import message_deliveries

    session_id = str(event_payload.get("session_id") or "").strip()
    delivery = event_payload.get("delivery")
    delivery_id = str(delivery.get("id") or "").strip() if isinstance(delivery, dict) else ""
    referenced_delivery_id = str(event_payload.get("delivery_id") or "").strip()
    if (
        not session_id
        or not delivery_id
        or referenced_delivery_id != delivery_id
    ):
        return False
    engine = _projects_engine()
    try:
        with engine.begin() as conn:
            retired = message_deliveries.retire_reserved(
                conn,
                session_id,
                delivery_id,
                reason=reason,
            )
    finally:
        engine.dispose()
    if retired:
        _settle_show_event_message(event_payload)
    return retired


def _settle_show_event_message(
    event_payload: dict[str, Any],
) -> dict[str, Any] | None:
    from core.show_session_events import ShowSessionEventStore

    session_id = event_payload.get("session_id")
    event_id = event_payload.get("id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(event_id, str) or not event_id:
        return None

    store = ShowSessionEventStore()
    try:
        settled_event = store.get_event(session_id, event_id)
    finally:
        store.close()
    if settled_event is None:
        return None
    event_payload.update(settled_event)
    delivery = settled_event.get("delivery")
    if isinstance(delivery, dict) and delivery.get("state") == "queued":
        from vibe.sse_broker import broker

        broker.publish(
            "queue.updated",
            {
                "session_id": session_id,
                "scope_id": event_payload.get("scope_id"),
            },
        )
    return delivery if isinstance(delivery, dict) else None


def _show_event_dispatch_error() -> ShowSessionEventError:
    return localized_show_event_error("show_event_dispatch_failed")


def _show_event_dispatch_pending_error() -> ShowSessionEventError:
    return localized_show_event_error("show_event_dispatch_pending")


def _unsupported_show_event_type_response():
    error = localized_show_event_error("unsupported_event_type")
    return _coded_error_response(
        error.code,
        str(error),
        400,
    )


def _load_session_message(session_id: str, message_id: str) -> dict[str, Any] | None:
    from storage import messages_service

    with _projects_engine().connect() as conn:
        window = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            around_id=message_id,
            limit=1,
        )
    return next(
        (item for item in window["messages"] if item.get("id") == message_id),
        None,
    )


def _show_event_dispatch_text(event_payload: dict[str, Any]) -> str:
    delivery = event_payload.get("delivery")
    if isinstance(delivery, dict):
        return str(delivery.get("dispatch_text") or "").strip()
    return _legacy_show_event_dispatch_text(event_payload)


def _legacy_show_event_dispatch_text(event_payload: dict[str, Any]) -> str:
    transcript_text = str(event_payload.get("transcript_text") or "").strip()
    if event_payload.get("type") != "human.annotation.created":
        return transcript_text

    event_id = str(event_payload.get("id") or "").strip()
    if not event_id:
        return transcript_text
    lines = [transcript_text, "", f"Show event id: {event_id}"]
    payload = event_payload.get("payload")
    intent = "comment"
    if isinstance(payload, dict):
        intent = str(payload.get("intent") or "").strip() or "comment"
    if intent in {"question", "comment"}:
        lines.extend(
            [
                "",
                "如需在页面上原位回应，可执行：",
                f"  vibe show reply {event_id} --message '<你的回答>'",
                "（也可以直接修改页面内容来响应，按场景选择。）",
            ]
        )
    return "\n".join(lines)


def _remote_safe_show_event_payload(event_payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the absolute host screenshot path from one Show event.

    ``ShowSessionEventStore`` records ``payload.screenshot.path`` as a local
    filesystem path so local tooling can read the materialized image. A remote
    subscriber reads the same bytes through its attachment id, so the path is
    useless to it and only discloses the host's directory layout — the public
    Show projection strips it for exactly that reason, and any authorized remote
    reader (workbench SSE, private Show page events, its own POST echo) must get
    the same treatment.
    """

    payload = event_payload.get("payload")
    if not isinstance(payload, dict):
        return event_payload
    screenshot = payload.get("screenshot")
    if not isinstance(screenshot, dict) or "path" not in screenshot:
        return event_payload
    local_path = screenshot.get("path")
    safe_screenshot = {key: value for key, value in screenshot.items() if key != "path"}
    safe_event = {**event_payload, "payload": {**payload, "screenshot": safe_screenshot}}
    transcript_text = safe_event.get("transcript_text")
    if isinstance(local_path, str) and local_path and isinstance(transcript_text, str):
        safe_reference = str(safe_screenshot.get("attachmentId") or "screenshot attachment")
        safe_event["transcript_text"] = transcript_text.replace(local_path, safe_reference)
    return safe_event


def _show_event_response_payload(
    event_payload: dict[str, Any],
    *,
    public: bool = False,
    public_share_id: str | None = None,
    remote: bool = False,
) -> dict[str, Any]:
    if not public:
        return _remote_safe_show_event_payload(event_payload) if remote else event_payload
    public_event = {
        key: value
        for key, value in event_payload.items()
        if key
        not in {
            "session_id",
            "scope_id",
            "message_id",
            "message",
            "delivery_id",
            "delivery",
        }
    }
    payload = public_event.get("payload")
    if isinstance(payload, dict):
        public_payload = dict(payload)
        author = public_payload.get("author")
        if isinstance(author, dict) and "email" in author:
            public_payload["author"] = {key: value for key, value in author.items() if key != "email"}
        screenshot = public_payload.get("screenshot")
        if isinstance(screenshot, dict):
            local_path = screenshot.get("path")
            public_screenshot = {key: value for key, value in screenshot.items() if key != "path"}
            attachment_id = public_screenshot.get("attachmentId")
            if (
                public_share_id
                and isinstance(local_path, str)
                and local_path
                and isinstance(attachment_id, str)
                and attachment_id
            ):
                public_screenshot["url"] = (
                    f"/p/{quote(public_share_id, safe='')}/__show/media/{quote(attachment_id, safe='')}"
                )
            public_payload["screenshot"] = public_screenshot
            transcript_text = public_event.get("transcript_text")
            if isinstance(local_path, str) and local_path and isinstance(transcript_text, str):
                public_ref = str(public_screenshot.get("attachmentId") or "screenshot attachment")
                public_event["transcript_text"] = transcript_text.replace(local_path, public_ref)
        public_event["payload"] = public_payload
    return public_event


def _show_dispatch_response_payload(
    event_payload: dict[str, Any],
    *,
    public: bool = False,
) -> dict[str, Any]:
    if not public:
        return event_payload
    return {
        key: _redact_public_dispatch_value(value)
        for key, value in event_payload.items()
        if key
        not in {
            "session_id",
            "scope_id",
            "message_id",
            "message",
            "user_message_id",
        }
    }


def _redact_public_dispatch_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_public_dispatch_value(nested)
            for key, nested in value.items()
            if key
            not in {
                "session_id",
                "scope_id",
                "message_id",
                "message",
                "user_message_id",
            }
        }
    if isinstance(value, list):
        return [_redact_public_dispatch_value(item) for item in value]
    return value


def _show_events_list_payload(
    payload: dict[str, Any],
    *,
    public: bool = False,
    public_share_id: str | None = None,
    remote: bool = False,
) -> dict[str, Any]:
    if not public and not remote:
        return payload
    return {
        **payload,
        "events": [
            _show_event_response_payload(
                event_payload,
                public=public,
                public_share_id=public_share_id,
                remote=remote,
            )
            for event_payload in payload.get("events", [])
            if isinstance(event_payload, dict)
        ],
    }


async def _show_events_stream(
    session_id: str,
    *,
    after_id: str | None = None,
    public: bool = False,
    public_share_id: str | None = None,
    remote: bool = False,
    authorization_refresh_at: float | None = None,
    authorization_context: Any = None,
    remote_session_identity: Mapping[str, Any] | None = None,
    remote_session_payload: Mapping[str, Any] | None = None,
    remote_session_cookie: str | None = None,
    remote_request_host: str | None = None,
    remote_config: V2Config | None = None,
):
    import asyncio

    from fastapi.responses import StreamingResponse

    from vibe.sse_broker import broker

    def _event_visible(event_payload: dict[str, Any]) -> bool:
        return event_payload.get("session_id") == session_id

    async def _authorization_state() -> str:
        if remote_session_payload is None:
            return "current"
        identity = remote_session_identity or remote_session_payload
        if remote_config is None:
            return "invalid_identity"
        return await _remote_stream_authorization_state(
            remote_config,
            identity,
            remote_session_payload,
            session_cookie=remote_session_cookie,
            request_host=remote_request_host,
        )

    async def generate():
        sub_id, queue = broker.subscribe()
        replayed_ids: set[str] = set()
        try:
            store = _show_session_event_store()
            try:
                cursor = after_id
                state = await _authorization_state()
                if state != "current":
                    yield _remote_authorization_sse_frame(state)
                    return
                yield ": show events connected\n\n"
                if not public and not _show_page_resource_access_allowed(
                    authorization_context,
                    session_id,
                ):
                    if remote_session_payload is not None:
                        yield _remote_authorization_sse_frame("revoked")
                    return
                while True:
                    state = await _authorization_state()
                    if state != "current":
                        yield _remote_authorization_sse_frame(state)
                        return
                    batch = store.list(session_id, after_id=cursor, limit=500)
                    events = batch["events"]
                    if not events:
                        break
                    for event_payload in events:
                        if isinstance(event_payload.get("id"), str):
                            replayed_ids.add(event_payload["id"])
                        yield _sse_frame(
                            "show.event",
                            _show_event_response_payload(
                                event_payload,
                                public=public,
                                public_share_id=public_share_id,
                                remote=remote,
                            ),
                        )
                    cursor = batch.get("next_after_id")
                    if not cursor:
                        break
            finally:
                store.close()

            while True:
                state = await _authorization_state()
                if state != "current":
                    yield _remote_authorization_sse_frame(state)
                    return
                try:
                    event_type, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    state = await _authorization_state()
                    if state != "current":
                        yield _remote_authorization_sse_frame(state)
                        return
                    decoded = json.loads(payload)
                    event_payload = decoded.get("data") if isinstance(decoded, dict) else None
                    if event_type == "authorization.changed" and not public:
                        if not _show_page_resource_access_allowed(
                            authorization_context,
                            session_id,
                        ):
                            if remote_session_payload is not None:
                                yield _remote_authorization_sse_frame("revoked")
                            return
                    elif event_type == "show.event" and isinstance(event_payload, dict) and _event_visible(event_payload):
                        event_id = event_payload.get("id")
                        if isinstance(event_id, str) and event_id in replayed_ids:
                            continue
                        if isinstance(event_id, str):
                            replayed_ids.add(event_id)
                        yield _sse_frame(
                            "show.event",
                            _show_event_response_payload(
                                event_payload,
                                public=public,
                                public_share_id=public_share_id,
                                remote=remote,
                            ),
                        )
                    elif (
                        event_type == "show.dispatch"
                        and isinstance(event_payload, dict)
                        and _event_visible(event_payload)
                    ):
                        yield _sse_frame(
                            "show.dispatch",
                            _show_dispatch_response_payload(
                                event_payload,
                                public=public,
                            ),
                        )
                except asyncio.TimeoutError:
                    state = await _authorization_state()
                    if state != "current":
                        yield _remote_authorization_sse_frame(state)
                        return
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


async def _show_events_response(
    session_id: str,
    *,
    public: bool = False,
    public_share_id: str | None = None,
):
    # Resolve the projection before the SSE generator loses request context.
    authorization_context = None if public else getattr(g, "authorization_context", None)
    remote = not public and _is_remote_show_page_request()
    remote_session_payload = None if public else getattr(g, "remote_session_payload", None)
    remote_session_cookie = None
    remote_request_host = None
    if remote_session_payload is not None:
        from vibe import remote_access

        remote_session_cookie = request.cookies.get(remote_access.SESSION_COOKIE_NAME)
        remote_request_host = _effective_normalized_host()
    if request.method == "GET":
        if request.args.get("stream") == "1":
            return await _show_events_stream(
                session_id,
                after_id=request.args.get("after_id") or _last_event_id_from_request(),
                public=public,
                public_share_id=public_share_id,
                remote=remote,
                authorization_context=authorization_context,
                remote_session_identity=(
                    None if public else getattr(g, "remote_session_identity", None)
                ),
                remote_session_payload=remote_session_payload,
                remote_session_cookie=remote_session_cookie,
                remote_request_host=remote_request_host,
                remote_config=(
                    None
                    if remote_session_payload is None
                    else _load_remote_access_config()
                ),
            )
        store = _show_session_event_store()
        try:
            try:
                limit = int(request.args.get("limit") or 100)
            except (TypeError, ValueError):
                limit = 100
            payload = store.list(session_id, after_id=request.args.get("after_id") or None, limit=limit)
            return jsonify(
                _show_events_list_payload(
                    payload,
                    public=public,
                    public_share_id=public_share_id,
                    remote=remote,
                )
            )
        finally:
            store.close()

    if request.method != "POST":
        return jsonify({"ok": False, "code": "method_not_allowed"}), 405
    if not _show_event_write_authorized(session_id):
        return jsonify({"ok": False, "code": "show_event_write_forbidden"}), 403

    return await _show_event_response_from_payload(
        session_id,
        _show_events_payload_from_request(),
        author=_show_request_author(),
    )


@app.route("/api/show/sessions/<session_id>/events", methods=["POST"])
async def show_session_events_create(session_id: str):
    if not _is_cli_show_event_request():
        return jsonify({"ok": False, "code": "forbidden"}), 403
    return await _show_event_response_from_payload(session_id, _show_events_payload_from_request())


@app.route("/api/show/sessions/<session_id>/prewarm", methods=["POST"])
async def show_session_prewarm(session_id: str):
    if not _is_cli_show_event_request():
        return jsonify({"ok": False, "code": "forbidden"}), 403
    payload = _show_events_payload_from_request()
    from core.show_runtime import ShowRuntimeContext, prewarm_show_page_session

    try:
        context = ShowRuntimeContext(payload.get("context"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "code": "invalid_show_runtime_context"}), 400

    result = await prewarm_show_page_session(session_id, context=context)
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
    from core.show_runtime import ShowRuntimeUnavailableError, get_show_runtime_manager

    forwarded_headers = _show_runtime_forwarded_headers(request._request.headers)
    try:
        proxied = await get_show_runtime_manager().request_global(
            request.method,
            runtime_path,
            headers=forwarded_headers,
            body=None,
        )
    except ShowRuntimeUnavailableError as exc:
        return _show_page_runtime_unavailable_response(exc.reason)
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
    content = _compress_response_content(proxied.content, response_headers, request._request)
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


_SHOW_PAGE_MARKDOWN_ERROR_I18N_KEYS = {
    "authentication_required": "show.markdown.errors.authenticationRequired",
    "forbidden": "show.markdown.errors.forbidden",
    "page_offline": "show.markdown.errors.pageOffline",
    "renderer_unavailable": "show.markdown.errors.rendererUnavailable",
    "render_timeout": "show.markdown.errors.renderTimeout",
    "render_failed": "show.markdown.errors.renderFailed",
    "session_unknown": "show.markdown.errors.sessionUnknown",
}


def _show_page_markdown_error_response(
    code: str,
    status_code: int,
    message: str | None = None,
):
    if message is None:
        key = _SHOW_PAGE_MARKDOWN_ERROR_I18N_KEYS.get(code, "show.markdown.errors.renderFailed")
        message = t(key, _request_ui_language())
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = _append_vary_header(response.headers.get("Vary"), "Accept")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _show_page_markdown_runtime_error_response(proxied: Any):
    expected_codes = {
        400: {"invalid_target"},
        404: {"session_unknown"},
        502: {"render_failed", "output_too_large"},
        503: {"renderer_unavailable"},
        504: {"render_timeout"},
    }
    try:
        payload = proxied.json()
    except (UnicodeDecodeError, ValueError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if (
        proxied.status_code in expected_codes
        and isinstance(code, str)
        and code in expected_codes[proxied.status_code]
        and isinstance(message, str)
        and message
    ):
        return _show_page_markdown_error_response(code, proxied.status_code, message)
    if proxied.status_code == 404:
        return _show_page_markdown_error_response("renderer_unavailable", 503)
    fallback_codes = {
        502: "render_failed",
        503: "renderer_unavailable",
        504: "render_timeout",
    }
    fallback_code = fallback_codes.get(proxied.status_code, "render_failed")
    fallback_status = proxied.status_code if proxied.status_code in fallback_codes else 502
    return _show_page_markdown_error_response(fallback_code, fallback_status)


def _show_page_markdown_render_target(
    asset_path: str,
    starlette_request: FastAPIRequest,
) -> str:
    relative = _decode_show_page_asset_path(asset_path)
    target = f"/{quote(relative, safe='/@:-._~')}" if relative else "/"
    if asset_path and asset_path.endswith("/") and target != "/":
        target = f"{target}/"
    if starlette_request.url.query:
        target = f"{target}?{starlette_request.url.query}"
    return target


async def _show_page_markdown_runtime_response(
    session_id: str,
    asset_path: str,
    starlette_request: FastAPIRequest,
    *,
    external_prefix: str | None = None,
    runtime_retry_authorized: bool = False,
):
    from core.show_runtime import (
        SHOW_RUNTIME_REQUEST_TIMEOUT_SECONDS,
        ShowRuntimeContext,
        ShowRuntimeProtocolEnvelope,
        ShowRuntimeRequestTimeoutError,
        get_show_runtime_manager,
    )
    from httpx import ReadTimeout

    manager = get_show_runtime_manager()
    automatic = not (
        runtime_retry_authorized
        and starlette_request.headers.get("X-Avibe-Show-Recovery-Retry") == "1"
    )
    try:
        if not await manager.supports_render_markdown(automatic=automatic):
            return _show_page_markdown_error_response("renderer_unavailable", 503)
    except Exception:
        logger.debug("Show Runtime Markdown capability probe unavailable", exc_info=True)
        return _show_page_markdown_error_response("renderer_unavailable", 503)

    session_part = quote(session_id, safe="")
    runtime_path = f"/sessions/{session_part}/render-markdown"
    base_path = (
        f"{external_prefix.rstrip('/')}/"
        if external_prefix
        else f"/show/{session_part}/"
    )
    context = ShowRuntimeContext.SHARED if external_prefix else ShowRuntimeContext.PRIVATE
    envelope = ShowRuntimeProtocolEnvelope(context)
    render_target = _show_page_markdown_render_target(asset_path, starlette_request)
    forwarded_headers = _show_runtime_forwarded_headers(starlette_request.headers)
    try:
        proxied = await manager.request(
            "GET",
            runtime_path,
            envelope=envelope,
            headers=forwarded_headers,
            body=None,
            base_path=base_path,
            render_target=render_target,
            timeout_seconds=SHOW_RUNTIME_REQUEST_TIMEOUT_SECONDS,
            automatic=automatic,
        )
    except (ReadTimeout, ShowRuntimeRequestTimeoutError):
        logger.debug("Show Runtime Markdown request timed out", exc_info=True)
        return _show_page_markdown_error_response("render_timeout", 504)
    except Exception:
        logger.debug("Show Runtime Markdown request unavailable", exc_info=True)
        return _show_page_markdown_error_response("renderer_unavailable", 503)

    if proxied.status_code != 200:
        return _show_page_markdown_runtime_error_response(proxied)

    response_headers = {
        key: value
        for key, value in proxied.headers.items()
        if key.lower() in _SHOW_RUNTIME_RESPONSE_HEADER_ALLOWLIST
    }
    content_type = _response_header(response_headers, "content-type") or ""
    if content_type.split(";", 1)[0].strip().lower() != "text/markdown":
        return _show_page_markdown_error_response("render_failed", 502)
    if "charset=" not in content_type.lower():
        _set_response_header(response_headers, "Content-Type", "text/markdown; charset=utf-8")
    _mark_show_runtime_document_no_store(response_headers)
    _set_response_header(
        response_headers,
        "Vary",
        _append_vary_header(_response_header(response_headers, "vary"), "Accept"),
    )
    response_headers["X-Content-Type-Options"] = "nosniff"
    response_headers["Referrer-Policy"] = "no-referrer"
    content = _compress_response_content(proxied.content, response_headers, starlette_request)
    if starlette_request.method == "HEAD":
        content = b""
    return FastAPIResponse(content=content, status_code=200, headers=response_headers)


async def _show_page_runtime_response(
    session_id: str,
    asset_path: str,
    starlette_request: FastAPIRequest,
    *,
    external_prefix: str | None = None,
    inject_show_config: bool = False,
    show_authenticated: bool = False,
    runtime_retry_authorized: bool = False,
    show_config_session_id: str | None = None,
    include_annotation_bootstrap: bool = True,
):
    from core.show_runtime import (
        ShowRuntimeContext,
        ShowRuntimeProtocolEnvelope,
        get_show_runtime_manager,
    )

    session_part = quote(session_id, safe="")

    def runtime_app_path(relative_asset_path: str) -> str:
        asset_part = quote(relative_asset_path.lstrip("/"), safe="/@:-._~")
        path = f"/sessions/{session_part}/app/"
        if asset_part:
            path = f"{path}{asset_part}"
        if starlette_request.url.query:
            path = f"{path}?{starlette_request.url.query}"
        return path

    forwarded_headers = _show_runtime_forwarded_headers(starlette_request.headers)
    history_route_candidate = (
        not _is_show_page_entry_asset(asset_path)
        and _is_show_page_spa_route_request(asset_path, starlette_request)
    )
    served_entry_fallback = history_route_candidate and not _show_page_runtime_asset_exists(
        session_id,
        asset_path,
    )
    runtime_path = runtime_app_path("" if served_entry_fallback else asset_path)
    context = ShowRuntimeContext.SHARED if external_prefix else ShowRuntimeContext.PRIVATE
    envelope = ShowRuntimeProtocolEnvelope(context)
    body = await starlette_request.body()
    request_started = time.monotonic()
    manager = get_show_runtime_manager()
    request_options: dict[str, float] = {}
    if _is_show_page_api_handler_path(asset_path):
        from core.services import settings as settings_service

        config = await asyncio.to_thread(settings_service.load_config_or_default)
        request_options["timeout_seconds"] = config.runtime.show_page_api_timeout_seconds
    proxied = await manager.request(
        starlette_request.method,
        runtime_path,
        envelope=envelope,
        headers=forwarded_headers,
        body=body or None,
        automatic=not (
            runtime_retry_authorized
            and starlette_request.headers.get("X-Avibe-Show-Recovery-Retry") == "1"
        ),
        **request_options,
    )
    proxy_duration_ms = int((time.monotonic() - request_started) * 1000)
    if (
        proxy_duration_ms >= SHOW_RUNTIME_SLOW_REQUEST_MS
        or _is_show_page_entry_asset(asset_path)
        or served_entry_fallback
    ):
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
    _rewrite_show_runtime_url_headers(
        response_headers,
        session_id=session_id,
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
    if _should_inject_show_runtime_config(
        proxied.status_code,
        response_headers,
        inject_show_config=inject_show_config,
    ):
        base_path = f"{external_prefix.rstrip('/')}/" if external_prefix else f"/show/{quote(session_id, safe='')}/"
        content = _inject_show_runtime_config(
            content,
            show_config_session_id or session_id,
            base_path=base_path,
            authenticated=show_authenticated,
            include_write_token=external_prefix is None and show_authenticated,
            include_annotation_bootstrap=include_annotation_bootstrap,
        )
        if external_prefix:
            response_headers["Referrer-Policy"] = "same-origin"
        _mark_show_runtime_document_no_store(response_headers)
    elif (_is_show_page_entry_asset(asset_path) or served_entry_fallback) and 200 <= proxied.status_code < 300:
        # The entry document is per-session/per-share dynamic (it embeds the import map
        # and base path); never let it be cached. App modules and per-session deps keep
        # the runtime's own cache headers (Vite marks optimized deps immutable).
        _mark_show_runtime_document_no_store(response_headers)
    content = _compress_response_content(content, response_headers, starlette_request)
    return FastAPIResponse(content=content, status_code=proxied.status_code, headers=response_headers)


def _should_inject_show_runtime_config(
    status_code: int,
    headers: dict[str, str],
    *,
    inject_show_config: bool,
) -> bool:
    if not inject_show_config or status_code != 200:
        return False
    if _show_response_is_attachment(_response_header(headers, "content-disposition")):
        return False
    return _show_response_is_html(_response_header(headers, "content-type"))


def _mark_show_runtime_document_no_store(headers: dict[str, str]) -> None:
    for name in ("cache-control", "etag", "expires", "last-modified", "content-length"):
        _remove_response_header(headers, name)
    headers["Cache-Control"] = "no-store"


def _compress_response_content(content: bytes, headers: dict[str, str], starlette_request: FastAPIRequest) -> bytes:
    if len(content) < _SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES:
        return content
    if _response_header(headers, "content-encoding"):
        return content
    if _show_response_is_attachment(_response_header(headers, "content-disposition")):
        return content
    if starlette_request.headers.get("upgrade", "").lower() == "websocket":
        return content
    content_type = _response_header(headers, "content-type") or ""
    if not _show_response_is_compressible(content_type):
        return content
    _set_response_header(headers, "Vary", _append_vary_header(_response_header(headers, "vary"), "Accept-Encoding"))
    if not _accept_encoding_allows_gzip(starlette_request.headers.get("accept-encoding")):
        return content
    compressed = gzip.compress(content, compresslevel=6)
    if len(compressed) >= len(content):
        return content
    _remove_response_header(headers, "content-length")
    _remove_response_header(headers, "etag")
    _set_response_header(headers, "Content-Encoding", "gzip")
    _set_response_header(headers, "Content-Length", str(len(compressed)))
    return compressed


def _compress_materialized_api_response(response: Response) -> Response:
    if not (request.path or "").startswith("/api/"):
        return response
    body = getattr(response, "body", None)
    if body is None:
        return response
    if not isinstance(body, bytes | bytearray | memoryview):
        return response
    content = bytes(body)
    compressed = _compress_response_content(content, response.headers, request._request)
    if compressed is content:
        return response
    response.body = compressed
    return response


def _accept_encoding_allows_gzip(accept_encoding: str | None) -> bool:
    if not accept_encoding:
        return False
    for item in accept_encoding.split(","):
        parts = [part.strip() for part in item.split(";") if part.strip()]
        if not parts or parts[0].lower() != "gzip":
            continue
        q = 1.0
        for param in parts[1:]:
            name, separator, value = param.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
                break
        return q > 0
    return False


def _set_response_header(headers: dict[str, str], name: str, value: str) -> None:
    _remove_response_header(headers, name)
    headers[name] = value


def _append_vary_header(existing: str | None, value: str) -> str:
    values = [item.strip() for item in (existing or "").split(",") if item.strip()]
    if not any(item.lower() == value.lower() for item in values):
        values.append(value)
    return ", ".join(values)


def _show_response_is_compressible(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    if "text/event-stream" in lowered:
        return False
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
    return (
        _show_response_is_javascript(content_type)
        or _show_response_is_html(content_type)
        or bool(content_type and "text/css" in content_type.lower())
    )


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


def _is_current_immutable_static_asset_request() -> bool:
    return (request.path or "").startswith("/assets/") or _is_current_show_runtime_immutable_asset_request()


def _remove_response_header(headers: dict[str, str], name: str) -> None:
    normalized = name.lower()
    for key in list(headers):
        if key.lower() == normalized:
            del headers[key]


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


def _show_runtime_config_payload(
    session_id: str,
    *,
    base_path: str,
    authenticated: bool,
    include_write_token: bool,
) -> dict[str, Any]:
    events_path = f"{base_path}__show/events"
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "basePath": base_path,
        "eventsPath": events_path,
        "streamPath": f"{events_path}?stream=1",
        "annotation": {
            "authenticated": authenticated,
            "mePath": "__show/me",
        },
    }
    if include_write_token:
        payload["writeToken"] = show_event_write_token(session_id)
    return payload


def _show_runtime_config_script(
    session_id: str,
    *,
    base_path: str,
    authenticated: bool,
    include_write_token: bool,
) -> str:
    payload = json.dumps(
        _show_runtime_config_payload(
            session_id,
            base_path=base_path,
            authenticated=authenticated,
            include_write_token=include_write_token,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        "<script>"
        "(function(){"
        f"var next={payload};"
        "globalThis.__AVIBE_SHOW__=Object.assign({},globalThis.__AVIBE_SHOW__||{},next);"
        "function parentNavigate(){"
        "try{"
        "var candidate=window.parent!==window&&window.parent.__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__;"
        "return typeof candidate==='function'?candidate:null;"
        "}catch(_){return null;}"
        "}"
        "function isIosStandalone(){"
        "var ua=navigator.userAgent||'';"
        "var ios=/iP(hone|ad|od)/.test(ua)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);"
        "return ios&&(navigator.standalone===true||(window.matchMedia&&window.matchMedia('(display-mode: standalone)').matches));"
        "}"
        "document.addEventListener('click',function(event){"
        "var bridge=parentNavigate();"
        "if(!bridge&&!isIosStandalone())return;"
        "var element=event.target instanceof Element?event.target:null;"
        "var anchor=element&&element.closest('a[href]');"
        "if(!anchor||String(anchor.target).toLowerCase()!=='_blank'||anchor.hasAttribute('download'))return;"
        "var target;try{target=new URL(anchor.href,window.location.href);}catch(_){return;}"
        "if(target.origin!==window.location.origin||!/^https?:$/.test(target.protocol))return;"
        "event.preventDefault();event.stopImmediatePropagation();"
        "var path=target.pathname+target.search+target.hash;"
        "var base=String(next.basePath||'');"
        "var withinShow=base&&(target.pathname===base.slice(0,-1)||target.pathname.indexOf(base)===0);"
        "if(window.parent!==window&&!withinShow&&bridge){bridge(target.href);return;}"
        "window.location.assign(path);"
        "},true);"
        "}());"
        "</script>"
    )


def _inject_show_runtime_config(
    content: bytes,
    session_id: str,
    *,
    base_path: str,
    authenticated: bool,
    include_write_token: bool,
    include_annotation_bootstrap: bool = True,
) -> bytes:
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    script = _show_runtime_config_script(
        session_id,
        base_path=base_path,
        authenticated=authenticated,
        include_write_token=include_write_token,
    )
    bootstrap = (
        f'<script type="module" src="{base_path}__show/annotation.js"></script>'
        if include_annotation_bootstrap
        else ""
    )
    module_match = _SHOW_RUNTIME_MODULE_SCRIPT_RE.search(html)
    if module_match:
        html = f"{html[: module_match.start()]}{script}\n    {html[module_match.start() :]}"
    elif "</head>" in html:
        html = html.replace("</head>", f"{script}\n  </head>", 1)
    elif "</body>" in html:
        html = html.replace("</body>", f"{script}\n  </body>", 1)
    else:
        html = f"{script}\n{html}"
    if bootstrap:
        if "</body>" in html:
            html = html.replace("</body>", f"{bootstrap}\n  </body>", 1)
        elif "</html>" in html:
            html = html.replace("</html>", f"{bootstrap}\n</html>", 1)
        else:
            html = f"{html}\n{bootstrap}"
    return html.encode("utf-8")


def _rewrite_show_runtime_url_headers(
    headers: dict[str, str],
    *,
    session_id: str,
    external_prefix: str | None,
) -> None:
    for header in ("location", "sourcemap", "x-sourcemap"):
        value = _response_header(headers, header)
        if value is None:
            continue
        _set_response_header(
            headers,
            header,
            _rewrite_show_runtime_url(session_id, value, external_prefix=external_prefix),
        )


def _rewrite_show_runtime_url(session_id: str, value: str, *, external_prefix: str | None = None) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc) and not _is_local_show_runtime_url(parsed):
        return value
    internal_prefix = f"/sessions/{quote(session_id, safe='')}/app"
    private_prefix = f"/show/{quote(session_id, safe='')}"
    resolved_external_prefix = (external_prefix or private_prefix).rstrip("/")
    if parsed.path == internal_prefix:
        public_path = f"{resolved_external_prefix}/"
    elif parsed.path.startswith(f"{internal_prefix}/"):
        suffix = parsed.path[len(internal_prefix) :].lstrip("/")
        public_path = f"{resolved_external_prefix}/{suffix}"
    elif external_prefix and parsed.path == private_prefix:
        public_path = f"{resolved_external_prefix}/"
    elif external_prefix and parsed.path.startswith(f"{private_prefix}/"):
        suffix = parsed.path[len(private_prefix) :].lstrip("/")
        public_path = f"{resolved_external_prefix}/{suffix}"
    else:
        return value
    return urlunsplit(("", "", public_path, parsed.query, parsed.fragment))


def _is_local_show_runtime_url(parsed) -> bool:
    if parsed.scheme.lower() != "http":
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _with_show_event_write_cookie(response: Response, session_id: str, *, enabled: bool) -> Response:
    # 'self' (not 'none') so the workbench can frame a Show Page in the chat
    # view — same origin as the page — while cross-origin clickjacking stays
    # blocked. Direct navigation is unaffected (frame-ancestors only governs
    # framing).
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    if enabled:
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


async def sweep_orphan_show_runtime_servers_on_startup() -> None:
    """Reap a Show Runtime server left bound to the workspace root by a prior UI
    server process that died without running its shutdown hook (SIGKILL / crash).

    The Node ``cli.js`` runtime is a child of THIS UI server process. On a hard
    death it is reparented to init and keeps serving stale code on its old port
    (avibe#813). ``vibe`` only spawns a new UI server when no healthy one exists,
    so at startup any runtime still on the root is an orphan and safe to sweep.

    Offloaded to a thread so the psutil scan + terminate/kill never blocks the
    event loop (and thus /health readiness) during startup."""
    from core.show_runtime import sweep_orphan_show_runtime_servers

    try:
        await asyncio.to_thread(sweep_orphan_show_runtime_servers)
    except Exception:
        logger.debug("startup show runtime orphan sweep skipped", exc_info=True)


def stop_show_runtime_on_shutdown() -> None:
    from core.show_runtime import stop_show_runtime_manager, sweep_orphan_show_runtime_servers

    # Stop the tracked child (its process group, incl. esbuild workers), then sweep
    # the workspace root so any untracked stray from an earlier respawn is reaped too.
    stop_show_runtime_manager()
    try:
        sweep_orphan_show_runtime_servers()
    except Exception:
        logger.debug("shutdown show runtime orphan sweep skipped", exc_info=True)


@app.route("/show/<session_id>")
def redirect_private_show_page_to_canonical_path(session_id):
    from core.show_pages import ShowPageError, ShowPageStore

    store = ShowPageStore()
    try:
        try:
            page = store.require_access(
                session_id,
                user_context=_request_authorization_context(),
            )
        except ShowPageError as exc:
            if exc.code == "resource_access_forbidden":
                return _show_page_access_forbidden_response()
            return _show_page_not_found_response()
        # The authenticated editor surface accepts every configured audience;
        # offline still redirects to the explanatory offline page.
        if page.visibility not in {"private", "limited", "public", "offline"}:
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
    from core.show_pages import ShowPageError, ShowPageStore, ensure_show_page_dir

    authorization_context = _request_authorization_context()
    runtime_retry_authorized = _has_runtime_owner_access(authorization_context)
    markdown_requested = _is_show_page_markdown_request(asset_path, request._request)
    store = ShowPageStore()
    try:
        try:
            page = store.require_access(
                session_id,
                user_context=authorization_context,
            )
        except ShowPageError as exc:
            if exc.code == "resource_access_forbidden":
                if markdown_requested:
                    return _show_page_markdown_error_response("forbidden", 403)
                return _show_page_access_forbidden_response()
            if markdown_requested:
                return _show_page_markdown_error_response("session_unknown", 404)
            return _show_page_not_found_response()
        if page.visibility == "offline":
            if markdown_requested:
                return _show_page_markdown_error_response("page_offline", 404)
            return _show_page_offline_response()
        # The Workbench editor route serves every online audience mode. This is
        # no new anonymous exposure: `/show` stays behind Workbench and resource
        # authorization, while `/p` owns shared navigation admission. `offline`
        # (handled above) and unexpected states still fail closed.
        if page.visibility not in {"private", "limited", "public"}:
            if markdown_requested:
                return _show_page_markdown_error_response("session_unknown", 404)
            return _show_page_not_found_response()
        # A remote viewer is an untrusted viewer even on the private surface: keep
        # its asset reads inside the page workspace so an authored symlink cannot
        # serve out-of-Project disk files across the tunnel.
        if _is_show_page_runtime_denied_path(
            asset_path,
            session_id=page.session_id,
            confine_to_workspace=_is_remote_show_page_request(),
        ):
            if markdown_requested:
                return _show_page_markdown_error_response("session_unknown", 404)
            return _show_page_file_not_found_response()
        show_author = _show_request_author()
        can_annotate = _show_annotation_capability(
            author=show_author,
            page=page,
        )
        # §3.2: /show reads admit every Instance Viewer, but the route forwards
        # mutation methods straight to Show Runtime — keep Viewers read-only.
        if request.method not in {"GET", "HEAD"} and not _show_page_mutation_allowed(
            authorization_context
        ):
            return _show_page_access_forbidden_response()
        markdown_requested = markdown_requested and _show_page_markdown_target_is_document(
            page.session_id,
            asset_path,
        )
        if markdown_requested:
            return await _show_page_markdown_runtime_response(
                page.session_id,
                asset_path,
                request._request,
                runtime_retry_authorized=runtime_retry_authorized,
            )
        if asset_path.strip("/") == "__show/me":
            if request.method not in {"GET", "HEAD"}:
                return jsonify({"ok": False, "code": "method_not_allowed"}), 405
            return _show_me_response(
                show_author,
                can_annotate=can_annotate,
                write_token=show_event_write_token(page.session_id) if can_annotate else None,
            )
        if asset_path.strip("/") in {"__show/events", "__events"}:
            return await _show_events_response(page.session_id)
        page_dir = ensure_show_page_dir(page.session_id)
        response = None
        if request.method in {"GET", "HEAD"} or _is_show_api_asset(asset_path):
            from core.show_runtime import (
                ShowRuntimeRequestTimeoutError,
                ShowRuntimeUnavailableError,
            )

            try:
                starlette_request = request._request
                response = await _show_page_runtime_response(
                    page.session_id,
                    asset_path,
                    starlette_request,
                    inject_show_config=request.method == "GET" and not _is_show_api_asset(asset_path),
                    show_authenticated=can_annotate,
                    runtime_retry_authorized=runtime_retry_authorized,
                )
            except (ShowRuntimeUnavailableError, ShowRuntimeRequestTimeoutError) as exc:
                if isinstance(exc, ShowRuntimeRequestTimeoutError):
                    return _show_page_runtime_error_response(asset_path, exc)
                reason, failure_class, recovery_action = _show_page_runtime_failure_evidence(exc)
                if _is_show_api_asset(asset_path) or _is_show_annotation_asset(asset_path):
                    return _show_page_runtime_error_response(asset_path, exc)
                response = _show_page_runtime_failure_response(
                    page_dir,
                    page.session_id,
                    asset_path,
                    request._request,
                    reason=reason,
                    failure_class=failure_class,
                    recovery_action=recovery_action,
                    retry_authorized=runtime_retry_authorized,
                )
                _log_show_runtime_unavailable(reason, public=False, fallback=response is not None)
        if response is None:
            response = _show_page_file_response(page_dir, asset_path)
        if request.method in {"GET", "HEAD"}:
            if _is_show_runtime_immutable_asset_path(asset_path):
                return response
            return _with_show_event_write_cookie(response, page.session_id, enabled=can_annotate)
        return response
    finally:
        store.close()


def _show_identity_error_response(error: str, status: int):
    response = jsonify({"ok": False, "error": error})
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _show_identity_not_found_response():
    """Hide whether a share exists when identity admission is denied."""
    if _show_page_accepts_html():
        return _show_page_not_found_html_response()
    response = jsonify({"error": "not_found"})
    response.status_code = 404
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _show_identity_error_status(error: str, *, default: int = 400) -> int:
    if error == "identity_unavailable":
        return 503
    if error == "identity_not_verified":
        return 403
    return default


async def _read_show_identity_callback_body(
    starlette_request: FastAPIRequest,
) -> bytes:
    from vibe.show_identity import MAX_CALLBACK_BODY_BYTES, ShowIdentityError

    body = bytearray()
    async for chunk in starlette_request.stream():
        if len(body) + len(chunk) > MAX_CALLBACK_BODY_BYTES:
            raise ShowIdentityError("invalid_callback")
        body.extend(chunk)
    return bytes(body)


async def _show_identity_callback_fields() -> dict[str, str]:
    from vibe.show_identity import MAX_CALLBACK_BODY_BYTES, ShowIdentityError

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ShowIdentityError("invalid_callback")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0 or parsed_content_length > MAX_CALLBACK_BODY_BYTES:
                raise ShowIdentityError("invalid_callback")
        except ValueError as exc:
            raise ShowIdentityError("invalid_callback") from exc
    body = await _read_show_identity_callback_body(request._request)
    try:
        pairs = parse_qsl(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ShowIdentityError("invalid_callback") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise ShowIdentityError("invalid_callback")
        fields[key] = value
    if set(fields) not in ({"state", "assertion"}, {"state", "error"}):
        raise ShowIdentityError("invalid_callback")
    if any(not value for value in fields.values()):
        raise ShowIdentityError("invalid_callback")
    return fields


def _show_guest_lease(config: V2Config | None, share_id: str):
    from core.show_pages import ShowPageError
    from vibe import show_identity

    if config is None:
        return None
    try:
        return show_identity.read_show_guest_lease(
            config,
            request.cookies.get(show_identity.show_guest_cookie_name(share_id)),
            expected_share_id=share_id,
        )
    except (ShowPageError, show_identity.ShowIdentityError):
        return None


def _with_limited_show_policy(response: Response) -> Response:
    response.headers["Cache-Control"] = "private, no-store"
    vary = response.headers.get("Vary", "")
    vary_values = {value.strip() for value in vary.split(",") if value.strip()}
    vary_values.add("Cookie")
    response.headers["Vary"] = ", ".join(sorted(vary_values))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


def _show_limited_not_found_response():
    result = _show_page_not_found_response()
    if isinstance(result, tuple):
        response, status = result
        return _with_limited_show_policy(response), status
    return _with_limited_show_policy(result)


@app.route("/auth/show-identity/callback", methods=["POST"])
async def complete_show_identity_login():
    from core.show_pages import ShowPageStore
    from vibe import show_identity

    if _auth_rate_limited():
        return _auth_rate_limit_response()
    config = _load_remote_access_config()
    if config is None:
        return _show_identity_error_response("identity_unavailable", 503)
    try:
        fields = await _show_identity_callback_fields()
        state = show_identity.read_show_identity_state(
            config,
            fields.get("state"),
            callback_origin=_current_origin(),
        )
        if "error" in fields:
            if fields["error"] not in {"identity_not_verified", "identity_unavailable"}:
                raise show_identity.ShowIdentityError("invalid_callback")
            if fields["error"] == "identity_not_verified":
                return _show_identity_not_found_response()
            return _show_identity_error_response(
                fields["error"],
                _show_identity_error_status(fields["error"]),
            )
        identity = await asyncio.to_thread(
            show_identity.verify_show_identity_assertion,
            config,
            fields.get("assertion"),
            expected_nonce=state.nonce,
        )
    except show_identity.ShowIdentityError as exc:
        return _show_identity_error_response(
            exc.reason,
            _show_identity_error_status(exc.reason),
        )
    except Exception:
        logger.warning("Show identity callback failed", exc_info=True)
        return _show_identity_error_response("identity_unavailable", 503)

    store = ShowPageStore()
    try:
        page = store.get_by_share_id(state.share_id)
        if page is None:
            return _show_identity_not_found_response()
        if page.visibility == "offline":
            return _show_identity_not_found_response()
        access = store.get_access(page.session_id)
        if access is None:
            return _show_identity_not_found_response()
        if access.access_mode == "public":
            if page.visibility != "public":
                return _show_identity_not_found_response()
            try:
                show_identity.consume_verified_show_identity(identity)
            except show_identity.ShowIdentityError as exc:
                return _show_identity_error_response(
                    exc.reason,
                    _show_identity_error_status(exc.reason),
                )
            return redirect(state.return_target, code=303)
        grant = _limited_show_access_grant(access, identity.visitor())
        if (
            access.access_mode != "limited"
            or page.visibility != "limited"
            or access.share_id != state.share_id
            or grant is None
        ):
            return _show_identity_not_found_response()

        try:
            show_identity.consume_verified_show_identity(identity)
        except show_identity.ShowIdentityError as exc:
            return _show_identity_error_response(
                exc.reason,
                _show_identity_error_status(exc.reason),
            )

        # This browser-session lease intentionally has no live revision check:
        # membership changes affect new admissions, not a page already opened.
        lease = show_identity.make_show_guest_lease(
            config,
            page_id=page.session_id,
            share_id=state.share_id,
            normalized_email=identity.normalized_email,
            grant=grant,
        )
        response = redirect(state.return_target, code=303)
        response.set_cookie(
            show_identity.show_guest_cookie_name(state.share_id),
            lease,
            httponly=True,
            secure=True,
            samesite="Lax",
            path=show_identity.show_guest_cookie_path(state.share_id),
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    finally:
        store.close()


@app.route("/p/<share_id>")
def redirect_public_show_page_to_canonical_path(share_id):
    from core.show_pages import ShowPageStore

    config = _load_remote_access_config()
    lease = _show_guest_lease(config, share_id)
    store = ShowPageStore()
    try:
        page = store.get_by_share_id(share_id)
        if page is None and lease is not None:
            # A lease preserves an already-admitted browser across audience and
            # share-link changes. New visitors cannot resolve the retired link;
            # explicit offline is the only immediate availability withdrawal.
            page = store.get(lease.page_id)
        if page is None:
            return _show_page_not_found_response()
        if lease is None and page.visibility not in {"public", "limited", "offline"}:
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
    from core.show_pages import ShowPageError, ShowPageStore, ensure_show_page_dir
    from vibe import show_identity

    representation_candidate = _is_public_show_page_document_candidate(
        asset_path,
        request._request,
    )
    request._request.state.public_show_representation_varies = representation_candidate
    markdown_requested = _is_public_show_page_markdown_request(asset_path, request._request)
    config = _load_remote_access_config()
    lease = _show_guest_lease(config, share_id)
    request._request.state.public_show_representation_varies_cookie = lease is not None
    editor_admitted = False
    limited_authenticated = False
    store = ShowPageStore()
    try:
        page = store.get_by_share_id(share_id)
        if page is None and lease is not None:
            page = store.get(lease.page_id)
            if page is not None:
                access = store.get_access(page.session_id)
                if access is None or access.share_id != share_id:
                    if markdown_requested:
                        return _show_page_markdown_error_response("session_unknown", 404)
                    return _show_limited_not_found_response()
        if page is None:
            if markdown_requested:
                return _show_page_markdown_error_response("session_unknown", 404)
            return _show_page_not_found_response()
        if page.visibility == "limited":
            request._request.state.public_show_representation_varies_cookie = True
        limited_guest = (
            lease is not None
            and lease.page_id == page.session_id
            and page.visibility != "public"
        )
        if page.visibility == "offline":
            if markdown_requested:
                return _show_page_markdown_error_response("page_offline", 404)
            return _show_page_offline_response()
        runtime_path_denied = _is_show_page_runtime_denied_path(
            asset_path,
            session_id=page.session_id,
            confine_to_workspace=True,
        )
        if not runtime_path_denied:
            representation_candidate = (
                representation_candidate
                and _show_page_markdown_target_is_document(page.session_id, asset_path)
            )
            request._request.state.public_show_representation_varies = (
                representation_candidate
            )
            markdown_requested = markdown_requested and representation_candidate
        is_spa_navigation = markdown_requested or _is_show_page_spa_route_request(
            asset_path,
            request._request,
        )
        admission_navigation_method = request.method == "GET" or (
            markdown_requested and request.method == "HEAD"
        )
        if page.visibility != "public" and is_spa_navigation:
            editor_context = await asyncio.to_thread(_show_public_editor_context)
            if editor_context is not None:
                try:
                    store.require_access(
                        page.session_id,
                        user_context=editor_context,
                    )
                except ShowPageError:
                    pass
                else:
                    if markdown_requested:
                        editor_admitted = True
                    else:
                        private_target = f"/show/{quote(page.session_id, safe='')}/"
                        if asset_path:
                            private_target += quote(asset_path.lstrip("/"), safe="/@:-._~")
                        query = urlsplit(request.full_path).query
                        if query:
                            private_target = f"{private_target}?{query}"
                        return redirect(private_target)
        if limited_guest:
            # A guest lease does not grant a grace period after access changes.
            # Already-rendered pages are not proactively closed, but every
            # subsequent request must match the current local access record.
            access = store.get_access(page.session_id)
            lease_is_current = (
                access is not None
                and page.visibility == "limited"
                and access.access_mode == "limited"
                and access.share_id == share_id
                and _limited_show_access_grant_is_current(access, lease.grant)
            )
            if not lease_is_current:
                current_limited_binding = (
                    access is not None
                    and page.visibility == "limited"
                    and access.access_mode == "limited"
                    and access.share_id == share_id
                )
                if current_limited_binding:
                    authenticated_context = await asyncio.to_thread(
                        _show_public_authenticated_context,
                        config,
                    )
                    if (
                        authenticated_context is not None
                        and admission_navigation_method
                        and is_spa_navigation
                        and (_show_page_accepts_html() or markdown_requested)
                    ):
                        if not _show_limited_viewer_is_allowed(
                            authenticated_context,
                            access,
                        ):
                            if markdown_requested:
                                return _show_page_markdown_error_response("forbidden", 403)
                            return _show_page_access_denied_response(
                                include_back_link=True
                            )
                        # The current identity may be allowed again, but the
                        # old lease must not be treated as valid guest access.
                        limited_guest = False
                        limited_authenticated = markdown_requested
                    else:
                        if markdown_requested:
                            return _show_page_markdown_error_response("session_unknown", 404)
                        return _show_limited_not_found_response()
                else:
                    if markdown_requested:
                        return _show_page_markdown_error_response("session_unknown", 404)
                    return _show_limited_not_found_response()
        if page.visibility == "limited":
            if not limited_guest and not editor_admitted and not limited_authenticated:
                if not admission_navigation_method or not is_spa_navigation:
                    if markdown_requested:
                        return _show_page_markdown_error_response("session_unknown", 404)
                    return _show_page_not_found_response()
                authenticated_context = await asyncio.to_thread(
                    _show_public_authenticated_context,
                    config,
                )
                if authenticated_context is not None:
                    access = store.get_access(page.session_id)
                    if not _show_limited_viewer_is_allowed(
                        authenticated_context,
                        access,
                    ):
                        if markdown_requested:
                            return _show_page_markdown_error_response("forbidden", 403)
                        return _show_page_access_denied_response(
                            include_back_link=True
                        )
                    if markdown_requested:
                        limited_authenticated = True
                elif markdown_requested:
                    return _show_page_markdown_error_response("authentication_required", 401)
                if not limited_authenticated:
                    if config is None:
                        return _show_identity_error_response("identity_unavailable", 503)
                    return_target = request.full_path if request.query_string else request.path
                    try:
                        authorization_url = show_identity.begin_show_identity_authorization(
                            config,
                            callback_origin=_current_origin(),
                            share_id=share_id,
                            return_target=return_target,
                        )
                    except show_identity.ShowIdentityError:
                        return _show_identity_error_response("identity_unavailable", 503)
                    response = redirect(authorization_url)
                    response.headers["Cache-Control"] = "private, no-store"
                    response.headers["Referrer-Policy"] = "no-referrer"
                    return response
        if not limited_guest and not editor_admitted and not limited_authenticated and page.visibility != "public":
            if markdown_requested:
                return _show_page_markdown_error_response("session_unknown", 404)
            return _show_page_not_found_response()
        if runtime_path_denied:
            if markdown_requested:
                return _show_page_markdown_error_response("session_unknown", 404)
            return _show_page_file_not_found_response()
        if markdown_requested:
            return await _show_page_markdown_runtime_response(
                page.session_id,
                asset_path,
                request._request,
                external_prefix=f"/p/{quote(share_id, safe='')}",
            )
        if asset_path.strip("/") == "__show/me":
            if request.method not in {"GET", "HEAD"}:
                return jsonify({"ok": False, "code": "method_not_allowed"}), 405
            author = None if limited_guest else await _show_public_request_author()
            can_annotate = _show_annotation_capability(
                author=author,
                page=page,
                public_share_id=share_id,
            ) if not limited_guest else False
            response = _show_me_response(
                author,
                can_annotate=can_annotate,
                write_token=(
                    show_public_event_write_token(share_id, page.session_id) if can_annotate else None
                ),
            )
            return _with_limited_show_policy(response) if limited_guest else response
        if asset_path.strip("/").startswith("__show/media/"):
            if limited_guest:
                return _show_page_file_not_found_response()
            if request.method not in {"GET", "HEAD"}:
                return jsonify({"ok": False, "code": "method_not_allowed"}), 405
            token = asset_path.strip("/").removeprefix("__show/media/")
            if not token or "/" in token:
                return _show_page_file_not_found_response()
            return _registered_media_response(
                token,
                expected_session_id=page.session_id,
                expected_source="show_annotation",
                public_show_page=True,
            )
        if asset_path.strip("/") in {"__show/events", "__events"}:
            if limited_guest:
                return _show_page_file_not_found_response()
            if request.method == "GET":
                return await _show_events_response(
                    page.session_id,
                    public=True,
                    public_share_id=share_id,
                )
            if request.method != "POST":
                return jsonify({"ok": False, "code": "method_not_allowed"}), 405
            author = await _show_public_request_author()
            if author is None:
                return jsonify({"ok": False, "code": "public_show_events_login_required"}), 403
            can_annotate = _show_annotation_capability(
                author=author,
                page=page,
                public_share_id=share_id,
            )
            if not can_annotate:
                return jsonify({"ok": False, "code": "public_show_events_forbidden"}), 403
            if not _public_show_referer_matches(share_id):
                return jsonify({"ok": False, "code": "public_show_events_origin_mismatch"}), 403
            if not _public_show_event_write_authorized(share_id, page.session_id):
                return jsonify({"ok": False, "code": "show_event_write_forbidden"}), 403
            payload = _sanitize_public_show_event_payload(_show_events_payload_from_request())
            event_type = str(payload.get("type") or "").strip()
            if event_type not in HUMAN_EVENT_TYPES and event_type != "assistant.mark.resolved":
                return _unsupported_show_event_type_response()
            return await _show_event_response_from_payload(
                page.session_id,
                payload,
                author=author,
                public=True,
                public_share_id=share_id,
                allow_dispatch=can_annotate,
            )
        if request.method in {"GET", "HEAD"}:
            if shim_response := _show_runtime_public_client_shim_response(asset_path):
                return shim_response
            if limited_guest and _is_show_annotation_asset(asset_path):
                return _show_page_file_not_found_response()
        page_dir = ensure_show_page_dir(page.session_id)
        response = None
        if request.method in {"GET", "HEAD"} or _is_show_api_asset(asset_path):
            from core.show_runtime import (
                ShowRuntimeRequestTimeoutError,
                ShowRuntimeUnavailableError,
            )

            try:
                starlette_request = request._request
                show_authenticated = False
                if not limited_guest:
                    show_authenticated = await _show_public_request_author() is not None
                response = await _show_page_runtime_response(
                    page.session_id,
                    asset_path,
                    starlette_request,
                    external_prefix=f"/p/{quote(share_id, safe='')}",
                    inject_show_config=request.method == "GET" and not _is_show_api_asset(asset_path),
                    show_authenticated=show_authenticated,
                    runtime_retry_authorized=False,
                    show_config_session_id=share_id,
                    include_annotation_bootstrap=not limited_guest,
                )
            except (ShowRuntimeUnavailableError, ShowRuntimeRequestTimeoutError) as exc:
                if isinstance(exc, ShowRuntimeRequestTimeoutError):
                    return _show_page_runtime_error_response(asset_path, exc)
                reason, failure_class, recovery_action = _show_page_runtime_failure_evidence(exc)
                if _is_show_api_asset(asset_path) or _is_show_annotation_asset(asset_path):
                    return _show_page_runtime_error_response(asset_path, exc)
                response = _show_page_runtime_failure_response(
                    page_dir,
                    page.session_id,
                    asset_path,
                    request._request,
                    reason=reason,
                    failure_class=failure_class,
                    recovery_action=recovery_action,
                    retry_authorized=False,
                )
                _log_show_runtime_unavailable(reason, public=True, fallback=response is not None)
        if response is None:
            response = _show_page_file_response(page_dir, asset_path)
        if limited_guest:
            return _with_limited_show_policy(response)
        if request.method in {"GET", "HEAD"}:
            if _is_show_runtime_immutable_asset_path(asset_path):
                return response
            return _with_show_event_write_cookie(response, page.session_id, enabled=False)
        return response
    finally:
        store.close()


def _ui_static_file_response(resolved_path: Path, *, content_type: str, cache_control: str) -> Response:
    response = send_file(resolved_path, mimetype=content_type)
    response.headers["Cache-Control"] = cache_control
    if hasattr(response, "set_stat_headers"):
        response.set_stat_headers(resolved_path.stat())
    if request.method != "GET" or request.headers.get("range"):
        return response
    headers = response.headers
    content = resolved_path.read_bytes()
    compressed = _compress_response_content(content, headers, request._request)
    if compressed is content:
        return response
    _remove_response_header(headers, "accept-ranges")
    return Response(content=compressed, headers=headers)


@app.route("/", defaults={"path": ""}, methods=["GET", "HEAD"])
@app.route("/<path:path>", methods=["GET", "HEAD"])
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
        if path.startswith("assets/"):
            cache_control = "public, max-age=31536000, immutable"
        elif resolved_path.name == "index.html":
            cache_control = "no-store, private"
        else:
            cache_control = "public, max-age=3600"
        return _ui_static_file_response(
            resolved_path,
            content_type=mime_type or "application/octet-stream",
            cache_control=cache_control,
        )

    # SPA fallback: serve index.html for routes without file extension
    if "." not in path:
        index_path = ui_dist / "index.html"
        if index_path.exists():
            return _ui_static_file_response(
                index_path,
                content_type="text/html",
                cache_control="no-store, private",
            )

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


async def _wait_for_ui_host_ready() -> None:
    """Do not mutate managed dependencies until Uvicorn accepts traffic."""

    while _server is None or not bool(getattr(_server, "started", False)):
        await asyncio.sleep(0.05)


async def _reconcile_startup_dependencies_task() -> None:
    start = time.monotonic()
    try:
        await _wait_for_ui_host_ready()
        from vibe import api

        result = await asyncio.to_thread(api.reconcile_startup_dependencies)
        show_runtime = result.get("show_runtime") if isinstance(result.get("show_runtime"), dict) else {}
        policy = show_runtime.get("policy") if isinstance(show_runtime.get("policy"), dict) else {}
        install = show_runtime.get("install") if isinstance(show_runtime.get("install"), dict) else {}
        if policy.get("state") == "allowed" and install.get("state") == "installed":
            from core.show_runtime import (
                ShowRuntimeContext,
                prewarm_show_page_session,
                prewarm_show_runtime,
            )

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
                    try:
                        context = ShowRuntimeContext(page.get("context"))
                    except (TypeError, ValueError):
                        page_results.append(
                            {
                                "session_id": session_id,
                                "ok": False,
                                "reason": "invalid_show_runtime_context",
                            }
                        )
                        continue
                    session_prewarm = await prewarm_show_page_session(
                        session_id,
                        context=context,
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
            model_hub_engine = (
                result.get("model_hub_engine")
                if isinstance(result.get("model_hub_engine"), dict)
                else {}
            )
            memory_package = (
                result.get("memory_package")
                if isinstance(result.get("memory_package"), dict)
                else {}
            )
            logger.warning(
                "Startup dependency reconcile completed with issues in %sms: "
                "memory_package=%s askill=%s model_hub_engine=%s show_runtime=%s",
                duration_ms,
                memory_package.get("message")
                or memory_package.get("reason")
                or memory_package.get("status")
                or memory_package.get("ok"),
                askill.get("message") or askill.get("status") or askill.get("ok"),
                model_hub_engine.get("message")
                or model_hub_engine.get("reason")
                or model_hub_engine.get("status")
                or model_hub_engine.get("ok"),
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


app.add_event_handler("startup", sweep_orphan_show_runtime_servers_on_startup)
app.add_event_handler("startup", _start_startup_dependency_reconcile)
app.add_event_handler("shutdown", _stop_startup_dependency_reconcile)
app.add_event_handler("shutdown", stop_show_runtime_on_shutdown)


# cloudflared holds idle origin connections in a pool for up to
# --proxy-keepalive-timeout (default 1m30s) and reuses them for later requests.
# uvicorn's own default is 5s, so the origin closes connections the tunnel still
# considers reusable: a request handed to one while it is being torn down loses
# the race, cloudflared reports "connection reset by peer", and the browser sees
# a 502 even though the server is healthy. Outliving the upstream pool keeps
# idle teardown on the proxy side, where it cannot collide with a live request.
# The same reasoning covers any reverse proxy in front of the UI; nginx's
# keepalive_timeout default (75s) is also below this value.
_CLOUDFLARED_PROXY_KEEPALIVE_TIMEOUT_SECONDS = 90
_UI_KEEPALIVE_TIMEOUT_SECONDS = 120


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

    from vibe.memory_ui_access import initialize_process_ui_read_secret

    initialize_process_ui_read_secret()
    global _UI_RUNTIME_ACTIVE, _server
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

            remote_access.start_runtime_monitoring(config)
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
                timeout_keep_alive=_UI_KEEPALIVE_TIMEOUT_SECONDS,
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
            _UI_RUNTIME_ACTIVE = True
            try:
                _server.run(sockets=[bound_socket])
            finally:
                _UI_RUNTIME_ACTIVE = False
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
