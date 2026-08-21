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
from core.vibe_agents import VibeAgentStore
from storage import (
    media_service,
    message_deliveries,
    messages_service,
    project_access_service,
    projects_service,
    resource_access_service,
)
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, media_object_references, media_objects, scopes
from storage.workbench_sessions_service import create_session
from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
from vibe import api, internal_client, remote_access, ui_server
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
    shared_media = tmp_path / "shared.txt"
    shared_media.write_text("shared media", encoding="utf-8")
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
            local_path=str(shared_media.resolve()),
        )
        media_b_token = media_service.register(
            conn,
            scope_id=project_b["scope_id"],
            session_id=session_b["id"],
            kind="file",
            source="agent_reply",
            local_path=str(shared_media.resolve()),
        )
        assert media_a_token != media_b_token
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


def _remote_client(
    config: V2Config,
    *,
    role: str,
    email: str,
    active_org: bool = True,
):
    client = app.test_client()
    if active_org:
        cookie = remote_session_cookie(
            config,
            email,
            f"user-{role}-{email}",
            role=role,
            access_source="organization_group",
            organization_id="org-1",
            organization_member_id=f"member-{email}",
            organization_role="member",
            group_ids=[],
        )
    else:
        cookie = remote_session_cookie(config, email, f"user-{role}-{email}", role=role)
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        cookie,
        domain="alex.avibe.bot",
    )
    return client


def _get(client, path: str):
    return client.get(path, base_url=REMOTE_ORIGIN, environ_base=REMOTE_PEER)


def test_active_org_member_can_use_every_project_runtime_surface(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            scopes.update()
            .where(scopes.c.native_id == ids["project_a"])
            .values(metadata_json=json.dumps({"host_path_hint": "/private/host"}))
        )
    client = _remote_client(config, role="editor", email="alice@example.com")
    monkeypatch.setattr(
        api,
        "list_show_pages",
        lambda **_kwargs: {
            "ok": True,
            "count": 3,
            "pages": [
                {"session_id": ids["session_a"]},
                {"session_id": ids["session_b"]},
                {"session_id": ids["unscoped"]},
            ],
        },
    )

    project_rows = _get(client, "/api/projects").get_json()["projects"]
    assert {row["id"] for row in project_rows} == {ids["project_a"]}
    project_a = next(row for row in project_rows if row["id"] == ids["project_a"])
    assert project_a["folder_path"] == str((tmp_path / "project-a").resolve())
    assert project_a["metadata"] == {"host_path_hint": "/private/host"}
    assert project_a["capabilities"] == {"can_chat": True, "has_folder": True}
    sessions = _get(client, "/api/sessions?status=active").get_json()["sessions"]
    assert {row["id"] for row in sessions} == {
        ids["session_a"],
    }
    sessions_by_id = {row["id"]: row for row in sessions}
    local_sessions = app.test_client().get(
        "/api/sessions?status=active",
        base_url="http://localhost",
    ).get_json()["sessions"]
    local_sessions_by_id = {row["id"]: row for row in local_sessions}
    for session_id in (ids["session_a"],):
        assert sessions_by_id[session_id]["workdir"] == local_sessions_by_id[session_id]["workdir"]
        assert sessions_by_id[session_id]["metadata"] == local_sessions_by_id[session_id]["metadata"]
    assert _get(client, f"/api/projects/{ids['project_a']}").status_code == 200
    assert _get(client, f"/api/projects/{ids['project_b']}").status_code == 404
    session = _get(client, f"/api/sessions/{ids['session_a']}").get_json()
    assert session["workdir"] == str((tmp_path / "project-a").resolve())
    assert session["metadata"] == {"created_via": "workbench"}
    assert _get(client, f"/api/sessions/{ids['session_a']}/messages").status_code == 200
    assert _get(client, f"/api/sessions/{ids['session_b']}/messages").status_code == 404
    assert _get(client, f"/api/sessions/{ids['unscoped']}").status_code == 404

    search = _get(client, "/api/search/messages?q=needle").get_json()
    assert {row["session_id"] for row in search["sessions"]} == {
        ids["session_a"],
    }
    inbox = _get(client, "/api/inbox").get_json()
    assert {row["session_id"] for row in inbox["sessions"]} == {
        ids["session_a"],
    }
    assert set(inbox["unread_by_session"]) == {ids["session_a"]}
    media_response = _get(client, f"/api/media/{ids['media_a']}")
    assert media_response.status_code == 200
    assert media_response.headers["Cache-Control"] == "private, no-store"
    assert _get(client, f"/api/media/{ids['media_b']}").status_code == 404
    show_pages = _get(client, "/api/show-pages").get_json()
    assert {page["session_id"] for page in show_pages["pages"]} == {
        ids["session_a"],
        ids["session_b"],
        ids["unscoped"],
    }
    assert show_pages["count"] == 3

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
    # Empty JSON is rejected by the upload parser, but the request must reach
    # the endpoint rather than being stopped by the remote execution gate.
    assert allowed_action.status_code != 403 or allowed_action.get_json().get("code") != "remote_execution_disabled"
    assert hidden_action.status_code != 403 or hidden_action.get_json().get("code") != "remote_execution_disabled"


