import re

import pytest

from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    _EDITOR_HTTP_NAMESPACES,
    _MEMBER_HTTP_RULES,
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
    The member surface is the opposite shape: an explicit allow-list, so an
    unknown API fails closed to Owner along with allowlist mutation,
    pairing-identity writes, and bulk Agent onboarding.
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
        # Read-only model catalogs behind Chat's route picker and the Agents
        # detail panel. Editor-tier because the picker is an editor surface.
        ("GET", "/api/models/agents/claude/models"),
        ("GET", "/api/claude/models"),
        ("GET", "/api/codex/models"),
    )
    for method, path in editor_examples:
        assert http_authorization_policy(method, path).minimum_role == "editor", path

    viewer_examples = (
        ("GET", "/api/memory/settings"),
        ("PATCH", "/api/memory/settings"),
        ("POST", "/api/memory/runtime/wake"),
        ("POST", "/api/memory/repair"),
        ("POST", "/api/memory/delete-data"),
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
        ("POST", "/api/agents/default"),
        ("PATCH", "/api/agents/demo"),
        ("DELETE", "/api/agents/demo"),
        ("GET", "/api/models/agents/codex/chains"),
        ("PUT", "/api/models/agents/codex/chain"),
        ("PUT", "/api/models/agents/codex/models"),
        ("PUT", "/api/global-prompts"),
        ("POST", "/api/projects"),
        ("PATCH", "/api/projects/project-1"),
        ("PUT", "/api/projects/project-1/agents-md"),
        ("GET", "/api/settings"),
        ("GET", "/api/users"),
        ("GET", "/api/remote-access/status"),
        ("GET", "/api/remote-access/network-interfaces"),
        ("POST", "/api/remote-access/optimize-route"),
        ("POST", "/api/remote-access/diagnostics"),
    )
    for method, path in member_examples:
        assert http_authorization_policy(method, path).minimum_role == "member", path

    owner_examples = (
        # Unknown APIs, present and future, keep the role they had before the
        # member rank existed.
        ("GET", "/api/future-owner-capability"),
        ("POST", "/api/control"),
        ("POST", "/api/upgrade"),
        ("POST", "/api/logs"),
        # Classifying the read-only catalogs above does not widen /api/backend:
        # credential, custom-provider, install, and runtime routes keep Owner by
        # the unknown-route default. That includes OpenCode's provider catalog --
        # it is the Settings surface (base URLs, masked keys, active auth type,
        # tool-call permission state) and reading it starts the daemon, so unlike
        # the Claude and Codex snapshots it is not a catalog a lower rank may
        # read. The model picker treats its 403 as "no catalog".
        ("GET", "/api/backend/opencode/providers"),
        ("POST", "/api/backend/codex/auth"),
        ("POST", "/api/backend/claude/auth"),
        ("POST", "/api/backend/opencode/auth"),
        ("DELETE", "/api/backend/opencode/auth/anthropic"),
        ("POST", "/api/backend/opencode/providers"),
        ("POST", "/api/backend/opencode/future-mutation"),
        ("POST", "/api/claude/models/refresh"),
        ("POST", "/api/codex/future-mutation"),
        # Access administration and bearer credentials.
        ("PUT", "/api/permissions/authorized-users"),
        ("GET", "/api/users/bind-codes"),
        ("POST", "/api/users/first-bind-code"),
        # ACL writes and the IM access boundary.
        ("PUT", "/api/permissions/projects/project-1/access"),
        ("PUT", "/api/permissions/resources/agent/agent-1/access"),
        ("POST", "/api/settings"),
        ("POST", "/api/settings/thread"),
        # Host reach.
        ("POST", "/api/browse"),
        ("POST", "/api/browse/mkdir"),
        # Bulk migration.
        ("GET", "/api/agent-onboarding"),
        ("POST", "/api/agent-onboarding"),
    )
    for method, path in owner_examples:
        assert http_authorization_policy(method, path).minimum_role == "owner", path

    assert _REMOTE_ACCESS_HTTP_NAMESPACE == "/api/remote-access"
    assert {(method, pattern.pattern) for method, pattern in _REMOTE_ACCESS_MEMBER_HTTP_RULES} == {
        ("GET", r"^/api/remote-access/status$"),
        ("GET", r"^/api/remote-access/network-interfaces$"),
        ("POST", r"^/api/remote-access/optimize-route$"),
        ("POST", r"^/api/remote-access/diagnostics$"),
    }


