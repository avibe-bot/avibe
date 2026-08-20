from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore
from core.vibe_agents import (
    AgentImportCandidate,
    VibeAgent,
    VibeAgentAccessError,
    VibeAgentDefaultAudienceError,
    VibeAgentStore,
    ensure_agent_selection_access,
    ensure_session_agent_access,
)
from core.watches import ManagedWatchStore
from storage import resource_access_service, workbench_sessions_service
from storage.db import get_cached_sqlite_engine
from storage.models import resource_access_groups, resource_access_policies
from storage.settings_service import upsert_scope
from tests.ui_server_test_helpers import _remote_peer, _save_config
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
        "vibe_instance_authorization_revision": (
            remote_access.current_authorization_revision(config) or 0
        ),
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


def _seed_legacy_default_agent(store: VibeAgentStore, agent_name: str) -> None:
    """Persist an instance default the setter now refuses.

    ``set_default_agent_name`` rejects a single-subject Agent as the instance
    default (``core.vibe_agents.default_routing_audience_error``), so this state
    can now only arrive from a database written before that rule. The read-path
    fences must still fail closed on it, which is what the callers below assert.
    """

    with store.engine.begin() as connection:
        store._write_default_agent_name(  # noqa: SLF001
            connection,
            agent_name,
            now="2026-07-20T00:00:00Z",
        )


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


def test_owner_can_create_agent_without_a_trusted_local_identity(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path, paired=True, instance_kind="organization")
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="owner-1",
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
        store.set_default_agent_name(agents["public"].name, user_context=admin)
        assert store.get_default_agent_name() == agents["public"].name
        # Management authority does not extend to publishing a single-subject
        # Agent as the instance-wide default route.
        with pytest.raises(VibeAgentDefaultAudienceError):
            store.set_default_agent_name(agents["private"].name, user_context=admin)
        assert store.get_default_agent_name() == agents["public"].name
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


def test_editor_clearing_session_agent_authorizes_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store, agents = _seed_agents_with_policies()
    try:
        _seed_legacy_default_agent(store, agents["private"].name)
        engine = get_cached_sqlite_engine()
        with engine.begin() as connection:
            scope_id = upsert_scope(
                connection,
                platform="avibe",
                scope_type="project",
                native_id="proj_clear_default",
                now="2026-07-20T00:00:00Z",
            )
            session = workbench_sessions_service.create_session(
                connection,
                scope_id=scope_id,
                agent_backend="",
                agent_id=agents["public"].id,
                user_context=_organization_context("member-1"),
            )
        with pytest.raises(VibeAgentAccessError):
            with engine.begin() as connection:
                workbench_sessions_service.update_session(
                    connection,
                    session["id"],
                    agent_name="",
                    agent_id="",
                    agent_backend="",
                    user_context=_organization_context("member-1"),
                )
        store.set_default_agent_name(agents["public"].name)
        with engine.begin() as connection:
            cleared = workbench_sessions_service.update_session(
                connection,
                session["id"],
                agent_name="",
                agent_id="",
                agent_backend="",
                user_context=_organization_context("member-1"),
            )
        store.set_default_agent_name(agents["scope"].name)
        with engine.connect() as connection:
            effective = ensure_session_agent_access(
                connection,
                cleared,
                user_context=_organization_context("member-1"),
            )
    finally:
        store.close()

    assert (cleared["agent_id"], cleared["agent_name"], cleared["agent_backend"]) == (
        None,
        None,
        "",
    )
    assert effective is not None
    assert effective.id == agents["scope"].id


