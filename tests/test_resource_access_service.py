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
from storage.models import agent_runs, agent_sessions, message_deliveries, resource_access_policies, run_definitions, state_meta
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


def test_resource_acl_is_enforced_for_editors_and_unknown_kinds_fail_closed(tmp_path, sqlite_schema_db_factory) -> None:
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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


def test_personal_editor_uses_all_agents_without_organization_acl(tmp_path, sqlite_schema_db_factory) -> None:
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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


def test_show_page_is_no_longer_a_resource_kind(monkeypatch, tmp_path, sqlite_schema_db_factory) -> None:
    """§3.2 retired show_page from the Resource ACL: every entry point fails
    closed with ``invalid_resource_kind`` and nothing reads or writes a
    ``resource_access_policies`` row for it.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    owner = _context("owner-1", instance_role="owner", access_source="owner")
    try:
        with engine.begin() as connection:
            with pytest.raises(resource_access_service.ResourceAccessError, match="invalid_resource_kind"):
                resource_access_service.ensure_resource_policy(
                    connection,
                    resource_kind="show_page",
                    resource_id="legacy-page",
                    organization_id=None,
                    owner_user_id="owner-1",
                    access_level="private",
                )
        with engine.connect() as connection:
            for call in (
                lambda: resource_access_service.can_use_resource(
                    owner, "show_page", "legacy-page", connection=connection
                ),
                lambda: resource_access_service.can_manage_resource_acl(
                    owner, "show_page", "legacy-page", connection=connection
                ),
                lambda: resource_access_service.can_control_resource_sharing(
                    owner, "show_page", "legacy-page", connection=connection
                ),
                lambda: resource_access_service.filter_accessible_resources(
                    owner,
                    "show_page",
                    [{"session_id": "legacy-page"}],
                    connection=connection,
                ),
                lambda: resource_access_service.get_resource_policy(
                    "show_page", "legacy-page", connection=connection
                ),
            ):
                with pytest.raises(resource_access_service.ResourceAccessError, match="invalid_resource_kind"):
                    call()
            assert "show_page" not in resource_access_service.RESOURCE_KINDS
    finally:
        engine.dispose()


def _insert_legacy_show_page_policy(connection, *, organization_id: str | None, resource_id: str = "legacy-page") -> None:
    """Write a retired-kind policy row exactly as an older release shipped it."""
    now = "2026-07-27T20:00:00.000000+00:00"
    connection.execute(
        resource_access_policies.insert().values(
            resource_kind="show_page",
            resource_id=resource_id,
            organization_id=organization_id,
            owner_user_id="owner-1",
            owner_email=None,
            access_level="public",
            created_by_user_id="owner-1",
            updated_by_user_id="owner-1",
            policy_revision=1,
            last_applied_control_plane_revision=1,
            created_at=now,
            updated_at=now,
        )
    )


def test_retired_show_page_rows_stay_inert_in_unscoped_queries(tmp_path, sqlite_schema_db_factory) -> None:
    """§3.2 kept legacy ``show_page`` policy rows in place for load safety, but
    unscoped queries must not resurrect them: neither the sync organization
    enumeration nor an unscoped policy listing may surface a retired kind.
    """

    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            _seed_policies(connection)
            _insert_legacy_show_page_policy(connection, organization_id="legacy-org")

        with engine.connect() as connection:
            # The row is present on disk, so any exclusion below is a query
            # filter, not a failed insert.
            retained = connection.execute(
                resource_access_policies.select().where(
                    resource_access_policies.c.resource_kind == "show_page"
                )
            ).mappings().all()
            assert len(retained) == 1

            assert resource_access_service.list_resource_organization_ids(connection=connection) == ["org-1"]
            policies = resource_access_service.list_resource_policies(connection=connection)
            assert [policy["resource_kind"] for policy in policies] == [
                "agent",
                "agent",
                "agent",
            ]
            assert all(policy["organization_id"] == "org-1" for policy in policies)
    finally:
        engine.dispose()


def test_show_page_instance_ownership_fence_survives_config_failure(
    monkeypatch,
    tmp_path,
) -> None:
    """The pairing fence moved to ``vibe.permissions`` and still fails closed
    to ``configuration_unavailable`` when the config cannot be loaded.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    V2Config.default().save()

    def fail_load(_cls, *_args, **_kwargs):
        raise OSError("config unavailable")

    monkeypatch.setattr(V2Config, "load", classmethod(fail_load))

    resolved = permissions.resolve_current_instance_ownership()
    assert resolved["mode"] == permissions.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE
    assert resolved["organization_id"] is None


