import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    can_receive_workbench_event,
    context_from_session_payload,
    http_authorization_policy,
    instance_owner_context,
    require_instance_role,
)


def _context(role: str, *, remote: bool, source: str = "owner") -> AuthorizationContext:
    return AuthorizationContext(
        instance_role=role,
        subject=f"{role}-subject",
        instance_access_source=source,
        is_remote=remote,
    )


def test_capabilities_depend_on_instance_role_not_origin_or_membership() -> None:
    for role in ("viewer", "editor", "owner"):
        local = _context(role, remote=False)
        remote = _context(role, remote=True, source="organization_group")
        assert local.capability_projection() == remote.capability_projection()

    viewer = _context("viewer", remote=True, source="organization_group")
    editor = _context("editor", remote=True, source="organization_group")
    owner = _context("owner", remote=True, source="organization_group")
    assert viewer.can_read_instance and not viewer.can_chat
    assert not viewer.can_use_resource("agent")
    assert editor.can_chat and editor.can_use_resource("agent")
    assert editor.can_use_files and editor.can_use_terminal
    assert not editor.can_manage_instance
    assert owner.can_manage_instance and owner.can_use_system


def test_organization_membership_does_not_elevate_role() -> None:
    member = AuthorizationContext(
        instance_role="viewer",
        subject="member-1",
        instance_access_source="organization_group",
        organization_id="org-1",
        organization_member_id="membership-1",
        organization_role="admin",
        is_remote=True,
    )
    assert member.is_active_organization_member
    assert member.capability_projection()["can_chat"] is False
    assert member.capability_projection()["can_manage_instance"] is False


def test_context_from_session_payload_keeps_role_and_acl_claims() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "membership-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-a"],
        }
    )
    assert context.instance_role == "editor"
    assert context.group_ids == frozenset({"group-a"})
    assert context.can_chat
    assert not context.can_manage_instance


def test_show_page_email_context_is_exactly_page_scoped() -> None:
    context = context_from_session_payload(
        {
            "sub": "guest-1",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "show_page_email",
            "vibe_show_page_id": "session-one",
        }
    )
    assert context.can_use_show_page("session-one")
    assert not context.can_use_show_page("session-two")
    assert not context.can_chat


def test_http_policy_is_role_only_and_unknown_api_routes_fail_closed() -> None:
    editor_routes = (
        ("GET", "/api/agents"),
        ("GET", "/api/files/list"),
        ("PUT", "/api/files/write"),
        ("GET", "/api/dock"),
        ("PUT", "/api/dock/order"),
        ("GET", "/api/skills"),
        ("GET", "/api/vault/secrets"),
        ("GET", "/api/harness/bootstrap"),
        ("PATCH", "/api/harness/tasks/task-1"),
        ("DELETE", "/api/harness/watches/watch-1"),
        ("DELETE", "/api/terminal/term-1"),
    )
    for method, path in editor_routes:
        assert http_authorization_policy(method, path).minimum_role == "editor"

    for method, path in (("GET", "/api/future-owner-capability"), ("POST", "/api/control")):
        assert http_authorization_policy(method, path).minimum_role == "owner"

    assert http_authorization_policy("GET", "/api/org/context").minimum_role == "viewer"
    assert http_authorization_policy("GET", "/show/ses-1/").minimum_role == "viewer"


def test_workbench_events_follow_role_boundaries() -> None:
    viewer = _context("viewer", remote=True)
    editor = _context("editor", remote=True)
    owner = _context("owner", remote=True)
    assert can_receive_workbench_event(viewer, "message.new")
    assert not can_receive_workbench_event(viewer, "queue.updated")
    assert can_receive_workbench_event(editor, "queue.updated")
    assert not can_receive_workbench_event(editor, "runs.updated")
    assert can_receive_workbench_event(owner, "runs.updated")
    assert not can_receive_workbench_event(viewer, "future.management.event")


def test_service_role_guard_defaults_to_instance_owner() -> None:
    local = require_instance_role(None, "owner")
    assert local.instance_role == "owner"
    assert local.is_instance_owner
    with pytest.raises(InstanceAuthorizationError):
        require_instance_role(_context("viewer", remote=True), "editor")


def test_session_service_mutations_recheck_instance_role() -> None:
    from storage import workbench_sessions_service

    viewer = _context("viewer", remote=True)
    with pytest.raises(InstanceAuthorizationError):
        workbench_sessions_service.create_session(
            None,
            scope_id=None,
            agent_backend="codex",
            authorization_context=viewer,
        )
