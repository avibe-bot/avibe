from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import inbox_events
from core.session_titles import backfill_agent_session_title
from core.services import sessions as sessions_service
from storage import messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import scope_settings
from storage.settings_service import upsert_scope


def test_backfill_agent_session_title_uses_first_user_message_for_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(inbox_events.bus, "publish", lambda event_type, data: published.append((event_type, data)))

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_titles",
            now="2026-06-02T08:00:00Z",
        )
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=str(tmp_path),
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json="{}",
                created_at="2026-06-02T08:00:00Z",
                updated_at="2026-06-02T08:00:00Z",
            )
        )
        session = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            platform="avibe",
            author="user",
            source="user",
            message_type="user",
            text="  帮我\n实现 session title 回填  ",
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="title backfill done",
        )

    updated = backfill_agent_session_title(
        agent_session_id=session["id"],
        backend="claude",
        native_session_id="claude-native-1",
        working_path="/repo",
        fallback_first_user_message="fallback should not win",
    )

    assert updated is not None
    assert updated["title"] == "帮我 实现 sess"
    assert updated["metadata"]["title_source"] == "derived_first_prompt"
    assert [event_type for event_type, _data in published] == ["session.activity", "inbox.session.updated"]
    assert published[0] == (
        "session.activity",
        {
            "session_id": session["id"],
            "scope_id": scope_id,
            "event": "updated",
            "title": "帮我 实现 sess",
        },
    )
    assert published[1][1]["session_id"] == session["id"]
    assert published[1][1]["title"] == "帮我 实现 sess"
    assert published[1][1]["preview_text"] == "title backfill done"
