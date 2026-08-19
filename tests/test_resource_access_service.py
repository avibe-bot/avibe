from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import select

from config.v2_config import V2Config
from storage import resource_access_service
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.message_deliveries import enqueue_queued
from storage.migrations import run_migrations
from storage.models import agent_runs, agent_sessions, message_deliveries, run_definitions, state_meta
from vibe import permissions


def _context(
    subject: str,
    *,
    organization_id: str | None = "org-1",
    group_ids: frozenset[str] | None = frozenset(),
    role: str | None = "member",
    instance_role: str = "editor",
    access_source: str = "organization_group",
    instance_kind: str | None = None,
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
        instance_kind=instance_kind,
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


def test_personal_editor_uses_all_agents_without_organization_acl(tmp_path) -> None:
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            _seed_policies(connection)
            personal_editor = _context(
                "personal-user",
                organization_id=None,
                role=None,
                access_source="owner",
                instance_kind="personal",
            )
            organization_editor = _context(
                "organization-user",
                instance_kind="organization",
            )

            assert resource_access_service.can_use_resource(
                personal_editor, "agent", "private-agent", connection=connection
            )
            assert resource_access_service.can_use_resource(
                personal_editor, "agent", "scoped-agent", connection=connection
            )
            assert resource_access_service.can_use_resource(
                personal_editor, "agent", "unmanaged-agent", connection=connection
            )
            assert resource_access_service.filter_accessible_resources(
                personal_editor,
                "agent",
                [
                    {"id": "private-agent"},
                    {"id": "scoped-agent"},
                    {"id": "unmanaged-agent"},
                ],
                connection=connection,
            ) == [
                {"id": "private-agent"},
                {"id": "scoped-agent"},
                {"id": "unmanaged-agent"},
            ]
            assert not resource_access_service.can_use_resource(
                organization_editor, "agent", "private-agent", connection=connection
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "load_error",
    [
        pytest.param(OSError("config unavailable"), id="io-error"),
        pytest.param(AttributeError("config shape unavailable"), id="attribute-error"),
        pytest.param(TypeError("config shape unavailable"), id="type-error"),
        pytest.param(ValueError("config value unavailable"), id="value-error"),
    ],
)
def test_show_page_config_reload_failure_preserves_exact_page_access_but_fails_closed_elsewhere(
    monkeypatch,
    tmp_path,
    load_error: Exception,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    legacy_owner = _context(
        "legacy-owner",
        organization_id=None,
        role=None,
        instance_role="editor",
        access_source="email_invitation",
    )
    link_guest = resource_access_service.ResourceUserContext(
        subject="link-guest",
        email="link-guest@example.com",
        instance_role="viewer",
        instance_access_source="show_page_email",
        show_page_id="legacy-page",
        is_remote=True,
    )
    local_owner = resource_access_service.instance_owner_context()

    def fail_load(_cls, *_args, **_kwargs):
        raise load_error

    try:
        with engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id="legacy-page",
                organization_id=None,
                owner_user_id="legacy-owner",
                access_level="private",
            )
        monkeypatch.setattr(V2Config, "load", classmethod(fail_load))

        resolved = permissions.resolve_current_instance_ownership()
        assert resolved["mode"] == (
            resource_access_service.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE
        )

        with engine.begin() as connection:
            reconciliation = resource_access_service.reconcile_show_page_resource_policy(
                connection,
                resource_id="legacy-page",
                ownership=resolved,
                owner_user_id="legacy-owner",
            )
        assert reconciliation["status"] == (
            resource_access_service.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE
        )
        assert reconciliation["policy"]["organization_id"] is None
        assert reconciliation["policy"]["owner_user_id"] == "legacy-owner"

        with engine.connect() as connection:
            ownership = resource_access_service.current_show_page_instance_ownership(
                connection=connection
            )
            assert ownership["mode"] == (
                resource_access_service.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE
            )
            assert not resource_access_service.can_use_resource(
                legacy_owner,
                "show_page",
                "legacy-page",
                connection=connection,
            )
            assert not resource_access_service.can_manage_resource_acl(
                legacy_owner,
                "show_page",
                "legacy-page",
                connection=connection,
            )
            assert not resource_access_service.can_manage_show_page_access(
                legacy_owner,
                "legacy-page",
                connection=connection,
            )
            assert not resource_access_service.can_control_resource_sharing(
                legacy_owner,
                "show_page",
                "legacy-page",
                connection=connection,
            )
            assert resource_access_service.filter_accessible_resources(
                legacy_owner,
                "show_page",
                [{"session_id": "legacy-page"}],
                connection=connection,
            ) == []
            # The signed Limited-link session is scoped to this exact page and
            # does not depend on the instance pairing/configuration reload.
            assert resource_access_service.can_use_resource(
                link_guest,
                "show_page",
                "legacy-page",
                connection=connection,
            )
            wrong_page_guest = resource_access_service.ResourceUserContext(
                subject="link-guest",
                email="link-guest@example.com",
                instance_role="viewer",
                instance_access_source="show_page_email",
                show_page_id="another-page",
                is_remote=True,
            )
            assert not resource_access_service.can_use_resource(
                wrong_page_guest,
                "show_page",
                "legacy-page",
                connection=connection,
            )
            assert resource_access_service.can_use_resource(
                local_owner,
                "show_page",
                "legacy-page",
                connection=connection,
            )
    finally:
        engine.dispose()


def test_show_page_unpaired_configuration_remains_explicitly_unmanaged(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    V2Config.default().save()
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.connect() as connection:
            ownership = resource_access_service.current_show_page_instance_ownership(
                connection=connection
            )
        assert ownership == {
            "mode": "unmanaged",
            "instance_id": None,
            "organization_id": None,
            "source": "config",
        }
    finally:
        engine.dispose()


def test_show_page_authorizers_adopt_legacy_policy_before_enforcing_organization_fence(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.example"
    cloud.instance_id = "inst-1"
    cloud.instance_secret = "device-secret"
    cloud.instance_kind = "organization"
    config.save()
    monkeypatch.setattr(
        permissions,
        "resolve_current_instance_ownership",
        lambda: {
            "mode": "organization",
            "instance_id": "inst-1",
            "organization_id": "org-1",
            "source": "live",
        },
    )
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    owner = _context("owner-1", instance_role="editor")
    try:
        with engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id="legacy-page",
                organization_id=None,
                owner_user_id="owner-1",
                access_level="private",
            )
            assert resource_access_service.can_use_resource(
                owner, "show_page", "legacy-page", connection=connection
            )
            assert resource_access_service.can_manage_resource_acl(
                owner, "show_page", "legacy-page", connection=connection
            )
            assert resource_access_service.can_manage_show_page_access(
                owner, "legacy-page", connection=connection
            )
            assert resource_access_service.can_control_resource_sharing(
                owner, "show_page", "legacy-page", connection=connection
            )
            assert resource_access_service.filter_accessible_resources(
                owner,
                "show_page",
                [{"session_id": "legacy-page"}],
                connection=connection,
            ) == [{"session_id": "legacy-page"}]
            policy = resource_access_service.get_resource_policy(
                "show_page", "legacy-page", connection=connection
            )
        assert policy is not None
        assert policy["organization_id"] == "org-1"
    finally:
        engine.dispose()


def test_show_page_legacy_reconciliation_reopens_after_authority_lookup(
    monkeypatch,
    tmp_path,
) -> None:
    """An authority lookup must finish before the legacy policy read snapshot."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.example"
    cloud.instance_id = "inst-1"
    cloud.instance_secret = "device-secret"
    cloud.instance_kind = "organization"
    config.save()
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    owner = _context("owner-1", instance_role="editor")

    try:
        with engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id="legacy-page",
                organization_id=None,
                owner_user_id="owner-1",
                access_level="private",
            )

        def resolve_ownership() -> dict[str, str]:
            # A concurrent writer commits while the authoritative lookup is
            # in flight. The later reconciliation must use a fresh snapshot.
            with engine.begin() as writer:
                writer.execute(
                    state_meta.insert().values(
                        key="interleaved-lookup",
                        value_json="{}",
                        updated_at="now",
                    )
                )
            return {
                "mode": "organization",
                "instance_id": "inst-1",
                "organization_id": "org-1",
                "source": "live",
            }

        monkeypatch.setattr(permissions, "resolve_current_instance_ownership", resolve_ownership)

        with engine.begin() as connection:
            assert resource_access_service.can_use_resource(
                owner,
                "show_page",
                "legacy-page",
                connection=connection,
            )

        with engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page",
                "legacy-page",
                connection=connection,
            )
        assert policy is not None
        assert policy["organization_id"] == "org-1"
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


def test_deferred_remote_context_remains_valid_past_authorization_refresh_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    from vibe import remote_access

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "organization-instance"
    config.remote_access.vibe_cloud.instance_kind = "organization"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

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
        instance_kind="organization",
        instance_id="organization-instance",
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


def test_deferred_personal_context_keeps_instance_kind(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "personal-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

    context = resource_access_service.ResourceUserContext(
        subject="personal-user",
        instance_id="personal-instance",
        instance_role="editor",
        instance_access_source="owner",
        instance_kind="personal",
        is_remote=True,
    )

    metadata = resource_access_service.metadata_with_resource_user_context({}, context)
    restored = resource_access_service.resource_user_context_from_metadata(metadata)

    assert restored is not None
    assert restored.is_personal_instance


def test_deferred_context_from_previous_pairing_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "current-instance"
    config.remote_access.vibe_cloud.instance_kind = "organization"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

    stale_context = resource_access_service.ResourceUserContext(
        subject="personal-user",
        instance_id="previous-instance",
        instance_role="editor",
        instance_access_source="owner",
        instance_kind="personal",
        is_remote=True,
    )
    metadata = resource_access_service.metadata_with_resource_user_context({}, stale_context)

    assert resource_access_service.resource_user_context_from_metadata(metadata) is None
    assert not resource_access_service.metadata_allows_harness_runtime(metadata)


@pytest.mark.parametrize("pairing_state", ["disabled", "unreadable", "partial"])
def test_explicit_deferred_context_requires_current_pairing(
    monkeypatch,
    tmp_path,
    pairing_state: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = pairing_state != "disabled"
    config.remote_access.vibe_cloud.instance_id = "personal-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    if pairing_state != "partial":
        config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()
    if pairing_state == "unreadable":
        def fail_load(_cls, *_args, **_kwargs):
            raise OSError("config unavailable")

        monkeypatch.setattr(V2Config, "load", classmethod(fail_load))

    context = resource_access_service.ResourceUserContext(
        subject="personal-user",
        instance_id="personal-instance",
        instance_role="editor",
        instance_access_source="owner",
        instance_kind="personal",
        is_remote=True,
    )
    metadata = resource_access_service.metadata_with_resource_user_context({}, context)

    assert resource_access_service.resource_user_context_from_metadata(metadata) is None
    assert not resource_access_service.metadata_allows_harness_runtime(metadata)


@pytest.mark.parametrize(
    ("paired_kind", "expected_kind", "should_restore"),
    [
        ("personal", "personal", False),
        ("organization", "organization", False),
        ("", None, True),
    ],
)
def test_unbound_legacy_deferred_context_cannot_adopt_current_pairing_kind(
    monkeypatch,
    tmp_path,
    paired_kind: str,
    expected_kind: str | None,
    should_restore: bool,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_kind = paired_kind
    config.remote_access.vibe_cloud.instance_id = "paired-instance"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

    legacy_metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-user",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "claims_issued_at": 1_700_000_000,
        }
    }
    restored = resource_access_service.resource_user_context_from_metadata(legacy_metadata)

    assert (restored is not None) is should_restore
    if restored is not None:
        assert restored.instance_kind == expected_kind


def test_kindless_deferred_context_adopts_matching_validated_pairing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "paired-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

    legacy_metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-user",
            "vibe_instance_id": "paired-instance",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "owner",
            "vibe_instance_kind": None,
            "claims_issued_at": 1_700_000_000,
        }
    }

    restored = resource_access_service.resource_user_context_from_metadata(legacy_metadata)

    assert restored is not None
    assert restored.is_personal_instance


@pytest.mark.parametrize(
    ("paired_kind", "access_source", "organization_claims"),
    [
        ("personal", "owner", False),
        ("organization", "organization_group", True),
        ("organization", "email", False),
    ],
)
def test_migrate_legacy_deferred_contexts_binds_definitions_and_queued_deliveries(
    monkeypatch,
    tmp_path,
    paired_kind: str,
    access_source: str,
    organization_claims: bool,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "paired-instance"
    config.remote_access.vibe_cloud.instance_kind = paired_kind
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

    legacy_context = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": access_source,
        "claims_issued_at": 1_700_000_000,
    }
    if organization_claims:
        legacy_context.update(
            {
                "vibe_organization_id": "org-1",
                "vibe_organization_member_id": "member-1",
                "vibe_organization_role": "member",
                "vibe_group_ids": ["group-1"],
            }
        )
    legacy_metadata = {resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: legacy_context}
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        assert store.upsert_scheduled_task(
            {
                "id": "legacy-task",
                "name": "legacy task",
                "message": "run",
                "schedule_type": "interval",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "metadata": legacy_metadata,
            }
        )
        assert store.upsert_watch(
            {
                "id": "legacy-watch",
                "name": "legacy watch",
                "message": "watch",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "metadata": legacy_metadata,
            }
        )
        assert store.enqueue_definition_run(
            {
                "id": "run-1",
                "definition_id": "legacy-task",
                "run_type": "scheduled",
                "source_kind": "scheduler",
                "prompt": "run",
                "message": "run",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "metadata": legacy_metadata,
            }
        ) is not None
        with engine.begin() as connection:
            connection.execute(
                agent_sessions.insert().values(
                    id="session-1",
                    scope_id=None,
                    agent_id=None,
                    agent_name="default",
                    agent_backend="opencode",
                    agent_variant="default",
                    model=None,
                    reasoning_effort=None,
                    session_anchor="anchor-1",
                    workdir=None,
                    native_session_id="native-1",
                    title=None,
                    status="active",
                    visibility="foreground",
                    pinned=0,
                    agent_status="idle",
                    composer_draft_text=None,
                    composer_draft_updated_at=None,
                    metadata_json="{}",
                    created_at="2026-08-20T00:00:00Z",
                    updated_at="2026-08-20T00:00:00Z",
                    last_active_at=None,
                )
            )
            enqueue_queued(
                connection,
                scope_id=None,
                session_id="session-1",
                text="queued",
                metadata=legacy_metadata,
            )
            from storage.importer import _run_sqlite_data_migrations

            counts = _run_sqlite_data_migrations(connection)
            assert counts == {
                "legacy_deferred_definitions": 2,
                "legacy_deferred_runs": 1,
                "legacy_deferred_deliveries": 1,
            }

            definition_rows = connection.execute(
                select(run_definitions.c.metadata_json).where(
                    run_definitions.c.id.in_(("legacy-task", "legacy-watch"))
                )
            ).scalars()
            for raw_metadata in definition_rows:
                metadata = json.loads(raw_metadata)
                snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
                assert snapshot["vibe_instance_id"] == "paired-instance"
                assert snapshot["vibe_instance_kind"] == paired_kind
                assert resource_access_service.resource_user_context_from_metadata(metadata) is not None

            run_metadata = connection.execute(
                select(agent_runs.c.metadata_json).where(agent_runs.c.definition_id == "legacy-task")
            ).scalar_one()
            run_snapshot = json.loads(run_metadata)[
                resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY
            ]
            assert run_snapshot["vibe_instance_id"] == "paired-instance"
            assert run_snapshot["vibe_instance_kind"] == paired_kind

            delivery = connection.execute(
                select(message_deliveries.c.snapshot_json, message_deliveries.c.snapshot_sha256).where(
                    message_deliveries.c.session_id == "session-1"
                )
            ).mappings().one()
            snapshot = json.loads(delivery["snapshot_json"])
            metadata = json.loads(snapshot["metadata_json"])
            assert metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY][
                "vibe_instance_id"
            ] == "paired-instance"
            assert metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY][
                "vibe_instance_kind"
            ] == paired_kind
            assert delivery["snapshot_sha256"] == hashlib.sha256(
                delivery["snapshot_json"].encode("utf-8")
            ).hexdigest()

            assert _run_sqlite_data_migrations(
                connection
            ) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
    finally:
        store.close()
        engine.dispose()


def test_legacy_migration_keeps_opposite_instance_semantics_unbound(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "personal-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()
    legacy_metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "organization-user",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-1",
            "vibe_organization_role": "member",
        }
    }
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        assert store.upsert_scheduled_task(
            {
                "id": "organization-task",
                "name": "organization task",
                "message": "run",
                "schedule_type": "interval",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "metadata": legacy_metadata,
            }
        )
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            assert _run_sqlite_data_migrations(connection) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            raw_metadata = connection.execute(
                select(run_definitions.c.metadata_json).where(
                    run_definitions.c.id == "organization-task"
                )
            ).scalar_one()
            metadata = json.loads(raw_metadata)
            assert "vibe_instance_id" not in metadata[
                resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY
            ]
            assert resource_access_service.resource_user_context_from_metadata(metadata) is None
    finally:
        store.close()
        engine.dispose()


def test_legacy_migration_does_not_bind_after_later_pairing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    legacy_metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-user",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "owner",
            "claims_issued_at": 1_700_000_000,
        }
    }
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        assert store.upsert_scheduled_task(
            {
                "id": "legacy-task",
                "name": "legacy task",
                "message": "run",
                "schedule_type": "interval",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "metadata": legacy_metadata,
            }
        )
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            assert _run_sqlite_data_migrations(connection) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            assert connection.execute(
                select(state_meta.c.value_json).where(
                    state_meta.c.key
                    == resource_access_service.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
                )
            ).scalar_one_or_none()

        config.remote_access.vibe_cloud.enabled = True
        config.remote_access.vibe_cloud.instance_id = "later-instance"
        config.remote_access.vibe_cloud.instance_kind = "personal"
        config.remote_access.vibe_cloud.instance_secret = "instance-secret"
        config.save()

        with engine.begin() as connection:
            assert _run_sqlite_data_migrations(connection) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            raw_metadata = connection.execute(
                select(run_definitions.c.metadata_json).where(
                    run_definitions.c.id == "legacy-task"
                )
            ).scalar_one()
            metadata = json.loads(raw_metadata)
            snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
            assert "vibe_instance_id" not in snapshot
            assert resource_access_service.resource_user_context_from_metadata(metadata) is None
    finally:
        store.close()
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
