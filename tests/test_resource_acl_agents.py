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
    instance_role: str = "editor",
    organization_role: str = "member",
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role=organization_role,
        group_ids=group_ids,
        instance_role=instance_role,
        instance_access_source="organization_group",
        is_remote=True,
    )


def _organization_cookie(
    config,
    *,
    subject: str,
    groups: list[str] | None = None,
    instance_role: str = "viewer",
    organization_role: str = "member",
) -> str:
    claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_role": instance_role,
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": f"member-{subject}",
        "vibe_organization_role": organization_role,
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


def test_active_org_agent_catalog_includes_every_builtin_and_acl_shape(monkeypatch, tmp_path) -> None:
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
    assert member_names == {"private-agent", "public-agent", "scope-agent"}
    assert no_group_names == {"private-agent", "public-agent", "scope-agent"}


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


def test_active_org_agent_creation_is_allowed_without_trusted_local_identity(monkeypatch, tmp_path) -> None:
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

    assert response.status_code in {200, 201}
    store = VibeAgentStore()
    try:
        assert store.get("remote-private") is not None
    finally:
        store.close()


def test_active_org_agent_management_is_allowed_inside_the_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store, agents = _seed_agents_with_policies()
    # Resource ACL management stays reserved to owner/admin Organization roles
    # under the temporary full-access rollout (see #1343); a plain member has
    # no management authority.
    member = _organization_context("member-1")
    admin = resource_access_service.ResourceUserContext(
        subject="admin-1",
        email="admin-1@example.com",
        organization_id="org-1",
        organization_member_id="member-admin-1",
        organization_role="admin",
        group_ids=frozenset({"group-engineering"}),
        instance_role="owner",
        instance_access_source="organization_group",
        is_remote=True,
    )
    owner = _organization_context("owner-1", instance_role="owner")
    try:
        with pytest.raises(VibeAgentAccessError):
            store.update(agents["public"].name, description="member update", user_context=member)
        with pytest.raises(VibeAgentAccessError):
            store.remove(agents["public"].name, user_context=member)
        updated = store.update(agents["public"].name, description="admin update", user_context=admin)
        assert updated.description == "admin update"
        store.set_default_agent_name(agents["private"].name, user_context=admin)
        assert store.get_default_agent_name() == agents["private"].name
        assert store.remove(agents["public"].name, user_context=admin) is True
        owner_updated = store.update(
            agents["private"].name,
            description="owner update",
            user_context=owner,
        )
        assert owner_updated.description == "owner update"
    finally:
        store.close()


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


