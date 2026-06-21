"""Web UI admin Show Pages API: listing (with title join), visibility, rotate."""

import pytest

from core.show_pages import ShowPageError, ShowPageStore, ensure_show_page_dir
from tests.test_ui_remote_access_auth import _save_config
from vibe import api
from vibe.ui_server import app


def _seed_session(session_id: str, *, title: str | None = None) -> None:
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="slack", scope_type="channel", native_id=f"chan_{session_id}", now=now)
            conn.execute(
                agent_sessions.insert().values(
                    id=session_id,
                    scope_id=scope_id,
                    agent_backend="claude",
                    agent_variant="default",
                    session_anchor="anchor_" + session_id,
                    native_session_id="",
                    title=title,
                    status="active",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                    last_active_at=now,
                )
            )
    finally:
        engine.dispose()


def _set_visibility(session_id: str, visibility: str) -> None:
    ensure_show_page_dir(session_id)
    store = ShowPageStore()
    try:
        store.update_visibility(session_id, visibility)
    finally:
        store.close()


def test_list_show_pages_orders_newest_first_and_joins_title(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_titled", title="Q2 funnel dashboard")
    _seed_session("ses_plain")
    _set_visibility("ses_titled", "public")
    _set_visibility("ses_plain", "private")

    result = api.list_show_pages()

    assert result["ok"] is True
    assert result["count"] == 2
    assert "url_available" in result
    by_id = {page["session_id"]: page for page in result["pages"]}
    assert by_id["ses_titled"]["title"] == "Q2 funnel dashboard"
    assert by_id["ses_titled"]["platform"] == "slack"
    assert by_id["ses_titled"]["agent"] == "Claude"
    assert by_id["ses_titled"]["visibility"] == "public"
    assert by_id["ses_titled"]["share_id"]
    # IM-dispatch sessions persist title=None; the UI falls back to the id.
    assert by_id["ses_plain"]["title"] is None
    assert by_id["ses_plain"]["visibility"] == "private"
    updated_ats = [page["updated_at"] for page in result["pages"]]
    assert updated_ats == sorted(updated_ats, reverse=True)


def test_set_show_page_visibility_public_then_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_x")
    _set_visibility("ses_x", "private")

    public = api.set_show_page_visibility("ses_x", "public")
    assert public["ok"] is True
    assert public["visibility"] == "public"
    assert public["share_id"]

    offline = api.set_show_page_visibility("ses_x", "offline")
    assert offline["visibility"] == "offline"
    assert offline["offline"] is True
    assert offline["offline_at"]


def test_set_show_page_visibility_rejects_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_x")

    with pytest.raises(ShowPageError) as excinfo:
        api.set_show_page_visibility("ses_x", "bogus")
    assert excinfo.value.code == "invalid_visibility"


def test_rotate_share_requires_public_and_revokes_previous(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_x")
    _set_visibility("ses_x", "private")

    with pytest.raises(ShowPageError) as excinfo:
        api.rotate_show_page_share("ses_x")
    assert excinfo.value.code == "not_public"

    public = api.set_show_page_visibility("ses_x", "public")
    rotated = api.rotate_show_page_share("ses_x")
    assert rotated["ok"] is True
    assert rotated["share_id"]
    assert rotated["share_id"] != public["share_id"]
    assert rotated["previous_share_id"] == public["share_id"]


def test_show_pages_list_route_returns_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_route", title="Release notes preview")
    _set_visibility("ses_route", "public")

    response = app.test_client().get("/api/show-pages", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    page = next(item for item in body["pages"] if item["session_id"] == "ses_route")
    assert page["title"] == "Release notes preview"
    assert page["visibility"] == "public"
