from __future__ import annotations

import pytest

from core.scheduled_tasks import ScheduledTaskStore
from core.vibe_agents import AgentImportCandidate, VibeAgent, VibeAgentAccessError, VibeAgentStore
from core.watches import ManagedWatchStore
from storage import resource_access_service, workbench_sessions_service
from storage.db import get_cached_sqlite_engine
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


def _organization_cookie(config, *, subject: str, groups: list[str] | None = None) -> str:
    claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_role": "viewer",
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


def test_remote_agent_creation_defaults_to_private_organization_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="member-1", groups=["group-engineering"]),
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
        _organization_cookie(config, subject="member-1", groups=["group-engineering"]),
        domain="alex.avibe.bot",
    )
    response = client.get(
        "/api/agents/private-agent",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "agent_access_forbidden"
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
    assert public_mutation.get_json()["code"] == "agent_access_forbidden"

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
    blocked_default = client.post(
        "/api/sessions",
        json={"project_id": "proj_acl_agents"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert blocked_default.status_code == 403

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