def test_active_org_agent_detail_uses_full_runtime_projection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store = VibeAgentStore()
    try:
        agent = store.create(
            name="imported-agent",
            backend="codex",
            system_prompt="Safe remote prompt",
            source="file",
            source_ref="/Users/alex/private/AGENT.md",
            metadata={"import_path": "/Users/alex/private", "secret": "local-only"},
        )
        with store.engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="agent",
                resource_id=agent.id,
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="private",
            )
    finally:
        store.close()

    remote = app.test_client()
    remote.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="owner-1", groups=[], instance_role="owner"),
        domain="alex.avibe.bot",
    )
    remote_response = remote.get(
        "/api/agents/imported-agent",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    local_response = app.test_client().get("/api/agents/imported-agent")

    assert remote_response.status_code == 200
    remote_agent = remote_response.get_json()["agent"]
    assert remote_agent["system_prompt"] == "Safe remote prompt"
    assert remote_agent["metadata"]["secret"] == "local-only"
    assert remote_agent["source_ref"] == "/Users/alex/private/AGENT.md"
    assert remote_agent["normalized_name"] == "imported-agent"
    assert local_response.get_json()["agent"]["source_ref"] == "/Users/alex/private/AGENT.md"
    assert local_response.get_json()["agent"]["metadata"]["secret"] == "local-only"


def test_active_org_agent_selection_and_harness_bindings_are_allowed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store, agents = _seed_agents_with_policies()
    private_agent = agents["private"]
    store.set_default_agent_name(private_agent.name)
    store.close()
    context = _organization_context("member-1")

    # Agent management stays reserved to admin/owner Organization roles under
    # the temporary full-access rollout (see #1343). The reads and catalog
    # remain open to all active members.
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="member-1", groups=[], instance_role="editor"),
        domain="alex.avibe.bot",
    )
    response = client.get(
        "/api/agents/private-agent",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert response.status_code == 200
    catalog = client.get("/api/agents", base_url="https://alex.avibe.bot", environ_base=_remote_peer())
    assert catalog.status_code == 200
    assert {"private-agent", "public-agent", "scope-agent"}.issubset(
        {agent["name"] for agent in catalog.get_json()["agents"]}
    )
    # A plain active Organization member is denied mutations under the
    # Resource ACL boundary.
    denied_mutation = client.patch(
        "/api/agents/public-agent",
        json={"description": "member update"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert denied_mutation.status_code == 403
    # An active Organization admin/owner can manage.
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="admin-1",
            groups=[],
            instance_role="owner",
            organization_role="admin",
        ),
        domain="alex.avibe.bot",
    )
    mutation = client.patch(
        "/api/agents/public-agent",
        json={"description": "admin update"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert mutation.status_code == 200
    default_mutation = client.post(
        "/api/agents/default",
        json={"name": private_agent.name},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert default_mutation.status_code == 200

    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        scope_id = upsert_scope(
            connection,
            platform="avibe",
            scope_type="project",
            native_id="proj_acl_agents",
            now="2026-07-20T00:00:00Z",
        )
        session = workbench_sessions_service.create_session(
            connection,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name=private_agent.name,
            agent_id=private_agent.id,
            user_context=context,
        )
        assert session["agent_id"] == private_agent.id
        backend_only = workbench_sessions_service.create_session(
            connection,
            scope_id=scope_id,
            agent_backend="codex",
            user_context=context,
        )
        assert backend_only["agent_backend"] == "codex"

    task_store = ScheduledTaskStore(tmp_path / "tasks.json")
    watch_store = ManagedWatchStore(tmp_path / "watches.json")
    try:
        task = task_store.add_task(
            session_key="avibe::project::proj_acl_agents",
            prompt="run",
            schedule_type="cron",
            agent_name=private_agent.name,
            cron="0 * * * *",
            timezone_name="UTC",
            user_context=context,
        )
        watch = watch_store.add_watch(
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
        assert task.agent_name == private_agent.name
        assert watch.agent_name == private_agent.name
    finally:
        pass


def test_non_member_cannot_create_agent_but_active_org_member_can(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        created = store.create(
            name="editor-agent",
            backend="codex",
            user_context=_organization_context("member-1"),
        )
        assert created.name == "editor-agent"
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


def test_active_org_background_definitions_are_allowed_but_legacy_rows_fail_before_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
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
        assert task.agent_name == agents["public"].name
        assert watch.agent_name == agents["public"].name
        assert resource_access_service.metadata_allows_temporary_unrestricted_runtime(
            task.metadata
        )
        assert resource_access_service.metadata_allows_temporary_unrestricted_runtime(
            watch.metadata
        )
        restored = resource_access_service.resource_user_context_from_metadata(
            task.metadata
        )
        assert restored is not None
        assert restored.subject == context.subject
        assert restored.is_remote is True
        assert restored.is_trusted_local is False

        task = task_store.add_task(
            session_key="slack::channel::C123",
            prompt="run task",
            schedule_type="cron",
            agent_name=agents["public"].name,
            cron="0 * * * *",
            timezone_name="UTC",
            shell_command="touch should-not-run",
        )
        task.metadata = {
            resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
                "sub": "legacy-remote-user"
            }
        }
        task_store.upsert_task(task)
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
        )
        watch.metadata = {
            resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
                "sub": "legacy-remote-user"
            }
        }
        watch_store.upsert_watch(watch)

        service = ScheduledTaskService(
            controller=SimpleNamespace(),
            store=task_store,
            request_store=request_store,
        )
        task_result = asyncio.run(
            service._execute_task(task, execution_id="task-run", disable_one_shot=False)
        )
        assert task_result.error == "remote_autonomous_harness_disabled"
        stored_task = task_store.get_task(task.id)
        assert stored_task is not None
        assert stored_task.enabled is False
        assert stored_task.last_error == "remote_autonomous_harness_disabled"
        assert not (tmp_path / "should-not-run").exists()

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
        assert completed["error"] == "remote_autonomous_harness_disabled"
    finally:
        agent_store.close()
