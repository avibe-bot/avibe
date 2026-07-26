from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore
from core.vibe_agents import AgentImportCandidate, VibeAgent, VibeAgentAccessError, VibeAgentStore
from core.watches import ManagedWatchStore
from storage import resource_access_service, workbench_sessions_service
from storage.db import get_cached_sqlite_engine
from storage.models import resource_access_groups, resource_access_policies
from storage.settings_service import upsert_scope
from tests.test_ui_remote_access_auth import _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access
from vibe.ui_server import app


def _organization_context(
    subject: str,
    *,
    group_ids: frozenset[str] | None = frozenset({"group-engineering"}),
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role="member",
        group_ids=group_ids,
        instance_role="viewer",
        instance_access_source="organization_group",
        is_remote=True,
    )


def _organization_cookie(
    config,
    *,
    subject: str,
    groups: list[str] | None = None,
    instance_role: str = "viewer",
) -> str:
    claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_role": instance_role,
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": f"member-{subject}",
        "vibe_organization_role": "member",
        "vibe_membership_version": "membership-v2",
    }
    if groups is not None:
        claims["vibe_group_ids"] = groups
    return remote_access.make_session_cookie(
        config,
        f"{subject}@example.com",
        subject,
        session_claims=claims,
    )


def _seed_agents_with_policies() -> tuple[VibeAgentStore, dict[str, VibeAgent]]:
    store = VibeAgentStore()
    agents = {
        "private": store.create(name="private-agent", backend="codex"),
        "public": store.create(name="public-agent", backend="codex"),
        "scope": store.create(name="scope-agent", backend="codex"),
    }
    with store.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id=agents["private"].id,
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="private",
        )
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id=agents["public"].id,
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="public",
        )
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id=agents["scope"].id,
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="scope",
            group_ids=["group-engineering"],
        )
    return store, agents


