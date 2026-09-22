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


def record_skill_observation_sync(
    observation: dict[str, Any], *, socket_path: Optional[Path] = None,
    timeout: float = 0.25,
) -> dict[str, Any]:
    """Best-effort queue submission; the response is not a durable receipt."""
    target = _verified_socket_path(socket_path)
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(target)),
            base_url="http://localhost",
            timeout=httpx.Timeout(timeout),
        ) as client:
            response = client.post("/internal/skill-observations", json=observation)
            if response.status_code != 202:
                raise InternalServerUnavailable("Skill observation rejected")
            return response.json()
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable("Skill observation transport unavailable") from exc


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


async def archive_session(session_id: str, *, socket_path: Optional[Path] = None, timeout: float = 10.0) -> dict[str, Any]:
    """Archive a Workbench session through the controller lifecycle seam."""
    target = await _verified_socket_path_async(socket_path)
    try:
        transport = httpx.AsyncHTTPTransport(uds=str(target))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=httpx.Timeout(timeout, connect=5.0)) as client:
            resp = await client.post("/internal/sessions/archive", json={"session_id": session_id})
    except _SOCKET_CONNECT_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    except httpx.TimeoutException as exc:
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


async def backend_application(
    backend: str, *, socket_path: Optional[Path] = None, timeout: float = 5.0,
) -> dict[str, Any]:
    from modules.agents.catalog import AGENT_BACKENDS

    if backend not in AGENT_BACKENDS:
        raise ValueError("unsupported_backend")
    target = await _verified_socket_path_async(socket_path)
    transport = httpx.AsyncHTTPTransport(uds=str(target))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=timeout,
        ) as client:
            response = await client.get(f"/internal/backend-application/{backend}")
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": response.status_code, "body": response.json() if response.content else {}}


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