def test_member_project_mutations_are_bounded_by_the_acl_that_hides_them(monkeypatch, tmp_path) -> None:
    """Instance role admits the route; the Project ACL still picks the Project.

    ``can_manage_projects`` is instance-wide Project administration, but every
    one of these routes names a single Project, and the instance role says
    nothing about which. The middleware used to skip member-tier routes
    entirely, so a member who knew an id could PATCH or archive a Project that
    ``GET /api/projects`` and ``GET /api/projects/<id>`` already hid from them.

    404 on every hidden Project, matching the read path: a 403/404 split would
    let a caller enumerate the restricted Projects they are excluded from.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)

    def _patch(client, project_id, headers, name):
        return client.patch(
            f"/api/projects/{project_id}",
            base_url=REMOTE_ORIGIN,
            environ_base=REMOTE_PEER,
            headers=headers,
            json={"display_name": name},
        )

    def _archive(client, project_id, headers):
        return client.delete(
            f"/api/projects/{project_id}",
            base_url=REMOTE_ORIGIN,
            environ_base=REMOTE_PEER,
            headers=headers,
        )

    # Bound to neither Project: the list is empty, and so is what they can touch.
    excluded = _remote_client(config, role="member", email="carol@example.com")
    excluded_headers = csrf_headers(excluded, REMOTE_ORIGIN)
    assert _get(excluded, "/api/projects").get_json()["projects"] == []
    for project_id in (ids["project_a"], ids["project_b"]):
        assert _get(excluded, f"/api/projects/{project_id}").status_code == 404
        assert _patch(excluded, project_id, excluded_headers, "Stolen").status_code == 404
        assert _archive(excluded, project_id, excluded_headers).status_code == 404

    # An explicit editor binding is below "member", and that is the point: the
    # floor for these routes is the ACL's visibility floor, so the Projects the
    # list shows are exactly the Projects that can be mutated.
    included = _remote_client(config, role="member", email="alice@example.com")
    included_headers = csrf_headers(included, REMOTE_ORIGIN)
    assert {row["id"] for row in _get(included, "/api/projects").get_json()["projects"]} == {
        ids["project_a"]
    }
    renamed = _patch(included, ids["project_a"], included_headers, "Alice Renamed")
    assert renamed.status_code == 200
    assert renamed.get_json()["display_name"] == "Alice Renamed"
    assert _patch(included, ids["project_b"], included_headers, "Stolen").status_code == 404
    assert _archive(included, ids["project_b"], included_headers).status_code == 404
    assert _archive(included, ids["project_a"], included_headers).status_code == 200


def test_session_bootstrap_uses_effective_project_chat_role(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(
            conn,
            {
                **_intent(ids["project_a"], "alice@example.com", role="viewer"),
                "revision": 2,
            },
        )
        scope_id = project_access_service.project_scope_id(ids["project_a"])
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=ids["session_a"],
            text="editor-only queued prompt",
        )
        message_deliveries.set_draft(
            conn,
            ids["session_a"],
            "editor-only draft",
        )
    with engine.connect() as conn:
        draft_state = message_deliveries.get_draft_state(conn, ids["session_a"])
    expected_draft = {
        "text": "editor-only draft",
        "updated_at": draft_state["updated_at"],
    }
    assert expected_draft["updated_at"]

    monkeypatch.setattr(
        api,
        "get_vibe_agents",
        lambda **kwargs: {
            "agents": [{"name": "editor-agent", "backend": "codex", "enabled": True}],
            "default_agent_name": "editor-agent",
        },
    )
    client = _remote_client(config, role="editor", email="alice@example.com")

    viewer_project = _get(client, f"/api/projects/{ids['project_a']}").get_json()
    assert viewer_project["capabilities"] == {"can_chat": False, "has_folder": True}
    assert viewer_project["folder_path"] == ""
    assert viewer_project["metadata"] == {}

    viewer_bootstrap = _get(client, f"/api/sessions/{ids['session_a']}/bootstrap")

    assert viewer_bootstrap.status_code == 200
    viewer_payload = viewer_bootstrap.get_json()
    assert viewer_payload["capabilities"] == {"can_chat": False}
    assert viewer_payload["agents"] == []
    assert viewer_payload["default_agent_name"] is None
    assert viewer_payload["queued"] == []
    assert viewer_payload["draft"] == {"text": ""}
    assert [message["text"] for message in viewer_payload["messages"]] == ["shared needle"]
    assert viewer_payload["session"]["workdir"] == str((tmp_path / "project-a").resolve())
    assert viewer_payload["session"]["metadata"] == {"created_via": "workbench"}

    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(
            conn,
            {
                **_intent(ids["project_a"], "alice@example.com"),
                "revision": 3,
            },
        )

    editor_bootstrap = _get(client, f"/api/sessions/{ids['session_a']}/bootstrap")

    assert editor_bootstrap.status_code == 200
    editor_payload = editor_bootstrap.get_json()
    assert editor_payload["capabilities"] == {"can_chat": True}
    assert editor_payload["agents"][0]["name"] == "editor-agent"
    assert editor_payload["default_agent_name"] == "editor-agent"
    assert editor_payload["queued"][0]["text"] == "editor-only queued prompt"
    assert editor_payload["draft"] == expected_draft
    # Editors receive the non-sensitive runtime UI projection used by ChatPage;
    # Owner-only management data remains available only to the Owner payload.
    # Asserted as the declared key set rather than as named exclusions: any
    # block that starts reaching non-owners fails here even if nobody thought
    # to forbid it, and cloud pairing appears only as a readiness boolean —
    # never as an endpoint, identifier, or secret.
    assert editor_payload["config"]["ui"]["chat_message_font_size"]
    assert set(editor_payload["config"]) == set(api._EDITOR_CONFIG_WRITE_FIELDS) | set(
        api._NON_OWNER_CONFIG_CONTEXT_FIELDS
    )
    assert editor_payload["config"]["remote_access"] == {"vibe_cloud": {"paired": False}}

    owner = _remote_client(config, role="owner", email="owner@example.com")
    owner_payload = _get(owner, f"/api/sessions/{ids['session_a']}/bootstrap").get_json()
    assert owner_payload["config"]["runtime"]["default_cwd"] == "."
    assert "agents" in owner_payload["config"]
    assert "memory" not in owner_payload["config"]
    assert "remote_access" in owner_payload["config"]

    local_payload = app.test_client().get(
        f"/api/sessions/{ids['session_a']}/bootstrap",
        base_url="http://localhost",
    ).get_json()
    assert local_payload["config"]["runtime"]["default_cwd"] == "."

    draft_get = _get(client, f"/api/sessions/{ids['session_a']}/draft")
    draft_put = client.put(
        f"/api/sessions/{ids['session_a']}/draft",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=csrf_headers(client, REMOTE_ORIGIN),
        json={
            "text": "remote overwrite",
            "expected_updated_at": expected_draft["updated_at"],
        },
    )
    assert draft_get.status_code == 200
    assert draft_get.get_json() == expected_draft
    assert draft_put.status_code == 200
    with engine.connect() as conn:
        assert message_deliveries.get_draft(conn, ids["session_a"])["text"] == "remote overwrite"
    editor_project = _get(client, f"/api/projects/{ids['project_a']}").get_json()
    assert editor_project["capabilities"] == {"can_chat": True, "has_folder": True}


def test_editor_config_write_always_answers_with_a_renderable_code(monkeypatch, tmp_path) -> None:
    """Every rejected Editor config write leaves the API as a client-renderable code.

    Stated over the ways an Editor write can fail rather than over the one
    message a review happened to name: a field outside the write allowlist is
    refused up front, and an allowlisted field carrying a bad value is refused
    much later inside ``V2Config.from_payload``. Both leave through the same
    chokepoint, so a non-English client never has to render a raw English
    validation sentence. The accepted write is asserted alongside them so the
    codes cannot be produced by an endpoint that refuses everything.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, _ids = _setup_state(tmp_path)
    client = _remote_client(config, role="editor", email="alice@example.com")

    def _post(payload: dict):
        return client.post(
            "/api/config",
            base_url=REMOTE_ORIGIN,
            environ_base=REMOTE_PEER,
            headers=csrf_headers(client, REMOTE_ORIGIN),
            json=payload,
        )

    forbidden = _post({"runtime": {"default_cwd": "/tmp/editor-should-not-write"}})
    assert forbidden.status_code == 400
    assert forbidden.get_json()["error"] == {
        "code": "editor_config_write_forbidden",
        "message": "editor_config_write_forbidden",
    }

    invalid = _post({"ack_mode": "not-a-mode"})
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == {
        "code": "editor_config_write_invalid",
        "message": "editor_config_write_invalid",
    }

    accepted = _post({"ack_mode": "reaction"})
    assert accepted.status_code == 200
    assert V2Config.load().ack_mode == "reaction"
    assert V2Config.load().runtime.default_cwd == "."


