"""Tests for ``core.internal_server`` — the controller-side Unix socket
ASGI app that exposes ``POST /internal/dispatch_async`` (fire-and-forget turn
dispatch) plus the turn-control surface (cancel / send-now / turn-state) for
the Web UI / CLI callers.

We exercise three layers:

1. The app's request/response shape via ``httpx.ASGITransport`` (no
   actual socket; locks the contract independent of uvicorn).
2. The fire-and-forget dispatch lifecycle: the turn is held open (in_flight)
   and its ``turn.start`` / ``turn.end`` published on the bus, the reply
   itself arriving over ``message.new`` rather than the response.
3. The boot-time socket file lifecycle (default path + chmod).
"""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import internal_server, session_turns
from core.services.dispatch import SOURCE_HUMAN, SOURCE_SCHEDULED, dispatch_turn
from modules.im import MessageContext


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _seed_project_workdir(conn, scope_id: str, workdir: Path, *, now: str = "2026-05-31T00:00:00Z") -> None:
    from storage.models import scope_settings

    conn.execute(
        scope_settings.insert().values(
            scope_id=scope_id,
            enabled=1,
            role=None,
            workdir=str(workdir),
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
            require_mention=None,
            settings_version=1,
            settings_json="{}",
            created_at=now,
            updated_at=now,
        )
    )


def _build_controller_double(handler=None):
    """A MagicMock controller whose ``message_handler.handle_user_message``
    can be patched to emit chunks via the real ``_stream_chunk`` hook.

    It carries a *real* turn-sink registry (not MagicMock auto-attrs) so
    ``dispatch_turn`` and ``_stream_chunk`` interoperate exactly as in
    production: dispatch_turn registers the sink, the handler's emits
    resolve it by session key, and a result emit releases the dispatch.
    """

    controller = MagicMock()
    controller.message_handler = MagicMock()
    controller.message_handler.handle_user_message = AsyncMock(side_effect=handler or (lambda ctx, text: None))

    sinks: dict = {}
    controller.active_turn_sinks = sinks
    controller._get_session_key = lambda ctx: f"{getattr(ctx, 'platform', None)}::{getattr(ctx, 'channel_id', None)}"

    def _register(session_key, *, on_chunk, done_event, turn_token=None, context=None):
        sinks[session_key] = {"on_chunk": on_chunk, "done_event": done_event, "turn_token": turn_token}

    controller.register_turn_sink = _register

    def _pop(session_key, done_event=None):
        s = sinks.get(session_key)
        if s is None:
            return
        if done_event is not None and s.get("done_event") is not done_event:
            return
        sinks.pop(session_key, None)

    controller.pop_turn_sink = _pop
    controller.get_turn_sink = lambda session_key: sinks.get(session_key)

    def _mark_turn_complete(ctx):
        sink = sinks.get(controller._get_session_key(ctx))
        if sink and sink.get("done_event") is not None:
            sink["done_event"].set()

    controller.mark_turn_complete = _mark_turn_complete

    # Cancel reuses the IM /stop path to interrupt the backend turn.
    controller.command_handler = MagicMock()
    controller.command_handler.handle_stop = AsyncMock(return_value=True)

    # ``_t`` returns the key verbatim so refusal chunks stay JSON-serializable
    # (a bare MagicMock would blow up ``json.dumps`` in ``_sse_event``).
    controller._t = lambda key, **kwargs: key
    return controller


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def test_default_socket_path_lives_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("VIBE_INTERNAL_DISPATCH_SOCKET", raising=False)
    path = internal_server.default_socket_path()
    assert path.name == "dispatch.sock"
    assert tmp_path in path.parents


def test_default_socket_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "dispatch.sock"
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(target))

    assert internal_server.default_socket_path() == target


def test_bind_socket_prebinds_unix_listener():
    # macOS has a short sockaddr_un limit, and pytest tmp paths can exceed it.
    with tempfile.TemporaryDirectory(prefix="vr-") as tmp:
        target = Path(tmp) / "dispatch.sock"
        listener, bound = internal_server._bind_socket(target)

        try:
            assert bound == target.resolve()
            assert target.exists()
            assert listener.family == socket.AF_UNIX
            assert listener.type == socket.SOCK_STREAM
        finally:
            listener.close()
            target.unlink(missing_ok=True)


def test_create_app_exposes_minimal_endpoints():
    app = internal_server.create_app(_build_controller_double())
    routes = {(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")}
    # Endpoints locked by the design doc §7.4 v1 row + the health probe. Both
    # dispatch shapes exist: ``/internal/dispatch_async`` (fire-and-forget, the
    # Chat page) and the streaming ``/internal/dispatch`` (the Show-page dispatch
    # flow re-publishes its SSE chunks as ``show.dispatch``).
    assert ("/internal/health", ("GET",)) in routes
    assert ("/internal/dispatch_async", ("POST",)) in routes
    assert ("/internal/reconcile-platforms", ("POST",)) in routes
    assert ("/internal/reconcile-agent-backends", ("POST",)) in routes
    assert ("/internal/cancel/{session_id}", ("POST",)) in routes
    assert ("/internal/dispatch", ("POST",)) in routes
    assert ("/internal/events", ("GET",)) in routes
    assert ("/internal/events", ("POST",)) in routes


def test_streaming_dispatch_publishes_single_bus_lifecycle(monkeypatch):
    """The Show-page stream owns one bus lifecycle because it bypasses the FSM."""
    from core import inbox_events

    context = MessageContext(user_id="workbench", channel_id="ses_show", platform="avibe")

    async def _build_payload(_payload):
        return "edit the show page", context

    async def _dispatch_turn(_controller, _context, _text, *, on_chunk=None):
        assert on_chunk is not None
        await on_chunk({"kind": "result", "text": "done"})

    monkeypatch.setattr(internal_server, "_build_dispatch_payload", _build_payload)
    monkeypatch.setattr(internal_server, "dispatch_turn", _dispatch_turn)
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    lifecycle = []

    async def _go():
        subscription_id = inbox_events.bus.subscribe_callback(
            lambda event_type, data: lifecycle.append((event_type, data))
            if event_type in {"turn.start", "turn.end"}
            else None
        )
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(
                    "/internal/dispatch",
                    json={"session_id": "ses_show", "text": "edit the show page"},
                )
        finally:
            inbox_events.bus.unsubscribe(subscription_id)

    response = asyncio.run(_go())

    assert response.status_code == 200
    assert lifecycle == [
        ("turn.start", {"session_id": "ses_show"}),
        ("turn.end", {"session_id": "ses_show"}),
    ]


# ---------------------------------------------------------------------
# ASGI round-trips
# ---------------------------------------------------------------------


async def _health_round_trip():
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/internal/health")
    return resp


def test_health_endpoint():
    resp = asyncio.run(_health_round_trip())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "vibe-remote-internal", "version": 1}


async def _publish_event_round_trip():
    from core import inbox_events

    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)
    sub_id, queue = inbox_events.bus.subscribe()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/internal/events",
                json={
                    "type": "vaults.updated",
                    "data": {"scope": "request", "request_id": "vreq_1", "request_status": "pending"},
                },
            )
            bad_resp = await client.post("/internal/events", json={"type": "unsupported", "data": {}})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        return resp, bad_resp, event
    finally:
        inbox_events.bus.unsubscribe(sub_id)


def test_publish_event_endpoint_emits_allowlisted_bus_event():
    resp, bad_resp, event = asyncio.run(_publish_event_round_trip())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert bad_resp.status_code == 400
    assert event == (
        "vaults.updated",
        {"scope": "request", "request_id": "vreq_1", "request_status": "pending"},
    )


def test_reconcile_platforms_endpoint_calls_controller(monkeypatch):
    controller = _build_controller_double()
    calls = []

    async def reconcile_platforms(config):
        calls.append(config)
        return {"ok": True, "added": ["discord"]}

    controller.reconcile_platforms = reconcile_platforms
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: "v2-config")
    monkeypatch.setattr("config.v2_compat.to_app_config", lambda config: f"compat:{config}")
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/reconcile-platforms")

    resp = asyncio.run(_go())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "added": ["discord"]}
    assert calls == ["compat:v2-config"]


def test_reconcile_platforms_endpoint_reports_controller_failure(monkeypatch):
    controller = _build_controller_double()

    async def reconcile_platforms(config):
        raise RuntimeError("IM thread for discord did not stop within timeout")

    controller.reconcile_platforms = reconcile_platforms
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: "v2-config")
    monkeypatch.setattr("config.v2_compat.to_app_config", lambda config: f"compat:{config}")
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/reconcile-platforms")

    resp = asyncio.run(_go())

    assert resp.status_code == 500
    assert resp.json() == {"ok": False, "error": "IM thread for discord did not stop within timeout"}


def test_reconcile_agent_backends_endpoint_calls_controller():
    controller = _build_controller_double()
    calls = []

    async def reconcile_agent_backends(backends):
        calls.append(backends)
        return {
            "ok": True,
            "backends": backends,
            "states": {backend: "restarted" for backend in backends},
        }

    controller.reconcile_agent_backends = reconcile_agent_backends
    app = internal_server.create_app(controller)

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/reconcile-agent-backends",
                json={"backends": ["codex", "opencode"]},
            )

    resp = asyncio.run(_go())

    assert resp.status_code == 200
    assert resp.json()["states"] == {
        "codex": "restarted",
        "opencode": "restarted",
    }
    assert calls == [["codex", "opencode"]]