def test_member_reachable_routes_are_bounded_by_declaration() -> None:
    """Sweep the live router: nothing is member-reachable except by declaration.

    The member rank was first added as the *fallback* for an unclassified
    ``/api`` route. That inverted default-deny: every management route no other
    table happened to name was silently widened -- ``POST /api/control``,
    ``POST /api/upgrade``, the ``/api/backend/*/auth`` routes, the ACL PUTs, and
    a hundred more. Review could only find them one head at a time, because a
    list of Owner exceptions is never more complete than the last audit.

    So the fallback is Owner and the member surface is the allow-list, and this
    is the property that replaces those exceptions: every registered ``/api``
    route resolves to an explicit tier; a route resolves to member only because
    ``_MEMBER_HTTP_RULES`` or the remote-access ops quartet says so; and a route
    the router does not have yet resolves to Owner. A management route added
    tomorrow therefore keeps exactly the role it would have had before this rank
    existed.
    """

    from vibe.ui_server import app

    def _member_declared(method: str, path: str) -> bool:
        return any(
            rule_method == method and pattern.fullmatch(path)
            for rule_method, pattern in (*_MEMBER_HTTP_RULES, *_REMOTE_ACCESS_MEMBER_HTTP_RULES)
        )

    seen_member = False
    swept = 0
    for route in app.routes:
        raw_path = getattr(route, "path", None) or ""
        if not raw_path.startswith("/api/") and raw_path != "/api":
            continue
        path = _sample_path(raw_path)
        for method in sorted(getattr(route, "methods", None) or ()):
            method = method.upper()
            if method in {"HEAD", "OPTIONS"}:
                continue
            swept += 1
            policy = http_authorization_policy(method, path)
            assert policy is not None, f"{method} {path}"
            assert policy.minimum_role in {"viewer", "editor", "member", "owner"}, f"{method} {path}"
            if policy.minimum_role == "member":
                seen_member = True
                assert _member_declared(method, path), (
                    f"{method} {path} is member-reachable without a declared rule"
                )
    assert swept > 100, "router sweep found too few /api routes to be meaningful"
    assert seen_member, "the member surface must be reachable from the live router"

    # A route nobody has classified -- including one that does not exist yet --
    # is Owner, not member.
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        synthetic = http_authorization_policy(method, "/api/route-added-after-the-member-rank")
        assert synthetic is not None
        assert synthetic.minimum_role == "owner", method


def test_agents_page_load_reads_are_admitted_for_every_rank_that_sees_the_page() -> None:
    """The GETs one Agents-page load issues must be admitted by the ranks it renders for.

    A remote member landing on ``/agents`` got two ``instance_access_forbidden``
    toasts after the Agent ACL fix: the page's own reads were reachable, but the
    onboarding inventory was requested off ``can_manage_agents`` (a member bit)
    while the route is deliberately Owner, and the backend model catalogs the
    detail panel loads were classified by nobody and so fell to the Owner
    default. The property is stated over the ranks rather than over a list of
    fixed cases: whoever can reach the surface can complete its load. Chat's
    route picker shares the catalog loader and is an editor surface, so editor is
    the floor for the catalogs; the rest of the page is member management.

    OpenCode is the exception the test also pins. It has no catalog separable
    from its Settings surface, so its read stays Owner and the picker degrades to
    a typed model id -- silently, because the loader declares that refusal
    expected. "Completes its load" therefore means "issues no request whose 403
    the user is told about", not "every backend answers".
    """

    page_load_reads = (
        ("GET", "/api/agents"),
        ("GET", "/api/agents/demo"),
        ("GET", "/api/running-agents"),
        ("GET", "/api/models/agents"),
        ("GET", "/api/models/agents/claude/chains"),
    )
    catalog_reads = (
        ("GET", "/api/models/agents/claude/models"),
        ("GET", "/api/claude/models"),
        ("GET", "/api/codex/models"),
    )

    for role in ("editor", "member", "owner"):
        context = _context(role, remote=True)
        for method, path in catalog_reads:
            minimum_role = http_authorization_policy(method, path).minimum_role
            assert minimum_role is not None and context.has_role(minimum_role), f"{role} {path}"

    for role in ("member", "owner"):
        context = _context(role, remote=True)
        for method, path in (*page_load_reads, *catalog_reads):
            minimum_role = http_authorization_policy(method, path).minimum_role
            assert minimum_role is not None and context.has_role(minimum_role), f"{role} {path}"
    # The counterparts a member page load must not announce. Bulk onboarding is a
    # one-way instance-wide migration and must not be requested at all; the
    # OpenCode provider catalog may be requested but its refusal is expected data
    # for the picker. Both stay Owner, so a toast from either is a UI defect
    # rather than a policy gap.
    owner_only_page_neighbours = (
        ("GET", "/api/agent-onboarding"),
        ("POST", "/api/agent-onboarding"),
        ("GET", "/api/backend/opencode/providers"),
    )
    for method, path in owner_only_page_neighbours:
        minimum_role = http_authorization_policy(method, path).minimum_role
        assert minimum_role == "owner", path
        assert not _context("member", remote=True).has_role(minimum_role), path


