import asyncio
import gzip
import json
import threading
from types import SimpleNamespace

import pytest

from fastapi import Request as FastAPIRequest
from storage.importer import ensure_sqlite_state
from vibe.ui_compat import (
    TEST_REMOTE_ADDR_HEADER,
    CompatApp,
    _read_json_payload,
    normalize_response,
    route_path_to_fastapi,
    run_maybe_async,
    request,
)
from starlette.websockets import WebSocketDisconnect

from vibe import remote_access, ui_server
from vibe.ui_server import app
from tests.test_api_save_config_merge import _full_config_payload
from tests.ui_server_test_helpers import csrf_headers


@pytest.fixture(autouse=True)
def _clear_web_push_delivery_dispositions():
    from core import web_push_notifications

    web_push_notifications._RECENT_DELIVERY_DISPOSITIONS.clear()
    yield


def _raw_client_get(client, path: str, *, headers: dict[str, str] | None = None):
    request_headers = {TEST_REMOTE_ADDR_HEADER: "127.0.0.1"}
    request_headers.update(headers or {})
    with client._client.stream(
        "GET",
        f"http://127.0.0.1{path}",
        headers=request_headers,
    ) as response:
        body = b"".join(response.iter_raw())
    return response, body


def test_archive_definition_wakes_do_not_block_the_asgi_loop(monkeypatch):
    published: list[tuple[int, str]] = []

    def _publish(*, definition_type: str) -> None:
        published.append((threading.get_ident(), definition_type))

    monkeypatch.setattr(
        "core.inbox_events.publish_definitions_updated",
        _publish,
    )

    async def _exercise() -> int:
        loop_thread = threading.get_ident()
        await ui_server._archive_publish_definition_updates(
            {"tasks": 1, "watches": 1}
        )
        return loop_thread

    loop_thread = asyncio.run(_exercise())

    assert {definition_type for _, definition_type in published} == {
        "scheduled",
        "watch",
    }
    assert all(thread_id != loop_thread for thread_id, _ in published)


def test_hfr_283_archive_run_cancellations_wake_runtime_consumers(monkeypatch):
    published: list[tuple[str, dict[str, str], float]] = []

    async def _publish(event_type, data, *, timeout):  # noqa: ANN001, ANN202
        published.append((event_type, data, timeout))
        return {"ok": True}

    monkeypatch.setattr("vibe.internal_client.publish_event", _publish)

    asyncio.run(
        ui_server._archive_publish_run_updates(
            "session-a",
            {"runs": 2},
        )
    )
    asyncio.run(
        ui_server._archive_publish_run_updates(
            "session-b",
            {"runs": 0},
        )
    )

    assert published == [
        (
            "runs.updated",
            {"session_id": "session-a", "reason": "session_archived"},
            1.5,
        )
    ]


def test_session_archive_delegates_terminal_mutation_to_controller(
    monkeypatch,
    tmp_path,
):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage import workbench_sessions_service
    from core.services import sessions as sessions_service
    from vibe import internal_client

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        session_id = workbench_sessions_service.create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend="claude",
            title="Archive me",
        )["id"]

    events: list[str] = []
    controller_call_active = False

    async def _archive_via_controller(actual_session_id: str):
        nonlocal controller_call_active
        assert actual_session_id == session_id
        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "active"
        events.append("controller")
        controller_call_active = True
        try:
            with engine.begin() as conn:
                session = sessions_service.archive_session(conn, session_id)
        finally:
            controller_call_active = False
        return {
            "status_code": 200,
            "body": {"ok": True, "session": session},
        }

    original_archive = sessions_service.archive_session

    def _archive(conn, actual_session_id):
        assert controller_call_active
        events.append("archive")
        return original_archive(conn, actual_session_id)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(internal_client, "memory_archive_session", _archive_via_controller)
    monkeypatch.setattr(sessions_service, "archive_session", _archive)
    monkeypatch.setattr(ui_server, "_archive_cancel_turn", _noop)

    client = app.test_client()
    response = client.delete(
        f"/api/sessions/{session_id}",
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert events == ["controller", "archive"]
    with engine.connect() as conn:
        assert workbench_sessions_service.get_session(conn, session_id)["status"] == "archived"


def test_session_archive_fails_closed_when_controller_is_unavailable(
    monkeypatch,
    tmp_path,
):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage import workbench_sessions_service
    from vibe import internal_client

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        session_id = workbench_sessions_service.create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend="claude",
            title="Keep active",
        )["id"]

    async def _unavailable(_session_id: str):
        raise internal_client.InternalServerUnavailable("controller unavailable")

    monkeypatch.setattr(internal_client, "memory_archive_session", _unavailable)
    client = app.test_client()
    response = client.delete(
        f"/api/sessions/{session_id}",
        headers=csrf_headers(client),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "session_archive_unavailable"
    with engine.connect() as conn:
        assert workbench_sessions_service.get_session(conn, session_id)["status"] == "active"


@pytest.mark.parametrize("session_kind", ["missing", "reserved", "archived"])
def test_session_archive_preflight_skips_controller_lifecycle_for_ineligible_rows(
    monkeypatch,
    tmp_path,
    session_kind,
):
    from unittest.mock import AsyncMock

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import archive_session, create_session
    from vibe import internal_client

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    session_id = "ses-missing"
    with engine.begin() as conn:
        if session_kind == "reserved":
            session_id = WORKSPACE_NOTICE_SESSION_ID
        elif session_kind == "archived":
            project_dir = tmp_path / "project"
            project_dir.mkdir()
            project = create_project(conn, str(project_dir), display_name="Project")
            session_id = create_session(
                conn,
                scope_id=project["scope_id"],
                agent_backend="claude",
            )["id"]
            archive_session(conn, session_id)

    archive_session = AsyncMock()
    monkeypatch.setattr(internal_client, "memory_archive_session", archive_session)
    client = app.test_client()

    response = client.delete(
        f"/api/sessions/{session_id}",
        headers=csrf_headers(client),
    )

    assert response.status_code == {
        "missing": 404,
        "reserved": 403,
        "archived": 200,
    }[session_kind]
    archive_session.assert_not_awaited()


def test_websocket_echo_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VIBE_UI_ENABLE_WS_ECHO", raising=False)

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect("/ws/echo"):
            pass

    assert exc.value.code == 1008


def test_websocket_echo_smoke_when_enabled(monkeypatch):
    monkeypatch.setenv("VIBE_UI_ENABLE_WS_ECHO", "1")

    with app.test_client().websocket_connect("/ws/echo") as websocket:
        websocket.send_text("hello")

        assert websocket.receive_text() == "echo: hello"


def test_fastapi_schema_routes_are_not_exposed():
    client = app.test_client()

    docs_response = client.get("/docs")
    assert b"swagger-ui" not in docs_response.content.lower()
    assert client.get("/openapi.json").status_code != 200


def test_backend_auth_test_routes_through_controller_runtime(monkeypatch):
    from vibe import internal_client

    calls = []

    async def _test_backend_auth(backend, *, model=None):
        calls.append((backend, model))
        return {
            "status_code": 200,
            "body": {"ok": True, "excerpt": "hello"},
        }

    monkeypatch.setattr(internal_client, "test_backend_auth", _test_backend_auth)
    client = app.test_client()

    response = client.post(
        "/api/backend/codex/auth/test",
        json={"model": "gpt-5.4-mini"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "excerpt": "hello"}
    assert calls == [("codex", "gpt-5.4-mini")]


def test_thread_settings_routes_use_native_fastapi(monkeypatch):
    from vibe import api

    saved_payloads = []
    deleted_scopes = []
    monkeypatch.setattr(
        api,
        "save_thread_settings",
        lambda payload: saved_payloads.append(payload) or {"ok": True, "settings": payload["settings"]},
    )
    monkeypatch.setattr(
        api,
        "delete_thread_settings",
        lambda platform, channel_id, thread_id: deleted_scopes.append(
            (platform, channel_id, thread_id)
        )
        or {"ok": True, "removed": True},
    )

    post_route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/settings/thread" and "POST" in route.methods
    )
    delete_route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/settings/thread" and "DELETE" in route.methods
    )
    assert post_route.endpoint.__name__ == "thread_settings_post"
    assert delete_route.endpoint.__name__ == "thread_settings_delete"

    client = app.test_client()
    saved = client.post(
        "/api/settings/thread",
        json={
            "platform": "telegram",
            "channel_id": "-1001",
            "thread_id": "42",
            "settings": {"enabled": True},
        },
        headers=csrf_headers(client),
    )
    deleted = client.delete(
        "/api/settings/thread?platform=telegram&channel_id=-1001&thread_id=42",
        headers=csrf_headers(client),
    )

    assert saved.status_code == 200
    assert saved.get_json()["settings"] == {"enabled": True}
    assert saved_payloads[0]["thread_id"] == "42"
    assert deleted.status_code == 200
    assert deleted.get_json() == {"ok": True, "removed": True}
    assert deleted_scopes == [("telegram", "-1001", "42")]


def test_the_usage_read_is_served_natively_rather_than_from_a_compat_worker(monkeypatch):
    """Review 4966281026 finding 4: this read blocks, so where it is served matters.

    Summarising the ledger takes the lock its writers hold across an fsync. The
    compat surface hands a sync handler to a threadpool worker, so serving it
    there occupies a UI worker for as long as the disk takes — while the native
    surface awaits it on the loop, which is what the async client below exists
    for. `ui_compat` names its endpoints `<name>_compat_endpoint`, so the route
    table is where the two are told apart.
    """

    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    asked: list[int] = []

    class LedgerClient:
        async def usage_summary(self, *, days: int) -> dict:
            asked.append(days)
            return {"window_days": days}

    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: LedgerClient())

    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/models/usage" and "GET" in route.methods
    )
    assert route.endpoint.__name__ == "model_hub_usage_get"

    response = app.test_client().get("/api/models/usage?days=7")

    assert response.status_code == 200
    assert response.get_json()["usage"] == {"window_days": 7}
    assert asked == [7]


def test_scope_settings_routes_report_localized_stale_agent_binding_conflicts(monkeypatch):
    from core.services import settings as settings_service
    from storage.settings_service import StaleScopeAgentBindingError
    from vibe import api

    def stale(*_args, **_kwargs):
        raise StaleScopeAgentBindingError(scope_id="slack::channel::C1")

    monkeypatch.setattr(
        settings_service,
        "load_config_or_default",
        lambda: SimpleNamespace(language="zh"),
    )
    monkeypatch.setattr(api, "save_settings", stale)
    monkeypatch.setattr(api, "save_thread_settings", stale)
    monkeypatch.setattr(api, "save_users", stale)

    client = app.test_client()
    headers = csrf_headers(client)
    responses = (
        client.post("/api/settings", json={"platform": "slack"}, headers=headers),
        client.post(
            "/api/settings/thread",
            json={"platform": "telegram", "channel_id": "C1", "thread_id": "T1", "settings": {}},
            headers=headers,
        ),
        client.post("/api/users", json={"platform": "slack", "users": {}}, headers=headers),
    )

    for response in responses:
        assert response.status_code == 409
        assert response.get_json() == {
            "ok": False,
            "code": "settings_conflict",
            "message": "这些设置打开后，Agent 路由已发生变化。",
            "error": {
                "code": "settings_conflict",
                "message": "这些设置打开后，Agent 路由已发生变化。",
            },
            "hint": "请重新加载设置后再次修改。",
            "details": {"scope_id": "slack::channel::C1"},
        }


def test_scope_settings_routes_localize_unavailable_agent_errors(monkeypatch):
    from core.services import settings as settings_service
    from storage.settings_service import ScopeAgentUnavailableError
    from vibe import api

    def unavailable(*_args, **_kwargs):
        raise ScopeAgentUnavailableError(agent_name="pm")

    monkeypatch.setattr(
        settings_service,
        "load_config_or_default",
        lambda: SimpleNamespace(language="zh"),
    )
    monkeypatch.setattr(api, "save_settings", unavailable)
    monkeypatch.setattr(api, "save_thread_settings", unavailable)
    monkeypatch.setattr(api, "save_users", unavailable)

    client = app.test_client()
    headers = csrf_headers(client)
    responses = (
        client.post("/api/settings", json={"platform": "slack"}, headers=headers),
        client.post(
            "/api/settings/thread",
            json={"platform": "telegram", "channel_id": "C1", "thread_id": "T1", "settings": {}},
            headers=headers,
        ),
        client.post("/api/users", json={"platform": "slack", "users": {}}, headers=headers),
    )

    for response in responses:
        assert response.status_code == 400
        assert response.get_json() == {
            "ok": False,
            "code": "agent_unavailable",
            "message": "Agent `pm` 无法用于此路由。",
            "error": {
                "code": "agent_unavailable",
                "message": "Agent `pm` 无法用于此路由。",
            },
            "hint": "请选择一个已启用的 Agent 后重新保存。",
            "details": {"agent_name": "pm"},
        }