def test_reconcile_agent_backends_endpoint_rejects_invalid_shape():
    app = internal_server.create_app(_build_controller_double())

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/internal/reconcile-agent-backends",
                json={"backends": "codex"},
            )

    resp = asyncio.run(_go())

    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False,
        "error": "backends must be a list of strings",
    }


async def _dispatch_round_trip(body: dict) -> httpx.Response:
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/internal/dispatch_async", json=body)


def test_dispatch_rejects_missing_text():
    # Payload validation runs before any turn/queue work, so a bad request 400s
    # the same way on the fire-and-forget endpoint.
    resp = asyncio.run(_dispatch_round_trip({"session_id": "s1"}))
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["ok"] is False
    assert "text" in payload["error"]


def test_dispatch_rejects_missing_session_id():
    resp = asyncio.run(_dispatch_round_trip({"text": "hi"}))
    assert resp.status_code == 400
    assert "session_id" in resp.json()["error"]


def test_register_turn_sink_ignores_duplicate_and_pop_is_identity_guarded():
    """Streaming turns are serialized per session (dispatch_turn rejects a
    concurrent one). As defense in depth, register_turn_sink must NOT clobber
    an in-flight sink, and pop_turn_sink must only remove the sink whose
    done_event matches the caller's — so no stale turn can satisfy or evict
    another turn's sink. The sink registry is owned by SessionTurnManager (the
    Controller methods are thin delegations)."""
    mgr = session_turns.SessionTurnManager()
    first = asyncio.Event()
    mgr.register_turn_sink("avibe::s", on_chunk=AsyncMock(), done_event=first)
    second = asyncio.Event()
    mgr.register_turn_sink("avibe::s", on_chunk=AsyncMock(), done_event=second)

    # The in-flight sink is kept; the duplicate is dropped and NOT released.
    assert mgr.active_turn_sinks["avibe::s"]["done_event"] is first
    assert not first.is_set()

    # pop is identity-guarded: a non-matching done_event is a no-op.
    mgr.pop_turn_sink("avibe::s", second)
    assert "avibe::s" in mgr.active_turn_sinks
    mgr.pop_turn_sink("avibe::s", first)
    assert "avibe::s" not in mgr.active_turn_sinks


def test_dispatch_rejects_concurrent_same_session_turn():
    """dispatch_turn serializes per session: when a streaming turn is already
    in flight (a sink is registered), a second streaming dispatch is refused
    with a terminal error chunk and never starts a competing agent turn —
    so two streams can't race over one session and cross-feed."""
    chunks: list[dict] = []

    async def on_chunk(env):
        chunks.append(env)

    handler_calls: list = []

    async def handler(ctx, text):
        handler_calls.append(text)

    controller = _build_controller_double(handler=handler)
    controller._t = lambda key, **kw: f"i18n:{key}"
    ctx = MessageContext(user_id="U", channel_id="C", platform="avibe")
    # Simulate a streaming turn already in flight for this session.
    controller.register_turn_sink(
        controller._get_session_key(ctx), on_chunk=AsyncMock(), done_event=asyncio.Event()
    )

    asyncio.run(dispatch_turn(controller, ctx, "second", on_chunk=on_chunk))

    assert handler_calls == [], "a concurrent turn must not start the agent"
    assert any(c.get("kind") == "error" for c in chunks), "a terminal error chunk must be emitted"


def test_dispatch_forwards_session_routing_into_platform_specific(monkeypatch, tmp_path):
    """Regression for the Codex P1: ``/internal/dispatch_async`` must hand the
    workbench session's agent / model / effort to ``MessageHandler`` via
    ``platform_specific["agent_session_target"]`` + ``vibe_agent_name``
    so the Chat header's chosen agent is actually used instead of the
    controller's default routing.
    """

    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_routing",
            now="2026-05-26T13:00:00Z",
        )
        _seed_project_workdir(conn, scope_id, tmp_path, now="2026-05-26T13:00:00Z")
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="contract-bot",
            model="claude-sonnet-4-6",
            reasoning_effort="high",
            metadata={
                "created_via": "session_fork",
                "fork_source_session_id": "ses-source",
                "fork_source_native_session_id": "thread-source",
                "fork_source_backend": "claude",
            },
        )
    session_id = session["id"]

    captured: dict = {}

    async def capture(ctx, text):
        captured["platform_specific"] = dict(ctx.platform_specific or {})
        # Release the held turn the way a real result emit would so the
        # fire-and-forget dispatch settles promptly.
        controller.mark_turn_complete(ctx)

    controller = _build_controller_double(handler=capture)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/internal/dispatch_async", json={"session_id": session_id, "text": "hi"})
            assert resp.status_code == 202
        # Fire-and-forget: wait for the background turn to run + capture.
        for _ in range(200):
            if "platform_specific" in captured and session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)

    asyncio.run(_go())
    payload = captured["platform_specific"]
    assert payload.get("workbench_session_id") == session_id
    assert payload.get("vibe_agent_name") == "contract-bot"
    target = payload.get("agent_session_target") or {}
    assert target.get("agent_name") == "contract-bot"
    assert target.get("agent_backend") == "claude"
    assert target.get("model") == "claude-sonnet-4-6"
    assert target.get("reasoning_effort") == "high"
    # session_anchor is carried so resume binds by the stored anchor after a
    # restart instead of a computed avibe_<id> (Codex P2). Workbench sessions
    # self-anchor to their id.
    assert target.get("session_anchor") == session_id
    assert target.get("metadata", {}).get("fork_source_native_session_id") == "thread-source"


def test_dispatch_async_starts_turn_and_returns_202(monkeypatch, tmp_path):
    """The fire-and-forget path starts the turn and returns 202 immediately.
    It still holds the turn open (via a no-op on_chunk) so ``in_flight`` is set
    for the turn's lifetime, then released when the turn completes — the reply
    itself reaches the browser over ``message.new``, not this response.

    It also publishes the session-level ``turn.start`` / ``turn.end`` lifecycle
    on the inbox bus (the browser's working-indicator signal)."""
    from core import inbox_events
    from storage.importer import ensure_sqlite_state

    # dispatch_async reads the queue (to preserve order after a Stop), so it needs
    # an initialized state DB even on the empty-queue happy path.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    started = asyncio.Event()

    async def handler(ctx, text):
        started.set()
        # Release the held turn the way a real result emit would.
        controller.mark_turn_complete(ctx)
        return None

    controller = _build_controller_double(handler=handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        sub_id, queue = inbox_events.bus.subscribe()
        events: list[str] = []
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post("/internal/dispatch_async", json={"session_id": "ses_a", "text": "hi"})
            await asyncio.wait_for(started.wait(), timeout=3)
            for _ in range(100):
                if "ses_a" not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)
            # Drain the bus: turn.start (at accept) + turn.end (at settle).
            for _ in range(2):
                try:
                    evt, _data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    events.append(evt)
                except asyncio.TimeoutError:
                    break
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return resp, events

    resp, events = asyncio.run(_go())
    assert resp.status_code == 202
    assert resp.json()["ok"] is True
    controller.message_handler.handle_user_message.assert_awaited()
    assert "ses_a" not in app.state.in_flight_dispatches, "slot released after the turn"
    assert events == ["turn.start", "turn.end"], "publishes session turn lifecycle on the bus"


def test_dispatch_async_no_terminal_result_keeps_session_in_flight(monkeypatch, tmp_path):
    """There is NO turn-duration timeout (Phase 1a): a turn whose backend never
    emits a terminal result stays in_flight indefinitely — the slot is freed ONLY
    by a real terminal result or a cancel, never by any timer. A long-running
    agent can run for hours and must keep its Stop control the whole time.

    We patch ``dispatch_turn`` to a coroutine that just sleeps (never fires the
    turn's done_event), confirm the session is still held in_flight after a beat,
    then cancel to clean up.
    """
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    started = asyncio.Event()

    async def _never_settles(ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None):
        # Model a long agent turn: the backend accepted the prompt but hasn't
        # produced its terminal result yet. dispatch_turn would normally hold on
        # ``await done.wait()`` with no timeout — emulate that by just sleeping so
        # the turn never settles on its own.
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(session_turns, "dispatch_turn", _never_settles)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    captured: dict = {}

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/internal/dispatch_async", json={"session_id": "ses_long", "text": "hi"})
            assert resp.status_code == 202
            await asyncio.wait_for(started.wait(), timeout=3)
            # Give any (nonexistent) timer ample time to fire, then confirm the slot
            # is STILL held — no timer auto-freed it.
            await asyncio.sleep(0.1)
            entry = app.state.in_flight_dispatches.get("ses_long")
            captured["held"] = entry is not None and not entry.task.done()
            # Only a real cancel frees the slot — clean up so the loop tears down.
            resp_cancel = await client.post("/internal/cancel/ses_long")
            captured["cancel_status"] = resp_cancel.status_code
            for _ in range(200):
                if "ses_long" not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)
            captured["freed_after_cancel"] = "ses_long" not in app.state.in_flight_dispatches

    asyncio.run(_go())
    assert captured["held"] is True, "a turn with no terminal result is NOT auto-freed by any timer"
    assert captured["cancel_status"] == 200, "the user's Stop ends the wedged turn"
    assert captured["freed_after_cancel"] is True, "only a cancel (or terminal result) frees the slot"