def test_backend_catalog_write_follows_the_agent_management_boundary() -> None:
    path = "/api/models/agents/claude/models"
    assert http_authorization_policy("PUT", path).minimum_role == "member"
    assert not _context("editor", remote=True).has_role("member")
    assert _context("member", remote=True).has_role("member")

    source_model_mutations = (
        ("POST", "/api/models/sources/src-1/models"),
        ("PATCH", "/api/models/sources/src-1/models/model-1"),
        ("DELETE", "/api/models/sources/src-1/models/model-1"),
    )
    for method, source_path in source_model_mutations:
        assert http_authorization_policy(method, source_path).minimum_role == "owner"


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
    """Project CRUD follows can_manage_projects, not owner identity.

    Seed every existing instance role so a leftover require_instance_role(...,
    "owner") or is_instance_owner check cannot silently deny member while the
    capability projection still advertises the surface. Bulk Agent onboarding is
    asserted separately below: it is an instance-wide migration rather than an
    advertised management surface, so it stays on Owner identity.
    """

    for role in ("viewer", "editor", "member", "owner"):
        context = _context(role, remote=True)
        if context.can_manage_projects:
            assert require_instance_role(context, "member") is context
        else:
            with pytest.raises(InstanceAuthorizationError):
                require_instance_role(context, "member")


def test_bulk_agent_onboarding_follows_owner_identity() -> None:
    """Onboarding is a migration tool, so only the Instance Owner may run it.

    Seed every existing instance role rather than naming the rejected ones, so a
    role added later is covered here without editing this test: the property is
    that exactly the Owner passes. Deliberately not gated on
    ``can_manage_access_members`` -- claiming every policy-less Agent is not
    member management, it is an instance-wide one-way migration.
    """

    from core.vibe_agents import VibeAgentAccessError, _require_agent_onboarding_access

    for role in ("viewer", "editor", "member", "owner"):
        context = _context(role, remote=True)
        if context.is_instance_owner:
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


# Namespaces whose routes decide who reaches this instance at all. Contract
# amendment (issue #1596): the member set an Owner controls is cloud allowlist
# entries AND multi-platform IM bound users, so the IM bound-user and bind-code
# routes are member management. ``/api/agent-onboarding`` is in the sweep for a
# different reason -- it is an instance-wide one-way Agent migration -- but the
# assertion is the same: Owner-only.
_ACCESS_ADMINISTRATION_NAMESPACES = (
    "/api/users",
    "/api/bind-codes",
    "/api/setup/first-bind-code",
    "/api/agent-onboarding",
)

# The only route in those namespaces allowed below Owner: it discloses the member
# set without disclosing or minting a credential, matching the cloud allowlist
# whose GET is member and whose PUT is Owner. Everything else registered in the
# namespaces now or later must be Owner, which is what the sweep asserts.
_ACCESS_ADMINISTRATION_MEMBER_READS = frozenset({("GET", "/api/users")})


def _registered_access_administration_routes():
    from vibe.ui_server import app

    routes = []
    for route in app.routes:
        path = getattr(route, "path", None) or ""
        if not path.startswith(_ACCESS_ADMINISTRATION_NAMESPACES):
            continue
        for method in getattr(route, "methods", None) or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.append((method.upper(), path))
    return tuple(sorted(set(routes)))


