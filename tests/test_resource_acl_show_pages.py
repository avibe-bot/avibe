from __future__ import annotations

import pytest

from core.show_pages import ShowPageError, ShowPageStore, public_url
from storage import resource_access_service
from tests.test_ui_remote_access_auth import _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access
from vibe.ui_server import app


def _organization_context(
    subject: str,
    *,
    group_ids: frozenset[str] | None = frozenset({"group-engineering"}),
) -> resource_access_service.ResourceUserContext:
    return resource_access_service.ResourceUserContext(
        subject=subject,
        email=f"{subject}@example.com",
        organization_id="org-1",
        organization_member_id=f"member-{subject}",
        organization_role="member",
        group_ids=group_ids,
        instance_access_source="organization_group",
        is_remote=True,
    )


def _organization_cookie(config, *, subject: str, groups: list[str] | None = None) -> str:
    claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": f"member-{subject}",
        "vibe_organization_role": "member",
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


def test_show_page_list_filters_private_public_scope_and_missing_group_context(monkeypatch, tmp_path) -> None:
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


def test_remote_show_page_list_and_direct_requests_enforce_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    store = _seed_show_pages_with_policies()
    store.close()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="member-1", groups=["group-engineering"]),
        domain="alex.avibe.bot",
    )

    catalog = client.get(
        "/api/show-pages",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    mutation = client.post(
        "/api/show-pages/ses-private/visibility",
        json={"visibility": "offline"},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    page = client.get(
        "/show/ses-private/",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert catalog.status_code == 200
    assert {item["session_id"] for item in catalog.get_json()["pages"]} == {"ses-public", "ses-scope"}
    assert mutation.status_code == 403
    assert mutation.get_json()["code"] == "resource_access_forbidden"
    assert page.status_code == 403


def test_remote_show_page_creation_defaults_private_and_org_public_does_not_share(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    owner_context = _organization_context("owner-1")
    store = ShowPageStore()
    try:
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


def test_show_page_scope_without_group_context_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = _seed_show_pages_with_policies()
    try:
        with pytest.raises(ShowPageError, match="Show Page access is not permitted") as excinfo:
            store.require_access("ses-scope", user_context=_organization_context("member-1", group_ids=None))
    finally:
        store.close()

    assert excinfo.value.code == "resource_access_forbidden"
