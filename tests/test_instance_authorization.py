import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER,
    REMOTE_HTTP_ALLOWED,
    REMOTE_HTTP_LOCAL_ONLY,
    can_receive_workbench_event,
    context_from_session_payload,
    has_temporary_unrestricted_org_access,
    http_authorization_policy,
    require_instance_role,
    trusted_local_context,
)


def _active_org_context(role: str) -> AuthorizationContext:
    return AuthorizationContext(
        instance_role=role,
        subject=f"{role}-subject",
        instance_access_source="organization_group",
        organization_id="org-1",
        organization_member_id=f"membership-{role}",
        organization_role="member",
        is_remote=True,
    )


def _non_member_context(role: str = "owner") -> AuthorizationContext:
    return AuthorizationContext(
        instance_role=role,
        instance_access_source="owner",
        is_remote=True,
    )


def test_non_member_remote_roles_keep_rank_without_local_machine_capabilities() -> None:
    viewer = _non_member_context("viewer")
    editor = _non_member_context("editor")
    owner = _non_member_context("owner")

    assert viewer.can_read_instance is True
    assert viewer.can_chat is False
    assert viewer.can_use_cloud_asr is False
    assert editor.can_read_instance is True
    assert editor.can_chat is True
    assert editor.can_use_cloud_asr is True
    assert editor.can_manage_projects is False
    assert owner.can_chat is True
    assert owner.can_use_cloud_asr is True
    assert owner.can_manage_projects is True
    assert owner.can_manage_agents is True
    assert owner.can_use_terminal is False
    assert owner.can_use_files is False
    assert owner.can_use_system is False
    assert trusted_local_context().can_manage_instance is True
    assert trusted_local_context().can_chat is True
    assert trusted_local_context().can_use_cloud_asr is False


def test_temporary_org_access_projects_runtime_rights_without_fake_local_capabilities() -> None:
    member = AuthorizationContext(
        instance_role="viewer",
        subject="member-1",
        organization_id="org-1",
        organization_member_id="membership-1",
        organization_role="member",
        instance_access_source="organization_group",
        is_remote=True,
    )

    assert has_temporary_unrestricted_org_access(member) is True
    assert member.capability_projection() == {
        "is_instance_owner": False,
        "can_read_instance": True,
        "can_chat": True,
        "can_manage_projects": True,
        "can_manage_agents": True,
        "can_manage_instance": True,
        "can_use_agents": True,
        "can_use_skills": True,
        "can_use_vault_secrets": True,
        "can_use_show_pages": True,
        "can_use_terminal_files": False,
        "can_use_terminal": False,
        "can_use_files": False,
        "can_use_system": False,
    }
    assert has_temporary_unrestricted_org_access(
        AuthorizationContext(
            **{
                **member.__dict__,
                "organization_member_id": None,
            }
        )
    ) is False
    assert has_temporary_unrestricted_org_access(
        AuthorizationContext(
            **{
                **member.__dict__,
                "instance_access_source": "show_page_email",
            }
        )
    ) is False


def test_temporary_org_members_receive_show_events_at_viewer_instance_role() -> None:
    member = AuthorizationContext(
        instance_role="viewer",
        organization_id="org-1",
        organization_member_id="membership-1",
        organization_role="member",
        instance_access_source="organization_group",
        is_remote=True,
    )
    non_member = AuthorizationContext(
        instance_role="viewer",
        instance_access_source="email",
        is_remote=True,
    )

    assert can_receive_workbench_event(member, "show.event") is True
    assert can_receive_workbench_event(non_member, "show.event") is False


@pytest.mark.parametrize("role", ["viewer", "editor", "owner"])
def test_active_org_members_bypass_role_splits_for_known_runtime_resources(role) -> None:
    context = _active_org_context(role)
    expected = {"agent": True, "skill": True, "vault_secret": True, "show_page": True}

    assert {kind: context.can_use_resource(kind) for kind in expected} == expected
    assert context.can_manage_instance is True
    assert context.can_use_resource("future_resource") is False


def test_context_uses_role_not_diagnostic_source_for_owner() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "email": "editor@example.com",
            "vibe_instance_id": "inst-1",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "owner",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "membership-1",
            "vibe_organization_role": "member",
            "vibe_instance_authorization_revision": 0,
        }
    )

    assert context.is_instance_owner is False
    assert context.can_chat is True
    assert context.can_manage_instance is True
    assert context.authorization_revision == 0


def test_malformed_role_context_fails_closed() -> None:
    context = context_from_session_payload(
        {"vibe_instance_role": "admin", "vibe_instance_access_source": "owner"}
    )

    assert context.is_remote is True
    assert context.can_read_instance is False


