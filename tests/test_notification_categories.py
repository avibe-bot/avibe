"""Server-side Inbox category and account notification preference contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


pytestmark = pytest.mark.real_web_push_sender


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_session(conn, session_id: str, *, scope_id: str | None, title: str) -> None:
    from storage.messages_service import _utc_now_iso
    from storage.models import agent_sessions

    now = _utc_now_iso()
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
            agent_backend="vibey",
            agent_variant="default",
            session_anchor=f"anchor_{session_id}",
            native_session_id="",
            title=title,
            status="active",
            visibility="foreground",
            metadata_json="{}",
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )


def _seed_result(conn, *, scope_id: str | None, session_id: str, text: str, user_key: str = "local") -> dict:
    from storage import messages_service

    messages_service.append(
        conn,
        scope_id=scope_id,
        session_id=session_id,
        platform="avibe",
        author="user",
        source="user",
        message_type="user",
        text="Please finish",
        metadata={"_web_push_user_key": user_key},
    )
    return messages_service.append(
        conn,
        scope_id=scope_id,
        session_id=session_id,
        platform="avibe",
        author="agent",
        source="agent",
        message_type="result",
        text=text,
        read_at=None,
    )


def test_preferences_default_and_unknown_state_fields_survive_write(isolated_state):
    from core.chat_discovery import get_state_meta, set_state_meta
    from core.notification_preferences import (
        get_notification_preferences,
        preferences_key,
        set_notification_preferences,
    )

    assert {
        key: get_notification_preferences("local")[key]
        for key in ("enabled", "bots", "sessions")
    } == {"enabled": True, "bots": True, "sessions": True}

    set_state_meta(
        preferences_key("local"),
        {"enabled": False, "bots": True, "muted_bots": ["researcher"], "future": {"x": 1}},
    )
    stored = set_notification_preferences("local", {"sessions": False})

    assert stored["enabled"] is False
    assert stored["bots"] is True
    assert stored["sessions"] is False
    assert stored["muted_bots"] == ["researcher"]
    assert stored["future"] == {"x": 1}
    assert get_state_meta(preferences_key("local")) == stored


def test_preferences_api_is_account_scoped_and_csrf_protected(isolated_state):
    client = app.test_client()

    default = client.get("/api/notifications/preferences", base_url="http://127.0.0.1:5123")
    assert default.status_code == 200
    assert default.get_json() == {
        "ok": True,
        "preferences": {"enabled": True, "bots": True, "sessions": True},
    }

    forbidden = client.put(
        "/api/notifications/preferences",
        json={"sessions": False},
        base_url="http://127.0.0.1:5123",
    )
    assert forbidden.status_code == 403

    headers = csrf_headers(client, "http://127.0.0.1:5123")
    updated = client.put(
        "/api/notifications/preferences",
        json={"sessions": False},
        headers=headers,
        base_url="http://127.0.0.1:5123",
    )
    assert updated.status_code == 200
    assert updated.get_json()["preferences"] == {
        "enabled": True,
        "bots": True,
        "sessions": False,
    }


def test_preference_sse_event_is_visible_only_to_the_same_user():
    from vibe.authorization import AuthorizationContext, instance_owner_context
    from vibe.ui_server import _workbench_event_visible_to_context

    payload = json.dumps(
        {
            "type": "notifications.preferences.updated",
            "data": {
                "user_key": "remote:alice",
                "preferences": {"enabled": True, "bots": False, "sessions": True},
            },
        }
    )
    alice = AuthorizationContext(instance_role="member", is_remote=True, subject="alice")
    bob = AuthorizationContext(instance_role="member", is_remote=True, subject="bob")
    assert _workbench_event_visible_to_context(alice, "notifications.preferences.updated", payload)
    assert not _workbench_event_visible_to_context(bob, "notifications.preferences.updated", payload)
    assert not _workbench_event_visible_to_context(instance_owner_context(), "notifications.preferences.updated", payload)


def test_inbox_categories_filters_and_badge_total_follow_registry_and_preferences(isolated_state):
    from core.bots import BotService
    from core.notification_preferences import set_notification_preferences
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project

    with BotService() as service:
        bot = service.ensure_main_bot()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project_dir = isolated_state / "inbox-category-project"
        project_dir.mkdir()
        project = create_project(conn, str(project_dir), display_name="Project")
        ordinary_id = "ses_ordinary_category"
        _seed_session(conn, ordinary_id, scope_id=project["scope_id"], title="Ordinary")
        _seed_result(conn, scope_id=None, session_id=bot.session_id, text="Bot reply")
        _seed_result(conn, scope_id=project["scope_id"], session_id=ordinary_id, text="Session reply")

    client = app.test_client()
    all_rows = client.get("/api/inbox?platform=avibe", base_url="http://127.0.0.1:5123").get_json()
    assert {row["category"] for row in all_rows["sessions"]} == {"bot", "session"}
    assert all_rows["unread_by_category"] == {"bot": 1, "session": 1}
    assert all_rows["badge_total"] == 2

    bots = client.get("/api/inbox?platform=avibe&category=bot", base_url="http://127.0.0.1:5123").get_json()
    sessions = client.get("/api/inbox?platform=avibe&category=session", base_url="http://127.0.0.1:5123").get_json()
    assert [row["session_id"] for row in bots["sessions"]] == [bot.session_id]
    assert [row["session_id"] for row in sessions["sessions"]] == [ordinary_id]

    set_notification_preferences("local", {"bots": False})
    muted = client.get("/api/inbox?platform=avibe", base_url="http://127.0.0.1:5123").get_json()
    assert muted["unread_by_category"] == {"bot": 1, "session": 1}
    assert muted["unread_total"] == 2
    assert muted["badge_total"] == 1
    engine.dispose()


def test_member_cannot_turn_owner_bot_session_into_a_session_category(isolated_state):
    from core.bots import BotService, hidden_bot_session_ids
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from vibe.authorization import AuthorizationContext, instance_owner_context

    with BotService() as service:
        bot = service.ensure_main_bot()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project_dir = isolated_state / "member-category-project"
        project_dir.mkdir()
        project = create_project(conn, str(project_dir), display_name="Project")
        ordinary_id = "ses_member_category"
        _seed_session(conn, ordinary_id, scope_id=project["scope_id"], title="Ordinary")
        _seed_result(conn, scope_id=None, session_id=bot.session_id, text="Bot reply")
        _seed_result(conn, scope_id=project["scope_id"], session_id=ordinary_id, text="Session reply")

    member = AuthorizationContext(instance_role="member", is_remote=True, subject="member-1")
    with engine.connect() as conn:
        owner_rows = messages_service.list_inbox_sessions(
            conn,
            hidden_session_ids=hidden_bot_session_ids(conn, instance_owner_context()),
        )["sessions"]
        member_rows = messages_service.list_inbox_sessions(
            conn,
            hidden_session_ids=hidden_bot_session_ids(conn, member),
        )["sessions"]

    assert {row["category"] for row in owner_rows} == {"bot", "session"}
    assert [row["session_id"] for row in member_rows] == [ordinary_id]
    assert member_rows[0]["category"] == "session"
    engine.dispose()


@pytest.mark.parametrize("updates", [{"bots": False}, {"enabled": False}])
def test_push_gating_records_muted_and_badge_total_excludes_quiet_category(
    monkeypatch,
    isolated_state,
    updates,
):
    from core.bots import BotService
    from core.notification_preferences import set_notification_preferences
    from core import web_push_notifications
    from storage import messages_service, web_push_service
    from storage.db import create_sqlite_engine

    with BotService() as service:
        bot = service.ensure_main_bot()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        message = _seed_result(conn, scope_id=None, session_id=bot.session_id, text="Bot reply")
        web_push_service.upsert_subscription(
            conn,
            user_key="local",
            payload={
                "endpoint": "https://push.example.test/muted-bot",
                "keys": {"p256dh": "p256dh", "auth": "auth"},
            },
        )
    set_notification_preferences("local", updates)
    sent: list[dict] = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("core.web_push.send_web_push", lambda *, subscription, payload: sent.append(payload))

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Bot", "body": "Bot reply", "session_id": bot.session_id, "message_id": message["id"]}
    )

    assert sent == []
    recent = web_push_notifications.recent_delivery_dispositions(user_key="local")
    assert recent[0]["owners"]["local"]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_MUTED
    engine.dispose()


def test_service_worker_badge_prefers_badge_total_and_falls_back_to_unread_total():
    worker = (Path(__file__).resolve().parents[1] / "ui" / "public" / "push-sw.js").read_text()
    assert "payload?.badge_total ?? payload?.unread_total" in worker


def test_push_badge_uses_loud_category_total(monkeypatch, isolated_state):
    from core.bots import BotService
    from core.notification_preferences import set_notification_preferences
    from core import web_push_notifications
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project

    with BotService() as service:
        bot = service.ensure_main_bot()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project_dir = isolated_state / "badge-category-project"
        project_dir.mkdir()
        project = create_project(conn, str(project_dir), display_name="Project")
        ordinary_id = "ses_badge_category"
        _seed_session(conn, ordinary_id, scope_id=project["scope_id"], title="Ordinary")
        _seed_result(conn, scope_id=None, session_id=bot.session_id, text="Bot reply")
        _seed_result(conn, scope_id=project["scope_id"], session_id=ordinary_id, text="Session reply")
    set_notification_preferences("local", {"bots": False})

    with engine.connect() as conn:
        assert web_push_notifications._badge_count_for_user_key(
            conn,
            user_key="local",
            contexts={},
        ) == 1
    engine.dispose()
