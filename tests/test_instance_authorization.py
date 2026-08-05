import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    REMOTE_HTTP_ALLOWED,
    REMOTE_HTTP_LOCAL_ONLY,
    REMOTE_HTTP_PAYLOAD_FILTERED,
    can_receive_workbench_event,
    context_from_session_payload,
    http_authorization_policy,
    require_instance_role,
    required_instance_role,
    trusted_local_context,
)


def _remote_context(role: str) -> AuthorizationContext:
    return AuthorizationContext(instance_role=role, is_remote=True)


def test_remote_roles_keep_management_monotonic_but_execution_disabled() -> None:
    viewer = AuthorizationContext(instance_role="viewer", is_remote=True)
    editor = AuthorizationContext(instance_role="editor", is_remote=True)
    owner = AuthorizationContext(instance_role="owner", is_remote=True)

    assert viewer.can_read_instance is True
    assert viewer.can_chat is False
    assert editor.can_read_instance is True
    assert editor.can_chat is False
    assert editor.can_manage_projects is False
    assert owner.can_chat is False
    assert owner.can_manage_projects is True
    assert owner.can_manage_agents is True
    assert owner.can_use_terminal is False
    assert owner.can_use_files is False
    assert owner.can_use_system is False
    assert trusted_local_context().can_manage_instance is True
    assert trusted_local_context().can_chat is True


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            "viewer",
            {"agent": False, "skill": False, "vault_secret": False, "show_page": True},
        ),
        (
            "editor",
            {"agent": True, "skill": True, "vault_secret": True, "show_page": True},
        ),
        (
            "owner",
            {"agent": True, "skill": True, "vault_secret": True, "show_page": True},
        ),
    ],
)
def test_resource_use_capability_is_distinct_from_owner_management(role, expected) -> None:
    context = _remote_context(role)

    assert {kind: context.can_use_resource(kind) for kind in expected} == expected
    assert context.can_manage_instance is (role == "owner")
    assert context.can_use_resource("future_resource") is False


def test_context_uses_role_not_diagnostic_source_for_owner() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "email": "editor@example.com",
            "vibe_instance_id": "inst-1",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "owner",
            "vibe_instance_authorization_revision": 0,
        }
    )

    assert context.is_instance_owner is False
    assert context.can_chat is False
    assert context.can_manage_instance is False
    assert context.authorization_revision == 0


def test_malformed_role_context_fails_closed() -> None:
    context = context_from_session_payload(
        {"vibe_instance_role": "admin", "vibe_instance_access_source": "owner"}
    )

    assert context.is_remote is True
    assert context.can_read_instance is False