def test_dispatch_async_enqueues_during_busy_turn(monkeypatch, tmp_path):
    """A dispatch for a session that already has a turn in flight ENQUEUES
    (send-while-busy) instead of refusing: it atomically re-types the
    pre-persisted user row as queued and returns 202 {queued}, and never starts
    a competing agent turn. The row flushes when the running turn ends."""
    from core.services import sessions as sessions_service

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_enq", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        # The UI reserves the user row as 'pending' before dispatching; the
        # controller promotes it to 'queued' when it finds a turn in flight.
        user_row = messages_service.append(
            conn, scope_id=scope_id, session_id=session["id"], platform="avibe", author="user",
            source="user", message_type=messages_service.PENDING_TYPE, text="while busy",
        )
    session_id = session["id"]

    from core.inbox_events import bus

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=task,
            context=MessageContext(user_id="U", channel_id="C", platform="avibe"),
        )
        sub_id, events = bus.subscribe()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/internal/dispatch_async",
                    json={"session_id": session_id, "text": "while busy", "user_message_id": user_row["id"]},
                )
        finally:
            task.cancel()
        # bus.publish defers delivery via loop.call_soon_threadsafe; yield so the
        # scheduled puts land before we drain.
        await asyncio.sleep(0.05)
        published = []
        while not events.empty():
            published.append(events.get_nowait())
        bus.unsubscribe(sub_id)
        return resp, published

    resp, published = asyncio.run(_go())
    assert resp.status_code == 202
    assert resp.json()["queued"] is True
    controller.message_handler.handle_user_message.assert_not_awaited()
    # Enqueue surfaces the queue growth immediately so the UI reflects it without
    # waiting for the flush (queue.updated-on-enqueue, #3336001455).
    assert ("queue.updated", {"session_id": session_id}) in published
    with engine.connect() as conn:
        # The row was atomically re-typed to queued (now out of the transcript).
        assert [q["text"] for q in messages_service.list_queued(conn, session_id)] == ["while busy"]
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert transcript["messages"] == []


def test_async_dispatch_flushes_queue_on_turn_end(monkeypatch, tmp_path):
    """When a turn ends, messages queued (send-while-busy) during it are popped,
    merged (newline-joined) into ONE user row, and run as the next turn —
    draining the queue. Exercises the controller-side flush wiring end to end."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_flush", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
    session_id = session["id"]

    seen_texts: list[str] = []

    async def handler(ctx, text):
        seen_texts.append(text)
        # Simulate the user queueing two messages WHILE the first turn runs (the
        # real flow — queued rows only exist during an active turn).
        if text == "first turn":
            with engine.begin() as conn:
                messages_service.append(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type=messages_service.QUEUED_TYPE,
                    text="q1",
                    author_id="remote:user-a",
                    metadata={"_web_push_user_key": "remote:user-a"},
                )
                messages_service.append(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type=messages_service.QUEUED_TYPE,
                    text="q2",
                )
        controller.mark_turn_complete(ctx)  # release each turn immediately
        return None

    controller = _build_controller_double(handler=handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post("/internal/dispatch_async", json={"session_id": session_id, "text": "first turn"})
        # Wait for the first turn AND the flush turn to both drain the queue.
        for _ in range(200):
            if len(seen_texts) >= 2 and session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)

    asyncio.run(_go())
    # First the user's turn, then ONE merged flush turn for the two queued msgs.
    assert seen_texts == ["first turn", "q1\nq2"]
    with engine.connect() as conn:
        assert messages_service.list_queued(conn, session_id) == []
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert [m["text"] for m in transcript["messages"]] == ["q1\nq2"], "the flush persisted one merged user row"
    assert transcript["messages"][0]["author_id"] == "remote:user-a"
    assert transcript["messages"][0]["metadata"]["_web_push_user_key"] == "remote:user-a"


def test_cancel_does_not_flush_queue(monkeypatch, tmp_path):
    """A user Stop interrupts the turn but must NOT flush the queue — the user
    asked to keep queued messages on stop ('不清空队列'). The queued rows survive
    the cancellation; only a natural turn end (or send-now) runs them."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_noflush", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
    session_id = session["id"]

    started = asyncio.Event()

    async def long_handler(ctx, text):
        started.set()
        await asyncio.sleep(5)  # held until the test cancels it
        return None

    controller = _build_controller_double(handler=long_handler)
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post("/internal/dispatch_async", json={"session_id": session_id, "text": "first"})
            await asyncio.wait_for(started.wait(), timeout=3)
            # Queue a message while the turn runs, then Stop.
            with engine.begin() as conn:
                messages_service.enqueue_queued(conn, scope_id=scope_id, session_id=session_id, text="q1")
            await client.post(f"/internal/cancel/{session_id}")
            for _ in range(200):
                if session_id not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)

    asyncio.run(_go())
    with engine.connect() as conn:
        queued = messages_service.list_queued(conn, session_id)
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert [q["text"] for q in queued] == ["q1"], "Stop must keep the queue intact"
    assert transcript["messages"] == [], "Stop must not flush the queue into a turn"


def test_turn_state_reflects_in_flight():
    """``/internal/turn-state`` reports whether a turn is running, so a freshly
    loaded / reconnected Chat page can restore its Stop state."""
    controller = _build_controller_double()
    controller.agent_service.runtime_turn_started.return_value = True
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            idle = (await client.get("/internal/turn-state/ses_ts")).json()
            # Simulate an in-flight turn.
            task = asyncio.create_task(asyncio.sleep(60))
            app.state.in_flight_dispatches["ses_ts"] = session_turns.Turn(
                task=task,
                context=MessageContext(
                    user_id="U",
                    channel_id="C",
                    platform="avibe",
                    platform_specific={"agent_session_target": {"agent_backend": "opencode"}},
                ),
            )
            busy = (await client.get("/internal/turn-state/ses_ts")).json()
            task.cancel()
            return idle, busy

    idle, busy = asyncio.run(_go())
    assert idle["in_flight"] is False
    assert busy["in_flight"] is True
    assert busy["native_turn_started"] is True
    assert busy["backend"] == "opencode"


def test_cancel_returns_404_when_session_not_in_flight():
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/internal/cancel/ses_unknown")

    resp = asyncio.run(_go())
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "not_in_flight"


def test_cancel_releases_stale_turn_when_backend_not_active():
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    statuses = []
    notices = []
    controller.set_agent_status = lambda session_id, status: statuses.append((session_id, status))

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": "ses_stale"},
        )
        app.state.in_flight_dispatches["ses_stale"] = session_turns.Turn(task=task, context=context)

        async def _not_active(_context):
            notices.append(_context.platform_specific.get("suppress_stop_no_active_notice"))
            _context.platform_specific["stop_failure_reason"] = "not_active"
            return False

        controller.command_handler.handle_stop = AsyncMock(side_effect=_not_active)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/internal/cancel/ses_stale")
        for _ in range(200):
            if "ses_stale" not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)
        return resp, task

    resp, task = asyncio.run(_go())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stale_released"
    assert body["reason"] == "not_active"
    assert task.cancelled()
    assert "ses_stale" not in app.state.in_flight_dispatches
    assert statuses == [("ses_stale", "idle")]
    assert notices == [True]


def test_cancel_waits_for_stale_dispatch_cleanup_before_releasing():
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    controller.set_agent_status = lambda session_id, status: None

    async def _go():
        done_event = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": "ses_stale_cleanup"},
        )

        async def _stale_dispatch():
            controller.register_turn_sink(
                controller._get_session_key(context),
                on_chunk=AsyncMock(),
                done_event=done_event,
                turn_token="old-turn",
            )
            try:
                await asyncio.sleep(60)
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                controller.pop_turn_sink(controller._get_session_key(context), done_event)

        task = asyncio.create_task(_stale_dispatch())
        for _ in range(200):
            if controller.get_turn_sink("avibe::C") is not None:
                break
            await asyncio.sleep(0.01)
        assert controller.get_turn_sink("avibe::C") is not None
        app.state.in_flight_dispatches["ses_stale_cleanup"] = session_turns.Turn(task=task, context=context)

        async def _not_active(_context):
            _context.platform_specific["stop_failure_reason"] = "not_active"
            return False

        controller.command_handler.handle_stop = AsyncMock(side_effect=_not_active)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            cancel_task = asyncio.create_task(client.post("/internal/cancel/ses_stale_cleanup"))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            await asyncio.sleep(0.02)
            assert not cancel_task.done()
            assert "ses_stale_cleanup" in app.state.in_flight_dispatches
            allow_cleanup.set()
            resp = await cancel_task
        return resp, task

    resp, task = asyncio.run(_go())
    assert resp.status_code == 200
    assert resp.json()["status"] == "stale_released"
    assert task.cancelled()
    assert controller.get_turn_sink("avibe::C") is None


def test_cancel_keeps_turn_when_backend_interrupt_failed():
    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            platform_specific={"agent_session_id": "ses_failed_stop"},
        )
        app.state.in_flight_dispatches["ses_failed_stop"] = session_turns.Turn(task=task, context=context)

        async def _interrupt_failed(_context):
            _context.platform_specific["stop_failure_reason"] = "interrupt_failed"
            return False

        controller.command_handler.handle_stop = AsyncMock(side_effect=_interrupt_failed)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/internal/cancel/ses_failed_stop")
        held = "ses_failed_stop" in app.state.in_flight_dispatches and not task.done()
        task.cancel()
        return resp, held

    resp, held = asyncio.run(_go())
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "stop_failed"
    assert body["reason"] == "interrupt_failed"
    assert held is True