def test_config_write_requires_an_object_body_whatever_the_role(monkeypatch, tmp_path) -> None:
    """A config write is a patch object, and a body that is not one is refused.

    Stated over the JSON value space rather than over the shapes a review
    happened to name: the route decoded the body with the usual ``request.json
    or {}``, which turned every falsy value — including a missing body — into
    an empty patch, so a malformed write persisted nothing and still answered
    200. Truthy non-objects were refused only because they survived that
    coercion, which is why an enumeration would have missed exactly the half
    that was broken. Both roles are asserted because the rule belongs to the
    route, not to the Editor allowlist that happened to cover one side of it.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, _ids = _setup_state(tmp_path)
    editor = _remote_client(config, role="editor", email="alice@example.com")
    owner = app.test_client()
    editor_headers = {**csrf_headers(editor, REMOTE_ORIGIN), "Content-Type": "application/json"}
    owner_headers = {**csrf_headers(owner, "http://localhost"), "Content-Type": "application/json"}

    def _post_body(body):
        return (
            editor.post(
                "/api/config",
                base_url=REMOTE_ORIGIN,
                environ_base=REMOTE_PEER,
                headers=editor_headers,
                content=json.dumps(body),
            ),
            owner.post(
                "/api/config",
                base_url="http://localhost",
                headers=owner_headers,
                content=json.dumps(body),
            ),
        )

    for body in (None, [], [1], "", "text", 0, 1, False, True):
        editor_response, owner_response = _post_body(body)
        assert editor_response.status_code == 400, body
        assert editor_response.get_json()["error"] == {
            "code": "editor_config_write_invalid",
            "message": "editor_config_write_invalid",
        }, body
        # The Owner keeps the descriptive message its Settings pages render.
        assert owner_response.status_code == 400, body
        assert owner_response.get_json()["error"] == "Config payload must be an object", body

    # An empty object is a valid no-op patch, so the refusals above cannot be
    # produced by a route that has started refusing every body.
    editor_response, owner_response = _post_body({})
    assert editor_response.status_code == 200
    assert owner_response.status_code == 200
    assert V2Config.load().ack_mode == "typing"


def test_archived_project_invalidates_retained_remote_urls(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    published = []
    monkeypatch.setattr(
        broker,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    local = app.test_client()
    archive = local.delete(
        f"/api/projects/{ids['project_a']}",
        base_url="http://localhost",
        headers=csrf_headers(local, "http://localhost"),
    )
    assert archive.status_code == 200
    assert (
        "authorization.changed",
        {"project_ids": [ids["project_a"]]},
    ) in published

    monkeypatch.setattr(
        api,
        "list_show_pages",
        lambda **_kwargs: {
            "ok": True,
            "count": 1,
            "pages": [{"session_id": ids["session_a"]}],
        },
    )
    client = _remote_client(config, role="editor", email="alice@example.com")

    assert _get(client, "/api/projects").get_json()["projects"] == []
    archived_project = _get(client, f"/api/projects/{ids['project_a']}")
    assert archived_project.status_code == 404
    assert _get(client, f"/api/sessions/{ids['session_a']}/messages").status_code == 404
    assert _get(client, f"/api/media/{ids['media_a']}").status_code == 404
    assert _get(client, "/api/show-pages").get_json()["pages"] == [
        {"session_id": ids["session_a"]}
    ]

    response = client.post(
        f"/api/sessions/{ids['session_a']}/attachments",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=csrf_headers(client, REMOTE_ORIGIN),
        json={},
    )
    assert response.status_code != 403 or response.get_json().get("code") != "remote_execution_disabled"


def test_legacy_media_token_uses_all_migrated_session_references(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    engine = create_sqlite_engine()
    token = "legacy-shared-token"
    with engine.begin() as conn:
        original = media_service.get_by_token(conn, ids["media_a"])
        assert original is not None
        conn.execute(media_objects.insert().values(**{**original, "token": token}))
        conn.execute(
            media_object_references.insert(),
            [
                {
                    "token": token,
                    "session_id": ids["session_a"],
                    "created_at": original["created_at"],
                },
                {
                    "token": token,
                    "session_id": ids["session_b"],
                    "created_at": original["created_at"],
                },
            ],
        )

    alice = _remote_client(config, role="editor", email="alice@example.com")
    beta = _remote_client(config, role="editor", email="beta@example.com")
    beta.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "beta@example.com",
            "user-beta",
            role="editor",
            organization_id="org_beta",
            organization_member_id="member_beta",
            organization_role="member",
            group_ids=["grp_beta"],
        ),
        domain="alex.avibe.bot",
    )

    assert _get(alice, f"/api/media/{token}").status_code == 200
    assert _get(beta, f"/api/media/{token}").status_code == 200


def test_remote_editor_message_persists_authoritative_identity_and_dispatches(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    engine = create_sqlite_engine()
    client = _remote_client(config, role="editor", email="alice@example.com")
    store = VibeAgentStore()
    try:
        agent = store.create(name="remote-editor-agent", backend="codex")
        with store.engine.begin() as conn:
            resource_access_service.ensure_resource_policy(
                conn,
                resource_kind="agent",
                resource_id=agent.id,
                organization_id=None,
                owner_user_id="user-editor-alice@example.com",
                access_level="private",
            )
    finally:
        store.close()
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == ids["session_a"])
            .values(agent_id=agent.id, agent_name=agent.name)
        )

    with engine.connect() as conn:
        before_ids = {
            row["id"]
            for row in messages_service.list_session_messages(
                conn,
                session_id=ids["session_a"],
                limit=500,
            )["messages"]
        }

    dispatch_calls = []

    async def dispatch_async(payload):
        dispatch_calls.append(payload)
        return {"status_code": 202, "body": {"delivery_state": "queued"}}

    monkeypatch.setattr(internal_client, "dispatch_async", dispatch_async)
    response = client.post(
        f"/api/sessions/{ids['session_a']}/messages",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=csrf_headers(client, REMOTE_ORIGIN),
        json={
            "text": "Run it",
            "metadata": {
                "_web_push_authorization_contexts": [
                    {"user_key": "remote:spoofed", "email": "spoofed@example.com"}
                ],
                "resource_user_context": {
                    "sub": "spoofed",
                    "vibe_instance_role": "owner",
                    "vibe_instance_access_source": "owner",
                },
            },
        },
    )

    assert response.status_code == 202
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["text"] == "Run it"
    with engine.connect() as conn:
        stored = message_deliveries.get_delivery(conn, response.get_json()["id"])
        after_ids = {
            row["id"]
            for row in messages_service.list_session_messages(
                conn,
                session_id=ids["session_a"],
                limit=500,
            )["messages"]
        }
    assert stored is not None
    stored_metadata = message_deliveries.delivery_payload(stored)["metadata"]
    assert stored_metadata["resource_user_context"]["sub"] == "user-editor-alice@example.com"
    assert stored_metadata["resource_user_context"]["vibe_instance_role"] == "editor"
    assert stored_metadata["_web_push_authorization_contexts"][0]["sub"] == (
        "user-editor-alice@example.com"
    )
    assert "spoofed" not in json.dumps(stored_metadata)
    assert after_ids == before_ids


def test_viewer_no_match_owner_and_local_matrix(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            scopes.update()
            .where(scopes.c.native_id == ids["project_a"])
            .values(metadata_json=json.dumps({"host_path_hint": "/private/host"}))
        )
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == ids["session_a"])
            .values(
                workdir=str((tmp_path / "project-a").resolve()),
                metadata_json=json.dumps({"host_session_hint": "/private/session"}),
            )
        )
    engine.dispose()

    viewer = _remote_client(config, role="viewer", email="alice@example.com")
    viewer_projects = _get(viewer, "/api/projects").get_json()["projects"]
    viewer_project = next(row for row in viewer_projects if row["id"] == ids["project_a"])
    assert viewer_project["folder_path"] == ""
    assert viewer_project["metadata"] == {}
    viewer_project_detail = _get(viewer, f"/api/projects/{ids['project_a']}").get_json()
    assert viewer_project_detail["folder_path"] == ""
    assert viewer_project_detail["metadata"] == {}
    assert _get(viewer, f"/api/sessions/{ids['session_a']}").status_code == 200
    assert _get(viewer, f"/api/sessions/{ids['session_b']}").status_code == 404
    headers = csrf_headers(viewer, REMOTE_ORIGIN)
    mark_read = viewer.post(
        f"/api/sessions/{ids['session_a']}/mark-read",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=headers,
        json={},
    )
    assert mark_read.status_code == 200
    for action in ("messages", "attachments", "cancel"):
        response = viewer.post(
            f"/api/sessions/{ids['session_a']}/{action}",
            base_url=REMOTE_ORIGIN,
            environ_base=REMOTE_PEER,
            headers=headers,
            json={"text": "viewer must not send"} if action == "messages" else {},
        )
        assert response.status_code == 403
    assert viewer.post(
        "/api/sessions",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers=headers,
        json={"project_id": ids["project_a"]},
    ).status_code == 403

    no_match = _remote_client(
        config,
        role="editor",
        email="guest@example.net",
        active_org=False,
    )
    assert _get(no_match, "/api/projects").status_code == 200
    assert _get(no_match, "/api/projects").get_json()["projects"] == []
    assert _get(no_match, "/api/sessions").status_code == 200
    assert _get(no_match, f"/api/sessions/{ids['session_a']}").status_code == 404
    no_match_headers = csrf_headers(no_match, REMOTE_ORIGIN)
    denied = no_match.post(
        "/api/sessions",
        base_url=REMOTE_ORIGIN,
        environ_base=REMOTE_PEER,
        headers={**no_match_headers, "Accept-Language": "zh-CN,zh;q=0.9"},
        json={"project_id": ids["project_a"]},
    )
    assert denied.status_code == 403

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
    assert {row["id"] for row in _get(organization_member, "/api/projects").get_json()["projects"]} == {ids["project_b"]}
    assert _get(organization_member, f"/api/sessions/{ids['session_b']}").status_code == 200
    assert _get(organization_member, f"/api/sessions/{ids['session_a']}").status_code == 404
    assert _get(organization_member, f"/api/media/{ids['media_b']}").status_code == 200
    assert _get(organization_member, f"/api/media/{ids['media_a']}").status_code == 404

    owner = _remote_client(config, role="owner", email="owner@example.com")
    owner_projects = _get(owner, "/api/projects").get_json()["projects"]
    assert {row["id"] for row in owner_projects} == {
        ids["project_a"],
        ids["project_b"],
    }
    owner_project_a = next(row for row in owner_projects if row["id"] == ids["project_a"])
    assert owner_project_a["folder_path"] == str((tmp_path / "project-a").resolve())
    assert owner_project_a["metadata"] == {"host_path_hint": "/private/host"}
    owner_project = _get(owner, f"/api/projects/{ids['project_a']}").get_json()
    assert owner_project["folder_path"] == str((tmp_path / "project-a").resolve())
    assert owner_project["metadata"] == {"host_path_hint": "/private/host"}
    owner_bootstrap = _get(owner, "/api/workbench/projects-bootstrap").get_json()
    bootstrap_project_a = next(
        row for row in owner_bootstrap["projects"] if row["id"] == ids["project_a"]
    )
    assert bootstrap_project_a["folder_path"] == str((tmp_path / "project-a").resolve())
    assert bootstrap_project_a["metadata"] == {"host_path_hint": "/private/host"}
    owner_sessions = _get(owner, "/api/sessions").get_json()["sessions"]
    owner_session_a = next(row for row in owner_sessions if row["id"] == ids["session_a"])
    assert owner_session_a["workdir"] == str((tmp_path / "project-a").resolve())
    assert owner_session_a["metadata"] == {"host_session_hint": "/private/session"}
    owner_session_a = _get(owner, f"/api/sessions/{ids['session_a']}").get_json()
    assert owner_session_a["workdir"] == str((tmp_path / "project-a").resolve())
    assert owner_session_a["metadata"] == {"host_session_hint": "/private/session"}
    assert _get(owner, f"/api/sessions/{ids['unscoped']}").status_code == 200

    local = app.test_client()
    local_projects = local.get("/api/projects", base_url="http://localhost").get_json()["projects"]
    assert {row["id"] for row in local_projects} == {ids["project_a"], ids["project_b"]}
    local_project_a = next(row for row in local_projects if row["id"] == ids["project_a"])
    assert local_project_a["folder_path"] == str((tmp_path / "project-a").resolve())
    assert local_project_a["metadata"] == {"host_path_hint": "/private/host"}
    local_session_a = local.get(
        f"/api/sessions/{ids['session_a']}",
        base_url="http://localhost",
    ).get_json()
    assert local_session_a["workdir"] == str((tmp_path / "project-a").resolve())
    assert local_session_a["metadata"] == {"host_session_hint": "/private/session"}
    assert local.get(f"/api/sessions/{ids['unscoped']}", base_url="http://localhost").status_code == 200


def test_show_page_payload_redacts_path_for_viewers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(
            conn,
            {
                **_intent(ids["project_a"], "alice@example.com", role="viewer"),
                "revision": 2,
            },
        )
    monkeypatch.setattr(
        api,
        "list_show_pages",
        lambda **_kwargs: {
            "ok": True,
            "count": 1,
            "pages": [{"session_id": ids["session_a"], "path": "/private/show-page"}],
        },
    )

    viewer = _remote_client(config, role="viewer", email="alice@example.com")
    viewer_page = _get(viewer, "/api/show-pages").get_json()["pages"][0]
    assert "path" not in viewer_page

    editor = _remote_client(config, role="editor", email="alice@example.com")
    editor_page = _get(editor, "/api/show-pages").get_json()["pages"][0]
    assert "path" not in editor_page

    owner = _remote_client(config, role="owner", email="owner@example.com")
    owner_page = _get(owner, "/api/show-pages").get_json()["pages"][0]
    assert owner_page["path"] == "/private/show-page"


def test_show_page_mutation_payload_redacts_direct_and_nested_pages(monkeypatch) -> None:
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        subject="user-editor",
        is_remote=True,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: _Engine())
    monkeypatch.setattr(
        project_access_service,
        "get_effective_session_role",
        lambda _conn, _context, _session_id: "viewer",
    )
    monkeypatch.setattr(
        project_access_service,
        "get_session_project_id",
        lambda _conn, _session_id: "project-1",
    )
    monkeypatch.setattr(project_access_service, "session_exists", lambda _conn, _session_id: True)
    monkeypatch.setattr(
        resource_access_service,
        "can_manage_resource_acl",
        lambda _context, _kind, _resource_id, *, connection: False,
    )

    direct = ui_server._show_page_response_for_request(
        {"ok": True, "session_id": "session-a", "path": "/private/page"},
        context,
    )
    nested = ui_server._show_page_response_for_request(
        {
            "ok": True,
            "page": {"session_id": "session-a", "path": "/private/page"},
        },
        context,
    )

    assert "path" not in direct
    assert "path" not in nested["page"]


def test_show_page_payload_preserves_path_for_non_project_page_owner(monkeypatch) -> None:
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        subject="user-editor",
        is_remote=True,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: _Engine())
    monkeypatch.setattr(
        project_access_service,
        "get_effective_session_role",
        lambda _conn, _context, _session_id: None,
    )
    monkeypatch.setattr(
        project_access_service,
        "get_session_project_id",
        lambda _conn, _session_id: None,
    )
    monkeypatch.setattr(project_access_service, "session_exists", lambda _conn, _session_id: True)
    monkeypatch.setattr(
        resource_access_service,
        "can_manage_resource_acl",
        lambda _context, _kind, _resource_id, *, connection: True,
    )

    payload = ui_server._show_page_response_for_request(
        {"ok": True, "session_id": "im-session", "path": "/private/page"},
        context,
    )

    assert payload["path"] == "/private/page"


def test_show_page_payload_project_viewer_downgrade_wins_over_instance_editor(monkeypatch) -> None:
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        subject="user-editor",
        is_remote=True,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: _Engine())
    monkeypatch.setattr(
        project_access_service,
        "get_effective_session_role",
        lambda _conn, _context, _session_id: "viewer",
    )
    monkeypatch.setattr(
        project_access_service,
        "get_session_project_id",
        lambda _conn, _session_id: "project-1",
    )
    monkeypatch.setattr(project_access_service, "session_exists", lambda _conn, _session_id: True)

    payload = ui_server._show_page_response_for_request(
        {"ok": True, "session_id": "project-session", "path": "/private/page"},
        context,
    )

    assert "path" not in payload


def test_show_page_payload_does_not_treat_inaccessible_project_as_unscoped(monkeypatch) -> None:
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        subject="user-editor",
        is_remote=True,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: _Engine())
    monkeypatch.setattr(
        project_access_service,
        "get_effective_session_role",
        lambda _conn, _context, _session_id: None,
    )
    monkeypatch.setattr(
        project_access_service,
        "get_session_project_id",
        lambda _conn, _session_id: "project-removed-binding",
    )
    monkeypatch.setattr(project_access_service, "session_exists", lambda _conn, _session_id: True)

    payload = ui_server._show_page_response_for_request(
        {"ok": True, "session_id": "project-session", "path": "/private/page"},
        context,
    )

    assert "path" not in payload


def test_show_page_payload_redacts_path_when_session_is_missing(monkeypatch) -> None:
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        subject="user-editor",
        is_remote=True,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: _Engine())
    monkeypatch.setattr(project_access_service, "session_exists", lambda _conn, _session_id: False)

    payload = ui_server._show_page_response_for_request(
        {"ok": True, "session_id": "deleted-session", "path": "/private/page"},
        context,
    )

    assert "path" not in payload


def test_project_access_filters_sse_and_show_websocket(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, ids = _setup_state(tmp_path)
    context = AuthorizationContext(
        instance_role="editor",
        email="alice@example.com",
        subject="user-editor",
        instance_access_source="organization_group",
        organization_id="org-1",
        organization_member_id="member-1",
        organization_role="member",
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

    cookie = remote_session_cookie(
        config,
        "alice@example.com",
        "user-editor",
        role="editor",
        access_source="organization_group",
        organization_id="org-1",
        organization_member_id="member-1",
        organization_role="member",
        group_ids=["grp_beta"],
    )
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
    # §3.2: /show admission follows the Instance role alone, independent of the
    # Project ACL, so the show websocket authorizes a page outside the caller's
    # Project access exactly like an in-Project one (a Viewer enters every page).
    assert ui_server._show_runtime_websocket_authorized(
        websocket,
        minimum_role="viewer",
        project_session_id=ids["session_b"],
    ) is True


def test_terminal_websocket_requires_editor_role_without_a_project(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config, _ids = _setup_state(tmp_path)
    viewer_cookie = remote_session_cookie(
        config,
        "viewer@example.com",
        "user-viewer",
        role="viewer",
        access_source="organization_group",
        organization_id="org-1",
        organization_member_id="member-viewer",
        organization_role="member",
        group_ids=["grp_beta"],
    )
    editor_cookie = remote_session_cookie(
        config,
        "alice@example.com",
        "user-editor",
        role="editor",
        access_source="organization_group",
        organization_id="org-1",
        organization_member_id="member-1",
        organization_role="member",
        group_ids=["grp_beta"],
    )
    viewer_socket = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"host": "alex.avibe.bot"},
        cookies={remote_access.SESSION_COOKIE_NAME: viewer_cookie},
        url=SimpleNamespace(scheme="wss"),
    )
    editor_socket = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"host": "alex.avibe.bot"},
        cookies={remote_access.SESSION_COOKIE_NAME: editor_cookie},
        url=SimpleNamespace(scheme="wss"),
    )
    assert ui_server._show_runtime_websocket_authorized(
        viewer_socket,
        minimum_role="editor",
    ) is False
    assert ui_server._show_runtime_websocket_authorized(
        editor_socket,
        minimum_role="editor",
    ) is True
