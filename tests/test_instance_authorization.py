import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    _EDITOR_HTTP_NAMESPACES,
    _VIEWER_HTTP_NAMESPACES,
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
    assert not context.can_read_instance
    assert context.capability_projection()["can_read_instance"] is False
    assert context.capability_projection()["can_use_show_pages"] is False
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
        ("GET", "/api/browse/favorites"),
    )
    for method, path in editor_routes:
        assert http_authorization_policy(method, path).minimum_role == "editor"

    for method, path in (("GET", "/api/future-owner-capability"), ("POST", "/api/control")):
        assert http_authorization_policy(method, path).minimum_role == "owner"

    assert http_authorization_policy("GET", "/show/ses-1/").minimum_role == "viewer"


def test_advertised_capability_namespaces_cover_current_and_future_routes() -> None:
    """Every advertised Editor/Viewer surface is a namespace, not a case list.

    A newly added Skills, Vault, Harness, Files, Dock, Terminal, or Web Push
    route must inherit the same Instance role as the rest of that capability.
    Owner-only Agent create/import and unknown APIs stay fail-closed.
    """

    assert _EDITOR_HTTP_NAMESPACES == (
        "/api/skills",
        "/api/vault",
        "/api/files",
        "/api/dock",
        "/api/harness",
        "/api/terminal",
    )
    assert _VIEWER_HTTP_NAMESPACES == (
        "/api/memory",
        "/api/web-push",
    )

    editor_examples = (
        ("POST", "/api/skills"),
        ("POST", "/api/skills/update"),
        ("POST", "/api/skills/preview"),
        ("POST", "/api/skills/upload"),
        ("DELETE", "/api/skills/demo"),
        ("POST", "/api/skills/future-mutation"),
        ("POST", "/api/vault/secrets"),
        ("PATCH", "/api/vault/secrets/OPENAI_API_KEY"),
        ("DELETE", "/api/vault/secrets/OPENAI_API_KEY"),
        ("POST", "/api/vault/requests/access"),
        ("POST", "/api/vault/grants"),
        ("DELETE", "/api/vault/grants/grant-1"),
        ("PATCH", "/api/vault/settings"),
        ("POST", "/api/vault/future-mutation"),
        ("POST", "/api/harness/tasks"),
        ("PATCH", "/api/harness/watches/watch-1"),
        ("GET", "/api/files/list"),
        ("PUT", "/api/files/write"),
        ("POST", "/api/dock/pins"),
        ("GET", "/api/browse/favorites"),
        ("GET", "/api/asr/status"),
        ("POST", "/api/asr/transcribe"),
        ("POST", "/api/asr/telemetry"),
        ("POST", "/api/config"),
        ("POST", "/api/sessions/session-1/messages"),
        ("POST", "/api/sessions/session-1/attachments"),
        ("POST", "/api/sessions/session-1/cancel"),
        ("GET", "/api/show-pages/session-1"),
        ("POST", "/api/show-pages/session-1/ensure"),
        ("POST", "/api/show-pages/session-1/availability"),
        ("POST", "/api/show-pages/session-1/access-settings/read"),
        ("POST", "/api/show-pages/session-1/access-settings/apply"),
    )
    for method, path in editor_examples:
        assert http_authorization_policy(method, path).minimum_role == "editor", path

    viewer_examples = (
        ("GET", "/api/memory/settings"),
        ("PATCH", "/api/memory/settings"),
        ("POST", "/api/memory/runtime/restart"),
        ("POST", "/api/memory/future-capability"),
        ("POST", "/api/sessions/session-1/mark-read"),
        ("DELETE", "/api/terminal/term-1"),
        ("GET", "/api/web-push/status"),
        ("POST", "/api/web-push/status"),
        ("GET", "/api/web-push/vapid-public-key"),
        ("POST", "/api/web-push/subscriptions"),
        ("DELETE", "/api/web-push/subscriptions"),
        ("POST", "/api/web-push/test"),
        ("POST", "/api/web-push/future-mutation"),
    )
    for method, path in viewer_examples:
        assert http_authorization_policy(method, path).minimum_role == "viewer", path

    owner_examples = (
        ("POST", "/api/agents"),
        ("POST", "/api/agents/import"),
        ("PATCH", "/api/agents/demo"),
        ("DELETE", "/api/agents/demo"),
        ("GET", "/api/future-owner-capability"),
        ("POST", "/api/browse"),
        ("POST", "/api/browse/mkdir"),
    )
    for method, path in owner_examples:
        assert http_authorization_policy(method, path).minimum_role == "owner", path


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