def test_release_for_backend_refresh_cancels_matching_turn_and_sets_idle():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    statuses = []
    controller.set_agent_status = lambda session_id, status: statuses.append((session_id, status))

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        ctx = MessageContext(user_id="U", channel_id="ses_codex", platform="avibe")
        ctx.platform_specific = {
            "agent_session_id": "ses_codex",
            "agent_session_target": {"agent_backend": "codex"},
        }
        manager.in_flight["ses_codex"] = session_turns.Turn(task=task, context=ctx)
        manager.begin_backend_drain("codex")

        released = await manager.release_for_backend_refresh(
            backend="codex",
            base_session_ids={"ses_codex"},
        )
        try:
            await task
        except asyncio.CancelledError:
            pass
        return released, task.cancelled()

    released, cancelled = asyncio.run(_go())

    assert released == 1
    assert cancelled is True
    assert statuses == [("ses_codex", "idle")]
    assert manager._deferred_restart_sessions == {"codex": {"ses_codex"}}


def test_release_for_backend_refresh_leaves_other_backend_turn_running():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    statuses = []
    controller.set_agent_status = lambda session_id, status: statuses.append((session_id, status))

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        ctx = MessageContext(user_id="U", channel_id="ses_claude", platform="avibe")
        ctx.platform_specific = {
            "agent_session_id": "ses_claude",
            "agent_session_target": {"agent_backend": "claude"},
        }
        manager.in_flight["ses_claude"] = session_turns.Turn(task=task, context=ctx)
        try:
            released = await manager.release_for_backend_refresh(
                backend="codex",
                base_session_ids={"ses_claude"},
            )
            return released, task.done()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    released, done = asyncio.run(_go())

    assert released == 0
    assert done is False
    assert statuses == []


def test_backend_drain_queues_idle_session_without_dispatching():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    enqueue = Mock()
    context = MessageContext(user_id="U", channel_id="ses_codex", platform="avibe")
    context.platform_specific = {
        "agent_session_id": "ses_codex",
        "agent_session_target": {"agent_backend": "codex"},
    }

    async def _go():
        manager.begin_backend_drain("codex")
        return await manager.submit("ses_codex", context, "next", enqueue=enqueue)

    result = asyncio.run(_go())

    assert result == "enqueued"
    enqueue.assert_called_once_with()
    assert manager.is_in_flight("ses_codex") is False
    assert manager._deferred_restart_sessions == {"codex": {"ses_codex"}}


def test_backend_drain_resolves_inherited_default_agent_backend():
    controller = _build_controller_double()
    controller.resolve_agent_for_context = Mock(return_value="codex")
    manager = session_turns.SessionTurnManager(controller)
    enqueue = Mock()
    context = MessageContext(user_id="U", channel_id="ses_default", platform="avibe")
    context.platform_specific = {
        "agent_session_id": "ses_default",
        "agent_session_target": {"agent_name": None, "agent_backend": None},
    }

    async def _go():
        manager.begin_backend_drain("codex")
        return await manager.submit("ses_default", context, "next", enqueue=enqueue)

    assert asyncio.run(_go()) == "enqueued"
    enqueue.assert_called_once_with()
    controller.resolve_agent_for_context.assert_called_once_with(context)
    assert manager._deferred_restart_sessions == {"codex": {"ses_default"}}


def test_backend_drain_flushes_deferred_session_after_cutover():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    manager.flush_queue = AsyncMock(return_value=True)
    manager.begin_backend_drain("codex")
    manager._deferred_restart_sessions["codex"].add("ses_codex")

    asyncio.run(manager.end_backend_drain("codex"))

    manager.flush_queue.assert_awaited_once_with("ses_codex")
    assert "codex" not in manager._draining_backends


def test_backend_drain_blocks_direct_queue_flush():
    controller = _build_controller_double()
    context = MessageContext(user_id="U", channel_id="ses_codex", platform="avibe")
    context.platform_specific = {
        "agent_session_id": "ses_codex",
        "agent_session_target": {"agent_backend": "codex"},
    }
    manager = session_turns.SessionTurnManager(controller, build_context=lambda _session_id: context)
    manager._run = AsyncMock()  # type: ignore[method-assign]
    manager.begin_backend_drain("codex")

    assert asyncio.run(manager.flush_queue("ses_codex")) is False
    manager._run.assert_not_awaited()
    assert manager._deferred_restart_sessions == {"codex": {"ses_codex"}}


def test_stop_during_backend_drain_keeps_existing_queue_parked():
    controller = _build_controller_double()
    manager = session_turns.SessionTurnManager(controller)
    context = MessageContext(user_id="U", channel_id="ses_codex", platform="avibe")
    context.platform_specific = {
        "agent_session_id": "ses_codex",
        "agent_session_target": {"agent_backend": "codex"},
    }

    async def _go():
        task = asyncio.create_task(asyncio.sleep(60))
        manager.in_flight["ses_codex"] = session_turns.Turn(task=task, context=context)
        manager.begin_backend_drain("codex")
        manager._deferred_restart_sessions["codex"].add("ses_codex")
        result = await manager.cancel("ses_codex")
        await asyncio.gather(task, return_exceptions=True)
        return result

    result = asyncio.run(_go())

    assert result["status"] == "cancel_requested"
    assert manager._deferred_restart_sessions == {"codex": set()}


# ---------------------------------------------------------------------
# Dispatcher hook contract
# ---------------------------------------------------------------------


def test_dispatch_turn_registers_sink_for_dispatcher_hook():
    """Locks the contract between ``dispatch_turn`` and the dispatcher's
    ``_stream_chunk`` helper: the streaming ``on_chunk`` is registered as a
    per-session turn sink (resolvable by session key while the turn runs)
    and cleaned up afterward — not stashed on the per-turn context.
    """

    async def on_chunk(envelope):
        pass

    seen: dict = {}

    async def capture(ctx, text):
        sink = controller.get_turn_sink(controller._get_session_key(ctx))
        seen["on_chunk"] = sink["on_chunk"] if sink else None
        # Release the dispatch the way a real result emit would.
        if sink:
            sink["done_event"].set()

    controller = _build_controller_double(handler=capture)
    ctx = MessageContext(user_id="U", channel_id="C", platform="avibe")
    asyncio.run(dispatch_turn(controller, ctx, "ping", on_chunk=on_chunk))
    assert seen["on_chunk"] is on_chunk
    assert controller.get_turn_sink("avibe::C") is None, "sink cleaned up after the turn"


# ---------------------------------------------------------------------
# Scheduled / watch turn gate (controller.session_turn_gate)
# ---------------------------------------------------------------------