def test_editor_creating_default_session_validates_without_pinning(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store, agents = _seed_agents_with_policies()
    engine = get_cached_sqlite_engine()
    try:
        with engine.begin() as connection:
            scope_id = upsert_scope(
                connection,
                platform="avibe",
                scope_type="project",
                native_id="proj_create_default",
                now="2026-08-13T00:00:00Z",
            )

        _seed_legacy_default_agent(store, agents["private"].name)
        with pytest.raises(VibeAgentAccessError):
            with engine.begin() as connection:
                workbench_sessions_service.create_session(
                    connection,
                    scope_id=scope_id,
                    agent_backend="",
                    user_context=_organization_context("member-1"),
                )

        store.set_default_agent_name(agents["public"].name)
        with engine.begin() as connection:
            created = workbench_sessions_service.create_session(
                connection,
                scope_id=scope_id,
                agent_backend="",
                user_context=_organization_context("member-1"),
            )

        store.set_default_agent_name(agents["scope"].name)
        with engine.connect() as connection:
            effective = ensure_session_agent_access(
                connection,
                created,
                user_context=_organization_context("member-1"),
            )
    finally:
        store.close()

    assert (created["agent_id"], created["agent_name"], created["agent_backend"]) == (
        None,
        None,
        "",
    )
    assert effective is not None
    assert effective.id == agents["scope"].id


def test_missing_agent_selector_fails_closed_for_editor_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        with store.engine.connect() as connection:
            with pytest.raises(VibeAgentAccessError):
                ensure_agent_selection_access(
                    connection,
                    agent_name="deleted-legacy-agent",
                    user_context=_organization_context("member-1"),
                )
            assert (
                ensure_agent_selection_access(
                    connection,
                    agent_name="deleted-legacy-agent",
                    user_context=_organization_context("owner-1", instance_role="owner"),
                )
                is None
            )
    finally:
        store.close()


def test_active_org_agent_detail_uses_full_runtime_projection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path, paired=True, instance_kind="organization")
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


def test_editor_agent_selection_and_harness_bindings_follow_acl(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path, paired=True, instance_kind="organization")
    store, agents = _seed_agents_with_policies()
    private_agent = agents["private"]
    _seed_legacy_default_agent(store, private_agent.name)
    store.close()
    context = _organization_context("member-1")

    # Editors may read only Agents allowed by Agent ACL and cannot manage them.
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
    catalog = client.get("/api/agents", base_url="https://alex.avibe.bot", environ_base=_remote_peer())
    assert catalog.status_code == 200
    assert {"public-agent", "scope-agent"} == {
        agent["name"] for agent in catalog.get_json()["agents"]
    }
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
    # Only the Instance Owner can manage Agents.
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="admin-1",
            groups=["group-engineering"],
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
    # Instance-wide default routing serves everyone, so even an Owner cannot
    # point it at a single-subject Agent; an audience-usable one still works.
    default_mutation = client.post(
        "/api/agents/default",
        json={"name": private_agent.name},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert default_mutation.status_code == 403
    assert default_mutation.get_json()["code"] == "agent_access_forbidden"
    shared_default = client.post(
        "/api/agents/default",
        json={"name": agents["scope"].name},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert shared_default.status_code == 200

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
            agent_name=agents["scope"].name,
            agent_id=agents["scope"].id,
            user_context=context,
        )
        assert session["agent_id"] == agents["scope"].id
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
            agent_name=agents["scope"].name,
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
            agent_name=agents["scope"].name,
            user_context=context,
        )
        assert task.agent_name == agents["scope"].name
        assert watch.agent_name == agents["scope"].name
    finally:
        pass


