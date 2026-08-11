from __future__ import annotations

import pytest
from sqlalchemy import select

from config import paths
from core.dock_store import BUILTIN_DOCK_IDS, DockError
from core.show_pages import ShowPageError, ShowPageStore, public_url
from storage import media_service, project_access_service, projects_service, resource_access_service
from storage import workbench_sessions_service as sessions_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, show_pages
from tests.test_ui_remote_access_auth import _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, remote_access, ui_server
from vibe.ui_server import app


def _organization_context(
    subject: str,
    *,
    group_ids: frozenset[str] | None = frozenset({"group-engineering"}),
    organization_role: str = "member",
    instance_role: str = "viewer",
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role=organization_role,
        group_ids=group_ids,
        instance_role=instance_role,
        instance_access_source="organization_group",
        is_remote=True,
    )


def _organization_cookie(
    config,
    *,
    subject: str,
    groups: list[str] | None = None,
    instance_role: str = "viewer",
    organization_role: str = "member",
) -> str:
    claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_role": instance_role,
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": f"member-{subject}",
        "vibe_organization_role": organization_role,
        "vibe_membership_version": "membership-v2",
    }
    if groups is not None:
        claims["vibe_group_ids"] = groups
    return remote_access.make_session_cookie(
        config,
        f"{subject}@example.com",
        subject,
        session_claims=claims,
    )


def _seed_show_pages_with_policies() -> ShowPageStore:
    store = ShowPageStore()
    for session_id in ("ses-private", "ses-public", "ses-scope"):
        store.ensure(session_id)
    with store.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="show_page",
            resource_id="ses-private",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="private",
        )
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="show_page",
            resource_id="ses-public",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="public",
        )
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="show_page",
            resource_id="ses-scope",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="scope",
            group_ids=["group-engineering"],
        )
    return store