def test_scheduled_gate_idle_runs_turn_with_lifecycle(monkeypatch, tmp_path):
    """An IDLE scheduled run goes through ``_run_turn`` like a Chat turn: it
    registers ``in_flight`` + publishes ``turn.start`` / ``turn.end`` on the bus
    (so the Chat page shows the working indicator + Stop works) and calls
    ``dispatch_turn`` with ``source=SOURCE_SCHEDULED`` and the no-op chunk sink —
    NOT ``on_chunk=None``. The sink isn't about the browser (chunks are discarded;
    avibe renders from ``message.new``); it makes ``dispatch_turn`` HOLD the turn
    open until the backend's terminal result, which keeps ``in_flight`` populated
    for the scheduled turn's whole lifetime so a Chat send can't preempt a
    still-running scheduled turn (Codex P2)."""
    from core import inbox_events
    from storage.importer import ensure_sqlite_state

    # submit_scheduled reads the queue (idle → empty-queue happy path), so it
    # needs an initialized state DB.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    captured: dict = {}
    started = asyncio.Event()

    async def _fake_dispatch_turn(ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None):
        captured["source"] = source
        captured["on_chunk"] = on_chunk
        captured["text"] = text
        captured["in_flight_while_running"] = "ses_sched" in app.state.in_flight_dispatches
        started.set()

    monkeypatch.setattr(session_turns, "dispatch_turn", _fake_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    ctx = MessageContext(user_id="workbench", channel_id="ses_sched", platform="avibe")

    async def _go():
        sub_id, queue = inbox_events.bus.subscribe()
        events: list[str] = []
        try:
            await controller.session_turn_gate.submit_scheduled("ses_sched", ctx, "digest please")
            await asyncio.wait_for(started.wait(), timeout=3)
            for _ in range(100):
                if "ses_sched" not in app.state.in_flight_dispatches:
                    break
                await asyncio.sleep(0.02)
            for _ in range(2):
                try:
                    evt, _data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    events.append(evt)
                except asyncio.TimeoutError:
                    break
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return events

    events = asyncio.run(_go())
    assert captured["source"] == SOURCE_SCHEDULED, "scheduled run dispatches on the scheduler path"
    # A scheduled run passes the no-op chunk SINK (callable, NOT None) so dispatch_turn
    # HOLDS the turn open to its terminal result — same as a Chat turn — instead of an
    # async backend returning at prompt-submit and freeing the slot (Codex P2). The sink
    # discards chunks; the reply still surfaces over ``message.new``, not a live stream.
    assert captured["on_chunk"] is not None, "scheduled run holds the turn open via the no-op sink"
    assert callable(captured["on_chunk"]), "the held-open sink is the no-op chunk callable"
    assert captured["text"] == "digest please"
    assert captured["in_flight_while_running"] is True, "registered in_flight (Stop works) while running"
    assert events == ["turn.start", "turn.end"], "publishes the session turn lifecycle on the bus"
    assert "ses_sched" not in app.state.in_flight_dispatches, "slot released after the turn"


def test_scheduled_gate_busy_enqueues_and_leaves_chat_turn_untouched(monkeypatch, tmp_path):
    """A scheduled run for a session that already has a turn in flight ENQUEUES a
    harness-attributed ``queued`` row (so it runs AFTER the active turn via the
    existing flush) instead of preempting it — and it never starts a competing
    turn nor disturbs the in-flight Chat task (Codex P2)."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_sched_busy", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
    session_id = session["id"]

    # A scheduled run must NEVER reach dispatch_turn while busy — a call here fails
    # the test loudly.
    async def _explode_dispatch_turn(*args, **kwargs):
        raise AssertionError("a busy scheduled run must enqueue, not dispatch a turn")

    monkeypatch.setattr(session_turns, "dispatch_turn", _explode_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    ctx = MessageContext(user_id="workbench", channel_id=session_id, platform="avibe")

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        chat_task = asyncio.create_task(_busy())
        chat_ctx = MessageContext(user_id="U", channel_id="C", platform="avibe")
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(task=chat_task, context=chat_ctx)
        try:
            await controller.session_turn_gate.submit_scheduled(session_id, ctx, "scheduled while busy")
        finally:
            entry = app.state.in_flight_dispatches.get(session_id)
            # The in-flight Chat turn is undisturbed: same task object, not cancelled.
            assert entry is not None and entry.task is chat_task and not chat_task.done()
            chat_task.cancel()
        return chat_ctx

    chat_ctx = asyncio.run(_go())
    controller.message_handler.handle_user_message.assert_not_awaited()
    with engine.connect() as conn:
        queued = messages_service.list_queued(conn, session_id)
        # The queued row is drainable + carries the session's scope and harness
        # attribution; it stays OUT of the user transcript.
        transcript = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))
    assert [q["text"] for q in queued] == ["scheduled while busy"]
    assert queued[0]["scope_id"] == scope_id
    assert queued[0]["author"] == "harness"
    assert transcript["messages"] == []


def test_scheduled_gate_busy_duplicate_native_id_is_skipped(monkeypatch, tmp_path):
    """A retried harness callback already sitting in the queue is a duplicate,
    not a scheduler failure."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_sched_dup", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
    session_id = session["id"]

    async def _explode_dispatch_turn(*args, **kwargs):
        raise AssertionError("a busy scheduled run must enqueue, not dispatch a turn")

    monkeypatch.setattr(session_turns, "dispatch_turn", _explode_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    ctx = MessageContext(
        user_id="workbench",
        channel_id=session_id,
        platform="avibe",
        message_id="watch:def-watch:run-1",
        platform_specific={
            "task_execution_id": "run-1",
            "task_trigger_kind": "watch",
            "task_definition_id": "def-watch",
        },
    )

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        chat_task = asyncio.create_task(_busy())
        app.state.in_flight_dispatches[session_id] = session_turns.Turn(
            task=chat_task,
            context=MessageContext(
                user_id="U",
                channel_id="C",
                platform="avibe",
                message_id="watch:def-watch:running",
            ),
        )
        try:
            first = await controller.session_turn_gate.submit_scheduled(session_id, ctx, "first")
            second = await controller.session_turn_gate.submit_scheduled(session_id, ctx, "duplicate")
            running_ctx = MessageContext(
                user_id="workbench",
                channel_id=session_id,
                platform="avibe",
                message_id="watch:def-watch:running",
            )
            running_duplicate = await controller.session_turn_gate.submit_scheduled(
                session_id,
                running_ctx,
                "running duplicate",
            )
        finally:
            chat_task.cancel()
        return first, second, running_duplicate

    assert asyncio.run(_go()) == ("enqueued", "duplicate", "duplicate")
    with engine.connect() as conn:
        queued = messages_service.list_queued(conn, session_id)
    assert [row["text"] for row in queued] == ["first"]


def test_scheduled_gate_cancel_stops_scheduled_run(monkeypatch, tmp_path):
    """Stop works for a scheduled run: because the run goes through ``_run_turn``
    it registers the scheduled ``context`` in ``in_flight``, so
    ``/internal/cancel/{session_id}`` finds the task + reuses the IM ``/stop`` path
    to interrupt the backend (mirrors the Chat cancel test)."""
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_sched_cancel", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
    session_id = session["id"]

    started = asyncio.Event()

    async def _long_dispatch_turn(ctrl, ctx, text, *, source=SOURCE_HUMAN, on_chunk=None):
        started.set()
        await asyncio.sleep(5)  # held until the test cancels it

    monkeypatch.setattr(session_turns, "dispatch_turn", _long_dispatch_turn)

    controller = _build_controller_double()
    app = internal_server.create_app(controller)
    transport = httpx.ASGITransport(app=app)
    ctx = MessageContext(user_id="workbench", channel_id=session_id, platform="avibe")

    async def _go():
        # Start the scheduled run in the background (it holds in_flight open).
        run = asyncio.create_task(controller.session_turn_gate.submit_scheduled(session_id, ctx, "scheduled run"))
        await asyncio.wait_for(started.wait(), timeout=3)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(f"/internal/cancel/{session_id}")
        for _ in range(200):
            if session_id not in app.state.in_flight_dispatches:
                break
            await asyncio.sleep(0.02)
        run.cancel()
        return resp

    resp = asyncio.run(_go())
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancel_requested"
    # The cancel interrupted the backend through the IM /stop path with the
    # scheduled run's own context.
    controller.command_handler.handle_stop.assert_awaited_once()
    assert session_id not in app.state.in_flight_dispatches, "slot released after the scheduled run was stopped"


# --- #84: scheduled provenance survives the merge-queue --------------------------


def _seed_avibe_session_with_queue(queued):
    """Create an isolated avibe session and seed its queue (oldest first). Each
    ``queued`` entry is ``(text, scheduled_provenance | None)`` — None => a user row,
    a dict => a scheduled row carrying that provenance under the marker key."""
    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_q84", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, Path.cwd())
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        for text, prov in queued:
            messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=session["id"],
                platform="avibe",
                author=("harness" if prov is not None else "user"),
                source=("harness" if prov is not None else "user"),
                message_type=messages_service.QUEUED_TYPE,
                text=text,
                metadata=({session_turns.SCHEDULED_PROVENANCE_KEY: prov} if prov is not None else None),
                native_message_id=(prov or {}).get("message_id") if prov is not None else None,
            )
    return session["id"]


def _manager_capturing_runs():
    """A SessionTurnManager whose ``_run`` records each flushed turn's (text, source,
    suppress_delivery) instead of dispatching."""
    runs: list = []
    mgr = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda sid: MessageContext(
            user_id="U", channel_id="C", platform="avibe", platform_specific={"agent_session_id": sid}
        ),
    )

    async def _fake_run(sid, context, text, *, source=SOURCE_HUMAN):
        runs.append((text, source, context))

    mgr._run = _fake_run
    return mgr, runs


def test_flush_runs_scheduled_row_as_scheduled_with_provenance(tmp_path, monkeypatch):
    """A queued scheduled run flushes as its OWN SOURCE_SCHEDULED turn with its
    delivery provenance restored — not merged into a plain user turn (#84)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    override = {"channel_id": "slack-321", "platform": "slack"}
    session_id = _seed_avibe_session_with_queue(
        [(
            "scheduled prompt",
            {
                "message_id": "scheduled:exec-1",
                "platform_specific": {
                    "suppress_delivery": True,
                    "delivery_override": override,
                    "task_trigger_kind": "task",
                },
            },
        )]
    )
    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    # Ran as scheduled (not user), with the FULL provenance restored: the delivery
    # override (the redirect _get_target_context uses) + suppress_delivery (#84 / P1)
    # AND the stable scheduled native id for dedup (P2).
    assert (text, source) == ("scheduled prompt", SOURCE_SCHEDULED)
    assert ctx.platform_specific["suppress_delivery"] is True
    assert ctx.platform_specific["delivery_override"] == override
    assert ctx.message_id == "scheduled:exec-1"

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        assert messages_service.list_queued(conn, session_id) == []


def test_flush_coalesces_same_scheduled_callbacks_within_window(tmp_path, monkeypatch):
    """Harness callback rows stay visible as separate queued messages, but same
    watch/task callbacks inside the rolling window dispatch as one Agent turn."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, message_id: str = "watch:def-watch") -> dict:
        return {
            "message_id": message_id,
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1", "watch:def-watch:run-1")),
            ("second callback", prov("run-2", "watch:def-watch:run-2")),
            ("third callback", prov("run-3", "watch:def-watch:run-3")),
        ]
    )

    from sqlalchemy import select
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import messages

    with create_sqlite_engine().begin() as conn:
        assert [row["text"] for row in messages_service.list_queued(conn, session_id)] == [
            "first callback",
            "second callback",
            "third callback",
        ]

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert "first callback" in text
    assert "second callback" in text
    assert "third callback" in text
    assert "fired 3 times" in text
    assert "pause, remove, or update" in text
    assert ctx.platform_specific["task_definition_id"] == "def-watch"
    assert ctx.platform_specific["coalesced_queue"]["count"] == 3
    assert ctx.platform_specific["coalesced_queue"]["execution_ids"] == ["run-1", "run-2", "run-3"]
    assert ctx.platform_specific["coalesced_queue"]["native_message_ids"] == [
        "watch:def-watch:run-1",
        "watch:def-watch:run-2",
        "watch:def-watch:run-3",
    ]
    with create_sqlite_engine().begin() as conn:
        assert messages_service.list_queued(conn, session_id) == []
        dedupe_rows = conn.execute(select(messages)).mappings().all()
    dedupe_native_ids = {
        row["native_message_id"]
        for row in dedupe_rows
        if row["type"] == messages_service.HARNESS_DEDUPE_TYPE
    }
    assert dedupe_native_ids == {"watch:def-watch:run-2", "watch:def-watch:run-3"}