def test_status_endpoint_uses_fast_runtime_status(monkeypatch):
    from vibe import runtime

    calls = []

    def fake_render_status(*, detect_extra_processes=True):
        calls.append(detect_extra_processes)
        return json.dumps({"state": "running", "running": True, "service_pid": 12345})

    monkeypatch.setattr(runtime, "render_status", fake_render_status)

    response = app.test_client().get("/status")

    assert response.status_code == 200
    assert response.get_json()["service_pid"] == 12345
    assert calls == [False]


def test_sessions_activity_endpoint_summary_detail_and_404(monkeypatch, tmp_path):
    """GET /api/sessions/<id>/activity: summary (no params) lists turn groups;
    ``?group_id=`` returns that group's rows; an unknown session is 404."""
    import json as _json

    from storage.db import create_sqlite_engine
    from storage.models import agent_events, agent_sessions, messages
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    sid = "ses_ep"
    with engine.begin() as conn:
        scope = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_ep", now="2026-06-01T10:00:00Z")
        conn.execute(agent_sessions.insert().values(
            id=sid, scope_id=scope, agent_backend="claude", agent_variant="default",
            session_anchor="anchor_ep", native_session_id="", status="active",
            metadata_json="{}", created_at="2026-06-01T10:00:00Z", updated_at="2026-06-01T10:00:00Z",
            last_active_at="2026-06-01T10:00:00Z",
        ))

        def _m(mid, mtype, author, ts, text):
            conn.execute(messages.insert().values(
                id=mid, scope_id=scope, session_id=sid, platform="avibe", author=author,
                type=mtype, source=("user" if author == "user" else "agent"), content_text=text,
                content_json="{}", metadata_json="{}", created_at=ts, updated_at=ts,
            ))

        _m("m_u", "user", "user", "2026-06-01T10:00:00.000000+00:00", "q")
        _m("m_a", "assistant", "agent", "2026-06-01T10:00:01.000000+00:00", "thinking")
        conn.execute(agent_events.insert().values(
            id="e_t", scope_id=scope, session_id=sid, platform="avibe", event_type="tool_call",
            visibility="trace", content_text="🔧 `Bash`", content_json=_json.dumps({"kind": "tool_call", "text": "🔧 `Bash`"}),
            metadata_json="{}", source="agent", created_at="2026-06-01T10:00:02Z", updated_at="2026-06-01T10:00:02Z",
        ))
        _m("m_r", "result", "agent", "2026-06-01T10:00:03.000000+00:00", "answer")

    client = app.test_client()

    summary = client.get(f"/api/sessions/{sid}/activity")
    assert summary.status_code == 200
    groups = summary.get_json()["groups"]
    assert len(groups) == 1
    assert groups[0]["status"] == "done"
    assert groups[0]["anchor_message_id"] == "m_r"
    assert groups[0]["steps"] == 2
    group_id = groups[0]["id"]

    detail = client.get(f"/api/sessions/{sid}/activity?group_id={group_id}")
    assert detail.status_code == 200
    rows = detail.get_json()["rows"]
    assert [r["kind"] for r in rows] == ["assistant", "tool_call"]

    assert client.get(f"/api/sessions/{sid}/activity?group_id=nope").status_code == 404
    assert client.get("/api/sessions/ses_missing/activity").status_code == 404


