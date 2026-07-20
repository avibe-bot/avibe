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
    access_source: str = "organization_group",
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        organization_id=organization_id,
        organization_member_id="member-1" if organization_id else None,
        organization_role=role,
        group_ids=group_ids,
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
            owner = _context("owner-1", access_source="owner")
            local = resource_access_service.ResourceUserContext(is_trusted_local=True)

            assert not resource_access_service.can_use_resource(member, "agent", "legacy-agent", connection=connection)
            assert resource_access_service.can_use_resource(owner, "agent", "legacy-agent", connection=connection)
            assert resource_access_service.can_use_resource(local, "agent", "legacy-agent", connection=connection)
    finally:
        engine.dispose()


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