def test_flush_claims_every_coalesced_agent_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    for index in range(4):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"cli agent message {index + 1}", prov(request.id)))

    session_id = _seed_avibe_session_with_queue(queued)

    from sqlalchemy import update
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import messages

    with create_sqlite_engine().begin() as conn:
        rows = messages_service.list_queued(conn, session_id)
        for index, row in enumerate(rows):
            conn.execute(
                update(messages)
                .where(messages.c.id == row["id"])
                .values(created_at=f"2026-06-22T00:00:0{index}Z")
            )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert "cli agent message 1" in text
    assert "cli agent message 2" in text
    assert "cli agent message 3" in text
    assert "cli agent message 4" in text
    execution_ids = ctx.platform_specific["coalesced_queue"]["execution_ids"]
    assert len(execution_ids) == 4

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in execution_ids}
    finally:
        bg.close()

    assert stored[execution_ids[0]]["status"] == "running"
    assert [stored[run_id]["status"] for run_id in execution_ids[1:]] == ["queued", "queued", "queued"]
    assert [stored[run_id]["metadata"]["effective_run_id"] for run_id in execution_ids] == [
        execution_ids[0],
        execution_ids[0],
        execution_ids[0],
        execution_ids[0],
    ]
    assert stored[execution_ids[0]]["metadata"].get("coalesced_into_run_id") is None
    assert [stored[run_id]["metadata"]["coalesced_into_run_id"] for run_id in execution_ids[1:]] == [
        execution_ids[0],
        execution_ids[0],
        execution_ids[0],
    ]
    assert stored[execution_ids[0]]["metadata"]["workbench_queue_holds_run"] is False
    assert [stored[run_id]["metadata"]["workbench_queue_holds_run"] for run_id in execution_ids[1:]] == [
        True,
        True,
        True,
    ]


def test_flush_background_agent_run_preserves_primary_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.message_mirror import mirror_harness_inbound
    from core.scheduled_tasks import TaskExecutionStore
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import messages

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="placeholder",
        message="background prompt",
        agent_name="codex",
    )
    assert request_store.claim(request.id) is not None
    request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "background prompt",
                {
                    "message_id": f"agent_run:{request.id}",
                    "platform_specific": {
                        "task_execution_id": request.id,
                        "task_trigger_kind": "agent_run",
                        "task_definition_id": None,
                        "vibe_agent_name": "codex",
                        "suppress_delivery": True,
                    },
                },
            )
        ]
    )
    mgr, runs = _manager_capturing_runs()

    async def _mirror_run(sid, context, text, *, source=SOURCE_HUMAN):
        mirror_harness_inbound(context, text)
        runs.append((text, source, context))

    mgr._run = _mirror_run

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    with create_sqlite_engine().connect() as conn:
        rows = conn.execute(
            select(messages).where(messages.c.session_id == session_id)
        ).mappings().all()

    harness_rows = [row for row in rows if row["type"] == "harness"]
    assert [(row["native_message_id"], row["content_text"]) for row in harness_rows] == [
        (f"agent_run:{request.id}", "background prompt")
    ]
    assert [row for row in rows if row["type"] == messages_service.HARNESS_DEDUPE_TYPE] == []


def test_coalesced_agent_run_claim_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        run_ids.append(request.id)

    bg = SQLiteBackgroundTaskStore()
    try:
        assert bg.cancel_run(run_ids[1]) is True
        assert bg.claim_queued_runs_for_workbench(run_ids) == []
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert stored[run_ids[0]]["status"] == "queued"
    assert stored[run_ids[1]]["status"] == "canceled"
    assert stored[run_ids[2]]["status"] == "queued"
    assert {row["status"] for row in stored.values()} == {"queued", "canceled"}
    assert all(row["status"] != "running" for row in stored.values())
    assert stored[run_ids[0]]["metadata"]["workbench_queue_holds_run"] is True
    assert stored[run_ids[2]]["metadata"]["workbench_queue_holds_run"] is True
    assert stored[run_ids[0]]["metadata"].get("effective_run_id") is None
    assert stored[run_ids[2]]["metadata"].get("effective_run_id") is None


def test_flush_removes_stale_coalesced_agent_run_row_and_dispatches_survivors(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)

    from storage import messages_service
    from storage.db import create_sqlite_engine

    bg = SQLiteBackgroundTaskStore()
    try:
        assert bg.cancel_run(run_ids[1]) is True
    finally:
        bg.close()

    from core.inbox_events import bus

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(bus, "publish", lambda event, payload: published.append((event, payload)))

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert "cli agent message 1" in text
    assert "cli agent message 3" in text
    assert "cli agent message 2" not in text
    assert ctx.platform_specific["coalesced_queue"]["execution_ids"] == [run_ids[0], run_ids[2]]

    with create_sqlite_engine().begin() as conn:
        queued_rows = messages_service.list_queued(conn, session_id)
    assert queued_rows == []

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert stored[run_ids[0]]["status"] == "running"
    assert stored[run_ids[1]]["status"] == "canceled"
    assert stored[run_ids[2]]["status"] == "queued"
    assert stored[run_ids[0]]["metadata"]["workbench_queue_holds_run"] is False
    assert stored[run_ids[2]]["metadata"]["workbench_queue_holds_run"] is True
    assert published.count(("queue.updated", {"session_id": session_id})) >= 2


def test_flush_preserves_recovered_coalesced_agent_run_children(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str, coalesced_ids: list[str] | None = None) -> dict:
        platform_specific = {
            "task_execution_id": execution_id,
            "task_trigger_kind": "agent_run",
            "task_definition_id": None,
            "vibe_agent_name": "codex",
            "source_kind": None,
            "callback_session_id": None,
        }
        if coalesced_ids:
            platform_specific["coalesced_queue"] = {"execution_ids": coalesced_ids}
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": platform_specific,
        }

    recovered_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"recovered message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": index != 0})
        recovered_ids.append(request.id)
    new_request = request_store.enqueue_agent_run(
        session_id="placeholder",
        message="new message",
        agent_name="codex",
    )
    assert request_store.claim(new_request.id) is not None
    request_store.requeue(new_request.id, metadata={"workbench_queue_holds_run": True})

    session_id = _seed_avibe_session_with_queue(
        [
            ("recovered primary", prov(recovered_ids[0], recovered_ids)),
            ("new message", prov(new_request.id)),
        ]
    )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    _text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert ctx.platform_specific["coalesced_queue"]["execution_ids"] == [
        recovered_ids[0],
        recovered_ids[1],
        new_request.id,
    ]


def test_flush_retargets_recovered_coalesced_row_when_primary_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str, coalesced_ids: list[str]) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
                "coalesced_queue": {
                    "execution_ids": coalesced_ids,
                    "messages": [
                        {"execution_id": coalesced_ids[0], "message": "stale primary prompt"},
                        {"execution_id": coalesced_ids[1], "message": "surviving child prompt"},
                    ],
                },
            },
        }

    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"coalesced message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": index != 0})
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(
        [("stale primary prompt", prov(run_ids[0], run_ids))]
    )

    bg = SQLiteBackgroundTaskStore()
    try:
        assert bg.cancel_run(run_ids[0]) is True
    finally:
        bg.close()

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "surviving child prompt"
    assert ctx.message_id == f"agent_run:{run_ids[1]}"
    assert ctx.platform_specific["task_execution_id"] == run_ids[1]
    assert ctx.platform_specific["coalesced_queue"]["execution_ids"] == [run_ids[1]]

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()
    assert stored[run_ids[0]]["status"] == "canceled"
    assert stored[run_ids[1]]["status"] == "running"