def _seed_search_corpus(tmp_path, monkeypatch):
    """One active + one archived session, each with a matching message."""
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions, messages
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-01T10:00:00Z"
    with engine.begin() as conn:
        scope = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_search", now=now)
        for sid, status in (("ses_search_live", "active"), ("ses_search_arch", "archived")):
            conn.execute(
                agent_sessions.insert().values(
                    id=sid,
                    scope_id=scope,
                    agent_backend="claude",
                    agent_variant="default",
                    session_anchor="anchor_" + sid,
                    native_session_id="",
                    status=status,
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                    last_active_at=now,
                )
            )
            conn.execute(
                messages.insert().values(
                    id="msg_" + sid,
                    scope_id=scope,
                    session_id=sid,
                    platform="avibe",
                    author="user",
                    type="user",
                    source="user",
                    content_text="deploy the searchable thing",
                    content_json="{}",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
    return engine


def test_search_messages_route_plumbs_include_archived(monkeypatch, tmp_path):
    """GET /api/search/messages excludes archived sessions by default and opts
    them in for ``include_archived=1``, marking each group with ``archived``."""
    _seed_search_corpus(tmp_path, monkeypatch)
    client = app.test_client()

    default = client.get("/api/search/messages?q=deploy")
    explicit_off = client.get("/api/search/messages?q=deploy&include_archived=0")
    included = client.get("/api/search/messages?q=deploy&include_archived=1")

    assert default.status_code == 200
    assert [s["session_id"] for s in default.get_json()["sessions"]] == ["ses_search_live"]
    assert default.get_json()["sessions"][0]["archived"] is False
    # Anything that is not 1/true/yes stays opt-out.
    assert explicit_off.get_json() == default.get_json()

    assert included.status_code == 200
    flags = {s["session_id"]: s["archived"] for s in included.get_json()["sessions"]}
    assert flags == {"ses_search_live": False, "ses_search_arch": True}


def _ui_error_code(body: dict):
    """The Web UI parser's own precedence rule, in its three lines.

    ``selectApiErrorFields`` (``ui/src/context/ApiContext.tsx``): take ``error`` if
    present, else a top-level ``{code, message}`` — and a STRING ``error`` *is* the
    code. Replicated here (mirrored by ui/src/context/ApiErrorParse.test.ts, which runs
    the real function) so a route that regresses to the flat coded shape fails on the
    server side, where the body is authored, instead of shipping a mangled
    ``ApiError.code`` and raw English to every locale.
    """
    raw = body.get("error") or ({"code": body["code"], "message": body.get("message")} if body.get("code") else None)
    return raw if isinstance(raw, str) else (raw or {}).get("code")


def _machine_coded_error_builders():
    """Every ``vibe/ui_server.py`` helper that answers with a MACHINE-READABLE code.

    One table, one assertion loop: the next coded route is covered by adding its
    builder here, not by writing another near-duplicate test — and the structural guard
    below fails for any coded body that skips the shared builder entirely, so a route
    cannot quietly opt out of this list.
    """
    from core.services.session_fork import (
        SESSION_AGENT_UNAVAILABLE_CODE,
        SessionForkError,
    )
    from storage.workbench_sessions_service import SessionBackendLockedError

    class _Coded(Exception):
        def __init__(self, message: str, code: str) -> None:
            super().__init__(message)
            self.code = code

    locked = SessionBackendLockedError(
        session_id="ses_1", current_backend="claude", requested_backend="codex"
    )
    return [
        # The archived 409 the whole read-only convergence hangs off (PATCH + messages POST).
        ("session_archived", lambda: ui_server._session_archived_response(), "session_archived", 409),
        # Round 5d: terminal archive vs retryable lock, on the same route. A client can
        # only tell them apart if BOTH codes survive.
        ("backend_locked", lambda: ui_server._backend_locked_response(locked), "backend_locked", 409),
        # The runtime-reserved workspace-notifications row: one code, three verbs, so the
        # per-verb sentence is the only thing that varies and BOTH must keep the code.
        (
            "reserved_session_protected",
            lambda: ui_server._reserved_session_response(ui_server.RESERVED_SESSION_PROTECTED_I18N_KEY),
            "reserved_session",
            403,
        ),
        (
            "reserved_session_read_only",
            lambda: ui_server._reserved_session_response(ui_server.RESERVED_SESSION_READ_ONLY_I18N_KEY),
            "reserved_session",
            403,
        ),
        # Fork — every branch, since they share one body builder (this round's finding).
        (
            "fork_archived",
            lambda: ui_server._session_fork_error_response(SessionForkError("agent session is archived: ses_1")),
            "session_archived",
            409,
        ),
        (
            "fork_not_found",
            lambda: ui_server._session_fork_error_response(SessionForkError("agent session id not found: ses_1")),
            "session_not_found",
            404,
        ),
        (
            "fork_not_bound",
            lambda: ui_server._session_fork_error_response(
                SessionForkError("agent session has no native session id to fork: ses_1")
            ),
            "session_not_bound",
            409,
        ),
        (
            "fork_backend_unsupported",
            lambda: ui_server._session_fork_error_response(SessionForkError("session backend cannot be forked: x")),
            "session_backend_unsupported",
            409,
        ),
        (
            "fork_backend_mismatch",
            lambda: ui_server._session_fork_error_response(SessionForkError("session backend does not match: x")),
            "session_backend_mismatch",
            409,
        ),
        (
            "fork_agent_unavailable",
            lambda: ui_server._session_fork_error_response(
                SessionForkError(
                    "source session Agent is unavailable",
                    code=SESSION_AGENT_UNAVAILABLE_CODE,
                    details={"source_session_id": "ses_1"},
                )
            ),
            SESSION_AGENT_UNAVAILABLE_CODE,
            409,
        ),
        (
            "fork_failed",
            lambda: ui_server._session_fork_error_response(SessionForkError("something else broke")),
            "session_fork_failed",
            400,
        ),
        # Show Page / Dock / icon families — already structured; pinned so the shared
        # builder they now delegate to cannot regress them either.
        (
            "show_page",
            lambda: ui_server._show_page_error_response(_Coded("nope", "session_archived")),
            "session_archived",
            400,
        ),
        (
            "show_page_conflict",
            lambda: ui_server._show_page_error_response(_Coded("taken", "share_id_taken")),
            "share_id_taken",
            409,
        ),
        (
            "show_page_missing",
            lambda: ui_server._show_page_error_response(_Coded("missing", "show_page_not_found")),
            "show_page_not_found",
            404,
        ),
        ("dock", lambda: ui_server._dock_error_response(_Coded("nope", "show_page_not_found")), "show_page_not_found", 404),
        (
            "show_page_icon",
            lambda: ui_server._show_page_icon_upload_error("session_archived", "nope"),
            "session_archived",
            400,
        ),
    ]


@pytest.mark.parametrize(
    "label,build,expected_code,expected_status",
    [pytest.param(*case, id=case[0]) for case in _machine_coded_error_builders()],
)
def test_machine_coded_error_bodies_survive_the_ui_error_parse(
    monkeypatch, tmp_path, label, build, expected_code, expected_status
):
    """Every machine-coded error body must hand the Web UI back its CODE, not a sentence.

    This is the contract round 4b established for the PATCH 409 and round 6 found
    violated on ``POST /api/sessions/<id>/fork``: the parser reads ``error`` first and a
    string there *is* the code, so the flat ``{"error": "<sentence>", "code": "<code>"}``
    shape destroys it. Asserting only the top-level ``code`` — which is what these
    routes' existing tests do — passes while every client branch keyed on the code is
    dead, which is exactly how the fork body survived two review rounds.

    Parametrized over the builders rather than duplicated per route so the next coded
    route inherits the assertion by adding one table row.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    response, status = build()
    body = json.loads(response.body)

    assert status == expected_status, label
    # What the Web UI actually resolves ``errors.<code>`` and its branches from.
    assert _ui_error_code(body) == expected_code, label
    assert isinstance(body["error"], dict), label
    # And the flat top-level fields the CLI / direct consumers read stay put.
    assert body["code"] == expected_code, label
    assert isinstance(body["message"], str) and body["message"], label
    assert body["error"]["message"] == body["message"], label


def test_session_fork_agent_unavailable_message_follows_configured_language(monkeypatch, tmp_path):
    from config import paths
    from core.services.session_fork import (
        SESSION_AGENT_UNAVAILABLE_CODE,
        SESSION_AGENT_UNAVAILABLE_I18N_KEY,
        SessionForkError,
    )
    from core.services.settings import default_config
    from vibe.i18n import t as i18n_t

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = default_config()
    config.language = "zh"
    config.save(paths.get_config_path())
    error = SessionForkError(
        "source session Agent is unavailable; choose an enabled Agent override",
        code=SESSION_AGENT_UNAVAILABLE_CODE,
        details={"source_session_id": "ses_1"},
    )

    response, status = ui_server._session_fork_error_response(error)
    body = json.loads(response.body)
    expected_message = i18n_t(f"{SESSION_AGENT_UNAVAILABLE_I18N_KEY}.message", "zh")
    expected_hint = i18n_t(f"{SESSION_AGENT_UNAVAILABLE_I18N_KEY}.hint", "zh")

    assert status == 409
    assert body["code"] == SESSION_AGENT_UNAVAILABLE_CODE
    assert body["message"] == expected_message
    assert body["message"] != str(error)
    assert body["hint"] == expected_hint
    assert body["source_session_id"] == "ses_1"


# Coded bodies that deliberately keep the flat shape, each with the reason it cannot
# reach the Web UI's shared parser. Keyed by enclosing function so line churn can't
# silently widen the exemption.
_FLAT_CODED_BODY_EXEMPTIONS = {
    # POST /api/control — StatusContext.control() uses a raw ``apiFetch`` and reads the
    # top-level ``body.code`` itself, exactly like ChatPage's messages POST.
    "control",
    # Public Show Page document + its annotation overlay: a SEPARATE document with its
    # own fetch and React tree, so no host ``ApiProvider`` ever parses these bodies
    # (round 5's "out of reach" row).
    "_show_session_event_error_response",
    "_show_event_response_from_payload",
    "serve_public_show_page",
}


def test_no_route_hand_rolls_the_flat_coded_error_body():
    """Structural guard: a NEW coded route can't reintroduce the flat shape unnoticed.

    Three review rounds spent on the same one-line defect (the PATCH body in 4b, the
    fork body in round 6) because each was found by reading rather than by a test. The
    anti-shape is mechanically detectable — a ``jsonify`` dict literal carrying both a
    machine ``code`` and a non-object ``error`` — so detect it, and require any new
    instance to be either routed through ``_coded_error_response`` or added to the
    exemption set with a reason. That is the by-construction half of the coverage; the
    parametrized test above is the positive half.
    """
    import ast
    import pathlib

    source = pathlib.Path(ui_server.__file__).read_text()
    tree = ast.parse(source)

    # Widest spans first so an inner handler overwrites its enclosing route function:
    # the exemption should name the function that actually authors the body.
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    owner_by_line: dict[int, str] = {}
    for node in sorted(functions, key=lambda n: (n.end_lineno or n.lineno) - n.lineno, reverse=True):
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
            owner_by_line[line] = node.name

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "jsonify"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        payload = node.args[0]
        if not all(isinstance(key, ast.Constant) for key in payload.keys):
            continue
        keys = [key.value for key in payload.keys]
        if "code" not in keys or "error" not in keys:
            continue
        if isinstance(payload.values[keys.index("error")], ast.Dict):
            continue
        owner = owner_by_line.get(node.lineno, "<module>")
        if owner not in _FLAT_CODED_BODY_EXEMPTIONS:
            offenders.append(f"{owner} (line {node.lineno})")

    assert not offenders, (
        "These responses pair a machine code with a flat string ``error``, which the Web UI's "
        "parser reads as the code itself — route them through ``_coded_error_response`` or add "
        f"them to _FLAT_CODED_BODY_EXEMPTIONS with the reason they can't reach it: {offenders}"
    )


def test_sessions_patch_on_archived_session_is_409(monkeypatch, tmp_path):
    """Archive is terminal — PATCH /api/sessions/<id> on an archived row answers
    409 ``session_archived`` (the backstop the read-only chat UI relies on).

    The body must use the STRUCTURED error shape. The Web UI's shared parser
    (``selectApiErrorFields`` in ``ui/src/context/ApiContext.tsx``) reads ``error``
    before the top-level ``code`` and treats a string ``error`` as the machine code,
    so a flat ``{"error": "session is archived", "code": ...}`` hands callers
    ``ApiError.code == "session is archived"``, never resolves
    ``errors.session_archived``, and renders that English sentence under every
    locale. Asserting only ``body["code"]`` passed while that was broken — the field
    the frontend consumes is the nested one, so pin both.

    The ``message`` must also come from ``vibe/i18n`` rather than a literal in the
    route (AGENTS.md §6): direct API/CLI consumers read it verbatim, and a Web UI
    client missing ``errors.session_archived`` renders it as the fallback.
    """
    from core.services.sessions import SESSION_ARCHIVED_I18N_KEY, session_archived_message
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import archive_session, create_session, get_session
    from vibe.i18n import t as i18n_t

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        sid = create_session(conn, scope_id=project["scope_id"], agent_backend="claude", title="Before")["id"]

    client = app.test_client()
    ok = client.patch(f"/api/sessions/{sid}", json={"title": "Live rename"}, headers=csrf_headers(client))
    assert ok.status_code == 200

    with engine.begin() as conn:
        archive_session(conn, sid)

    blocked = client.patch(f"/api/sessions/{sid}", json={"title": "Nope"}, headers=csrf_headers(client))
    assert blocked.status_code == 409
    body = blocked.get_json()
    # Sourced from the i18n bundle, NOT a literal in the route. Resolving the key
    # here (rather than pinning the English sentence) is what makes a hardcoded
    # regression fail: an inlined string would no longer equal the bundle value.
    expected_message = i18n_t(SESSION_ARCHIVED_I18N_KEY, "en")
    assert expected_message != SESSION_ARCHIVED_I18N_KEY  # the key really resolves
    assert body["message"] == expected_message == session_archived_message("en")
    # ...and the pre-fix literal is gone for good.
    assert body["message"] != "session is archived"
    # What the Web UI parser consumes: a nested object carrying the machine code.
    assert body["error"] == {"code": "session_archived", "message": expected_message}
    # Kept flat as well for the CLI / any direct consumer.
    assert body["code"] == "session_archived"
    assert body["ok"] is False
    # Whatever the shape, ``error`` must never be a bare string here — that is the
    # exact form the parser mis-reads as the code.
    assert not isinstance(body["error"], str)
    with engine.connect() as conn:
        assert get_session(conn, sid)["title"] == "Live rename"


def test_sessions_patch_archived_message_follows_configured_language(monkeypatch, tmp_path):
    """The 409 ``message`` is localized, not just centralized.

    A direct API/CLI consumer reads this field verbatim, and a Web UI client without
    the ``errors.session_archived`` key falls back to it — so under a ``zh`` config
    it must not be English. Guards the whole path (config language → ``vibe/i18n``
    → response body), which is what a hardcoded literal or a hardwired ``lang="en"``
    would break.
    """
    from config import paths
    from core.services.sessions import session_archived_message
    from core.services.settings import default_config
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import archive_session, create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    config = default_config()
    config.language = "zh"
    config.save(paths.get_config_path())

    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        sid = create_session(conn, scope_id=project["scope_id"], agent_backend="claude", title="Before")["id"]
        archive_session(conn, sid)

    client = app.test_client()
    blocked = client.patch(f"/api/sessions/{sid}", json={"title": "Nope"}, headers=csrf_headers(client))
    assert blocked.status_code == 409
    body = blocked.get_json()
    assert body["message"] == session_archived_message("zh")
    assert body["message"] != session_archived_message("en")
    assert body["error"]["message"] == body["message"]


def test_messages_post_into_the_reserved_system_session_dispatches_nothing(monkeypatch, tmp_path):
    """The workspace-notifications row accepts NO turn — the third and last door.

    ``visibility='system'`` deliberately keeps the reserved row in the Inbox, so its card
    is a clickable chat (``ui/src/components/workbench/InboxPage.tsx``) — while
    ``archive_session`` / ``update_session`` are the only writes that refused it. That
    left ``POST /api/sessions/ses-workspace-notices/messages``, which checked archived
    status only: a user could type into the workspace-notifications card and dispatch a
    real agent turn into a machine-owned row whose ``agent_backend`` is empty, mixing
    their conversation into the failure-notice transcript and breaking the accepted
    plan's "no backend and no turns" contract
    (``docs/plans/harness-run-reliability.md``).

    Pinned by what the CONTROLLER saw, not just by the status code: the refusal has to
    happen before dispatch and before the row is reserved, so the assertions are
    ``dispatch_async`` await count plus an unchanged message count. The ordinary session
    is the positive control — the guard must not make every send a 403.
    """
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import func, select

    from storage.agent_session_rows import (
        WORKSPACE_NOTICE_SESSION_ID,
        resolve_workspace_notice_session,
    )
    from storage.db import create_sqlite_engine
    from storage.models import messages as messages_table
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import create_session
    from vibe.i18n import t as i18n_t

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        ordinary = create_session(
            conn, scope_id=project["scope_id"], agent_backend="claude", title="Live"
        )["id"]
        assert resolve_workspace_notice_session(conn, title="Workspace notifications") == (
            WORKSPACE_NOTICE_SESSION_ID
        )

    def _message_count(session_id: str) -> int:
        with engine.connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(messages_table)
                .where(messages_table.c.session_id == session_id)
            ).scalar_one()

    assert _message_count(WORKSPACE_NOTICE_SESSION_ID) == 0
    client = app.test_client()
    headers = csrf_headers(client)
    dispatch = AsyncMock(return_value={"status_code": 202, "body": {}})

    with patch("vibe.internal_client.dispatch_async", dispatch):
        blocked = client.post(
            f"/api/sessions/{WORKSPACE_NOTICE_SESSION_ID}/messages",
            json={"text": "hello?"},
            headers=headers,
        )
        # Positive control: an ordinary session still dispatches its turn.
        accepted = client.post(
            f"/api/sessions/{ordinary}/messages",
            json={"text": "hello?"},
            headers=headers,
        )

    assert blocked.status_code == 403
    body = blocked.get_json()
    # The code the Web UI resolves ``errors.reserved_session`` from, in BOTH the nested
    # object the shared parser reads and the flat field the CLI reads.
    assert _ui_error_code(body) == "reserved_session"
    assert body["code"] == "reserved_session"
    assert not isinstance(body["error"], str)
    # Copy from ``vibe/i18n``, and the SEND-specific sentence rather than the
    # archive/edit one ("cannot be archived or modified" answers a different question).
    expected = i18n_t(ui_server.RESERVED_SESSION_READ_ONLY_I18N_KEY, "en")
    assert expected != ui_server.RESERVED_SESSION_READ_ONLY_I18N_KEY  # the key resolves
    assert body["message"] == body["error"]["message"] == expected
    assert expected != i18n_t(ui_server.RESERVED_SESSION_PROTECTED_I18N_KEY, "en")
    # Refused BEFORE the reservation and BEFORE the controller: no row, no turn.
    assert _message_count(WORKSPACE_NOTICE_SESSION_ID) == 0
    assert dispatch.await_count == 1
    assert accepted.status_code == 202
    assert _message_count(ordinary) == 0


def test_messages_post_reserved_refusal_follows_configured_language(monkeypatch, tmp_path):
    """The 403 ``message`` is localized, like the archived 409's.

    A direct API/CLI consumer reads it verbatim, and a Web UI client without the
    ``errors.reserved_session`` key falls back to it — so a hardwired ``lang="en"`` in the
    shared ``_reserved_session_response`` would ship English into a Chinese install.
    """
    from config import paths
    from core.services.settings import default_config
    from storage.agent_session_rows import (
        WORKSPACE_NOTICE_SESSION_ID,
        resolve_workspace_notice_session,
    )
    from storage.db import create_sqlite_engine
    from vibe.i18n import t as i18n_t

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    config = default_config()
    config.language = "zh"
    config.save(paths.get_config_path())

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        resolve_workspace_notice_session(conn, title="工作区通知")

    client = app.test_client()
    blocked = client.post(
        f"/api/sessions/{WORKSPACE_NOTICE_SESSION_ID}/messages",
        json={"text": "hello?"},
        headers=csrf_headers(client),
    )
    assert blocked.status_code == 403
    key = ui_server.RESERVED_SESSION_READ_ONLY_I18N_KEY
    assert blocked.get_json()["message"] == i18n_t(key, "zh") != i18n_t(key, "en")


def test_sessions_patch_archived_outranks_the_backend_lock_preflight(monkeypatch, tmp_path):
    """Archive is TERMINAL, so it must win over the transient backend lock.

    ``archive_session`` cannot cancel an in-flight chat turn inside its transaction
    (the turn lives in the controller process), so the DELETE route commits the
    archive first and cancels best-effort afterwards. A stale cross-backend PATCH
    landing in that window used to hit the controller-consulting preflight first and
    come back ``409 backend_locked`` — a retryable code that masked the terminal
    state, so the client could never recognize ``session_archived`` and converge.

    Pinned two ways: the archived row answers ``session_archived`` AND the controller
    is never consulted at all; the live row keeps its existing ``backend_locked``
    answer, so the ordering change is scoped to archived sessions only.
    """
    from unittest.mock import AsyncMock, patch

    from core.vibe_agents import VibeAgentStore
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import archive_session, create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        live = create_session(conn, scope_id=project["scope_id"], agent_backend="claude", title="Live")["id"]
        archived = create_session(conn, scope_id=project["scope_id"], agent_backend="claude", title="Gone")["id"]
        archive_session(conn, archived)

    store = VibeAgentStore()
    try:
        store.create(name="reviewer", backend="codex")
    finally:
        store.close()

    client = app.test_client()
    headers = csrf_headers(client)
    in_flight = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "in_flight": True}})

    with patch("vibe.internal_client.turn_state", in_flight):
        # Positive control: an ACTIVE session with a live turn still refuses the
        # cross-backend switch with the transient lock, via the controller.
        locked = client.patch(f"/api/sessions/{live}", json={"agent_name": "reviewer"}, headers=headers)
        assert locked.status_code == 409
        assert locked.get_json()["code"] == "backend_locked"
        assert in_flight.await_count == 1

        blocked = client.patch(f"/api/sessions/{archived}", json={"agent_name": "reviewer"}, headers=headers)

    assert blocked.status_code == 409
    body = blocked.get_json()
    assert body["code"] == "session_archived"
    assert body["error"]["code"] == "session_archived"
    # Short-circuited BEFORE the controller: no extra turn-state call was made.
    assert in_flight.await_count == 1


def test_sessions_patch_missing_session_is_still_404(monkeypatch, tmp_path):
    """The archived short-circuit must not swallow the not-found case.

    ``is_session_archived`` is "exists AND archived", so an unknown id falls through
    to the 404 the route always returned.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    client = app.test_client()
    missing = client.patch("/api/sessions/ses_nope", json={"title": "x"}, headers=csrf_headers(client))
    assert missing.status_code == 404
    # And an empty patch is still a 400 (validated before any row read).
    empty = client.patch("/api/sessions/ses_nope", json={}, headers=csrf_headers(client))
    assert empty.status_code == 400


def test_sessions_patch_archived_conflict_survives_ui_error_parse(monkeypatch, tmp_path):
    """Apply the Web UI parser's own precedence rule to the real 409 body.

    ``selectApiErrorFields`` (ApiContext.tsx) is: take ``error`` if present, else a
    top-level ``{code, message}``; a STRING ``error`` *is* the code. Replicated here
    — kept to those three lines, and mirrored by ui/src/context/ApiErrorParse.test.ts
    which runs the real function — so a route that regresses to the flat shape fails
    on the server side too, instead of shipping raw English to every locale.
    """
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import archive_session, create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        sid = create_session(conn, scope_id=project["scope_id"], agent_backend="claude", title="Before")["id"]
        archive_session(conn, sid)

    client = app.test_client()

    # Both mutation kinds a stale tab can attempt: a rename, and an agent re-route.
    for payload in ({"title": "Nope"}, {"agent_name": "codex"}):
        res = client.patch(f"/api/sessions/{sid}", json=payload, headers=csrf_headers(client))
        assert res.status_code == 409, payload
        assert _ui_error_code(res.get_json()) == "session_archived", payload


def test_sessions_fork_on_archived_source_survives_ui_error_parse(monkeypatch, tmp_path):
    """Fork refuses an archived source — and must say so in a way the Web UI can read.

    ``askInNewSession`` (Quote -> "Ask in a new session") goes through the shared JSON
    helpers, so its 409 reaches ``handleApiError`` and is the *first* rejected action a
    stale tab can take. With the flat body this route used to return, the parser took
    the human sentence as the code, ``archivedConflictSessionId`` returned null, the
    ``onSessionArchived`` subscription never fired, and the chat stayed fully writable
    after a permanent refusal.

    Refusal itself is unchanged (archive is terminal, fork stays forbidden) — only the
    body shape is.
    """
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import archive_session, create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        sid = create_session(conn, scope_id=project["scope_id"], agent_backend="claude", title="Source")["id"]
        archive_session(conn, sid)

    client = app.test_client()
    res = client.post(f"/api/sessions/{sid}/fork", json={}, headers=csrf_headers(client))

    assert res.status_code == 409
    body = res.get_json()
    assert _ui_error_code(body) == "session_archived"
    # The nested object is what the parser reads; a bare string there is the defect.
    assert isinstance(body["error"], dict)
    # Flat top-level code/message stay for the CLI and any direct consumer.
    assert body["code"] == "session_archived"
    assert isinstance(body.get("message"), str) and body["message"]


def test_doctor_post_runs_fast_diagnostics_by_default(monkeypatch):
    from vibe import cli

    calls = []

    monkeypatch.setattr(
        cli,
        "_doctor",
        lambda *, deep=True: calls.append(deep)
        or {"mode": "fast", "groups": [], "summary": {"pass": 0, "warn": 0, "fail": 0}, "ok": True},
    )

    client = app.test_client()
    response = client.post("/api/doctor", json={}, headers=csrf_headers(client))

    assert response.status_code == 200
    assert response.get_json()["mode"] == "fast"
    assert calls == [False]


def test_doctor_post_can_run_deep_diagnostics(monkeypatch):
    from vibe import cli

    calls = []

    monkeypatch.setattr(
        cli,
        "_doctor",
        lambda *, deep=True: calls.append(deep)
        or {"mode": "deep", "groups": [], "summary": {"pass": 0, "warn": 0, "fail": 0}, "ok": True},
    )

    client = app.test_client()
    response = client.post("/api/doctor", json={"deep": True}, headers=csrf_headers(client))

    assert response.status_code == 200
    assert response.get_json()["mode"] == "deep"
    assert calls == [True]


def test_route_path_to_fastapi_converts_named_path_converter():
    assert route_path_to_fastapi("/files/<path:file_path>") == "/files/{file_path:path}"


def test_opencode_model_delete_route_captures_slashes():
    routes = [getattr(route, "path", "") for route in app.routes]

    assert (
        "/api/backend/opencode/provider/{provider_id}/models/{model_id:path}"
        in routes
    )


def test_compat_app_matches_named_path_converter():
    compat_app = CompatApp()

    @compat_app.route("/files/<path:file_path>")
    def get_file(file_path):
        return {"file_path": file_path}

    response = compat_app.test_client().get("/files/nested/example.txt")

    assert response.status_code == 200
    assert response.get_json() == {"file_path": "nested/example.txt"}


def test_normalize_response_supports_body_headers_tuple():
    response = normalize_response(("ok", {"X-Test": "yes"}))

    assert response.status_code == 200
    assert response.headers["X-Test"] == "yes"
    assert response.body == b"ok"


def test_harness_routes_page_filter_and_return_counts(monkeypatch, tmp_path):
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(5):
            store.upsert_scheduled_task(
                {
                    "id": f"task-{index}",
                    "name": f"Task {index}",
                    "prompt": "run it",
                    "schedule_type": "cron",
                    "cron": "0 * * * *",
                    "enabled": index < 3,
                    "created_at": f"2026-06-04T00:0{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:0{index}:00+00:00",
                }
            )
        for index in range(6):
            store.upsert_watch(
                {
                    "id": f"watch-{index}",
                    "name": f"Deploy watch {index}",
                    "shell_command": f"tail deploy-{index}.log",
                    "enabled": index == 0,
                    "created_at": f"2026-06-04T00:1{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:1{index}:00+00:00",
                }
            )
        for index, status in enumerate(["pending", "processing", "completed", "failed"]):
            store.enqueue_run(
                {
                    "id": f"run-{index}",
                    "request_type": "watch",
                    "status": status,
                    "message": "deploy status",
                    "created_at": f"2026-06-04T00:2{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:2{index}:00+00:00",
                }
            )
    finally:
        store.close()

    client = app.test_client()
    legacy_tasks = client.get("/api/harness/tasks").get_json()
    legacy_watches = client.get("/api/harness/watches").get_json()
    tasks = client.get("/api/harness/tasks?status=waiting&page=1&limit=2").get_json()
    watches = client.get("/api/harness/watches?status=paused&query=deploy&page=1&limit=2").get_json()
    runs = client.get("/api/harness/runs?page=1&limit=2").get_json()
    counts = client.get("/api/harness/counts").get_json()

    assert len(legacy_tasks["tasks"]) == 5
    assert legacy_tasks["has_more"] is False
    assert len(legacy_watches["watches"]) == 6
    assert legacy_watches["has_more"] is False
    assert [item["id"] for item in tasks["tasks"]] == ["task-2", "task-1"]
    assert tasks["counts"] == {"total": 5, "running": 0, "waiting": 3, "paused": 2, "finished": 0}
    assert tasks["total"] == 3
    assert tasks["has_more"] is True
    assert [item["id"] for item in watches["watches"]] == ["watch-5", "watch-4"]
    assert watches["counts"] == {"total": 6, "running": 0, "waiting": 1, "paused": 5, "finished": 0}
    assert watches["total"] == 5
    assert watches["has_more"] is True
    # The default view spans two states, so its total is a sum rather than a
    # count key — the one place a wrong "total" would show a number the list
    # cannot produce.
    active = client.get("/api/harness/watches?status=active&page=1&limit=10").get_json()
    assert active["total"] == 1
    assert len(active["watches"]) == 1
    # The frozen row contract (plan §3) reaches the client over HTTP, not just
    # out of the store.
    assert active["watches"][0]["lifecycle_state"] == "waiting"
    assert active["watches"][0]["lifecycle_detail"] is None
    assert active["watches"][0]["process_alive"] is None
    assert tasks["tasks"][0]["next_run_at"]
    assert [item["id"] for item in runs["runs"]] == ["run-3", "run-2"]
    assert runs["total"] == 4
    # The type facet, so the selector can offer a type the UI has no name for.
    # Asserted on both endpoints because the Runs tab loads through either one
    # and a facet present on only one of them is a selector that comes and goes.
    assert runs["run_types"] == ["watch"]
    bootstrap_runs = client.get("/api/harness/bootstrap?tab=runs&page=1&limit=2").get_json()
    assert bootstrap_runs["page"]["run_types"] == runs["run_types"]
    assert runs["counts"]["queued"] == 1
    assert runs["counts"]["running"] == 1
    assert runs["counts"]["succeeded"] == 1
    assert runs["counts"]["failed"] == 1
    assert counts["tasks"]["total"] == 5
    assert counts["watches"]["paused"] == 5
    assert counts["runs"]["all"] == 4


def test_harness_task_resume_rejects_orphaned_owner_without_target(
    monkeypatch, tmp_path
):
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = SQLiteBackgroundTaskStore()
    try:
        store.upsert_scheduled_task(
            {
                "id": "orphaned-task",
                "name": "Orphaned task",
                "prompt": "run it",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": False,
                "created_at": "2026-08-11T00:00:00+00:00",
                "updated_at": "2026-08-11T00:00:00+00:00",
                "metadata": {
                    "orphaned_task_owner": {
                        "reason_code": "task_owner_session_unavailable",
                        "owner_session_id": "ses-removed",
                    }
                },
            }
        )
    finally:
        store.close()

    client = app.test_client()
    response = client.patch(
        "/api/harness/tasks/orphaned-task",
        json={"enabled": True},
        headers=csrf_headers(client),
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == "task_owner_session_unavailable"
    assert body["error"]["code"] == "task_owner_session_unavailable"
    assert "Create a replacement Task" in body["hint"]
    assert body["details"] == {
        "task_id": "orphaned-task",
        "owner_session_id": "ses-removed",
    }
    store = SQLiteBackgroundTaskStore()
    try:
        saved = store.get_scheduled_task("orphaned-task")
    finally:
        store.close()
    assert saved is not None
    assert saved["enabled"] is False
    assert saved["resume_blocked"] == {
        "code": "task_owner_session_unavailable",
        "owner_session_id": "ses-removed",
    }


def test_harness_task_resume_rejects_retired_one_shot(monkeypatch, tmp_path):
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = SQLiteBackgroundTaskStore()
    try:
        store.upsert_scheduled_task(
            {
                "id": "retired-task",
                "name": "Retired task",
                "prompt": "run it",
                "schedule_type": "at",
                "run_at": "2026-08-11T00:00:00+00:00",
                "timezone": "UTC",
                "enabled": False,
                "retired_at": "2026-08-11T00:00:01+00:00",
                "retirement_reason": "schedule_missed",
                "created_at": "2026-08-11T00:00:00+00:00",
                "updated_at": "2026-08-11T00:00:01+00:00",
            }
        )
    finally:
        store.close()

    client = app.test_client()
    response = client.patch(
        "/api/harness/tasks/retired-task",
        json={"enabled": True},
        headers=csrf_headers(client),
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == "task_schedule_retired"
    assert body["details"] == {"task_id": "retired-task"}
    store = SQLiteBackgroundTaskStore()
    try:
        saved = store.get_scheduled_task("retired-task")
    finally:
        store.close()
    assert saved is not None
    assert saved["enabled"] is False
    assert saved["retirement_reason"] == "schedule_missed"


def test_harness_bootstrap_returns_counts_and_selected_page(monkeypatch, tmp_path):
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(4):
            store.upsert_scheduled_task(
                {
                    "id": f"task-{index}",
                    "name": f"Task {index}",
                    "prompt": "run it",
                    "schedule_type": "cron",
                    "cron": "0 * * * *",
                    "enabled": index < 2,
                    "created_at": f"2026-06-04T00:0{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:0{index}:00+00:00",
                }
            )
        for index, status in enumerate(["pending", "completed"]):
            store.enqueue_run(
                {
                    "id": f"run-{index}",
                    "request_type": "task",
                    "status": status,
                    "message": "run status",
                    "created_at": f"2026-06-04T00:2{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:2{index}:00+00:00",
                }
            )
    finally:
        store.close()

    client = app.test_client()
    response = client.get("/api/harness/bootstrap?tab=tasks&status=waiting&page=1&limit=1")

    assert response.status_code == 200
    assert response.headers["X-Vibe-Request-Ms"]
    payload = response.get_json()
    assert payload["counts"]["tasks"] == {
        "total": 4,
        "running": 0,
        "waiting": 2,
        "paused": 2,
        "finished": 0,
    }
    assert payload["counts"]["runs"]["all"] == 2
    assert payload["page"]["tasks"][0]["id"] == "task-1"
    assert payload["page"]["total"] == 2
    assert payload["page"]["has_more"] is True


def test_workbench_projects_bootstrap_returns_requested_session_pages(monkeypatch, tmp_path):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_a_dir = tmp_path / "project-a"
    project_b_dir = tmp_path / "project-b"
    project_a_dir.mkdir()
    project_b_dir.mkdir()
    with engine.begin() as conn:
        project_a = create_project(conn, str(project_a_dir), display_name="Project A")
        project_b = create_project(conn, str(project_b_dir), display_name="Project B")
        create_session(conn, scope_id=project_a["scope_id"], agent_backend="", title="First")
        create_session(conn, scope_id=project_a["scope_id"], agent_backend="", title="Second")
        create_session(conn, scope_id=project_b["scope_id"], agent_backend="", title="Other")

    client = app.test_client()
    response = client.get(f"/api/workbench/projects-bootstrap?project_id={project_a['id']}&limit=1")

    assert response.status_code == 200
    assert response.headers["Server-Timing"].startswith("app;dur=")
    payload = response.get_json()
    assert {project["id"] for project in payload["projects"]} == {project_a["id"], project_b["id"]}
    assert set(payload["sessions"]) == {project_a["id"]}
    page = payload["sessions"][project_a["id"]]
    assert len(page["sessions"]) == 1
    assert page["next_before_id"] == page["sessions"][0]["id"]


def test_project_patch_rejects_stale_agent_route_after_archive(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from core.services import settings as settings_service
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.models import scope_settings
    from storage.projects_service import create_project, update_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(
        settings_service,
        "load_config_or_default",
        lambda: SimpleNamespace(language="zh"),
    )
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    folder = tmp_path / "project"
    folder.mkdir()
    store = VibeAgentStore()
    try:
        store.create(name="pm", backend="claude")
        store.create(name="zz-fallback", backend="claude")
        with engine.begin() as conn:
            project = create_project(conn, str(folder), display_name="Project")
            update_project(conn, project["id"], agent_name="pm")
        archived = store.archive("pm")
        assert archived is not None

        client = app.test_client()
        response = client.patch(
            f"/api/projects/{project['id']}",
            json={"agent_name": "pm"},
            headers=csrf_headers(client),
        )

        assert response.status_code == 400
        assert response.get_json() == {
            "ok": False,
            "code": "project_agent_unavailable",
            "message": "Agent `pm` 无法用于此项目。",
            "error": {
                "code": "project_agent_unavailable",
                "message": "Agent `pm` 无法用于此项目。",
            },
            "hint": "请选择一个已启用的 Agent 后重新保存项目设置。",
            "details": {"agent_name": "pm"},
        }
        with engine.connect() as conn:
            stored_name = conn.execute(
                select(scope_settings.c.agent_name).where(
                    scope_settings.c.scope_id == project["scope_id"]
                )
            ).scalar_one()
        assert stored_name == archived.archived_name
    finally:
        store.close()
        engine.dispose()


def test_project_patch_forwards_stable_agent_ids_and_localizes_conflicts(monkeypatch):
    from core.services import settings as settings_service
    from storage import projects_service

    captured: dict[str, object] = {}

    def stale(_conn, project_id, **kwargs):
        captured.update({"project_id": project_id, **kwargs})
        raise projects_service.StaleProjectAgentBindingError(
            project_id=project_id,
            expected_agent_id="agent-original",
            current_agent_id="agent-replacement",
        )

    monkeypatch.setattr(projects_service, "update_project", stale)
    monkeypatch.setattr(
        settings_service,
        "load_config_or_default",
        lambda: SimpleNamespace(language="zh"),
    )

    client = app.test_client()
    response = client.patch(
        "/api/projects/proj-stale",
        json={
            "agent_id": "agent-original",
            "expected_agent_id": "agent-original",
            "agent_name": "pm",
            "model": "updated-model",
        },
        headers=csrf_headers(client),
    )

    authorization_context = captured.pop("authorization_context")
    assert authorization_context.is_instance_owner
    assert captured == {
        "project_id": "proj-stale",
        "display_name": None,
        "folder_path": None,
        "agent_id": "agent-original",
        "expected_agent_id": "agent-original",
        "agent_name": "pm",
        "model": "updated-model",
    }
    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "code": "project_agent_conflict",
        "message": "项目设置打开后，该项目的 Agent 已发生变化。",
        "error": {
            "code": "project_agent_conflict",
            "message": "项目设置打开后，该项目的 Agent 已发生变化。",
        },
        "hint": "请重新加载项目设置后再次修改。",
        "details": {
            "project_id": "proj-stale",
            "expected_agent_id": "agent-original",
            "current_agent_id": "agent-replacement",
        },
    }


def test_config_get_on_fresh_install_returns_default_needing_setup(monkeypatch, tmp_path):
    # Fresh install edge: no config file exists yet, but the setup wizard
    # (and the reused provider-config modal that calls getConfig()) must be
    # able to load. GET /api/config must serve an in-memory default with
    # needs_setup=True instead of propagating FileNotFoundError as a 500 —
    # and must not create the file (the read stays a read; save_config owns
    # the first write).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config import paths

    assert not paths.get_config_path().exists()

    client = app.test_client()
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.get_json()
    assert data["mode"] == "self_host"
    assert data["setup_completed"] is False
    assert data["setup_state"]["needs_setup"] is True
    assert not paths.get_config_path().exists(), "GET must not persist a config file"


def test_first_config_post_starts_remote_access_monitoring(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "_UI_RUNTIME_ACTIVE", True)
    monitoring = []
    monkeypatch.setattr(remote_access, "start_runtime_monitoring", lambda config=None: monitoring.append(config))
    client = app.test_client()

    response = client.post(
        "/api/config",
        json=_full_config_payload(),
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert len(monitoring) == 1
    assert monitoring[0].version == "v2"


def test_config_post_policy_only_save_does_not_start_a_stopped_tunnel(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config.v2_config import V2Config
    from vibe import api

    payload = _full_config_payload()
    payload["remote_access"] = {
        "provider": "vibe_cloud",
        "vibe_cloud": {"enabled": True, "auto_recovery": True},
    }
    api.save_config(payload)
    monkeypatch.setattr(
        remote_access,
        "reconcile",
        lambda: (_ for _ in ()).throw(
            AssertionError("policy-only save must not start the Connector")
        ),
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"auto_recovery": False}}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert V2Config.load().remote_access.vibe_cloud.auto_recovery is False
    assert "remote_access_runtime" not in response.get_json()


def test_config_post_rejects_connector_controls_that_require_make_before_break(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config.v2_config import V2Config
    from vibe import api

    api.save_config(_full_config_payload())
    monkeypatch.setattr(
        remote_access,
        "reconcile",
        lambda: (_ for _ in ()).throw(
            AssertionError("rejected Connector controls must not reconcile")
        ),
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"transport_protocol": "http2"}}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert "/api/remote-access/settings" in response.get_json()["error"]
    assert V2Config.load().remote_access.vibe_cloud.transport_protocol == "auto"


def test_config_routes_redact_platform_and_gateway_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["slack"] = {
        **payload["slack"],
        "bot_token": "xoxb-route-secret",
        "app_token": "xapp-route-secret",
        "signing_secret": "slack-route-secret",
    }
    payload["telegram"] = {
        "bot_token": "123456:telegram-route-secret",
        "webhook_secret_token": "telegram-webhook-route-secret",
        "require_mention": True,
        "forum_auto_topic": True,
        "use_webhook": True,
    }
    payload["lark"] = {
        "app_id": "cli_route_lark_id",
        "app_secret": "lark-route-secret",
        "require_mention": False,
        "domain": "feishu",
    }
    payload["wechat"] = {
        "bot_token": "wechat-route-secret",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "require_mention": False,
    }
    payload["gateway"] = {
        "relay_url": "https://relay.example",
        "workspace_token": "workspace-route-secret",
        "client_id": "client-id",
        "client_secret": "client-route-secret",
    }

    client = app.test_client()
    response = client.post("/api/config", json=payload, headers=csrf_headers(client))

    assert response.status_code == 200
    saved = response.get_json()
    fetched = client.get("/api/config").get_json()
    for data in (saved, fetched):
        assert data["slack"]["has_bot_token"] is True
        assert data["slack"]["has_app_token"] is True
        assert data["slack"]["has_signing_secret"] is True
        assert "bot_token" not in data["slack"]
        assert "app_token" not in data["slack"]
        assert "signing_secret" not in data["slack"]
        assert data["discord"]["has_bot_token"] is True
        assert "bot_token" not in data["discord"]
        assert data["telegram"]["has_bot_token"] is True
        assert data["telegram"]["has_webhook_secret_token"] is True
        assert "bot_token" not in data["telegram"]
        assert "webhook_secret_token" not in data["telegram"]
        assert data["lark"]["has_app_secret"] is True
        assert "app_secret" not in data["lark"]
        assert data["wechat"]["has_bot_token"] is True
        assert "bot_token" not in data["wechat"]
        assert data["gateway"]["has_workspace_token"] is True
        assert data["gateway"]["has_client_secret"] is True
        assert "workspace_token" not in data["gateway"]
        assert "client_secret" not in data["gateway"]


def test_config_post_hot_reconciles_platform_enablement(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    reconcile_calls = []
    restart_calls = []

    async def _reconcile_platforms():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "added": ["slack"]}}

    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(ui_server, "_schedule_service_restart_for_config_fallback", lambda: restart_calls.append(True) or {"ok": True})

    next_payload = {
        **payload,
        "platforms": {"enabled": ["discord", "slack"], "primary": "discord"},
        "slack": {"bot_token": "xoxb-hot-token", "app_token": "xapp-hot-token"},
    }
    client = app.test_client()
    response = client.post("/api/config", json=next_payload, headers=csrf_headers(client))

    assert response.status_code == 200
    data = response.get_json()
    assert data["platform_runtime"]["hot_reconciled"] is True
    assert reconcile_calls == [True]
    assert restart_calls == []


def test_config_post_hot_reconciles_platform_runtime_credential_change(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    reconcile_calls = []

    async def _reconcile_platforms():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "rebuilt": ["discord"]}}

    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"discord": {"bot_token": "discord-new-token-12345"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["platform_runtime"]["body"]["rebuilt"] == ["discord"]
    assert reconcile_calls == [True]


def test_platform_runtime_fields_changed_detects_primary_only_change():
    from config.v2_config import V2Config

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord", "slack"], "primary": "discord"}
    payload["slack"] = {"bot_token": "xoxb-hot-token", "app_token": "xapp-hot-token"}
    previous = V2Config.from_payload(payload)
    current = V2Config.from_payload(
        {
            **payload,
            "platform": "slack",
            "platforms": {"enabled": ["discord", "slack"], "primary": "slack"},
        }
    )

    assert (
        ui_server._platform_runtime_fields_changed(
            previous,
            current,
            {"platforms": {"enabled": ["discord", "slack"], "primary": "slack"}},
        )
        is True
    )


def test_changed_agent_backend_runtimes_uses_backend_runtime_projection():
    from config.v2_config import V2Config

    payload = _full_config_payload()
    payload["agents"]["codex"]["enabled"] = False
    previous = V2Config.from_payload(payload)

    changed_payload = json.loads(json.dumps(payload))
    changed_payload["agents"]["codex"] = {
        **changed_payload["agents"]["codex"],
        "enabled": True,
        "cli_path": "/opt/codex",
    }
    changed_payload["agents"]["opencode"] = {
        **changed_payload["agents"]["opencode"],
        "cli_path": "/opt/opencode",
    }
    changed_payload["agents"]["claude"] = {
        **changed_payload["agents"]["claude"],
        "cli_path": "/opt/claude",
    }
    current = V2Config.from_payload(changed_payload)

    assert ui_server._changed_agent_backend_runtimes(
        previous,
        current,
        {"agents": changed_payload["agents"]},
    ) == ["opencode", "claude", "codex"]
    assert ui_server._changed_agent_backend_runtimes(previous, current, {"show_duration": False}) == []
    assert ui_server._changed_agent_backend_runtimes(None, current, {"agents": changed_payload["agents"]}) == []


def test_config_post_hot_reconciles_first_setup_codex_enablement(monkeypatch, tmp_path):
    """Scenario: AUTH-SETUP-902."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["agents"]["codex"]["enabled"] = False
    api.save_config(payload)

    reconcile_calls = []

    async def _reconcile_agent_backends(backends):
        reconcile_calls.append(backends)
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "backends": backends,
                "states": {"codex": "restarted"},
            },
        }

    monkeypatch.setattr(internal_client, "reconcile_agent_backends", _reconcile_agent_backends)

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={
            "agents": {
                "codex": {
                    "enabled": True,
                    "cli_path": "/opt/codex",
                }
            }
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["agents"]["codex"]["enabled"] is True
    assert data["agent_backend_runtime"] == {
        "ok": True,
        "hot_reconciled": True,
        "backends": ["codex"],
        "body": {
            "ok": True,
            "backends": ["codex"],
            "states": {"codex": "restarted"},
        },
    }
    assert reconcile_calls == [["codex"]]


def test_config_post_defers_backend_reconcile_until_next_start_when_service_is_stopped(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client
    from vibe import runtime

    payload = _full_config_payload()
    payload["agents"]["codex"]["enabled"] = False
    api.save_config(payload)

    async def _reconcile_agent_backends(_backends):
        raise internal_client.InternalServerUnavailable("missing socket")

    restart_calls = []
    monkeypatch.setattr(internal_client, "reconcile_agent_backends", _reconcile_agent_backends)
    monkeypatch.setattr(runtime, "service_process_running", lambda: False)
    monkeypatch.setattr(
        ui_server,
        "_schedule_service_restart_for_config_fallback",
        lambda: restart_calls.append(True) or {"ok": True},
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"agents": {"codex": {"enabled": True}}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    runtime_result = response.get_json()["agent_backend_runtime"]
    assert runtime_result["hot_reconciled"] is False
    assert runtime_result["apply_on_next_start"] is True
    assert restart_calls == []


def test_config_post_restarts_running_service_when_backend_reconcile_is_unavailable(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client
    from vibe import runtime

    payload = _full_config_payload()
    payload["agents"]["opencode"]["enabled"] = False
    api.save_config(payload)

    async def _reconcile_agent_backends(_backends):
        raise internal_client.InternalServerUnavailable("missing socket")

    restart_calls = []
    monkeypatch.setattr(internal_client, "reconcile_agent_backends", _reconcile_agent_backends)
    monkeypatch.setattr(runtime, "service_process_running", lambda: True)
    monkeypatch.setattr(
        ui_server,
        "_schedule_service_restart_for_config_fallback",
        lambda: restart_calls.append(True)
        or {"ok": True, "restart": {"job_id": "job-backend-fallback"}},
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"agents": {"opencode": {"enabled": True}}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    runtime_result = response.get_json()["agent_backend_runtime"]
    assert runtime_result["hot_reconciled"] is False
    assert runtime_result["restart_scheduled"] is True
    assert runtime_result["restart"]["job_id"] == "job-backend-fallback"
    assert restart_calls == [True]


def test_config_post_non_platform_change_does_not_reconcile_platforms(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    async def _reconcile_platforms():
        raise AssertionError("platform reconcile should not run")

    async def _reconcile_agent_backends(_backends):
        raise AssertionError("Agent backend reconcile should not run")

    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(internal_client, "reconcile_agent_backends", _reconcile_agent_backends)

    client = app.test_client()
    response = client.post("/api/config", json={"show_duration": False}, headers=csrf_headers(client))

    assert response.status_code == 200
    assert "platform_runtime" not in response.get_json()
    assert "agent_backend_runtime" not in response.get_json()


def test_config_post_schedules_service_restart_when_hot_reconcile_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    async def _reconcile_platforms():
        raise internal_client.InternalServerUnavailable("missing socket")

    restart_calls = []
    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(
        ui_server,
        "_schedule_service_restart_for_config_fallback",
        lambda: restart_calls.append(True) or {"ok": True, "restart": {"job_id": "job-hot-fallback"}},
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"platforms": {"enabled": ["discord", "slack"], "primary": "discord"}, "slack": {"bot_token": "xoxb-hot-token", "app_token": "xapp-hot-token"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    runtime = response.get_json()["platform_runtime"]
    assert runtime["hot_reconciled"] is False
    assert runtime["restart_scheduled"] is True
    assert restart_calls == [True]


def test_config_post_schedules_service_restart_when_hot_reconcile_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    async def _reconcile_platforms():
        return {
            "status_code": 500,
            "body": {"ok": False, "error": "IM thread for discord did not stop within timeout"},
        }

    restart_calls = []
    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(
        ui_server,
        "_schedule_service_restart_for_config_fallback",
        lambda: restart_calls.append(True) or {"ok": True, "restart": {"job_id": "job-hot-failure"}},
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"discord": {"bot_token": "discord-new-token-12345"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    runtime = response.get_json()["platform_runtime"]
    assert runtime["hot_reconciled"] is False
    assert runtime["restart_scheduled"] is True
    assert runtime["body"]["error"] == "IM thread for discord did not stop within timeout"
    assert restart_calls == [True]


def test_config_restart_fallback_marks_pending_restart_when_restart_in_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import restart_supervisor
    from vibe import runtime

    runtime.get_restart_status_path().parent.mkdir(parents=True, exist_ok=True)
    restart_status = {
        "ok": None,
        "state": "running",
        "job_id": "job-in-flight",
        "supervisor_pid": 4242,
    }
    runtime.write_json(runtime.get_restart_status_path(), restart_status)
    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: True)
    monkeypatch.setattr(
        restart_supervisor,
        "schedule_restart",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not overlap restart jobs")),
    )

    result = ui_server._schedule_service_restart_for_config_fallback()

    assert result["ok"] is True
    assert result["code"] == "restart_pending_after_in_progress"
    assert result["restart"] == restart_status
    pending = runtime.read_json(restart_supervisor._pending_restart_path())
    assert pending["restart_job_id"] == "job-in-flight"
    assert pending["trigger"] == "web-ui-config-pending"
    assert pending["scope"] == "service"


def test_config_restart_fallback_schedules_when_in_flight_finishes_after_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import restart_supervisor
    from vibe import runtime

    runtime.get_restart_status_path().parent.mkdir(parents=True, exist_ok=True)
    restart_status = {
        "ok": None,
        "state": "running",
        "job_id": "job-in-flight",
        "supervisor_pid": 4242,
    }
    runtime.write_json(runtime.get_restart_status_path(), restart_status)
    in_flight_results = iter([True, False])
    scheduled: list[dict] = []

    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: next(in_flight_results))
    monkeypatch.setattr(restart_supervisor, "schedule_restart", lambda **kwargs: scheduled.append(kwargs) or {"job_id": "followup"})
    monkeypatch.setattr(runtime, "read_status", lambda: {"service_pid": 11, "ui_pid": 22})

    result = ui_server._schedule_service_restart_for_config_fallback()

    assert result["ok"] is True
    assert result["code"] == "restart_scheduled_after_in_flight_finished"
    assert result["restart"] == {"job_id": "followup"}
    assert scheduled == [{"delay_seconds": 0.0, "trigger": "web-ui-config", "scope": "service"}]
    assert runtime.read_json(restart_supervisor._pending_restart_path()) is None


def test_static_ui_assets_use_cache_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    (ui_dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (ui_dist / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    (assets_dir / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    client = app.test_client()
    asset_response = client.get("/assets/app-abc123.js")
    manifest_response = client.get("/manifest.webmanifest")
    index_response = client.get("/")
    spa_response = client.get("/workbench/session-1")

    assert asset_response.status_code == 200
    assert asset_response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert manifest_response.status_code == 200
    assert manifest_response.headers["Cache-Control"] == "public, max-age=3600"
    assert index_response.status_code == 200
    assert index_response.headers["Cache-Control"] == "no-store, private"
    assert spa_response.status_code == 200
    assert spa_response.headers["Cache-Control"] == "no-store, private"


def test_static_ui_asset_omits_csrf_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "index-abc123.js").write_text("console.log('ok')", encoding="utf-8")

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    response = app.test_client().get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert not any(header.startswith("vibe_csrf_token=") for header in response.headers.getlist("Set-Cookie"))


def test_static_ui_documents_keep_csrf_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    ui_dist.mkdir(parents=True)
    (ui_dist / "index.html").write_text("<html>app</html>", encoding="utf-8")

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    index_response = app.test_client().get("/")
    spa_response = app.test_client().get("/workbench/session-1")

    assert index_response.status_code == 200
    assert any(header.startswith("vibe_csrf_token=") for header in index_response.headers.getlist("Set-Cookie"))
    assert spa_response.status_code == 200
    assert any(header.startswith("vibe_csrf_token=") for header in spa_response.headers.getlist("Set-Cookie"))


def test_static_ui_asset_gzip_uses_shared_response_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    original = b"console.log('edge cache');\n" * 200
    (assets_dir / "index-abc123.js").write_bytes(original)

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    client = app.test_client()
    identity_response = client.get("/assets/index-abc123.js", headers={"Accept-Encoding": ""})
    assert identity_response.status_code == 200
    assert identity_response.content == original
    assert "Content-Encoding" not in identity_response.headers
    assert "Accept-Encoding" in identity_response.headers["Vary"]
    assert identity_response.headers["Accept-Ranges"] == "bytes"
    assert identity_response.headers["ETag"]
    assert identity_response.headers["Last-Modified"]

    gzip_disabled_response = client.get("/assets/index-abc123.js", headers={"Accept-Encoding": "br, gzip;q=0"})
    assert gzip_disabled_response.status_code == 200
    assert gzip_disabled_response.content == original
    assert "Content-Encoding" not in gzip_disabled_response.headers
    assert "Accept-Encoding" in gzip_disabled_response.headers["Vary"]

    with client._client.stream(
        "GET",
        "http://127.0.0.1/assets/index-abc123.js",
        headers={
            "Accept-Encoding": "gzip",
            TEST_REMOTE_ADDR_HEADER: "127.0.0.1",
        },
    ) as gzip_response:
        compressed = b"".join(gzip_response.iter_raw())

    assert gzip_response.status_code == 200
    assert gzip_response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in gzip_response.headers["Vary"]
    assert gzip.decompress(compressed) == original


def test_static_ui_asset_range_request_keeps_file_response_semantics(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    original = b"0123456789abcdef"
    (assets_dir / "index-abc123.js").write_bytes(original)

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    response = app.test_client().get(
        "/assets/index-abc123.js",
        headers={
            "Accept-Encoding": "gzip",
            "Range": "bytes=0-9",
        },
    )

    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["Content-Range"] == f"bytes 0-9/{len(original)}"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "Content-Encoding" not in response.headers


def test_static_ui_asset_gzip_skips_small_and_binary_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    small_js = b"console.log('small')"
    png = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 2048)
    (assets_dir / "small-abc123.js").write_bytes(small_js)
    (assets_dir / "logo-abc123.png").write_bytes(png)

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    client = app.test_client()
    small_response = client.get("/assets/small-abc123.js", headers={"Accept-Encoding": "gzip"})
    binary_response = client.get("/assets/logo-abc123.png", headers={"Accept-Encoding": "gzip"})

    assert small_response.status_code == 200
    assert small_response.content == small_js
    assert "Content-Encoding" not in small_response.headers
    assert binary_response.status_code == 200
    assert binary_response.content == png
    assert "Content-Encoding" not in binary_response.headers
    assert "Vary" not in binary_response.headers


def test_json_api_gzip_uses_shared_response_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))

    client = app.test_client()
    identity_response = client.get("/api/config", headers={"Accept-Encoding": ""})
    original = identity_response.content

    assert identity_response.status_code == 200
    assert identity_response.is_json
    assert len(original) >= ui_server._SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES
    assert "Content-Encoding" not in identity_response.headers
    assert "Accept-Encoding" in identity_response.headers["Vary"]

    gzip_disabled_response = client.get("/api/config", headers={"Accept-Encoding": "br, gzip;q=0"})
    assert gzip_disabled_response.status_code == 200
    assert gzip_disabled_response.content == original
    assert "Content-Encoding" not in gzip_disabled_response.headers
    assert "Accept-Encoding" in gzip_disabled_response.headers["Vary"]

    gzip_client = app.test_client()
    gzip_response, compressed = _raw_client_get(
        gzip_client,
        "/api/config",
        headers={"Accept-Encoding": "gzip"},
    )

    assert gzip_response.status_code == 200
    assert gzip_response.headers["Content-Encoding"] == "gzip"
    assert gzip_response.headers["Content-Length"] == str(len(compressed))
    assert "Accept-Encoding" in gzip_response.headers["Vary"]
    assert gzip.decompress(compressed) == original
    assert any(header.startswith("vibe_csrf_token=") for header in gzip_response.headers.get_list("Set-Cookie"))


def test_json_api_gzip_skips_small_responses(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))

    response = app.test_client().get("/api/csrf-token", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.is_json
    assert len(response.content) < ui_server._SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES
    assert "Content-Encoding" not in response.headers


def test_json_api_gzip_skips_sse_streaming_response():
    from fastapi.responses import StreamingResponse

    async def generate():
        yield b": stream connected\n\n"

    response = StreamingResponse(generate(), media_type="text/event-stream")

    with app.test_request_context("/api/events", headers={"Accept-Encoding": "gzip"}):
        compressed_response = ui_server._compress_materialized_api_response(response)

    assert compressed_response is response
    assert "Content-Encoding" not in response.headers
    assert response.media_type == "text/event-stream"

    body = b"event: message\ndata: {}\n\n" * 100
    materialized = ui_server.Response(content=body, mimetype="text/event-stream")
    with app.test_request_context("/api/events", headers={"Accept-Encoding": "gzip"}):
        materialized_response = ui_server._compress_materialized_api_response(materialized)

    assert materialized_response is materialized
    assert materialized_response.body == body
    assert "Content-Encoding" not in materialized_response.headers


def test_workbench_events_filter_privileged_events_for_viewers(monkeypatch, tmp_path):
    from storage import projects_service
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import create_session
    from vibe.authorization import AuthorizationContext
    from vibe.sse_broker import broker
    from vibe.ui_compat import g

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            project = projects_service.create_project(conn, str(project_dir))
            session = create_session(conn, scope_id=project["scope_id"], agent_backend="codex")
    finally:
        engine.dispose()

    async def collect_next_live_event() -> str:
        with app.test_request_context("/api/events"):
            g.authorization_context = AuthorizationContext(instance_role="viewer", is_remote=True)
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                initial_chunks = [await iterator.__anext__() for _ in range(3)]
                broker.publish("vaults.updated", {"secret_name": "hidden-secret"})
                broker.publish("authorization.changed", {"project_ids": ["hidden-project"]})
                broker.publish("message.new", {"session_id": session["id"]})
                live_chunks = [
                    await asyncio.wait_for(iterator.__anext__(), timeout=1)
                    for _ in range(2)
                ]
            finally:
                await iterator.aclose()
        chunks = [*initial_chunks, *live_chunks]
        return "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks)

    body = asyncio.run(collect_next_live_event())

    assert "event: authorization.changed" in body
    assert "event: message.new" in body
    assert "hidden-project" not in body
    assert "hidden-secret" not in body


def test_workbench_events_heartbeat_proves_liveness_on_its_own_clock(monkeypatch, tmp_path):
    """A browser must be able to tell a quiet stream from a dead one.

    The keep-alive comment cannot say it -- ``EventSource`` never surfaces a
    comment -- so the stream emits an observable frame on a wall-clock cadence
    that no amount of traffic can suppress.
    """
    from vibe.authorization import AuthorizationContext
    from vibe.sse_broker import broker
    from vibe.ui_compat import g

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    # Only to keep the test quick. The client reads the cadence off the frame
    # rather than assuming the shipped value.
    monkeypatch.setattr(ui_server, "WORKBENCH_EVENT_HEARTBEAT_INTERVAL_S", 0.05)

    def decode(chunk) -> str:
        return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

    async def collect_heartbeats() -> tuple[str, str]:
        with app.test_request_context("/api/events"):
            g.authorization_context = AuthorizationContext(instance_role="viewer", is_remote=True)
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                for _ in range(3):
                    await iterator.__anext__()
                idle = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                # A stream carrying events this viewer may not see is silent
                # from the browser's side while being anything but idle, so a
                # cadence measured from the last delivered frame would leave
                # exactly this subscriber unable to prove its stream alive.
                broker.publish("vaults.updated", {"secret_name": "hidden-secret"})
                busy = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            finally:
                await iterator.aclose()
        return decode(idle), decode(busy)

    idle, busy = asyncio.run(collect_heartbeats())

    assert "event: heartbeat" in idle
    # The cadence rides along so the client sizes its staleness window from
    # whichever side actually sets it.
    assert '"interval_ms":50' in idle
    assert "event: heartbeat" in busy
    assert "hidden-secret" not in busy


def test_workbench_events_allow_show_events_when_show_page_acl_allows(monkeypatch, tmp_path) -> None:
    from vibe.authorization import AuthorizationContext
    from vibe.sse_broker import broker
    from vibe.ui_compat import g
    from storage import resource_access_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="show_page",
            resource_id="show-session-1",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="public",
        )
    engine.dispose()

    async def collect_show_event() -> str:
        with app.test_request_context("/api/events"):
            g.authorization_context = AuthorizationContext(
                instance_role="viewer",
                subject="viewer-1",
                email="viewer@example.com",
                instance_access_source="organization_group",
                organization_id="org-1",
                organization_member_id="member-1",
                organization_role="member",
                is_remote=True,
            )
            monkeypatch.setattr(
                ui_server,
                "_show_page_resource_access_allowed",
                lambda context, session_id: session_id == "show-session-1",
            )
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                for _ in range(3):
                    await iterator.__anext__()
                broker.publish(
                    "show.event",
                    {
                        "session_id": "show-session-1",
                        "payload": {
                            "screenshot": {
                                "path": "/private/host/path.png",
                                "attachmentId": "attachment-1",
                            }
                        },
                    },
                )
                await asyncio.sleep(0)
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            finally:
                await iterator.aclose()
        return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

    body = asyncio.run(collect_show_event())

    assert "event: show.event" in body
    assert "show-session-1" in body
    assert "/private/host/path.png" not in body


def test_local_workbench_events_ignore_legacy_authorization_refresh_deadline():
    from vibe.authorization import AuthorizationContext
    from vibe.sse_broker import broker
    from vibe.ui_compat import g

    async def collect_until_expired() -> list[str | bytes]:
            with app.test_request_context("/api/events"):
                g.authorization_context = AuthorizationContext(instance_role="owner", is_remote=True)
                g.remote_authorization_refresh_at = ui_server.time.time()
                response = await ui_server.workbench_events()
                iterator = response.body_iterator.__aiter__()
                try:
                    chunks = [await iterator.__anext__() for _ in range(3)]
                    next_chunk = asyncio.create_task(iterator.__anext__())
                    await asyncio.sleep(0)
                    broker.publish("authorization.changed", {"project_ids": []})
                    chunks.append(await asyncio.wait_for(next_chunk, timeout=1))
                finally:
                    await iterator.aclose()
            return chunks

    chunks = asyncio.run(collect_until_expired())
    assert len(chunks) == 4
    assert "event: authorization.changed" in chunks[-1]


def test_json_api_gzip_skips_attachments_and_existing_encoding():
    body = b'{"items":[' + (b'"payload",' * 300) + b'"end"]}'
    attachment = ui_server.Response(
        content=body,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=data.json"},
    )
    with app.test_request_context("/api/export", headers={"Accept-Encoding": "gzip"}):
        attachment_response = ui_server._compress_materialized_api_response(attachment)

    assert attachment_response is attachment
    assert attachment_response.body == body
    assert "Content-Encoding" not in attachment_response.headers
    assert "Vary" not in attachment_response.headers

    encoded = ui_server.Response(
        content=body,
        mimetype="application/json",
        headers={"Content-Encoding": "br"},
    )
    with app.test_request_context("/api/precompressed", headers={"Accept-Encoding": "gzip"}):
        encoded_response = ui_server._compress_materialized_api_response(encoded)

    assert encoded_response is encoded
    assert encoded_response.body == body
    assert encoded_response.headers["Content-Encoding"] == "br"


def test_run_maybe_async_offloads_sync_handlers_without_losing_context():
    import asyncio
    import threading
    import time

    loop_thread_id = threading.get_ident()

    def blocking_handler():
        assert threading.get_ident() != loop_thread_id
        time.sleep(0.05)
        return request.path

    async def ticker():
        await asyncio.sleep(0.01)
        return "tick"

    async def exercise():
        return await asyncio.gather(
            run_maybe_async(blocking_handler),
            ticker(),
        )

    compat_app = CompatApp()
    with compat_app.test_request_context("/threadpool-check"):
        result, tick = asyncio.run(exercise())

    assert result == "/threadpool-check"
    assert tick == "tick"


def test_json_payload_parsing_runs_off_the_asgi_loop(monkeypatch):
    parse_threads: list[int] = []
    original_loads = json.loads

    def tracked_loads(payload):
        parse_threads.append(threading.get_ident())
        return original_loads(payload)

    monkeypatch.setattr("vibe.ui_compat.json.loads", tracked_loads)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"attachment":"legacy"}',
            "more_body": False,
        }

    starlette_request = FastAPIRequest(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/legacy-upload",
            "raw_path": b"/legacy-upload",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )

    async def exercise():
        loop_thread = threading.get_ident()
        payload = await _read_json_payload(starlette_request)
        return loop_thread, payload

    loop_thread, payload = asyncio.run(exercise())

    assert payload == {"attachment": "legacy"}
    assert parse_threads
    assert parse_threads[0] != loop_thread


def test_wechat_qr_poll_marks_bind_hint_and_schedules_managed_restart(monkeypatch):
    from vibe import runtime

    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {
                "status": "confirmed",
                "bot_token": "wechat-token",
                "base_url": "https://wechat.example.com",
                "user_id": "wx-user",
            }

    bound_users = []
    restart_calls = []
    persisted = []

    runtime.ensure_config()
    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(ui_server, "_persist_wechat_qr_credentials", lambda result: persisted.append(result.copy()))
    monkeypatch.setattr(
        ui_server,
        "_schedule_wechat_qr_login_restart",
        lambda: restart_calls.append(True) or {"job_id": "restart-1"},
    )
    monkeypatch.setattr(
        "vibe.api.auto_bind_wechat_user",
        lambda user_id: bound_users.append(user_id)
        or {"ok": True, "already_bound": False, "is_admin": True, "pending_bind_menu_hint": True},
    )

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "confirmed"
    assert persisted == [
        {
            "status": "confirmed",
            "bot_token": "wechat-token",
            "base_url": "https://wechat.example.com",
            "user_id": "wx-user",
        }
    ]
    assert bound_users == ["wx-user"]
    assert restart_calls == [True]


def test_wechat_qr_poll_passes_verify_code(monkeypatch):
    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {"status": "need_verifycode", "verify_code": verify_code}

    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session", "verify_code": "1234"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "need_verifycode", "verify_code": "1234"}


def test_persist_wechat_qr_credentials_saves_before_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    payload["wechat"] = {
        "bot_token": "",
        "base_url": "https://old-wechat.example.com",
        "cdn_base_url": "https://cdn.example.com/c2c",
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    api.save_config(payload)

    ui_server._persist_wechat_qr_credentials(
        {
            "status": "confirmed",
            "bot_token": "new-token",
            "base_url": "https://new-wechat.example.com",
            "user_id": "wx-user",
        }
    )

    updated = api.load_config()
    assert updated.wechat is not None
    assert updated.wechat.bot_token == "new-token"
    assert updated.wechat.base_url == "https://new-wechat.example.com"
    assert updated.wechat.cdn_base_url == "https://cdn.example.com/c2c"
    assert updated.wechat.proxy_url == "socks5://127.0.0.1:1080"
    assert updated.platforms.enabled == ["wechat"]
    assert updated.platforms.primary == "wechat"


def test_persist_wechat_qr_credentials_seeds_fresh_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config import paths
    from vibe import api

    assert not paths.get_config_path().exists()

    ui_server._persist_wechat_qr_credentials(
        {
            "status": "confirmed",
            "bot_token": "new-token",
            "base_url": "https://new-wechat.example.com",
            "user_id": "wx-user",
        }
    )

    updated = api.load_config()
    assert updated.wechat is not None
    assert updated.wechat.bot_token == "new-token"
    assert updated.wechat.base_url == "https://new-wechat.example.com"
    assert updated.platforms.enabled == ["wechat"]
    assert updated.platforms.primary == "wechat"


def test_wechat_qr_start_sends_saved_token_list_to_fixed_qr_host(monkeypatch):
    class _Auth:
        async def start_login(self, base_url=None, local_token_list=None):
            return {
                "session_key": "qr-session",
                "qrcode_url": "https://wechat.example.com/qr",
                "base_url": base_url,
                "local_token_list": local_token_list,
            }

    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(ui_server, "_load_wechat_local_tokens", lambda: ["saved-token"])

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/start",
        json={"base_url": "https://wechat.example.com"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["base_url"] == "https://ilinkai.weixin.qq.com"
    assert payload["local_token_list"] == ["saved-token"]


def test_wechat_qr_poll_does_not_autobind_without_user_id(monkeypatch):
    from vibe import runtime

    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {
                "status": "confirmed",
                "bot_token": "wechat-token",
                "base_url": "https://wechat.example.com",
            }

    bound_users = []
    restart_calls = []
    persisted = []

    runtime.ensure_config()
    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(ui_server, "_persist_wechat_qr_credentials", lambda result: persisted.append(result.copy()))
    monkeypatch.setattr(
        ui_server,
        "_schedule_wechat_qr_login_restart",
        lambda: restart_calls.append(True) or {"job_id": "restart-1"},
    )
    monkeypatch.setattr(
        "vibe.api.auto_bind_wechat_user",
        lambda user_id: bound_users.append(user_id)
        or {"ok": True, "already_bound": False, "is_admin": True, "pending_bind_menu_hint": True},
    )

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "confirmed"
    assert persisted == []
    assert bound_users == []
    assert restart_calls == []


def test_wechat_qr_poll_blocks_restart_when_credential_persist_fails(monkeypatch):
    from vibe import runtime

    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {
                "status": "confirmed",
                "bot_token": "wechat-token",
                "base_url": "https://wechat.example.com",
                "user_id": "wx-user",
            }

    restart_calls = []
    bound_users = []

    runtime.ensure_config()
    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(
        ui_server,
        "_persist_wechat_qr_credentials",
        lambda result: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    monkeypatch.setattr(
        ui_server,
        "_schedule_wechat_qr_login_restart",
        lambda: restart_calls.append(True) or {"job_id": "restart-1"},
    )
    monkeypatch.setattr("vibe.api.auto_bind_wechat_user", lambda user_id: bound_users.append(user_id))

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 500
    assert response.get_json()["error"] == "failed_to_persist_wechat_credentials"
    assert restart_calls == []
    assert bound_users == []


def test_web_push_subscription_routes_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    subscription = {
        "endpoint": "https://push.example.test/sub/1",
        "keys": {
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        },
    }

    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": subscription, "device_label": "iPhone", "device_id": "device-1"},
        headers=headers,
    )
    assert created.status_code == 200
    created_body = created.get_json()
    assert created_body["ok"] is True
    assert created_body["subscription"]["endpoint"] == subscription["endpoint"]
    assert created_body["subscription"]["enabled"] is True
    assert created_body["subscription"]["device_label"] == "iPhone"
    assert created_body["subscription"]["device_id"] == "device-1"

    status = client.post("/api/web-push/status", json={"endpoint": subscription["endpoint"]}, headers=headers)
    assert status.status_code == 200
    status_body = status.get_json()
    assert status_body["ok"] is True
    assert status_body["configured"] is True
    assert status_body["public_key"]
    assert status_body["subscription_count"] == 1
    assert status_body["current_subscription_enabled"] is True

    removed = client.delete(
        "/api/web-push/subscriptions",
        json={"endpoint": subscription["endpoint"]},
        headers=headers,
    )
    assert removed.status_code == 200
    assert removed.get_json() == {"ok": True, "disabled": True}

    status_after = client.post("/api/web-push/status", json={"endpoint": subscription["endpoint"]}, headers=headers)
    assert status_after.get_json()["subscription_count"] == 0
    assert status_after.get_json()["current_subscription_enabled"] is False


def test_web_push_status_sync_disables_previous_endpoint_for_same_device(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    old_subscription = {
        "endpoint": "https://push.example.test/sub/old",
        "keys": {"p256dh": "old-key", "auth": "old-auth"},
    }
    new_subscription = {
        "endpoint": "https://push.example.test/sub/new",
        "keys": {"p256dh": "new-key", "auth": "new-auth"},
    }

    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": old_subscription, "device_id": "device-1"},
        headers=headers,
    )
    assert created.status_code == 200
    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": new_subscription},
        headers=headers,
    )
    assert created.status_code == 200

    status = client.post(
        "/api/web-push/status",
        json={
            "endpoint": new_subscription["endpoint"],
            "subscription": new_subscription,
            "device_id": "device-1",
        },
        headers=headers,
    )

    assert status.status_code == 200
    assert status.get_json()["current_subscription_enabled"] is True
    assert status.get_json()["subscription_count"] == 1
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=old_subscription["endpoint"],
            user_key="local",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=new_subscription["endpoint"],
            user_key="local",
        ) is not None


def test_web_push_status_sync_disables_client_known_previous_endpoint(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    previous_subscription = {
        "endpoint": "https://push.example.test/sub/previous",
        "keys": {"p256dh": "previous-key", "auth": "previous-auth"},
    }
    current_subscription = {
        "endpoint": "https://push.example.test/sub/current",
        "keys": {"p256dh": "current-key", "auth": "current-auth"},
    }
    other_subscription = {
        "endpoint": "https://push.example.test/sub/other",
        "keys": {"p256dh": "other-key", "auth": "other-auth"},
    }

    for subscription in [previous_subscription, current_subscription, other_subscription]:
        created = client.post(
            "/api/web-push/subscriptions",
            json={"subscription": subscription},
            headers=headers,
        )
        assert created.status_code == 200

    status = client.post(
        "/api/web-push/status",
        json={
            "endpoint": current_subscription["endpoint"],
            "subscription": current_subscription,
            "device_id": "device-1",
            "previous_endpoints": [previous_subscription["endpoint"]],
        },
        headers=headers,
    )

    assert status.status_code == 200
    assert status.get_json()["current_subscription_enabled"] is True
    assert status.get_json()["subscription_count"] == 2
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=previous_subscription["endpoint"],
            user_key="local",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=current_subscription["endpoint"],
            user_key="local",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=other_subscription["endpoint"],
            user_key="local",
        ) is not None


def test_web_push_status_sync_does_not_reenable_disabled_endpoint(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    subscription = {
        "endpoint": "https://push.example.test/sub/dead",
        "keys": {"p256dh": "dead-key", "auth": "dead-auth"},
    }
    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": subscription, "device_id": "device-1"},
        headers=headers,
    )
    assert created.status_code == 200

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        web_push_service.mark_send_failure(conn, endpoint=subscription["endpoint"], disable=True)

    status = client.post(
        "/api/web-push/status",
        json={
            "endpoint": subscription["endpoint"],
            "subscription": subscription,
            "device_id": "device-1",
        },
        headers=headers,
    )

    assert status.status_code == 200
    assert status.get_json()["subscription_count"] == 0
    assert status.get_json()["current_subscription_enabled"] is False
    with engine.connect() as conn:
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=subscription["endpoint"],
            user_key="local",
        ) is None


def test_web_push_unsubscribe_is_scoped_to_current_user(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    monkeypatch.setattr(ui_server, "_web_push_user_key", lambda: "remote:user-a")

    endpoint = "https://push.example.test/sub/other"
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-b",
            payload={
                "endpoint": endpoint,
                "keys": {
                    "p256dh": "p256dh-key",
                    "auth": "auth-secret",
                },
            },
        )

    client = app.test_client()
    removed = client.delete(
        "/api/web-push/subscriptions",
        json={"endpoint": endpoint},
        headers=csrf_headers(client),
    )

    assert removed.status_code == 200
    assert removed.get_json() == {"ok": True, "disabled": False}
    with engine.connect() as conn:
        assert web_push_service.count_enabled(conn, user_key="remote:user-b") == 1


def test_web_push_test_route_sends_to_enabled_subscriptions(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    sends = []
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    client = app.test_client()
    headers = csrf_headers(client)
    subscription = {
        "endpoint": "https://push.example.test/sub/1",
        "keys": {
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        },
    }

    missing_endpoint = client.post("/api/web-push/test", json={}, headers=headers)
    assert missing_endpoint.status_code == 400
    assert missing_endpoint.get_json()["error"] == "endpoint_required"

    empty = client.post(
        "/api/web-push/test",
        json={"endpoint": subscription["endpoint"]},
        headers=headers,
    )
    assert empty.status_code == 404
    assert empty.get_json()["error"] == "no_subscription"

    client.post("/api/web-push/subscriptions", json={"subscription": subscription}, headers=headers)
    sent = client.post(
        "/api/web-push/test",
        json={"title": "Hello", "body": "World", "url": "/inbox", "endpoint": subscription["endpoint"]},
        headers=headers,
    )

    assert sent.status_code == 200
    sent_body = sent.get_json()
    assert sent_body["ok"] is True
    assert sent_body["sent"] == 1
    assert sent_body["failed"] == 0
    assert sends[0][0]["endpoint"] == subscription["endpoint"]
    assert sends[0][1]["title"] == "Hello"
    # The test surface carries the same authorization evaluation the normal
    # path applies, so a successful test send can be compared against the
    # normal-only gates (#1434). A local install has no remote gates.
    normal_delivery = sent_body["normal_delivery"]
    assert normal_delivery["user_key"] == "local"
    assert normal_delivery["policy"] == "local"
    assert normal_delivery["authorized"] is True
    assert normal_delivery["recent_deliveries"] == []


def test_web_push_status_reports_normal_delivery_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)

    status = client.post("/api/web-push/status", json={}, headers=headers)
    assert status.status_code == 200
    body = status.get_json()
    assert body["ok"] is True
    normal_delivery = body["normal_delivery"]
    assert normal_delivery["user_key"] == "local"
    assert normal_delivery["policy"] == "local"
    assert normal_delivery["authorized"] is True
    assert normal_delivery["disposition"] is None
    assert normal_delivery["recent_deliveries"] == []


def test_web_push_status_reads_cross_process_delivery_dispositions(monkeypatch, tmp_path):
    """The status surface reads dispositions persisted by the delivery process.

    Normal delivery runs in the controller process while this endpoint runs in
    the UI process, so the disposition ring must round-trip through storage.
    """

    from core import web_push_notifications
    from core.chat_discovery import set_state_meta

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    controller_entry = {
        "at": "2026-08-14T00:00:00Z",
        "message_id": "msg_controller",
        "session_id": "ses_controller",
        "owners": {"local": {"policy": "local", "disposition": "sent", "reason": ""}},
        "disposition": "sent",
    }
    other_entry = {
        "at": "2026-08-14T00:01:00Z",
        "message_id": "msg_other",
        "session_id": "ses_other",
        "owners": {"remote:user-a": {"policy": "personal", "disposition": None, "reason": ""}},
        "disposition": "sent",
    }
    set_state_meta(
        web_push_notifications._DELIVERY_DISPOSITIONS_STATE_KEY,
        [controller_entry, other_entry],
    )

    client = app.test_client()
    status = client.post("/api/web-push/status", json={}, headers=csrf_headers(client))
    assert status.status_code == 200
    normal_delivery = status.get_json()["normal_delivery"]
    # Newest first, scoped to the calling local owner: the remote entry stays
    # private to its own owner's diagnostics.
    assert normal_delivery["recent_deliveries"] == [controller_entry]


def test_web_push_test_route_targets_current_endpoint_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    sends = []
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    client = app.test_client()
    headers = csrf_headers(client)
    subscriptions = [
        {
            "endpoint": "https://push.example.test/sub/desktop",
            "keys": {"p256dh": "desktop-key", "auth": "desktop-auth"},
        },
        {
            "endpoint": "https://push.example.test/sub/mobile",
            "keys": {"p256dh": "mobile-key", "auth": "mobile-auth"},
        },
    ]
    for subscription in subscriptions:
        client.post("/api/web-push/subscriptions", json={"subscription": subscription}, headers=headers)

    sent = client.post(
        "/api/web-push/test",
        json={
            "title": "Hello",
            "body": "World",
            "url": "/inbox",
            "endpoint": subscriptions[0]["endpoint"],
        },
        headers=headers,
    )

    assert sent.status_code == 200
    assert sent.get_json()["ok"] is True
    assert sent.get_json()["sent"] == 1
    assert sent.get_json()["failed"] == 0
    assert [send[0]["endpoint"] for send in sends] == [subscriptions[0]["endpoint"]]


def test_sessions_create_preserves_metadata_without_web_push_owner(monkeypatch, tmp_path):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")

    client = app.test_client()
    response = client.post(
        "/api/sessions",
        json={"project_id": project["id"], "metadata": {"client": "test"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 201
    metadata = response.get_json()["metadata"]
    assert metadata["client"] == "test"
    assert "_web_push_user_key" not in metadata


def test_sessions_create_locks_before_agent_validation(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    route_engine = create_sqlite_engine()
    agent_store = VibeAgentStore()
    competitor = VibeAgentStore()
    try:
        agent_store.create(name="pm", backend="codex")
        project_dir = tmp_path / "locked-create-project"
        project_dir.mkdir()
        with route_engine.begin() as conn:
            project = create_project(conn, str(project_dir), display_name="Project")

        race = {"fired": 0, "refused": 0, "committed": 0}

        @event.listens_for(competitor.engine, "checkout")
        def _no_wait(dbapi_connection, *_args) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout = 0")
            cursor.close()

        @event.listens_for(route_engine, "after_cursor_execute")
        def _archive_on_agent_read(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            if race["fired"] or "FROM agents" not in " ".join(statement.split()):
                return
            race["fired"] = 1
            try:
                competitor.archive("pm")
            except OperationalError:
                race["refused"] = 1
            else:
                race["committed"] = 1

        monkeypatch.setattr(ui_server, "_projects_engine", lambda: route_engine)
        client = app.test_client()
        response = client.post(
            "/api/sessions",
            json={"project_id": project["id"], "agent_name": "pm", "agent_backend": "codex"},
            headers=csrf_headers(client),
        )

        assert response.status_code == 201
        assert response.get_json()["agent_name"] == "pm"
        assert race == {"fired": 1, "refused": 1, "committed": 0}
    finally:
        competitor.close()
        agent_store.close()
        route_engine.dispose()


def test_sessions_patch_locks_before_agent_validation(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    route_engine = create_sqlite_engine()
    agent_store = VibeAgentStore()
    competitor = VibeAgentStore()
    try:
        agent_store.create(name="pm", backend="codex")
        agent_store.create(name="reviewer", backend="codex")
        project_dir = tmp_path / "locked-patch-project"
        project_dir.mkdir()
        with route_engine.begin() as conn:
            project = create_project(conn, str(project_dir), display_name="Project")
            session = create_session(
                conn,
                scope_id=project["scope_id"],
                agent_name="pm",
                agent_backend="codex",
            )

        race = {"fired": 0, "refused": 0, "committed": 0}

        @event.listens_for(competitor.engine, "checkout")
        def _no_wait(dbapi_connection, *_args) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout = 0")
            cursor.close()

        @event.listens_for(route_engine, "after_cursor_execute")
        def _archive_on_agent_read(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            if race["fired"] or "FROM agents" not in " ".join(statement.split()):
                return
            race["fired"] = 1
            try:
                competitor.archive("reviewer")
            except OperationalError:
                race["refused"] = 1
            else:
                race["committed"] = 1

        monkeypatch.setattr(ui_server, "_projects_engine", lambda: route_engine)
        client = app.test_client()
        response = client.patch(
            f"/api/sessions/{session['id']}",
            json={"agent_name": "reviewer", "agent_backend": "codex"},
            headers=csrf_headers(client),
        )

        assert response.status_code == 200
        assert response.get_json()["agent_name"] == "reviewer"
        assert race == {"fired": 1, "refused": 1, "committed": 0}
    finally:
        competitor.close()
        agent_store.close()
        route_engine.dispose()
