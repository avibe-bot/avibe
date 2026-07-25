from __future__ import annotations

import json
from types import SimpleNamespace

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from storage import media_service, messages_service, project_access_service, projects_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.workbench_sessions_service import create_session
from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
from vibe import api, remote_access, ui_server
from vibe.authorization import AuthorizationContext
from vibe.sse_broker import broker
from vibe.ui_server import app


REMOTE_ORIGIN = "https://alex.avibe.bot"
REMOTE_PEER = {"REMOTE_ADDR": "203.0.113.10"}


def _save_config() -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.public_url = REMOTE_ORIGIN
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = f"{REMOTE_ORIGIN}/auth/callback"
    config.save()
    return config


def _intent(project_id: str, email: str, role: str = "editor") -> dict:
    return {
        "project_id": project_id,
        "revision": 1,
        "mode": "restricted",
        "bindings": [
            {
                "principal_kind": "email",
                "principal_value": email,
                "access_role": role,
            }
        ],
    }


def _group_intent(project_id: str, group_id: str, role: str = "editor") -> dict:
    return {
        "project_id": project_id,
        "revision": 1,
        "mode": "restricted",
        "bindings": [
            {
                "principal_kind": "organization_group",
                "principal_value": group_id,
                "access_role": role,
            }
        ],
    }


def _setup_state(tmp_path) -> tuple[V2Config, dict[str, str]]:
    config = _save_config()
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_a_dir = tmp_path / "project-a"
    project_b_dir = tmp_path / "project-b"
    project_a_dir.mkdir()
    project_b_dir.mkdir()
    media_a = tmp_path / "a.txt"
    media_b = tmp_path / "b.txt"
    media_a.write_text("project a", encoding="utf-8")
    media_b.write_text("project b", encoding="utf-8")
    with engine.begin() as conn:
        project_a = projects_service.create_project(conn, str(project_a_dir), display_name="A")
        project_b = projects_service.create_project(conn, str(project_b_dir), display_name="B")
        session_a = create_session(conn, scope_id=project_a["scope_id"], agent_backend="codex")
        session_b = create_session(conn, scope_id=project_b["scope_id"], agent_backend="codex")
        unscoped = create_session(conn, scope_id=None, agent_backend="codex")
        for project, session in ((project_a, session_a), (project_b, session_b)):
            messages_service.append(
                conn,
                scope_id=project["scope_id"],
                session_id=session["id"],
                platform="avibe",
                author="agent",
                message_type="result",
                text="shared needle",
            )
        media_a_token = media_service.register(
            conn,
            scope_id=project_a["scope_id"],
            session_id=session_a["id"],
            kind="file",
            source="agent_reply",
            local_path=str(media_a.resolve()),
        )
        media_b_token = media_service.register(
            conn,
            scope_id=project_b["scope_id"],
            session_id=session_b["id"],
            kind="file",
            source="agent_reply",
            local_path=str(media_b.resolve()),
        )
        project_access_service.apply_project_access_intent(
            conn,
            _intent(project_a["id"], "alice@example.com"),
        )
        project_access_service.apply_project_access_intent(
            conn,
            _group_intent(project_b["id"], "grp_beta"),
        )
    engine.dispose()
    return config, {
        "project_a": project_a["id"],
        "project_b": project_b["id"],
        "session_a": session_a["id"],
        "session_b": session_b["id"],
        "unscoped": unscoped["id"],
        "media_a": media_a_token,
        "media_b": media_b_token,
    }


def _remote_client(config: V2Config, *, role: str, email: str):
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, email, f"user-{role}-{email}", role=role),
        domain="alex.avibe.bot",
    )
    return client


def _get(client, path: str):
    return client.get(path, base_url=REMOTE_ORIGIN, environ_base=REMOTE_PEER)