def _sample_path(path: str) -> str:
    """Fill router path parameters so the route can actually be requested."""

    return re.sub(r"\{[^}]+\}", "sample-1", path)


def _install_access_administration_stubs(monkeypatch) -> None:
    """Stub the handlers so only the authorization outcome is under test."""

    from vibe import api

    ok = {"ok": True}
    for name in (
        "get_users",
        "save_users",
        "toggle_admin",
        "remove_user",
        "get_bind_codes",
        "create_bind_code",
        "delete_bind_code",
        "get_first_bind_code",
        "get_vibe_agent_onboarding",
        "onboard_vibe_agents",
    ):
        monkeypatch.setattr(api, name, lambda *args, **kwargs: dict(ok))


def test_registered_access_administration_routes_are_owner_only(monkeypatch, tmp_path) -> None:
    """Minting or mutating instance access is Owner-only, at policy and handler.

    Enumerated from the live router rather than from a list of known routes, so a
    bind-code, bound-user, or onboarding sibling added later is covered the day it
    is registered -- it inherits Owner from the unknown-route default and only a
    deliberate ``_MEMBER_HTTP_RULES`` entry can widen it. Editor and viewer stay
    403 across the whole set, matching master, where every one of these routes was
    already above their rank.
    """

    from tests.ui_server_test_helpers import (
        csrf_headers,
        remote_peer,
        remote_session_cookie,
        save_config,
    )
    from vibe import remote_access
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    _install_access_administration_stubs(monkeypatch)

    routes = _registered_access_administration_routes()
    assert routes, "router must expose the access-administration namespaces"

    for method, path in routes:
        policy = http_authorization_policy(method, _sample_path(path))
        if (method, path) in _ACCESS_ADMINISTRATION_MEMBER_READS:
            assert policy.minimum_role == "member", f"{method} {path}"
        else:
            assert policy.minimum_role == "owner", f"{method} {path} must be Owner-only"

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
            request_path = _sample_path(path)
            kwargs = {
                "headers": headers,
                "base_url": "https://alex.avibe.bot",
                "environ_base": remote_peer(),
            }
            if method not in {"GET", "HEAD", "OPTIONS"}:
                kwargs["json"] = {}
            response = client.request(method, request_path, **kwargs)
            policy_role = http_authorization_policy(method, request_path).minimum_role
            admitted = _context(role, remote=True).has_role(policy_role or "owner")
            if admitted:
                assert response.status_code != 403, f"{role} {method} {request_path}"
            else:
                assert response.status_code == 403, f"{role} {method} {request_path}"
                assert response.get_json()["error"] == "instance_access_forbidden"


def test_agent_cli_install_is_owner_only(monkeypatch, tmp_path) -> None:
    """Installing a backend CLI is host lifecycle, so it stays above member.

    The handler shells out to a package manager, a self-update, or a curl
    script, records the resulting CLI path, and can restart the backend. That is
    the "dependency installs" bullet the member policy declares Owner, and it
    had been on the member allow-list while the comment above that table said
    otherwise -- the table is hand-written, so the policy and its entries can
    disagree silently.

    Enforcement is by absence: neither route is declared, so both inherit the
    unknown-route Owner default and need no exception entry. Asserting the
    absence *and* the 403 it produces keeps those two from drifting apart, and
    the owner leg proves the routes are still live rather than unreachable for
    everyone. The install implementation is stubbed, so no package manager runs
    and the assertion at the end is what proves it: only the owner leg ever
    reached the handler.
    """

    from tests.ui_server_test_helpers import (
        csrf_headers,
        remote_peer,
        remote_session_cookie,
        save_config,
    )
    from vibe import api, remote_access
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)

    reached: list[tuple[str, str]] = []

    def _start(name: str) -> dict:
        reached.append(("start", name))
        return {"ok": True, "job_id": "job-1", "backend": name, "status": "running"}

    def _status(job_id: str, *, backend: str) -> dict:
        reached.append(("status", job_id))
        return {"ok": True, "job_id": job_id, "backend": backend, "status": "success"}

    monkeypatch.setattr(api, "start_agent_install_job", _start)
    monkeypatch.setattr(api, "get_agent_install_job", _status)

    routes = (
        ("POST", "/api/agent/claude/install"),
        ("GET", "/api/agent/claude/install/job-1"),
    )
    for method, path in routes:
        assert not any(
            rule_method == method and pattern.fullmatch(path)
            for rule_method, pattern in _MEMBER_HTTP_RULES
        ), f"{method} {path} must not be declared on the member allow-list"
        assert http_authorization_policy(method, path).minimum_role == "owner", f"{method} {path}"

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
            kwargs = {
                "headers": headers,
                "base_url": "https://alex.avibe.bot",
                "environ_base": remote_peer(),
            }
            if method != "GET":
                kwargs["json"] = {}
            response = client.request(method, path, **kwargs)
            if role == "owner":
                assert response.status_code == 200, f"{role} {method} {path}"
                assert response.get_json()["ok"] is True, f"{role} {method} {path}"
            else:
                assert response.status_code == 403, f"{role} {method} {path}"
                assert response.get_json()["error"] == "instance_access_forbidden"

    assert reached == [("start", "claude"), ("status", "job-1")], (
        "only the owner leg may reach the install handler"
    )


