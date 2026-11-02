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
