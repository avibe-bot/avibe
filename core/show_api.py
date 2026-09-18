"""Explicit, bounded admission for trusted public Show server handlers.

This module never executes a handler or supplies sender authentication. The
workspace author opts an exact route into handler-owned authentication.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import ClientDisconnect, Request

from core.show_pages import ShowPageStore, show_page_dir
from config import paths

logger = logging.getLogger(__name__)
MANIFEST_NAME = ".show-api.json"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_ROUTES = 16
MAX_BODY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_FORWARD_HEADERS = 16
MAX_HEADER_NAME_BYTES = 64
MAX_HEADER_VALUE_BYTES = 2048
MAX_REQUEST_HEADER_BYTES = 16 * 1024
MAX_REQUEST_HEADERS = 64
BODY_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 30.0
_ROUTE = re.compile(r"api/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
_PUBLIC_PATH = re.compile(rb"/p/([A-Za-z0-9_-]+)/((?:api/)[A-Za-z0-9_/-]+)\Z")
_HEADER = re.compile(r"x-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_BLOCKED_PREFIXES = ("x-avibe-", "x-vibe-", "x-forwarded-", "x-original-", "x-proxy-", "x-http-")
_BLOCKED_WORDS = {
    "authorization", "authentication", "auth", "cookie", "csrf", "xsrf", "token", "credential", "credentials",
    "secret", "key", "host", "origin", "referer", "connection", "upgrade",
    "transfer", "encoding", "length", "real", "remote", "method",
}


@dataclass(frozen=True)
class ServerAPIRegistration:
    session_id: str
    share_id: str
    path: str
    module: Path
    max_body_bytes: int
    forward_headers: tuple[str, ...]


class ServerAPIRequestError(Exception):
    """Only a fixed status crosses the public boundary, never exception text."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__("Show server API request rejected")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate declaration key")
        result[key] = value
    return result


def _metadata_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid metadata name")
    name = value.lower()
    if (
        len(name) > MAX_HEADER_NAME_BYTES
        or not _HEADER.fullmatch(name)
        or name.startswith(_BLOCKED_PREFIXES)
        or _BLOCKED_WORDS.intersection(name.split("-"))
    ):
        raise ValueError("invalid metadata name")
    return name


def _declarations(workspace: Path) -> list[dict]:
    manifest = workspace / MANIFEST_NAME
    # A FIFO/device must not block a request, and a symlink must stay confined.
    manifest.resolve(strict=True).relative_to(workspace)
    fd = os.open(manifest, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("invalid manifest file")
        data = source.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest too large")
    payload = json.loads(data, object_pairs_hook=_unique_object)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "server_to_server"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload["server_to_server"], list)
        or len(payload["server_to_server"]) > MAX_ROUTES
    ):
        raise ValueError("unsupported declaration")
    seen = set()
    for route in payload["server_to_server"]:
        if not isinstance(route, dict) or set(route) != {
            "path", "method", "auth", "max_body_bytes", "forward_headers",
        }:
            raise ValueError("invalid route declaration")
        path = route["path"]
        if not isinstance(path, str) or len(path) > 256 or not _ROUTE.fullmatch(path) or path in seen:
            raise ValueError("invalid route path")
        seen.add(path)
        if route["method"] != "POST" or route["auth"] != "handler":
            raise ValueError("unsupported route policy")
        limit = route["max_body_bytes"]
        if type(limit) is not int or not 1 <= limit <= MAX_BODY_BYTES:
            raise ValueError("invalid body limit")
        names = route["forward_headers"]
        if not isinstance(names, list) or len(names) > MAX_FORWARD_HEADERS:
            raise ValueError("invalid metadata list")
        names = tuple(_metadata_name(name) for name in names)
        if len(set(names)) != len(names):
            raise ValueError("duplicate metadata name")
        route["forward_headers"] = names
    return payload["server_to_server"]


def is_server_api_path(raw_path: bytes, method: str, query: bytes = b"") -> bool:
    """Identify only a possible declaration path, without granting admission."""
    if method != "POST" or query or len(raw_path) > 512:
        return False
    match = _PUBLIC_PATH.fullmatch(raw_path)
    return match is not None and _ROUTE.fullmatch(match[2].decode("ascii")) is not None


def resolve_server_api(raw_path: bytes, method: str, query: bytes = b"") -> ServerAPIRegistration | None:
    """Resolve admission from current page state and files, with no lifecycle writes."""
    if not is_server_api_path(raw_path, method, query):
        return None
    match = _PUBLIC_PATH.fullmatch(raw_path)
    share_id, route_path = (part.decode("ascii") for part in match.groups())
    try:
        store = ShowPageStore(read_only=True)
        try:
            page = store.get_by_share_id(share_id)
        finally:
            store.close()
    except Exception as exc:
        # Datastore failures are not an absent registration. Do not fall through
        # to browser admission or expose exception detail through the UI handler.
        logger.warning("Show server API datastore unavailable (%s)", type(exc).__name__)
        raise ServerAPIRequestError(503) from exc
    if page is None or page.share_id != share_id or page.visibility != "public":
        return None
    try:
        workspace = show_page_dir(page.session_id).resolve(strict=True)
        workspace.relative_to(paths.get_show_pages_dir().resolve(strict=True))
        declarations = _declarations(workspace)
        for route in declarations:
            if route["path"] != route_path:
                continue
            # Runtime selects api/<path>.ts before directory or root fallbacks.
            module = (workspace / f"{route_path}.ts").resolve(strict=True)
            module.relative_to(workspace)
            if not module.is_file():
                return None
            return ServerAPIRegistration(
                page.session_id, share_id, route_path, module,
                route["max_body_bytes"], route["forward_headers"],
            )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, RecursionError):
        logger.warning("Invalid optional Show server API declaration; admission disabled")
    return None


def server_api_headers(request: Request, registration: ServerAPIRegistration) -> dict[str, str]:
    """Forward only content type and declared non-credential metadata.

    Duplicate headers are rejected before any coalescing by HTTP libraries.
    Browser identity and caller-supplied runtime protocol headers are discarded.
    """
    raw = request.scope.get("headers", [])
    if len(raw) > MAX_REQUEST_HEADERS or sum(len(k) + len(v) for k, v in raw) > MAX_REQUEST_HEADER_BYTES:
        raise ServerAPIRequestError(431)
    headers = {}
    for key, value in raw:
        name = key.decode("latin-1").lower()
        if name in headers:
            raise ServerAPIRequestError(400)
        headers[name] = value.decode("latin-1")
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise ServerAPIRequestError(415)
    connection_fields = {part.strip().lower() for part in headers.get("connection", "").split(",")}
    if connection_fields.intersection(("content-type", *registration.forward_headers)):
        raise ServerAPIRequestError(400)
    length = headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,10}", length) or "transfer-encoding" in headers:
            raise ServerAPIRequestError(400)
        if int(length) > registration.max_body_bytes:
            raise ServerAPIRequestError(413)
    forwarded = {}
    for name in ("content-type", *registration.forward_headers):
        if name not in headers:
            continue
        value = headers[name]
        if len(value) > MAX_HEADER_VALUE_BYTES or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ServerAPIRequestError(400)
        forwarded[name] = value
    return forwarded


async def read_server_api_body(request: Request, registration: ServerAPIRegistration) -> bytes:
    async def read():
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > registration.max_body_bytes:
                raise ServerAPIRequestError(413)
            body.extend(chunk)
        return bytes(body)

    try:
        return await asyncio.wait_for(read(), BODY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ServerAPIRequestError(408) from exc
    except ClientDisconnect as exc:
        raise ServerAPIRequestError(400) from exc