def test_active_org_members_see_all_show_pages_during_temporary_bypass(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    try:
        owner_ids = {page.session_id for page in store.list(user_context=_organization_context("owner-1"))}
        member_ids = {page.session_id for page in store.list(user_context=_organization_context("member-1"))}
        no_group_ids = {
            page.session_id
            for page in store.list(user_context=_organization_context("member-2", group_ids=None))
        }
    finally:
        store.close()

    assert owner_ids == {"ses-private", "ses-public", "ses-scope"}
    assert member_ids == {"ses-private", "ses-public", "ses-scope"}
    assert no_group_ids == {"ses-private", "ses-public", "ses-scope"}


def test_show_page_email_context_bypasses_audience_only_for_its_signed_page(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    context = resource_access_service.ResourceUserContext(
        subject="guest-1",
        email="guest@example.com",
        instance_role="viewer",
        instance_access_source="show_page_email",
        show_page_id="ses-private",
        is_remote=True,
    )
    try:
        with store.engine.connect() as connection:
            assert resource_access_service.can_use_resource(
                context,
                "show_page",
                "ses-private",
                connection=connection,
            )
            assert not resource_access_service.can_use_resource(
                context,
                "show_page",
                "ses-public",
                connection=connection,
            )
            assert not resource_access_service.can_use_resource(
                context,
                "agent",
                "ses-private",
                connection=connection,
            )
    finally:
        store.close()


def test_non_org_email_context_keeps_exact_page_entitlement_separate_from_role_rank(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    context = resource_access_service.ResourceUserContext(
        subject="member-1",
        email="member@example.com",
        instance_role="editor",
        instance_access_source="email",
        show_page_id="ses-private",
        is_remote=True,
    )
    try:
        with store.engine.connect() as connection:
            assert resource_access_service.can_use_resource(
                context,
                "show_page",
                "ses-private",
                connection=connection,
            )
            assert not resource_access_service.can_use_resource(
                context,
                "show_page",
                "ses-scope",
                connection=connection,
            )
            assert context.can_chat
            assert context.can_use_resource("agent")
    finally:
        store.close()


@pytest.mark.parametrize(
    ("subject", "instance_role", "organization_role"),
    [
        ("owner-2", "owner", "owner"),
        ("member-1", "viewer", "member"),
    ],
)
def test_remote_org_members_can_open_all_show_pages_temporarily(
    monkeypatch,
    tmp_path,
    subject,
    instance_role,
    organization_role,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()

    async def _runtime_response(*args, **kwargs):
        return ui_server.FastAPIResponse(content=b"Show Page", media_type="text/html")

    monkeypatch.setattr(ui_server, "_show_page_runtime_response", _runtime_response)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject=subject,
            groups=["group-engineering"],
            instance_role=instance_role,
            organization_role=organization_role,
        ),
        domain="alex.avibe.bot",
    )

    catalog = client.get(
        "/api/show-pages",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    mutation = client.post(
        "/api/show-pages/ses-public/visibility",
        json={"visibility": "offline"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    page = client.get(
        "/show/ses-private/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert catalog.status_code == 200
    assert {item["session_id"] for item in catalog.get_json()["pages"]} == {
        "ses-private",
        "ses-public",
        "ses-scope",
    }
    assert all(item.get("path") for item in catalog.get_json()["pages"])
    assert mutation.status_code == 200
    assert mutation.get_json()["visibility"] == "offline"
    assert page.status_code == 200


def test_remote_show_page_creation_persists_real_org_identity(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    owner_context = _organization_context("owner-1", instance_role="owner")
    store = ShowPageStore()
    try:
        created = store.ensure(
            "ses-editor-created",
            user_context=_organization_context("member-1", instance_role="editor"),
        )
        assert created.session_id == "ses-editor-created"
        with store.engine.connect() as connection:
            created_policy = resource_access_service.get_resource_policy(
                "show_page", created.session_id, connection=connection
            )
        assert created_policy is not None
        assert created_policy["organization_id"] == "org-1"
        assert created_policy["owner_user_id"] == "member-1"
        assert "is_trusted_local" not in created_policy
        page = store.ensure("ses-org-public", user_context=owner_context)
        with store.engine.begin() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page",
                page.session_id,
                connection=connection,
            )
            assert policy is not None
            assert policy["organization_id"] == "org-1"
            assert policy["owner_user_id"] == "owner-1"
            assert policy["access_level"] == "private"
            resource_access_service.apply_control_plane_intent(
                connection,
                organization_id="org-1",
                resource_kind="show_page",
                resource_id=page.session_id,
                revision=1,
                access_level="public",
                group_ids=[],
            )
        updated = store.get(page.session_id)
        assert updated is not None
        assert updated.visibility == "private"
        assert updated.share_id is None
        assert public_url(updated.share_id) is None
    finally:
        store.close()


def test_show_page_scope_without_group_context_is_open_during_temporary_bypass(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    try:
        page = store.require_access("ses-scope", user_context=_organization_context("member-1", group_ids=None))
    finally:
        store.close()

    assert page.session_id == "ses-scope"


def test_active_organization_admin_can_manage_all_pages_temporarily(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    admin = _organization_context(
        "admin-1",
        group_ids=frozenset({"group-sales"}),
        organization_role="admin",
        instance_role="viewer",
    )
    owner = _organization_context("owner-1", instance_role="owner")
    try:
        public_page = store.update_visibility("ses-private", "public", user_context=owner)
        assert store.require_access("ses-private", user_context=admin).session_id == "ses-private"
        assert store.update_visibility("ses-private", "private", user_context=admin).visibility == "private"
        # Share-id operations retain their existing public-page invariant even
        # though the temporary Org policy admits the caller to the resource.
        assert store.update_visibility("ses-private", "public", user_context=admin).visibility == "public"
        admin_rotated, _ = store.rotate_share("ses-private", user_context=admin)
        admin_custom, _ = store.set_share_id("ses-private", "admin-link", user_context=admin)
        assert admin_rotated.share_id
        assert admin_custom.share_id == "admin-link"

        republished = store.update_visibility("ses-private", "public", user_context=owner)
        rotated, previous_share_id = store.rotate_share("ses-private", user_context=owner)
        custom, rotated_share_id = store.set_share_id("ses-private", "owner-link", user_context=owner)

        assert public_page.visibility == "public"
        assert republished.visibility == "public"
        assert previous_share_id == republished.share_id
        assert rotated_share_id == rotated.share_id
        assert custom.share_id == "owner-link"

        assert store.require_access("ses-scope", user_context=admin).session_id == "ses-scope"
        assert store.update_visibility("ses-scope", "offline", user_context=admin).visibility == "offline"
        restored = store.update_visibility("ses-scope", "private", user_context=admin)
        assert restored.visibility == "private"
    finally:
        store.close()


def test_access_only_manager_visibility_response_does_not_expose_page_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    admin = _organization_context(
        "admin-1",
        group_ids=frozenset({"group-sales"}),
        organization_role="admin",
        instance_role="viewer",
    )
    try:
        store.update_visibility(
            "ses-private",
            "public",
            user_context=_organization_context("owner-1", instance_role="viewer"),
        )
    finally:
        store.close()
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda _value=None: admin,
    )

    result = api.set_show_page_visibility("ses-private", "private")

    assert result["ok"] is True
    assert result["session_id"] == "ses-private"
    assert result["visibility"] == "private"


def test_excluded_scoped_owner_share_mutations_do_not_expose_page_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    excluded_owner = _organization_context(
        "owner-1",
        group_ids=frozenset({"group-sales"}),
        instance_role="viewer",
    )
    try:
        store.update_visibility("ses-scope", "public", user_context=excluded_owner)
    finally:
        store.close()
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda _value=None: excluded_owner,
    )

    rotated = api.rotate_show_page_share("ses-scope")
    customized = api.set_show_page_share_id("ses-scope", "excluded-owner-link")

    assert rotated["ok"] is True
    assert rotated["session_id"] == "ses-scope"
    assert rotated["visibility"] == "public"
    assert customized["ok"] is True
    assert customized["session_id"] == "ses-scope"
    assert customized["visibility"] == "public"


def test_remote_show_page_owner_can_control_sharing_without_instance_owner_role(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    page_owner = _organization_context("owner-1", instance_role="viewer")
    try:
        published = store.update_visibility("ses-private", "public", user_context=page_owner)
        closed = store.update_visibility("ses-private", "private", user_context=page_owner)
        republished = store.update_visibility("ses-private", "public", user_context=page_owner)
        rotated, previous_share_id = store.rotate_share("ses-private", user_context=page_owner)
        customized, rotated_share_id = store.set_share_id(
            "ses-private",
            "page-owner-link",
            user_context=page_owner,
        )

        assert published.visibility == "public"
        assert closed.visibility == "private"
        assert previous_share_id == republished.share_id
        assert rotated_share_id == rotated.share_id
        assert customized.share_id == "page-owner-link"
    finally:
        store.close()


def test_remote_show_page_owner_mutations_reach_resource_acl_as_viewer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store = ShowPageStore()
    project_path = tmp_path / "project"
    project_path.mkdir()
    with store.engine.begin() as connection:
        project = projects_service.create_project(connection, str(project_path))
        session = sessions_service.create_session(
            connection,
            scope_id=project["scope_id"],
            agent_backend="codex",
        )
        session_id = session["id"]
    store.ensure(session_id)
    with store.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="show_page",
            resource_id=session_id,
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="private",
        )
    store.close()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="owner-1", groups=[], instance_role="viewer"),
        domain="alex.avibe.bot",
    )
    request_options = {
        "headers": csrf_headers(client, "https://alex.avibe.bot"),
        "base_url": "https://alex.avibe.bot",
        "environ_base": _remote_peer(),
    }

    ensured = client.post(
        f"/api/show-pages/{session_id}/ensure",
        json={},
        **request_options,
    )
    published = client.post(
        f"/api/show-pages/{session_id}/visibility",
        json={"visibility": "public"},
        **request_options,
    )
    rotated = client.post(
        f"/api/show-pages/{session_id}/rotate-share",
        json={},
        **request_options,
    )
    customized = client.post(
        f"/api/show-pages/{session_id}/share-id",
        json={"share_id": "viewer-owner-link"},
        **request_options,
    )

    assert ensured.status_code == 200
    assert ensured.get_json()["existed"] is True
    assert published.status_code == 200
    assert rotated.status_code == 200
    assert customized.status_code == 200
    assert customized.get_json()["share_id"] == "viewer-owner-link"


def test_organization_admin_can_read_show_page_access_metadata_without_use_access(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()
    admin = _organization_context(
        "admin-1",
        group_ids=frozenset({"group-sales"}),
        organization_role="admin",
        instance_role="viewer",
    )
    monkeypatch.setattr(resource_access_service, "resolve_resource_access_context", lambda _value=None: admin)

    payload = api.get_show_page_access("ses-scope")

    assert payload["access_level"] == "scope"
    assert payload["instance_id"] is None
    assert payload["can_use"] is True
    assert payload["can_manage"] is True
    assert payload["can_publish_public"] is True


def test_opening_existing_organization_page_does_not_register_new_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-legacy")
        page = store.ensure(
            "ses-legacy",
            user_context=_organization_context("owner-1", instance_role="owner"),
        )
        with store.engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page",
                page.session_id,
                connection=connection,
            )
        assert policy is None
    finally:
        store.close()


def test_show_page_access_api_distinguishes_personal_and_organization_modes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    personal_store = ShowPageStore()
    try:
        personal_store.ensure("ses-personal")
    finally:
        personal_store.close()

    personal = app.test_client().get("/api/show-pages/ses-personal/access")
    assert personal.status_code == 200
    assert personal.get_json() == {
        "ok": True,
        "mode": "personal",
        "instance_id": None,
        "organization_id": None,
        "access_level": "private",
        "group_ids": [],
        "policy_revision": None,
        "last_applied_control_plane_revision": None,
        "can_use": True,
        "can_manage": True,
        "can_publish_public": True,
        "public_link_enabled": False,
    }

    config = _save_config(tmp_path)
    store = ShowPageStore()
    try:
        store.ensure("ses-organization")
        with store.engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id="ses-organization",
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="scope",
                group_ids=["group-engineering"],
                policy_revision=4,
                last_applied_control_plane_revision=4,
            )
    finally:
        store.close()
    local_organization = app.test_client().get(
        "/api/show-pages/ses-organization/access",
        base_url="http://127.0.0.1:15131",
    )
    assert local_organization.status_code == 200
    assert local_organization.get_json()["instance_id"] == "inst_123"

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="owner-1", groups=["group-engineering"], instance_role="owner"),
        domain="alex.avibe.bot",
    )
    organization = client.get(
        "/api/show-pages/ses-organization/access",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert organization.status_code == 200
    assert organization.get_json() == {
        "ok": True,
        "mode": "organization",
        "instance_id": "inst_123",
        "organization_id": "org-1",
        "access_level": "scope",
        "group_ids": ["group-engineering"],
        "policy_revision": 4,
        "last_applied_control_plane_revision": 4,
        "can_use": True,
        "can_manage": True,
        "can_publish_public": True,
        "public_link_enabled": False,
    }


def test_show_page_access_api_reports_missing_page_as_definitive_denial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    response = app.test_client().get("/api/show-pages/ses-missing/access")

    assert response.status_code == 404
    assert response.get_json()["code"] == "show_page_not_found"


def test_show_page_owner_can_read_and_replace_exact_email_grants(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-email-access")
    finally:
        store.close()

    calls: list[tuple] = []
    monkeypatch.setattr(
        remote_access,
        "get_show_page_authorized_emails",
        lambda show_page_id: calls.append(("GET", show_page_id))
        or {"emails": ["guest@example.com"]},
    )
    monkeypatch.setattr(
        remote_access,
        "replace_show_page_authorized_emails",
        lambda show_page_id, emails: calls.append(("PUT", show_page_id, emails))
        or {"emails": emails, "changed": True},
    )
    client = app.test_client()

    loaded = client.get("/api/show-pages/ses-email-access/authorized-emails")
    replaced = client.put(
        "/api/show-pages/ses-email-access/authorized-emails",
        json={"emails": [" Guest@Example.com ", "guest@example.com"]},
        headers=csrf_headers(client),
    )

    assert loaded.status_code == 200
    assert loaded.get_json() == {"ok": True, "emails": ["guest@example.com"]}
    assert loaded.headers["Cache-Control"] == "no-store, private"
    assert replaced.status_code == 200
    assert replaced.get_json() == {
        "ok": True,
        "emails": ["guest@example.com"],
        "changed": True,
    }
    assert calls == [
        ("GET", "ses-email-access"),
        ("PUT", "ses-email-access", ["guest@example.com"]),
    ]


@pytest.mark.parametrize(
    ("subject", "organization_role", "instance_role"),
    [
        ("member-1", "member", "viewer"),
        ("admin-1", "admin", "owner"),
    ],
)
def test_show_page_email_grants_reject_non_owner_without_contacting_backend(
    monkeypatch,
    tmp_path,
    subject: str,
    organization_role: str,
    instance_role: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda _value=None: resource_access_service.ResourceUserContext(
            subject=subject,
            email=f"{subject}@example.com",
            instance_role=instance_role,
            instance_access_source="email",
            is_remote=True,
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "get_show_page_authorized_emails",
        lambda _show_page_id: pytest.fail("backend must not be contacted"),
    )

    response = app.test_client().get(
        "/api/show-pages/ses-public/authorized-emails"
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "resource_access_forbidden"


def test_show_page_email_grants_report_unavailable_without_cloud_pairing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = ShowPageStore()
    try:
        store.ensure("ses-email-access")
    finally:
        store.close()

    response = app.test_client().get(
        "/api/show-pages/ses-email-access/authorized-emails"
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "show_page_email_access_not_configured"


def test_show_page_email_grants_report_transient_device_failures_as_retryable(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = ShowPageStore()
    try:
        store.ensure("ses-email-access-transient")
    finally:
        store.close()
    monkeypatch.setattr(
        remote_access,
        "get_show_page_authorized_emails",
        lambda _show_page_id: (_ for _ in ()).throw(
            RuntimeError("resource_acl_device_unavailable")
        ),
    )

    response = app.test_client().get(
        "/api/show-pages/ses-email-access-transient/authorized-emails"
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "show_page_email_access_transient"


def test_show_page_email_grant_device_requests_freeze_the_target(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(remote_access, "_resource_acl_sync_configured", lambda _config: True)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda cfg, method, suffix, payload=None: calls.append(
            (cfg, method, suffix, payload)
        )
        or (
            {"emails": ["guest@example.com"]}
            if method == "GET"
            else {
                "emails": ["guest@example.com"],
                "changed": True,
                "authorization_revision": 9,
            }
        ),
    )
    revisions: list[int] = []
    monkeypatch.setattr(
        remote_access,
        "_replace_authorization_revision",
        lambda _config, revision: revisions.append(revision) or revision,
    )

    loaded = remote_access.get_show_page_authorized_emails("session/one", config)
    replaced = remote_access.replace_show_page_authorized_emails(
        "session/one",
        ["guest@example.com"],
        config,
    )

    assert loaded == {"emails": ["guest@example.com"]}
    assert replaced == {"emails": ["guest@example.com"], "changed": True}
    assert calls == [
        (config, "GET", "show-pages/session%2Fone/authorized-emails", None),
        (
            config,
            "PUT",
            "show-pages/session%2Fone/authorized-emails",
            {"emails": ["guest@example.com"]},
        ),
    ]
    assert revisions == [9]


def test_show_page_email_grant_change_requires_authorization_revision(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    monkeypatch.setattr(remote_access, "_resource_acl_sync_configured", lambda _config: True)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *_args, **_kwargs: {
            "emails": ["guest@example.com"],
            "changed": True,
        },
    )

    with pytest.raises(RuntimeError, match="show_page_email_access_invalid_response"):
        remote_access.replace_show_page_authorized_emails(
            "session-one",
            ["guest@example.com"],
            config,
        )


def test_remote_org_dock_temporarily_bypasses_show_page_acl(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()
    owner = _organization_context("owner-1", instance_role="owner")
    member = _organization_context("member-1")
    admin = _organization_context(
        "admin-1",
        group_ids=frozenset({"group-sales"}),
        organization_role="admin",
        instance_role="owner",
    )
    api.pin_dock_show_page("ses-public", user_context=member)
    api.pin_dock_show_page("ses-scope", user_context=admin)
    for session_id in ("ses-private", "ses-public", "ses-scope"):
        api.pin_dock_show_page(session_id, user_context=owner)

    dock = api.get_dock(user_context=member)["dock"]
    visible_ids = {pin["session_id"] for pin in dock["pins"]}
    assert visible_ids == {"ses-private", "ses-public", "ses-scope"}
    assert "show:ses-private" in dock["order"]
    api.pin_dock_show_page("ses-private", user_context=member)
    api.unpin_dock_show_page("ses-public", user_context=member)
    api.set_dock_order([], user_context=member)

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(
            config,
            subject="member-1",
            groups=["group-engineering"],
            instance_role="owner",
        ),
        domain="alex.avibe.bot",
    )
    response = client.delete(
        "/api/dock/pins/ses-public",
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert response.status_code == 200
    assert {pin["session_id"] for pin in response.get_json()["dock"]["pins"]} == {
        "ses-private",
        "ses-scope",
    }


def test_remote_admin_dock_order_preserves_hidden_private_pins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()
    owner = _organization_context("owner-1", instance_role="owner")
    admin = _organization_context("admin-1", organization_role="admin", instance_role="owner")
    for session_id in ("ses-private", "ses-public", "ses-scope"):
        api.pin_dock_show_page(session_id, user_context=owner)

    visible = api.get_dock(user_context=admin)["dock"]
    known = [
        "files",
        "terminal",
        "editor",
        "library",
        *(f"show:{pin['session_id']}" for pin in visible["pins"]),
    ]
    submitted = ["show:ses-scope", "show:ses-public", "files"]
    updated = api.set_dock_order(submitted, known=known, user_context=admin)["dock"]

    assert updated["order"] == submitted
    owner_dock = api.get_dock(user_context=owner)["dock"]
    assert "show:ses-private" not in owner_dock["order"]
    assert owner_dock["order"] == submitted


def test_untrusted_dock_context_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()
    owner = _organization_context("owner-1", instance_role="owner")
    api.pin_dock_show_page("ses-public", user_context=owner)

    unresolved = resource_access_service.ResourceUserContext()
    dock = api.get_dock(user_context=unresolved)["dock"]

    assert dock["pins"] == []
    assert "show:ses-public" not in dock["order"]
    with pytest.raises(ShowPageError, match="Show Page access is not permitted"):
        api.unpin_dock_show_page("ses-public", user_context=unresolved)
    with pytest.raises(DockError, match="unknown id"):
        api.set_dock_order(
            [*BUILTIN_DOCK_IDS, "show:ses-public"],
            user_context=unresolved,
        )


def test_remote_member_can_archive_session_with_another_owners_page_temporarily(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    ensure_sqlite_state()
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as connection:
            project = projects_service.create_project(connection, str(project_dir))
            session_id = sessions_service.create_session(
                connection,
                scope_id=project["scope_id"],
                agent_backend="claude",
            )["id"]

        store = ShowPageStore()
        try:
            store.ensure(session_id)
            with store.engine.begin() as connection:
                resource_access_service.ensure_resource_policy(
                    connection,
                    resource_kind="show_page",
                    resource_id=session_id,
                    organization_id="org-1",
                    owner_user_id="owner-1",
                    access_level="private",
                )
        finally:
            store.close()

        client = app.test_client()
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _organization_cookie(
                config,
                subject="member-1",
                groups=["group-engineering"],
                instance_role="editor",
            ),
            domain="alex.avibe.bot",
        )
        response = client.delete(
            f"/api/sessions/{session_id}",
            headers=csrf_headers(client, "https://alex.avibe.bot"),
            base_url="https://alex.avibe.bot",
            environ_base=_remote_peer(),
        )

        assert response.status_code in {200, 204}
        with engine.connect() as connection:
            assert connection.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one() == "archived"
            assert connection.execute(
                select(show_pages.c.visibility).where(show_pages.c.session_id == session_id)
            ).scalar_one() == "offline"
    finally:
        engine.dispose()


def test_remote_org_show_annotation_media_temporarily_follows_open_page_policy(
    monkeypatch,
    tmp_path,
) -> None:

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    ensure_sqlite_state()
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as connection:
            project = projects_service.create_project(connection, str(project_dir))
            sessions = {
                access_level: sessions_service.create_session(
                    connection,
                    scope_id=project["scope_id"],
                    agent_backend="claude",
                )["id"]
                for access_level in ("private", "public")
            }
            project_access_service.apply_project_access_intent(
                connection,
                {
                    "project_id": project["id"],
                    "revision": 1,
                    "mode": "restricted",
                    "bindings": [],
                },
            )

        store = ShowPageStore()
        try:
            for access_level, session_id in sessions.items():
                store.ensure(session_id)
                with store.engine.begin() as connection:
                    resource_access_service.ensure_resource_policy(
                        connection,
                        resource_kind="show_page",
                        resource_id=session_id,
                        organization_id="org-1",
                        owner_user_id="owner-1",
                        access_level=access_level,
                    )
        finally:
            store.close()

        def _screenshot(name: str) -> str:
            path = tmp_path / f"{name}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            return str(path.resolve())

        with engine.begin() as connection:
            tokens = {
                f"{source}:{access_level}": media_service.register(
                    connection,
                    scope_id=project["scope_id"],
                    session_id=session_id,
                    kind="image",
                    source=source,
                    local_path=_screenshot(f"{source}-{access_level}"),
                )
                for source in ("show_annotation", "agent_reply")
                for access_level, session_id in sessions.items()
            }
            orphan_token = media_service.register(
                connection,
                scope_id=None,
                session_id=None,
                kind="image",
                source="show_annotation",
                local_path=_screenshot("orphan"),
            )

        client = app.test_client()
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            _organization_cookie(
                config,
                subject="member-1",
                groups=["group-engineering"],
                instance_role="viewer",
            ),
            domain="alex.avibe.bot",
        )

        def _media(token: str):
            return client.get(
                f"/api/media/{token}",
                base_url="https://alex.avibe.bot",
                environ_base=_remote_peer(),
            )

        # Show annotation screenshots are part of the temporarily open page.
        assert _media(tokens["show_annotation:private"]).status_code == 200
        assert _media(tokens["show_annotation:public"]).status_code == 200
        # An annotation with no page to check against cannot be authorized.
        assert _media(orphan_token).status_code == 404
        # Active Organization members have the temporary runtime bypass for
        # project/session media as well.
        assert _media(tokens["agent_reply:private"]).status_code == 200
        assert _media(tokens["agent_reply:public"]).status_code == 200
    finally:
        engine.dispose()
