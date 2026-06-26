"""``httpx`` wrapper for talking to the controller's internal Unix socket.

C5 of Plan 2 (see ``docs/plans/workbench-dispatch-architecture.md``).
The UI server runs as its own subprocess; this module is how it reaches
``core.internal_server`` to start agent turns and observe their lifecycle.

Single responsibility: keep all the socket-path / httpx-transport /
SSE-parsing boilerplate out of the UI route bodies. Routes call
``dispatch_async(...)`` to start a fire-and-forget turn (the Chat page — the
reply arrives over the persistent ``message.new`` session stream, not the
response), ``stream_dispatch(...)`` to run a turn and stream its chunks back
(the Show-page dispatch flow), ``stream_events(...)`` to subscribe to the
controller's event feed, and ``cancel_dispatch`` / ``send_now`` /
``turn_state`` / ``health`` for the turn-control surface — each raising
``InternalServerUnavailable`` so the route can degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from config import paths

logger = logging.getLogger(__name__)

_SOCKET_ERRORS = (httpx.ConnectError, httpx.TimeoutException, OSError)
_SOCKET_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, OSError)


class InternalServerUnavailable(Exception):
    """Raised when the dispatch socket cannot be reached.

    Routes should catch this and degrade to the queue-based fallback so
    a controller crash or socket-bind race doesn't take down the
    user-facing send-compose flow.
    """


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

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")

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

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")

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


async def dispatch_async(
    payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Start a fire-and-forget turn on the controller and return immediately.

    Hits ``POST /internal/dispatch_async``: the controller starts the turn and
    responds ``202`` right away (the reply arrives over the persistent
    ``message.new`` session stream, not this response). Returns
    ``{"status_code", "body"}`` so the caller can distinguish a started turn
    (202) from a concurrent-turn refusal (409). Raises
    ``InternalServerUnavailable`` on socket failure so the route can degrade.
    """

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post("/internal/dispatch_async", json=payload)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def reconcile_platforms(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Ask the controller to hot-apply the persisted platform configuration."""

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
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


async def cancel_dispatch(session_id: str, *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Ask the controller to cancel a running ``dispatch_turn`` for
    ``session_id``.

    Returns the controller's JSON response on success. Raises
    ``InternalServerUnavailable`` if the socket is missing / unreachable
    so the UI route can fall back gracefully.
    """

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
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
            resp = await client.post(f"/internal/cancel/{session_id}")
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

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
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


async def send_now(session_id: str, *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Ask the controller to run a session's send-while-busy queue immediately
    ("立即发送"): interrupt any running turn + flush the queue. Returns
    ``{status_code, body}``; raises ``InternalServerUnavailable`` on socket
    failure so the UI route can degrade.
    """

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
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
            resp = await client.post(f"/internal/send-now/{session_id}")
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def turn_state(session_id: str, *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Query whether a turn is in flight for ``session_id`` so a freshly loaded /
    reconnected Chat page can restore its Stop/working state. Returns
    ``{status_code, body}``; raises ``InternalServerUnavailable`` on socket
    failure so the route can degrade (assume idle)."""

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
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


async def list_running_agents(*, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Fetch the controller's read-only running-agents snapshot.

    Returns ``{status_code, body}``; raises ``InternalServerUnavailable`` on
    socket failure so the web route can render an explicit "runtime unreachable"
    state instead of a misleading "0 running". The snapshot reads in-memory
    registries plus a small DB enrichment, so the read timeout is a touch longer
    than ``turn_state``.
    """

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
        raise InternalServerUnavailable(f"dispatch socket missing at {target}")
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=httpx.Timeout(3.0, connect=0.5),
        ) as client:
            resp = await client.get("/internal/running-agents")
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

    target = (socket_path or default_socket_path()).expanduser().resolve()
    if not target.exists():
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
