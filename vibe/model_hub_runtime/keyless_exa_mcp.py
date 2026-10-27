"""Small, reusable MCP session for keyless Exa services.

The session is deliberately transport-only: it knows how to negotiate MCP,
adapt a live tool schema, and return one bounded JSON-RPC response. Search
mapping, service outcomes, cooldowns, and usage remain owned by Model Hub.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import aiohttp

from core.handlers.model_hub.adapter import SearchResponse
from vibe import __version__ as AVIBE_VERSION


KEYLESS_EXA_ENDPOINT = "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa,web_fetch_exa"
KEYLESS_EXA_SEARCH_TOOL = "web_search_advanced_exa"
KEYLESS_EXA_FETCH_TOOL = "web_fetch_exa"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SESSION_HEADER = "Mcp-Session-Id"
_MAX_BODY_BYTES = 8 * 1024 * 1024
_TIMEOUT_SECONDS = 30.0
_CONNECT_TIMEOUT_SECONDS = 5.0
_REQUIRED_SEARCH_FIELDS = frozenset({"query", "numResults", "enableHighlights", "textMaxCharacters"})
_REQUIRED_FETCH_FIELDS = frozenset({"urls", "maxCharacters"})


@dataclass(frozen=True)
class _Exchange:
    status: int | None
    headers: Mapping[str, str]
    body: bytes | None
    error: str = ""
    sent: bool = False
    received_at: datetime | None = None
    messages: tuple[Mapping[str, Any], ...] = ()
    request_id: int | None = None
    phase: str | None = None


def _parse_messages(body: bytes | None) -> tuple[Mapping[str, Any], ...]:
    if not body:
        return ()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    stripped = text.strip()
    candidates: list[str] = []
    if stripped.startswith("{") or stripped.startswith("["):
        candidates.append(stripped)
    else:
        data_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                value = line[5:].lstrip()
                if value and value != "[DONE]":
                    data_lines.append(value)
            elif not line and data_lines:
                candidates.append("\n".join(data_lines))
                data_lines = []
        if data_lines:
            candidates.append("\n".join(data_lines))
    messages: list[Mapping[str, Any]] = []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError):
            continue
        values = value if isinstance(value, list) else [value]
        messages.extend(item for item in values if isinstance(item, Mapping))
    return tuple(messages)


async def _read_bounded(content: Any) -> tuple[bytes | None, bool]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in content.iter_chunked(64 * 1024):
        data = bytes(chunk)
        total += len(data)
        if total > _MAX_BODY_BYTES:
            return None, True
        chunks.append(data)
    return b"".join(chunks), False


def _matching_message(messages: tuple[Mapping[str, Any], ...], request_id: int) -> Mapping[str, Any] | None:
    return next((message for message in messages if message.get("id") == request_id), None)


def _session_not_found(exchange: _Exchange) -> bool:
    if exchange.status != 404:
        return False
    text = (exchange.body or b"")[:1024].decode("utf-8", "ignore").lower()
    return "session" in text and "not found" in text


def _tool_properties(
    tool: Mapping[str, Any],
    *,
    require_search_controls: bool = False,
    require_fetch_controls: bool = False,
) -> Mapping[str, Any] | None:
    schema = tool.get("inputSchema")
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    if require_search_controls and not _REQUIRED_SEARCH_FIELDS.issubset(properties):
        return None
    if require_fetch_controls and not _REQUIRED_FETCH_FIELDS.issubset(properties):
        return None
    return properties


class KeylessExaMcpClient:
    """Reusable MCP session with single-flight setup and concurrent tool calls."""

    def __init__(
        self,
        *,
        endpoint: str = KEYLESS_EXA_ENDPOINT,
        client_version: str = AVIBE_VERSION,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        connect_timeout_seconds: float = _CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = endpoint
        self.client_version = client_version
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self._setup_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._tools: dict[str, Mapping[str, Any]] | None = None
        self._next_id = 0
        self._http_session: aiohttp.ClientSession | None = None
        self._trace_config = aiohttp.TraceConfig()
        self._trace_config.on_request_headers_sent.append(self._on_request_headers_sent)

    @staticmethod
    async def _on_request_headers_sent(_session, trace_ctx, _params) -> None:
        request_ctx = getattr(trace_ctx, "trace_request_ctx", None)
        if not isinstance(request_ctx, Mapping):
            return
        callback = request_ctx.get("on_sent")
        callback_state = request_ctx.get("callback_state")
        if not callable(callback) or not isinstance(callback_state, dict) or callback_state.get("called"):
            return
        admission = callback()
        if inspect.isawaitable(admission):
            await admission
        callback_state["called"] = True

    async def _http(self) -> aiohttp.ClientSession:
        session = self._http_session
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                connector=aiohttp.TCPConnector(limit=10, force_close=False),
                trust_env=False,
                trace_configs=[self._trace_config],
            )
            self._http_session = session
        return session

    async def close(self) -> None:
        """Close the keep-alive client at runtime shutdown."""

        session, self._http_session = self._http_session, None
        self._clear_session()
        if session is not None and not session.closed:
            await session.close()

    def _request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _clear_session(self) -> None:
        self._session_id = None
        self._protocol_version = None
        self._tools = None

    def _observe_notifications(self, messages: tuple[Mapping[str, Any], ...]) -> None:
        if any(message.get("method") == "notifications/tools/list_changed" for message in messages):
            self._tools = None

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        deadline: float,
        on_sent: Callable[[], Awaitable[None] | None] | None = None,
        notification: bool = False,
    ) -> _Exchange:
        if time.monotonic() >= deadline:
            return _Exchange(None, {}, None, "timeout", phase=method)
        request_id = None if notification else self._request_id()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = dict(params)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "User-Agent": f"Avibe/{self.client_version}",
            "X-Avibe-Client": "avibe",
            "x-exa-source": "avibe",
        }
        if self._session_id is not None:
            headers[MCP_SESSION_HEADER] = self._session_id
        callback_state = {"called": False}
        response_status: int | None = None
        response_headers: dict[str, str] = {}
        response_received_at: datetime | None = None
        timeout_total = max(0.001, deadline - time.monotonic())
        timeout = aiohttp.ClientTimeout(
            total=timeout_total,
            connect=min(self.connect_timeout_seconds, timeout_total),
        )
        trace_request_ctx = (
            {"on_sent": on_sent, "callback_state": callback_state}
            if on_sent is not None else None
        )

        try:
            session = await self._http()
            async with session.post(
                self.endpoint,
                headers=headers,
                json=payload,
                allow_redirects=False,
                timeout=timeout,
                trace_request_ctx=trace_request_ctx,
            ) as response:
                response_status = response.status
                response_received_at = datetime.now(timezone.utc)
                response_headers = {
                    name: value
                    for name, value in response.headers.items()
                    if name.lower() in {
                        "content-type", "retry-after", "x-ratelimit-reset", "mcp-session-id", "date", "content-length",
                    }
                }
                body, too_large = await _read_bounded(response.content)
                if too_large:
                    return _Exchange(
                        response_status, response_headers, None, "body_too_large", callback_state["called"],
                        response_received_at, request_id=request_id, phase=method,
                    )
                messages = await asyncio.to_thread(_parse_messages, body)
                if request_id is not None and not messages and body:
                    # Preserve malformed/opaque errors for the service mapper.
                    return _Exchange(
                        response_status, response_headers, body, "", callback_state["called"],
                        response_received_at, request_id=request_id, phase=method,
                    )
                return _Exchange(
                    response_status, response_headers, body, "", callback_state["called"],
                    response_received_at, messages, request_id, method,
                )
        except asyncio.CancelledError:
            return _Exchange(
                response_status, response_headers, None, "cancelled", callback_state["called"],
                response_received_at, phase=method,
            )
        except asyncio.TimeoutError:
            return _Exchange(
                response_status, response_headers, None, "timeout", callback_state["called"],
                response_received_at, phase=method,
            )
        except (aiohttp.ClientError, OSError, RuntimeError, ValueError):
            return _Exchange(
                response_status, response_headers, None, "connection", callback_state["called"],
                response_received_at, phase=method,
            )

    async def _initialize(self, *, deadline: float) -> _Exchange:
        request_id = self._next_id + 1
        exchange = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "avibe", "version": self.client_version},
            },
            deadline=deadline,
        )
        if exchange.status is not None:
            session_id = next((value for name, value in exchange.headers.items() if name.lower() == MCP_SESSION_HEADER.lower()), None)
            if session_id:
                self._session_id = session_id
        message = _matching_message(exchange.messages, request_id)
        result = message.get("result") if isinstance(message, Mapping) else None
        protocol = result.get("protocolVersion") if isinstance(result, Mapping) else None
        if exchange.status != 200 or not isinstance(result, Mapping) or protocol != MCP_PROTOCOL_VERSION:
            self._clear_session()
            if exchange.status == 200 and exchange.messages and not exchange.error:
                return _Exchange(
                    exchange.status, exchange.headers, exchange.body, "connection", exchange.sent,
                    exchange.received_at, exchange.messages, exchange.request_id, exchange.phase,
                )
            return exchange
        self._protocol_version = protocol
        initialized = await self._request(
            "notifications/initialized",
            None,
            deadline=deadline,
            notification=True,
        )
        if _session_not_found(initialized):
            self._clear_session()
        if initialized.error or initialized.status is None or initialized.status >= 400:
            self._clear_session()
            return initialized
        return exchange

    async def _list_tools(self, *, deadline: float) -> _Exchange | None:
        request_id = self._next_id + 1
        exchange = await self._request("tools/list", {}, deadline=deadline)
        self._observe_notifications(exchange.messages)
        if _session_not_found(exchange):
            return exchange
        message = _matching_message(exchange.messages, request_id)
        result = message.get("result") if isinstance(message, Mapping) else None
        raw_tools = result.get("tools") if isinstance(result, Mapping) else None
        if exchange.status != 200 or not isinstance(raw_tools, list):
            return exchange
        tools = {
            str(tool.get("name")): tool
            for tool in raw_tools
            if isinstance(tool, Mapping) and isinstance(tool.get("name"), str)
        }
        self._tools = tools
        return None

    async def _ensure_ready(self, *, deadline: float) -> _Exchange | None:
        """Single-flight only the session setup and cached schema refresh."""

        async with self._setup_lock:
            if self._session_id is None:
                initialized = await self._initialize(deadline=deadline)
                if initialized.error or initialized.status != 200 or self._session_id is None:
                    return initialized
            if self._tools is None:
                return await self._list_tools(deadline=deadline)
        return None

    async def _clear_if_stale(self, session_id: str | None) -> None:
        async with self._setup_lock:
            if session_id is None or self._session_id == session_id:
                self._clear_session()

    @staticmethod
    def _schema_changed(exchange: _Exchange) -> bool:
        message = _matching_message(exchange.messages, exchange.request_id) if exchange.request_id is not None else None
        error = message.get("error") if isinstance(message, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        return code in {-32601, -32602}

    @staticmethod
    def _adapt_arguments(properties: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        # The live schema is authoritative. Unknown catalog defaults never cross
        # the MCP boundary, while a missing required search control is rejected
        # before any tools/call bytes are sent.
        return {name: value for name, value in arguments.items() if name in properties}

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        on_sent: Callable[[], Awaitable[None] | None] | None = None,
    ) -> SearchResponse:
        """Call one live-schema MCP tool; retry one stale session before the call."""

        deadline = time.monotonic() + self.timeout_seconds
        last: _Exchange | None = None
        sent_callback_called = False

        async def admit_once() -> None:
            nonlocal sent_callback_called
            if sent_callback_called or on_sent is None:
                return
            admission = on_sent()
            if inspect.isawaitable(admission):
                await admission
            sent_callback_called = True

        for _ in range(2):
            setup_failure = await self._ensure_ready(deadline=deadline)
            if setup_failure is not None:
                last = setup_failure
                if _session_not_found(setup_failure):
                    await self._clear_if_stale(self._session_id)
                    continue
                return await self._as_search_response(setup_failure)
            tool = (self._tools or {}).get(tool_name)
            properties = (
                _tool_properties(
                    tool,
                    require_search_controls=tool_name == KEYLESS_EXA_SEARCH_TOOL,
                    require_fetch_controls=tool_name == KEYLESS_EXA_FETCH_TOOL,
                )
                if isinstance(tool, Mapping) else None
            )
            if properties is None:
                return SearchResponse(None, {"x-avibe-mcp-phase": "tools_list"}, None, "invalid_endpoint")
            adapted = self._adapt_arguments(properties, arguments)
            if adapted is None:
                return SearchResponse(None, {"x-avibe-mcp-phase": "tools_list"}, None, "invalid_endpoint")
            if tool_name == KEYLESS_EXA_FETCH_TOOL:
                required = tool["inputSchema"].get("required", [])
                if (
                    not isinstance(required, list)
                    or not all(isinstance(name, str) and name in adapted for name in required)
                    or not _REQUIRED_FETCH_FIELDS.issubset(adapted)
                ):
                    return SearchResponse(None, {"x-avibe-mcp-phase": "tools_list"}, None, "invalid_endpoint")
            request_session = self._session_id
            called = await self._request(
                "tools/call",
                {"name": tool_name, "arguments": adapted},
                deadline=deadline,
                on_sent=admit_once,
            )
            last = called
            self._observe_notifications(called.messages)
            if _session_not_found(called):
                await self._clear_if_stale(request_session)
                continue
            if self._schema_changed(called):
                self._tools = None
            return await self._as_search_response(called)
        return await self._as_search_response(last or _Exchange(None, {}, None, "connection", phase="tools_call"))

    async def search(
        self,
        arguments: Mapping[str, Any],
        *,
        on_sent: Callable[[], Awaitable[None] | None] | None = None,
    ) -> SearchResponse:
        return await self.call_tool(KEYLESS_EXA_SEARCH_TOOL, arguments, on_sent=on_sent)

    async def fetch(
        self,
        arguments: Mapping[str, Any],
        *,
        on_sent: Callable[[], Awaitable[None] | None] | None = None,
    ) -> SearchResponse:
        return await self.call_tool(KEYLESS_EXA_FETCH_TOOL, arguments, on_sent=on_sent)

    @staticmethod
    async def _as_search_response(exchange: _Exchange) -> SearchResponse:
        body = exchange.body
        error = exchange.error
        if exchange.request_id is not None:
            message = _matching_message(exchange.messages, exchange.request_id)
            if message is not None:
                body = await asyncio.to_thread(
                    lambda: json.dumps(message, separators=(",", ":")).encode("utf-8")
                )
            elif exchange.messages:
                body = None
                error = error or "connection"
        # Normalize SSE to the matching JSON-RPC envelope so the core mapper has
        # one bounded representation for both MCP transport modes.
        elif exchange.messages:
            body = await asyncio.to_thread(
                lambda: json.dumps(exchange.messages[-1], separators=(",", ":")).encode("utf-8")
            )
        if not error and exchange.status is not None and 300 <= exchange.status < 400:
            error = "redirect"
        headers = dict(exchange.headers)
        if exchange.phase is not None:
            headers["x-avibe-mcp-phase"] = exchange.phase
        return SearchResponse(
            exchange.status,
            headers,
            body,
            error,
            on_sent_fired=exchange.sent,
            response_received_at=exchange.received_at,
        )


__all__ = ["KEYLESS_EXA_ENDPOINT", "KEYLESS_EXA_SEARCH_TOOL", "KEYLESS_EXA_FETCH_TOOL", "KeylessExaMcpClient"]