def test_flush_retries_when_agent_run_claim_finds_late_stale_row(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage import background as background_module
    from storage.background import SQLiteBackgroundTaskStore
    from storage.models import agent_runs
    from sqlalchemy import update

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)
    real_claim = background_module.claim_queued_runs_for_workbench_in_connection
    injected = {"done": False}

    def late_stale_claim(conn, pending_run_ids, *, started_at=None):
        if not injected["done"]:
            injected["done"] = True
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_ids[1])
                .values(status="canceled", cancel_requested=1, completed_at="2026-06-22T00:00:10Z")
            )
            return []
        return real_claim(conn, pending_run_ids, started_at=started_at)

    monkeypatch.setattr(background_module, "claim_queued_runs_for_workbench_in_connection", late_stale_claim)
    monkeypatch.setattr(session_turns, "claim_queued_runs_for_workbench_in_connection", late_stale_claim, raising=False)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert "cli agent message 1" in text
    assert "cli agent message 3" in text
    assert "cli agent message 2" not in text
    assert ctx.platform_specific["coalesced_queue"]["execution_ids"] == [run_ids[0], run_ids[2]]

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert stored[run_ids[0]]["status"] == "running"
    assert stored[run_ids[1]]["status"] == "canceled"
    assert stored[run_ids[2]]["status"] == "queued"


def test_flush_does_not_coalesce_agent_runs_with_different_callback_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str, callback_session_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "callback_session_id": callback_session_id,
            },
        }

    queued: list[tuple[str, dict]] = []
    for index, callback_session_id in enumerate(["caller-a", "caller-b"]):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
            callback_session_id=callback_session_id,
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"cli agent message {index + 1}", prov(request.id, callback_session_id)))

    session_id = _seed_avibe_session_with_queue(queued)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "cli agent message 1"
    assert "coalesced_queue" not in ctx.platform_specific


def test_flush_does_not_coalesce_callback_backed_agent_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "callback_session_id": "same-caller",
            },
        }

    queued: list[tuple[str, dict]] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"callback agent message {index + 1}",
            agent_name="codex",
            callback_session_id="same-caller",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"callback agent message {index + 1}", prov(request.id)))

    session_id = _seed_avibe_session_with_queue(queued)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "callback agent message 1"
    assert "coalesced_queue" not in ctx.platform_specific


def test_flush_isolates_legacy_agent_runs_without_source_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
            },
        }

    queued: list[tuple[str, dict]] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"legacy callback message {index + 1}",
            agent_name="codex",
            callback_session_id=f"caller-{index + 1}",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"legacy callback message {index + 1}", prov(request.id)))

    session_id = _seed_avibe_session_with_queue(queued)

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert source == SOURCE_SCHEDULED
    assert text == "legacy callback message 1"
    assert "coalesced_queue" not in ctx.platform_specific


def test_flush_does_not_claim_agent_runs_when_context_build_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
                "source_kind": None,
                "callback_session_id": None,
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)

    mgr = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda _sid: (_ for _ in ()).throw(RuntimeError("context unavailable")),
    )

    assert asyncio.run(mgr.flush_queue(session_id)) is False

    from storage import messages_service
    from storage.db import create_sqlite_engine

    from storage.models import messages

    with create_sqlite_engine().begin() as conn:
        queued_rows = messages_service.list_queued(conn, session_id)
        dedupe_rows = conn.execute(
            select(messages)
            .where(messages.c.session_id == session_id)
            .where(messages.c.type == messages_service.HARNESS_DEDUPE_TYPE)
        ).mappings().all()
    assert [row["text"] for row in queued_rows] == ["cli agent message 1", "cli agent message 2"]
    assert dedupe_rows == []

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert {row["status"] for row in stored.values()} == {"queued"}
    assert all(row["metadata"]["workbench_queue_holds_run"] is True for row in stored.values())


def test_flush_resets_agent_run_claim_when_run_start_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    request_store = TaskExecutionStore()

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"agent_run:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "agent_run",
                "task_definition_id": None,
                "vibe_agent_name": "codex",
            },
        }

    queued: list[tuple[str, dict]] = []
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id="placeholder",
            message=f"cli agent message {index + 1}",
            agent_name="codex",
        )
        assert request_store.claim(request.id) is not None
        request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
        queued.append((f"cli agent message {index + 1}", prov(request.id)))
        run_ids.append(request.id)

    session_id = _seed_avibe_session_with_queue(queued)
    mgr, _runs = _manager_capturing_runs()

    async def _failing_run(*_args, **_kwargs):
        raise RuntimeError("dispatch did not start")

    mgr._run = _failing_run

    assert asyncio.run(mgr.flush_queue(session_id)) is False

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        queued_rows = messages_service.list_queued(conn, session_id)
    assert [row["text"] for row in queued_rows] == ["cli agent message 1", "cli agent message 2"]

    bg = SQLiteBackgroundTaskStore()
    try:
        stored = {run_id: bg.get_run(run_id) for run_id in run_ids}
    finally:
        bg.close()

    assert {row["status"] for row in stored.values()} == {"queued"}
    assert all(row["metadata"]["workbench_queue_holds_run"] is True for row in stored.values())
    assert all(row["metadata"].get("effective_run_id") is None for row in stored.values())


def test_flush_suppressed_segment_reserves_first_native_id_for_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "suppress_delivery": True,
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1")),
            ("second callback", prov("run-2")),
        ]
    )

    from sqlalchemy import select

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import messages

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    assert runs[0][2].message_id == "watch:def-watch:run-1"
    with create_sqlite_engine().begin() as conn:
        dedupe_rows = conn.execute(select(messages)).mappings().all()
    dedupe_native_ids = {
        row["native_message_id"]
        for row in dedupe_rows
        if row["type"] == messages_service.HARNESS_DEDUPE_TYPE
    }
    assert dedupe_native_ids == {"watch:def-watch:run-2"}


