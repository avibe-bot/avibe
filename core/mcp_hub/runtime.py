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

import anyio

from core.mcp_hub import commands as mcp_commands
from core.mcp_hub import native, repository
from core.mcp_hub.definition import HTTP, STDIO, definition_from_provider, normalize_description
from core.mcp_hub.errors import McpHubError

logger = logging.getLogger(__name__)

STDIO_PROCESS_CAP = 8
IDLE_CLOSE_SECONDS = 600
CALL_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 60
GENERATION_TIMEOUT_SECONDS = 300
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


def _client_for(definition: dict[str, Any], stack: contextlib.AsyncExitStack, mode: str = "auto"):
    """Build an SDK client for a revision snapshot (never the edited top-level file)."""

    from mcp import Client, StdioServerParameters
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.types import Implementation

    transport = definition["transport"]
    info = Implementation(name="avibe", version="1")
    if transport["type"] == STDIO:
        params = StdioServerParameters(
            command=transport["command"],
            args=list(transport.get("args") or []),
            env=dict(transport.get("env") or {}) or None,
            cwd=transport.get("cwd"),
        )
        return Client(params, client_info=info, mode=mode)
    http_client = create_mcp_http_client(headers=dict(transport.get("headers") or {}))
    stack.push_async_callback(http_client.aclose)
    return Client(streamable_http_client(transport["url"], http_client=http_client), client_info=info, mode=mode)


def _closed_during_handshake(exc: BaseException) -> bool:
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    if isinstance(exc, MCPError):
        return exc.error.code == CONNECTION_CLOSED
    # Exception groups from the SDK's task groups (no builtin name before 3.11).
    return any(_closed_during_handshake(item) for item in getattr(exc, "exceptions", ()))


async def _enter_client(definition: dict[str, Any], stack: contextlib.AsyncExitStack):
    """Enter a client; retry once with the initialize handshake for strict pre-2026 servers.

    The default probe sends ``server/discover`` first, and strict legacy servers
    (e.g. rmcp) exit on it. The retry happens before any request is dispatched.
    """

    try:
        return await stack.enter_async_context(_client_for(definition, stack))
    except Exception as exc:
        if not _closed_during_handshake(exc):
            raise
    return await stack.enter_async_context(_client_for(definition, stack, mode="legacy"))


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
            raise McpHubError("connection", f"Could not connect to the MCP server: {_describe(exc)}") from None

    async def _run(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                client = await _enter_client(self.definition, stack)
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
                    return {
                        "name": name,
                        "status": "added",
                        "revision": body.get("revision"),
                        "toolCount": body.get("toolCount"),
                    }
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
        deadline = time.monotonic() + GENERATION_TIMEOUT_SECONDS
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
                tools, server_info = await self._discover(snapshot)
        except TimeoutError:
            if unlist_on_failure:
                await asyncio.to_thread(repository.unlist, name)
            raise McpHubError("timeout", "Refresh timed out.") from None
        except McpHubError:
            if unlist_on_failure:
                await asyncio.to_thread(repository.unlist, name)
            raise
        except Exception as cop:
            if unlist_on_failure:
                await asyncio.to_thread(repository.unlist, name)
            raise McpHubError("connection", f"Refresh failed: {_describe(cop)}") from None
        finally:
            if acquired:
                self.release_stdio_slot()
        revision = await asyncio.to_thread(self._publish, name, snapshot, definition, tools, server_info)
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
                    return {"name": name, "status": "registered", "toolCount": body.get("toolCount")}
                error = (body.get("error") or {}).get("message") or "failed"
                logger.warning("MCP discover %s failed: %s", name, error)
                await asyncio.to_thread(repository.unlist, name)
                return {"name": name, "status": "failed", "error": error}

        return list(await asyncio.gather(*(one(name) for name in sorted(names))))

    @staticmethod
    async def _discover(snapshot: dict[str, Any]) -> tuple[list, Any]:
        async with contextlib.AsyncExitStack() as stack:
            client = await _enter_client(snapshot, stack)
            return await _list_all_tools(client), client.server_info

    @staticmethod
    def _publish(name, snapshot, definition, tools, server_info) -> str:
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
            "description": normalize_description(getattr(server_info, "description", None)),
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        version = getattr(server_info, "version", None)
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

    async def _remove(self, payload: dict[str, Any]) -> bytes:
        name = payload.get("name")
        async with self._lock(name):
            kind = await asyncio.to_thread(repository.remove_package, name)
            await self._retire_package(name)
        return _dumps({"ok": True, "name": name, "removed": True, "kind": kind})

    # -- calls ----------------------------------------------------------------

    @staticmethod
    def _admit(name: str, tool: str, arguments: Any) -> dict[str, Any]:
        published = repository.read_published(name)
        if published is None:
            repository.read_definition(name)
            raise McpHubError("not_generated", f"MCP server {name!r} has not been generated.", hint="vibe mcp --list")
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
                return await asyncio.to_thread(self._result_envelope, name, revision, tool, result)
        except McpHubError as exc:
            return _dumps(self._envelope(name, revision, tool, error=exc))
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
                    from mcp import MCPError

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
