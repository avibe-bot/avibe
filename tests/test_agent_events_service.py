"""The single-copy mirror optimization must not rewrite other event contracts."""

import json

import pytest
from sqlalchemy import select

from storage import agent_events_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events


@pytest.mark.parametrize(
    "event_type,visibility,content,expected_content",
    [
        ("tool_call", "context", {"version": 1}, {"version": 1, "text": "rendered"}),
        ("other", "trace", {"version": 1}, {"version": 1, "text": "rendered"}),
        ("tool_call", "trace", {"text": "explicit"}, {"text": "explicit"}),
    ],
)
def test_trace_text_optimization_preserves_other_bodies(
    tmp_path, monkeypatch, event_type, visibility, content, expected_content,
):
    """A broad append() change would alter explicit JSON or non-target events;
    mirror/API tests only cover tool-call trace rows, not those other callers."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            # This explicit dual-copy row stands for already-persisted data.
            old = agent_events_service.append(
                conn, scope_id=None, session_id=None, platform="avibe",
                event_type="tool_call", text="old", content={"text": "old"},
            )
            old_row = dict(conn.execute(select(agent_events)).mappings().one())
            new = agent_events_service.append(
                conn, scope_id=None, session_id=None, platform="avibe",
                event_type=event_type, visibility=visibility, text="rendered", content=content,
            )
            rows = {row["id"]: dict(row) for row in conn.execute(select(agent_events)).mappings()}
        assert rows[old["id"]] == old_row
        assert rows[new["id"]]["content_text"] == "rendered"
        assert json.loads(rows[new["id"]]["content_json"]) == expected_content
        assert new["text"] == "rendered"
    finally:
        engine.dispose()
