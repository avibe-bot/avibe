from __future__ import annotations

from config import paths
from storage import resource_access_service
from storage.db import get_cached_sqlite_engine
from storage.migrations import run_migrations
from tests.test_ui_remote_access_auth import _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access
from vibe.ui_server import app


def _organization_cookie(config) -> str:
    return remote_access.make_session_cookie(
        config,
        "member@example.com",
        "member-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "organization-member-1",
            "vibe_organization_role": "admin",
            "vibe_group_ids": ["group-engineering"],
            "vibe_membership_version": "membership-v2",
        },
    )


def _external_guest_cookie(config) -> str:
    return remote_access.make_session_cookie(
        config,
        "guest@example.com",
        "guest-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_access_source": "email",
        },
    )


def _seed_organization_policy() -> None:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id="agent-1",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="scope",
            group_ids=["group-engineering"],
            policy_revision=2,
            last_applied_control_plane_revision=2,
        )


def test_organization_context_and_policy_routes_use_signed_cookie_claims(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _seed_organization_policy()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config),
        domain="alex.avibe.bot",
    )

    context = client.get("/api/org/context", base_url="https://alex.avibe.bot")
    groups = client.get("/api/org/groups", base_url="https://alex.avibe.bot")
    policies = client.get("/api/resource-policies?kind=agent", base_url="https://alex.avibe.bot")

    assert context.status_code == 200
    assert context.get_json()["organization"] == {
        "id": "org-1",
        "member_id": "organization-member-1",
        "role": "admin",
        "group_ids": ["group-engineering"],
        "membership_version": "membership-v2",
    }
    assert groups.get_json() == {"groups": [{"id": "group-engineering", "name": None, "archived_at": None}]}
    assert policies.status_code == 200
    assert policies.get_json()["policies"] == [
        {
            "resource_kind": "agent",
            "resource_id": "agent-1",
            "access_level": "scope",
            "owner_user_id": "owner-1",
            "organization_id": "org-1",
            "group_ids": ["group-engineering"],
            "policy_revision": 2,
            "last_applied_control_plane_revision": 2,
            "can_use": True,
            "can_manage": True,
        }
    ]


def test_organization_policy_put_does_not_create_local_revision(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _seed_organization_policy()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config),
        domain="alex.avibe.bot",
    )
    monkeypatch.setattr(
        remote_access,
        "sync_resource_acl_once",
        lambda *_args, **_kwargs: {"ok": True, "organizations": [{"organization_id": "org-1"}]},
    )

    response = client.put(
        "/api/resource-policies/agent/agent-1",
        json={"access_level": "public", "group_ids": []},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "resource_acl_control_plane_required"
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", "agent-1", connection=connection)
    assert policy is not None
    assert policy["access_level"] == "scope"
    assert policy["policy_revision"] == 2


def test_external_guest_cannot_update_an_organization_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    _seed_organization_policy()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _external_guest_cookie(config),
        domain="alex.avibe.bot",
    )

    response = client.put(
        "/api/resource-policies/agent/agent-1",
        json={"access_level": "public", "group_ids": []},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "resource_not_found"
