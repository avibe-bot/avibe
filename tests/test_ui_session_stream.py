"""Tests for ``POST /api/sessions/<id>/messages`` (fire-and-forget dispatch)
and ``POST /api/sessions/<id>/cancel`` in ``vibe.ui_server``.

These cover the bridge between the browser and the controller's Unix socket:
the session/page-scoped model persists the user row and fire-and-forgets the
turn (the reply arrives over the ``message.new`` stream). We mock
``vibe.internal_client`` so the tests stay hermetic and don't need a real
controller process.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from sqlalchemy import select, update

from storage.importer import ensure_sqlite_state
from storage.db import create_sqlite_engine
from storage import message_deliveries, messages_service
from storage.models import agent_sessions, messages
from storage.models import scope_settings
from storage.settings_service import upsert_scope
from tests.ui_server_test_helpers import csrf_headers


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _make_session(
    tmp_path: Path,
    *,
    agent_name: str = "worker",
    agent_backend: str = "claude",
) -> tuple[str, str]:
    """Create a real avibe project + session row so the route handler
    can find it. Returns ``(scope_id, session_id)``.
    """

    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine

    agent = _ensure_vibe_agent(agent_name, agent_backend)
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
            agent_backend=agent.backend,
            agent_id=agent.id,
            agent_name=agent.name,
        )
    return scope_id, session["id"]


def _ensure_vibe_agent(name: str, backend: str):
    from core.vibe_agents import VibeAgentStore

    store = VibeAgentStore()
    try:
        agent = store.get(name)
        if agent is None:
            agent = store.create(name=name, backend=backend)
        return agent
    finally:
        store.close()


def _seed_opencode_messages(xdg_home: Path, native_session_id: str, roles: list[str]) -> None:
    db_path = xdg_home / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, time_created INTEGER, data TEXT)"
        )
        for index, role in enumerate(roles, start=1):
            message_id = f"oc-msg-{index}"
            conn.execute(
                "INSERT INTO message (id, data) VALUES (?, ?)",
                (message_id, json.dumps({"role": role})),
            )
            conn.execute(
                "INSERT INTO part (id, session_id, message_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (f"part-{index}", native_session_id, message_id, index, json.dumps({"type": "text"})),
            )


def _settle_reserved_delivery(payload: dict, *, state: str) -> dict:
    with create_sqlite_engine().begin() as conn:
        delivery = message_deliveries.get_delivery(conn, payload["user_message_id"])
        assert delivery is not None and delivery["state"] == "reserved"
        if state == "queued":
            settled = message_deliveries.cas_delivery(
                conn,
                delivery["id"],
                expected_version=delivery["version"],
                expected_states=("reserved",),
                values={"state": "queued"},
            )
        else:
            turn_id = message_deliveries.new_turn_id()
            message_deliveries.insert_turn(
                conn,
                turn_id=turn_id,
                session_id=delivery["session_id"],
                initial_delivery_id=delivery["id"],
                state="starting",
                backend="claude",
            )
            attempt_id = message_deliveries.new_attempt_id()
            message_deliveries.open_start_attempt(
                conn,
                delivery["id"],
                expected_version=delivery["version"],
                turn_id=turn_id,
                attempt_id=attempt_id,
            )
            turn = message_deliveries.get_turn(conn, turn_id)
            assert turn is not None
            assert message_deliveries.bind_native_start(
                conn,
                turn_id,
                expected_version=int(turn["version"]),
                runtime_key=f"runtime:{turn_id}",
                runtime_turn_id=f"runtime-turn:{turn_id}",
                native_turn_id=f"native:{turn_id}",
            ) is not None
            accepted = message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=turn_id,
                evidence={"kind": "test_native_acceptance"},
            )
            settled = accepted[0] if accepted else None
        assert settled is not None
        return settled


def _accepted_dispatch(session_id: str) -> AsyncMock:
    async def dispatch(payload: dict) -> dict:
        settled = _settle_reserved_delivery(payload, state="accepted")
        return {
            "status_code": 202,
            "body": {
                "ok": True,
                "session_id": session_id,
                "delivery_state": settled["state"],
            },
        }

    return AsyncMock(side_effect=dispatch)


def test_route_fire_and_forgets_dispatch(isolated_state, tmp_path):
    """The web Chat POST persists the user row AND fire-and-forgets the turn via
    ``/internal/dispatch_async``. The reply arrives over the persistent
    ``message.new`` stream, so the response returns 201 immediately with the row
    (it does NOT hold the turn open).
    """

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def dispatch(payload):
        settled = _settle_reserved_delivery(payload, state="accepted")
        return {
            "status_code": 202,
            "body": {
                "ok": True,
                "session_id": session_id,
                "delivery_state": settled["state"],
            },
        }

    dispatch_mock = AsyncMock(side_effect=dispatch)
    with (
        patch("vibe.internal_client.dispatch_async", dispatch_mock),
        patch("vibe.ui_server._web_push_user_key", return_value="remote:user-a"),
        patch("vibe.ui_server.is_direct_loopback_memory_request", return_value=False),
    ):
        client = app.test_client()
        headers = csrf_headers(client)
        saved_draft = client.put(
            f"/api/sessions/{session_id}/draft",
            json={"text": "draft before send", "expected_updated_at": None},
            headers=headers,
        ).get_json()["draft"]
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
    assert payload["metadata"]["_memory_cli_admitted"] is False
    assert payload["text"] == "no stream"
    assert payload["draft_advanced"] is True
    assert payload["draft"]["text"] == ""
    assert payload["draft"]["updated_at"] not in (None, saved_draft["updated_at"])
    # The turn was kicked off fire-and-forget with the session + text.
    dispatch_mock.assert_awaited_once()
    sent = dispatch_mock.await_args.args[0]
    assert sent["session_id"] == session_id
    assert sent["text"] == "no stream"
    assert sent["memory_cli_admitted"] is False
    assert sent["is_ordinary_text"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "Yes", "metadata": {"quick_reply_for": "agent-message-1"}},
        {"text": "forwarded text", "metadata": {"forwarded": True}},
    ],
)
def test_workbench_side_actions_are_not_marked_as_ordinary_memory_input(
    isolated_state,
    tmp_path,
    payload,
):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    dispatch = _accepted_dispatch(session_id)
    with patch("vibe.internal_client.dispatch_async", dispatch):
        client = app.test_client()
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json=payload,
            headers=csrf_headers(client),
        )

    assert response.status_code == 201
    assert dispatch.await_args.args[0]["is_ordinary_text"] is False
    assert response.get_json()["draft_advanced"] is (
        "quick_reply_for" not in payload.get("metadata", {})
    )



def test_workbench_memory_text_is_persisted_and_dispatched_as_ordinary_input(
    isolated_state,
    tmp_path,
):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    dispatch = _accepted_dispatch(session_id)
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")

    with patch("vibe.internal_client.dispatch_async", dispatch):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "/memory status"},
            headers=headers,
            base_url="http://127.0.0.1:15131",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

    assert response.status_code == 201
    response_payload = response.get_json()
    assert response_payload["text"] == "/memory status"
    payload = dispatch.await_args.args[0]
    assert payload["text"] == "/memory status"
    assert payload["user_id"] == "local"
    assert payload["message_id"] == response_payload["id"]
    assert payload["memory_cli_admitted"] is True


@pytest.mark.parametrize(
    "session_payload,expected",
    [
        ({"sub": "user-a"}, "remote:user-a"),
        ({}, None),
        (None, None),
    ],
)
def test_remote_workbench_memory_identity_requires_a_stable_subject(session_payload, expected):
    from vibe import remote_access
    from vibe.ui_server import app, _workbench_memory_user_id

    with (
        app.test_request_context("/chat/session", headers={"Cookie": "avibe_remote_session=session"}),
        patch("vibe.ui_server.is_direct_loopback_memory_request", return_value=False),
        patch("vibe.ui_server._load_remote_access_config", return_value=object()),
        patch.object(remote_access, "parse_session_cookie", return_value=session_payload),
    ):
        assert _workbench_memory_user_id() == expected


def test_workbench_dispatch_propagates_attachment_and_resolved_identity(
    isolated_state,
    tmp_path,
):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    dispatch = _accepted_dispatch(session_id)
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    upload = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("diagram.png", b"attachment-bytes", "image/png")},
        headers=headers,
        base_url="http://127.0.0.1:15131",
    )
    assert upload.status_code == 201

    with patch("vibe.internal_client.dispatch_async", dispatch):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": {
                    "text": "remember this diagram",
                    "attachments": [{"token": upload.get_json()["token"]}],
                }
            },
            headers=headers,
            base_url="http://127.0.0.1:15131",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

    assert response.status_code == 201
    payload = dispatch.await_args.args[0]
    assert payload["user_id"] == "local"
    assert payload["message_id"] == response.get_json()["id"]
    assert len(payload["files"]) == 1
    assert payload["files"][0]["name"] == "diagram.png"
    assert Path(payload["files"][0]["path"]).read_bytes() == b"attachment-bytes"


def test_route_reads_the_materialized_message_id_for_a_merged_batch(
    isolated_state,
    tmp_path,
) -> None:
    from vibe.ui_server import app

    scope_id, session_id = _make_session(tmp_path)
    older_delivery_id = "msg_merged_segment_head"

    async def dispatch(payload):
        with create_sqlite_engine().begin() as conn:
            current = message_deliveries.get_delivery(conn, payload["user_message_id"])
            assert current is not None and current["state"] == "reserved"
            older = message_deliveries.insert_delivery(
                conn,
                delivery_id=older_delivery_id,
                session_id=session_id,
                priority="p3",
                state="queued",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=scope_id,
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    text="older queued input",
                    metadata={"_web_push_user_key": "remote:user-a"},
                    author_id="remote:user-a",
                ),
                dispatch_text="older queued input",
                now="2026-01-01T00:00:00Z",
            )
            turn_id = message_deliveries.new_turn_id()
            claimed = message_deliveries.claim_start_batch(
                conn,
                turn_id=turn_id,
                session_id=session_id,
                backend="claude",
                deliveries=[older, current],
                dispatch_text="older queued input\nnew input",
            )
            turn = claimed["turn"]
            assert message_deliveries.bind_native_start(
                conn,
                turn_id,
                expected_version=int(turn["version"]),
                runtime_key=f"runtime:{turn_id}",
                runtime_turn_id=f"runtime-turn:{turn_id}",
                native_turn_id=f"native:{turn_id}",
            ) is not None
            accepted = message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=turn_id,
                evidence={"kind": "test_native_acceptance"},
            )
            current_after = message_deliveries.get_delivery(
                conn,
                payload["user_message_id"],
            )
        assert accepted and current_after is not None
        return {
            "status_code": 202,
            "body": {
                "ok": True,
                "session_id": session_id,
                "delivery_id": payload["user_message_id"],
                "message_id": accepted[0]["message_id"],
                "delivery_state": current_after["state"],
            },
        }

    with (
        patch("vibe.internal_client.dispatch_async", AsyncMock(side_effect=dispatch)),
        patch("vibe.ui_server._web_push_user_key", return_value="remote:user-a"),
    ):
        client = app.test_client()
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "new input"},
            headers=csrf_headers(client),
        )

    assert response.status_code == 201
    body = response.get_json()
    assert body["id"] == older_delivery_id
    assert body["message_id"] == older_delivery_id
    assert body["delivery_id"] != older_delivery_id


def test_route_enqueues_when_turn_in_progress(isolated_state, tmp_path):
    """When the controller reports a turn already running (202 {queued}), the
    route persists the user row, hands its id to the controller to re-type as
    queued, and returns 202 {queued:true} marked as the queued type. (The actual
    re-type is the controller's atomic job, covered in test_internal_server.)"""

    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def dispatch(payload):
        settled = _settle_reserved_delivery(payload, state="queued")
        return {
            "status_code": 202,
            "body": {
                "ok": True,
                "queued": True,
                "delivery_state": settled["state"],
            },
        }

    dispatch_mock = AsyncMock(side_effect=dispatch)
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


@pytest.mark.parametrize(
    ("error_kind", "expected_status", "expected_error", "expected_delivery_state"),
    (
        ("timeout", 504, "dispatch_pending", "reserved"),
        ("ambiguous", 502, "dispatch_pending", "reserved"),
        ("unavailable", 502, "internal_unavailable", "retired"),
    ),
)
def test_route_dispatch_failure_classifies_unclaimed_delivery_outside_transcript(
    isolated_state,
    tmp_path,
    monkeypatch,
    error_kind,
    expected_status,
    expected_error,
    expected_delivery_state,
):
    from vibe import internal_client
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    published = []

    async def fail_after_connect(_payload):
        if error_kind == "timeout":
            raise internal_client.InternalServerTimeout("acceptance unknown")
        if error_kind == "unavailable":
            raise internal_client.InternalServerUnavailable("socket missing")
        raise RuntimeError("ambiguous response")

    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    dispatch_mock = AsyncMock(side_effect=fail_after_connect)
    with patch("vibe.internal_client.dispatch_async", dispatch_mock):
        client = app.test_client()
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "settle this after the timeout"},
            headers=csrf_headers(client),
        )

    assert response.status_code == expected_status
    body = response.get_json()
    assert body["dispatch_error"] == expected_error
    assert body["type"] == "user"
    dispatch_mock.assert_awaited_once()

    with create_sqlite_engine().connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
        visible = messages_service.list_session_messages(
            conn,
            session_id=session_id,
            around_id=body["id"],
            limit=1,
            types=messages_service.TRANSCRIPT_TYPES,
        )
        delivery = message_deliveries.get_delivery(conn, body["id"])
    assert queued == []
    assert visible["messages"] == []
    assert delivery is not None and delivery["state"] == expected_delivery_state
    assert "message.new" not in [event_type for event_type, _data in published]


