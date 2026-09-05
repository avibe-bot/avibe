"""``httpx`` wrapper for talking to the controller's internal Unix socket.

C5 of Plan 2 (see ``docs/plans/workbench-dispatch-architecture.md``).
The UI server runs as its own subprocess; this module is how it reaches
``core.internal_server`` to start agent turns and observe their lifecycle.

Single responsibility: keep all the socket-path / httpx-transport /
SSE-parsing boilerplate out of the UI route bodies. Routes call
``dispatch_async(...)`` to start a fire-and-forget turn (the reply arrives over
the persistent ``message.new`` session stream, not the response),
``stream_events(...)`` to subscribe to the controller's event feed, and
``cancel_dispatch`` / ``send_now`` / ``turn_state`` / ``health`` for the
turn-control surface — each raising ``InternalServerUnavailable`` so the route
can degrade gracefully.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

import httpx

from config import paths
from vibe.memory_contract import (
    MAX_AGENTIC_TIMEOUT_SECONDS,
    PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_SOCKET_ERRORS = (httpx.TransportError, OSError)
_SOCKET_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, OSError)
_OWNER_ONLY_SOCKET_MODES = frozenset({0o600, 0o700})
_CHECK_POSIX_SOCKET_MODE = os.name != "nt"

# A transport deadline shorter than the operation it wraps turns a slow
# success into a reported failure while the controller keeps working, and
# leaves the caller free to retry into the unfinished operation. Both of these
# must therefore stay outside the bound of the work they wait on;
# ``tests/test_internal_client_timeouts.py`` asserts the relationship against
# the sources below rather than trusting these numbers to stay in step.
#
# Most Memory reads wait on one provider operation bounded by
# ``avibe_memory.module.PROVIDER_READ_TIMEOUT_SECONDS`` (20s). Search can first
# probe capabilities and then issue an agentic provider read bounded at 30s,
# so it needs a separate transport bound outside both sequential steps.
MEMORY_READ_TIMEOUT_SECONDS = 25.0
MEMORY_SEARCH_TIMEOUT_SECONDS = 55.0
MEMORY_STATUS_TIMEOUT_SECONDS = MEMORY_READ_TIMEOUT_SECONDS
# The host contract owns the transport deadline; its relationship test keeps it
# outside the implementation's complete identity/journal/provider/store budget.
MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS = (
    PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS
)
MEMORY_FAILURES_TIMEOUT_SECONDS = MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS
MEMORY_MAINTENANCE_TIMEOUT_SECONDS = MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS
# Reconcile can probe processing (20s), drain an active add (30s), stop the
# prior child (10s), and wait for replacement readiness (30s). Keep transport
# outside the whole sequence so a slow success cannot race a settings rollback.
MEMORY_RECONCILE_TIMEOUT_SECONDS = 120.0
# Install waits on the controller's download/extract/activate. The Dependencies
# UI polls the job for 310s (``startAndPollDependencyInstall``), so anything
# shorter reports a false failure on a slow link while the install continues.
MEMORY_INSTALL_TIMEOUT_SECONDS = 300.0
# Clearing can include provider-side deletion and journal recovery. Keep the
# transport outside the controller's bounded operation so a slow success does
# not race a retry from the settings UI.
MEMORY_CLEAR_TIMEOUT_SECONDS = 150.0


class InternalServerUnavailable(Exception):
    """Raised when the dispatch socket cannot be reached before acceptance."""


class InternalServerTimeout(Exception):
    """Raised when the internal server accepts a probe but does not answer in time."""


def default_socket_path() -> Path:
    """Mirror ``core.internal_server.default_socket_path`` without an
    import cycle.

    ``core.internal_server`` lives in the controller process and we
    deliberately don't import controller-side modules from the UI
    server. Duplicating the one-line path-derivation keeps the
    boundaries clean.
    """

    override = os.environ.get("VIBE_INTERNAL_DISPATCH_SOCKET")
    if override:
        return Path(override).expanduser()
    return paths.get_state_dir() / "dispatch.sock"


def _verified_socket_path(socket_path: Optional[Path]) -> Path:
    """Return an owner-only controller socket without following filesystem links."""

    target = (socket_path or default_socket_path()).expanduser()
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise InternalServerUnavailable(f"dispatch socket missing at {target}") from exc
    except OSError as exc:
        raise InternalServerUnavailable(f"dispatch socket cannot be inspected at {target}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise InternalServerUnavailable(f"dispatch socket is unsafe at {target}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise InternalServerUnavailable(f"dispatch socket owner mismatch at {target}")
    if _CHECK_POSIX_SOCKET_MODE and stat.S_IMODE(info.st_mode) not in _OWNER_ONLY_SOCKET_MODES:
        raise InternalServerUnavailable(f"dispatch socket mode mismatch at {target}")
    return target


async def _verified_socket_path_async(socket_path: Optional[Path]) -> Path:
    """Keep socket metadata checks off the UI server's event loop."""

    return await asyncio.to_thread(_verified_socket_path, socket_path)


