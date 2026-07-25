import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    can_receive_workbench_event,
    context_from_session_payload,
    require_instance_role,
    required_instance_role,
    trusted_local_context,
)


def _remote_context(role: str) -> AuthorizationContext:
    return AuthorizationContext(instance_role=role, is_remote=True)


def test_role_capabilities_are_monotonic() -> None:
    viewer = AuthorizationContext(instance_role="viewer", is_remote=True)
    editor = AuthorizationContext(instance_role="editor", is_remote=True)
    owner = AuthorizationContext(instance_role="owner", is_remote=True)

    assert viewer.can_read_instance is True
    assert viewer.can_chat is False
    assert editor.can_read_instance is True
    assert editor.can_chat is True
    assert editor.can_manage_projects is False
    assert owner.can_manage_projects is True
    assert owner.can_manage_agents is True
    assert trusted_local_context().can_manage_instance is True


def test_context_uses_role_not_diagnostic_source_for_owner() -> None:
    context = context_from_session_payload(
        {
            "sub": "user-1",
            "email": "editor@example.com",
            "vibe_instance_id": "inst-1",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "owner",
        }
    )

    assert context.is_instance_owner is False
    assert context.can_chat is True
    assert context.can_manage_instance is False


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
    assert required_instance_role("GET", "/api/config") == "viewer"
    assert required_instance_role("GET", "/api/workbench/prefs") == "viewer"
    assert required_instance_role("PUT", "/api/workbench/prefs") == "owner"
    assert required_instance_role("GET", "/api/sessions/ses-1/draft") == "editor"
    assert required_instance_role("POST", "/api/sessions/ses-1/messages") == "editor"
    assert required_instance_role("POST", "/api/projects") == "owner"
    assert required_instance_role("GET", "/api/new-management-surface") == "owner"
    assert required_instance_role("GET", "/show/ses-1/") == "viewer"
    assert required_instance_role("POST", "/show/ses-1/api/action") == "editor"
    assert required_instance_role("GET", "/assets/app.js") is None


def test_workbench_event_policy_filters_privileged_and_unknown_events() -> None:
    viewer = _remote_context("viewer")
    editor = _remote_context("editor")
    owner = _remote_context("owner")

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
