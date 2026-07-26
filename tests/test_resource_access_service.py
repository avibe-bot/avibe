from __future__ import annotations

import pytest

from storage import resource_access_service
from storage.db import create_sqlite_engine
from storage.migrations import run_migrations


def _context(
    subject: str,
    *,
    organization_id: str | None = "org-1",
    group_ids: frozenset[str] | None = frozenset(),
    role: str | None = "member",
    instance_role: str = "viewer",
    access_source: str = "organization_group",
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        organization_id=organization_id,
        organization_member_id="member-1" if organization_id else None,
        organization_role=role,
        group_ids=group_ids,
        instance_role=instance_role,
        instance_access_source=access_source,
        is_remote=True,
    )


def _seed_policies(connection) -> None:
    resource_access_service.ensure_resource_policy(
        connection,
        resource_kind="agent",
        resource_id="private-agent",
        organization_id="org-1",
        owner_user_id="owner-1",
        access_level="private",
    )
    resource_access_service.ensure_resource_policy(
        connection,
        resource_kind="agent",
        resource_id="public-agent",
        organization_id="org-1",
        owner_user_id="owner-1",
        access_level="public",
    )
    resource_access_service.ensure_resource_policy(
        connection,
        resource_kind="agent",
        resource_id="scoped-agent",
        organization_id="org-1",
        owner_user_id="owner-1",
        access_level="scope",
        group_ids=["group-engineering"],
    )


def test_policy_evaluation_private_public_scope_and_missing_group_context(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            _seed_policies(connection)

            owner = _context("owner-1")
            engineering_member = _context("member-2", group_ids=frozenset({"group-engineering"}))
            member_without_groups = _context("member-3", group_ids=None)
            member_other_group = _context("member-4", group_ids=frozenset({"group-sales"}))
            outside_org = _context("member-5", organization_id="org-2", group_ids=frozenset({"group-engineering"}))

            assert resource_access_service.can_use_resource(owner, "agent", "private-agent", connection=connection)
            assert not resource_access_service.can_use_resource(
                engineering_member, "agent", "private-agent", connection=connection
            )

            assert resource_access_service.can_use_resource(
                engineering_member, "agent", "public-agent", connection=connection
            )
            assert not resource_access_service.can_use_resource(outside_org, "agent", "public-agent", connection=connection)

            assert resource_access_service.can_use_resource(
                engineering_member, "agent", "scoped-agent", connection=connection
            )
            assert not resource_access_service.can_use_resource(
                member_without_groups, "agent", "scoped-agent", connection=connection
            )
            assert not resource_access_service.can_use_resource(
                member_other_group, "agent", "scoped-agent", connection=connection
            )
    finally:
        engine.dispose()


def test_no_policy_is_local_private_but_instance_owner_keeps_legacy_access(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.connect() as connection:
            member = _context("member-1", group_ids=frozenset({"group-engineering"}))
            owner = _context("owner-1", instance_role="owner", access_source="owner")
            diagnostic_owner = _context("member-2", instance_role="viewer", access_source="owner")
            local = resource_access_service.ResourceUserContext(is_trusted_local=True)

            assert not resource_access_service.can_use_resource(member, "agent", "legacy-agent", connection=connection)
            assert resource_access_service.can_use_resource(owner, "agent", "legacy-agent", connection=connection)
            assert not resource_access_service.can_use_resource(
                diagnostic_owner,
                "agent",
                "legacy-agent",
                connection=connection,
            )
            assert resource_access_service.can_use_resource(local, "agent", "legacy-agent", connection=connection)
    finally:
        engine.dispose()


def test_removed_org_owner_loses_org_private_resources_but_keeps_personal_resources(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            _seed_policies(connection)
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="agent",
                resource_id="personal-agent",
                organization_id=None,
                owner_user_id="owner-1",
                access_level="private",
            )
            removed_owner = _context(
                "owner-1",
                organization_id=None,
                role=None,
                access_source="email_invitation",
            )

            assert not resource_access_service.can_use_resource(
                removed_owner,
                "agent",
                "private-agent",
                connection=connection,
            )
            assert not resource_access_service.can_manage_resource_acl(
                removed_owner,
                "agent",
                "private-agent",
                connection=connection,
            )
            assert resource_access_service.can_use_resource(
                removed_owner,
                "agent",
                "personal-agent",
                connection=connection,
            )
            assert resource_access_service.can_manage_resource_acl(
                removed_owner,
                "agent",
                "personal-agent",
                connection=connection,
            )
    finally:
        engine.dispose()


def test_request_context_resolution_failure_does_not_become_trusted_local(monkeypatch) -> None:
    from vibe.ui_server import app

    monkeypatch.setattr(
        resource_access_service,
        "current_resource_context",
        lambda *args, **kwargs: resource_access_service.ResourceUserContext(),
    )

    with app.test_request_context("/api/agents", base_url="https://alex.avibe.bot"):
        request_context = resource_access_service.resolve_resource_access_context()
    local_context = resource_access_service.resolve_resource_access_context()

    assert request_context.is_trusted_local is False
    assert local_context.is_trusted_local is True


def test_deferred_remote_context_expires_at_authorization_refresh_boundary() -> None:
    from vibe import remote_access

    issued_at = 1_700_000_000
    context = resource_access_service.ResourceUserContext(
        subject="member-1",
        organization_id="org-1",
        organization_member_id="organization-member-1",
        organization_role="member",
        group_ids=frozenset({"group-engineering"}),
        membership_version="membership-v2",
        instance_role="viewer",
        instance_access_source="organization_group",
        claims_issued_at=issued_at,
        is_remote=True,
    )

    metadata = resource_access_service.metadata_with_resource_user_context({}, context)
    expires_at = issued_at + remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS

    restored = resource_access_service.resource_user_context_from_metadata(
        metadata,
        now=expires_at - 1,
    )
    assert restored is not None
    assert restored.subject == "member-1"
    with pytest.raises(resource_access_service.ResourceAccessError, match="resource_authorization_expired"):
        resource_access_service.resource_user_context_from_metadata(
            metadata,
            now=expires_at,
        )


def test_personal_resources_cannot_use_organization_access_levels(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            with pytest.raises(resource_access_service.ResourceAccessError, match="invalid_resource_acl_intent"):
                resource_access_service.ensure_resource_policy(
                    connection,
                    resource_kind="agent",
                    resource_id="personal-agent",
                    organization_id=None,
                    owner_user_id="owner-1",
                    access_level="public",
                )
    finally:
        engine.dispose()
