"""The Controller-owned MCP runtime: generation, management and tool calls.

All MCP SDK sessions run on one dedicated thread with its own event loop, so the
SDK's synchronous work never blocks the Controller loop. Artifact I/O and schema
work run on that loop's worker threads, and responses are serialized here, so the
Controller loop only hands IPC requests over (``submit``).

Management operations are serialized per package. Tool calls use one connection
per (revision, caller session); context-free calls are one-shot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import anyio

from core.mcp_hub import commands as mcp_commands
from core.mcp_hub import native, repository
from core.mcp_hub import oauth as mcp_oauth
from core.mcp_hub.definition import HTTP, STDIO, definition_from_provider, normalize_description
from core.mcp_hub.errors import McpHubError

logger = logging.getLogger(__name__)

STDIO_PROCESS_CAP = 8
IDLE_CLOSE_SECONDS = 600
CALL_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 60
GENERATION_TIMEOUT_SECONDS = 300
OAUTH_LOGIN_TIMEOUT_SECONDS = 600
TOOLS_PAGE_CAP = 100
# Results above this are not returned (the call still completed; see envelope).
RESULT_BYTE_CAP = 4 * 1024 * 1024
ENVELOPE_SCHEMA_VERSION = 1

# The SDK's default client callbacks reject with these messages. A call that
# fails through one may already have acted, so it is an unknown outcome.
# Codes the SDK synthesizes locally when no server answer arrived (the
# connection closed or its own timeout fired): the tool may have acted.
_CLIENT_SIDE_CODES = {-32000, -32001}  # CONNECTION_CLOSED, REQUEST_TIMEOUT
_CALLBACK_REJECTIONS = {"Sampling not supported", "Elicitation not supported", "List roots not supported"}


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _http_session_lost(exc: Exception) -> bool:
    """Recognize transport-level MCP session loss, not tool error text."""

    error = getattr(exc, "error", None)
    message = str(getattr(error, "message", "") or exc)
    return message == "Session terminated"


def _client_for(definition: dict[str, Any], stack: contextlib.AsyncExitStack, mode: str = "auto", auth=None):
    """Build an SDK client for a revision snapshot (never the edited top-level file)."""

    from mcp import Client, StdioServerParameters
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.types import Implementation

    transport = definition["transport"]
    info = Implementation(name="avibe", version="1")
    if transport["type"] == STDIO:
        params = StdioServerParameters(
            command=transport["command"], args=list(transport.get("args") or []),
            env=dict(transport.get("env") or {}) or None, cwd=transport.get("cwd"),
        )
        return Client(params, client_info=info, mode=mode)
    headers = dict(transport.get("headers") or {})
    http_client = create_mcp_http_client(headers=headers, auth=auth)
    stack.push_async_callback(http_client.aclose)
    return Client(streamable_http_client(transport["url"], http_client=http_client), client_info=info, mode=mode)


def _closed_during_handshake(exc: BaseException) -> bool:
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    if isinstance(exc, MCPError):
        return exc.error.code == CONNECTION_CLOSED
    # Exception groups from the SDK's task groups (no builtin name before 3.11).
    return any(_closed_during_handshake(item) for item in getattr(exc, "exceptions", ()))


async def _enter_client(definition: dict[str, Any], stack: contextlib.AsyncExitStack, auth=None):
    """Enter a client; retry once with the initialize handshake for strict pre-2026 servers.

    The default probe sends ``server/discover`` first, and strict legacy servers
    (e.g. rmcp) exit on it. The retry happens before any request is dispatched.
    """

    try:
        return await stack.enter_async_context(_client_for(definition, stack, auth=auth))
    except Exception as exc:
        if not _closed_during_handshake(exc):
            raise
    return await stack.enter_async_context(_client_for(definition, stack, mode="legacy", auth=auth))


def _first_sentence(text: Optional[str]) -> Optional[str]:
    """The server instructions up to the first sentence end or line break."""

    if not isinstance(text, str):
        return None
    text = text.strip()
    ends = [index + 1 for index in (text.find(". "), text.find(".\n"), text.find("。")) if index >= 0]
    if "\n" in text:
        ends.append(text.index("\n"))
    return text[: min(ends)] if ends else text


async def _list_all_tools(client) -> list:
    tools: list = []
    cursor: Optional[str] = None
    for _ in range(TOOLS_PAGE_CAP):
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools
    raise McpHubError("connection", f"tools/list did not finish within {TOOLS_PAGE_CAP} pages.")


class _Connection:
    """One open SDK client, held by a task that enters and exits it."""

    def __init__(self, runtime: "McpRuntime", key: tuple, definition: dict[str, Any]) -> None:
        self.runtime = runtime
        self.key = key
        self.definition = definition
        self.stdio = definition["transport"]["type"] == STDIO
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.retired = False
        self.client = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task: Optional[asyncio.Task] = None

    async def open(self) -> None:
        if self.stdio:
            self.runtime.acquire_stdio_slot()
        self._task = asyncio.create_task(self._run())
        try:
            with anyio.fail_after(CONNECT_TIMEOUT_SECONDS):
                self.client = await asyncio.shield(self._ready)
        except TimeoutError:
            await self.close()
            raise McpHubError("timeout", "Connecting to the MCP server timed out.") from None
        except McpHubError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            hub = _hub_error(exc)
            if hub is not None:
                raise hub from None
            raise McpHubError("connection", f"Could not connect to the MCP server: {_describe(exc)}") from None

    async def _run(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                client = await _enter_client(self.definition, stack, auth=await self.runtime._oauth_provider(self.definition))
                # The SDK learns every tool's output schema before the first call.
                await _list_all_tools(client)
                self._ready.set_result(client)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported through _ready
            if not self._ready.done():
                self._ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            elif not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
        finally:
            self.retired = True
            if self.stdio:
                self.runtime.release_stdio_slot()
            self.runtime.forget(self)

    @property
    def alive(self) -> bool:
        return not self.retired and self._task is not None and not self._task.done()

    async def close(self) -> None:
        self.retired = True
        self._stop.set()
        if self._task is not None:
            try:
                with anyio.fail_after(10):
                    await asyncio.shield(self._task)
            except BaseException:  # noqa: BLE001 - best-effort close
                self._task.cancel()


def _hub_error(exc: BaseException, *, _seen: Optional[set[int]] = None) -> Optional[McpHubError]:
    seen = _seen if _seen is not None else set()
    marker = id(exc)
    if marker in seen:
        return None
    seen.add(marker)
    if isinstance(exc, McpHubError):
        return exc
    for item in getattr(exc, "exceptions", ()) or ():
        if isinstance(item, BaseException):
            nested = _hub_error(item, _seen=seen)
            if nested is not None:
                return nested
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return _hub_error(cause, _seen=seen) if isinstance(cause, BaseException) else None


def _http_status(exc: BaseException, *, _seen: Optional[set[int]] = None) -> Optional[int]:
    """HTTP status on this exception or its cause chain, not from error text."""
    seen = _seen if _seen is not None else set()
    marker = id(exc)
    if marker in seen:
        return None
    seen.add(marker)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    for item in getattr(exc, "exceptions", ()) or ():
        if isinstance(item, BaseException):
            nested = _http_status(item, _seen=seen)
            if nested is not None:
                return nested
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return _http_status(cause, _seen=seen) if isinstance(cause, BaseException) else None


def _explicit_authorization(definition: dict[str, Any]) -> bool:
    headers = (definition.get("transport") or {}).get("headers") or {}
    return any(key.lower() == "authorization" for key in headers)


def _oauth_eligible(definition: dict[str, Any]) -> bool:
    return (definition.get("transport") or {}).get("type") == HTTP and not _explicit_authorization(definition)


def _oauth_challenge(exc: BaseException, *, _seen: Optional[set[int]] = None) -> bool:
    """Recognize a protected-resource OAuth challenge through exception causes."""
    seen = _seen if _seen is not None else set()
    marker = id(exc)
    if marker in seen:
        return False
    seen.add(marker)
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 401:
        try:
            from mcp.client.auth.utils import extract_resource_metadata_from_www_auth

            if extract_resource_metadata_from_www_auth(response):
                return True
        except Exception:
            headers = getattr(response, "headers", None) or {}
            header = ""
            if hasattr(headers, "get"):
                header = str(headers.get("WWW-Authenticate") or headers.get("www-authenticate") or "")
            if "resource_metadata=" in header:
                return True
    for item in getattr(exc, "exceptions", ()) or ():
        if isinstance(item, BaseException) and _oauth_challenge(item, _seen=seen):
            return True
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return isinstance(cause, BaseException) and _oauth_challenge(cause, _seen=seen)


def _http_excerpt(response: object) -> str:
    status = getattr(response, "status_code", None)
    if status is None:
        return ""
    text = ""
    for attr in ("reason_phrase", "reason"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    body = getattr(response, "text", None)
    if isinstance(body, str) and body.strip() and body.strip() != text:
        snippet = " ".join(body.strip().split())
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        text = f"{text}: {snippet}" if text else snippet
    return f"HTTP {status}: {text}".rstrip(": ")


def _describe(exc: BaseException, *, _seen: Optional[set[int]] = None) -> str:
    seen = _seen if _seen is not None else set()
    marker = id(exc)
    if marker in seen:
        return ""
    seen.add(marker)
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        return "; ".join(part for part in (_describe(item, _seen=seen) for item in nested) if part)
    for candidate in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if candidate is None:
            continue
        http = _http_excerpt(getattr(candidate, "response", None) or candidate)
        if http:
            return http
    error = getattr(exc, "error", None)
    if error is not None:
        message = getattr(error, "message", None) or str(error)
        data = getattr(error, "data", None)
        extra = ""
        if isinstance(data, dict):
            extra = str(data.get("message") or data.get("error") or "").strip()
        if extra and extra not in str(message):
            return f"{message}: {extra}"
        if message and message != "Server returned an error response":
            return str(message)
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if isinstance(cause, BaseException):
        nested_text = _describe(cause, _seen=seen)
        if nested_text:
            return nested_text
    return f"{type(exc).__name__}: {exc}"


class McpRuntime:
    """Owns the runtime thread, package locks, and live connections."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._package_locks: dict[Any, asyncio.Lock] = {}
        self._connections: dict[tuple, _Connection] = {}
        self._stdio_open = 0
        self._stdio_lock: Optional[asyncio.Lock] = None
        self._stdio_free: Optional[asyncio.Condition] = None
        self._sweeper: Optional[asyncio.Task] = None
        self._discover_lock = asyncio.Lock()
        self._discover_task: Optional[asyncio.Task] = None
        self._oauth_providers: dict[str, Any] = {}
        self._pending_logins: dict[str, dict[str, Any]] = {}
        self._oauth_tasks: dict[str, asyncio.Task] = {}
        self._oauth_logins: set[str] = set()
        self._oauth_provider_locks: dict[str, asyncio.Lock] = {}

    # -- thread ---------------------------------------------------------------

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, name="mcp-runtime", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
                asyncio.run_coroutine_threadsafe(self._start_sweeper(), loop)
            return self._loop

    async def submit(self, operation: str, payload: dict[str, Any]) -> tuple[int, bytes]:
        """Hand one request to the runtime loop; returns ``(status, json_bytes)``."""

        loop = self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(self.handle(operation, payload), loop)
        return await asyncio.wrap_future(future)

    def stop(self, timeout: float = 5.0) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), loop).result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
            logger.debug("MCP runtime close skipped: %s", exc)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._loop = self._thread = None

    # -- bookkeeping ----------------------------------------------------------

    def _stdio_sync(self) -> tuple[asyncio.Lock, asyncio.Condition]:
        if self._stdio_lock is None:
            self._stdio_lock = asyncio.Lock()
            self._stdio_free = asyncio.Condition(self._stdio_lock)
        assert self._stdio_free is not None
        return self._stdio_lock, self._stdio_free

    def acquire_stdio_slot(self) -> None:
        if self._stdio_open >= STDIO_PROCESS_CAP:
            raise McpHubError(
                "capacity",
                f"At most {STDIO_PROCESS_CAP} MCP stdio server processes may run at once; retry later.",
            )
        self._stdio_open += 1

    async def acquire_stdio_slot_wait(self) -> None:
        """Wait for a stdio slot instead of failing with ``capacity`` (batch paths)."""

        _lock, free = self._stdio_sync()
        async with free:
            while self._stdio_open >= STDIO_PROCESS_CAP:
                await free.wait()
            self._stdio_open += 1

    def release_stdio_slot(self) -> None:
        self._stdio_open = max(0, self._stdio_open - 1)
        free = self._stdio_free
        if free is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _notify() -> None:
            async with free:
                free.notify()

        loop.create_task(_notify())

    def forget(self, connection: _Connection) -> None:
        if self._connections.get(connection.key) is connection:
            del self._connections[connection.key]

    @staticmethod
    def _oauth_cache_identity(definition: dict[str, Any]) -> tuple[Any, Any, Any]:
        transport = definition.get("transport") or {}
        oauth = definition.get("oauth") or {}
        return (transport.get("url"), oauth.get("clientId"), oauth.get("scopes"))

    async def _oauth_provider(self, definition: dict[str, Any]):
        transport = definition.get("transport") or {}
        if not _oauth_eligible(definition):
            return None
        name = definition["name"]
        identity = self._oauth_cache_identity(definition)
        lock = self._oauth_provider_locks.setdefault(name, asyncio.Lock())
        async with lock:
            provider = self._oauth_providers.get(name)
            if provider is not None and getattr(provider, "_cache_identity", None) != identity:
                self._oauth_providers.pop(name, None)
                provider = None
            if provider is not None:
                return provider
            provider = await self._make_call_time_provider(name, definition, transport)
            provider._cache_identity = identity
            self._oauth_providers[name] = provider
            return provider

    async def _make_call_time_provider(self, name: str, definition: dict[str, Any], transport: dict[str, Any]):
        async def _redirect(_url: str) -> None:
            raise McpHubError("auth_required", "MCP OAuth login is required.", hint=f"vibe mcp {name} --login")
        async def _callback():
            raise McpHubError("auth_required", "MCP OAuth login is required.")
        raw = await asyncio.to_thread(mcp_oauth.read_state, name, transport["url"])
        uris = (raw.get("client") or {}).get("redirect_uris") or []
        redirect_uri = str(uris[0]) if uris else "http://127.0.0.1/api/mcp/oauth/callback"
        return mcp_oauth.make_provider(
            name,
            definition,
            redirect_uri,
            redirect_handler=_redirect,
            callback_handler=_callback,
            filter_redirect=False,
        )

    def _lock(self, name: str) -> asyncio.Lock:
        return self._package_locks.setdefault(name, asyncio.Lock())

    async def _start_sweeper(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep_forever())

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(30)
            cutoff = time.monotonic() - IDLE_CLOSE_SECONDS
            for connection in list(self._connections.values()):
                if not connection.lock.locked() and connection.last_used < cutoff:
                    await connection.close()

    async def _retire_package(self, name: str, *, keep_revision: Optional[str] = None) -> None:
        """Close idle superseded connections now; busy ones close when their call settles."""

        for connection in list(self._connections.values()):
            if connection.key[0] != name or connection.key[1] == keep_revision:
                continue
            connection.retired = True
            self.forget(connection)
            if not connection.lock.locked():
                await connection.close()

    async def _close_all(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        task = self._discover_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for connection in list(self._connections.values()):
            await connection.close()

    # -- dispatch -------------------------------------------------------------

    async def handle(self, operation: str, payload: dict[str, Any]) -> tuple[int, bytes]:
        handlers = {
            "add": self._add,
            "refresh": self._refresh,
            "discover": self._discover_all,
            "remove": self._remove,
            "call": self._call,
            "oauth_start": self._oauth_start,
            "oauth_complete": self._oauth_complete,
            "oauth_callback": self._oauth_callback,
            "oauth_logout": self._oauth_logout,
        }
        handler = handlers.get(operation)
        if handler is None:
            return 400, _dumps({"ok": False, "error": {"type": "usage", "message": "Unknown MCP operation."}})
        try:
            return 200, await handler(payload)
        except McpHubError as exc:
            return 200, _dumps({"ok": False, "error": exc.payload()})
        except Exception:
            logger.exception("MCP operation %s failed", operation)
            return 500, _dumps({"ok": False, "error": {"type": "internal", "message": "MCP operation failed."}})

    # -- management -----------------------------------------------------------

    async def _add(self, payload: dict[str, Any]) -> bytes:
        replace = payload.get("replace") is True
        definitions = [
            definition_from_provider(item.get("name"), item.get("server"))
            for item in payload.get("servers") or []
        ]
        if not definitions:
            raise McpHubError("usage", "No servers to save.", usage=True)
        results = await asyncio.gather(*(self._add_one(item, replace) for item in definitions))
        added = [item for item in results if item.get("status") == "added"]
        unchanged = [item for item in results if item.get("status") == "unchanged"]
        failed = [item for item in results if item.get("status") == "failed"]
        ok = bool(added or unchanged)
        body: dict[str, Any] = {"ok": ok, "added": added, "unchanged": unchanged, "failed": failed}
        if len(added) == 1:
            body.update({k: added[0][k] for k in ("name", "revision", "toolCount") if k in added[0]})
        if failed and not added:
            summary = "; ".join(f"{item['name']}: {item.get('error') or 'failed'}" for item in failed)
            body["error"] = {"type": "add_failed", "message": summary, "failed": failed}
        elif failed:
            summary = "; ".join(f"{item['name']}: {item.get('error') or 'failed'}" for item in failed)
            body["error"] = {"type": "add_partial", "message": summary, "failed": failed}
        return _dumps(body)

    async def _add_one(self, definition: dict[str, Any], replace: bool) -> dict[str, Any]:
        name = definition["name"]
        async with self._lock(name):
            previous = None
            was_hidden = False
            saved = False
            try:
                previous = await asyncio.to_thread(repository.read_avibe_definition, name)
                was_hidden = name in await asyncio.to_thread(repository.hidden_names)
                outcome = await asyncio.to_thread(self._save_one, definition, replace)
                if outcome == "unchanged":
                    return {"name": name, "status": "unchanged"}
                if outcome == "exists":
                    return {
                        "name": name,
                        "status": "failed",
                        "error": f"{name} already exists with a different definition.",
                        "type": "exists",
                    }
                saved = True
                body = json.loads(await self._refresh({"name": name, "waitStdio": True, "locked": True}))
                if body.get("ok"):
                    result = {
                        "name": name,
                        "status": "added",
                        "revision": body.get("revision"),
                        "toolCount": body.get("toolCount"),
                    }
                    if body.get("auth") == "needs_login":
                        result["auth"] = "needs_login"
                    return result
                error = (body.get("error") or {}).get("message") or "failed"
            except McpHubError as cop:
                error = cop.message
            except Exception as cop:  # noqa: BLE001
                error = str(cop)
            if saved:
                await asyncio.to_thread(repository.rollback_avibe_definition, name, previous, hidden=was_hidden)
            return {"name": name, "status": "failed", "error": error}

    @staticmethod
    def _comparable_definition(definition: dict[str, Any]) -> dict[str, Any]:
        transport = dict(definition.get("transport") or {})
        if transport.get("type") == STDIO and not transport.get("cwd"):
            transport["cwd"] = repository.default_cwd()
        return {
            "name": definition.get("name"),
            "transport": transport,
        }

    @staticmethod
    def _save_one(definition: dict[str, Any], replace: bool) -> str:
        name = definition["name"]
        if definition.get("transport", {}).get("type") == STDIO and not definition["transport"].get("cwd"):
            definition = json.loads(json.dumps(definition))
            definition["transport"]["cwd"] = repository.default_cwd()
        existing = None
        try:
            existing = repository.read_definition(name)
        except McpHubError:
            existing = repository.read_avibe_definition(name)
        published = repository.read_published(name)
        exists = existing is not None or published is not None
        listed = name not in repository.hidden_names() and published is not None
        if exists and not replace:
            published_definition = published["definition"] if published is not None else None
            if (
                listed
                and existing is not None
                and published_definition is not None
                and McpRuntime._comparable_definition(existing) == McpRuntime._comparable_definition(definition)
                and McpRuntime._comparable_definition(published_definition) == McpRuntime._comparable_definition(definition)
            ):
                return "unchanged"
            if listed:
                return "exists"
        repository.write_definition(definition)
        return name

    async def _refresh(self, payload: dict[str, Any]) -> bytes:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise McpHubError("usage", "name is required.", usage=True)
        if payload.get("locked"):
            return await self._refresh_locked(name, wait_stdio=payload.get("waitStdio") is True, unlist_on_failure=False)
        async with self._lock(name):
            return await self._refresh_locked(name, wait_stdio=payload.get("waitStdio") is True, unlist_on_failure=True)

    async def _refresh_locked(self, name: str, *, wait_stdio: bool, unlist_on_failure: bool = False) -> bytes:
        definition = await asyncio.to_thread(repository.read_definition, name)
        snapshot = json.loads(json.dumps(definition))

        if snapshot["transport"]["type"] == STDIO and not snapshot["transport"].get("cwd"):
            snapshot["transport"]["cwd"] = await asyncio.to_thread(repository.default_cwd)
        stdio = snapshot["transport"]["type"] == STDIO
        deadline = time.monotonic() + (
            OAUTH_LOGIN_TIMEOUT_SECONDS if name in self._oauth_logins else GENERATION_TIMEOUT_SECONDS
        )
        acquired = False
        try:
            if stdio:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                with anyio.fail_after(remaining):
                    if wait_stdio:
                        await self.acquire_stdio_slot_wait()
                    else:
                        self.acquire_stdio_slot()
                acquired = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with anyio.fail_after(remaining):
                tools, server_info, instructions = await self._discover(snapshot)
        except TimeoutError:
            if unlist_on_failure:
                await asyncio.to_thread(repository.unlist, name)
            raise McpHubError("timeout", "Refresh timed out.") from None
        except McpHubError as cop:
            if _oauth_eligible(definition) and cop.type in {"auth_required", "oauth"} and name not in self._oauth_logins:
                revision = await asyncio.to_thread(self._publish_needs_login, name, definition)
                return _dumps({"ok": True, "name": name, "revision": revision, "toolCount": 0, "auth": "needs_login"})
            if unlist_on_failure:
                await asyncio.to_thread(repository.unlist, name)
            raise
        except Exception as cop:
            hub = _hub_error(cop)
            if _oauth_eligible(definition) and name not in self._oauth_logins and (
                (hub is not None and hub.type in {"auth_required", "oauth"}) or _oauth_challenge(cop)
            ):
                revision = await asyncio.to_thread(self._publish_needs_login, name, definition)
                return _dumps({"ok": True, "name": name, "revision": revision, "toolCount": 0, "auth": "needs_login"})
            if unlist_on_failure:
                await asyncio.to_thread(repository.unlist, name)
            if hub is not None:
                raise hub from None
            raise McpHubError("connection", f"Refresh failed: {_describe(cop)}") from None
        finally:
            if acquired:
                self.release_stdio_slot()
        revision = await asyncio.to_thread(self._publish, name, snapshot, definition, tools, server_info, instructions)
        await self._retire_package(name, keep_revision=revision)
        return _dumps({"ok": True, "name": name, "revision": revision, "toolCount": len(tools)})

    async def _discover_all(self, payload: dict[str, Any]) -> bytes:
        _ = payload
        async with self._discover_lock:
            task = self._discover_task
            if task is None or task.done():
                task = asyncio.create_task(self._run_discover_pass())
                self._discover_task = task
        results = await task
        return _dumps({"ok": True, "results": results})

    async def _run_discover_pass(self) -> list[dict[str, Any]]:
        native_servers = await asyncio.to_thread(native.read_all)
        names = await asyncio.to_thread(repository._candidate_names, native_servers)
        def _stale() -> list[str]:
            leftover = []
            for name in repository._generated_names():
                if name in names:
                    continue
                try:
                    if repository.read_avibe_definition(name) is None:
                        leftover.append(name)
                except McpHubError:
                    leftover.append(name)
            return leftover

        stale = await asyncio.to_thread(_stale)
        for name in stale:
            await asyncio.to_thread(repository.delete_artifacts, name)
        async def one(name: str) -> dict[str, Any]:
            async with self._lock(name):
                try:
                    body = json.loads(await self._refresh({"name": name, "waitStdio": True, "locked": True}))
                except McpHubError as cop:
                    logger.warning("MCP discover %s failed: %s", name, cop.message)
                    await asyncio.to_thread(repository.unlist, name)
                    return {"name": name, "status": "failed", "error": cop.message}
                except Exception as cop:  # noqa: BLE001
                    logger.warning("MCP discover %s failed: %s", name, cop)
                    await asyncio.to_thread(repository.unlist, name)
                    return {"name": name, "status": "failed", "error": str(cop)}
                if body.get("ok"):
                    result = {"name": name, "status": "registered", "toolCount": body.get("toolCount")}
                    if body.get("auth") == "needs_login":
                        result["auth"] = "needs_login"
                    return result
                error = (body.get("error") or {}).get("message") or "failed"
                logger.warning("MCP discover %s failed: %s", name, error)
                await asyncio.to_thread(repository.unlist, name)
                return {"name": name, "status": "failed", "error": error}

        return list(await asyncio.gather(*(one(name) for name in sorted(names))))

    async def _discover(self, snapshot: dict[str, Any]) -> tuple[list, Any, Optional[str]]:
        async with contextlib.AsyncExitStack() as stack:
            client = await _enter_client(snapshot, stack, auth=await self._oauth_provider(snapshot))
            return await _list_all_tools(client), client.server_info, client.instructions

    @staticmethod
    def _publish_needs_login(name: str, definition: dict[str, Any]) -> str:
        return repository.publish(name, definition=definition, manifest={"description": "", "auth": "needs_login"}, tools=[], commands=[])

    async def _publish_call_time_needs_login(self, name: str, revision: str, definition: dict[str, Any]) -> None:
        async with self._lock(name):
            def _maybe() -> None:
                published = repository.read_published(name)
                if published is None or published.get("revision") != revision:
                    return
                if (published.get("manifest") or {}).get("auth") == "needs_login":
                    return
                self._publish_needs_login(name, definition)

            await asyncio.to_thread(_maybe)
    @staticmethod
    def _publish(name, snapshot, definition, tools, server_info, instructions=None) -> str:
        descriptors, diagnostics, seen = [], [], set()
        for tool in tools:
            descriptor = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            task_support = (descriptor.get("execution") or {}).get("taskSupport")
            if task_support == "required":
                diagnostics.append(f"Skipped {tool.name}: it requires task-augmented execution.")
                continue
            if tool.name in seen:
                diagnostics.append(f"Skipped a duplicate listing of {tool.name}.")
                continue
            seen.add(tool.name)
            descriptors.append(descriptor)
        descriptors.sort(key=lambda item: item["name"])
        manifest: dict[str, Any] = {
            "description": normalize_description(
                getattr(server_info, "description", None) or _first_sentence(instructions)
            ),
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        version = getattr(server_info, "version", None)
        if _oauth_eligible(snapshot):
            manifest["auth"] = "ok"
        if version:
            manifest["serverInfo"] = {"version": str(version)}
        if diagnostics:
            manifest["diagnostics"] = diagnostics
        revision = repository.publish(
            name,
            definition=snapshot,
            manifest=manifest,
            tools=descriptors,
            commands=[mcp_commands.build_command(item) for item in descriptors],
        )
        return revision

    async def _oauth_start(self, payload: dict[str, Any]) -> bytes:
        name = payload.get("name")
        definition = await asyncio.to_thread(repository.read_definition, name)
        transport = definition.get("transport") or {}
        if not _oauth_eligible(definition):
            raise McpHubError("usage", "OAuth login is only available for HTTP MCP servers without an Authorization header.", usage=True)
        port = payload.get("port")
        mode, redirect_uri = mcp_oauth.redirect_mode(
            payload.get("origin"),
            cloud_origin=payload.get("cloudOrigin"),
            cli=payload.get("cli") is True,
            port=int(port) if port is not None else None,
        )
        async with self._lock(name):
            previous = self._oauth_tasks.pop(name, None)
            for state, item in list(self._pending_logins.items()):
                if item.get("name") == name:
                    self._pending_logins.pop(state, None)
                    future = item.get("callback")
                    if future is not None and not future.done():
                        future.cancel()
            if previous is not None and not previous.done():
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            stored_client = (await asyncio.to_thread(mcp_oauth.read_state, name, transport["url"])).get("client")
            if isinstance(stored_client, dict) and redirect_uri not in [str(item) for item in stored_client.get("redirect_uris") or []]:
                await asyncio.to_thread(mcp_oauth.AvibeTokenStorage(name, transport["url"]).drop_client)
            url_future = asyncio.get_running_loop().create_future()
            callback_future = asyncio.get_running_loop().create_future()
            origin = str(payload.get("origin") or "").rstrip("/")
            record = {
                "name": name,
                "callback": callback_future,
                "url": url_future,
                "redirect_uri": redirect_uri,
                "mode": mode,
                "returnUrl": f"{origin}/skills?mcp={name}&oauth=success" if origin else f"/skills?mcp={name}&oauth=success",
            }

            async def redirect_handler(url: str) -> None:
                parsed = urlparse(url)
                query = parse_qs(parsed.query)
                state = query.get("state", [""])[0]
                if not state:
                    raise McpHubError("oauth", "OAuth authorization URL did not contain state.")
                self._pending_logins[state] = record
                if not url_future.done():
                    url_future.set_result(url)

            async def callback_handler():
                with anyio.fail_after(OAUTH_LOGIN_TIMEOUT_SECONDS):
                    return await callback_future

            login_provider = mcp_oauth.make_provider(name, definition, redirect_uri, redirect_handler=redirect_handler, callback_handler=callback_handler)
            login_provider._cache_identity = self._oauth_cache_identity(definition)
            cached = self._oauth_providers.get(name)
            self._oauth_providers[name] = login_provider
            self._oauth_logins.add(name)
            async def run_login() -> None:
                try:
                    await self._refresh_locked(name, wait_stdio=False, unlist_on_failure=False)
                except asyncio.CancelledError:
                    if not url_future.done():
                        url_future.set_exception(McpHubError("oauth", "OAuth authorization URL was not created."))
                    raise
                except Exception as cop:
                    if not url_future.done():
                        url_future.set_exception(cop)
                    logger.info("MCP OAuth login %s ended: %s", name, _describe(cop))
                    raise
                finally:
                    self._oauth_logins.discard(name)
                    if self._oauth_providers.get(name) is login_provider:
                        if cached is not None:
                            cached._initialized = False
                            self._oauth_providers[name] = cached
                        else:
                            self._oauth_providers.pop(name, None)
                    for state, item in list(self._pending_logins.items()):
                        if item is record:
                            self._pending_logins.pop(state, None)
            task = asyncio.create_task(run_login(), name=f"mcp-oauth-{name}")
            self._oauth_tasks[name] = task
            try:
                url = await asyncio.wait_for(url_future, 30)
            except TimeoutError:
                raise McpHubError("oauth", "OAuth authorization URL was not created.") from None
        return _dumps({"ok": True, "name": name, "authorizeUrl": url, "mode": mode})

    async def _oauth_callback(self, payload: dict[str, Any]) -> bytes:
        state = payload.get("state")
        item = self._pending_logins.get(state)
        if item is None or item["callback"].done():
            raise McpHubError("invalid_callback", "This OAuth callback is unknown or already used.")
        from mcp.shared.auth import AuthorizationCodeResult
        item["callback"].set_result(AuthorizationCodeResult(code=payload.get("code"), state=state, iss=payload.get("iss")))
        task = self._oauth_tasks.get(item.get("name"))
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), OAUTH_LOGIN_TIMEOUT_SECONDS)
            except Exception as cop:
                raise McpHubError("oauth", "MCP OAuth login failed.") from cop
        return _dumps({"ok": True, "redirect": item.get("returnUrl")})

    async def _oauth_complete(self, payload: dict[str, Any]) -> bytes:
        values = mcp_oauth.parse_callback_url(str(payload.get("callbackUrl") or ""))
        return await self._oauth_callback(values)

    async def _cancel_login(self, name: str) -> None:
        task = self._oauth_tasks.pop(name, None)
        for state, item in list(self._pending_logins.items()):
            if item.get("name") == name:
                self._pending_logins.pop(state, None)
                future = item.get("callback")
                if future is not None and not future.done():
                    future.cancel()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._oauth_logins.discard(name)
        self._oauth_providers.pop(name, None)

    async def _oauth_logout(self, payload: dict[str, Any]) -> bytes:
        name = payload.get("name")
        definition = await asyncio.to_thread(repository.read_definition, name)
        if not _oauth_eligible(definition):
            raise McpHubError("usage", "OAuth logout is only available for HTTP MCP servers without an Authorization header.", usage=True)
        async with self._lock(name):
            await self._cancel_login(name)
            await self._retire_package(name)
            for connection in list(self._connections.values()):
                if connection.key[0] == name:
                    connection.retired = True
                    self.forget(connection)
                    await connection.close()
            await asyncio.to_thread(self._publish_needs_login, name, definition)
            await asyncio.to_thread(mcp_oauth.delete_oauth, name)
        return _dumps({"ok": True, "name": name})

    async def _remove(self, payload: dict[str, Any]) -> bytes:
        name = payload.get("name")
        async with self._lock(name):
            await self._cancel_login(name)
            kind = await asyncio.to_thread(repository.remove_package, name)
            await self._retire_package(name)
            for connection in list(self._connections.values()):
                if connection.key[0] == name:
                    connection.retired = True
                    self.forget(connection)
                    await connection.close()
            await asyncio.to_thread(mcp_oauth.delete_oauth, name)
        return _dumps({"ok": True, "name": name, "removed": True, "kind": kind})



    @staticmethod
    def _admit(name: str, tool: str, arguments: Any) -> dict[str, Any]:
        published = repository.read_published(name)
        if published is None:
            repository.read_definition(name)
            raise McpHubError("not_generated", f"MCP server {name!r} has not been generated.", hint="vibe mcp --list")
        if published["manifest"].get("auth") == "needs_login":
            raise McpHubError("auth_required", "MCP OAuth login is required.", hint=f"vibe mcp {name} --login")
        descriptor = next((item for item in published["tools"] if item.get("name") == tool), None)
        if descriptor is None:
            raise McpHubError("not_found", f"{name} has no tool {tool!r}.", hint=f"vibe mcp {name} --list")
        if not isinstance(arguments, dict):
            raise McpHubError("invalid_arguments", "Tool arguments must be a JSON object.", usage=True)
        mcp_commands.validate_arguments(descriptor, arguments)
        return published

    async def _call(self, payload: dict[str, Any]) -> bytes:
        name, tool = payload.get("name"), payload.get("tool")
        arguments = payload.get("arguments") or {}
        session_id = payload.get("sessionId") or None
        try:
            published = await asyncio.to_thread(self._admit, name, tool, arguments)
        except McpHubError as exc:
            return _dumps(self._envelope(name, None, tool, error=exc))
        revision = published["revision"]
        definition = published["definition"]
        http = definition["transport"]["type"] == HTTP
        try:
            for attempt in range(2):
                try:
                    result = await self._call_once(name, revision, session_id, definition, tool, arguments)
                except _SessionLost:
                    if not http or attempt:
                        raise McpHubError("unknown_outcome", "The MCP HTTP session was lost; do not retry blindly.") from None
                    continue
                except Exception as cop:
                    hub = _hub_error(cop)
                    if hub is not None:
                        raise hub from None
                    raise
                return await asyncio.to_thread(self._result_envelope, name, revision, tool, result)
        except McpHubError as cop:
            if cop.type == "auth_required" and definition["transport"]["type"] == HTTP:
                with contextlib.suppress(Exception):
                    await self._publish_call_time_needs_login(name, revision, definition)
            return _dumps(self._envelope(name, revision, tool, error=cop))
        raise AssertionError("unreachable")

    async def _call_once(self, name, revision, session_id, definition, tool, arguments):
        key = (name, revision, session_id)
        if session_id:
            lock = self._package_locks.setdefault(("open",) + key, asyncio.Lock())
            async with lock:
                connection = self._connections.get(key)
                if connection is None or not connection.alive:
                    connection = _Connection(self, key, definition)
                    self._connections[key] = connection
                    await connection.open()
        else:
            connection = _Connection(self, key, definition)
            await connection.open()
        try:
            async with connection.lock:
                if not connection.alive or connection.client is None:
                    raise McpHubError("connection", "The MCP connection closed; retry the call.")
                connection.last_used = time.monotonic()
                outcome_unknown = True
                try:
                    with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                        result = await connection.client.call_tool(tool, arguments)
                    outcome_unknown = False
                    return result
                except TimeoutError:
                    raise McpHubError(
                        "unknown_outcome",
                        f"The tool did not answer within {CALL_TIMEOUT_SECONDS}s; it may have acted. Do not retry blindly.",
                    ) from None
                except Exception as exc:
                    if isinstance(exc, McpHubError):
                        raise
                    hub = _hub_error(exc)
                    if hub is not None:
                        raise hub from None
                    from mcp import MCPError

                    if _oauth_eligible(definition) and (
                        _http_status(exc) == 401 or (isinstance(exc, MCPError) and getattr(exc.error, "code", None) == 401)
                    ):
                        raise McpHubError(
                            "auth_required",
                            "MCP OAuth login is required.",
                            hint=f"vibe mcp {name} --login",
                        ) from None

                    if (
                        isinstance(exc, MCPError)
                        and exc.error.code not in _CLIENT_SIDE_CODES
                        and str(exc.error.message) not in _CALLBACK_REJECTIONS
                    ):
                        outcome_unknown = False
                        if definition["transport"]["type"] == HTTP and _http_session_lost(exc):
                            connection.retired = True
                            raise _SessionLost() from None
                        raise McpHubError("tool_error", f"The MCP server rejected the call: {exc.error.message}") from None
                    raise McpHubError(
                        "unknown_outcome",
                        f"The call failed after it was sent ({_describe(exc)}); it may have acted. Do not retry blindly.",
                    ) from None
                finally:
                    connection.last_used = time.monotonic()
                    if outcome_unknown:
                        connection.retired = True
        finally:
            if not session_id or connection.retired:
                self.forget(connection)
                await connection.close()

    @staticmethod
    def _envelope(name, revision, tool, *, result=None, error: Optional[McpHubError] = None) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "schemaVersion": ENVELOPE_SCHEMA_VERSION,
            "server": name,
            "revision": revision,
            "tool": tool,
            "status": "ok" if error is None else "error",
        }
        if result is not None:
            envelope["result"] = result
        if error is not None:
            envelope["error"] = error.payload()
        return envelope

    def _result_envelope(self, name, revision, tool, result) -> bytes:
        data = result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        data.setdefault("content", [])
        data.setdefault("isError", False)
        error = McpHubError("tool_error", "The tool reported an error.") if data["isError"] else None
        body = _dumps(self._envelope(name, revision, tool, result=data, error=error))
        if len(body) <= RESULT_BYTE_CAP:
            return body
        envelope = self._envelope(name, revision, tool, result={"isError": data["isError"]}, error=error)
        envelope["resultOmitted"] = "too_large"
        return _dumps(envelope)


class _SessionLost(Exception):
    """An HTTP ``Session not found`` before dispatch: reconnect and retry once."""
