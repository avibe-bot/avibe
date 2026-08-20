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


# Frozen master-era capability bits for editor/viewer. Seeded so a later
# role or capability cannot silently rewrite those ranks; member is asserted
# independently below rather than by enumerating every new key.
_PRE_MEMBER_EDITOR_VIEWER_CAPABILITIES = {
    "viewer": {
        "is_instance_owner": False,
        "can_read_instance": True,
        "can_chat": False,
        "can_manage_projects": False,
        "can_manage_agents": False,
        "can_manage_instance": False,
        "can_use_agents": False,
        "can_use_skills": False,
        "can_use_vault_secrets": False,
        "can_use_show_pages": True,
        "can_use_terminal_files": False,
        "can_use_terminal": False,
        "can_use_files": False,
        "can_use_system": False,
    },
    "editor": {
        "is_instance_owner": False,
        "can_read_instance": True,
        "can_chat": True,
        "can_manage_projects": False,
        "can_manage_agents": False,
        "can_manage_instance": False,
        "can_use_agents": True,
        "can_use_skills": True,
        "can_use_vault_secrets": True,
        "can_use_show_pages": True,
        "can_use_terminal_files": True,
        "can_use_terminal": True,
        "can_use_files": True,
        "can_use_system": False,
    },
}


def test_capabilities_depend_on_instance_role_not_origin_or_membership() -> None:
    for role in ("viewer", "editor", "member", "owner"):
        local = _context(role, remote=False)
        remote = _context(role, remote=True, source="organization_group")
        assert local.capability_projection() == remote.capability_projection()

    viewer = _context("viewer", remote=True, source="organization_group")
    editor = _context("editor", remote=True, source="organization_group")
    member = _context("member", remote=True, source="organization_group")
    owner = _context("owner", remote=True, source="organization_group")
    assert viewer.can_read_instance and not viewer.can_chat
    assert not viewer.can_use_resource("agent")
    assert editor.can_chat and editor.can_use_resource("agent")
    assert editor.can_use_files and editor.can_use_terminal
    assert not editor.can_manage_instance
    assert member.can_manage_instance and member.can_use_system
    assert member.has_role("editor") and member.can_use_resource("agent")
    assert not member.can_manage_access_members
    assert not member.is_instance_owner
    assert owner.can_manage_instance and owner.can_use_system
    assert owner.can_manage_access_members and owner.is_instance_owner


def test_editor_and_viewer_capabilities_match_pre_member_master() -> None:
    """Editor/viewer bits stay bitwise-equal to master; new keys may appear."""

    for role, expected in _PRE_MEMBER_EDITOR_VIEWER_CAPABILITIES.items():
        projection = _context(role, remote=True).capability_projection()
        for key, value in expected.items():
            assert projection[key] is value, (role, key)
        assert projection["can_manage_access_members"] is False


def test_member_is_owner_minus_member_management() -> None:
    member = _context("member", remote=True).capability_projection()
    owner = _context("owner", remote=True).capability_projection()
    assert set(member) == set(owner)
    for key, value in owner.items():
        if key in {"is_instance_owner", "can_manage_access_members"}:
            assert member[key] is False
        else:
            assert member[key] is value


def test_unknown_instance_role_fails_closed() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "vibe_instance_role": "admin",
            "vibe_instance_access_source": "email",
        }
    )
    assert context.instance_role is None
    projection = context.capability_projection()
    assert all(value is False for value in projection.values())


def test_pre_member_payload_without_member_role_still_loads() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
        }
    )
    assert context.instance_role == "editor"
    assert context.can_chat
    assert not context.can_manage_instance


def test_member_session_payload_is_accepted() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "vibe_instance_role": "member",
            "vibe_instance_access_source": "email",
        }
    )
    assert context.instance_role == "member"
    assert context.can_manage_instance
    assert not context.can_manage_access_members


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
            "vibe_instance_kind": "organization",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "membership-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-a"],
        }
    )
    assert context.instance_role == "editor"
    assert context.group_ids == frozenset({"group-a"})
    assert context.is_organization_instance
    assert context.can_chat
    assert not context.can_manage_instance


