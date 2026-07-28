from __future__ import annotations

import json
from typing import Any

import pytest

from core.vibe_agents import VibeAgentAccessError, VibeAgentStore
from storage import resource_access_service
from tests.test_ui_remote_access_auth import _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access
from vibe.ui_server import app


def _organization_context(
    *,
    subject: str = "owner-1",
    instance_role: str = "owner",
    group_ids: frozenset[str] = frozenset({"group-engineering"}),
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role="member",
        group_ids=group_ids,
        instance_role=instance_role,
        instance_access_source="organization_group",
        is_remote=True,
    )


def _organization_cookie(config, *, instance_role: str) -> str:
    return remote_access.make_session_cookie(
        config,
        "owner-1@example.com",
        "owner-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": instance_role,
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-owner-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-engineering"],
            "vibe_membership_version": "membership-v2",
        },
    )


def _apply_agent_intent(
    store: VibeAgentStore,
    agent_id: str,
    *,
    revision: int,
    access_level: str,
    group_ids: list[str] | None = None,
) -> None:
    with store.engine.begin() as connection:
        resource_access_service.apply_control_plane_intent(
            connection,
            organization_id="org-1",
            resource_kind="agent",
            resource_id=agent_id,
            revision=revision,
            access_level=access_level,
            group_ids=group_ids or [],
        )