async def stream_dispatch(
    payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 1800.0,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Send a dispatch request and yield the turn's SSE events as they arrive.

    Each yielded tuple is ``(event_name, parsed_data)`` — e.g. ``("turn.start",
    {...})``, ``("turn.chunk", {...})``, ``("turn.end", {...})``. The caller
    re-encodes them for the browser. Raises ``InternalServerUnavailable`` for
    connect-time failures so the caller can degrade.

    NB: the web **Chat** page no longer uses this (it's fire-and-forget +
    ``message.new``); this streaming round-trip backs the **Show-page** dispatch
    flow (``_run_show_event_dispatch`` re-publishes each event as ``show.dispatch``).
    """

    target = await _verified_socket_path_async(socket_path)

    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            try:
                stream = client.stream("POST", "/internal/dispatch", json=payload)
            except _SOCKET_ERRORS as exc:
                raise InternalServerUnavailable(str(exc)) from exc

            async with stream as resp:
                if resp.status_code >= 400:
                    detail = await resp.aread()
                    raise InternalServerUnavailable(
                        f"dispatch endpoint returned {resp.status_code}: {detail!r}"
                    )

                current_event: Optional[str] = None
                async for line in resp.aiter_lines():
                    if not line:
                        # Blank line ends an SSE event block; reset the
                        # event-name buffer so a missing ``event:`` field
                        # on the next block defaults to ``message``.
                        current_event = None
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        raw = line[5:].lstrip()
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("internal_client: invalid SSE data line %r", raw)
                            continue
                        yield (current_event or "message", parsed)
    except InternalServerUnavailable:
        raise
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
async def stream_events(
    *,
    socket_path: Optional[Path] = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Subscribe to the controller's long-lived ``GET /internal/events`` feed.

    Yields ``(event_name, parsed_data)`` for each event, e.g.
    ``("inbox.session.updated", {...inbox row...})``. The read timeout is
    disabled (the connection is meant to stay open); raises
    ``InternalServerUnavailable`` on connect failure so the UI server's
    subscriber loop can back off and reconnect.
    """

    target = await _verified_socket_path_async(socket_path)

    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(None, connect=5.0),
        ) as client:
            try:
                stream = client.stream("GET", "/internal/events")
            except _SOCKET_ERRORS as exc:
                raise InternalServerUnavailable(str(exc)) from exc

            async with stream as resp:
                if resp.status_code >= 400:
                    detail = await resp.aread()
                    raise InternalServerUnavailable(
                        f"events endpoint returned {resp.status_code}: {detail!r}"
                    )

                current_event: Optional[str] = None
                async for line in resp.aiter_lines():
                    if not line:
                        current_event = None
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        raw = line[5:].lstrip()
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("internal_client: invalid SSE data line %r", raw)
                            continue
                        yield (current_event or "message", parsed)
    except InternalServerUnavailable:
        raise
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc


