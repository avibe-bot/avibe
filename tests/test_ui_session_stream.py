"""Tests for ``POST /api/sessions/<id>/messages`` (fire-and-forget dispatch)
and ``POST /api/sessions/<id>/cancel`` in ``vibe.ui_server``.

These cover the bridge between the browser and the controller's Unix socket:
the session/page-scoped model persists the user row and fire-and-forgets the
turn (the reply arrives over the ``message.new`` stream). We mock
``vibe.internal_client`` so the tests stay hermetic and don't need a real
controller process.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from storage.importer import ensure_sqlite_state
from storage.models import scope_settings
from storage.settings_service import upsert_scope
from tests.ui_server_test_helpers import csrf_headers


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _make_session(tmp_path: Path) -> tuple[str, str]:
    """Create a real avibe project + session row so the route handler
    can find it. Returns ``(scope_id, session_id)``.
    """

    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_stream",
            now="2026-05-26T13:00:00Z",
        )
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=str(tmp_path),
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json="{}",
                created_at="2026-05-26T13:00:00Z",
                updated_at="2026-05-26T13:00:00Z",
            )
        )
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
        )
    return scope_id, session["id"]


def test_route_fire_and_forgets_dispatch(isolated_state, tmp_path):
    """The web Chat POST persists the user row AND fire-and-forgets the turn via
    ``/internal/dispatch_async``. The reply arrives over the persistent
    ``message.new`` stream, so the response returns 201 immediately with the row
    (it does NOT hold the turn open).
    """

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    dispatch_mock = AsyncMock(
        return_value={"status_code": 202, "body": {"ok": True, "session_id": session_id}}
    )
    with (
        patch("vibe.internal_client.dispatch_async", dispatch_mock),
        patch("vibe.ui_server._web_push_user_key", return_value="remote:user-a"),
    ):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "no stream", "author_id": "remote:spoofed"},
            headers=headers,
        )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["author"] == "user"
    assert payload["author_id"] == "remote:user-a"
    assert payload["metadata"]["_web_push_user_key"] == "remote:user-a"
    assert payload["text"] == "no stream"
    # The turn was kicked off fire-and-forget with the session + text.
    dispatch_mock.assert_awaited_once()
    sent = dispatch_mock.await_args.args[0]
    assert sent["session_id"] == session_id
    assert sent["text"] == "no stream"


def test_route_enqueues_when_turn_in_progress(isolated_state, tmp_path):
    """When the controller reports a turn already running (202 {queued}), the
    route persists the user row, hands its id to the controller to re-type as
    queued, and returns 202 {queued:true} marked as the queued type. (The actual
    re-type is the controller's atomic job, covered in test_internal_server.)"""

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    dispatch_mock = AsyncMock(return_value={"status_code": 202, "body": {"ok": True, "queued": True}})
    with patch("vibe.internal_client.dispatch_async", dispatch_mock):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "while busy"},
            headers=headers,
        )
    assert response.status_code == 202
    body = response.get_json()
    assert body["queued"] is True
    assert body["type"] == "queued"
    assert body["text"] == "while busy"
    # The user row was persisted first, and its id handed to the controller to
    # re-type as queued (atomic, no second row).
    dispatch_mock.assert_awaited_once()
    sent = dispatch_mock.await_args.args[0]
    assert sent["user_message_id"] == body["id"]


def test_create_session_without_backend_defers_to_default_agent(isolated_state, tmp_path):
    """POST /api/sessions with no ``agent_backend`` must NOT stamp a concrete
    backend onto the session. A stamped backend is treated by message_handler
    as an explicit override and bypasses default Vibe Agent resolution, so a
    plain "new chat" leaves the backend empty and lets the shared resolver
    pick the configured default agent at dispatch time.
    """

    from storage import projects_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, folder_path=str(tmp_path))

    client = app.test_client()
    headers = csrf_headers(client)
    response = client.post(
        "/api/sessions",
        json={"project_id": project["id"]},
        headers=headers,
    )
    assert response.status_code == 201
    # Empty/absent backend — resolution is deferred to dispatch, not pinned here.
    assert not response.get_json().get("agent_backend")