def test_route_definitive_dispatch_rejection_retires_reserved_delivery(
    isolated_state,
    tmp_path,
):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    dispatch_mock = AsyncMock(
        return_value={
            "status_code": 500,
            "body": {"error": "backend resolution failed"},
        }
    )
    with patch("vibe.internal_client.dispatch_async", dispatch_mock):
        client = app.test_client()
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "definitively rejected"},
            headers=csrf_headers(client),
        )

    assert response.status_code == 502
    body = response.get_json()
    assert body["dispatch_error"] == "dispatch_failed"
    with create_sqlite_engine().connect() as conn:
        delivery = message_deliveries.get_delivery(conn, body["id"])
    assert delivery is not None and delivery["state"] == "retired"


def test_route_timeout_observes_controller_queue_without_republishing(
    isolated_state,
    tmp_path,
    monkeypatch,
):
    from vibe import internal_client
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    published = []

    async def timeout_after_controller_queues(payload):
        _settle_reserved_delivery(payload, state="queued")
        published.append(("queue.updated", {"session_id": session_id}))
        raise internal_client.InternalServerTimeout("response lost after enqueue")

    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    dispatch_mock = AsyncMock(side_effect=timeout_after_controller_queues)
    with patch("vibe.internal_client.dispatch_async", dispatch_mock):
        client = app.test_client()
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "already queued by the controller"},
            headers=csrf_headers(client),
        )

    assert response.status_code == 504
    body = response.get_json()
    assert body["dispatch_error"] == "dispatch_pending"
    assert body["type"] == "queued"
    dispatch_mock.assert_awaited_once()

    with create_sqlite_engine().connect() as conn:
        queued = message_deliveries.list_queued(conn, session_id)
    assert [row["id"] for row in queued] == [body["id"]]
    assert "message.new" not in [event_type for event_type, _data in published]
    assert [event_type for event_type, _data in published].count("queue.updated") == 1


