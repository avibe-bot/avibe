from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hosted_email_routes_and_clients_are_removed() -> None:
    production_sources = [
        ROOT / "vibe" / "api.py",
        ROOT / "vibe" / "authorization.py",
        ROOT / "vibe" / "remote_access.py",
        ROOT / "vibe" / "ui_server.py",
        ROOT / "tests" / "test_instance_authorization.py",
    ]
    forbidden = {
        "authorized-emails",
        "get_show_page_authorized_emails",
        "replace_show_page_authorized_emails",
        "/visibility",
        "/rotate-share",
        "/share-id",
        "set_show_page_visibility",
        "rotate_show_page_share",
        "set_show_page_share_id",
        "public_link_enabled",
    }

    for source_path in production_sources:
        source = source_path.read_text(encoding="utf-8")
        for symbol in forbidden:
            assert symbol not in source, f"legacy hosted-email surface remains in {source_path}: {symbol}"


def test_legacy_show_page_frontend_surface_is_removed() -> None:
    component_dir = ROOT / "ui" / "src" / "components" / "workbench"
    assert not (component_dir / "ShowPageEmailAccessEditor.tsx").exists()
    assert not (component_dir / "ShowPageShareIdField.tsx").exists()

    forbidden = {
        "ShowPageEmailAccessEditor",
        "ShowPageShareIdField",
        "getShowPageAuthorizedEmails",
        "replaceShowPageAuthorizedEmails",
        "setShowPageVisibility",
        "rotateShowPageShare",
        "setShowPageShareId",
        "public_link_enabled",
    }
    frontend_sources = [
        path
        for path in (ROOT / "ui" / "src").rglob("*")
        if path.suffix in {".ts", ".tsx"} and ".test." not in path.name
    ]
    for source_path in frontend_sources:
        source = source_path.read_text(encoding="utf-8")
        for symbol in forbidden:
            assert symbol not in source, f"legacy frontend surface remains in {source_path}: {symbol}"


def test_legacy_show_page_copy_is_removed_from_both_locales() -> None:
    removed_show_pages_keys = {
        "visibilityLabel",
        "visibilityOffline",
        "shareLink",
        "rotate",
        "rotateHint",
    }
    removed_chat_keys = {
        "rotateLink",
        "visibilityPublic",
        "visibilityPrivate",
        "publicDesc",
        "privateDesc",
        "emailAccess",
        "emailAccessDesc",
        "emailAccessEmpty",
        "emailAccessLimit",
        "applyEmailAccess",
        "loadingEmailAccess",
        "emailAccessUnavailable",
        "emailAccessError",
        "publicLink",
        "publicLinkOnDesc",
        "publicLinkOffDesc",
        "publicLinkOwnerOnly",
        "publicLinkOffline",
        "customLink",
        "offlineNote",
        "publicUnavailable",
        "sharingOffline",
    }

    for locale in ("en", "zh"):
        payload = json.loads(
            (ROOT / "ui" / "src" / "i18n" / f"{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        assert removed_show_pages_keys.isdisjoint(payload["showPages"])
        assert removed_chat_keys.isdisjoint(payload["chat"]["showPage"])
        assert "show_page_email_access_transient" not in payload["errors"]


def test_legacy_show_page_ui_plans_are_removed() -> None:
    plans_dir = ROOT / "docs" / "plans"
    assert not (plans_dir / "show-page-email-access.md").exists()

    forbidden = {
        "ShowPageEmailAccessEditor",
        "ShowPageShareIdField",
        "getShowPageAuthorizedEmails",
        "replaceShowPageAuthorizedEmails",
        "setShowPageVisibility",
        "rotateShowPageShare",
        "setShowPageShareId",
        "visibility private/public/offline",
        "Share visibility / rotate / share-id",
    }
    for source_path in plans_dir.glob("*.md"):
        source = source_path.read_text(encoding="utf-8")
        for symbol in forbidden:
            assert symbol not in source, f"legacy Show Page UI plan remains in {source_path}: {symbol}"