def test_fork_session_creates_new_workbench_session(isolated_state, tmp_path):
    """POST /api/sessions/<id>/fork reserves a new Avibe Session row that is
    ready for the native backend fork on the first turn, and returns the row the
    sidebar needs to prepend/navigate immediately.
    """

    from sqlalchemy import update

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from vibe.ui_server import app

    scope_id, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == session_id)
            .values(native_session_id="native-source-1", title="Source session")
        )
        source_message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            message_type="user",
            text="fork from here",
        )

    with patch("vibe.sse_broker.broker.publish") as publish:
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/fork", json={}, headers=headers)

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["id"] != session_id
    assert payload["scope_id"] == scope_id
    assert payload["project_id"] == "proj_stream"
    assert payload["title"] == "Fork Source session"
    assert payload["agent_backend"] == "claude"
    assert payload["agent_name"] == "worker"
    assert payload["native_session_id"] == ""
    assert payload["metadata"]["created_via"] == "session_fork"
    assert payload["metadata"]["fork_source_session_id"] == session_id
    assert payload["metadata"]["fork_source_session_title"] == "Source session"
    assert payload["metadata"]["fork_source_message_id"] == source_message["id"]
    assert payload["metadata"]["fork_source_native_session_id"] == "native-source-1"
    publish.assert_called_with(
        "session.activity",
        {"session_id": payload["id"], "scope_id": scope_id, "event": "created"},
    )


def test_fork_session_rejects_unbound_source_session(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    client = app.test_client()
    headers = csrf_headers(client)
    response = client.post(f"/api/sessions/{session_id}/fork", json={}, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["code"] == "session_not_bound"


def test_patch_rejects_backend_switch_for_pending_fork(isolated_state, tmp_path):
    from sqlalchemy import update

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == session_id)
            .values(native_session_id="native-source-1")
        )

    client = app.test_client()
    headers = csrf_headers(client)
    fork_response = client.post(f"/api/sessions/{session_id}/fork", json={}, headers=headers)
    assert fork_response.status_code == 201
    forked_id = fork_response.get_json()["id"]

    response = client.patch(
        f"/api/sessions/{forked_id}",
        json={"agent_backend": "codex", "agent_name": "codex"},
        headers=headers,
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == "backend_locked"
    assert body["current_backend"] == "claude"
    assert body["requested_backend"] == "codex"


def test_chat_bootstrap_returns_first_screen_payload(isolated_state, tmp_path):
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    scope_id, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            message_type="user",
            text="question",
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type="assistant",
            text="thinking",
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type="tool_call",
            text="ran tool",
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type="result",
            text="answer",
        )
        messages_service.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="follow-up",
        )
        messages_service.set_draft(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="draft text",
        )

    async def in_flight(session_id_inner):
        assert session_id_inner == session_id
        return {"status_code": 200, "body": {"in_flight": True}}

    with (
        patch("vibe.internal_client.turn_state", in_flight),
        patch(
            "vibe.api.get_vibe_agents",
            return_value={
                "agents": [{"name": "worker", "backend": "claude", "enabled": True}],
                "default_agent_name": "worker",
            },
        ),
    ):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/bootstrap")

    assert response.status_code == 200
    body = response.get_json()
    assert body["session"]["id"] == session_id
    assert body["default_agent_name"] == "worker"
    assert body["agents"][0]["name"] == "worker"
    assert body["config"]["setup_state"]["needs_setup"] is True
    assert [message["text"] for message in body["messages"]] == ["question", "answer"]
    assert [message["type"] for message in body["messages"]] == ["user", "result"]
    assert body["queued"][0]["text"] == "follow-up"
    assert body["draft"]["text"] == "draft text"
    assert body["turn_state"]["in_flight"] is True


def test_chat_bootstrap_keeps_timeout_turn_state_unknown(isolated_state, tmp_path):
    from vibe import internal_client
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def timeout(session_id_inner):
        raise internal_client.InternalServerTimeout("slow internal turn-state")

    with (
        patch("vibe.internal_client.turn_state", timeout),
        patch("vibe.api.get_vibe_agents", return_value={"agents": [], "default_agent_name": None}),
    ):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/bootstrap")

    assert response.status_code == 200
    assert response.get_json()["turn_state"]["in_flight"] is None