def test_startup_has_no_type_owned_pending_recovery(isolated_state, tmp_path):
    from vibe import ui_server

    scope_id, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        pending = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_reserved_startup",
            session_id=session_id,
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                text="stuck reserved",
            ),
            dispatch_text="stuck reserved",
        )

    assert not hasattr(ui_server, "_recover_stale_pending_messages")
    with engine.connect() as conn:
        stored = message_deliveries.get_delivery(conn, pending["id"])
        materialized = conn.execute(
            select(messages.c.id).where(messages.c.id == pending["id"])
        ).scalar_one_or_none()
    assert stored is not None and stored["state"] == "reserved"
    assert materialized is None


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


def test_create_and_patch_session_reject_archived_agent(isolated_state, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from storage import projects_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, folder_path=str(tmp_path))

    store = VibeAgentStore()
    try:
        store.create(name="archive-fallback", backend="codex")
        original = store.create(name="retired-reviewer", backend="codex")
        archived = store.archive("retired-reviewer")
        assert archived is not None
        replacement = store.create(name="retired-reviewer", backend="codex")
    finally:
        store.close()

    client = app.test_client()
    headers = csrf_headers(client)
    create_response = client.post(
        "/api/sessions",
        json={
            "project_id": project["id"],
            "agent_backend": "codex",
            "agent_name": archived.archived_name,
        },
        headers=headers,
    )
    assert create_response.status_code == 404
    assert "not found or disabled" in create_response.get_json()["error"]

    for invalid_identity in (
        {"agent_id": original.id},
        {"agent_id": original.id, "agent_name": replacement.name},
    ):
        create_response = client.post(
            "/api/sessions",
            json={"project_id": project["id"], **invalid_identity},
            headers=headers,
        )
        assert create_response.status_code == 404
        assert "not found or disabled" in create_response.get_json()["error"]

    session_response = client.post(
        "/api/sessions",
        json={"project_id": project["id"]},
        headers=headers,
    )
    session_id = session_response.get_json()["id"]
    patch_response = client.patch(
        f"/api/sessions/{session_id}",
        json={
            "agent_backend": "codex",
            "agent_name": archived.archived_name,
        },
        headers=headers,
    )
    assert patch_response.status_code == 404
    assert "not found or disabled" in patch_response.get_json()["error"]

    for invalid_identity in (
        {"agent_id": original.id},
        {"agent_id": original.id, "agent_name": replacement.name},
    ):
        patch_response = client.patch(
            f"/api/sessions/{session_id}",
            json=invalid_identity,
            headers=headers,
        )
        assert patch_response.status_code == 404
        assert "not found or disabled" in patch_response.get_json()["error"]


