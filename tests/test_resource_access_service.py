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
    instance_role: str = "editor",
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


def test_resource_acl_is_enforced_for_editors_and_unknown_kinds_fail_closed(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            _seed_policies(connection)

            owner = _context("owner-1", instance_role="owner", access_source="owner")
            engineering_member = _context("member-2", group_ids=frozenset({"group-engineering"}))
            member_without_groups = _context("member-3", group_ids=None)
            member_other_group = _context("member-4", group_ids=frozenset({"group-sales"}))
            outside_org = _context("member-5", organization_id="org-2", group_ids=frozenset({"group-engineering"}))

            assert resource_access_service.can_use_resource(owner, "agent", "private-agent", connection=connection)
            assert resource_access_service.can_use_resource(owner, "agent", "public-agent", connection=connection) is True
            assert resource_access_service.can_use_resource(engineering_member, "agent", "scoped-agent", connection=connection)
            assert not resource_access_service.can_use_resource(member_without_groups, "agent", "scoped-agent", connection=connection)
            assert not resource_access_service.can_use_resource(member_other_group, "agent", "scoped-agent", connection=connection)
            assert not resource_access_service.can_use_resource(outside_org, "agent", "public-agent", connection=connection)
            with pytest.raises(resource_access_service.ResourceAccessError, match="invalid_resource_kind"):
                resource_access_service.can_use_resource(
                    engineering_member, "future_resource", "future-1", connection=connection
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("previous_level", "previous_groups", "updated_level", "updated_groups", "expected"),
    [
        ("public", [], "public", [], False),
        ("public", [], "scope", ["group-engineering"], True),
        ("public", [], "private", [], True),
        ("scope", ["group-engineering"], "public", [], False),
        ("scope", ["group-engineering"], "scope", ["group-engineering", "group-sales"], False),
        ("scope", ["group-engineering", "group-sales"], "scope", ["group-engineering"], True),
        ("scope", ["group-engineering"], "private", [], True),
        ("private", [], "private", [], False),
        ("private", [], "public", [], False),
        ("private", [], "scope", ["group-sales"], True),
    ],
)
def test_resource_policy_narrowing_matrix(
    previous_level: str,
    previous_groups: list[str],
    updated_level: str,
    updated_groups: list[str],
    expected: bool,
) -> None:
    assert (
        resource_access_service.resource_policy_narrowed(
            {"access_level": previous_level, "group_ids": previous_groups},
            {"access_level": updated_level, "group_ids": updated_groups},
        )
        is expected
    )


def test_active_org_members_can_use_legacy_resources_without_forging_local_identity(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.connect() as connection:
            member = _context("member-1", group_ids=frozenset({"group-engineering"}))
            owner = _context("owner-1", instance_role="owner", access_source="owner")
            diagnostic_owner = _context("member-2", instance_role="viewer", access_source="owner")
            non_member_owner = _context(
                "legacy-owner",
                organization_id=None,
                role=None,
                instance_role="owner",
                access_source="owner",
            )
            local = resource_access_service.instance_owner_context()

            assert not resource_access_service.can_use_resource(member, "agent", "legacy-agent", connection=connection)
            assert resource_access_service.can_use_resource(owner, "agent", "legacy-agent", connection=connection)
            assert not resource_access_service.can_use_resource(
                diagnostic_owner, "agent", "legacy-agent", connection=connection
            )
            assert resource_access_service.can_use_resource(
                non_member_owner, "agent", "legacy-agent", connection=connection
            )
            assert resource_access_service.can_use_resource(local, "agent", "legacy-agent", connection=connection)
    finally:
        engine.dispose()


def test_non_member_role_rank_does_not_bypass_organization_resource_acl(tmp_path) -> None:
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
                instance_role="owner",
                access_source="email_invitation",
            )

            assert resource_access_service.can_use_resource(
                removed_owner, "agent", "private-agent", connection=connection
            )
            assert resource_access_service.can_manage_resource_acl(
                removed_owner, "agent", "private-agent", connection=connection
            )
            # Direct service calls retain ordinary role plus resource-policy
            # semantics; HTTP admission rejects this non-member before runtime
            # APIs can reach the service.
            assert resource_access_service.can_use_resource(
                removed_owner, "agent", "personal-agent", connection=connection
            )
            assert resource_access_service.can_manage_resource_acl(
                removed_owner, "agent", "personal-agent", connection=connection
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

    assert request_context.is_instance_owner is False
    assert local_context.is_instance_owner is True


def test_http_resource_services_reuse_the_parsed_authorization_context() -> None:
    from vibe.ui_compat import g
    from vibe.ui_server import app

    context = _context("member-1")
    with app.test_request_context("/api/agents", base_url="https://alex.avibe.bot"):
        g.authorization_context = context

        assert resource_access_service.current_resource_context() is context
        assert resource_access_service.resolve_resource_access_context() is context


def test_deferred_remote_context_remains_valid_past_authorization_refresh_boundary() -> None:
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

    # The refresh boundary is recorded for audit but is no longer a hard
    # execution cutoff: durable Harness automation must keep running past the
    # creating browser session's refresh window. Active Organization membership
    # is re-derived from the signed claims at execution time (avibe#1343 P1).
    before = resource_access_service.resource_user_context_from_metadata(
        metadata,
        now=expires_at - 1,
    )
    after = resource_access_service.resource_user_context_from_metadata(
        metadata,
        now=expires_at + 86_400,  # well past the 12h refresh window
    )
    assert before is not None and after is not None
    assert before.subject == after.subject == "member-1"
    assert after.is_active_organization_member is True


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