def test_show_page_email_context_requires_and_matches_one_exact_target() -> None:
    context = context_from_session_payload(
        {
            "sub": "guest-1",
            "email": "guest@example.com",
            "vibe_instance_id": "inst-1",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "show_page_email",
            "vibe_show_page_id": "session-one",
            "vibe_instance_authorization_revision": 0,
        }
    )

    assert context.can_use_show_page("session-one") is True
    assert context.can_use_show_page("session-two") is False
    assert context.show_page_id == "session-one"

    missing_target = context_from_session_payload(
        {
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "show_page_email",
        }
    )
    broader_target = context_from_session_payload(
        {
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "vibe_show_page_id": "session-one",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "membership-1",
            "vibe_organization_role": "member",
        }
    )
    assert missing_target.can_read_instance is False
    assert broader_target.can_read_instance is True
    assert broader_target.can_chat is True
    assert broader_target.can_use_show_page("session-one") is True
    assert broader_target.can_use_show_page("session-two") is False

    elevated_role = context_from_session_payload(
        {
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "show_page_email",
            "vibe_show_page_id": "session-one",
        }
    )
    assert elevated_role.can_read_instance is False
    assert elevated_role.can_use_show_page("session-one") is False


def test_http_policy_keeps_unknown_routes_fail_closed_and_runtime_matrix_explicit() -> None:
    runtime_routes = (
        ("GET", "/api/harness/bootstrap"),
        ("PATCH", "/api/harness/tasks/task-1"),
        ("POST", "/api/agents"),
        ("POST", "/api/skills"),
        ("PATCH", "/api/vault/secrets/secret-1"),
        ("PATCH", "/api/memory/settings"),
        ("GET", "/api/memory/processing-record"),
        ("GET", "/api/memory/maintenance"),
        ("POST", "/api/memory/runtime/rebuild"),
        ("POST", "/api/memory/runtime/factory-reset"),
        ("POST", "/api/memory/runtime/repair"),
        ("POST", "/api/memory/clear/resume"),
        ("POST", "/api/memory/clear/abort"),
        ("POST", "/api/control"),
        ("POST", "/api/config"),
        ("GET", "/api/models/sources"),
        ("POST", "/api/models/runtime/start"),
        ("POST", "/api/backend/codex/restart"),
        ("POST", "/api/backend/claude/auth/oauth/start"),
        ("GET", "/api/dependencies"),
        ("POST", "/api/dependencies/avault/install"),
        ("PUT", "/api/projects/proj-1/agents-md"),
        ("POST", "/api/files/upload"),
        ("DELETE", "/api/terminal/term-1"),
        ("POST", "/api/show-pages/ses-1/icon"),
        ("POST", "/show/ses-1/api/action"),
        ("GET", "/api/users"),
        ("POST", "/api/users/user-1/admin"),
        ("DELETE", "/api/users/user-1"),
        ("GET", "/api/bind-codes"),
        ("POST", "/api/bind-codes"),
        ("DELETE", "/api/bind-codes/code-1"),
        ("GET", "/api/setup/first-bind-code"),
        ("GET", "/status"),
    )
    for method, path in runtime_routes:
        policy = http_authorization_policy(method, path)
        assert policy.remote_access == REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER, (method, path)

    for method, path in (
        ("GET", "/api/future-owner-capability"),
        ("POST", "/api/harness/future-capability"),
        ("GET", "/api/opencode/future-capability"),
        ("GET", "/api/backend/future/runtime"),
        ("GET", "/api/users/user-1"),
        ("POST", "/api/bind-codes/code-1"),
    ):
        policy = http_authorization_policy(method, path)
        assert policy.minimum_role == "owner"
        assert policy.remote_access == REMOTE_HTTP_LOCAL_ONLY

    for method, path in (
        ("POST", "/api/remote-access/vibe-cloud/pair"),
        ("POST", "/api/remote-access/start"),
        ("POST", "/api/remote-access/stop"),
        ("POST", "/api/remote-access/optimize-route"),
        ("POST", "/api/remote-access/settings"),
        ("POST", "/api/remote-access/diagnostics"),
        ("GET", "/api/remote-access/network-interfaces"),
    ):
        assert http_authorization_policy(method, path).remote_access == REMOTE_HTTP_LOCAL_ONLY


def test_http_policy_preserves_safe_remote_reads_and_show_routes() -> None:
    assert http_authorization_policy("GET", "/api/projects").remote_access == REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER
    assert http_authorization_policy("GET", "/api/org/context").remote_access == REMOTE_HTTP_ALLOWED
    assert http_authorization_policy("GET", "/api/cloud-management/session").remote_access == REMOTE_HTTP_ALLOWED
    assert http_authorization_policy("GET", "/show/ses-1/").remote_access == REMOTE_HTTP_ALLOWED
    assert http_authorization_policy("POST", "/show/ses-1/__show/events").remote_access == REMOTE_HTTP_ALLOWED