def test_only_owner_can_create_or_import_agents(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        with pytest.raises(VibeAgentAccessError):
            store.create(
                name="editor-agent",
                backend="codex",
                user_context=_organization_context("member-1"),
            )
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


def _create_acl_principal(role: str, *, organization: bool) -> resource_access_service.ResourceUserContext:
    if organization:
        return _organization_context(f"{role}-org", instance_role=role)
    return resource_access_service.ResourceUserContext(
        subject=f"{role}-personal",
        email=f"{role}-personal@example.com",
        instance_role=role,
        instance_access_source="email",
        is_remote=True,
    )


def test_agent_create_registers_acl_for_every_creating_subject(monkeypatch, tmp_path) -> None:
    """Create-path ACL follows the creating subject, not org membership.

    Seed every existing instance role on Personal (email) and Organization so a
    later create path cannot skip the policy for a newly admitted principal.
    Organization *use* of another member's private Agent stays denied.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        created: list[tuple[resource_access_service.ResourceUserContext, VibeAgent]] = []
        for organization in (False, True):
            for role in ("viewer", "editor", "member", "owner"):
                context = _create_acl_principal(role, organization=organization)
                name = f"{role}-{'org' if organization else 'personal'}-agent"
                if not context.can_manage_agents:
                    with pytest.raises(VibeAgentAccessError):
                        store.create(name=name, backend="codex", user_context=context)
                    continue
                agent = store.create(name=name, backend="codex", user_context=context)
                created.append((context, agent))
                with store.engine.connect() as connection:
                    policy = resource_access_service.get_resource_policy(
                        "agent",
                        agent.id,
                        connection=connection,
                    )
                assert policy is not None
                assert policy["owner_user_id"] == context.subject
                assert policy["access_level"] == "private"
                if organization:
                    assert policy["organization_id"] == context.organization_id
                else:
                    assert not policy["organization_id"]
                visible = {item.id for item in store.list_agents(user_context=context)}
                assert agent.id in visible

        org_member = next(ctx for ctx, _agent in created if ctx.instance_role == "member" and ctx.organization_id)
        personal_member_agent = next(
            agent for ctx, agent in created if ctx.instance_role == "member" and not ctx.organization_id
        )
        org_member_agent = next(
            agent for ctx, agent in created if ctx.instance_role == "member" and ctx.organization_id
        )
        org_editor = _create_acl_principal("editor", organization=True)
        org_visible = {item.id for item in store.list_agents(user_context=org_editor)}
        assert org_member_agent.id not in org_visible
        personal_visible = {
            item.id
            for item in store.list_agents(user_context=_create_acl_principal("editor", organization=False))
        }
        assert personal_member_agent.id not in personal_visible
        assert org_member.can_manage_agents
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
        assert resource_access_service.metadata_allows_harness_runtime(
            task.metadata
        )
        assert resource_access_service.metadata_allows_harness_runtime(
            watch.metadata
        )
        restored = resource_access_service.resource_user_context_from_metadata(
            task.metadata
        )
        assert restored is not None
        assert restored.subject == context.subject
        assert restored.is_remote is True
        assert restored.is_instance_owner is False

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
        assert task_result.error == "harness_access_forbidden"
        stored_task = task_store.get_task(task.id)
        assert stored_task is not None
        assert stored_task.enabled is False
        assert stored_task.last_error == "harness_access_forbidden"
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
        assert completed["error"] == "harness_access_forbidden"
    finally:
        agent_store.close()


def test_project_runtime_uses_acls_while_harness_remains_editor_wide(monkeypatch, tmp_path) -> None:
    """Project-bound runtime and global Harness follow their distinct contracts.

    Running Agents and the graph honor Project and Agent ACLs. Harness has no
    additional resource ACL in this MVP, so an Editor can see and manage every
    definition even when it references an inaccessible or deleted Agent.
    """

    from datetime import datetime, timezone

    from core.services import agent_graph
    from storage.background import SQLiteBackgroundTaskStore
    from storage.importer import ensure_sqlite_state
    from vibe import internal_client, ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    config = _save_config(tmp_path, paired=True, instance_kind="organization")
    store, agents = _seed_agents_with_policies()
    engine = get_cached_sqlite_engine()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with engine.begin() as connection:
            allowed_scope = upsert_scope(
                connection,
                platform="avibe",
                scope_type="project",
                native_id="proj_runtime_allowed",
                now=now,
            )
            denied_scope = upsert_scope(
                connection,
                platform="avibe",
                scope_type="project",
                native_id="proj_runtime_denied",
                now=now,
            )
            allowed_session = workbench_sessions_service.create_session(
                connection,
                scope_id=allowed_scope,
                agent_backend="codex",
                agent_name=agents["public"].name,
                agent_id=agents["public"].id,
            )
            denied_session = workbench_sessions_service.create_session(
                connection,
                scope_id=denied_scope,
                agent_backend="codex",
                agent_name=agents["private"].name,
                agent_id=agents["private"].id,
            )
    finally:
        store.close()

    harness = SQLiteBackgroundTaskStore()
    try:
        harness.upsert_scheduled_task(
            {
                "id": "task-allowed",
                "name": "allowed task",
                "prompt": "run allowed",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": True,
                "session_id": allowed_session["id"],
                "agent_name": agents["public"].name,
                "agent_id": agents["public"].id,
                "created_at": now,
                "updated_at": now,
            }
        )
        harness.upsert_scheduled_task(
            {
                "id": "task-denied",
                "name": "denied task",
                "prompt": "run denied",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": True,
                "session_id": denied_session["id"],
                "agent_name": agents["private"].name,
                "agent_id": agents["private"].id,
                "created_at": now,
                "updated_at": now,
            }
        )
        harness.upsert_watch(
            {
                "id": "watch-allowed",
                "name": "allowed watch",
                "shell_command": "true",
                "enabled": True,
                "session_id": allowed_session["id"],
                "agent_name": agents["public"].name,
                "agent_id": agents["public"].id,
                "created_at": now,
                "updated_at": now,
            }
        )
        harness.upsert_watch(
            {
                "id": "watch-denied",
                "name": "denied watch",
                "shell_command": "true",
                "enabled": True,
                "session_id": denied_session["id"],
                "agent_name": agents["private"].name,
                "agent_id": agents["private"].id,
                "created_at": now,
                "updated_at": now,
            }
        )
        harness.upsert_scheduled_task(
            {
                "id": "task-unbound-allowed",
                "name": "unbound allowed task",
                "prompt": "run unbound",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": True,
                "agent_name": agents["public"].name,
                "agent_id": agents["public"].id,
                "created_at": now,
                "updated_at": now,
            }
        )
        for index in range(3):
            harness.upsert_scheduled_task(
                {
                    "id": f"task-denied-page-{index}",
                    "name": f"denied page {index}",
                    "prompt": "run denied page",
                    "schedule_type": "cron",
                    "cron": "0 * * * *",
                    "enabled": True,
                    "session_id": denied_session["id"],
                    "agent_name": agents["private"].name,
                    "agent_id": agents["private"].id,
                    "created_at": f"2026-08-13T12:0{index}:00Z",
                    "updated_at": f"2026-08-13T12:0{index}:00Z",
                }
            )
        harness.upsert_scheduled_task(
            {
                "id": "task-missing-agent",
                "name": "missing agent task",
                "prompt": "run missing",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": True,
                "agent_name": "deleted-legacy-agent",
                "created_at": now,
                "updated_at": now,
            }
        )
    finally:
        harness.close()

    async def fake_list(**_kwargs):
        return {
            "status_code": 200,
            "body": {
                "agents": [
                    {
                        "session_id": allowed_session["id"],
                        "agent_name": agents["public"].name,
                        "agent_id": agents["public"].id,
                        "backend": "codex",
                        "state": "idle",
                        "title": "allowed live",
                    },
                    {
                        "session_id": denied_session["id"],
                        "agent_name": agents["private"].name,
                        "agent_id": agents["private"].id,
                        "backend": "codex",
                        "state": "active",
                        "title": "denied live",
                    },
                ],
                "counts": {
                    "total": 2,
                    "active": 1,
                    "idle": 1,
                    "orphan": 0,
                    "by_backend": {"codex": 2},
                },
            },
        }

    ended: list[dict] = []

    async def fake_end(payload):
        ended.append(payload)
        return {"status_code": 200, "body": {"ok": True}}

    monkeypatch.setattr(internal_client, "list_running_agents", fake_list)
    monkeypatch.setattr(internal_client, "end_running_agent", fake_end)

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
    remote = {"base_url": "https://alex.avibe.bot", "environ_base": _remote_peer()}

    graph_payload = {
        "nodes": [
            {
                "session_id": allowed_session["id"],
                "agent_name": agents["public"].name,
                "agent_id": agents["public"].id,
                "status": "idle",
                "live": False,
                "visibility": "foreground",
            },
            {
                "session_id": denied_session["id"],
                "agent_name": agents["private"].name,
                "agent_id": agents["private"].id,
                "status": "idle",
                "live": False,
                "visibility": "foreground",
            },
        ],
        "edges": [
            {
                "kind": "trigger",
                "from": "def:task-allowed",
                "to": allowed_session["id"],
            },
            {
                "kind": "trigger",
                "from": "def:task-denied",
                "to": denied_session["id"],
            },
        ],
        "trigger_nodes": [
            {"definition_id": "task-allowed"},
            {"definition_id": "task-denied"},
        ],
        "counts": {},
    }
    graph_body = ui_server._authorized_graph_payload(
        _organization_context("member-1"),
        graph_payload,
    )
    assert {node["session_id"] for node in graph_body["nodes"]} == {allowed_session["id"]}
    assert graph_body["edges"] == [
        {
            "kind": "trigger",
            "from": "def:task-allowed",
            "to": allowed_session["id"],
        }
    ]
    assert graph_body["trigger_nodes"] == [{"definition_id": "task-allowed"}]
    assert graph_body["counts"] == agent_graph._counts(graph_body["nodes"])

    running = client.get("/api/running-agents", **remote)
    assert running.status_code == 200
    running_body = running.get_json()
    running_ids = {row.get("session_id") for row in running_body.get("agents") or []}
    assert running_ids == {allowed_session["id"]}
    assert running_body["counts"] == {
        "total": 1,
        "active": 0,
        "idle": 1,
        "orphan": 0,
        "by_backend": {"codex": 1},
    }

    favorites = client.get("/api/browse/favorites", **remote)
    assert favorites.status_code == 200
    denied_browse = client.post(
        "/api/browse",
        json={"path": "~"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        **remote,
    )
    assert denied_browse.status_code == 403

    deny_end = client.post(
        "/api/running-agents/end",
        json={"session_id": denied_session["id"], "agent_name": agents["private"].name},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        **remote,
    )
    assert deny_end.status_code == 404
    assert ended == []

    allow_end = client.post(
        "/api/running-agents/end",
        json={"session_id": allowed_session["id"], "agent_name": agents["public"].name},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        **remote,
    )
    assert allow_end.status_code == 200
    assert ended

    tasks = client.get("/api/harness/tasks", **remote).get_json()["tasks"]
    watches = client.get("/api/harness/watches", **remote).get_json()["watches"]
    assert {row["id"] for row in tasks} == {
        "task-allowed",
        "task-denied",
        "task-denied-page-0",
        "task-denied-page-1",
        "task-denied-page-2",
        "task-missing-agent",
        "task-unbound-allowed",
    }
    assert {row["id"] for row in watches} == {"watch-allowed", "watch-denied"}

    paged = client.get("/api/harness/tasks?page=1&limit=1", **remote).get_json()
    assert len(paged["tasks"]) == 1
    assert paged["counts"]["total"] == 7
    assert paged["has_more"] is True

    patch_response = client.patch(
        "/api/harness/tasks/task-denied",
        json={"enabled": False},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        **remote,
    )
    delete_response = client.delete(
        "/api/harness/watches/watch-denied",
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        **remote,
    )
    assert patch_response.status_code == 200
    assert patch_response.get_json()["task"]["enabled"] is False
    assert delete_response.status_code == 200

    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="viewer-1",
            groups=["group-engineering"],
            instance_role="viewer",
        ),
        domain="alex.avibe.bot",
    )
    assert client.get("/api/harness/tasks", **remote).status_code == 403