def test_remote_editor_project_access_filters_every_read_surface(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    client = _remote_client(config, role="editor", email="alice@example.com")
    monkeypatch.setattr(
        api,
        "list_show_pages",
        lambda: {
            "ok": True,
            "count": 3,
            "pages": [
                {"session_id": ids["session_a"]},
                {"session_id": ids["session_b"]},
                {"session_id": ids["unscoped"]},
            ],
        },
    )

    assert [row["id"] for row in _get(client, "/api/projects").get_json()["projects"]] == [
        ids["project_a"]
    ]
    sessions = _get(client, "/api/sessions?status=active").get_json()["sessions"]
    assert {row["id"] for row in sessions} == {ids["session_a"]}
    assert _get(client, f"/api/projects/{ids['project_a']}").status_code == 200
    assert _get(client, f"/api/projects/{ids['project_b']}").status_code == 404
    assert _get(client, f"/api/sessions/{ids['session_a']}/messages").status_code == 200
    assert _get(client, f"/api/sessions/{ids['session_b']}/messages").status_code == 404
    assert _get(client, f"/api/sessions/{ids['unscoped']}").status_code == 404

    search = _get(client, "/api/search/messages?q=needle").get_json()
    assert [row["session_id"] for row in search["sessions"]] == [ids["session_a"]]
    inbox = _get(client, "/api/inbox").get_json()
    assert [row["session_id"] for row in inbox["sessions"]] == [ids["session_a"]]
    assert set(inbox["unread_by_session"]) == {ids["session_a"]}
    assert _get(client, f"/api/media/{ids['media_a']}").status_code == 200
    assert _get(client, f"/api/media/{ids['media_b']}").status_code == 404
    show_pages = _get(client, "/api/show-pages").get_json()
    assert show_pages["pages"] == [{"session_id": ids["session_a"]}]
    assert show_pages["count"] == 1

    published = []
    monkeypatch.setattr(broker, "publish", lambda event_type, data: published.append((event_type, data)))
    headers = csrf_headers(client, REMOTE_ORIGIN)
    mark_read = client.post(
        f"/api/sessions/{ids['session_a']}/mark-read",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=headers,
        json={},
    )
    assert mark_read.status_code == 200
    assert mark_read.get_json()["unread_counts"] == {}
    assert mark_read.get_json()["unread_by_session"] == {}
    event_type, event_data = published[-1]
    assert event_type == "inbox.unread.changed"
    raw_payload = json.dumps({"type": event_type, "data": event_data})
    alice_context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        is_remote=True,
    )
    alice_payload = json.loads(
        ui_server._workbench_event_payload_for_context(
            alice_context,
            event_type,
            raw_payload,
        )
    )
    assert alice_payload["data"]["unread_counts"] == {}
    assert alice_payload["data"]["unread_by_session"] == {}

    owner_payload = json.loads(
        ui_server._workbench_event_payload_for_context(None, event_type, raw_payload)
    )
    assert owner_payload["data"]["unread_counts"] == {
        project_access_service.project_scope_id(ids["project_b"]): 1,
    }
    assert owner_payload["data"]["unread_by_session"] == {ids["session_b"]: 1}
    filtered_owner_payload = json.loads(
        ui_server._workbench_event_payload_for_context(
            alice_context,
            event_type,
            json.dumps(owner_payload),
        )
    )
    assert filtered_owner_payload["data"]["unread_counts"] == {}
    assert filtered_owner_payload["data"]["unread_by_session"] == {}

    allowed_action = client.post(
        f"/api/sessions/{ids['session_a']}/attachments",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=headers,
        json={},
    )
    hidden_action = client.post(
        f"/api/sessions/{ids['session_b']}/attachments",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=headers,
        json={},
    )
    assert allowed_action.status_code == 400
    assert hidden_action.status_code == 404


def test_viewer_no_match_owner_and_local_matrix(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)

    viewer = _remote_client(config, role="viewer", email="alice@example.com")
    assert _get(viewer, f"/api/sessions/{ids['session_a']}").status_code == 200
    assert _get(viewer, f"/api/sessions/{ids['session_b']}").status_code == 404
    headers = csrf_headers(viewer, REMOTE_ORIGIN)
    assert viewer.post(
        "/api/sessions",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=headers,
        json={"project_id": ids["project_a"]},
    ).status_code == 403

    no_match = _remote_client(config, role="editor", email="guest@example.net")
    assert _get(no_match, "/api/projects").get_json()["projects"] == []
    assert _get(no_match, "/api/sessions").get_json()["sessions"] == []
    assert _get(no_match, f"/api/sessions/{ids['session_a']}").status_code == 404

    organization_member = app.test_client()
    organization_member.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "member@example.net",
            "user-org-member",
            role="editor",
            access_source="organization_group",
            organization_id="org_beta",
            organization_member_id="member_beta",
            organization_role="member",
            group_ids=["grp_beta"],
        ),
        domain="alex.avibe.bot",
    )
    assert [
        row["id"] for row in _get(organization_member, "/api/projects").get_json()["projects"]
    ] == [ids["project_b"]]
    assert _get(organization_member, f"/api/sessions/{ids['session_b']}").status_code == 200
    assert _get(organization_member, f"/api/sessions/{ids['session_a']}").status_code == 404

    owner = _remote_client(config, role="owner", email="owner@example.com")
    assert {row["id"] for row in _get(owner, "/api/projects").get_json()["projects"]} == {
        ids["project_a"],
        ids["project_b"],
    }
    assert _get(owner, f"/api/sessions/{ids['unscoped']}").status_code == 200

    local = app.test_client()
    local_projects = local.get("/api/projects", base_url="http://localhost").get_json()["projects"]
    assert {row["id"] for row in local_projects} == {ids["project_a"], ids["project_b"]}
    assert local.get(f"/api/sessions/{ids['unscoped']}", base_url="http://localhost").status_code == 200


def test_project_access_filters_sse_and_show_websocket(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        is_remote=True,
    )
    visible_payload = json.dumps({"type": "session.status", "data": {"session_id": ids["session_a"]}})
    hidden_payload = json.dumps({"type": "session.status", "data": {"session_id": ids["session_b"]}})
    assert ui_server._workbench_event_visible_to_context(context, "session.status", visible_payload) is True
    assert ui_server._workbench_event_visible_to_context(context, "session.status", hidden_payload) is False
    assert ui_server._workbench_event_visible_to_context(
        context,
        "authorization.changed",
        json.dumps({"type": "authorization.changed", "data": {"project_ids": []}}),
    ) is True

    cookie = remote_session_cookie(config, "alice@example.com", "user-editor", role="editor")
    websocket = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"host": "alex.avibe.bot"},
        cookies={remote_access.SESSION_COOKIE_NAME: cookie},
        url=SimpleNamespace(scheme="wss"),
    )
    assert ui_server._show_runtime_websocket_authorized(
        websocket,
        minimum_role="viewer",
        project_session_id=ids["session_a"],
    ) is True
    assert ui_server._show_runtime_websocket_authorized(
        websocket,
        minimum_role="viewer",
        project_session_id=ids["session_b"],
    ) is False
