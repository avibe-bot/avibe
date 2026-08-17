"""Web UI admin Show Pages API: listing and orthogonal availability."""

from core.show_pages import ShowPageStore, ensure_show_page_dir
from tests.ui_server_test_helpers import _save_config, csrf_headers
from vibe import api
from vibe.ui_server import app


def _seed_session(
    session_id: str,
    *,
    title: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
) -> None:
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
                    agent_id=agent_id,
                    agent_name=agent_name,
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
    assert by_id["ses_titled"]["can_manage"] is True
    assert by_id["ses_titled"]["can_publish_public"] is True
    # IM-dispatch sessions persist title=None; the UI falls back to the id.
    assert by_id["ses_plain"]["title"] is None
    assert by_id["ses_plain"]["visibility"] == "private"
    updated_ats = [page["updated_at"] for page in result["pages"]]
    assert updated_ats == sorted(updated_ats, reverse=True)


def test_list_show_pages_preserves_archived_agent_display_name(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore
    from storage.models import agent_sessions

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    store = VibeAgentStore()
    try:
        original = store.create(name="pm", backend="claude")
        store.create(name="zz-fallback", backend="claude")
        _seed_session(
            "ses_archived_agent",
            agent_id=original.id,
            agent_name=original.name,
        )
        _set_visibility("ses_archived_agent", "private")

        archived = store.archive(original.name)
        assert archived is not None
        result = api.list_show_pages()

        page = next(item for item in result["pages"] if item["session_id"] == "ses_archived_agent")
        assert page["agent"] == "pm"
        with store.engine.connect() as conn:
            internal_name = conn.execute(
                agent_sessions.select()
                .with_only_columns(agent_sessions.c.agent_name)
                .where(agent_sessions.c.id == "ses_archived_agent")
            ).scalar_one()
        assert internal_name == archived.archived_name
    finally:
        store.close()


def test_set_show_page_availability_preserves_configured_access(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _seed_session("ses_x")
    _set_visibility("ses_x", "public")

    offline = api.set_show_page_availability("ses_x", True)
    assert offline["visibility"] == "offline"
    assert offline["offline"] is True
    assert offline["offline_at"]
    share_id = offline["share_id"]

    online = api.set_show_page_availability("ses_x", False)
    assert online["visibility"] == "public"
    assert online["offline"] is False
    assert online["share_id"] == share_id


def test_show_page_availability_route_rejects_non_boolean_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    client = app.test_client()
    response = client.post(
        "/api/show-pages/ses_x/availability",
        json={"offline": "yes"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_availability"


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