def test_show_page_instance_ownership_is_unmanaged_without_pairing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    V2Config.default().save()

    ownership = permissions.current_show_page_instance_ownership()
    assert ownership == {
        "mode": "unmanaged",
        "instance_id": None,
        "organization_id": None,
        "source": "config",
    }


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


def test_active_org_members_can_use_legacy_resources_without_forging_local_identity(tmp_path, sqlite_schema_db_factory) -> None:
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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


def test_non_member_role_rank_does_not_bypass_organization_resource_acl(tmp_path, sqlite_schema_db_factory) -> None:
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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
    from storage import remote_access_authorization_service as _auth

    started = _auth.begin_instance_binding_transition(
        instance_id="organization-instance",
        instance_kind="organization",
    )
    _auth.complete_instance_binding_transition(
        instance_id="organization-instance",
        instance_kind="organization",
        generation=started["generation"],
    )

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
    from storage import remote_access_authorization_service as _auth

    started = _auth.begin_instance_binding_transition(
        instance_id="personal-instance",
        instance_kind="personal",
    )
    _auth.complete_instance_binding_transition(
        instance_id="personal-instance",
        instance_kind="personal",
        generation=started["generation"],
    )

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


def test_deferred_context_does_not_project_personal_bypass_while_reconciling(
    monkeypatch,
    tmp_path,
) -> None:
    """Non-interactive metadata must fail closed while the binding is reconciling."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "current-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.save()

    from storage import remote_access_authorization_service
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    remote_access_authorization_service.begin_instance_binding_transition(
        instance_id="current-instance",
        instance_kind="personal",
    )

    personal_context = resource_access_service.ResourceUserContext(
        subject="personal-user",
        instance_id="current-instance",
        instance_role="editor",
        instance_access_source="owner",
        instance_kind="personal",
        is_remote=True,
    )
    metadata = resource_access_service.metadata_with_resource_user_context({}, personal_context)
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

    from storage import remote_access_authorization_service as _auth

    started = _auth.begin_instance_binding_transition(
        instance_id="paired-instance",
        instance_kind="personal",
    )
    _auth.complete_instance_binding_transition(
        instance_id="paired-instance",
        instance_kind="personal",
        generation=started["generation"],
    )
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
    organization_claims: bool, sqlite_schema_db_factory,
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
    sqlite_schema_db_factory(db)
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

            _seed_ready_binding(connection, instance_id="paired-instance", instance_kind=paired_kind)
            counts = _run_sqlite_data_migrations(connection)
            assert _migration_counts(counts) == {
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
                from storage import remote_access_authorization_service as _auth

                started = _auth.begin_instance_binding_transition(
                    instance_id="paired-instance",
                    instance_kind=paired_kind,
                )
                _auth.complete_instance_binding_transition(
                    instance_id="paired-instance",
                    instance_kind=paired_kind,
                    generation=started["generation"],
                )
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

            assert _migration_counts(_run_sqlite_data_migrations(
                connection
            )) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
    finally:
        store.close()
        engine.dispose()


def test_legacy_migration_keeps_opposite_instance_semantics_unbound(monkeypatch, tmp_path, sqlite_schema_db_factory) -> None:
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
    sqlite_schema_db_factory(db)
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
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
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


def test_legacy_migration_does_not_bind_after_later_pairing(monkeypatch, tmp_path, sqlite_schema_db_factory) -> None:
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
    sqlite_schema_db_factory(db)
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
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
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
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
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


def test_legacy_migration_retries_when_same_pairing_kind_is_backfilled(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "same-instance"
    config.remote_access.vibe_cloud.instance_kind = ""
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
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }

        config.remote_access.vibe_cloud.instance_kind = "personal"
        config.save()

        with engine.begin() as connection:
            _seed_ready_binding(connection, instance_id="same-instance", instance_kind="personal")
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                "legacy_deferred_definitions": 1,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            raw_metadata = connection.execute(
                select(run_definitions.c.metadata_json).where(
                    run_definitions.c.id == "legacy-task"
                )
            ).scalar_one()
            snapshot = json.loads(raw_metadata)[
                resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY
            ]
            assert snapshot["vibe_instance_id"] == "same-instance"
            assert snapshot["vibe_instance_kind"] == "personal"
    finally:
        store.close()
        engine.dispose()


def test_legacy_migration_preserves_instance_id_while_pairing_is_partial(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "same-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = ""
    config.save()
    legacy_metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-user",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "claims_issued_at": 1_700_000_000,
        }
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            marker = json.loads(
                connection.execute(
                    select(state_meta.c.value_json).where(
                        state_meta.c.key
                        == resource_access_service.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
                    )
                ).scalar_one()
            )
            assert marker["state"] == "pending"
            assert marker["instance_id"] == "same-instance"

        config.remote_access.vibe_cloud.instance_secret = "instance-secret"
        config.save()

        with engine.begin() as connection:
            _seed_ready_binding(connection, instance_id="same-instance", instance_kind="personal")
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                "legacy_deferred_definitions": 1,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            raw_metadata = connection.execute(
                select(run_definitions.c.metadata_json).where(
                    run_definitions.c.id == "legacy-task"
                )
            ).scalar_one()
            snapshot = json.loads(raw_metadata)[
                resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY
            ]
            assert snapshot["vibe_instance_id"] == "same-instance"
            assert snapshot["vibe_instance_kind"] == "personal"
    finally:
        store.close()
        engine.dispose()


def test_typed_binding_reader_preserves_partial_identity_and_does_not_latch_read_failure(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.instance_id = "same-instance"
    cloud.instance_kind = "personal"
    cloud.instance_secret = ""
    config.save()

    partial = resource_access_service._configured_resource_state()
    assert partial.status == resource_access_service.RESOURCE_BINDING_STATE_PARTIAL
    assert partial.instance_id == "same-instance"
    assert partial.instance_kind == "personal"

    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    marker = {
        "schema_version": 2,
        "state": "pending",
        "instance_id": "same-instance",
        "updated_at": "before-read-failure",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                state_meta.insert().values(
                    key=resource_access_service.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
                    value_json=json.dumps(marker),
                    updated_at="before-read-failure",
                )
            )

        monkeypatch.setattr(
            V2Config,
            "load",
            classmethod(lambda cls, *args, **kwargs: (_ for _ in ()).throw(OSError("temporary read"))),
        )
        unavailable = resource_access_service._configured_resource_state()
        assert unavailable.status == resource_access_service.RESOURCE_BINDING_STATE_UNAVAILABLE

        with engine.begin() as connection:
            assert _migration_counts(resource_access_service.migrate_legacy_deferred_resource_contexts(connection)) == {
                "legacy_deferred_definitions": 0,
                "legacy_deferred_runs": 0,
                "legacy_deferred_deliveries": 0,
            }
            stored = connection.execute(
                select(state_meta.c.value_json).where(
                    state_meta.c.key == resource_access_service.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
                )
            ).scalar_one()
            assert json.loads(stored) == marker
    finally:
        engine.dispose()


_EMPTY_MIGRATION_COUNTS = {
    "legacy_deferred_definitions": 0,
    "legacy_deferred_runs": 0,
    "legacy_deferred_deliveries": 0,
}


def _migration_counts(result: dict) -> dict[str, int]:
    """Numeric migration counts, ignoring the typed binding-status field."""

    return {
        key: int(result[key])
        for key in (
            "legacy_deferred_definitions",
            "legacy_deferred_runs",
            "legacy_deferred_deliveries",
        )
    }


def _paired_cloud_config(
    tmp_path,
    *,
    enabled: bool = True,
    instance_id: str = "same-instance",
    instance_kind: str = "personal",
    instance_secret: str = "instance-secret",
) -> V2Config:
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = enabled
    cloud.instance_id = instance_id
    cloud.instance_kind = instance_kind
    cloud.instance_secret = instance_secret
    config.save()
    return config


def _seed_ready_binding(connection, *, instance_id: str, instance_kind: str, generation: int = 1) -> None:
    """Write a validated ready binding onto THIS connection's database."""

    payload = json.dumps(
        {
            "schema_version": 1,
            "state": "ready",
            "instance_id": instance_id,
            "instance_kind": instance_kind,
            "generation": generation,
            "updated_at": "1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        state_meta.insert().values(
            key="remote_access.instance_binding.v1",
            value_json=payload,
            updated_at="1",
        )
    )


