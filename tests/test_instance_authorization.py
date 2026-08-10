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
    assert viewer.can_use_cloud_asr is False
    assert editor.can_read_instance is True
    assert editor.can_chat is False
    assert editor.can_use_cloud_asr is True
    assert editor.can_manage_projects is False
    assert owner.can_chat is False
    assert owner.can_use_cloud_asr is True
    assert owner.can_manage_projects is True
    assert owner.can_manage_agents is True
    assert owner.can_use_terminal is False
    assert owner.can_use_files is False
    assert owner.can_use_system is False
    assert trusted_local_context().can_manage_instance is True
    assert trusted_local_context().can_chat is True
    assert trusted_local_context().can_use_cloud_asr is False


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
    assert required_instance_role("GET", "/status") is None
    assert required_instance_role("GET", "/show/ses-1/") == "viewer"
    assert required_instance_role("POST", "/show/ses-1/api/action") == "editor"
    assert required_instance_role("GET", "/assets/app.js") is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/show-pages/ses-1/visibility"),
        ("GET", "/api/backend/codex/runtime"),
        ("GET", "/api/backend/claude/auth"),
        ("GET", "/api/backend/codex/auth"),
        ("GET", "/api/opencode/permission-status"),
        ("GET", "/api/harness/tasks"),
        ("GET", "/api/harness/watches"),
        ("GET", "/api/global-prompts"),
        ("GET", "/api/harness/runs"),
        ("GET", "/api/harness/runs/run-1"),
        ("GET", "/api/harness/bootstrap"),
        ("GET", "/api/vault/pubkey"),
        ("GET", "/api/vault/agent/pubkey"),
        ("GET", "/api/vault/sandbox/root-metadata"),
        ("GET", "/api/vault/vmk"),
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
        ("GET", "/api/memory/future-capability"),
        ("POST", "/api/memory/future-capability"),
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
        ("GET", "/api/workbench/prefs", REMOTE_HTTP_ALLOWED),
        ("PUT", "/api/workbench/prefs", REMOTE_HTTP_LOCAL_ONLY),
        ("PUT", "/api/resource-policies/agent/agent-1", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/models/runtime/status", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/models/agents/codex/chain", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/models/turns/turn-1/provenance", REMOTE_HTTP_ALLOWED),
        # Only the local owner can start an OAuth flow, and a status poll hands
        # back that flow's authorization URL and device code - plus, once a Model
        # Hub flow succeeds, the credential and account payload `/api/models/
        # sources` keeps local. Polling follows its flow and stays local too.
        ("GET", "/api/models/oauth/status/oaf_test0001", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/backend/codex/auth/oauth/status/flow-1", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/backend/codex/runtime", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/opencode/permission-status", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/vault/audit", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/dock", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/dock/pins", REMOTE_HTTP_LOCAL_ONLY),
        ("PUT", "/api/dock/order", REMOTE_HTTP_LOCAL_ONLY),
        # A push endpoint is caller-supplied and `send_web_push()` fetches it
        # from this host, so registering or testing one from across the tunnel
        # would let a remote caller aim an outbound request at loopback, a
        # private LAN host or a rebinding name. Status reads stay remote: they
        # report the caller's own subscription and reach no endpoint.
        ("GET", "/api/web-push/status", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/web-push/status", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/web-push/vapid-public-key", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/web-push/subscriptions", REMOTE_HTTP_LOCAL_ONLY),
        ("DELETE", "/api/web-push/subscriptions", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/web-push/test", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/harness/runs/run-1", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/users", REMOTE_HTTP_LOCAL_ONLY),
        ("HEAD", "/api/users", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/skills/check", REMOTE_HTTP_LOCAL_ONLY),
        ("HEAD", "/api/skills/find", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/vault/requests/access", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/vault/requests/sign", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/show-pages/ses-1/rotate-share", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/show-pages/ses-1/share-id", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/sessions/ses-1/draft", REMOTE_HTTP_LOCAL_ONLY),
        ("PUT", "/api/sessions/ses-1/draft", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/sessions/ses-1/mark-read", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/status", REMOTE_HTTP_LOCAL_ONLY),
        ("HEAD", "/status", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/show-pages/ses-1/ensure", REMOTE_HTTP_ALLOWED),
        ("GET", "/api/memory/settings", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/memory/failures", REMOTE_HTTP_LOCAL_ONLY),
        ("HEAD", "/api/memory/log/entry", REMOTE_HTTP_LOCAL_ONLY),
        ("PATCH", "/api/memory/settings", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/memory/search", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/memory/runtime/restart", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/memory/clear", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/projects/proj-1/agents-md", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/show/ses-1/", REMOTE_HTTP_ALLOWED),
        ("POST", "/show/ses-1/__show/events", REMOTE_HTTP_ALLOWED),
        ("POST", "/api/config", REMOTE_HTTP_PAYLOAD_FILTERED),
        ("PATCH", "/api/projects/proj-1", REMOTE_HTTP_PAYLOAD_FILTERED),
        ("PATCH", "/api/sessions/ses-1", REMOTE_HTTP_PAYLOAD_FILTERED),
        ("GET", "/api/settings", REMOTE_HTTP_LOCAL_ONLY),
        ("HEAD", "/api/settings", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/settings", REMOTE_HTTP_LOCAL_ONLY),
        ("POST", "/api/settings/thread", REMOTE_HTTP_LOCAL_ONLY),
        ("DELETE", "/api/settings/thread", REMOTE_HTTP_LOCAL_ONLY),
        ("GET", "/api/bind-codes", REMOTE_HTTP_LOCAL_ONLY),
        ("DELETE", "/api/projects/proj-1", REMOTE_HTTP_LOCAL_ONLY),
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


@pytest.mark.parametrize(
    ("gate", "method", "path", "expected"),
    [
        # canManageSkills: read-only catalog remotely, every mutation local.
        ("canManageSkills", "GET", "/api/skills", REMOTE_HTTP_ALLOWED),
        ("canManageSkills", "POST", "/api/skills", REMOTE_HTTP_LOCAL_ONLY),
        ("canManageSkills", "POST", "/api/skills/askill/update", REMOTE_HTTP_LOCAL_ONLY),
        ("canManageSkills", "DELETE", "/api/skills/askill", REMOTE_HTTP_LOCAL_ONLY),
        (
            "canManageSkills",
            "POST",
            "/api/dependencies/askill/install",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        # canManageVaultSecrets: inventory and audit reads stay remote, mutations
        # and key material do not.
        ("canManageVaultSecrets", "GET", "/api/vault/secrets", REMOTE_HTTP_ALLOWED),
        ("canManageVaultSecrets", "GET", "/api/vault/settings", REMOTE_HTTP_ALLOWED),
        ("canManageVaultSecrets", "GET", "/api/vault/grants", REMOTE_HTTP_ALLOWED),
        ("canManageVaultSecrets", "POST", "/api/vault/secrets", REMOTE_HTTP_LOCAL_ONLY),
        (
            "canManageVaultSecrets",
            "PATCH",
            "/api/vault/secrets/prod",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        (
            "canManageVaultSecrets",
            "DELETE",
            "/api/vault/secrets/prod",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        ("canManageVaultSecrets", "POST", "/api/vault/vmk", REMOTE_HTTP_LOCAL_ONLY),
        # canUseHarness: the page cannot even bootstrap remotely.
        ("canUseHarness", "GET", "/api/harness/bootstrap", REMOTE_HTTP_LOCAL_ONLY),
        # canArchiveProjects: list and rename stay remote, archive does not.
        ("canArchiveProjects", "GET", "/api/projects", REMOTE_HTTP_ALLOWED),
        (
            "canArchiveProjects",
            "PATCH",
            "/api/projects/proj-1",
            REMOTE_HTTP_PAYLOAD_FILTERED,
        ),
        ("canArchiveProjects", "DELETE", "/api/projects/proj-1", REMOTE_HTTP_LOCAL_ONLY),
        # canEditProjectInstructions: local in both directions, because an
        # AGENTS.md / CLAUDE.md symlink can resolve outside the Project.
        (
            "canEditProjectInstructions",
            "GET",
            "/api/projects/proj-1/agents-md",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        (
            "canEditProjectInstructions",
            "PUT",
            "/api/projects/proj-1/agents-md",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        # canEditAgentDefinitions: catalog reads remotely, definition writes locally.
        ("canEditAgentDefinitions", "GET", "/api/agents", REMOTE_HTTP_ALLOWED),
        ("canEditAgentDefinitions", "POST", "/api/agents", REMOTE_HTTP_LOCAL_ONLY),
        # canAdministerMemory: principal-scoped reads and search stay remote,
        # the cross-principal admin log and the sidecar administration do not.
        ("canAdministerMemory", "GET", "/api/memory/status", REMOTE_HTTP_ALLOWED),
        ("canAdministerMemory", "GET", "/api/memory/profile", REMOTE_HTTP_ALLOWED),
        ("canAdministerMemory", "POST", "/api/memory/search", REMOTE_HTTP_ALLOWED),
        ("canAdministerMemory", "GET", "/api/memory/log", REMOTE_HTTP_LOCAL_ONLY),
        ("canAdministerMemory", "GET", "/api/memory/log/entry", REMOTE_HTTP_LOCAL_ONLY),
        ("canAdministerMemory", "PATCH", "/api/memory/settings", REMOTE_HTTP_LOCAL_ONLY),
        (
            "canAdministerMemory",
            "POST",
            "/api/memory/runtime/restart",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        ("canAdministerMemory", "POST", "/api/memory/clear", REMOTE_HTTP_LOCAL_ONLY),
        # Dock/Workbench prefs: the read is remote, the process-global writes
        # are local until the records are keyed by the authenticated subject.
        ("dockAndWorkbenchPrefs", "GET", "/api/dock", REMOTE_HTTP_ALLOWED),
        ("dockAndWorkbenchPrefs", "GET", "/api/workbench/prefs", REMOTE_HTTP_ALLOWED),
        ("dockAndWorkbenchPrefs", "POST", "/api/dock/pins", REMOTE_HTTP_LOCAL_ONLY),
        (
            "dockAndWorkbenchPrefs",
            "DELETE",
            "/api/dock/pins/ses-1",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        ("dockAndWorkbenchPrefs", "PUT", "/api/dock/order", REMOTE_HTTP_LOCAL_ONLY),
        ("dockAndWorkbenchPrefs", "PUT", "/api/workbench/prefs", REMOTE_HTTP_LOCAL_ONLY),
        # canRegisterWebPush: status reads stay remote, but registering or
        # testing an endpoint this host will call does not.
        ("canRegisterWebPush", "GET", "/api/web-push/status", REMOTE_HTTP_ALLOWED),
        ("canRegisterWebPush", "POST", "/api/web-push/status", REMOTE_HTTP_ALLOWED),
        (
            "canRegisterWebPush",
            "POST",
            "/api/web-push/subscriptions",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        (
            "canRegisterWebPush",
            "DELETE",
            "/api/web-push/subscriptions",
            REMOTE_HTTP_LOCAL_ONLY,
        ),
        ("canRegisterWebPush", "POST", "/api/web-push/test", REMOTE_HTTP_LOCAL_ONLY),
    ],
)
def test_local_only_workbench_gates_match_the_remote_http_policy(
    gate,
    method,
    path,
    expected,
) -> None:
    """Pin the endpoint classifications the Workbench UI gates are derived from.

    `can_manage_instance` / `can_manage_agents` / `can_manage_projects` stay true
    for a remote Instance owner, so `ui/src/lib/remoteAuth.ts` adds a locality
    check for every control whose endpoint is local-only. If one of these routes
    is reclassified, that UI gate is stale and must be revisited with it.
    """
    assert http_authorization_policy(method, path).remote_access == expected, gate


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