def test_upgrade_onboarding_inventories_custom_and_system_agents_as_private(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        legacy = store.create(
            name="legacy-custom",
            backend="codex",
            system_prompt="legacy prompt",
        )
        builtins = store.ensure_builtin_default_agents(["codex", "claude"])

        before = store.organization_onboarding_inventory(user_context=_organization_context())
        assert before["counts"] == {
            "total": 3,
            "system": 2,
            "custom": 1,
            "not_onboarded": 3,
            "private": 0,
            "published": 0,
            "conflicts": 0,
        }

        result = store.onboard_organization_agents(user_context=_organization_context())
        assert result["created"] == 3
        assert result["counts"]["private"] == 3
        with store.engine.connect() as connection:
            policies = resource_access_service.list_resource_policies(
                resource_kind="agent",
                organization_id="org-1",
                connection=connection,
            )
        assert len(policies) == 3
        assert all(policy["access_level"] == "private" for policy in policies)
        assert all(policy["group_ids"] == [] for policy in policies)

        _apply_agent_intent(
            store,
            legacy.id,
            revision=1,
            access_level="scope",
            group_ids=["group-engineering"],
        )
        _apply_agent_intent(store, builtins[0].id, revision=1, access_level="public")
        visible = {
            agent.name
            for agent in store.list_agents(
                user_context=_organization_context(subject="editor-1", instance_role="editor"),
            )
        }
        assert visible == {legacy.name, builtins[0].name}
    finally:
        store.close()


def test_fresh_install_onboarding_preserves_new_private_agent_and_authorizes_intended_set(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        builtin = store.ensure_builtin_default_agent(backend="codex")
        custom = store.create(
            name="fresh-private",
            backend="codex",
            user_context=_organization_context(),
        )

        result = store.onboard_organization_agents(user_context=_organization_context())
        assert result["created"] == 1
        assert result["unchanged"] == 1
        assert result["counts"]["private"] == 2

        _apply_agent_intent(
            store,
            builtin.id,
            revision=1,
            access_level="scope",
            group_ids=["group-engineering"],
        )
        visible = store.list_agents(
            user_context=_organization_context(subject="editor-1", instance_role="editor"),
        )
        assert [agent.name for agent in visible] == [builtin.name]
        assert custom.name not in {agent.name for agent in visible}
    finally:
        store.close()


def test_onboarding_is_owner_only_and_does_not_overwrite_existing_acl_revision(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        agent = store.create(name="revisioned", backend="codex")
        with pytest.raises(VibeAgentAccessError):
            store.organization_onboarding_inventory(
                user_context=_organization_context(subject="editor-1", instance_role="editor"),
            )
        with pytest.raises(VibeAgentAccessError):
            store.onboard_organization_agents(
                user_context=_organization_context(subject="editor-1", instance_role="editor"),
            )

        store.onboard_organization_agents(user_context=_organization_context())
        _apply_agent_intent(
            store,
            agent.id,
            revision=7,
            access_level="scope",
            group_ids=["group-engineering"],
        )
        with store.engine.connect() as connection:
            before = resource_access_service.get_resource_policy(
                "agent",
                agent.id,
                connection=connection,
            )

        rerun = store.onboard_organization_agents(user_context=_organization_context())
        with store.engine.connect() as connection:
            after = resource_access_service.get_resource_policy(
                "agent",
                agent.id,
                connection=connection,
            )

        assert rerun["created"] == 0
        assert rerun["unchanged"] == 1
        assert after == before
        assert after is not None
        assert after["policy_revision"] == 7
        assert after["last_applied_control_plane_revision"] == 7
        assert after["group_ids"] == ["group-engineering"]
    finally:
        store.close()


def test_onboarding_publication_redacts_prompt_credentials_paths_and_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "inst_123"
    config.remote_access.vibe_cloud.instance_secret = "paired-device-secret"
    store = VibeAgentStore()
    try:
        store.create(
            name="safe-display-name",
            backend="codex",
            description="credential=description-secret",
            system_prompt="prompt-secret /home/owner/private AGENT_TOKEN=token-secret",
            source_ref="/home/owner/.codex/agents/private.md",
            metadata={"credential": "metadata-secret", "cwd": "/home/owner/private"},
        )
        store.onboard_organization_agents(user_context=_organization_context())
    finally:
        store.close()

    published: list[dict[str, Any]] = []

    def device_request(_config, method: str, suffix: str, payload=None, **_kwargs):
        if method == "PUT" and suffix == "resource-index":
            published.extend(payload["resources"])
            return {"organization_id": "org-1", "resources": payload["resources"]}
        if method == "GET" and suffix == "resource-acl-intents":
            return {"organization_id": "org-1", "poll_after_seconds": 30, "intents": []}
        raise AssertionError((method, suffix, payload))

    monkeypatch.setattr(remote_access, "_device_json_request", device_request)
    result = remote_access.sync_resource_acl_once(config, organization_id="org-1")

    assert result["ok"] is True
    assert len(published) == 1
    assert set(published[0]) <= {
        "resource_id",
        "resource_kind",
        "display_name",
        "owner_user_id",
        "metadata_revision",
        "applied_acl_revision",
        "access_level",
        "group_ids",
        "sync_status",
    }
    serialized = json.dumps(published)
    for forbidden in (
        "prompt-secret",
        "description-secret",
        "token-secret",
        "metadata-secret",
        "/home/owner",
        "system_prompt",
        "source_ref",
        "credential",
        "cwd",
    ):
        assert forbidden not in serialized


def test_agent_rename_and_delete_converge_in_full_resource_index(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = VibeAgentStore()
    try:
        old = store.create(name="old-name", backend="codex")
        store.onboard_organization_agents(user_context=_organization_context())
        renamed = store.create(
            name="new-name",
            backend="codex",
            user_context=_organization_context(),
        )
        assert store.remove(old.name) is True

        descriptors = remote_access._local_policy_resource_descriptors("org-1")
        assert {item["resource_id"] for item in descriptors} == {renamed.id}
        assert resource_access_service.list_resource_organization_ids() == ["org-1"]

        assert store.remove(renamed.name) is True
        assert remote_access._local_policy_resource_descriptors("org-1") == []
        assert resource_access_service.list_resource_organization_ids() == ["org-1"]
    finally:
        store.close()


def test_owner_http_workflow_lists_and_onboards_agents_while_editor_is_denied(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store = VibeAgentStore()
    try:
        store.create(name="legacy-http", backend="codex", system_prompt="must-not-appear")
    finally:
        store.close()
    monkeypatch.setattr(
        remote_access,
        "sync_resource_acl_once",
        lambda *_args, **_kwargs: {"ok": True, "organizations": [{"organization_id": "org-1"}]},
    )

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, instance_role="owner"),
        domain="alex.avibe.bot",
    )
    inventory = client.get(
        "/api/agent-onboarding",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    onboarded = client.post(
        "/api/agent-onboarding",
        json={},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert inventory.status_code == 200
    assert inventory.get_json()["available"] is True
    assert {item["name"] for item in inventory.get_json()["agents"]} >= {"legacy-http"}
    assert all(
        set(item)
        <= {
            "id",
            "name",
            "backend",
            "source",
            "enabled",
            "status",
            "access_level",
            "group_ids",
            "policy_revision",
            "applied_acl_revision",
        }
        for item in inventory.get_json()["agents"]
    )
    assert onboarded.status_code == 200
    assert onboarded.get_json()["counts"]["not_onboarded"] == 0
    assert onboarded.get_json()["console_url"].endswith("/app/organizations/org-1/resources")

    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, instance_role="editor"),
        domain="alex.avibe.bot",
    )
    denied = client.get(
        "/api/agent-onboarding",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "instance_access_forbidden"