def test_agent_catalog_filters_private_public_scope_and_missing_group_context(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store, _agents = _seed_agents_with_policies()
    try:
        owner_names = {agent.name for agent in store.list_agents(user_context=_organization_context("owner-1"))}
        member_names = {agent.name for agent in store.list_agents(user_context=_organization_context("member-1"))}
        no_group_names = {
            agent.name
            for agent in store.list_agents(user_context=_organization_context("member-2", group_ids=None))
        }
    finally:
        store.close()

    assert owner_names == {"private-agent", "public-agent", "scope-agent"}
    assert member_names == {"public-agent", "scope-agent"}
    assert no_group_names == {"public-agent"}


def test_agent_removal_deletes_resource_policy_and_groups(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        agent = store.create(name="removed-agent", backend="codex")
        with store.engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="agent",
                resource_id=agent.id,
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="scope",
                group_ids=["group-engineering"],
            )

        assert store.remove(agent.name) is True
        with store.engine.connect() as connection:
            policies = connection.execute(
                select(resource_access_policies).where(
                    resource_access_policies.c.resource_kind == "agent",
                    resource_access_policies.c.resource_id == agent.id,
                )
            ).all()
            groups = connection.execute(
                select(resource_access_groups).where(
                    resource_access_groups.c.resource_kind == "agent",
                    resource_access_groups.c.resource_id == agent.id,
                )
            ).all()
    finally:
        store.close()

    assert policies == []
    assert groups == []


def test_remote_agent_creation_defaults_to_private_organization_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="member-1",
            groups=["group-engineering"],
            instance_role="owner",
        ),
        domain="alex.avibe.bot",
    )

    response = client.post(
        "/api/agents",
        json={"name": "remote-private", "backend": "codex"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    agent_id = response.get_json()["agent"]["id"]
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", agent_id, connection=connection)
    assert policy is not None
    assert policy["organization_id"] == "org-1"
    assert policy["owner_user_id"] == "member-1"
    assert policy["access_level"] == "private"


def test_remote_partial_agent_updates_persist_canonical_selector_pair(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store, agents = _seed_agents_with_policies()
    store.close()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        scope_id = upsert_scope(
            connection,
            platform="avibe",
            scope_type="project",
            native_id="proj_partial_agent",
            now="2026-07-20T00:00:00Z",
        )
        session = workbench_sessions_service.create_session(
            connection,
            scope_id=scope_id,
            agent_backend="",
            agent_id=agents["public"].id,
            user_context=_organization_context("member-1"),
        )
        by_name = workbench_sessions_service.update_session(
            connection,
            session["id"],
            agent_name=agents["scope"].name,
            user_context=_organization_context("member-1"),
        )
        by_id = workbench_sessions_service.update_session(
            connection,
            session["id"],
            agent_id=agents["public"].id,
            user_context=_organization_context("member-1"),
        )

    assert (session["agent_id"], session["agent_name"], session["agent_backend"]) == (
        agents["public"].id,
        agents["public"].name,
        agents["public"].backend,
    )
    assert (by_name["agent_id"], by_name["agent_name"]) == (
        agents["scope"].id,
        agents["scope"].name,
    )
    assert (by_id["agent_id"], by_id["agent_name"]) == (
        agents["public"].id,
        agents["public"].name,
    )


def test_remote_agent_request_and_selection_reject_inaccessible_agent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store, agents = _seed_agents_with_policies()
    private_agent = agents["private"]
    public_agent = agents["public"]
    store.set_default_agent_name(private_agent.name)
    store.close()
    context = _organization_context("member-1")

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="member-1",
            groups=["group-engineering"],
            instance_role="editor",
        ),
        domain="alex.avibe.bot",
    )
    response = client.get(
        "/api/agents/private-agent",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "instance_access_forbidden"
    catalog = client.get(
        "/api/agents",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    public_mutation = client.patch(
        "/api/agents/public-agent",
        json={"description": "not allowed"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert catalog.status_code == 200
    assert {agent["name"] for agent in catalog.get_json()["agents"]} == {"public-agent", "scope-agent"}
    assert public_mutation.status_code == 403
    assert public_mutation.get_json()["error"] == "instance_access_forbidden"
    default_mutation = client.post(
        "/api/agents/default",
        json={"name": public_agent.name},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert default_mutation.status_code == 403
    assert default_mutation.get_json()["error"] == "instance_access_forbidden"
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == private_agent.name
    finally:
        store.close()

    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        scope_id = upsert_scope(
            connection,
            platform="avibe",
            scope_type="project",
            native_id="proj_acl_agents",
            now="2026-07-20T00:00:00Z",
        )
        with pytest.raises(VibeAgentAccessError):
            workbench_sessions_service.create_session(
                connection,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name=private_agent.name,
                agent_id=private_agent.id,
                user_context=context,
            )
        with pytest.raises(VibeAgentAccessError):
            workbench_sessions_service.create_session(
                connection,
                scope_id=scope_id,
                agent_backend="",
                user_context=context,
            )
        with pytest.raises(VibeAgentAccessError):
            workbench_sessions_service.create_session(
                connection,
                scope_id=scope_id,
                agent_backend="codex",
                user_context=context,
            )
        backend_only_session = workbench_sessions_service.create_session(
            connection,
            scope_id=scope_id,
            agent_backend="codex",
            user_context=resource_access_service.ResourceUserContext(is_trusted_local=True),
        )
        with pytest.raises(VibeAgentAccessError):
            workbench_sessions_service.update_session(
                connection,
                backend_only_session["id"],
                agent_backend="claude",
                agent_name=None,
                user_context=context,
            )
    blocked_default = client.post(
        "/api/sessions",
        json={"project_id": "proj_acl_agents"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert blocked_default.status_code == 403
    blocked_backend_only = client.post(
        "/api/sessions",
        json={"project_id": "proj_acl_agents", "agent_backend": "codex"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert blocked_backend_only.status_code == 403

    store = VibeAgentStore()
    try:
        store.set_default_agent_name(public_agent.name)
    finally:
        store.close()
    allowed_default = client.post(
        "/api/sessions",
        json={"project_id": "proj_acl_agents"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert allowed_default.status_code == 201
    session = allowed_default.get_json()
    assert session["agent_id"] == public_agent.id
    assert session["agent_name"] == public_agent.name
    assert session["agent_backend"] == public_agent.backend

    dispatch_calls = []

    async def dispatch_async(payload):
        dispatch_calls.append(payload)
        return {"status_code": 202, "body": {}}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch_async)
    blocked_legacy_turn = client.post(
        f"/api/sessions/{backend_only_session['id']}/messages",
        json={"text": "must not dispatch"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert blocked_legacy_turn.status_code == 403
    assert blocked_legacy_turn.get_json()["code"] == "agent_access_forbidden"
    assert dispatch_calls == []

    with engine.begin() as connection:
        resource_access_service.apply_control_plane_intent(
            connection,
            organization_id="org-1",
            resource_kind="agent",
            resource_id=public_agent.id,
            revision=1,
            access_level="private",
            group_ids=[],
        )
    revoked_turn = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"text": "must not dispatch"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert revoked_turn.status_code == 403
    assert revoked_turn.get_json()["code"] == "agent_access_forbidden"
    assert dispatch_calls == []

    with pytest.raises(VibeAgentAccessError):
        ScheduledTaskStore(tmp_path / "tasks.json").add_task(
            session_key="avibe::project::proj_acl_agents",
            prompt="run",
            schedule_type="cron",
            agent_name=private_agent.name,
            cron="0 * * * *",
            timezone_name="UTC",
            user_context=context,
        )
    with pytest.raises(VibeAgentAccessError):
        ManagedWatchStore(tmp_path / "watches.json").add_watch(
            name="acl watch",
            session_key="avibe::project::proj_acl_agents",
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
            agent_name=private_agent.name,
            user_context=context,
        )


def test_remote_external_guest_cannot_create_agent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        with pytest.raises(VibeAgentAccessError):
            store.create(
                name="guest-agent",
                backend="codex",
                user_context=resource_access_service.ResourceUserContext(
                    subject="guest-1",
                    instance_role="viewer",
                    instance_access_source="email",
                    is_remote=True,
                ),
            )
        with pytest.raises(VibeAgentAccessError):
            store.import_candidates(
                [AgentImportCandidate(name="guest-import", backend="codex")],
                user_context=resource_access_service.ResourceUserContext(
                    subject="guest-1",
                    instance_role="viewer",
                    instance_access_source="email",
                    is_remote=True,
                ),
            )
    finally:
        store.close()


def test_remote_background_definitions_recheck_agent_acl_before_dispatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    agent_store, agents = _seed_agents_with_policies()
    context = _organization_context("member-1")
    task_store = ScheduledTaskStore(tmp_path / "tasks.json")
    watch_store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    try:
        task = task_store.add_task(
            session_key="slack::channel::C123",
            prompt="run task",
            schedule_type="cron",
            agent_name=agents["public"].name,
            cron="0 * * * *",
            timezone_name="UTC",
            metadata={
                resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
                    "sub": "owner-1",
                }
            },
            user_context=context,
        )
        watch = watch_store.add_watch(
            name="acl watch",
            session_key="slack::channel::C123",
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
            agent_name=agents["public"].name,
            user_context=context,
        )
        assert task.metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]["sub"] == "member-1"
        assert watch.metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]["sub"] == "member-1"

        with agent_store.engine.begin() as connection:
            resource_access_service.apply_control_plane_intent(
                connection,
                organization_id="org-1",
                resource_kind="agent",
                resource_id=agents["public"].id,
                revision=1,
                access_level="private",
                group_ids=[],
            )

        service = ScheduledTaskService(
            controller=SimpleNamespace(),
            store=task_store,
            request_store=request_store,
        )
        task_result = asyncio.run(
            service._execute_task(task, execution_id="task-run", disable_one_shot=False)
        )
        assert task_result.error == "Agent access is not permitted."

        request = request_store.enqueue_hook_send(
            session_key=watch.session_key,
            prompt="run watch",
            agent_name=watch.agent_name,
            session_policy=watch.session_policy,
            run_type="watch",
            definition_id=watch.id,
            source_kind="watch",
            metadata=watch.metadata,
        )
        claimed = request_store.claim(request.id)
        assert claimed is not None
        asyncio.run(service._execute_claimed_request(claimed))
        completed = request_store.get_run(request.id)
        assert completed is not None
        assert completed["status"] == "failed"
        assert completed["error"] == "Agent access is not permitted."
    finally:
        agent_store.close()
