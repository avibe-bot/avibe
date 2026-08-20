import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    _EDITOR_HTTP_NAMESPACES,
    _REMOTE_ACCESS_HTTP_NAMESPACE,
    _REMOTE_ACCESS_MEMBER_HTTP_RULES,
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
    assert http_authorization_policy("POST", "/api/remote-access/vibe-cloud/pair").minimum_role == "owner"
    assert http_authorization_policy("POST", "/api/remote-access/start").minimum_role == "owner"
    assert http_authorization_policy("POST", "/api/remote-access/stop").minimum_role == "owner"
    assert http_authorization_policy("POST", "/api/remote-access/settings").minimum_role == "owner"
    assert http_authorization_policy("POST", "/api/remote-access/future-unpair").minimum_role == "owner"
    assert http_authorization_policy("GET", "/api/remote-access/status").minimum_role == "member"


def test_advertised_capability_namespaces_cover_current_and_future_routes() -> None:
    """Every advertised Editor/Viewer surface is a namespace, not a case list.

    A newly added Skills, Vault, Harness, Files, Dock, Terminal, or Web Push
    route must inherit the same Instance role as the rest of that capability.
    Agent create/import and unknown APIs fail closed to member; allowlist
    mutation, pairing-identity writes, and instance-wide default-agent
    routing stay owner-only.
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
        ("GET", "/api/remote-access/status"),
        ("GET", "/api/remote-access/network-interfaces"),
        ("POST", "/api/remote-access/optimize-route"),
        ("POST", "/api/remote-access/diagnostics"),
    )
    for method, path in member_examples:
        assert http_authorization_policy(method, path).minimum_role == "member", path

    assert (
        http_authorization_policy("PUT", "/api/permissions/authorized-users").minimum_role
        == "owner"
    )
    assert http_authorization_policy("POST", "/api/agents/default").minimum_role == "owner"
    assert _REMOTE_ACCESS_HTTP_NAMESPACE == "/api/remote-access"
    assert {(method, pattern.pattern) for method, pattern in _REMOTE_ACCESS_MEMBER_HTTP_RULES} == {
        ("GET", r"^/api/remote-access/status$"),
        ("GET", r"^/api/remote-access/network-interfaces$"),
        ("POST", r"^/api/remote-access/optimize-route$"),
        ("POST", r"^/api/remote-access/diagnostics$"),
    }


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


_REMOTE_ACCESS_MEMBER_OPS = frozenset(
    {
        ("GET", "/api/remote-access/status"),
        ("GET", "/api/remote-access/network-interfaces"),
        ("POST", "/api/remote-access/optimize-route"),
        ("POST", "/api/remote-access/diagnostics"),
    }
)


def _registered_remote_access_routes():
    from vibe.ui_server import app

    routes = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or ()
        if not path or not path.startswith("/api/remote-access"):
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.append((method.upper(), path))
    return tuple(sorted(set(routes)))


def _remote_access_ok_payload(method: str, path: str) -> dict:
    if method == "GET" and path.endswith("/status"):
        return {"ok": True, "paired": True, "running": False}
    if method == "GET" and path.endswith("/network-interfaces"):
        return {"ok": True, "interfaces": []}
    if path.endswith("/optimize-route"):
        return {"ok": True}
    if path.endswith("/diagnostics"):
        return {"ok": True, "checks": []}
    return {"ok": True}


def _install_remote_access_handler_stubs(monkeypatch) -> None:
    from vibe import remote_access

    monkeypatch.setattr(
        remote_access,
        "status",
        lambda *args, **kwargs: _remote_access_ok_payload("GET", "/api/remote-access/status"),
    )
    monkeypatch.setattr(
        remote_access,
        "network_interfaces",
        lambda *args, **kwargs: _remote_access_ok_payload(
            "GET", "/api/remote-access/network-interfaces"
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "optimize_route",
        lambda *args, **kwargs: _remote_access_ok_payload(
            "POST", "/api/remote-access/optimize-route"
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "connectivity_diagnostics",
        lambda *args, **kwargs: _remote_access_ok_payload(
            "POST", "/api/remote-access/diagnostics"
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "pair",
        lambda *args, **kwargs: _remote_access_ok_payload(
            "POST", "/api/remote-access/vibe-cloud/pair"
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "start",
        lambda *args, **kwargs: _remote_access_ok_payload("POST", "/api/remote-access/start"),
    )
    monkeypatch.setattr(
        remote_access,
        "stop",
        lambda *args, **kwargs: _remote_access_ok_payload("POST", "/api/remote-access/stop"),
    )
    monkeypatch.setattr(
        remote_access,
        "apply_settings",
        lambda *args, **kwargs: _remote_access_ok_payload(
            "POST", "/api/remote-access/settings"
        ),
    )


def _request_remote_access(client, method: str, path: str, headers: dict[str, str]):
    from tests.ui_server_test_helpers import remote_peer

    kwargs = {
        "headers": headers,
        "base_url": "https://alex.avibe.bot",
        "environ_base": remote_peer(),
    }
    if method not in {"GET", "HEAD", "OPTIONS"}:
        kwargs["json"] = {}
    return client.request(method, path, **kwargs)


def test_registered_remote_access_writes_default_to_owner(monkeypatch, tmp_path) -> None:
    """Every registered /api/remote-access write is owner unless it is a named ops route.

    Introspect the live router so a newly added pair/unpair/settings sibling
    fails this test until it is classified. Viewer and editor stay 403 on the
    whole namespace, matching master. Member keeps the four ops that cannot
    change pairing identity.
    """

    from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie, save_config
    from vibe import remote_access
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    _install_remote_access_handler_stubs(monkeypatch)

    routes = _registered_remote_access_routes()
    assert routes, "router must expose /api/remote-access"
    for method, path in _REMOTE_ACCESS_MEMBER_OPS:
        assert (method, path) in routes, f"{method} {path} must remain registered"

    for method, path in routes:
        policy = http_authorization_policy(method, path)
        if (method, path) in _REMOTE_ACCESS_MEMBER_OPS:
            assert policy.minimum_role == "member", f"{method} {path}"
        else:
            assert policy.minimum_role == "owner", (
                f"{method} {path} must default-deny to owner"
            )

    for role in ("viewer", "editor", "member", "owner"):
        client = app.test_client()
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            remote_session_cookie(
                config,
                f"{role}@example.com",
                f"{role}-1",
                role=role,
                access_source="email" if role != "owner" else "owner",
            ),
            domain="alex.avibe.bot",
        )
        headers = csrf_headers(client, base_url="https://alex.avibe.bot")
        for method, path in routes:
            response = _request_remote_access(client, method, path, headers)
            policy_role = http_authorization_policy(method, path).minimum_role
            admitted = _context(role, remote=True).has_role(policy_role or "owner")
            if admitted:
                assert response.status_code != 403, f"{role} {method} {path}"
            else:
                assert response.status_code == 403, f"{role} {method} {path}"
                assert response.get_json()["error"] == "instance_access_forbidden"


@pytest.mark.parametrize(
    "remote_access_payload",
    (
        None,
        False,
        [],
        0,
        "",
        {
            "vibe_cloud": {
                "instance_id": "inst-hijacked",
                "backend_url": "https://attacker.example",
                "instance_secret": "stolen-secret",
                "tunnel_token": "stolen-tunnel",
                "session_secret": "stolen-session",
                "enabled": False,
            }
        },
    ),
)
def test_member_config_write_cannot_change_pairing_identity(
    monkeypatch, tmp_path, remote_access_payload
) -> None:
    """Member POST /api/config cannot change pairing identity in any value shape."""

    from config.v2_config import V2Config
    from tests.ui_server_test_helpers import csrf_headers, remote_peer, remote_session_cookie, save_config
    from vibe import remote_access
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.example"
    cloud.instance_secret = "device-secret"
    cloud.tunnel_token = "tunnel-token"
    config.save()
    remote_access._replace_authorization_revision(config, 1)  # noqa: SLF001
    cookie = remote_session_cookie(
        config,
        "member@example.com",
        "member-1",
        role="member",
        access_source="email",
        session_claims={
            "vibe_instance_id": cloud.instance_id,
            "vibe_instance_role": "member",
            "vibe_instance_access_source": "email",
            "vibe_instance_authorization_revision": 1,
        },
    )
    before = V2Config.load().remote_access.vibe_cloud
    monkeypatch.setattr(remote_access, "reconcile", lambda: {"ok": True})

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        cookie,
        domain="alex.avibe.bot",
    )
    headers = csrf_headers(client, base_url="https://alex.avibe.bot")
    response = client.post(
        "/api/config",
        json={"remote_access": remote_access_payload},
        headers=headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )

    assert response.status_code == 200
    after = V2Config.load().remote_access.vibe_cloud
    assert after.instance_id == before.instance_id == "inst_123"
    assert after.backend_url == before.backend_url == "https://backend.example"
    assert after.instance_secret == before.instance_secret == "device-secret"
    assert after.tunnel_token == before.tunnel_token == "tunnel-token"
    assert after.session_secret == before.session_secret == "session-secret"
    assert after.enabled is True


def test_pair_is_forbidden_for_member_and_succeeds_for_owner(monkeypatch, tmp_path) -> None:
    from tests.ui_server_test_helpers import csrf_headers, remote_peer, remote_session_cookie, save_config
    from vibe import remote_access
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    paired: list[tuple[str, str, str]] = []

    def pair(pairing_key, backend_url, device_name):
        paired.append((pairing_key, backend_url, device_name))
        return {"ok": True, "instance_id": "inst_new"}

    monkeypatch.setattr(remote_access, "pair", pair)

    member_client = app.test_client()
    member_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "member@example.com",
            "member-1",
            role="member",
            access_source="email",
        ),
        domain="alex.avibe.bot",
    )
    member_headers = csrf_headers(member_client, base_url="https://alex.avibe.bot")
    member_response = member_client.post(
        "/api/remote-access/vibe-cloud/pair",
        json={"pairing_key": "key", "backend_url": "https://avibe.bot"},
        headers=member_headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert member_response.status_code == 403
    assert member_response.get_json()["error"] == "instance_access_forbidden"
    assert paired == []

    owner_client = app.test_client()
    owner_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "owner@example.com",
            "owner-1",
            role="owner",
            access_source="owner",
        ),
        domain="alex.avibe.bot",
    )
    owner_headers = csrf_headers(owner_client, base_url="https://alex.avibe.bot")
    owner_response = owner_client.post(
        "/api/remote-access/vibe-cloud/pair",
        json={"pairing_key": "key", "backend_url": "https://avibe.bot", "device_name": "avibe"},
        headers=owner_headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert owner_response.status_code == 200
    assert owner_response.get_json()["ok"] is True
    assert paired == [("key", "https://avibe.bot", "avibe")]


def test_member_cannot_set_instance_default_agent(monkeypatch, tmp_path) -> None:
    """Instance-wide default routing stays owner-only, and audience-usable for all.

    Two independent rules guard the same surface: only the Owner may write it,
    and whatever is written must be usable by the audience it routes for. A
    member's private Agent fails the first as a member and the second as the
    Owner.
    """

    from core.vibe_agents import VibeAgentAccessError, VibeAgentStore
    from tests.ui_server_test_helpers import csrf_headers, remote_peer, remote_session_cookie, save_config
    from vibe import remote_access
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    member = AuthorizationContext(
        instance_role="member",
        subject="member-1",
        email="member@example.com",
        instance_access_source="email",
        is_remote=True,
    )
    store = VibeAgentStore()
    try:
        default_agent = store.ensure_builtin_default_agent(backend="codex")
        store.set_default_agent_name(default_agent.name)
        before = store.get_default_agent_name()
        private_agent = store.create(
            name="member-private",
            backend="codex",
            user_context=member,
        )
        with pytest.raises(VibeAgentAccessError):
            store.set_default_agent_name(private_agent.name, user_context=member)
        assert store.get_default_agent_name() == before
    finally:
        store.close()

    member_client = app.test_client()
    member_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "member@example.com",
            "member-1",
            role="member",
            access_source="email",
        ),
        domain="alex.avibe.bot",
    )
    member_headers = csrf_headers(member_client, base_url="https://alex.avibe.bot")
    member_response = member_client.post(
        "/api/agents/default",
        json={"name": "member-private"},
        headers=member_headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert member_response.status_code == 403
    assert member_response.get_json()["error"] == "instance_access_forbidden"
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == before
    finally:
        store.close()

    owner_client = app.test_client()
    owner_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "owner@example.com",
            "owner-1",
            role="owner",
            access_source="owner",
        ),
        domain="alex.avibe.bot",
    )
    owner_headers = csrf_headers(owner_client, base_url="https://alex.avibe.bot")
    # Owner-only is the role gate; the audience rule is the second, independent
    # one. Instance-wide default routing serves everyone, so a single-subject
    # Agent is refused even here — only an audience-usable one is accepted.
    owner_response = owner_client.post(
        "/api/agents/default",
        json={"name": "member-private"},
        headers=owner_headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert owner_response.status_code == 403
    assert owner_response.get_json()["code"] == "agent_access_forbidden"
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == before
        store.create(name="team-shared", backend="codex")
    finally:
        store.close()

    shared_response = owner_client.post(
        "/api/agents/default",
        json={"name": "team-shared"},
        headers=owner_headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert shared_response.status_code == 200
    assert shared_response.get_json()["ok"] is True
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == "team-shared"
    finally:
        store.close()