def test_create_and_patch_session_canonicalize_agent_identity(isolated_state, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from storage import projects_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, folder_path=str(tmp_path))

    store = VibeAgentStore()
    try:
        agent = store.create(name="active-reviewer", backend="codex")
    finally:
        store.close()

    client = app.test_client()
    headers = csrf_headers(client)
    create_response = client.post(
        "/api/sessions",
        json={
            "project_id": project["id"],
            "agent_id": agent.id,
            "agent_backend": "claude",
        },
        headers=headers,
    )
    assert create_response.status_code == 201
    created = create_response.get_json()
    assert (created["agent_id"], created["agent_name"], created["agent_backend"]) == (
        agent.id,
        agent.name,
        agent.backend,
    )

    plain_response = client.post(
        "/api/sessions",
        json={"project_id": project["id"]},
        headers=headers,
    )
    patch_response = client.patch(
        f"/api/sessions/{plain_response.get_json()['id']}",
        json={"agent_id": agent.id},
        headers=headers,
    )
    assert patch_response.status_code == 200
    patched = patch_response.get_json()
    assert (patched["agent_id"], patched["agent_name"], patched["agent_backend"]) == (
        agent.id,
        agent.name,
        agent.backend,
    )

    cleared_response = client.patch(
        f"/api/sessions/{plain_response.get_json()['id']}",
        json={"agent_name": None},
        headers=headers,
    )
    assert cleared_response.status_code == 200
    cleared = cleared_response.get_json()
    assert cleared["agent_id"] is None
    assert cleared["agent_name"] is None
    assert cleared["agent_backend"] == ""


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


def test_fork_session_marks_running_source_for_trim(isolated_state, tmp_path, monkeypatch):
    from sqlalchemy import update

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from vibe.ui_server import app

    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "native-source-1", ["user", "assistant", "user"])
    scope_id, session_id = _make_session(
        tmp_path,
        agent_name="opencode",
        agent_backend="opencode",
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == session_id)
            .values(
                agent_backend="opencode",
                agent_variant="opencode",
                agent_name="opencode",
                native_session_id="native-source-1",
                title="Source session",
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            message_type="user",
            text="do work",
        )

    in_flight = AsyncMock(
        return_value={
            "status_code": 200,
            "body": {"ok": True, "in_flight": True, "native_turn_started": True},
        }
    )
    with (
        patch("vibe.sse_broker.broker.publish"),
        patch("vibe.internal_client.turn_state", in_flight),
    ):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/fork", json={}, headers=headers)

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["metadata"]["fork_trim_latest_running_turn"] is True
    assert payload["metadata"]["fork_native_turn_started"] is True
    assert payload["metadata"]["fork_opencode_message_id"] == "oc-msg-3"
    in_flight.assert_awaited_once_with(session_id)


def test_fork_session_does_not_mark_claude_running_source_for_trim(isolated_state, tmp_path):
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
            .values(native_session_id="claude-source-1", title="Source session")
        )

    in_flight = AsyncMock(
        return_value={
            "status_code": 200,
            "body": {"ok": True, "in_flight": True, "native_turn_started": True},
        }
    )
    with (
        patch("vibe.sse_broker.broker.publish"),
        patch("vibe.internal_client.turn_state", in_flight),
    ):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/fork", json={}, headers=headers)

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["metadata"]["fork_source_backend"] == "claude"
    assert payload["metadata"]["fork_trim_latest_running_turn"] is False
    assert payload["metadata"]["fork_native_turn_started"] is False