def _seed_legacy_scheduled_task(store, *, definition_id: str, snapshot: dict) -> None:
    assert store.upsert_scheduled_task(
        {
            "id": definition_id,
            "name": definition_id,
            "message": "run",
            "schedule_type": "interval",
            "created_at": "2026-08-20T00:00:00Z",
            "updated_at": "2026-08-20T00:00:00Z",
            "metadata": {resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: snapshot},
        }
    )


def _legacy_definition_metadata(connection, definition_id: str) -> dict:
    raw = connection.execute(
        select(run_definitions.c.metadata_json).where(run_definitions.c.id == definition_id)
    ).scalar_one()
    return json.loads(raw)


def _stored_migration_marker(connection) -> str | None:
    return connection.execute(
        select(state_meta.c.value_json).where(
            state_meta.c.key == resource_access_service.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
        )
    ).scalar_one_or_none()


def test_partial_pairing_marker_records_configured_instance_without_claiming_a_kind(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    """A deferred opportunity keeps its identity but never a guessed kind."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path, instance_secret="")

    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    try:
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            marker = json.loads(_stored_migration_marker(connection))

        assert marker["schema_version"] == 2
        assert marker["state"] == "pending"
        assert marker["instance_id"] == "same-instance"
        # A partial pairing cannot prove its kind, so the marker must not
        # record one and must not look terminal to a later startup.
        assert "instance_kind" not in marker
        assert "completed_at" not in marker
    finally:
        engine.dispose()


def test_credential_repair_on_the_same_pairing_completes_the_deferred_migration(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = _paired_cloud_config(tmp_path, instance_secret="")
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS

        config.remote_access.vibe_cloud.instance_secret = "instance-secret"
        config.save()

        with engine.begin() as connection:
            _seed_ready_binding(connection, instance_id="same-instance", instance_kind="personal")
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                **_EMPTY_MIGRATION_COUNTS,
                "legacy_deferred_definitions": 1,
            }
            metadata = _legacy_definition_metadata(connection, "legacy-task")
            snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
            assert snapshot["vibe_instance_id"] == "same-instance"
            assert snapshot["vibe_instance_kind"] == "personal"
            from storage import remote_access_authorization_service as _auth

            started = _auth.begin_instance_binding_transition(
                instance_id="same-instance",
                instance_kind="personal",
            )
            _auth.complete_instance_binding_transition(
                instance_id="same-instance",
                instance_kind="personal",
                generation=started["generation"],
            )
            assert resource_access_service.resource_user_context_from_metadata(metadata) is not None
            completed = json.loads(_stored_migration_marker(connection))

        assert completed["state"] == "completed"
        assert completed["instance_id"] == "same-instance"
        assert completed["instance_kind"] == "personal"
        assert completed["completed_at"]

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            assert json.loads(_stored_migration_marker(connection)) == completed
            assert (
                _legacy_definition_metadata(connection, "legacy-task") == metadata
            )
    finally:
        store.close()
        engine.dispose()


def test_transient_configuration_failure_leaves_the_migration_retryable(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        from storage.importer import _run_sqlite_data_migrations

        real_load = V2Config.load
        with monkeypatch.context() as unreadable:
            unreadable.setattr(
                V2Config,
                "load",
                classmethod(
                    lambda cls, *args, **kwargs: (_ for _ in ()).throw(OSError("temporary read"))
                ),
            )
            with engine.begin() as connection:
                assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
                # No first migration opportunity may be recorded from a read
                # failure; otherwise the retry below would be fenced out.
                assert _stored_migration_marker(connection) is None

        assert V2Config.load.__func__ is real_load.__func__

        with engine.begin() as connection:
            _seed_ready_binding(connection, instance_id="same-instance", instance_kind="personal")
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                **_EMPTY_MIGRATION_COUNTS,
                "legacy_deferred_definitions": 1,
            }
            metadata = _legacy_definition_metadata(connection, "legacy-task")
            from storage import remote_access_authorization_service as _auth

            started = _auth.begin_instance_binding_transition(
                instance_id="same-instance",
                instance_kind="personal",
            )
            _auth.complete_instance_binding_transition(
                instance_id="same-instance",
                instance_kind="personal",
                generation=started["generation"],
            )
            assert resource_access_service.resource_user_context_from_metadata(metadata) is not None
    finally:
        store.close()
        engine.dispose()


def test_unpaired_first_opportunity_cannot_be_adopted_by_a_later_pairing(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = _paired_cloud_config(
        tmp_path,
        enabled=False,
        instance_id="",
        instance_kind="",
        instance_secret="",
    )
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "owner",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            sealed = json.loads(_stored_migration_marker(connection))

        assert sealed["state"] == "sealed_unattributed"
        assert sealed["instance_id"] is None

        cloud = config.remote_access.vibe_cloud
        cloud.enabled = True
        cloud.instance_id = "later-instance"
        cloud.instance_kind = "personal"
        cloud.instance_secret = "instance-secret"
        config.save()

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            metadata = _legacy_definition_metadata(connection, "legacy-task")
            snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
            assert "vibe_instance_id" not in snapshot
            assert resource_access_service.resource_user_context_from_metadata(metadata) is None
            assert json.loads(_stored_migration_marker(connection))["state"] == "sealed_unattributed"
    finally:
        store.close()
        engine.dispose()


def test_pending_marker_for_one_instance_cannot_be_adopted_by_another_instance(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = _paired_cloud_config(tmp_path, instance_id="instance-a", instance_secret="")
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            pending = json.loads(_stored_migration_marker(connection))
        assert pending["state"] == "pending"
        assert pending["instance_id"] == "instance-a"

        cloud = config.remote_access.vibe_cloud
        cloud.instance_id = "instance-b"
        cloud.instance_secret = "instance-secret"
        config.save()

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            sealed = json.loads(_stored_migration_marker(connection))
            assert sealed["state"] == "sealed_unattributed"
            # The original owner stays recorded so no later pairing, including
            # instance A itself, can reopen the sealed opportunity.
            assert sealed["instance_id"] == "instance-a"

        cloud.instance_id = "instance-a"
        config.save()

        with engine.begin() as connection:
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
            metadata = _legacy_definition_metadata(connection, "legacy-task")
            snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
            assert "vibe_instance_id" not in snapshot
            assert resource_access_service.resource_user_context_from_metadata(metadata) is None
    finally:
        store.close()
        engine.dispose()


@pytest.mark.parametrize("paired_kind", ["organization", "personal"])
@pytest.mark.parametrize("access_source", ["email", "email_domain", "public_instance"])
def test_shared_access_source_snapshots_migrate_for_either_pairing_kind(
    monkeypatch,
    tmp_path,
    paired_kind: str,
    access_source: str, sqlite_schema_db_factory,
) -> None:
    """``email``/``email_domain``/``public_instance`` are kind-agnostic.

    Neither their presence nor the absence of Organization membership claims
    is instance-kind evidence, so the still-current pairing decides.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path, instance_id="paired-instance", instance_kind=paired_kind)
    legacy_snapshot = {
        "sub": "legacy-editor",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": access_source,
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            _seed_ready_binding(connection, instance_id="paired-instance", instance_kind=paired_kind)
            assert _migration_counts(_run_sqlite_data_migrations(connection)) == {
                **_EMPTY_MIGRATION_COUNTS,
                "legacy_deferred_definitions": 1,
            }
            metadata = _legacy_definition_metadata(connection, "legacy-task")
            snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
            assert snapshot["vibe_instance_id"] == "paired-instance"
            assert snapshot["vibe_instance_kind"] == paired_kind
            from storage import remote_access_authorization_service as _auth

            started = _auth.begin_instance_binding_transition(
                instance_id="paired-instance",
                instance_kind=paired_kind,
            )
            _auth.complete_instance_binding_transition(
                instance_id="paired-instance",
                instance_kind=paired_kind,
                generation=started["generation"],
            )
            restored = resource_access_service.resource_user_context_from_metadata(metadata)
            assert restored is not None
            assert restored.is_personal_instance is (paired_kind == "personal")
            # Organization membership is never synthesized by the migration.
            assert "vibe_organization_id" not in snapshot
    finally:
        store.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("marker_json", "expected_state", "expect_marker_preserved"),
    [
        pytest.param(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "completed",
                    "instance_id": None,
                    "completed_at": "2026-08-19T00:00:00Z",
                    "updated_at": "2026-08-19T00:00:00Z",
                }
            ),
            "sealed_unattributed",
            True,
            id="released_completed_without_instance",
        ),
        pytest.param(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "completed",
                    "instance_id": "same-instance",
                    "completed_at": "2026-08-19T00:00:00Z",
                    "updated_at": "2026-08-19T00:00:00Z",
                }
            ),
            "completed",
            True,
            id="released_completed_for_current_instance",
        ),
        pytest.param(
            json.dumps({"instance_id": "same-instance", "completed_at": "2026-08-19T00:00:00Z"}),
            "completed",
            True,
            id="released_marker_without_state_field",
        ),
        pytest.param(
            json.dumps({"instance_id": None}),
            "sealed_unattributed",
            False,
            id="released_marker_without_state_or_instance",
        ),
        pytest.param(
            json.dumps({"state": "pending", "instance_id": "   "}),
            None,
            True,
            id="blank_instance_id",
        ),
        pytest.param(
            json.dumps({"state": "sealed_unattributed", "instance_id": "same-instance"}),
            "sealed_unattributed",
            True,
            id="sealed_for_current_instance",
        ),
        pytest.param("not-json", None, True, id="corrupt_marker"),
        pytest.param(json.dumps(["completed"]), None, True, id="non_object_marker"),
    ],
)
def test_released_migration_marker_shapes_stay_idempotent_and_fail_closed(
    monkeypatch,
    tmp_path,
    marker_json: str,
    expected_state: str | None,
    expect_marker_preserved: bool, sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        with engine.begin() as connection:
            connection.execute(
                state_meta.insert().values(
                    key=resource_access_service.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
                    value_json=marker_json,
                    updated_at="2026-08-19T00:00:00Z",
                )
            )
        from storage.importer import _run_sqlite_data_migrations

        for _ in range(2):
            with engine.begin() as connection:
                assert _migration_counts(_run_sqlite_data_migrations(connection)) == _EMPTY_MIGRATION_COUNTS
                metadata = _legacy_definition_metadata(connection, "legacy-task")
                snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
                assert "vibe_instance_id" not in snapshot
                assert resource_access_service.resource_user_context_from_metadata(metadata) is None
                stored = _stored_migration_marker(connection)
                if expect_marker_preserved:
                    # A terminal or unreadable marker is never rewritten, so
                    # repeated startups cannot churn the released shape.
                    assert stored == marker_json
                else:
                    assert json.loads(stored)["state"] == expected_state
    finally:
        store.close()


def test_member_resource_user_context_snapshot_round_trips() -> None:
    context = resource_access_service.ResourceUserContext(
        subject="member-1",
        organization_id="org-1",
        organization_member_id="organization-member-1",
        organization_role="member",
        group_ids=frozenset({"group-engineering"}),
        membership_version="membership-v2",
        instance_role="member",
        instance_access_source="email",
        claims_issued_at=1_700_000_000,
        is_remote=True,
    )
    metadata = resource_access_service.metadata_with_resource_user_context({}, context)
    restored = resource_access_service.resource_user_context_from_metadata(metadata)
    assert restored is not None
    assert restored.instance_role == "member"
    assert restored.can_manage_instance
    assert not restored.can_manage_access_members
    assert restored.has_role("editor")


def test_pre_member_resource_user_context_snapshot_keeps_editor_role() -> None:
    context = resource_access_service.ResourceUserContext(
        subject="editor-1",
        organization_id="org-1",
        organization_member_id="organization-member-1",
        organization_role="member",
        instance_role="editor",
        instance_access_source="email",
        claims_issued_at=1_700_000_000,
        is_remote=True,
    )
    metadata = resource_access_service.metadata_with_resource_user_context({}, context)
    restored = resource_access_service.resource_user_context_from_metadata(metadata)
    assert restored is not None
    assert restored.instance_role == "editor"
    assert not restored.can_manage_instance


def test_organization_member_remains_subject_to_resource_acl_for_use(tmp_path, sqlite_schema_db_factory) -> None:
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as connection:
            _seed_policies(connection)
            member = _context(
                "member-2",
                instance_role="member",
                group_ids=frozenset({"group-sales"}),
            )
            assert member.can_manage_instance
            assert not resource_access_service.can_use_resource(
                member, "agent", "private-agent", connection=connection
            )
            assert resource_access_service.can_use_resource(
                member, "agent", "public-agent", connection=connection
            )
            assert not resource_access_service.can_use_resource(
                member, "agent", "scoped-agent", connection=connection
            )
    finally:
        engine.dispose()


def test_personal_resources_cannot_use_organization_access_levels(tmp_path, sqlite_schema_db_factory) -> None:
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
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


def test_migration_defers_to_a_disagreeing_durable_binding_row(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    """A binding row from a peer's reclassification outranks the config read.

    Regression: PR #1606 round 1 — the migration running from a bare
    ``ensure_sqlite_state()`` must not bind ambiguous snapshots to a config
    kind that the durable binding row does not (yet) agree with.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path, instance_kind="personal")
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        binding_row = json.dumps(
            {
                "schema_version": 1,
                "state": "ready",
                "instance_id": "same-instance",
                "instance_kind": "organization",
                "generation": 3,
                "updated_at": "1700000000",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            connection.execute(
                state_meta.insert().values(
                    key="remote_access.instance_binding.v1",
                    value_json=binding_row,
                    updated_at="1700000000",
                )
            )
            counts = _migration_counts(_run_sqlite_data_migrations(connection))
            marker = json.loads(_stored_migration_marker(connection))
            metadata = _legacy_definition_metadata(connection, "legacy-task")

        # No snapshot was bound and the opportunity stays retriable.
        assert counts == _EMPTY_MIGRATION_COUNTS
        assert marker["state"] == "pending"
        assert marker["instance_id"] == "same-instance"
        snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
        assert "vibe_instance_kind" not in snapshot
    finally:
        engine.dispose()


def test_recovered_config_is_unavailable_and_does_not_seal_legacy_snapshots(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    """Regression PR #1606 r3: a recovered/defaulted V2Config.load() (broken
    JSON, load_warnings set) is UNAVAILABLE, never authoritative UNPAIRED.
    Repairing the config later must still be able to migrate the snapshots.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = _paired_cloud_config(tmp_path)
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)

        class Recovered:
            load_warnings = ("invalid json recovered to defaults",)
            class remote_access:
                class vibe_cloud:
                    instance_id = ""
                    instance_kind = ""
                    enabled = False
                    @staticmethod
                    def runtime_credentials():
                        return None

        monkeypatch.setattr(V2Config, "load", staticmethod(lambda: Recovered()))
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            counts = _migration_counts(_run_sqlite_data_migrations(connection))
            marker = _stored_migration_marker(connection)

        assert _migration_counts(counts) == _EMPTY_MIGRATION_COUNTS
        # No terminal seal was written.
        if marker is not None:
            payload = json.loads(marker)
            assert payload.get("state") != "sealed_unattributed"

        # Repair: restore a real pairing and migrate.
        monkeypatch.undo()
        monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
        _paired_cloud_config(tmp_path)
        with engine.begin() as connection:
            _seed_ready_binding(connection, instance_id="same-instance", instance_kind="personal")
            repaired = _migration_counts(_run_sqlite_data_migrations(connection))
            metadata = _legacy_definition_metadata(connection, "legacy-task")
        snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
        assert snapshot["vibe_instance_id"] == "same-instance"
        assert snapshot["vibe_instance_kind"] == "personal"
        assert repaired.get("legacy_deferred_definitions", 0) >= 1
    finally:
        engine.dispose()


