"""The server's Inbox badge and the Web page's live badge follow one rule.

The page splits its live unread map by the Owner's Bot list and sums the loud
categories itself (``ui/src/lib/notificationPreferences.ts``); the push worker
and the desktop host read the server's ``badge_total``. Both sides run the same
fixture, so the sidebar number and the app icon cannot drift apart, including
across a roster change (``removed_bot``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.notification_categories import split_unread_counts
from core.notification_preferences import badge_total
from storage.importer import ensure_sqlite_state

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "ui/src/lib/notificationBadge.fixture.json").read_text(encoding="utf-8")
)
REMOVED = FIXTURE["removed_bot"]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_server_badge_matches_the_shared_fixture(case):
    counts = split_unread_counts(FIXTURE["unread_by_session"], bot_session_ids=case["bot_session_ids"])

    assert counts == case["unread_by_category"]
    assert badge_total(counts, case["preferences"]) == case["badge_total"]


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_ordinary_session(conn, session_id: str, scope_id: str) -> None:
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
            title=session_id,
            status="active",
            visibility="foreground",
            metadata_json="{}",
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )


def test_removing_a_bot_leaves_its_session_out_of_the_servers_map_and_badge(isolated_state):
    from core.bots import BotService
    from core.notification_preferences import set_notification_preferences
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from vibe.ui_server import app

    with BotService() as service:
        service.ensure_main_bot()
        for handle in REMOVED["bots"]:
            service.create_bot(display_name=handle.title(), handle=handle)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        project_dir = isolated_state / "project"
        project_dir.mkdir()
        project = create_project(conn, str(project_dir), display_name="Project")
        for session_id, replies in REMOVED["agent_replies"].items():
            ordinary = not session_id.startswith("ses_")
            if ordinary:
                _seed_ordinary_session(conn, session_id, project["scope_id"])
            for index in range(replies):
                messages_service.append(
                    conn,
                    scope_id=project["scope_id"] if ordinary else None,
                    session_id=session_id,
                    platform="avibe",
                    author="agent",
                    source="agent",
                    message_type="result",
                    text=f"reply {index}",
                )
    engine.dispose()

    client = app.test_client()

    def inbox() -> dict:
        return client.get("/api/inbox?platform=avibe", base_url="http://127.0.0.1:5123").get_json()

    assert inbox()["unread_by_session"] == REMOVED["before"]["unread_by_session"]
    with BotService() as service:
        service.remove_bot(REMOVED["removed"])
    for case in REMOVED["cases"]:
        set_notification_preferences("local", case["preferences"])
        after = inbox()
        assert after["unread_by_session"] == REMOVED["after"]["unread_by_session"], case["name"]
        assert after["unread_by_category"] == case["unread_by_category"], case["name"]
        assert after["badge_total"] == case["badge_total"], case["name"]
        # The page's own computation on what it is then handed agrees.
        counts = split_unread_counts(after["unread_by_session"], bot_session_ids=REMOVED["after"]["bot_session_ids"])
        assert badge_total(counts, case["preferences"]) == case["badge_total"], case["name"]


def test_workspace_notice_realtime_api_and_push_badges_agree(isolated_state, monkeypatch):
    """The real notice writer publishes unread cards; API/read events share that total.

    Storage tests cannot reach mirror publication or HTTP badge projection. The
    old predicate publishes zero-unread cards and misses the notices in /inbox.
    """
    from core.message_mirror import persist_agent_message
    from core.web_push_notifications import _badge_count_for_user_key
    from modules.im import MessageContext
    from storage import messages_service
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID, resolve_workspace_notice_session
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from vibe.sse_broker import broker
    from vibe.ui_server import app
    from tests.ui_server_test_helpers import csrf_headers

    engine = create_sqlite_engine()
    (isolated_state / "project").mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(isolated_state / "project"), display_name="Project")
        _seed_ordinary_session(conn, "ordinary", project["scope_id"])
        messages_service.append(
            conn, scope_id=project["scope_id"], session_id="ordinary", platform="avibe",
            author="agent", message_type="result", text="One ordinary reply",
        )
        resolve_workspace_notice_session(conn)
    published = []
    read_events = []
    monkeypatch.setattr("core.message_mirror._publish_event", lambda topic, payload: published.append((topic, payload)))
    monkeypatch.setattr(broker, "publish", lambda topic, payload: read_events.append((topic, payload)))
    # Observe scheduling, without creating threads or contacting Push providers.
    push_attempts = []
    monkeypatch.setattr("core.web_push_notifications.maybe_notify_inbox_message", lambda row, card: push_attempts.append((row, card)))
    monkeypatch.setattr("core.web_push_notifications.schedule_badge_reconciliation", lambda *_args: None)
    context = MessageContext(
        user_id="harness", channel_id=WORKSPACE_NOTICE_SESSION_ID, platform="avibe",
        platform_specific={"agent_session_id": WORKSPACE_NOTICE_SESSION_ID},
    )
    for index in range(3):
        persist_agent_message(context, "notify", f"Workspace notice {index}")
    cards = [payload for topic, payload in published if topic == "inbox.session.updated"]
    assert [card["unread_count"] for card in cards] == [1, 2, 3]
    assert len(push_attempts) == 3  # Same pre-existing scheduling path, not a new Push.

    client = app.test_client()
    payload = client.get("/api/inbox?platform=avibe&unread=true", base_url="http://127.0.0.1:5123").get_json()
    assert payload["unread_by_session"] == {"ordinary": 1, WORKSPACE_NOTICE_SESSION_ID: 3}
    assert payload["unread_total"] == payload["badge_total"] == 4
    with engine.connect() as conn:
        assert _badge_count_for_user_key(conn, user_key="local", contexts={}) == 4
    response = client.post(
        f"/api/sessions/{WORKSPACE_NOTICE_SESSION_ID}/mark-read", json={},
        base_url="http://127.0.0.1:5123", headers=csrf_headers(client, "http://127.0.0.1:5123"),
    )
    assert response.status_code == 200
    assert response.get_json()["updated"] == 3
    assert response.get_json()["badge_total"] == 1
    [event] = [payload for topic, payload in read_events if topic == "inbox.unread.changed"]
    assert event["unread_by_session"] == {"ordinary": 1}
    assert event["badge_total"] == 1
    with engine.connect() as conn:
        assert _badge_count_for_user_key(conn, user_key="local", contexts={}) == 1