def test_fork_session_trims_post_accept_open_code_before_native_turn_starts(isolated_state, tmp_path, monkeypatch):
    from sqlalchemy import update

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from vibe.ui_server import app

    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "native-source-1", ["user"])
    _, session_id = _make_session(
        tmp_path,
        agent_name="opencode",
        agent_backend="opencode",
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == session_id)
            .values(
                agent_backend="opencode",
                agent_variant="opencode",
                agent_name="opencode",
                native_session_id="native-source-1",
                title="Source session",
            )
        )

    in_flight = AsyncMock(
        return_value={
            "status_code": 200,
            "body": {"ok": True, "in_flight": True, "native_turn_started": False},
        }
    )
    with (
        patch("vibe.sse_broker.broker.publish"),
        patch("vibe.internal_client.turn_state", in_flight),
    ):
        client = app.test_client()
        headers = csrf_headers(client)
        response = client.post(f"/api/sessions/{session_id}/fork", json={}, headers=headers)

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["metadata"]["fork_trim_latest_running_turn"] is True
    assert payload["metadata"]["fork_native_turn_started"] is True
    assert payload["metadata"]["fork_opencode_message_id"] == "oc-msg-1"


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
    _ensure_vibe_agent("codex", "codex")
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
            message_type="result",
            text="answer",
        )
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            text="follow-up",
        )
        message_deliveries.set_draft(conn, session_id, "draft text")

    async def in_flight(session_id_inner):
        assert session_id_inner == session_id
        return {
            "status_code": 200,
            "body": {
                "in_flight": True,
                "foreground": "running",
                "pending_input_count": 1,
                "background_activities": [
                    {
                        "id": "task-1",
                        "backend": "claude",
                        "runtime_key": "runtime-1",
                        "kind": "background_task",
                        "status": "running",
                    }
                ],
                "pending_activity_output_count": 0,
                "connection": "connected",
            },
        }

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
    assert body["draft"]["updated_at"] is not None
    assert body["turn_state"]["in_flight"] is True
    assert body["turn_state"]["foreground"] == "running"
    assert body["turn_state"]["pending_input_count"] == 1
    assert body["turn_state"]["background_activities"][0]["id"] == "task-1"
    assert body["turn_state"]["connection"] == "connected"


