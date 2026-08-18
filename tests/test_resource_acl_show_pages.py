from __future__ import annotations

import pytest
from sqlalchemy import select

from config import paths
from core.dock_store import BUILTIN_DOCK_IDS, DockError
from core.show_pages import ShowPageError, ShowPageStore
from storage import media_service, project_access_service, projects_service, resource_access_service
from storage import workbench_sessions_service as sessions_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, show_pages
from tests.ui_server_test_helpers import _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, internal_client, permissions, remote_access, ui_server
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
        "vibe_instance_authorization_revision": 0,
    }
    if groups is not None:
        claims["vibe_group_ids"] = groups
    return remote_access.make_session_cookie(
        config,
        f"{subject}@example.com",
        subject,
        session_claims=claims,
    )


def _paired_config(tmp_path, *, instance_kind: str):
    config = _save_config(tmp_path)
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.example"
    cloud.instance_secret = "device-secret"
    cloud.instance_kind = instance_kind
    config.save()
    return config


def _ownership(
    mode: str,
    *,
    organization_id: str | None = None,
    source: str = "live",
) -> dict:
    return {
        "mode": mode,
        "instance_id": "inst_123",
        "organization_id": organization_id,
        "source": source,
    }


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


def test_show_page_catalog_follows_instance_role_and_show_page_acl(monkeypatch, tmp_path) -> None:
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
    assert member_ids == {"ses-public", "ses-scope"}
    assert no_group_ids == {"ses-public"}


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
def test_remote_show_page_access_does_not_bypass_acl(
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
        "/api/show-pages/ses-public/availability",
        json={"offline": True},
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
    expected = {"ses-private", "ses-public", "ses-scope"} if instance_role == "owner" else {
        "ses-public", "ses-scope"
    }
    pages = catalog.get_json()["pages"]
    assert {item["session_id"] for item in pages} == expected
    if instance_role == "owner":
        assert all(item.get("path") for item in pages)
        assert all(item["can_manage"] for item in pages)
        assert all(item["can_publish_public"] for item in pages)
    else:
        assert all("path" not in item for item in pages)
        assert all(not item["can_manage"] for item in pages)
        assert all(not item["can_publish_public"] for item in pages)
    assert mutation.status_code == (200 if instance_role == "owner" else 403)
    assert page.status_code == (200 if instance_role == "owner" else 302)


def test_show_page_creation_uses_exact_instance_organization_without_request_claims(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_config(tmp_path, instance_kind="organization")
    monkeypatch.setattr(
        permissions,
        "resolve_current_instance_ownership",
        lambda: _ownership("organization", organization_id="org-1"),
    )
    owner_context = _organization_context("owner-1", instance_role="owner")
    store = ShowPageStore()
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        with store.engine.begin() as connection:
            project = projects_service.create_project(connection, str(project_path))
            session = sessions_service.create_session(
                connection, scope_id=project["scope_id"], agent_backend="codex"
            )
            project_access_service.apply_project_access_intent(
                connection,
                {
                    "project_id": project["id"],
                    "revision": 1,
                    "mode": "restricted",
                    "bindings": [{
                        "principal_kind": "email",
                        "principal_value": "member-1@example.com",
                        "access_role": "editor",
                    }],
                },
            )
        created = store.ensure(session["id"])
        assert created.session_id == session["id"]
        with store.engine.connect() as connection:
            created_policy = resource_access_service.get_resource_policy(
                "show_page", created.session_id, connection=connection
            )
        assert created_policy is not None
        assert created_policy["organization_id"] == "org-1"
        assert created_policy["owner_user_id"] is None
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
            assert updated.share_id
    finally:
        store.close()


def test_show_page_scope_without_group_context_is_denied(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    try:
        with pytest.raises(ShowPageError):
            store.require_access("ses-scope", user_context=_organization_context("member-1", group_ids=None))
    finally:
        store.close()

    # Missing group claims fail closed for scoped pages.


def test_organization_admin_without_instance_owner_role_cannot_manage_pages(monkeypatch, tmp_path) -> None:
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
        with pytest.raises(ShowPageError):
            store.require_access("ses-private", user_context=admin)
        with pytest.raises(ShowPageError):
            store.update_visibility("ses-private", "private", user_context=admin)

        republished = store.update_visibility("ses-private", "public", user_context=owner)
        rotated, previous_share_id = store.rotate_share("ses-private", user_context=owner)
        custom, rotated_share_id = store.set_share_id("ses-private", "owner-link", user_context=owner)

        assert public_page.visibility == "public"
        assert republished.visibility == "public"
        assert previous_share_id == republished.share_id
        assert rotated_share_id == rotated.share_id
        assert custom.share_id == "owner-link"

        with pytest.raises(ShowPageError):
            store.require_access("ses-scope", user_context=admin)
    finally:
        store.close()


def test_access_only_manager_availability_response_does_not_expose_page_payload(monkeypatch, tmp_path) -> None:
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

    with pytest.raises(ShowPageError):
        api.set_show_page_availability("ses-private", True)


def test_excluded_scoped_owner_availability_mutations_do_not_expose_page_payload(monkeypatch, tmp_path) -> None:
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

    offline = api.set_show_page_availability("ses-scope", True)
    online = api.set_show_page_availability("ses-scope", False)

    assert offline["ok"] is True
    assert online["ok"] is True


def test_remote_show_page_editor_can_control_sharing_without_instance_owner_role(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    page_owner = _organization_context("owner-1", instance_role="editor")
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


def test_remote_show_page_viewer_mutations_are_instance_role_denied(monkeypatch, tmp_path) -> None:
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
    availability = client.post(
        f"/api/show-pages/{session_id}/availability",
        json={"offline": True},
        **request_options,
    )
    settings = client.post(
        f"/api/show-pages/{session_id}/access-settings/read",
        json={"page_id": session_id},
        **request_options,
    )
    applied = client.post(
        f"/api/show-pages/{session_id}/access-settings/apply",
        json={
            "page_id": session_id,
            "expected_revision": 0,
            "target_access_mode": "public",
            "target_share_id": "viewer-owner-link",
            "target_emails": [],
        },
        **request_options,
    )

    assert ensured.status_code == 403
    assert availability.status_code == 403
    assert settings.status_code == 403
    assert applied.status_code == 403


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

    with pytest.raises(ShowPageError):
        api.get_show_page_access("ses-scope")


def test_existing_show_page_is_adopted_idempotently_without_changing_link_access(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-legacy")
        applied = store.apply_access(
            "ses-legacy",
            expected_revision=0,
            target_access_mode="limited",
            target_share_id="stable-link",
            target_emails=["guest@example.com"],
        )
        store.set_offline("ses-legacy", True)
        before = store.get_access("ses-legacy")
        assert applied.status == "applied"
        _paired_config(tmp_path, instance_kind="organization")
        monkeypatch.setattr(
            permissions,
            "resolve_current_instance_ownership",
            lambda: _ownership("organization", organization_id="org-1"),
        )
        page = store.ensure("ses-legacy")
        store.ensure("ses-legacy")
        with store.engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page",
                page.session_id,
                connection=connection,
            )
        after = store.get_access("ses-legacy")
        assert policy is not None
        assert policy["organization_id"] == "org-1"
        assert policy["access_level"] == "private"
        assert policy["policy_revision"] == 0
        assert before == after
        assert page.offline
    finally:
        store.close()


def test_legacy_show_page_policy_reconciliation_requires_existing_page_or_project_access(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    project_path = tmp_path / "project"
    project_path.mkdir()
    editor = _organization_context("editor-1", instance_role="editor")
    try:
        with store.engine.begin() as connection:
            project = projects_service.create_project(connection, str(project_path))
            session_id = sessions_service.create_session(
                connection,
                scope_id=project["scope_id"],
                agent_backend="codex",
            )["id"]
        store.ensure(session_id)
        _paired_config(tmp_path, instance_kind="organization")
        monkeypatch.setattr(
            permissions,
            "resolve_current_instance_ownership",
            lambda: _ownership("organization", organization_id="org-1"),
        )
        with store.engine.begin() as connection:
            project_access_service.apply_project_access_intent(
                connection,
                {
                    "project_id": project["id"],
                    "revision": 1,
                    "mode": "restricted",
                    "bindings": [],
                },
            )

        with pytest.raises(ShowPageError, match="Show Page access is not permitted"):
            api.get_show_page_access(session_id, user_context=editor)
        with store.engine.connect() as connection:
            assert resource_access_service.get_resource_policy(
                "show_page",
                session_id,
                connection=connection,
            ) is None

        with store.engine.begin() as connection:
            project_access_service.apply_project_access_intent(
                connection,
                {
                    "project_id": project["id"],
                    "revision": 2,
                    "mode": "restricted",
                    "bindings": [{
                        "principal_kind": "email",
                        "principal_value": "editor-1@example.com",
                        "access_role": "editor",
                    }],
                },
            )

        response = api.get_show_page_access(session_id, user_context=editor)
        with store.engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page",
                session_id,
                connection=connection,
            )
        assert response["ownership_status"] == "created"
        assert response["can_use"] is True
        assert response["can_manage"] is True
        assert policy is not None
        assert policy["owner_user_id"] == "editor-1"
        assert policy["organization_id"] == "org-1"
    finally:
        store.close()


def test_show_page_access_api_distinguishes_personal_and_organization_modes(monkeypatch, tmp_path) -> None:
    personal_home = tmp_path / "personal"
    monkeypatch.setenv("AVIBE_HOME", str(personal_home))
    _paired_config(personal_home, instance_kind="personal")
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
        "ownership_status": "unchanged",
        "instance_id": "inst_123",
        "organization_id": None,
        "policy_organization_id": None,
        "access_level": "private",
        "group_ids": [],
        "policy_revision": 0,
        "last_applied_control_plane_revision": None,
        "can_use": True,
        "can_manage": True,
        "can_publish_public": True,
    }

    organization_home = tmp_path / "organization"
    monkeypatch.setenv("AVIBE_HOME", str(organization_home))
    config = _paired_config(organization_home, instance_kind="organization")
    monkeypatch.setattr(
        permissions,
        "resolve_current_instance_ownership",
        lambda: _ownership("organization", organization_id="org-1"),
    )
    store = ShowPageStore()
    try:
        store.ensure("ses-organization")
        with store.engine.begin() as connection:
            resource_access_service.apply_control_plane_intent(
                connection,
                organization_id="org-1",
                resource_kind="show_page",
                resource_id="ses-organization",
                revision=4,
                access_level="scope",
                group_ids=["group-engineering"],
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
        "ownership_status": "unchanged",
        "instance_id": "inst_123",
        "organization_id": "org-1",
        "policy_organization_id": "org-1",
        "access_level": "scope",
        "group_ids": ["group-engineering"],
        "policy_revision": 4,
        "last_applied_control_plane_revision": 4,
        "can_use": True,
        "can_manage": True,
        "can_publish_public": True,
    }


def test_null_organization_adoption_preserves_policy_and_show_access(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-adopt")
        applied = store.apply_access(
            "ses-adopt",
            expected_revision=0,
            target_access_mode="limited",
            target_share_id="adopt-link",
            target_emails=["guest@example.com"],
        )
        with store.engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id="ses-adopt",
                organization_id=None,
                owner_user_id="owner-1",
                owner_email="owner@example.com",
                access_level="private",
                policy_revision=4,
                last_applied_control_plane_revision=3,
            )
        before_access = store.get_access("ses-adopt")
        _paired_config(tmp_path, instance_kind="organization")
        monkeypatch.setattr(
            permissions,
            "resolve_current_instance_ownership",
            lambda: _ownership("organization", organization_id="org-1"),
        )

        store.ensure("ses-adopt")
        store.ensure("ses-adopt")

        with store.engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page",
                "ses-adopt",
                connection=connection,
            )
        assert applied.status == "applied"
        assert store.get_access("ses-adopt") == before_access
        assert policy is not None
        assert policy["organization_id"] == "org-1"
        assert policy["owner_user_id"] == "owner-1"
        assert policy["owner_email"] == "owner@example.com"
        assert policy["access_level"] == "private"
        assert policy["group_ids"] == []
        assert policy["policy_revision"] == 4
        assert policy["last_applied_control_plane_revision"] == 3
    finally:
        store.close()


def test_same_organization_retry_preserves_acl_revision_and_groups(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_config(tmp_path, instance_kind="organization")
    monkeypatch.setattr(
        permissions,
        "resolve_current_instance_ownership",
        lambda: _ownership("organization", organization_id="org-1"),
    )
    store = ShowPageStore()
    try:
        store.ensure(
            "ses-same-org",
            user_context=_organization_context("owner-1", instance_role="owner"),
        )
        with store.engine.begin() as connection:
            resource_access_service.apply_control_plane_intent(
                connection,
                organization_id="org-1",
                resource_kind="show_page",
                resource_id="ses-same-org",
                revision=7,
                access_level="scope",
                group_ids=["group-engineering"],
            )
            before = resource_access_service.get_resource_policy(
                "show_page", "ses-same-org", connection=connection
            )

        store.ensure(
            "ses-same-org",
            user_context=_organization_context("owner-1", instance_role="owner"),
        )

        with store.engine.connect() as connection:
            after = resource_access_service.get_resource_policy(
                "show_page", "ses-same-org", connection=connection
            )
        assert after == before
    finally:
        store.close()


def test_cross_organization_policy_conflict_fails_closed_without_breaking_link_guest(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-conflict")
        with store.engine.begin() as connection:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id="ses-conflict",
                organization_id="org-other",
                owner_user_id="owner-other",
                access_level="public",
                policy_revision=5,
                last_applied_control_plane_revision=5,
            )
        _paired_config(tmp_path, instance_kind="organization")
        monkeypatch.setattr(
            permissions,
            "resolve_current_instance_ownership",
            lambda: _ownership("organization", organization_id="org-1"),
        )
        store.ensure("ses-conflict")

        response = api.get_show_page_access("ses-conflict")
        with store.engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page", "ses-conflict", connection=connection
            )
            link_guest = resource_access_service.can_use_resource(
                resource_access_service.ResourceUserContext(
                    subject="guest-1",
                    email="guest@example.com",
                    instance_role="viewer",
                    instance_access_source="show_page_email",
                    show_page_id="ses-conflict",
                    is_remote=True,
                ),
                "show_page",
                "ses-conflict",
                connection=connection,
            )
        with pytest.raises(ShowPageError):
            store.require_access(
                "ses-conflict",
                user_context=_organization_context("member-1"),
            )
        assert store.list(user_context=_organization_context("member-1")) == []
        assert response["ownership_status"] == "conflict"
        assert response["organization_id"] == "org-1"
        assert response["policy_organization_id"] == "org-other"
        assert policy is not None
        assert policy["organization_id"] == "org-other"
        assert policy["access_level"] == "public"
        assert policy["policy_revision"] == 5
        assert link_guest is True
    finally:
        store.close()


def test_organization_pending_is_stable_private_and_does_not_block_local_creation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_config(tmp_path, instance_kind="organization")
    monkeypatch.setattr(
        permissions,
        "get_current_permissions",
        lambda _config=None: (_ for _ in ()).throw(
            permissions.PermissionsUnavailableError("permissions_backend_unavailable")
        ),
    )
    store = ShowPageStore()
    try:
        created = store.ensure("ses-pending")
        response = api.get_show_page_access("ses-pending")
        with store.engine.connect() as connection:
            policy = resource_access_service.get_resource_policy(
                "show_page", "ses-pending", connection=connection
            )
        assert created.session_id == "ses-pending"
        assert policy is None
        assert response["mode"] == "organization_pending"
        assert response["ownership_status"] == "pending"
        assert response["access_level"] == "private"
        assert response["organization_id"] is None
        assert response["can_use"] is True
    finally:
        store.close()


def test_last_known_organization_binding_is_exact_instance_scoped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path, instance_kind="organization")
    store = ShowPageStore()
    try:
        with store.engine.begin() as connection:
            resource_access_service.remember_show_page_instance_ownership(
                connection,
                _ownership("organization", organization_id="org-1"),
            )
        monkeypatch.setattr(
            permissions,
            "get_current_permissions",
            lambda _config=None: (_ for _ in ()).throw(
                permissions.PermissionsUnavailableError("permissions_backend_unavailable")
            ),
        )
        offline = permissions.resolve_current_instance_ownership()
        assert offline == _ownership(
            "organization",
            organization_id="org-1",
            source="stored",
        )

        config.remote_access.vibe_cloud.instance_id = "inst-new"
        config.save()
        repaired = permissions.resolve_current_instance_ownership()
        assert repaired["mode"] == "organization_pending"
        assert repaired["instance_id"] == "inst-new"
        assert repaired["organization_id"] is None
    finally:
        store.close()


def test_show_page_access_api_reports_missing_page_as_definitive_denial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    response = app.test_client().get("/api/show-pages/ses-missing/access")

    assert response.status_code == 404
    assert response.get_json()["code"] == "show_page_not_found"


def _show_access_payload(page_id: str, *, revision: int = 0) -> dict:
    return {
        "page_id": page_id,
        "access_mode": "private",
        "share_id": "stable-link",
        "revision": revision,
        "normalized_emails": [],
    }


def test_show_access_owner_read_and_apply_use_controller_ipc(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-access-settings")
    finally:
        store.close()
    calls: list[tuple[str, dict]] = []

    async def _read(payload):
        calls.append(("read", payload))
        return {
            "status_code": 200,
            "body": {"show_access": _show_access_payload("ses-access-settings")},
        }

    async def _apply(payload):
        calls.append(("apply", payload))
        return {
            "status_code": 200,
            "body": {
                "status": "applied",
                "show_access": {
                    **_show_access_payload("ses-access-settings", revision=1),
                    "access_mode": "limited",
                    "normalized_emails": ["guest@example.com"],
                },
            },
        }

    monkeypatch.setattr(internal_client, "show_access_settings_read", _read)
    monkeypatch.setattr(internal_client, "show_access_apply", _apply)
    client = app.test_client()
    headers = csrf_headers(client)
    loaded = client.post(
        "/api/show-pages/ses-access-settings/access-settings/read",
        json={"page_id": "ses-access-settings"},
        headers=headers,
    )
    request_payload = {
        "page_id": "ses-access-settings",
        "expected_revision": 0,
        "target_access_mode": "limited",
        "target_share_id": "stable-link",
        "target_emails": ["guest@example.com"],
    }
    applied = client.post(
        "/api/show-pages/ses-access-settings/access-settings/apply",
        json=request_payload,
        headers=headers,
    )

    assert loaded.status_code == 200
    assert loaded.headers["Cache-Control"] == "private, no-store"
    assert loaded.headers["Vary"] == "Cookie"
    assert loaded.get_json()["show_access"]["page_id"] == "ses-access-settings"
    assert applied.status_code == 200
    assert applied.get_json()["status"] == "applied"
    assert calls == [
        ("read", {"page_id": "ses-access-settings"}),
        ("apply", request_payload),
    ]


def test_show_access_route_identity_mismatch_is_rejected_before_ipc(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    async def _unexpected(_payload):
        pytest.fail("identity mismatch must not reach controller IPC")

    monkeypatch.setattr(internal_client, "show_access_settings_read", _unexpected)
    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses-route/access-settings/read",
        json={"page_id": "ses-other"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "error": "show_access_page_identity_mismatch",
    }


def test_show_access_malformed_apply_is_rejected_before_ipc(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    async def _unexpected(_payload):
        pytest.fail("malformed request must not reach controller IPC")

    monkeypatch.setattr(internal_client, "show_access_apply", _unexpected)
    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses-route/access-settings/apply",
        json={
            "page_id": "ses-route",
            "expected_revision": True,
            "target_access_mode": "limited",
            "target_share_id": ["not-a-string"],
            "target_emails": ["guest@example.com"],
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_show_access_apply_request"


def test_show_access_non_owner_is_rejected_before_ipc(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    store.close()
    viewer = _organization_context("member-1", instance_role="viewer")
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda _value=None: viewer,
    )

    async def _unexpected(_payload):
        pytest.fail("unauthorized request must not reach controller IPC")

    monkeypatch.setattr(internal_client, "show_access_settings_read", _unexpected)
    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses-public/access-settings/read",
        json={"page_id": "ses-public"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "resource_access_forbidden"


def test_show_access_internal_identity_mismatch_hides_email_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-access-settings")
    finally:
        store.close()

    async def _mismatched(_payload):
        return {
            "status_code": 200,
            "body": {
                "show_access": {
                    **_show_access_payload("ses-other"),
                    "access_mode": "limited",
                    "normalized_emails": ["secret@example.com"],
                }
            },
        }

    monkeypatch.setattr(internal_client, "show_access_settings_read", _mismatched)
    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses-access-settings/access-settings/read",
        json={"page_id": "ses-access-settings"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 502
    assert response.get_json() == {
        "ok": False,
        "error": "show_access_internal_protocol_error",
    }
    assert "secret@example.com" not in response.text


def test_show_access_controller_unavailable_is_retryable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-access-settings")
    finally:
        store.close()

    async def _unavailable(_payload):
        raise internal_client.InternalServerUnavailable("missing controller socket")

    monkeypatch.setattr(internal_client, "show_access_settings_read", _unavailable)
    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses-access-settings/access-settings/read",
        json={"page_id": "ses-access-settings"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "show_access_controller_unavailable"


def test_show_access_conflict_returns_current_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ShowPageStore()
    try:
        store.ensure("ses-access-settings")
    finally:
        store.close()

    async def _conflict(_payload):
        return {
            "status_code": 200,
            "body": {
                "status": "conflict",
                "show_access": _show_access_payload("ses-access-settings", revision=3),
            },
        }

    monkeypatch.setattr(internal_client, "show_access_apply", _conflict)
    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses-access-settings/access-settings/apply",
        json={
            "page_id": "ses-access-settings",
            "expected_revision": 2,
            "target_access_mode": "public",
            "target_share_id": "stable-link",
            "target_emails": [],
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "conflict"
    assert response.get_json()["show_access"]["revision"] == 3


def test_remote_org_dock_requires_admin_owner_role(monkeypatch, tmp_path) -> None:
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
    # Show Page dock/pin stays reserved to owner/admin Organization roles
    # under the Resource ACL boundary (see #1343); a plain member is denied.
    with pytest.raises(ShowPageError):
        api.pin_dock_show_page("ses-public", user_context=member)
    api.pin_dock_show_page("ses-scope", user_context=admin)
    for session_id in ("ses-private", "ses-public", "ses-scope"):
        api.pin_dock_show_page(session_id, user_context=owner)

    dock = api.get_dock(user_context=member)["dock"]
    visible_ids = {pin["session_id"] for pin in dock["pins"]}
    assert visible_ids == {"ses-public", "ses-scope"}
    assert "show:ses-private" not in dock["order"]
    # Pinning additional pages (require_management) stays denied for a plain
    # member; unpin and reorder are also dock mutations and stay denied.
    with pytest.raises(ShowPageError):
        api.pin_dock_show_page("ses-private", user_context=member)
    with pytest.raises(ShowPageError):
        api.unpin_dock_show_page("ses-public", user_context=member)
    with pytest.raises(ShowPageError):
        api.set_dock_order([], user_context=member)

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
    response = client.delete(
        "/api/dock/pins/ses-public",
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    # A plain active Organization member is denied dock mutations under the
    # Resource ACL boundary (see #1343).
    assert response.status_code == 403


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


def test_remote_member_archive_is_denied_under_resource_acl(monkeypatch, tmp_path) -> None:
    from unittest.mock import AsyncMock

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

        archive_session = AsyncMock()
        monkeypatch.setattr(
            "vibe.internal_client.memory_archive_session",
            archive_session,
        )

        # Show Page archive of another owner's page stays reserved to
        # owner/admin Organization roles under the Resource ACL boundary
        # (see #1343); a plain active Organization member is denied.
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

        assert response.status_code in {403, 404}
        archive_session.assert_not_awaited()
        with engine.connect() as connection:
            assert connection.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one() != "archived"
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

        # Media access follows the associated Project and Show Page ACLs.
        assert _media(tokens["show_annotation:private"]).status_code == 404
        assert _media(tokens["show_annotation:public"]).status_code == 404
        # An annotation with no page to check against cannot be authorized.
        assert _media(orphan_token).status_code == 404
        assert _media(tokens["agent_reply:private"]).status_code == 404
        assert _media(tokens["agent_reply:public"]).status_code == 404
    finally:
        engine.dispose()