def test_flush_does_not_coalesce_scheduled_callbacks_with_different_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, channel_id: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "delivery_override": {"platform": "slack", "channel_id": channel_id},
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1", "C1")),
            ("different delivery", prov("run-2", "C2")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert [(text, source) for text, source, _ in runs] == [("first callback", SOURCE_SCHEDULED)]
    with create_sqlite_engine().begin() as conn:
        remaining = messages_service.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["different delivery"]


def test_flush_does_not_coalesce_scheduled_callbacks_with_different_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str, agent_name: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "vibe_agent_name": agent_name,
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("codex callback", prov("run-1", "codex")),
            ("claude callback", prov("run-2", "claude")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    text, source, ctx = runs[0]
    assert (text, source) == ("codex callback", SOURCE_SCHEDULED)
    assert ctx.platform_specific["vibe_agent_name"] == "codex"
    with create_sqlite_engine().begin() as conn:
        remaining = messages_service.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["claude callback"]


def test_capture_scheduled_target_agent_splits_coalescing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def captured_prov(execution_id: str, agent_name: str) -> dict:
        ctx = MessageContext(
            user_id="U",
            channel_id="C",
            platform="avibe",
            message_id=f"watch:def-watch:{execution_id}",
            platform_specific={
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
                "agent_session_target": {"id": f"ses-{agent_name}", "agent_name": agent_name},
            },
        )
        return session_turns.capture_scheduled_provenance(ctx)

    session_id = _seed_avibe_session_with_queue(
        [
            ("codex target callback", captured_prov("run-1", "codex")),
            ("claude target callback", captured_prov("run-2", "claude")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    text, source, ctx = runs[0]
    assert (text, source) == ("codex target callback", SOURCE_SCHEDULED)
    assert ctx.platform_specific[session_turns.SCHEDULED_TARGET_AGENT_KEY] == "codex"
    with create_sqlite_engine().begin() as conn:
        remaining = messages_service.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["claude target callback"]


def test_flush_coalesced_scheduled_copy_uses_i18n(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "language": "zh",
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("第一次 callback", prov("run-1")),
            ("第二次 callback", prov("run-2")),
        ]
    )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    text = runs[0][0]
    assert "同一个 watch/task callback" in text
    assert "已合并的 callback 消息" in text


def test_flush_preserves_scheduled_prompt_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "  indented: true\n",
                {
                    "message_id": "watch:def-watch:run-1",
                    "platform_specific": {
                        "task_execution_id": "run-1",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                },
            )
        ]
    )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert runs[0][0] == "  indented: true\n"


def test_flush_skips_scheduled_callback_when_native_id_already_claimed(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "duplicate callback",
                {
                    "message_id": "watch:def-watch:run-2",
                    "platform_specific": {
                        "task_execution_id": "run-2",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                },
            )
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        queued = messages_service.list_queued(conn, session_id)
        messages_service.delete_queued(conn, [queued[0]["id"]])
        messages_service.append(
            conn,
            scope_id=queued[0]["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type=messages_service.HARNESS_DEDUPE_TYPE,
            text="",
            metadata={"coalesced_from": "older-row"},
            native_message_id="watch:def-watch:run-2",
        )
        messages_service.append(
            conn,
            scope_id=queued[0]["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type=messages_service.QUEUED_TYPE,
            text="duplicate callback",
            metadata={
                session_turns.SCHEDULED_PROVENANCE_KEY: {
                    "message_id": "watch:def-watch:run-2",
                    "platform_specific": {
                        "task_execution_id": "run-2",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                }
            },
            native_message_id=None,
        )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is False
    assert runs == []
    with create_sqlite_engine().begin() as conn:
        assert messages_service.list_queued(conn, session_id) == []


def test_flush_drops_only_claimed_scheduled_callbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("duplicate callback", prov("run-1")),
            ("fresh callback", prov("run-2")),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        from sqlalchemy import update

        from storage.models import messages

        queued = messages_service.list_queued(conn, session_id)
        duplicate = queued[0]
        fresh = queued[1]
        messages_service.delete_queued(conn, [duplicate["id"]])
        messages_service.append(
            conn,
            scope_id=duplicate["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type=messages_service.HARNESS_DEDUPE_TYPE,
            text="",
            metadata={"coalesced_from": "older-row"},
            native_message_id="watch:def-watch:run-1",
        )
        duplicate = messages_service.append(
            conn,
            scope_id=duplicate["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type=messages_service.QUEUED_TYPE,
            text="duplicate callback",
            metadata={
                session_turns.SCHEDULED_PROVENANCE_KEY: {
                    "message_id": "watch:def-watch:run-1",
                    "platform_specific": {
                        "task_execution_id": "run-1",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                }
            },
            native_message_id=None,
        )
        conn.execute(
            update(messages)
            .where(messages.c.id == duplicate["id"])
            .values(created_at="2026-06-22T00:00:00Z")
        )
        conn.execute(update(messages).where(messages.c.id == fresh["id"]).values(created_at="2026-06-22T00:00:01Z"))

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert len(runs) == 1
    text, source, ctx = runs[0]
    assert (text, source) == ("fresh callback", SOURCE_SCHEDULED)
    assert ctx.message_id == "watch:def-watch:run-2"
    assert ctx.platform_specific["task_execution_id"] == "run-2"
    with create_sqlite_engine().begin() as conn:
        assert messages_service.list_queued(conn, session_id) == []


def test_flush_does_not_mark_native_ids_when_context_build_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "suppressed callback",
                {
                    "message_id": "watch:def-watch:run-1",
                    "platform_specific": {
                        "task_execution_id": "run-1",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                        "suppress_delivery": True,
                    },
                },
            )
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    mgr = session_turns.SessionTurnManager(
        controller=types.SimpleNamespace(),
        build_context=lambda sid: (_ for _ in ()).throw(RuntimeError("missing session")),
    )

    assert asyncio.run(mgr.flush_queue(session_id)) is False
    with create_sqlite_engine().begin() as conn:
        from sqlalchemy import select

        from storage.models import messages

        rows = conn.execute(select(messages).where(messages.c.session_id == session_id)).mappings().all()
    assert [row for row in rows if row["type"] == messages_service.HARNESS_DEDUPE_TYPE] == []


def test_flush_continues_after_dropping_duplicate_scheduled_segment(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            (
                "duplicate callback",
                {
                    "message_id": "watch:def-watch:run-2",
                    "platform_specific": {
                        "task_execution_id": "run-2",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                },
            ),
            ("queued user follow-up", None),
        ]
    )

    from storage import messages_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().begin() as conn:
        from sqlalchemy import update

        from storage.models import messages

        queued = messages_service.list_queued(conn, session_id)
        messages_service.delete_queued(conn, [queued[0]["id"]])
        messages_service.append(
            conn,
            scope_id=queued[0]["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type=messages_service.HARNESS_DEDUPE_TYPE,
            text="",
            metadata={"coalesced_from": "older-row"},
            native_message_id="watch:def-watch:run-2",
        )
        messages_service.append(
            conn,
            scope_id=queued[0]["scope_id"],
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type=messages_service.QUEUED_TYPE,
            text="duplicate callback",
            metadata={
                session_turns.SCHEDULED_PROVENANCE_KEY: {
                    "message_id": "watch:def-watch:run-2",
                    "platform_specific": {
                        "task_execution_id": "run-2",
                        "task_trigger_kind": "watch",
                        "task_definition_id": "def-watch",
                    },
                }
            },
            native_message_id=None,
        )
        queued = messages_service.list_queued(conn, session_id)
        for row in queued:
            created_at = "2026-06-22T00:00:00Z" if row["text"] == "duplicate callback" else "2026-06-22T00:01:00Z"
            conn.execute(update(messages).where(messages.c.id == row["id"]).values(created_at=created_at))

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert [(text, source) for text, source, _ in runs] == [("queued user follow-up", SOURCE_HUMAN)]
    with create_sqlite_engine().begin() as conn:
        assert messages_service.list_queued(conn, session_id) == []


def test_flush_does_not_coalesce_scheduled_callbacks_outside_window(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def prov(execution_id: str) -> dict:
        return {
            "message_id": f"watch:def-watch:{execution_id}",
            "platform_specific": {
                "task_execution_id": execution_id,
                "task_trigger_kind": "watch",
                "task_definition_id": "def-watch",
            },
        }

    session_id = _seed_avibe_session_with_queue(
        [
            ("first callback", prov("run-1")),
            ("late callback", prov("run-2")),
        ]
    )

    from sqlalchemy import update

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import messages

    with create_sqlite_engine().begin() as conn:
        rows = messages_service.list_queued(conn, session_id)
        conn.execute(update(messages).where(messages.c.id == rows[0]["id"]).values(created_at="2026-06-22T00:00:00Z"))
        conn.execute(update(messages).where(messages.c.id == rows[1]["id"]).values(created_at="2026-06-22T00:02:01Z"))

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert [(text, source) for text, source, _ in runs] == [("first callback", SOURCE_SCHEDULED)]
    with create_sqlite_engine().begin() as conn:
        remaining = messages_service.list_queued(conn, session_id)
    assert [row["text"] for row in remaining] == ["late callback"]


def test_flush_segments_user_then_scheduled_in_order(tmp_path, monkeypatch):
    """A mixed queue drains one segment per flush, in order: leading user rows merge
    into one user turn; the scheduled row then runs separately with its provenance.
    The completion-reflush handles the next segment, so one flush runs only the first
    segment and leaves the rest (#84)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _seed_avibe_session_with_queue(
        [
            ("u1", None),
            ("u2", None),
            ("sched", {"message_id": "scheduled:x", "platform_specific": {"suppress_delivery": True}}),
        ]
    )
    mgr, runs = _manager_capturing_runs()

    from storage import messages_service
    from storage.db import create_sqlite_engine

    # First flush: leading user rows merge into one user turn; the scheduled row stays.
    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert [(t, s) for t, s, _ in runs] == [("u1\nu2", SOURCE_HUMAN)]
    assert "suppress_delivery" not in (runs[0][2].platform_specific or {})
    with create_sqlite_engine().begin() as conn:
        remaining = messages_service.list_queued(conn, session_id)
    assert [r["text"] for r in remaining] == ["sched"]

    # Second flush (what the turn completion triggers): the scheduled row runs as
    # SOURCE_SCHEDULED with its provenance.
    assert asyncio.run(mgr.flush_queue(session_id)) is True
    assert (runs[-1][0], runs[-1][1]) == ("sched", SOURCE_SCHEDULED)
    assert runs[-1][2].platform_specific["suppress_delivery"] is True


def test_flush_mixed_owner_user_rows_preserves_owner_list(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    from core.services import sessions as sessions_service
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_mixed_owner", now="2026-05-31T00:00:00Z"
        )
        _seed_project_workdir(conn, scope_id, tmp_path)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="worker"
        )
        for text, owner in (("u1", "remote:user-a"), ("u2", "remote:user-b")):
            messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=session["id"],
                platform="avibe",
                author="user",
                source="user",
                message_type=messages_service.QUEUED_TYPE,
                text=text,
                author_id=owner,
                metadata={"_web_push_user_key": owner},
            )

    mgr, runs = _manager_capturing_runs()

    assert asyncio.run(mgr.flush_queue(session["id"])) is True
    assert [(t, s) for t, s, _ in runs] == [("u1\nu2", SOURCE_HUMAN)]
    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(conn, session_id=session["id"], types=("user",))
    assert transcript["messages"][0]["author_id"] is None
    assert transcript["messages"][0]["metadata"]["_web_push_user_keys"] == [
        "remote:user-a",
        "remote:user-b",
    ]
    assert "_web_push_user_key" not in transcript["messages"][0]["metadata"]


def test_capture_scheduled_provenance_keeps_delivery_drops_routing():
    """capture_scheduled_provenance keeps the delivery / attribution keys — notably
    delivery_override, the redirect MessageDispatcher._get_target_context uses — and
    DROPS the routing keys the flush rebuilds, so a queued scheduled run keeps its
    delivery target (#84 / Codex P1 #3338692433)."""
    override = {"channel_id": "slack-9", "platform": "slack"}
    ctx = MessageContext(
        user_id="U",
        channel_id="C",
        platform="avibe",
        message_id="scheduled:exec-9",
        platform_specific={
            "platform": "avibe",
            "is_dm": False,
            "agent_session_id": "ses1",
            "agent_session_target": {"id": "ses1", "agent_name": "worker"},
            "delivery_override": override,
            "suppress_delivery": True,
            "turn_source": "scheduled",
            "task_trigger_kind": "task",
        },
    )
    prov = session_turns.capture_scheduled_provenance(ctx)
    # The stable native id is captured for dedup (Codex P2).
    assert prov["message_id"] == "scheduled:exec-9"
    spec = prov["platform_specific"]
    # Delivery / attribution provenance kept.
    assert spec["delivery_override"] == override
    assert spec["suppress_delivery"] is True
    assert spec["turn_source"] == "scheduled"
    assert spec["task_trigger_kind"] == "task"
    assert spec[session_turns.SCHEDULED_TARGET_AGENT_KEY] == "worker"
    # Routing keys the flush rebuilds are NOT carried.
    for routing in ("platform", "is_dm", "agent_session_id", "agent_session_target"):
        assert routing not in spec