def test_http_policy_defaults_unknown_api_to_owner() -> None:
    assert required_instance_role("GET", "/api/projects") == "viewer"
    assert required_instance_role("GET", "/api/projects/proj-1") == "viewer"
    assert required_instance_role("GET", "/api/projects/proj-1/agents-md") == "owner"
    assert required_instance_role("GET", "/api/agents") == "editor"
    assert required_instance_role("GET", "/api/skills") == "editor"
    assert required_instance_role("GET", "/api/vault/secrets") == "editor"
    assert required_instance_role("GET", "/api/vault/tags") == "editor"
    assert required_instance_role("POST", "/api/vault/requests/access") == "editor"
    assert required_instance_role("POST", "/api/vault/requests/sign") == "editor"
    assert required_instance_role("POST", "/api/vault/secrets") == "owner"
    assert required_instance_role("POST", "/api/skills") == "owner"
    assert required_instance_role("GET", "/api/show-pages") == "viewer"
    assert required_instance_role("GET", "/api/config") == "viewer"
    assert required_instance_role("GET", "/api/workbench/prefs") == "viewer"
    assert required_instance_role("PUT", "/api/workbench/prefs") == "owner"
    assert required_instance_role("GET", "/api/org/context") == "viewer"
    assert required_instance_role("GET", "/api/org/groups") == "viewer"
    assert required_instance_role("GET", "/api/resource-policies") == "viewer"
    assert required_instance_role("PUT", "/api/resource-policies/agent/agent-1") == "owner"
    assert required_instance_role("GET", "/api/sessions/ses-1/draft") == "editor"
    assert required_instance_role("POST", "/api/sessions/ses-1/messages") == "editor"
    assert required_instance_role("POST", "/api/sessions/ses-1/fork") == "editor"
    assert required_instance_role("POST", "/api/projects") == "owner"
    assert required_instance_role("GET", "/api/new-management-surface") == "owner"
    assert required_instance_role("GET", "/show/ses-1/") == "viewer"
    assert required_instance_role("POST", "/show/ses-1/api/action") == "editor"
    assert required_instance_role("GET", "/assets/app.js") is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/doctor"),
        ("POST", "/api/doctor"),
        ("POST", "/api/logs"),
        ("POST", "/api/ui/reload"),
        ("POST", "/api/opencode/options"),
        ("POST", "/api/opencode/setup-permission"),
        ("POST", "/api/vault/secrets"),
        ("PATCH", "/api/vault/secrets/secret-1"),
        ("DELETE", "/api/vault/grants/grant-1"),
        ("POST", "/api/vault/grants"),
        ("POST", "/api/vault/sign"),
        ("POST", "/api/skills/preview"),
        ("POST", "/api/users"),
        ("POST", "/api/users/user-1/admin"),
        ("DELETE", "/api/users/user-1"),
        ("POST", "/api/bind-codes"),
        ("DELETE", "/api/bind-codes/code-1"),
        ("GET", "/api/vault/future-capability"),
        ("GET", "/api/dock/future-capability"),
        ("GET", "/api/web-push/future-capability"),
        ("GET", "/api/models/future-capability"),
        ("GET", "/api/harness/future-capability"),
        ("GET", "/api/users/future-capability"),
        ("GET", "/api/bind-codes/future-capability"),
        ("GET", "/api/future-owner-capability"),
        ("POST", "/api/future-owner-capability"),
    ],
)
def test_remote_http_policy_defaults_local_machine_and_unknown_routes_to_local_only(
    method,
    path,
) -> None:
    policy = http_authorization_policy(method, path)

    assert policy.minimum_role == "owner"
    assert policy.remote_access == REMOTE_HTTP_LOCAL_ONLY


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/api/projects", REMOTE_HTTP_ALLOWED),
        ("PUT", "/api/workbench/prefs", REMOTE_HTTP_ALLOWED),
        ("PUT", "/api/resource-policies/agent/agent-1", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/models/runtime/status", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/models/agents/codex/chain", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/backend/codex/runtime", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/opencode/permission-status", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/vault/audit", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/dock/pins", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/web-push/subscriptions", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/harness/runs/run-1", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/users", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/bind-codes", REMOTE_HTTP_ALLOWED),
        ("GET", "/show/ses-1/", REMOTE_HTTP_ALLOWED),
        ("POST", "/show/ses-1/__show/events", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/config", REMOTE_HTTP_PAYLOAD_FILTERED),
        ("PATCH", "/api/projects/proj-1", REMOTE_HTTP_PAYLOAD_FILTERED),
        ("PATCH", "/api/sessions/ses-1", REMOTE_HTTP_PAYLOAD_FILTERED),
        ("POST", "/api/settings", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/settings/thread", REMOTE_HTTP_LOCAL_ONLY),
        ("DELETE", "/api/settings/thread", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/sessions/ses-1/fork", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/show/ses-1/api/action", REMOTE_HTTP_LOCAL_ONLY),
    ],
)
def test_remote_http_policy_keeps_approved_management_and_read_surfaces(
    method,
    path,
    expected,
) -> None:
    assert http_authorization_policy(method, path).remote_access == expected


def test_workbench_event_policy_filters_privileged_and_unknown_events() -> None:
    viewer = _remote_context("viewer")
    editor = _remote_context("editor")
    owner = _remote_context("owner")

    assert can_receive_workbench_event(viewer, "authorization.changed") is True
    assert can_receive_workbench_event(viewer, "message.new") is True
    assert can_receive_workbench_event(viewer, "workbench.events.bridge.status") is True
    assert can_receive_workbench_event(viewer, "queue.updated") is False
    assert can_receive_workbench_event(editor, "queue.updated") is True
    assert can_receive_workbench_event(editor, "vaults.updated") is False
    assert can_receive_workbench_event(owner, "vaults.updated") is True
    assert can_receive_workbench_event(viewer, "future.management.event") is False
    assert can_receive_workbench_event(owner, "future.management.event") is True


def test_service_role_guard_defaults_local_and_denies_remote_viewer() -> None:
    assert require_instance_role(None, "owner").is_trusted_local is True

    viewer = AuthorizationContext(instance_role="viewer", is_remote=True)
    try:
        require_instance_role(viewer, "editor")
    except InstanceAuthorizationError as error:
        assert error.code == "instance_access_forbidden"
    else:
        raise AssertionError("viewer unexpectedly passed the editor service guard")


def test_session_service_mutations_recheck_instance_role() -> None:
    from storage import workbench_sessions_service

    viewer = _remote_context("viewer")
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

    editor = _remote_context("editor")
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
            authorization_context=_remote_context("viewer"),
        )