def test_cancel_route_proxies_to_internal_socket(isolated_state, tmp_path):
    _, session_id = _make_session(tmp_path)

    from vibe.ui_server import app

    cancel_mock = AsyncMock(
        return_value={"status_code": 200, "body": {"ok": True, "status": "cancel_requested"}}
    )
    with patch("vibe.internal_client.cancel_dispatch", cancel_mock):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/cancel", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "cancel_requested"
    cancel_mock.assert_awaited_once_with(session_id)


def test_cancel_route_returns_503_when_socket_unavailable(isolated_state, tmp_path):
    from vibe import internal_client
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def fail(session_id_inner):
        raise internal_client.InternalServerUnavailable("socket missing")

    with patch("vibe.internal_client.cancel_dispatch", fail):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/cancel", headers=headers)
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "internal_unavailable"


def test_cancel_route_recovers_stale_running_status_on_not_in_flight(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert sessions_service.set_agent_status(conn, session_id, "running") is True

    cancel_mock = AsyncMock(
        return_value={"status_code": 404, "body": {"ok": False, "code": "not_in_flight"}}
    )
    with patch("vibe.internal_client.cancel_dispatch", cancel_mock):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/cancel", headers=headers)

    assert response.status_code == 404
    body = response.get_json()
    assert body["code"] == "not_in_flight"
    assert body["recovered_agent_status"] is True
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, session_id)["agent_status"] == "idle"


def test_cancel_route_does_not_recover_failed_status_on_not_in_flight(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert sessions_service.set_agent_status(conn, session_id, "failed") is True

    cancel_mock = AsyncMock(
        return_value={"status_code": 404, "body": {"ok": False, "code": "not_in_flight"}}
    )
    with patch("vibe.internal_client.cancel_dispatch", cancel_mock):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/cancel", headers=headers)

    assert response.status_code == 404
    body = response.get_json()
    assert body["code"] == "not_in_flight"
    assert body["recovered_agent_status"] is False
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, session_id)["agent_status"] == "failed"


def test_cancel_route_preserves_not_in_flight_for_missing_session(isolated_state):
    from vibe.ui_server import app

    cancel_mock = AsyncMock(
        return_value={"status_code": 404, "body": {"ok": False, "code": "not_in_flight"}}
    )
    with patch("vibe.internal_client.cancel_dispatch", cancel_mock):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post("/api/sessions/ses_missing/cancel", headers=headers)

    assert response.status_code == 404
    body = response.get_json()
    assert body["code"] == "not_in_flight"
    assert body["recovered_agent_status"] is False


def test_turn_state_route_returns_504_on_probe_timeout(isolated_state, tmp_path):
    from vibe import internal_client
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def timeout(session_id_inner):
        raise internal_client.InternalServerTimeout("slow internal turn-state")

    with patch("vibe.internal_client.turn_state", timeout):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/turn-state")

    assert response.status_code == 504
    assert response.get_json()["error"]["code"] == "turn_state_timeout"


def test_turn_state_idle_recovers_stale_running_status(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert sessions_service.set_agent_status(conn, session_id, "running") is True

    async def idle(session_id_inner):
        assert session_id_inner == session_id
        return {"status_code": 200, "body": {"in_flight": False}}

    with patch("vibe.internal_client.turn_state", idle):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/turn-state")

    assert response.status_code == 200
    body = response.get_json()
    assert body["in_flight"] is False
    assert body["recovered_agent_status"] is True
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, session_id)["agent_status"] == "idle"


def test_turn_state_idle_does_not_recover_failed_status(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert sessions_service.set_agent_status(conn, session_id, "failed") is True

    async def idle(session_id_inner):
        assert session_id_inner == session_id
        return {"status_code": 200, "body": {"in_flight": False}}

    with patch("vibe.internal_client.turn_state", idle):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/turn-state")

    assert response.status_code == 200
    body = response.get_json()
    assert body["in_flight"] is False
    assert body["recovered_agent_status"] is False
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, session_id)["agent_status"] == "failed"