def test_gate_under_writer_lock_does_not_open_a_second_write_connection(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    """Regression PR #1606 r3: resource_user_context_from_metadata is called
    while a queued-delivery transaction already holds reserve_write_lock.
    Bootstrap must not open a second write connection (it would time out
    and permanently retire the delivery as unauthorized).
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    try:
        from storage.agent_session_rows import reserve_write_lock

        metadata = {
            resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
                "sub": "legacy-user",
                "vibe_instance_role": "editor",
                "vibe_instance_access_source": "email",
                "vibe_instance_id": "same-instance",
                "vibe_instance_kind": "personal",
                "claims_issued_at": 1_700_000_000,
            }
        }
        with engine.begin() as conn:
            reserve_write_lock(conn)
            # Absent binding row + known kind: the gate must fail closed
            # WITHOUT opening a second writer, and without raising.
            context = resource_access_service.resource_user_context_from_metadata(metadata)
            # Fail-closed is acceptable; a timeout / deadlock is not.
            assert context is None or context.instance_id == "same-instance"
    finally:
        engine.dispose()


def test_typed_reader_from_sqlite_migration_does_not_persist_config(
    monkeypatch,
    tmp_path,
) -> None:
    """Regression PR #1606 r4: the typed reader used under an open SQLite
    writer must not persist on-load config migrations (that would invert
    C2 lock order against a peer holding the config lock).
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)
    calls = {"persist": 0}
    real_load = V2Config.load

    def load_counting(*args, **kwargs):
        persist = kwargs.get("persist_migrations", True)
        if persist:
            calls["persist"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(V2Config, "load", staticmethod(load_counting))
    db = tmp_path / "vibe.sqlite"
    run_migrations(db)
    engine = create_sqlite_engine(db)
    try:
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            _run_sqlite_data_migrations(connection)
        assert calls["persist"] == 0
    finally:
        engine.dispose()


def test_terminal_delivery_snapshots_are_left_byte_identical(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    """Regression PR #1606 r4: accepted/retired delivery snapshots are the
    immutable submitted Message candidate and must not be rewritten.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    try:
        from storage.importer import _run_sqlite_data_migrations
        from storage.models import agent_sessions, message_deliveries, scopes
        import hashlib

        snapshot = {
            "metadata_json": json.dumps(
                {
                    resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
                        "sub": "legacy-user",
                        "vibe_instance_role": "editor",
                        "vibe_instance_access_source": "email",
                        "claims_issued_at": 1_700_000_000,
                    }
                }
            )
        }
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        snapshot_sha = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        with engine.begin() as connection:
            connection.execute(
                scopes.insert().values(
                    id="scope_term",
                    platform="avibe",
                    scope_type="project",
                    native_id="proj_term",
                    is_private=0,
                    supports_threads=0,
                    metadata_json="{}",
                    first_seen_at="now",
                    last_seen_at="now",
                    updated_at="now",
                )
            )
            connection.execute(
                agent_sessions.insert().values(
                    id="sess_term",
                    scope_id="scope_term",
                    title="t",
                    status="idle",
                    agent_backend="claude",
                    agent_variant="default",
                    session_anchor="anchor_term",
                    native_session_id="native_term",
                    created_at="now",
                    updated_at="now",
                    last_active_at="now",
                    metadata_json="{}",
                )
            )
            connection.execute(
                message_deliveries.insert().values(
                    id="del_retired",
                    session_id="sess_term",
                    priority="p3",
                    state="retired",
                    snapshot_json=snapshot_json,
                    snapshot_sha256=snapshot_sha,
                    dispatch_sha256=snapshot_sha,
                    submitted_at="now",
                    updated_at="now",
                )
            )
            _run_sqlite_data_migrations(connection)
            rows = {
                row["id"]: row
                for row in connection.execute(
                    select(
                        message_deliveries.c.id,
                        message_deliveries.c.snapshot_json,
                        message_deliveries.c.snapshot_sha256,
                    )
                ).mappings()
            }
        assert rows["del_retired"]["snapshot_json"] == snapshot_json
        assert rows["del_retired"]["snapshot_sha256"] == snapshot_sha
    finally:
        engine.dispose()


def test_absent_binding_fails_closed_for_deferred_personal_context(
    monkeypatch,
    tmp_path,
) -> None:
    """Regression PR #1606 r5: a known-kind pairing with no durable binding
    row must not project a Personal context for deferred execution.
    """

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
    assert resource_access_service.resource_user_context_from_metadata(metadata) is None


def test_migration_stays_pending_without_a_validated_binding_row(
    monkeypatch,
    tmp_path, sqlite_schema_db_factory,
) -> None:
    """Regression PR #1606 r5: known-kind config + absent binding row must
    not complete the deferred-context migration or relabel snapshots.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.instance_id = "same-instance"
    cloud.instance_kind = "personal"
    cloud.instance_secret = "instance-secret"
    config.save()
    legacy_snapshot = {
        "sub": "legacy-user",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "email",
        "claims_issued_at": 1_700_000_000,
    }
    db = tmp_path / "vibe.sqlite"
    sqlite_schema_db_factory(db)
    store = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        _seed_legacy_scheduled_task(store, definition_id="legacy-task", snapshot=legacy_snapshot)
        from storage.importer import _run_sqlite_data_migrations

        with engine.begin() as connection:
            counts = _migration_counts(_run_sqlite_data_migrations(connection))
            marker = json.loads(_stored_migration_marker(connection))
            metadata = _legacy_definition_metadata(connection, "legacy-task")
        assert counts == _EMPTY_MIGRATION_COUNTS
        assert marker["state"] == "pending"
        snapshot = metadata[resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY]
        assert "vibe_instance_kind" not in snapshot
    finally:
        engine.dispose()


def test_unsupported_deferred_snapshot_kind_is_rejected(monkeypatch, tmp_path) -> None:
    """Regression PR #1606 r5: a present-but-unrecognized kind is not a
    no-kind legacy snapshot for deferred execution either.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)
    metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-user",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "vibe_instance_id": "same-instance",
            "vibe_instance_kind": "enterprise",
            "claims_issued_at": 1_700_000_000,
        }
    }
    assert resource_access_service.resource_user_context_from_metadata(metadata) is None


def test_unrelated_section_load_warning_does_not_make_pairing_unavailable(
    monkeypatch,
    tmp_path,
) -> None:
    """Regression PR #1606 r5: Model Hub recovery warnings must not classify
    an intact Vibe Cloud pairing as UNAVAILABLE.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    _paired_cloud_config(tmp_path)

    class IntactPairing:
        load_warnings = ("Recovered invalid config section 'model_hub.sources.vendor': bad",)
        class remote_access:
            class vibe_cloud:
                enabled = True
                instance_id = "same-instance"
                instance_kind = "personal"
                instance_secret = "instance-secret"
                @staticmethod
                def runtime_credentials():
                    return ("https://backend.test", "same-instance", "instance-secret")

    monkeypatch.setattr(V2Config, "load", staticmethod(lambda **kwargs: IntactPairing()))
    state = resource_access_service._configured_resource_state()
    assert state.status == "ready"
    assert state.instance_id == "same-instance"
    assert state.instance_kind == "personal"


def test_kindless_deferred_snapshot_fails_closed_when_pairing_is_unpaired(
    monkeypatch,
    tmp_path,
) -> None:
    """P2 a0D_l: kindless snapshot + unpaired/unavailable config → no Editor context.

    The valid legacy no-kind PAIRING path (PARTIAL, runtime-ready, kind not
    yet backfilled) still returns the original context.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.instance_id = ""
    config.save()
    metadata = {
        resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-user",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "claims_issued_at": 1_700_000_000,
        }
    }
    assert resource_access_service.resource_user_context_from_metadata(metadata) is None

    # Valid legacy no-kind pairing: runtime-ready, kind not yet backfilled.
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "same-instance"
    config.remote_access.vibe_cloud.instance_kind = ""
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.remote_access.vibe_cloud.backend_url = "https://backend.test"
    config.save()
    restored = resource_access_service.resource_user_context_from_metadata(metadata)
    assert restored is not None
    assert restored.instance_role == "editor"
    assert restored.instance_kind is None