async def publish_event(
    event_type: str,
    data: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Ask the Controller process to publish an allowlisted SSE notification."""

    target = await _verified_socket_path_async(socket_path)

    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = await client.post("/internal/events", json={"type": event_type, "data": data})
            if resp.status_code >= 400:
                detail = await resp.aread()
                raise InternalServerUnavailable(f"events publish returned {resp.status_code}: {detail!r}")
            return resp.json()
    except InternalServerUnavailable:
        raise
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc


def publish_event_sync(
    event_type: str,
    data: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Synchronous wrapper for CLI/child-process notification publishers."""

    target = _verified_socket_path(socket_path)

    transport = httpx.HTTPTransport(uds=str(target))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = client.post("/internal/events", json={"type": event_type, "data": data})
            if resp.status_code >= 400:
                raise InternalServerUnavailable(
                    f"events publish returned {resp.status_code}: {resp.content!r}"
                )
            return resp.json()
    except InternalServerUnavailable:
        raise
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc


async def dispatch_async(
    payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float | None = 10.0,
) -> dict[str, Any]:
    """Start a fire-and-forget turn on the controller and return immediately.

    Hits ``POST /internal/dispatch_async``: the controller starts the turn and
    responds ``202`` right away (the reply arrives over the persistent
    ``message.new`` session stream, not this response). Returns
    ``{"status_code", "body"}`` so the caller can distinguish a started turn
    from one accepted into the shared queue. A pre-connect failure raises
    ``InternalServerUnavailable``; a post-connect timeout raises
    ``InternalServerTimeout`` because acceptance is unknown.
    """

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post("/internal/dispatch_async", json=payload)
    except _SOCKET_CONNECT_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    except httpx.TimeoutException as exc:
        # Once the socket connected, a timeout is acceptance-unknown: the
        # controller request may still settle the durable reservation.
        raise InternalServerTimeout(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def reconcile_platforms(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Ask the controller to hot-apply the persisted platform configuration."""

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post("/internal/reconcile-platforms")
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def invalidate_activity_streaming(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Make the controller re-read the persisted Agent Activity display flag."""

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = await client.post("/internal/invalidate-activity-streaming")
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def reconcile_agent_backends(
    backends: list[str],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Ask the controller to hot-apply persisted Agent backend config."""

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post(
                "/internal/reconcile-agent-backends",
                json={"backends": backends},
            )
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}




async def test_backend_auth(
    backend: str,
    *,
    model: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run a Settings connection probe on the controller-owned Agent runtime."""

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    payload: dict[str, Any] = {"backend": backend}
    if model:
        payload["model"] = model
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post("/internal/backend-auth/test", json=payload)
    except _SOCKET_CONNECT_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise InternalServerTimeout(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def reconcile_memory(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_RECONCILE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Ask the controller to apply persisted Memory settings in place."""

    return await _memory_request("POST", "/internal/reconcile-memory", socket_path=socket_path, timeout=timeout)


async def memory_wake(
    *,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Wait for one non-destructive wake attempt."""

    return await _memory_request(
        "POST",
        "/internal/memory/wake",
        socket_path=socket_path,
        timeout=None,
    )


async def memory_preflight(
    *, payload: dict, user_key: str, socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    path = "/internal/memory/preflight"
    return await _memory_request(
        "POST", path, payload=payload,
        headers=_memory_user_key_headers("POST", path, user_key),
        socket_path=socket_path, timeout=None,
    )


async def memory_repair(
    *,
    confirm_loss: bool,
    user_key: str,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    path = "/internal/memory/repair"
    return await _memory_request(
        "POST",
        path,
        payload={"confirm_loss": confirm_loss},
        headers=_memory_user_key_headers("POST", path, user_key),
        socket_path=socket_path,
        timeout=None,
    )


async def memory_delete_data(
    *,
    confirm_loss: bool,
    user_key: str,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    path = "/internal/memory/delete-data"
    return await _memory_request(
        "POST",
        path,
        payload={"confirm_loss": confirm_loss},
        headers=_memory_user_key_headers("POST", path, user_key),
        socket_path=socket_path,
        timeout=None,
    )


async def memory_reconfigure(
    *,
    confirm_loss: bool,
    memory: dict[str, Any],
    expected_memory: dict[str, Any],
    user_key: str,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    path = "/internal/memory/reconfigure"
    return await _memory_request(
        "POST",
        path,
        payload={
            "confirm_loss": confirm_loss,
            "memory": memory,
            "expected_memory": expected_memory,
        },
        headers=_memory_user_key_headers("POST", path, user_key),
        socket_path=socket_path,
        timeout=None,
    )


async def memory_archive_session(
    session_id: str,
    *,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Await the controller-owned Workbench archive write without a reporting deadline."""

    return await _memory_request(
        "POST",
        "/internal/memory/archive-session",
        payload={"session_id": session_id},
        socket_path=socket_path,
        timeout=None,
    )


def memory_install_runtime_sync(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Ask the controller to install EverOS through its live lifecycle."""

    return _memory_request_sync(
        "POST",
        "/internal/memory/install-runtime",
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_status(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_STATUS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return await _memory_request("GET", "/internal/memory/status", socket_path=socket_path, timeout=timeout)


async def memory_processing_record(
    *,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_PROCESSING_RECORD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = "/internal/memory/processing-record"
    return await _memory_request(
        "GET",
        path,
        headers=_memory_user_key_headers("GET", path, user_key),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_processing_record_entries(
    *,
    cursor: str | None,
    limit: int,
    project: str | None,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = "/internal/memory/processing-record/entries"
    params: dict[str, str | int] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    if project is not None:
        params["project"] = project
    return await _memory_request(
        "GET",
        path,
        params=params,
        headers=_memory_user_key_headers("GET", path, user_key),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_processing_record_entry(
    memcell_id: str,
    *,
    project: str | None,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = "/internal/memory/processing-record/entry"
    return await _memory_request(
        "GET",
        path,
        params={
            "memcell_id": memcell_id,
            **({"project": project} if project is not None else {}),
        },
        headers=_memory_user_key_headers("GET", path, user_key),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_failures(
    *,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_FAILURES_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = "/internal/memory/failures"
    return await _memory_request(
        "GET",
        path,
        headers=_memory_user_key_headers("GET", path, user_key),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_maintenance(
    *,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_MAINTENANCE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = "/internal/memory/maintenance"
    return await _memory_request(
        "GET",
        path,
        headers=_memory_user_key_headers("GET", path, user_key),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_profile(
    *,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return await _memory_request(
        "GET",
        "/internal/memory/profile",
        headers=_memory_user_key_headers(
            "GET",
            "/internal/memory/profile",
            user_key,
        ),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_projects(
    *,
    user_key: str,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return await _memory_request(
        "GET",
        "/internal/memory/projects",
        headers=_memory_user_key_headers(
            "GET",
            "/internal/memory/projects",
            user_key,
        ),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_list(
    *,
    user_key: str,
    project: str | None = None,
    page: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
    origin: Literal["user", "agent"] | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    payload: dict[str, object] = {"limit": limit}
    if project is not None:
        payload["project"] = project
    if page is not None:
        payload["page"] = page
    if cursor is not None:
        payload["cursor"] = cursor
    if origin is not None:
        payload["origin"] = origin
    return await _memory_request(
        "POST",
        "/internal/memory/list",
        payload=payload,
        headers=_memory_user_key_headers(
            "POST",
            "/internal/memory/list",
            user_key,
        ),
        socket_path=socket_path,
        timeout=timeout,
    )


async def memory_search(
    query: str,
    policy: dict[str, object],
    *,
    user_key: str,
    project: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_SEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    payload: dict[str, object] = {"query": query, "policy": policy}
    if project is not None:
        payload["project"] = project
    return await _memory_request(
        "POST",
        "/internal/memory/search",
        payload=payload,
        headers=_memory_user_key_headers(
            "POST",
            "/internal/memory/search",
            user_key,
        ),
        socket_path=socket_path,
        timeout=timeout,
    )




def memory_status_sync(
    *,
    caller_session_id: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_STATUS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return _memory_request_sync(
        "GET",
        "/internal/memory/status",
        headers=_memory_cli_session_headers(caller_session_id),
        socket_path=socket_path,
        timeout=timeout,
    )


def memory_profile_sync(
    *,
    caller_session_id: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return _memory_request_sync(
        "GET",
        "/internal/memory/profile",
        headers=_memory_cli_session_headers(caller_session_id),
        socket_path=socket_path,
        timeout=timeout,
    )


def memory_list_sync(
    *,
    page: int = 1,
    limit: int = 20,
    caller_session_id: str | None = None,
    project: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    payload: dict[str, object] = {"page": page, "limit": limit}
    if project is not None:
        payload["project"] = project
    return _memory_request_sync(
        "POST",
        "/internal/memory/list",
        payload=payload,
        headers=_memory_cli_session_headers(caller_session_id),
        socket_path=socket_path,
        timeout=timeout,
    )


def memory_search_sync(
    query: str,
    limit: int,
    *,
    mode: Literal["hybrid", "keyword", "vector", "agentic"] = "hybrid",
    caller_session_id: str | None = None,
    project: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = MEMORY_SEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    policy: dict[str, object] = {
        "mode": mode,
        "max_results": limit,
        "include_profile": True,
        "include_current_session": False,
    }
    if mode == "agentic":
        # RecallPolicy retains its complete legacy budget envelope. EverOSPort
        # enforces the wall-clock field; EverOS 1.2.3 has no model/token gate.
        policy.update(
            timeout_seconds=MAX_AGENTIC_TIMEOUT_SECONDS,
            max_model_calls=2,
            cost_budget_tokens=32_000,
        )
    payload: dict[str, object] = {
        "query": query,
        "policy": policy,
    }
    if project is not None:
        payload["project"] = project
    return _memory_request_sync(
        "POST",
        "/internal/memory/search",
        payload=payload,
        headers=_memory_cli_session_headers(caller_session_id),
        socket_path=socket_path,
        timeout=timeout,
    )


def memory_remember_sync(
    text: str,
    *,
    caller_session_id: str | None = None,
    project: str | None = None,
    socket_path: Optional[Path] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    payload: dict[str, object] = {"text": text}
    if project is not None:
        payload["project"] = project
    return _memory_request_sync(
        "POST",
        "/internal/memory/remember",
        payload=payload,
        headers=_memory_cli_session_headers(caller_session_id),
        socket_path=socket_path,
        timeout=timeout,
    )


async def _memory_request(
    method: str,
    route: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
    socket_path: Optional[Path] = None,
    timeout: float | None,
) -> dict[str, Any]:
    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    client_timeout = (
        httpx.Timeout(None, connect=5.0)
        if timeout is None
        else httpx.Timeout(timeout, connect=min(timeout, 5.0))
    )
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=client_timeout,
        ) as client:
            response = await client.request(
                method,
                route,
                json=payload,
                params=params,
                headers=headers,
            )
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {"status": "failed", "error": "memory_provider_response_invalid"}
    return {"status_code": response.status_code, "body": body}


def _memory_request_sync(
    method: str,
    route: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    socket_path: Optional[Path] = None,
    timeout: float,
) -> dict[str, Any]:
    target = _verified_socket_path(socket_path)
    transport = httpx.HTTPTransport(uds=str(target))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            response = client.request(method, route, json=payload, headers=headers)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {"status": "failed", "error": "memory_provider_response_invalid"}
    return {"status_code": response.status_code, "body": body}


def _memory_cli_session_headers(session_id: str | None) -> dict[str, str] | None:
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    from vibe.memory_http_headers import CALLER_SESSION_HEADER

    return {CALLER_SESSION_HEADER: session_id}


def _memory_user_key_headers(method: str, path: str, user_key: str) -> dict[str, str]:
    from vibe.memory_http_headers import MEMORY_USER_KEY_HEADER
    from vibe.memory_ui_access import (
        MEMORY_UI_PROOF_HEADER,
        build_ui_read_proof,
        process_ui_read_secret,
    )

    headers = {MEMORY_USER_KEY_HEADER: user_key}
    secret = process_ui_read_secret()
    if secret:
        headers[MEMORY_UI_PROOF_HEADER] = build_ui_read_proof(
            secret,
            method=method,
            path=path,
            user_key=user_key,
        )
    return headers


async def notify_vault_request_created(
    request_payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Ask the controller to send the IM degradation notice for a Vault request."""

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = await client.post("/internal/vault/request-created", json={"request": request_payload})
            if resp.status_code >= 400:
                detail = await resp.aread()
                raise InternalServerUnavailable(
                    f"vault request notification returned {resp.status_code}: {detail!r}"
                )
    except InternalServerUnavailable:
        raise
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


def notify_vault_request_created_sync(
    request_payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Synchronous wrapper for CLI/UI-server Vault request notifications."""

    target = _verified_socket_path(socket_path)

    transport = httpx.HTTPTransport(uds=str(target))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = client.post("/internal/vault/request-created", json={"request": request_payload})
            if resp.status_code >= 400:
                raise InternalServerUnavailable(
                    f"vault request notification returned {resp.status_code}: {resp.content!r}"
                )
    except InternalServerUnavailable:
        raise
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def cancel_dispatch(
    session_id: str,
    *,
    run_id: str | None = None,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Ask the controller to cancel a running ``dispatch_turn`` for
    ``session_id``.

    Returns the controller's JSON response on success. Raises
    ``InternalServerUnavailable`` if the socket is missing / unreachable
    so the UI route can fall back gracefully.
    """

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            # The cancel now WAITS for the backend interrupt to confirm before
            # acking (so a refused stop keeps the turn cancellable), and a
            # Claude interrupt / OpenCode abort can take a few seconds — give it
            # room so a slow-but-successful stop isn't read-timed-out into a 500.
            timeout=httpx.Timeout(30.0, connect=1.0),
        ) as client:
            resp = await client.post(
                f"/internal/cancel/{session_id}",
                params={"run_id": run_id} if run_id is not None else None,
            )
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def end_running_agent(payload: dict[str, Any], *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Ask the controller to terminate one running agent's live runtime.

    ``payload`` identifies the target (backend/state/composite_key/base_session_id
    /pid). Returns ``{status_code, body}``; raises ``InternalServerUnavailable``
    on socket failure. A Claude interrupt / OpenCode abort can take a few seconds,
    so the timeout matches ``cancel_dispatch``.
    """

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(30.0, connect=1.0),
        ) as client:
            resp = await client.post("/internal/running-agents/end", json=payload)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def _show_access_request(
    path: str,
    payload: dict[str, Any],
    *,
    read_timeout: float | None,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(read_timeout, connect=1.0),
        ) as client:
            resp = await client.post(path, json=payload)
    except httpx.ReadTimeout as exc:
        raise InternalServerTimeout(str(exc)) from exc
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def show_access_settings_read(
    payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    return await _show_access_request(
        "/internal/show-access/settings-read",
        payload,
        read_timeout=10.0,
        socket_path=socket_path,
    )


async def show_access_apply(
    payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    return await _show_access_request(
        "/internal/show-access/apply",
        payload,
        # Once accepted, the controller serializes this non-cancellable SQLite
        # write. Wait for its definitive CAS result so a slow commit is never
        # reported as a timeout that invites an ambiguous retry.
        read_timeout=None,
        socket_path=socket_path,
    )


async def send_now(
    session_id: str,
    *,
    expected_delivery_id: str | None = None,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Ask the controller to run a session's send-while-busy queue immediately
    ("立即发送"): interrupt any running turn + flush the queue. Returns
    ``{status_code, body}``; raises ``InternalServerUnavailable`` on socket
    failure so the UI route can degrade.
    """

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            # send-now interrupts the running turn before flushing, and that
            # backend stop can take a few seconds — match the cancel timeout so a
            # slow-but-successful interrupt isn't read-timed-out.
            timeout=httpx.Timeout(30.0, connect=1.0),
        ) as client:
            resp = await client.post(
                f"/internal/send-now/{session_id}",
                params=(
                    {"expected_delivery_id": expected_delivery_id}
                    if expected_delivery_id
                    else None
                ),
            )
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def turn_state(session_id: str, *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Query whether a turn is in flight for ``session_id`` so a freshly loaded /
    reconnected Chat page can restore its Stop/working state. Returns
    ``{status_code, body}``; raises ``InternalServerUnavailable`` on socket
    failure so the route can degrade (assume idle)."""

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(1.0, connect=0.2),
        ) as client:
            resp = await client.get(f"/internal/turn-state/{session_id}")
    except httpx.ReadTimeout as exc:
        raise InternalServerTimeout(str(exc)) from exc
    except _SOCKET_CONNECT_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def list_running_agents(
    *,
    run_ids: Optional[list[str]] = None,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Fetch the controller's read-only running-agents snapshot.

    Returns ``{status_code, body}``; raises ``InternalServerUnavailable`` on
    socket failure so the web route can render an explicit "runtime unreachable"
    state instead of a misleading "0 running". The snapshot reads in-memory
    registries plus a small DB enrichment, so the read timeout is a touch longer
    than ``turn_state``.
    """

    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(3.0, connect=0.5),
        ) as client:
            if run_ids is None:
                resp = await client.get("/internal/running-agents")
            else:
                resp = await client.post(
                    "/internal/running-agents/snapshot",
                    json={"run_ids": run_ids},
                )
    except httpx.ReadTimeout as exc:
        raise InternalServerTimeout(str(exc)) from exc
    except _SOCKET_CONNECT_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def health(socket_path: Optional[Path] = None) -> bool:
    """Probe ``GET /internal/health``. Returns False on any failure.

    Useful for UI startup checks and for the fallback decision in the
    streaming route body so we can decline cleanly before opening the
    longer-lived dispatch stream.
    """

    try:
        target = await _verified_socket_path_async(socket_path)
    except InternalServerUnavailable:
        return False
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(2.0, connect=1.0),
        ) as client:
            resp = await client.get("/internal/health")
            return resp.status_code == 200 and (resp.json() or {}).get("ok") is True
    except Exception:
        return False


def health_sync(
    socket_path: Optional[Path] = None,
    *,
    timeout: float = 2.0,
) -> bool:
    """Synchronously probe the controller health endpoint.

    Dependency reconciliation runs in a worker thread and must distinguish a
    connectable controller from a stale Unix-socket pathname left by a crashed
    process.
    """

    try:
        target = _verified_socket_path(socket_path)
    except InternalServerUnavailable:
        return False
    transport = httpx.HTTPTransport(uds=str(target))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=min(timeout, 1.0)),
        ) as client:
            resp = client.get("/internal/health")
            return resp.status_code == 200 and (resp.json() or {}).get("ok") is True
    except Exception:
        return False