def test_session_draft_compare_and_set_protects_newer_writes_and_clears(
    isolated_state,
    tmp_path,
):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    headers = csrf_headers(client)
    path = f"/api/sessions/{session_id}/draft"

    created = client.put(
        path,
        json={"text": "first", "expected_updated_at": None},
        headers=headers,
    )
    assert created.status_code == 200
    created_draft = created.get_json()["draft"]
    assert created_draft["text"] == "first"
    first_revision = created_draft["updated_at"]
    assert first_revision is not None

    fetched = client.get(path)
    assert fetched.status_code == 200
    assert fetched.get_json() == created_draft

    stale = client.put(
        path,
        json={"text": "stale", "expected_updated_at": None},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.get_json() == {
        "ok": False,
        "code": "draft_conflict",
        "draft": created_draft,
    }

    revisionless = client.put(
        path,
        json={"text": "legacy overwrite"},
        headers=headers,
    )
    assert revisionless.status_code == 409
    assert revisionless.get_json() == {
        "ok": False,
        "code": "draft_conflict",
        "draft": created_draft,
    }

    updated = client.put(
        path,
        json={"text": "second", "expected_updated_at": first_revision},
        headers=headers,
    )
    assert updated.status_code == 200
    updated_draft = updated.get_json()["draft"]
    assert updated_draft["text"] == "second"
    second_revision = updated_draft["updated_at"]
    assert second_revision not in (None, first_revision)

    cleared = client.put(
        path,
        json={"text": "", "expected_updated_at": second_revision},
        headers=headers,
    )
    assert cleared.status_code == 200
    cleared_draft = cleared.get_json()["draft"]
    assert cleared_draft["text"] == ""
    assert cleared_draft["updated_at"] not in (None, second_revision)

    resurrect = client.put(
        path,
        json={"text": "resurrected", "expected_updated_at": second_revision},
        headers=headers,
    )
    assert resurrect.status_code == 409
    assert resurrect.get_json()["draft"] == cleared_draft


def test_session_draft_reserves_writer_before_cas_reads(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.agent_session_rows import reserve_write_lock as real_reserve_write_lock
    from vibe.ui_server import app

    real_get_session = sessions_service.get_session
    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    headers = csrf_headers(client)
    calls = []

    def reserve_write_lock(conn):
        calls.append("write_lock")
        return real_reserve_write_lock(conn)

    def get_session(conn, target_session_id):
        calls.append("session_read")
        return real_get_session(conn, target_session_id)

    with (
        patch("storage.agent_session_rows.reserve_write_lock", reserve_write_lock),
        patch("core.services.sessions.get_session", get_session),
    ):
        response = client.put(
            f"/api/sessions/{session_id}/draft",
            json={"text": "serialized", "expected_updated_at": None},
            headers=headers,
        )

    assert response.status_code == 200
    assert calls[:2] == ["write_lock", "session_read"]


def test_queue_row_send_now_passes_the_exact_delivery_id(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    send_now = AsyncMock(
        return_value={
            "status_code": 409,
            "body": {"ok": False, "code": "stale_head"},
        }
    )
    with patch("vibe.internal_client.send_now", send_now):
        client = app.test_client()
        response = client.post(
            f"/api/sessions/{session_id}/queue/del_requested/send-now",
            headers=csrf_headers(client),
        )

    assert response.status_code == 409
    assert response.get_json()["code"] == "stale_head"
    send_now.assert_awaited_once_with(
        session_id,
        expected_delivery_id="del_requested",
    )


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


def test_chat_bootstrap_omits_memory_from_the_generic_config(isolated_state, tmp_path):
    """Bootstrap is reachable by an authenticated remote user over the tunnel.

    Memory settings -- enablement, both processing endpoint URLs and model
    names, and API-key-presence flags -- are served only by the
    direct-loopback-only /api/memory/* routes, so a generic config projection
    must not carry them. /api/config already excluded them; this endpoint did
    not, which is the whole reason the exclusion now lives in one projection.
    """

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
    config_payload = response.get_json()["config"]
    # Still a real config projection, just without the Memory block.
    assert "setup_state" in config_payload
    assert "memory" not in config_payload


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


def test_cancel_route_forwards_controller_status_recovery(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert sessions_service.set_agent_status(conn, session_id, "running") is True

    cancel_mock = AsyncMock(
        return_value={
            "status_code": 404,
            "body": {
                "ok": False,
                "code": "not_in_flight",
                "recovered_agent_status": True,
            },
        }
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
        assert sessions_service.get_session(conn, session_id)["agent_status"] == "running"


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


def test_turn_state_forwards_controller_status_recovery(isolated_state, tmp_path):
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert sessions_service.set_agent_status(conn, session_id, "running") is True

    async def idle(session_id_inner):
        assert session_id_inner == session_id
        return {
            "status_code": 200,
            "body": {"in_flight": False, "recovered_agent_status": True},
        }

    with patch("vibe.internal_client.turn_state", idle):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/turn-state")

    assert response.status_code == 200
    body = response.get_json()
    assert body["in_flight"] is False
    assert body["recovered_agent_status"] is True
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, session_id)["agent_status"] == "running"


def test_turn_state_route_preserves_orthogonal_runtime_axes(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    async def projected(session_id_inner):
        assert session_id_inner == session_id
        return {
            "status_code": 200,
            "body": {
                "in_flight": False,
                "foreground": "idle",
                "native_turn_started": False,
                "pending_input_count": 2,
                "background_activities": [
                    {
                        "id": "task-1",
                        "backend": "claude",
                        "runtime_key": "runtime-1",
                        "kind": "background_task",
                        "status": "running",
                    }
                ],
                "pending_activity_output_count": 1,
                "connection": "reconnecting",
            },
        }

    with patch("vibe.internal_client.turn_state", projected):
        client = app.test_client()
        response = client.get(f"/api/sessions/{session_id}/turn-state")

    assert response.status_code == 200
    body = response.get_json()
    assert body["foreground"] == "idle"
    assert body["in_flight"] is False
    assert body["pending_input_count"] == 2
    assert body["pending_activity_output_count"] == 1
    assert [item["id"] for item in body["background_activities"]] == ["task-1"]
    assert body["connection"] == "reconnecting"


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
    _ensure_vibe_agent("codex", "codex")

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


def test_patch_session_visibility_and_scope_are_independent(isolated_state, tmp_path, monkeypatch):
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    import vibe.sse_broker as sse_broker
    from vibe.ui_server import app

    original_scope_id, session_id = _make_session(tmp_path)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(sse_broker.broker, "publish", lambda topic, data: published.append((topic, data)))
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        original_workdir = conn.execute(
            agent_sessions.select().where(agent_sessions.c.id == session_id)
        ).mappings().one()["workdir"]

    client = app.test_client()
    headers = csrf_headers(client)
    visibility_response = client.patch(
        f"/api/sessions/{session_id}",
        json={"visibility": "background"},
        headers=headers,
    )
    assert visibility_response.status_code == 200
    assert visibility_response.get_json()["visibility"] == "background"
    assert visibility_response.get_json()["scope_id"] == original_scope_id
    activity = [data for topic, data in published if topic == "session.activity"]
    assert activity[-2]["event"] == "updated"
    assert activity[-2]["visibility"] == "background"
    assert activity[-1] == {
        "session_id": session_id,
        "scope_id": original_scope_id,
        "event": "user_message",
        "reason": "session_placement_changed",
    }

    published.clear()
    scope_response = client.patch(
        f"/api/sessions/{session_id}",
        json={"scope_id": None},
        headers=headers,
    )
    assert scope_response.status_code == 200
    body = scope_response.get_json()
    assert body["visibility"] == "background"
    assert body["scope_id"] is None
    assert body["project_id"] is None
    assert body["workdir"] == original_workdir
    activity = [data for topic, data in published if topic == "session.activity"]
    assert [event["event"] for event in activity] == ["updated", "user_message"]
    assert activity[-1]["scope_id"] == original_scope_id

    published.clear()
    foreground_response = client.patch(
        f"/api/sessions/{session_id}",
        json={"visibility": "foreground", "scope_id": original_scope_id},
        headers=headers,
    )
    assert foreground_response.status_code == 200
    assert foreground_response.get_json()["workdir"] == original_workdir
    activity = [data for topic, data in published if topic == "session.activity"]
    assert [event["event"] for event in activity] == ["updated", "created"]
    assert activity[-1]["scope_id"] == original_scope_id


def test_patch_session_pin_persists_and_broadcasts(isolated_state, tmp_path, monkeypatch):
    import vibe.sse_broker as sse_broker
    from vibe.ui_server import app

    scope_id, session_id = _make_session(tmp_path)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(sse_broker.broker, "publish", lambda topic, data: published.append((topic, data)))

    client = app.test_client()
    response = client.patch(
        f"/api/sessions/{session_id}",
        json={"pinned": True},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["pinned"] is True
    activity = [data for topic, data in published if topic == "session.activity"]
    assert activity == [
        {
            "session_id": session_id,
            "scope_id": scope_id,
            "event": "updated",
            "title": None,
            "visibility": "foreground",
            "pinned": True,
        }
    ]

    get_response = client.get(f"/api/sessions/{session_id}")
    assert get_response.get_json()["pinned"] is True


def test_patch_session_rejects_non_boolean_pin(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    response = client.patch(
        f"/api/sessions/{session_id}",
        json={"pinned": "true"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "pinned must be a boolean"


def test_patch_session_rejects_invalid_visibility(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    response = client.patch(
        f"/api/sessions/{session_id}",
        json={"visibility": "hidden"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400


def test_patch_session_rejects_unknown_target_scope_as_invalid_value(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    response = client.patch(
        f"/api/sessions/{session_id}",
        json={"scope_id": "avibe::project::missing"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400


def test_standalone_session_accepts_attachment_upload(isolated_state, monkeypatch):
    import base64
    import threading

    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.models import media_objects
    from vibe import ui_server

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = sessions_service.create_session(
            conn,
            scope_id=None,
            agent_backend="codex",
            visibility="foreground",
        )

    loop_threads: list[int] = []
    engine_threads: list[int] = []
    session_threads: list[int] = []
    decode_threads: list[int] = []
    original_dispatch = ui_server._dispatch_native_ui_request
    original_projects_engine = ui_server._projects_engine
    original_get_session = sessions_service.get_session
    original_b64decode = base64.b64decode

    async def tracked_dispatch(starlette_request, handler):
        loop_threads.append(threading.get_ident())
        return await original_dispatch(starlette_request, handler)

    def tracked_projects_engine():
        engine_threads.append(threading.get_ident())
        return original_projects_engine()

    def tracked_get_session(*args, **kwargs):
        session_threads.append(threading.get_ident())
        return original_get_session(*args, **kwargs)

    def tracked_b64decode(*args, **kwargs):
        decode_threads.append(threading.get_ident())
        return original_b64decode(*args, **kwargs)

    monkeypatch.setattr(ui_server, "_dispatch_native_ui_request", tracked_dispatch)
    monkeypatch.setattr(ui_server, "_projects_engine", tracked_projects_engine)
    monkeypatch.setattr(sessions_service, "get_session", tracked_get_session)
    monkeypatch.setattr(base64, "b64decode", tracked_b64decode)

    client = ui_server.app.test_client()
    response = client.post(
        f"/api/sessions/{session['id']}/attachments",
        json={
            "name": "standalone.txt",
            "mime": "text/plain",
            "data": "data:text/plain;base64,"
            + base64.b64encode(b"standalone upload").decode("ascii"),
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 201
    token = response.get_json()["token"]
    with engine.connect() as conn:
        row = conn.execute(
            media_objects.select().where(media_objects.c.token == token)
        ).mappings().one()
    assert row["scope_id"] is None
    assert row["session_id"] == session["id"]
    assert len(loop_threads) == 1
    assert engine_threads
    assert session_threads
    assert decode_threads
    assert all(
        thread_id != loop_threads[0]
        for thread_id in (*engine_threads, *session_threads, *decode_threads)
    )


def test_legacy_attachment_upload_preserves_json_contract(isolated_state, tmp_path, monkeypatch):
    import base64

    from core import workbench_media
    from storage import media_service
    from storage.db import create_sqlite_engine
    from vibe.ui_server import app

    raw = b"legacy upload"
    monkeypatch.setattr(workbench_media, "MAX_WORKBENCH_ATTACHMENT_BYTES", len(raw))
    encoded = base64.b64encode(raw).decode("ascii")
    wrapped = " \t\r\n\v\f".join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        content=json.dumps(
            {
                "name": "legacy.txt",
                "mime": "text/plain",
                "data": f"data:text/plain;base64,{wrapped}",
            }
        ),
        headers={
            **csrf_headers(client),
            "Content-Type": "application/vnd.avibe+json; charset=utf-8",
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["size"] == len(raw)
    with create_sqlite_engine().connect() as conn:
        row = media_service.get_by_token(conn, payload["token"])
    assert row is not None
    assert Path(row["local_path"]).read_bytes() == raw


def test_legacy_attachment_upload_rejects_invalid_base64(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        json={"name": "broken.txt", "mime": "text/plain", "data": "aGVs!bG8="},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_upload"


def test_legacy_attachment_upload_rejects_oversized_base64(isolated_state, tmp_path, monkeypatch):
    import base64

    from core import workbench_media
    from vibe.ui_server import app

    monkeypatch.setattr(workbench_media, "MAX_WORKBENCH_ATTACHMENT_BYTES", 4)
    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        json={
            "name": "large.txt",
            "mime": "text/plain",
            "data": base64.b64encode(b"1234567").decode("ascii"),
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.get_json()["code"] == "too_large"
    assert response.get_json()["max_file_bytes"] == 4


def test_attachment_upload_preserves_unicode_name_and_binary_body(isolated_state, tmp_path):
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()

    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("报告.txt", b"raw-binary\x00body", "text/plain")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["name"] == "报告.txt"
    assert payload["mime"] == "text/plain"
    assert payload["size"] == 15

    from storage import media_service
    from storage.db import create_sqlite_engine

    with create_sqlite_engine().connect() as conn:
        row = media_service.get_by_token(conn, payload["token"])
    assert row is not None
    assert Path(row["local_path"]).read_bytes() == b"raw-binary\x00body"


def test_attachment_upload_rejects_oversized_part_without_partial_file(
    isolated_state,
    tmp_path,
    monkeypatch,
):
    from config import paths
    from core import workbench_media
    from vibe.ui_server import app

    monkeypatch.setattr(workbench_media, "MAX_WORKBENCH_ATTACHMENT_BYTES", 4)
    _, session_id = _make_session(tmp_path)
    client = app.test_client()

    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("large.bin", b"12345", "application/octet-stream")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "ok": False,
        "error": {
            "code": "too_large",
            "message": "The file exceeds the attachment size limit.",
        },
        "code": "too_large",
        "message": "The file exceeds the attachment size limit.",
        "max_file_bytes": 4,
    }
    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    assert not upload_dir.exists() or not list(upload_dir.iterdir())


def test_attachment_upload_cleans_partial_file_after_registration_failure(
    isolated_state,
    tmp_path,
    monkeypatch,
):
    from config import paths
    from storage import media_service
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)

    def fail_register(*_args, **_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(media_service, "register", fail_register)
    client = app.test_client()
    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "upload_failed"
    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    assert not upload_dir.exists() or not list(upload_dir.iterdir())


def test_attachment_upload_cleans_failed_commit_before_releasing_upload_lock(
    isolated_state,
    tmp_path,
    monkeypatch,
):
    from config import paths
    from core import workbench_media
    from storage import media_service
    from storage.db import create_sqlite_engine
    from vibe import ui_server

    _, session_id = _make_session(tmp_path)
    engine = create_sqlite_engine()
    original_begin = engine.begin
    original_lock = workbench_media.workbench_attachment_upload_lock
    original_unlink = Path.unlink
    fail_commit = True
    lock_held = False
    cleanup_lock_states: list[bool] = []

    @contextmanager
    def fail_first_commit():
        nonlocal fail_commit
        with original_begin() as conn:
            yield conn
            if fail_commit:
                fail_commit = False
                raise OSError("commit failed")

    @contextmanager
    def tracked_upload_lock(*args, **kwargs):
        nonlocal lock_held
        with original_lock(*args, **kwargs):
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    def tracked_unlink(path: Path, *args, **kwargs):
        if path.name.startswith("upload-id-123456_"):
            cleanup_lock_states.append(lock_held)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(engine, "begin", fail_first_commit)
    monkeypatch.setattr(ui_server, "_projects_engine", lambda: engine)
    monkeypatch.setattr(
        workbench_media,
        "workbench_attachment_upload_lock",
        tracked_upload_lock,
    )
    monkeypatch.setattr(Path, "unlink", tracked_unlink)

    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    first = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("note.txt", b"first", "text/plain")},
        headers=headers,
    )
    retried = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("note.txt", b"retry", "text/plain")},
        headers=headers,
    )

    assert first.status_code == 500
    assert cleanup_lock_states == [True]
    assert retried.status_code == 201
    with engine.connect() as conn:
        row = media_service.get_by_token(conn, retried.get_json()["token"])
    assert row is not None
    assert Path(row["local_path"]).read_bytes() == b"retry"
    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    assert list(upload_dir.iterdir()) == [Path(row["local_path"])]


def test_attachment_upload_retry_reuses_committed_file_and_token(isolated_state, tmp_path):
    from config import paths
    from storage.db import create_sqlite_engine
    from storage.models import media_objects
    from vibe.ui_server import app

    _, session_id = _make_session(tmp_path)
    client = app.test_client()
    headers = csrf_headers(client)

    first = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers=headers,
    )
    retried = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers=headers,
    )

    assert first.status_code == 201
    assert retried.status_code == 200
    assert retried.get_json()["token"] == first.get_json()["token"]
    upload_dir = paths.get_attachments_dir() / "avibe" / session_id
    assert len(list(upload_dir.iterdir())) == 1
    with create_sqlite_engine().connect() as conn:
        rows = conn.execute(
            select(media_objects.c.token).where(
                media_objects.c.session_id == session_id,
                media_objects.c.source == "user_upload",
            )
        ).all()
    assert rows == [(first.get_json()["token"],)]


def test_attachment_upload_errors_follow_configured_language(isolated_state, monkeypatch):
    import threading

    from core.services import settings as settings_service
    from vibe import ui_server

    loop_threads: list[int] = []
    config_threads: list[int] = []
    original_dispatch = ui_server._dispatch_native_ui_request

    async def tracked_dispatch(starlette_request, handler):
        loop_threads.append(threading.get_ident())
        return await original_dispatch(starlette_request, handler)

    def load_config():
        config_threads.append(threading.get_ident())
        return SimpleNamespace(language="zh")

    monkeypatch.setattr(ui_server, "_dispatch_native_ui_request", tracked_dispatch)
    monkeypatch.setattr(
        settings_service,
        "load_config_or_default",
        load_config,
    )
    client = ui_server.app.test_client()

    response = client.post(
        "/api/sessions/ses_missing/attachments",
        data={"upload_id": "upload-id-123456"},
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == {
        "code": "session_not_found",
        "message": "当前会话已不可用。",
    }
    assert len(loop_threads) == 1
    assert config_threads
    assert all(thread_id != loop_threads[0] for thread_id in config_threads)


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
    _ensure_vibe_agent("claude-pro", "claude")

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
    _ensure_vibe_agent("codex", "codex")

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
    _ensure_vibe_agent("codex", "codex")

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
