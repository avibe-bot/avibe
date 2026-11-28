from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Engine, event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import inbox_events
from core.session_titles import backfill_agent_session_title, persist_and_publish_session_title
from core.services import sessions as sessions_service
from modules.agents.base import AgentRequest, BaseAgent
from modules.agents.native_sessions.service import AgentNativeSessionService
from modules.agents.native_sessions.types import BackendSessionTitle
from modules.im import MessageContext
from storage import messages_service
from storage.agent_session_rows import (
    create_agent_session_row,
    load_agent_session_metadata_entry,
    save_agent_session_metadata_entry,
)
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import scope_settings
from storage.settings_service import upsert_scope


class _TitleBackfillAgent(BaseAgent):
    name = "claude"

    async def handle_message(self, request: AgentRequest) -> None:
        return None


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


def test_request_title_fallback_uses_user_message_when_no_message_row(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_title_fallback",
            now="2026-07-26T08:00:00Z",
        )
        session = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")
        assert messages_service.first_user_text(conn, session["id"]) == ""

    user_message = "Investigate session title fallback\n\n[Audio Transcript: note.m4a]\nKeep the user's words"
    prepended_lines = [
        "[Current Time: 2026-07-26 16:00:00 UTC+08:00]",
        "[Alex<U123>]",
    ]
    prompt = "\n".join(
        [
            *prepended_lines,
            user_message,
            "",
            "[Attachment Download Errors]",
            "- report.pdf could not be downloaded",
        ]
    )
    captured: dict[str, str] = {}

    def _get_title(
        _service,
        *,
        working_path,
        agent,
        native_session_id,
        first_user_message="",
    ):
        captured["first_user_message"] = first_user_message
        return BackendSessionTitle(
            title="Clean title input",
            source="derived_first_prompt",
            confidence="low",
        )

    monkeypatch.setattr(AgentNativeSessionService, "get_title", _get_title)
    controller = SimpleNamespace(
        config=SimpleNamespace(),
        im_client=SimpleNamespace(),
        settings_manager=SimpleNamespace(sessions=None),
    )
    request = AgentRequest(
        context=MessageContext(
            user_id="U123",
            channel_id=session["id"],
            platform="avibe",
            platform_specific={"agent_session_id": session["id"]},
        ),
        message=prompt,
        user_message=user_message,
        working_path="/repo",
        base_session_id=session["id"],
        composite_session_id=f"{session['id']}:/repo",
        session_key=f"avibe::{scope_id}",
    )

    async def _run() -> None:
        _TitleBackfillAgent(controller)._maybe_backfill_session_title(request, "claude-native-2")
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(_run())

    assert captured["first_user_message"] == user_message
    assert not set(prepended_lines) & set(captured["first_user_message"].splitlines())