def test_turn_state_idle_preserves_response_for_missing_session(isolated_state):
    from vibe.ui_server import app

    async def idle(session_id_inner):
        assert session_id_inner == "ses_missing"
        return {"status_code": 200, "body": {"in_flight": False}}

    with patch("vibe.internal_client.turn_state", idle):
        client = app.test_client()
        response = client.get("/api/sessions/ses_missing/turn-state")

    assert response.status_code == 200
    body = response.get_json()
    assert body["in_flight"] is False
    assert body["recovered_agent_status"] is False


def test_patch_backend_switch_blocked_while_turn_in_flight(isolated_state, tmp_path):
    """The row's ``agent_status`` lags turn acceptance (``submit`` registers the
    in-flight gate before dispatch writes ``running``), so a cross-backend PATCH
    in that startup window must consult the controller's gate and 409 — otherwise
    the bind-time backend backfill would silently undo the switch."""

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    in_flight = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": True}})
    with patch("vibe.internal_client.turn_state", in_flight):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_backend": "codex", "agent_name": "codex"},
            headers=headers,
        )
    assert response.status_code == 409
    assert response.get_json()["code"] == "backend_locked"
    in_flight.assert_awaited_once()


def test_patch_agent_name_only_backend_switch_blocked_while_turn_in_flight(isolated_state, tmp_path):
    """A selected Vibe Agent implies its backend. The UI often sends only
    ``agent_name`` when changing the picker, so the route must derive the
    backend before deciding whether to consult the controller's in-flight gate.
    """

    from core.vibe_agents import VibeAgentStore
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    store = VibeAgentStore()
    try:
        store.create(name="reviewer", backend="codex")
    finally:
        store.close()

    in_flight = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": True}})
    with patch("vibe.internal_client.turn_state", in_flight):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_name": "reviewer"},
            headers=headers,
        )
    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == "backend_locked"
    assert body["current_backend"] == "claude"
    assert body["requested_backend"] == "codex"
    in_flight.assert_awaited_once()


def test_patch_agent_name_only_backend_switch_refreshes_variant_when_idle(isolated_state, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == session_id)
            .values(agent_variant="old-claude-profile")
        )

    store = VibeAgentStore()
    try:
        store.create(name="reviewer", backend="codex")
    finally:
        store.close()

    idle = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": False}})
    with patch("vibe.internal_client.turn_state", idle):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_name": "reviewer"},
            headers=headers,
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["agent_name"] == "reviewer"
    assert body["agent_backend"] == "codex"
    assert body["agent_variant"] == "codex"
    idle.assert_awaited_once()


def test_patch_same_backend_change_skips_in_flight_gate(isolated_state, tmp_path):
    """Same-backend agent/model changes stay allowed mid-turn and don't pay the
    internal turn-state round-trip."""

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    gate = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": True}})
    with patch("vibe.internal_client.turn_state", gate):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_backend": "claude", "agent_name": "claude-pro", "model": "opus"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.get_json()["agent_name"] == "claude-pro"
    gate.assert_not_awaited()


def test_patch_backend_switch_allowed_when_idle(isolated_state, tmp_path):
    """No native + no in-flight turn → the (project-default) backend is a soft
    pin and the cross-backend switch lands."""

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    idle = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": False}})
    with patch("vibe.internal_client.turn_state", idle):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_backend": "codex", "agent_name": "codex"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.get_json()["agent_backend"] == "codex"


def test_patch_backend_switch_falls_back_to_row_guard_when_controller_down(isolated_state, tmp_path):
    """An unreachable controller must not brick the picker: the gate check is
    best-effort and the row-status guard inside ``update_session`` still
    applies."""

    from vibe import internal_client
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def unavailable(session_id_inner):
        raise internal_client.InternalServerUnavailable("socket missing")

    with patch("vibe.internal_client.turn_state", unavailable):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.patch(
            f"/api/sessions/{session_id}",
            json={"agent_backend": "codex", "agent_name": "codex"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.get_json()["agent_backend"] == "codex"