def test_context_from_session_payload_only_recognizes_known_instance_kinds() -> None:
    base = {
        "sub": "user-1",
        "vibe_instance_role": "editor",
        "vibe_instance_access_source": "owner",
    }
    personal = context_from_session_payload({**base, "vibe_instance_kind": "personal"})
    unknown = context_from_session_payload({**base, "vibe_instance_kind": "workspace"})

    assert personal.is_personal_instance
    assert not personal.is_organization_instance
    assert personal.instance_kind == "personal"
    assert unknown.instance_kind is None
    assert not unknown.is_personal_instance


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
        assert http_authorization_policy(method, path).minimum_role == "member"

    assert (
        http_authorization_policy("PUT", "/api/permissions/authorized-users").minimum_role
        == "owner"
    )
    assert http_authorization_policy("GET", "/show/ses-1/").minimum_role == "viewer"


def test_advertised_capability_namespaces_cover_current_and_future_routes() -> None:
    """Every advertised Editor/Viewer surface is a namespace, not a case list.

    A newly added Skills, Vault, Harness, Files, Dock, Terminal, or Web Push
    route must inherit the same Instance role as the rest of that capability.
    Agent create/import and unknown APIs fail closed to member; allowlist
    mutation stays owner-only.
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
        ("POST", "/api/show-pages/session-1/ensure"),
        ("POST", "/api/show-pages/session-1/availability"),
        ("POST", "/api/show-pages/session-1/access-settings/read"),
        ("POST", "/api/show-pages/session-1/access-settings/apply"),
        # The Web UI's ShowPageShareControl reads the page it can mutate;
        # editors must keep GET access (regression: PR #1606 round 1).
        ("GET", "/api/show-pages/session-1"),
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

    member_examples = (
        ("POST", "/api/agents"),
        ("POST", "/api/agents/import"),
        ("PATCH", "/api/agents/demo"),
        ("DELETE", "/api/agents/demo"),
        ("GET", "/api/future-owner-capability"),
        ("POST", "/api/browse"),
        ("POST", "/api/browse/mkdir"),
        ("PUT", "/api/permissions/projects/project-1/access"),
        ("PUT", "/api/permissions/resources/show_page/page-1/access"),
    )
    for method, path in member_examples:
        assert http_authorization_policy(method, path).minimum_role == "member", path

    assert (
        http_authorization_policy("PUT", "/api/permissions/authorized-users").minimum_role
        == "owner"
    )


def test_workbench_events_follow_role_boundaries() -> None:
    viewer = _context("viewer", remote=True)
    editor = _context("editor", remote=True)
    member = _context("member", remote=True)
    owner = _context("owner", remote=True)
    assert can_receive_workbench_event(viewer, "message.new")
    assert not can_receive_workbench_event(viewer, "queue.updated")
    assert can_receive_workbench_event(editor, "queue.updated")
    assert not can_receive_workbench_event(editor, "runs.updated")
    assert can_receive_workbench_event(member, "runs.updated")
    assert can_receive_workbench_event(member, "definitions.updated")
    assert can_receive_workbench_event(member, "vaults.updated")
    assert not can_receive_workbench_event(member, "future.management.event")
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


def test_advertised_manage_capabilities_are_honored_by_service_guards() -> None:
    """Project CRUD and Agent onboarding follow can_manage_*, not owner identity.

    Seed every existing instance role so a leftover require_instance_role(...,
    "owner") or is_instance_owner check cannot silently deny member while the
    capability projection still advertises the surface.
    """

    from core.vibe_agents import VibeAgentAccessError, _require_agent_onboarding_access

    for role in ("viewer", "editor", "member", "owner"):
        context = _context(role, remote=True)
        if context.can_manage_projects:
            assert require_instance_role(context, "member") is context
        else:
            with pytest.raises(InstanceAuthorizationError):
                require_instance_role(context, "member")
        if context.can_manage_agents:
            assert _require_agent_onboarding_access(context) is context
        else:
            with pytest.raises(VibeAgentAccessError):
                _require_agent_onboarding_access(context)