def test_access_administration_handlers_gate_without_the_route_policy(monkeypatch, tmp_path) -> None:
    """The handler gate stands on its own, not only the route policy table.

    Two layers answer one question, so this drives the handlers with the policy
    table neutralised: a route re-registered under a path the table does not
    recognise must still refuse a member.
    """

    from tests.ui_server_test_helpers import (
        csrf_headers,
        remote_peer,
        remote_session_cookie,
        save_config,
    )
    from vibe import authorization, remote_access
    from vibe.authorization import HttpAuthorizationPolicy
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    _install_access_administration_stubs(monkeypatch)
    # The request hook imports this by name at call time, so replacing the
    # module attribute really does disarm the policy layer for this test.
    monkeypatch.setattr(
        authorization,
        "http_authorization_policy",
        lambda *args, **kwargs: HttpAuthorizationPolicy("member"),
    )

    client = app.test_client()
    client.set_cookie(
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
    headers = csrf_headers(client, base_url="https://alex.avibe.bot")
    for method, path in _registered_access_administration_routes():
        if (method, path) in _ACCESS_ADMINISTRATION_MEMBER_READS:
            continue
        request_path = _sample_path(path)
        kwargs = {
            "headers": headers,
            "base_url": "https://alex.avibe.bot",
            "environ_base": remote_peer(),
        }
        if method not in {"GET", "HEAD", "OPTIONS"}:
            kwargs["json"] = {}
        response = client.request(method, request_path, **kwargs)
        assert response.status_code == 403, f"member {method} {request_path}"


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
    """Member POST /api/config cannot change pairing identity in any value shape.

    The refusal is the Editor allowlist, which every writer below Owner now runs:
    ``remote_access`` is not a field they may set, so the write is rejected
    outright rather than accepted with one key quietly removed. Stored pairing is
    asserted unchanged either way — the point is the identity, not the status code.
    """

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

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "editor_config_write_forbidden",
        "message": "editor_config_write_forbidden",
    }
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


def test_member_can_set_instance_default_agent(monkeypatch, tmp_path) -> None:
    """Default selection follows Agent management at service and HTTP layers.

    Member may point routing at any Agent it can manage, including one narrower
    than the instance audience. Editor and Viewer stay denied. The default stays
    advisory: per-principal degradation remains covered in
    ``tests/test_resource_acl_agents.py``.
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
        for role in ("viewer", "editor"):
            with pytest.raises(VibeAgentAccessError):
                store.set_default_agent_name(
                    private_agent.name,
                    user_context=_context(role, remote=True),
                )
        assert store.get_default_agent_name() == before
        store.set_default_agent_name(private_agent.name, user_context=member)
        assert store.get_default_agent_name() == private_agent.name
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
    assert member_response.status_code == 200
    assert member_response.get_json()["default_agent_name"] == "member-private"
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == "member-private"
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
    # Owner-only is the whole gate. The same Agent the member could not publish
    # is accepted from the Owner, because the target's audience is a use-time
    # question rather than a bind-time one.
    owner_response = owner_client.post(
        "/api/agents/default",
        json={"name": "member-private"},
        headers=owner_headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert owner_response.status_code == 200
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == "member-private"
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
