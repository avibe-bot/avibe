"""PERMISSIONS-014..017: signed Member operations with test-owned state.

Only execution IPC is stubbed, after route and resource authorization. Services,
identity cookies, CSRF, persistence, ACLs and SSE delivery are real.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from core.inbox_events import run_updated_payload, vaults_updated_payload
from core.show_pages import ShowPageStore
from core.vibe_agents import VibeAgentAccessError, VibeAgentStore, ensure_agent_name_access
from core.web_push_notifications import _badge_count_for_user_key, _filter_project_authorized_user_keys
from storage import messages_service, project_access_service, projects_service, resource_access_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages, resource_access_policies, resource_access_groups
from storage.workbench_sessions_service import create_session
from tests.ui_server_test_helpers import _save_config, csrf_headers, remote_peer, remote_session_cookie
from vibe import internal_client, remote_access, ui_server
from vibe.authorization import INSTANCE_SCOPED_REFETCH_EVENTS, AuthorizationContext
from vibe.sse_broker import broker
from vibe.ui_compat import g

ORIGIN = "https://alex.avibe.bot"


@pytest.fixture(params=["personal", "organization"])
def operations(request, tmp_path, monkeypatch):
    config = _save_config(tmp_path, paired=True, instance_kind=request.param)
    ensure_sqlite_state()
    monkeypatch.setattr(ui_server, "_ensure_remote_access_monitoring", lambda *args: None)
    store = VibeAgentStore()
    store.ensure_builtin_default_agent(backend="codex")
    agent = store.require("codex")
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        # A built-in without an ACL is the reported Organization failure.
        conn.execute(resource_access_policies.delete().where(resource_access_policies.c.resource_id == agent.id))
        project = projects_service.create_project(conn, str(tmp_path), display_name="项目 α")
        project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": project["id"],
                "revision": 1,
                "mode": "restricted",
                "organization_id": "foreign-org",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": "other-group",
                        "access_role": "viewer",
                    }
                ],
            },
        )
        from storage.settings_service import upsert_scope

        im_scope = upsert_scope(
            conn, platform="slack", scope_type="channel", native_id="C_TEST", now="2026-09-17T00:00:00Z"
        )
        sessions = [
            create_session(conn, scope_id=scope, agent_backend=agent.backend, agent_name=agent.name, title="旧会话 α")
            for scope in (project["scope_id"], None, im_scope)
        ]
        for session in sessions:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session["id"])
                .values(native_session_id="native-test")
            )
            messages_service.append(
                conn,
                session_id=session["id"],
                scope_id=session["scope_id"],
                platform="avibe",
                author="agent",
                message_type="result",
                text="实例内容 α",
            )

    def context(role="member", subject="operator"):
        return AuthorizationContext(
            instance_role=role,
            subject=subject,
            email=f"{subject}@example.com",
            instance_id=config.remote_access.vibe_cloud.instance_id,
            instance_kind=request.param,
            instance_access_source="email",
            organization_id="org-1",
            organization_member_id="org-member",
            organization_role="member",
            is_remote=True,
        )

    def client(role="member", subject="operator"):
        value = ui_server.app.test_client()
        value.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            remote_session_cookie(
                config,
                f"{subject}@example.com",
                subject,
                role=role,
                access_source="owner" if role == "owner" else "email",
                organization_id="org-1",
                organization_member_id="org-member",
                organization_role="member",
                group_ids=[],
            ),
            domain="alex.avibe.bot",
        )
        headers = csrf_headers(value, ORIGIN)

        def call(method, path, payload=None, csrf=True):
            return value.request(
                method, path, json=payload, headers=headers if csrf else {}, base_url=ORIGIN, environ_base=remote_peer()
            )

        return call

    yield SimpleNamespace(
        config=config,
        engine=engine,
        store=store,
        agent=agent,
        project=project,
        sessions=sessions,
        client=client,
        context=context,
        kind=request.param,
    )
    store.close()
    engine.dispose()


def _policies(state):
    with state.engine.connect() as conn:
        return (
            project_access_service.get_project_policy(conn, state.project["id"]),
            [dict(row) for row in conn.execute(select(resource_access_policies)).mappings()],
            [dict(row) for row in conn.execute(select(resource_access_groups)).mappings()],
        )


def test_permissions_014_member_signed_agent_session_lifecycle(operations, monkeypatch):
    """PERMISSIONS-014: discovery, selection, chat, fork and Show agree."""
    state = operations
    call = state.client()
    before = _policies(state)
    from tests.test_ui_session_stream import _settle_reserved_delivery

    async def accept(payload):
        from core.session_turns import SessionTurnManager
        from storage import message_deliveries

        # Consume the exact persisted remote Delivery at the final execution
        # authorization seam, before stubbing the external provider acceptance.
        with state.engine.connect() as conn:
            delivery = message_deliveries.get_delivery(conn, payload["user_message_id"])
            assert message_deliveries.delivery_has_remote_resource_context(delivery)
            assert SessionTurnManager._remote_delivery_execution_denial(conn, delivery) is None
        _settle_reserved_delivery(payload, state="accepted")
        return {"status_code": 202, "body": {"ok": True, "delivery_state": "accepted"}}

    dispatch = AsyncMock(side_effect=accept)
    monkeypatch.setattr(internal_client, "dispatch_async", dispatch)
    listing = call("GET", "/api/agents").get_json()
    assert state.agent.name in {agent["name"] for agent in listing["agents"]}
    assert call("GET", f"/api/agents/{state.agent.name}").status_code == 200
    assert call("POST", "/api/agents/default", {"name": state.agent.name}).status_code == 200
    assert call("PATCH", f"/api/agents/{state.agent.name}", {"description": "成员管理 α"}).status_code == 200
    assert state.store.require(state.agent.name).description == "成员管理 α"
    assert (
        call(
            "POST", "/api/sessions", {"project_id": state.project["id"], "agent_name": state.agent.name}, csrf=False
        ).status_code
        == 403
    )
    created = call("POST", "/api/sessions", {"project_id": state.project["id"], "agent_name": state.agent.name})
    assert created.status_code == 201, created.get_json()
    current = created.get_json()
    visible = {row["id"] for row in call("GET", "/api/sessions").get_json()["sessions"]}
    assert {current["id"], *(session["id"] for session in state.sessions)} <= visible
    show = ShowPageStore()
    try:
        for session in [*state.sessions, current]:
            session_id = session["id"]
            assert call("GET", f"/api/sessions/{session_id}").status_code == 200
            changed = call("PATCH", f"/api/sessions/{session_id}", {"title": "改名 α"})
            assert changed.status_code == 200, changed.get_json()
            sent = call(
                "POST", f"/api/sessions/{session_id}/messages", {"text": "你好 α", "author_id": "remote:spoofed"}
            )
            assert sent.status_code == 201, sent.get_json()
            assert sent.get_json()["author_id"] == "remote:operator"
            assert dispatch.await_args.args[0]["session_id"] == session_id
            with state.engine.connect() as conn:
                row = conn.execute(select(messages).where(messages.c.id == sent.get_json()["id"])).mappings().one()
                metadata = json.loads(row["metadata_json"])
                assert row["author_id"] == "remote:operator"
                assert metadata["_web_push_user_key"] == "remote:operator"
                assert project_access_service.get_effective_session_role(conn, state.context(), session_id) == "member"
            page = show.ensure(session_id, user_context=state.context())
            assert show.get_for_use(session_id, user_context=state.context()).session_id == page.session_id
    finally:
        show.close()
    monkeypatch.setattr(internal_client, "turn_state", AsyncMock(return_value={"body": {"in_flight": False}}))
    forked = call("POST", f"/api/sessions/{state.sessions[1]['id']}/fork", {})
    assert forked.status_code == 201, forked.get_json()
    assert call("GET", f"/api/sessions/{forked.get_json()['id']}").status_code == 200
    ensure_agent_name_access(state.agent.name, user_context=state.context())
    with pytest.raises(ValueError):
        ensure_agent_name_access("missing-agent", user_context=state.context())
    assert not state.context().is_instance_owner
    assert _policies(state) == before


@pytest.mark.parametrize("shape", ["absent", "private", "foreign", "unmatched", "legacy"])
def test_permissions_014_resource_policy_shapes_preserve_rows(operations, shape):
    state = operations
    if shape != "absent":
        with state.engine.begin() as conn:
            resource_access_service.ensure_resource_policy(
                conn,
                resource_kind="agent",
                resource_id=state.agent.id,
                owner_user_id="someone-else",
                organization_id="foreign-org" if shape == "foreign" else "org-1",
                access_level="scope" if shape == "unmatched" else "private",
                group_ids=["other"] if shape == "unmatched" else [],
            )
            if shape == "legacy":
                conn.execute(
                    resource_access_policies.update()
                    .where(resource_access_policies.c.resource_id == state.agent.id)
                    .values(organization_id=None, owner_user_id=None)
                )
    before = _policies(state)
    assert state.store.require_accessible(state.agent.name, user_context=state.context()).id == state.agent.id
    for role in ("editor", "viewer"):
        context = state.context(role)
        if role == "editor" and state.kind == "personal":
            assert state.store.require_accessible(state.agent.name, user_context=context)
        else:
            with pytest.raises(VibeAgentAccessError):
                state.store.require_accessible(state.agent.name, user_context=context)
    assert _policies(state) == before


def test_permissions_015_access_administration_and_missing_resources(operations, monkeypatch):
    """PERMISSIONS-015: manager is neither owner nor a new access grant."""
    state = operations
    call = state.client()
    before = _policies(state)
    for method, path, body in [
        ("PUT", "/api/permissions/authorized-users", {"entries": []}),
        ("PUT", f"/api/permissions/projects/{state.project['id']}/access", {"mode": "inherit"}),
        ("PUT", f"/api/permissions/resources/agent/{state.agent.id}/access", {"access_level": "public"}),
        ("POST", "/api/remote-access/vibe-cloud/pair", {"pairing_key": "synthetic"}),
        ("POST", "/api/agent-onboarding", {}),
        ("POST", "/api/bind-codes", {}),
    ]:
        denied = call(method, path, body)
        assert denied.status_code == 403, (path, denied.get_json())
    assert call("GET", "/api/sessions/missing-session").status_code == 404
    with state.engine.connect() as conn:
        assert project_access_service.get_effective_project_role(conn, state.context(), "missing") is None
        assert project_access_service.get_effective_session_role(conn, state.context(), "missing") is None
    state.store.update(state.agent.name, enabled=False)
    denied = call("POST", "/api/sessions", {"project_id": state.project["id"], "agent_name": state.agent.name})
    assert denied.status_code == 404, denied.get_json()
    assert _policies(state) == before


@pytest.mark.parametrize("role", ["member", "owner", "editor", "viewer"])
def test_permissions_016_real_sse_management_and_session_delivery(operations, role):
    """PERMISSIONS-016: publisher payloads traverse both stream gates."""
    state = operations
    events = [
        (
            "vaults.updated",
            vaults_updated_payload(scope="requests", request_id="request-α", secret_name="SECRET_α"),
        ),
        ("definitions.updated", {"definition_type": "scheduled"}),
        ("runs.updated", run_updated_payload(run_id="run-α", status="running")),
        # The same instance-wide invalidation with the optional session id a
        # publisher may attach: admission must not start depending on it.
        (
            "runs.updated",
            run_updated_payload(run_id="run-β", status="running", session_id=state.sessions[0]["id"]),
        ),
        ("remote_access.quality.changed", {"state": "healthy", "sampled_at": "2026-09-17T14:00:00Z"}),
        ("message.updated", {"session_id": state.sessions[1]["id"], "id": "message-α"}),
        ("session.status", {"session_id": state.sessions[0]["id"], "status": "running"}),
    ]

    async def collect():
        with ui_server.app.test_request_context("/api/events"):
            g.authorization_context = state.context(role)
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                for _ in range(3):
                    await iterator.__anext__()
                for event_type, data in events:
                    broker.publish(event_type, data)
                broker.publish("future.management.event", {"secret": "not-a-known-event"})
                broker.publish("projects.changed", {})
                frames = []
                while True:
                    frame = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                    if isinstance(frame, bytes):
                        frame = frame.decode()
                    if "event: projects.changed\n" in frame:
                        return frames
                    frames.append(frame)
            finally:
                await iterator.aclose()

    frames = asyncio.run(collect())
    delivered = [
        json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: "))) for frame in frames
    ]
    if role in {"member", "owner"}:
        assert [(frame["type"], frame["data"]) for frame in delivered[: len(events)]] == events
        assert len(delivered) == len(events) + (role == "owner")
    elif role == "editor":
        # An Editor uses Harness, Vault and Runs, so the instance-wide
        # invalidations reach them as bare signals they refetch under their own
        # authority. Empty data is the assertion that no publisher identifier —
        # secret name or session id — rides along. Everything else is unchanged:
        # link quality stays management, the unknown event stays Owner-only, and
        # session frames still follow the Project ACL.
        invalidations = [
            (event_type, {}) for event_type, _ in events if event_type in INSTANCE_SCOPED_REFETCH_EVENTS
        ]
        reachable_sessions = [events[-1]] if state.kind == "personal" else []
        assert [(frame["type"], frame["data"]) for frame in delivered] == [*invalidations, *reachable_sessions]
    else:
        assert delivered == []


def test_permissions_017_notifications_keep_recipient_identity(operations):
    """PERMISSIONS-017: visibility widens without changing subscription identity."""
    state = operations
    contexts = {"remote:operator": state.context(), "remote:other": state.context("viewer", "other")}
    for subject in ("operator", "other"):
        subscribed = state.client(subject=subject)(
            "POST",
            "/api/web-push/subscriptions",
            {
                "subscription": {
                    "endpoint": f"https://push.example/{subject}",
                    "keys": {"p256dh": "test-key", "auth": "test-auth"},
                },
                "device_id": "same-device-id",
            },
        )
        assert subscribed.status_code == 200
        assert subscribed.get_json()["subscription"]["user_key"] == f"remote:{subject}"
    from storage.models import web_push_subscriptions

    with state.engine.connect() as conn:
        assert set(conn.execute(select(web_push_subscriptions.c.user_key)).scalars()) == {
            "remote:operator",
            "remote:other",
        }
        assert _filter_project_authorized_user_keys(
            conn, user_keys=list(contexts), contexts=contexts, session_id=state.sessions[0]["id"]
        ) == ["remote:operator"]
        assert _badge_count_for_user_key(conn, user_key="remote:operator", contexts=contexts) == len(state.sessions)
        assert _badge_count_for_user_key(conn, user_key="remote:other", contexts=contexts) == 0
        assert _badge_count_for_user_key(conn, user_key="remote:unknown", contexts=contexts) == 0


def test_permissions_014_runtime_discovery_and_harness_bindings(operations, tmp_path, monkeypatch):
    state = operations
    call = state.client()
    live = [
        {
            "session_id": row["id"],
            "agent_id": state.agent.id,
            "agent_name": state.agent.name,
            "backend": state.agent.backend,
            "state": "active",
        }
        for row in state.sessions
    ]
    monkeypatch.setattr(
        internal_client,
        "list_running_agents",
        AsyncMock(return_value={"status_code": 200, "body": {"agents": live, "counts": {"total": len(live)}}}),
    )
    assert call("GET", "/api/running-agents").get_json()["agents"] == live
    graph = call("GET", "/api/agents-graph").get_json()
    assert {row["id"] for row in state.sessions} <= {node["session_id"] for node in graph["nodes"]}
    inbox = call("GET", "/api/inbox").get_json()
    assert {row["id"] for row in state.sessions} <= {item["session_id"] for item in inbox["sessions"]}
    search = call("GET", "/api/search/messages?q=实例内容").get_json()
    assert {row["id"] for row in state.sessions} <= {item["session_id"] for item in search["sessions"]}
    from core.scheduled_tasks import ScheduledTaskStore
    from core.watches import ManagedWatchStore

    tasks = ScheduledTaskStore(tmp_path / "tasks.json")
    watches = ManagedWatchStore(tmp_path / "watches.json")
    task = tasks.add_task(
        session_key=state.project["scope_id"],
        prompt="test",
        schedule_type="cron",
        agent_name=state.agent.name,
        cron="0 * * * *",
        timezone_name="UTC",
        user_context=state.context(),
    )
    watch = watches.add_watch(
        name="test",
        session_key=state.project["scope_id"],
        command=["true"],
        shell_command=None,
        prefix=None,
        cwd=str(tmp_path),
        mode="once",
        timeout_seconds=1,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=1,
        post_to=None,
        deliver_key=None,
        agent_name=state.agent.name,
        user_context=state.context(),
    )
    assert task.agent_name == watch.agent_name == state.agent.name
    assert tasks.get_task(task.id).agent_name == state.agent.name
    assert watches.get_watch(watch.id).agent_name == state.agent.name


def test_permissions_015_archived_sessions_remain_inert(operations, monkeypatch):
    state = operations
    call = state.client()
    session_id = state.sessions[1]["id"]
    before = _policies(state)
    with state.engine.begin() as conn:
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == session_id).values(status="archived"))
    dispatch = AsyncMock()
    monkeypatch.setattr(internal_client, "dispatch_async", dispatch)
    monkeypatch.setattr(internal_client, "turn_state", AsyncMock(return_value={"body": {"in_flight": False}}))
    assert call("GET", f"/api/sessions/{session_id}").status_code == 200
    assert session_id not in {row["id"] for row in call("GET", "/api/sessions?status=active").get_json()["sessions"]}
    for method, path, payload in [
        ("PATCH", f"/api/sessions/{session_id}", {"title": "must not change"}),
        ("POST", f"/api/sessions/{session_id}/messages", {"text": "must not run"}),
        ("POST", f"/api/sessions/{session_id}/fork", {}),
    ]:
        denied = call(method, path, payload)
        assert denied.status_code == 409, denied.get_json()
    dispatch.assert_not_awaited()
    with state.engine.connect() as conn:
        assert (
            conn.execute(select(agent_sessions.c.title).where(agent_sessions.c.id == session_id)).scalar_one()
            == "旧会话 α"
        )
    assert _policies(state) == before


def test_permissions_014_member_restores_archived_project_and_deferred_authority(operations, monkeypatch):
    from core.session_turns import SessionTurnManager
    from storage import message_deliveries

    state = operations
    call = state.client()
    project_id = state.project["id"]
    session_id = state.sessions[0]["id"]
    before = _policies(state)
    dispatch = AsyncMock(return_value={"status_code": 202, "body": {"ok": True}})
    monkeypatch.setattr(internal_client, "dispatch_async", dispatch)
    monkeypatch.setattr(internal_client, "turn_state", AsyncMock(return_value={"body": {"in_flight": False}}))
    sent = call("POST", f"/api/sessions/{session_id}/messages", {"text": "queued before project archive"})
    assert sent.status_code == 202
    delivery_id = dispatch.await_args.args[0]["user_message_id"]
    assert call("DELETE", f"/api/projects/{project_id}").status_code == 200
    assert project_id not in {row["id"] for row in call("GET", "/api/projects").get_json()["projects"]}
    assert project_id in {row["id"] for row in call("GET", "/api/projects?include_archived=1").get_json()["projects"]}
    assert call("GET", f"/api/projects/{project_id}").status_code == 200
    for role in ("viewer", "editor"):
        assert state.client(role, subject=role)("GET", f"/api/projects/{project_id}").status_code == 404
    with state.engine.connect() as conn:
        assert project_access_service.get_effective_project_role(conn, state.context(), project_id) == "member"
        assert project_access_service.can_manage_project(conn, state.context(), project_id)
        assert project_access_service.get_effective_project_role(conn, state.context(), "missing") is None
        delivery = message_deliveries.get_delivery(conn, delivery_id)
        assert SessionTurnManager._remote_delivery_execution_denial(conn, delivery) is None
    show = ShowPageStore()
    try:
        assert show.ensure(session_id, user_context=state.context()).session_id == session_id
        assert show.get_for_use(session_id, user_context=state.context()).session_id == session_id
    finally:
        show.close()
    forked = call("POST", f"/api/sessions/{session_id}/fork", {})
    assert forked.status_code == 201, forked.get_json()
    reopened = call("POST", "/api/projects", {"folder_path": state.project["folder_path"]})
    assert reopened.status_code == 201, reopened.get_json()
    assert reopened.get_json()["id"] == project_id
    assert project_id in {row["id"] for row in call("GET", "/api/projects").get_json()["projects"]}
    assert _policies(state) == before
    # Operational Member scope does not bypass the deferred pairing fence.
    state.config.remote_access.vibe_cloud.instance_id = "another-instance"
    state.config.save()
    with state.engine.connect() as conn:
        assert SessionTurnManager._remote_delivery_execution_denial(conn, delivery) == "remote_chat_access_forbidden"
