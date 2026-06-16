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
from unittest.mock import AsyncMock, MagicMock

import httpx

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

    def _register(session_key, *, on_chunk, done_event, turn_token=None):
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
    assert ("/internal/cancel/{session_id}", ("POST",)) in routes
    assert ("/internal/dispatch", ("POST",)) in routes


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

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
    app = internal_server.create_app(_build_controller_double())
    transport = httpx.ASGITransport(app=app)

    async def _go():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            idle = (await client.get("/internal/turn-state/ses_ts")).json()
            # Simulate an in-flight turn.
            task = asyncio.create_task(asyncio.sleep(60))
            app.state.in_flight_dispatches["ses_ts"] = session_turns.Turn(
                task=task,
                context=MessageContext(user_id="U", channel_id="C", platform="avibe"),
            )
            busy = (await client.get("/internal/turn-state/ses_ts")).json()
            task.cancel()
            return idle, busy

    idle, busy = asyncio.run(_go())
    assert idle["in_flight"] is False
    assert busy["in_flight"] is True


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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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


def test_scheduled_gate_cancel_stops_scheduled_run(monkeypatch, tmp_path):
    """Stop works for a scheduled run: because the run goes through ``_run_turn``
    it registers the scheduled ``context`` in ``in_flight``, so
    ``/internal/cancel/{session_id}`` finds the task + reuses the IM ``/stop`` path
    to interrupt the backend (mirrors the Chat cancel test)."""
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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


def test_flush_segments_user_then_scheduled_in_order(tmp_path, monkeypatch):
    """A mixed queue drains one segment per flush, in order: leading user rows merge
    into one user turn; the scheduled row then runs separately with its provenance.
    The completion-reflush handles the next segment, so one flush runs only the first
    segment and leaves the rest (#84)."""
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))

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
            "agent_session_target": {"id": "ses1"},
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
    # Routing keys the flush rebuilds are NOT carried.
    for routing in ("platform", "is_dm", "agent_session_id", "agent_session_target"):
        assert routing not in spec