def test_temporary_runtime_signal_requires_membership_while_role_guard_uses_rank() -> None:
    member = AuthorizationContext(
        instance_role="viewer",
        subject="member-1",
        organization_id="org-1",
        organization_member_id="membership-1",
        organization_role="member",
        instance_access_source="organization_group",
        is_remote=True,
    )
    non_member = AuthorizationContext(
        instance_role="owner",
        subject="owner-1",
        instance_access_source="owner",
        is_remote=True,
    )
    assert has_temporary_unrestricted_org_access(member) is True
    assert has_temporary_unrestricted_org_access(non_member) is False
    assert require_instance_role(member, "owner") is member
    assert require_instance_role(non_member, "owner") is non_member


def test_temporary_runtime_route_matrix_is_not_a_capability_projection() -> None:
    member = AuthorizationContext(
        instance_role="viewer",
        organization_id="org-1",
        organization_member_id="membership-1",
        organization_role="member",
        instance_access_source="organization_group",
        is_remote=True,
    )
    assert member.capability_projection()["can_use_system"] is False
    assert member.capability_projection()["can_use_files"] is False
    assert member.capability_projection()["can_use_terminal"] is False


def test_workbench_event_policy_filters_privileged_and_unknown_events() -> None:
    viewer = _active_org_context("viewer")
    editor = _active_org_context("editor")
    owner = _active_org_context("owner")
    non_member = _non_member_context()
    local = trusted_local_context()

    assert can_receive_workbench_event(viewer, "authorization.changed") is True
    assert can_receive_workbench_event(viewer, "message.new") is True
    assert can_receive_workbench_event(viewer, "workbench.events.bridge.status") is True
    assert can_receive_workbench_event(viewer, "queue.updated") is True
    assert can_receive_workbench_event(editor, "queue.updated") is True
    assert can_receive_workbench_event(viewer, "vaults.updated") is True
    assert can_receive_workbench_event(editor, "definitions.updated") is True
    assert can_receive_workbench_event(owner, "vaults.updated") is True
    assert can_receive_workbench_event(non_member, "runs.updated") is False
    assert can_receive_workbench_event(non_member, "vaults.updated") is False
    assert can_receive_workbench_event(non_member, "definitions.updated") is False
    assert can_receive_workbench_event(local, "runs.updated") is True
    assert can_receive_workbench_event(viewer, "runs.updated") is True
    assert can_receive_workbench_event(editor, "runs.updated") is True
    assert can_receive_workbench_event(owner, "runs.updated") is True
    assert can_receive_workbench_event(viewer, "future.management.event") is False
    assert can_receive_workbench_event(owner, "future.management.event") is False


def test_service_role_guard_defaults_local_and_denies_remote_viewer() -> None:
    assert require_instance_role(None, "owner").is_trusted_local is True

    viewer = _non_member_context("viewer")
    try:
        require_instance_role(viewer, "editor")
    except InstanceAuthorizationError as error:
        assert error.code == "instance_access_forbidden"
    else:
        raise AssertionError("viewer unexpectedly passed the editor service guard")


def test_session_service_mutations_recheck_instance_role() -> None:
    from storage import workbench_sessions_service

    viewer = _non_member_context("viewer")
    with pytest.raises(InstanceAuthorizationError):
        workbench_sessions_service.create_session(
            None,
            scope_id=None,
            agent_backend="codex",
            authorization_context=viewer,
        )
    with pytest.raises(InstanceAuthorizationError):
        workbench_sessions_service.update_session(
            None,
            "ses-1",
            authorization_context=viewer,
        )
    with pytest.raises(InstanceAuthorizationError):
        workbench_sessions_service.archive_session(
            None,
            "ses-1",
            authorization_context=viewer,
        )


def test_project_service_mutations_require_owner() -> None:
    from storage import projects_service

    editor = _non_member_context("editor")
    with pytest.raises(InstanceAuthorizationError):
        projects_service.create_project(
            None,
            "/tmp",
            authorization_context=editor,
        )
    with pytest.raises(InstanceAuthorizationError):
        projects_service.update_project(
            None,
            "proj-1",
            authorization_context=editor,
        )
    with pytest.raises(InstanceAuthorizationError):
        projects_service.archive_project(
            None,
            "proj-1",
            authorization_context=editor,
        )


def test_session_fork_service_requires_editor() -> None:
    from core.services.session_fork import reserve_forked_session

    with pytest.raises(InstanceAuthorizationError):
        reserve_forked_session(
            source_session_id="ses-1",
            authorization_context=_non_member_context("viewer"),
        )
