"""``httpx`` wrapper for the Controller's cross-platform control IPC.

C5 of Plan 2 (see ``docs/plans/workbench-dispatch-architecture.md``).
The UI server runs as its own subprocess; this module is how it reaches
``core.internal_server`` to start agent turns and observe their lifecycle.

Single responsibility: keep all the endpoint discovery / httpx-transport /
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

from core import control_ipc

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

    return control_ipc.default_unix_socket_path()


def _platform_name() -> str:
    return os.name


def _resolve_endpoint(socket_path: Optional[Path]) -> control_ipc.ControlIpcClientEndpoint:
    try:
        endpoint = control_ipc.resolve_client_endpoint(
            platform_name=_platform_name(),
            socket_path=socket_path,
        )
    except control_ipc.ControlIpcDescriptorError as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    if endpoint.transport == "unix":
        target = endpoint.socket_path
        if target is None or not target.exists():
            raise InternalServerUnavailable(f"dispatch socket missing at {target}")
    return endpoint


def _async_transport(endpoint: control_ipc.ControlIpcClientEndpoint) -> httpx.AsyncBaseTransport:
    if endpoint.transport == "unix":
        return httpx.AsyncHTTPTransport(uds=str(endpoint.socket_path))
    return httpx.AsyncHTTPTransport()


def _sync_transport(endpoint: control_ipc.ControlIpcClientEndpoint) -> httpx.BaseTransport:
    if endpoint.transport == "unix":
        return httpx.HTTPTransport(uds=str(endpoint.socket_path))
    return httpx.HTTPTransport()


def _validate_response(
    response: httpx.Response,
    endpoint: control_ipc.ControlIpcClientEndpoint,
) -> None:
    descriptor = endpoint.descriptor
    if descriptor is None:
        return
    if response.status_code == 401:
        raise InternalServerUnavailable("control IPC authentication was rejected")
    response_instance = response.headers.get(control_ipc.CONTROL_IPC_INSTANCE_HEADER)
    if not control_ipc.response_instance_matches(descriptor, response_instance):
        raise InternalServerUnavailable("control IPC response came from a stale instance")


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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            try:
                stream = client.stream("POST", "/internal/dispatch", json=payload)
            except _SOCKET_ERRORS as exc:
                raise InternalServerUnavailable(str(exc)) from exc

            async with stream as resp:
                _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(None, connect=5.0),
        ) as client:
            try:
                stream = client.stream("GET", "/internal/events")
            except _SOCKET_ERRORS as exc:
                raise InternalServerUnavailable(str(exc)) from exc

            async with stream as resp:
                _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = await client.post("/internal/events", json={"type": event_type, "data": data})
            _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _sync_transport(endpoint)
    try:
        with httpx.Client(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = client.post("/internal/events", json={"type": event_type, "data": data})
            _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post("/internal/dispatch_async", json=payload)
            _validate_response(resp, endpoint)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def reconcile_platforms(
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Ask the controller to hot-apply the persisted platform configuration."""

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post("/internal/reconcile-platforms")
            _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            resp = await client.post(
                "/internal/reconcile-agent-backends",
                json={"backends": backends},
            )
            _validate_response(resp, endpoint)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def notify_vault_request_created(
    request_payload: dict[str, Any],
    *,
    socket_path: Optional[Path] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Ask the controller to send the IM degradation notice for a Vault request."""

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = await client.post("/internal/vault/request-created", json={"request": request_payload})
            _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _sync_transport(endpoint)
    try:
        with httpx.Client(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(timeout, connect=2.0),
        ) as client:
            resp = client.post("/internal/vault/request-created", json={"request": request_payload})
            _validate_response(resp, endpoint)
            if resp.status_code >= 400:
                raise InternalServerUnavailable(
                    f"vault request notification returned {resp.status_code}: {resp.content!r}"
                )
    except InternalServerUnavailable:
        raise
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            # The cancel now WAITS for the backend interrupt to confirm before
            # acking (so a refused stop keeps the turn cancellable), and a
            # Claude interrupt / OpenCode abort can take a few seconds — give it
            # room so a slow-but-successful stop isn't read-timed-out into a 500.
            timeout=httpx.Timeout(30.0, connect=1.0),
        ) as client:
            resp = await client.post(f"/internal/cancel/{session_id}")
            _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(30.0, connect=1.0),
        ) as client:
            resp = await client.post("/internal/running-agents/end", json=payload)
            _validate_response(resp, endpoint)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def send_now(session_id: str, *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Ask the controller to run a session's send-while-busy queue immediately
    ("立即发送"): interrupt any running turn + flush the queue. Returns
    ``{status_code, body}``; raises ``InternalServerUnavailable`` on socket
    failure so the UI route can degrade.
    """

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            # send-now interrupts the running turn before flushing, and that
            # backend stop can take a few seconds — match the cancel timeout so a
            # slow-but-successful interrupt isn't read-timed-out.
            timeout=httpx.Timeout(30.0, connect=1.0),
        ) as client:
            resp = await client.post(f"/internal/send-now/{session_id}")
            _validate_response(resp, endpoint)
    except _SOCKET_ERRORS as exc:
        raise InternalServerUnavailable(str(exc)) from exc
    return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}


async def turn_state(session_id: str, *, socket_path: Optional[Path] = None) -> dict[str, Any]:
    """Query whether a turn is in flight for ``session_id`` so a freshly loaded /
    reconnected Chat page can restore its Stop/working state. Returns
    ``{status_code, body}``; raises ``InternalServerUnavailable`` on socket
    failure so the route can degrade (assume idle)."""

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(1.0, connect=0.2),
        ) as client:
            resp = await client.get(f"/internal/turn-state/{session_id}")
            _validate_response(resp, endpoint)
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

    endpoint = _resolve_endpoint(socket_path)
    transport = _async_transport(endpoint)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(3.0, connect=0.5),
        ) as client:
            resp = await client.get("/internal/running-agents")
            _validate_response(resp, endpoint)
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
        endpoint = _resolve_endpoint(socket_path)
        transport = _async_transport(endpoint)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=endpoint.base_url,
            headers=endpoint.headers,
            timeout=httpx.Timeout(2.0, connect=1.0),
        ) as client:
            resp = await client.get("/internal/health")
            _validate_response(resp, endpoint)
            return resp.status_code == 200 and (resp.json() or {}).get("ok") is True
    except Exception:
        return False