def test_agent_session_metadata_entry_roundtrip_and_pk_query_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    missing_db = tmp_path / "does-not-exist.db"
    assert load_agent_session_metadata_entry("ses_any", "selection", db_path=missing_db) is None
    assert save_agent_session_metadata_entry("ses_any", "selection", {"effort": "high"}, db_path=missing_db) is False
    assert not missing_db.exists()

    empty_db = tmp_path / "empty-schema.db"
    sqlite3.connect(empty_db).close()
    assert load_agent_session_metadata_entry("ses_any", "selection", db_path=empty_db) is None
    assert save_agent_session_metadata_entry("ses_any", "selection", {"effort": "high"}, db_path=empty_db) is False

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_meta_entry",
                now="2026-11-28T00:00:00Z",
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="anchor_meta_entry",
                agent_backend="antigravity",
                workdir=str(tmp_path),
                metadata={"existing": "keep"},
            )
    finally:
        engine.dispose()

    assert load_agent_session_metadata_entry("   ", "selection") is None
    assert load_agent_session_metadata_entry(session_id, "") is None
    assert save_agent_session_metadata_entry("   ", "selection", {"a": 1}) is False
    assert save_agent_session_metadata_entry(session_id, "", {"a": 1}) is False
    assert load_agent_session_metadata_entry("ses_missing", "selection") is None
    assert save_agent_session_metadata_entry("ses_missing", "selection", {"a": 1}) is False

    executed: list[tuple[str, Any]] = []

    def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
        upper = statement.lstrip().upper()
        if "AGENT_SESSIONS" in upper and "WHERE 1 = 0" not in upper:
            executed.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        payload = {"native_session_id": "ag-1", "default_efforts": {"m1": "medium"}, "current_effort": "high"}
        assert save_agent_session_metadata_entry(session_id, "antigravity_selection", payload) is True
        assert load_agent_session_metadata_entry(session_id, "antigravity_selection") == payload
        assert load_agent_session_metadata_entry(session_id, "existing") == "keep"

        before_noop = len(executed)
        assert save_agent_session_metadata_entry(session_id, "antigravity_selection", payload) is True
        noop_statements = [stmt.lstrip().upper() for stmt, _params in executed[before_noop:]]
        assert any(stmt.startswith("SELECT ") for stmt in noop_statements)
        assert not any(stmt.startswith("UPDATE ") for stmt in noop_statements)
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    select_stmts = [(stmt, params) for stmt, params in executed if stmt.lstrip().upper().startswith("SELECT ")]
    update_stmts = [(stmt, params) for stmt, params in executed if stmt.lstrip().upper().startswith("UPDATE ")]
    assert select_stmts and update_stmts

    plan_engine = create_sqlite_engine(read_only=True)
    try:
        with plan_engine.connect() as conn:
            for statement, parameters in (select_stmts[0], update_stmts[0]):
                plan = " ".join(
                    str(row[3]) for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
                )
                assert "SEARCH agent_sessions USING" in plan, plan
                assert ("sqlite_autoindex_agent_sessions_1 (id=?)" in plan) or ("PRIMARY KEY" in plan), plan
                assert "SCAN " not in plan and "TEMP B-TREE" not in plan, plan
    finally:
        plan_engine.dispose()


def test_persist_and_publish_session_title(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    missing_db = tmp_path / "missing-titles.db"
    assert (
        persist_and_publish_session_title(
            agent_session_id="ses_any",
            backend="antigravity",
            native_session_id="ag-1",
            title="Live title",
            db_path=missing_db,
        )
        is None
    )
    assert not missing_db.exists()

    ensure_sqlite_state()
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(inbox_events.bus, "publish", lambda event_type, data: published.append((event_type, data)))

    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_live_title",
                now="2026-11-28T00:00:00Z",
            )
            session = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="antigravity")
            messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=session["id"],
                platform="avibe",
                author="agent",
                source="agent",
                message_type="result",
                text="live title preview",
            )
    finally:
        engine.dispose()

    assert (
        persist_and_publish_session_title(
            agent_session_id=session["id"],
            backend="antigravity",
            native_session_id="ag-live-1",
            title="   ",
        )
        is None
    )
    assert published == []

    updated = persist_and_publish_session_title(
        agent_session_id=session["id"],
        backend="antigravity",
        native_session_id="ag-live-1",
        title="Live Antigravity Title",
    )
    assert updated is not None
    assert updated["title"] == "Live Antigravity Title"
    assert updated["metadata"]["title_source"] == "backend"
    assert updated["metadata"]["title_backend"] == "antigravity"
    assert updated["metadata"]["title_confidence"] == "high"
    assert updated["metadata"]["title_native_session_id"] == "ag-live-1"
    assert [event_type for event_type, _data in published] == ["session.activity", "inbox.session.updated"]
    assert published[0] == (
        "session.activity",
        {
            "session_id": session["id"],
            "scope_id": scope_id,
            "event": "updated",
            "title": "Live Antigravity Title",
        },
    )
    assert published[1][1]["session_id"] == session["id"]
    assert published[1][1]["title"] == "Live Antigravity Title"
    assert published[1][1]["preview_text"] == "live title preview"

    published.clear()
    second = persist_and_publish_session_title(
        agent_session_id=session["id"],
        backend="antigravity",
        native_session_id="ag-live-1",
        title="Should Not Overwrite",
    )
    assert second is None
    assert published == []
