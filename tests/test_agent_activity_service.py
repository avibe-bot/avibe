"""Unit tests for turn-grouped agent activity (Chat Activity panel history read).

Covers the grouping contract in ``storage/agent_activity_service.py``:

* a turn with ≥1 activity row + a terminal reply → a ``done`` / ``failed`` group
  anchored at the terminal message,
* interim ``assistant`` rows and ``tool_call`` events are merged into one group
  ordered by PARSED timestamp (the two tables store different ISO precisions),
* a turn whose activity is followed by a NEW turn (no terminal) → ``interrupted``
  anchored at the next turn's opening message; a trailing one → anchor ``None``,
* a turn with no activity rows produces no group,
* Show-Page ``assistant`` marks (metadata.source='show_page') are not activity,
* detail mode returns the ordered rows; an unknown group id returns ``None``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select  # noqa: F401  (kept parallel to sibling tests)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import agent_activity_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, agent_sessions, messages
from storage.settings_service import upsert_scope


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_session(conn, *, session_id="ses_act"):
    scope_id = upsert_scope(
        conn, platform="avibe", scope_type="project", native_id="proj_act", now="2026-06-01T10:00:00Z"
    )
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
            agent_backend="claude",
            agent_variant="default",
            session_anchor=f"anchor_{session_id}",
            native_session_id="",
            status="active",
            metadata_json="{}",
            created_at="2026-06-01T10:00:00Z",
            updated_at="2026-06-01T10:00:00Z",
            last_active_at="2026-06-01T10:00:00Z",
        )
    )
    return scope_id


def _msg(conn, scope_id, session_id, *, mid, mtype, author, created_at, text="", source="agent", metadata=None):
    conn.execute(
        messages.insert().values(
            id=mid,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author=author,
            type=mtype,
            source=source,
            content_text=text,
            content_json="{}",
            metadata_json=json.dumps(metadata or {}),
            created_at=created_at,
            updated_at=created_at,
        )
    )


def _evt(conn, scope_id, session_id, *, eid, created_at, text):
    conn.execute(
        agent_events.insert().values(
            id=eid,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            event_type="tool_call",
            visibility="trace",
            content_text=text,
            content_json=json.dumps({"kind": "tool_call", "text": text}),
            metadata_json="{}",
            source="agent",
            created_at=created_at,
            updated_at=created_at,
        )
    )


def test_done_failed_interrupted_and_trailing_groups(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_act"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        # Turn 1 — done: user, assistant, tool_call, result.
        _msg(conn, scope, sid, mid="m_u1", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q1", source="user")
        _msg(conn, scope, sid, mid="m_a1", mtype="assistant", author="agent", created_at="2026-06-01T10:00:01.000000+00:00", text="thinking")
        _evt(conn, scope, sid, eid="e_t1", created_at="2026-06-01T10:00:02Z", text="🔧 `Bash` `{\"command\":\"ls\"}`")
        _msg(conn, scope, sid, mid="m_r1", mtype="result", author="agent", created_at="2026-06-01T10:00:03.000000+00:00", text="answer 1")
        # Turn 2 — no activity: user + result only → no group.
        _msg(conn, scope, sid, mid="m_u2", mtype="user", author="user", created_at="2026-06-01T10:01:00.000000+00:00", text="q2", source="user")
        _msg(conn, scope, sid, mid="m_r2", mtype="result", author="agent", created_at="2026-06-01T10:01:01.000000+00:00", text="answer 2")
        # Turn 3 — failed: user, tool_call, error.
        _msg(conn, scope, sid, mid="m_u3", mtype="user", author="user", created_at="2026-06-01T10:02:00.000000+00:00", text="q3", source="user")
        _evt(conn, scope, sid, eid="e_t3", created_at="2026-06-01T10:02:01Z", text="🔧 `Read` `{\"path\":\"x\"}`")
        _msg(conn, scope, sid, mid="m_er3", mtype="error", author="agent", created_at="2026-06-01T10:02:02.000000+00:00", text="boom")
        # Turn 4 — interrupted (no terminal), then Turn 5 opens.
        _msg(conn, scope, sid, mid="m_u4", mtype="user", author="user", created_at="2026-06-01T10:03:00.000000+00:00", text="q4", source="user")
        _msg(conn, scope, sid, mid="m_a4", mtype="assistant", author="agent", created_at="2026-06-01T10:03:01.000000+00:00", text="partial")
        # Turn 5 — trailing interrupted (activity, no terminal, end of session).
        _msg(conn, scope, sid, mid="m_u5", mtype="user", author="user", created_at="2026-06-01T10:04:00.000000+00:00", text="q5", source="user")
        _evt(conn, scope, sid, eid="e_t5", created_at="2026-06-01T10:04:01Z", text="🔧 `Bash` `{\"command\":\"sleep\"}`")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    groups = summary["groups"]
    # Turn 2 has no activity → excluded. So 4 groups: done, failed, interrupted, trailing.
    assert [g["status"] for g in groups] == ["done", "failed", "interrupted", "interrupted"]

    done = groups[0]
    assert done["anchor_message_id"] == "m_r1"
    assert done["steps"] == 2  # assistant + tool_call
    assert done["duration_ms"] == 3000  # 10:00:00 → 10:00:03 (turn start → terminal)

    failed = groups[1]
    assert failed["anchor_message_id"] == "m_er3"
    assert failed["steps"] == 1

    interrupted = groups[2]
    # Anchored at the NEXT turn's opening message (chip sits above it).
    assert interrupted["anchor_message_id"] == "m_u5"
    assert interrupted["steps"] == 1

    trailing = groups[3]
    assert trailing["anchor_message_id"] is None  # trails the transcript
    assert trailing["steps"] == 1

    # ``id`` is the first activity row's id (stable key for lazy detail).
    assert done["id"] == "m_a1"
    assert failed["id"] == "e_t3"


def test_rows_merge_across_tables_by_parsed_timestamp(isolated_state):
    """A ``tool_call`` at ``...:02Z`` (= .000000) precedes an ``assistant`` at
    ``...:02.500000+00:00`` in real time even though a raw string sort would
    order them the other way — the group rows must reflect parsed order."""
    engine = create_sqlite_engine()
    sid = "ses_merge"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q", source="user")
        # Insert assistant FIRST so a naive/stable insertion order would be wrong.
        _msg(conn, scope, sid, mid="m_a", mtype="assistant", author="agent", created_at="2026-06-01T10:00:02.500000+00:00", text="second")
        _evt(conn, scope, sid, eid="e_t", created_at="2026-06-01T10:00:02Z", text="first")
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:05.000000+00:00", text="done")

    with engine.connect() as conn:
        detail = agent_activity_service.get_turn_group(conn, session_id=sid, group_id="e_t")
    assert detail is not None
    assert detail["status"] == "done"
    assert detail["anchor_message_id"] == "m_r"
    assert [(r["kind"], r["text"]) for r in detail["rows"]] == [
        ("tool_call", "first"),
        ("assistant", "second"),
    ]


def test_show_page_assistant_marks_are_not_activity(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_sp"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q", source="user")
        _msg(
            conn, scope, sid, mid="m_sp", mtype="assistant", author="agent",
            created_at="2026-06-01T10:00:01.000000+00:00", text="show page mark",
            metadata={"source": "show_page"},
        )
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:02.000000+00:00", text="done")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    # The only ``assistant`` row is a Show-Page mark → no activity → no group.
    assert summary["groups"] == []


def test_get_turn_group_unknown_id_returns_none(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_none"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q", source="user")
        _evt(conn, scope, sid, eid="e_t", created_at="2026-06-01T10:00:01Z", text="tool")
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:02.000000+00:00", text="done")

    with engine.connect() as conn:
        assert agent_activity_service.get_turn_group(conn, session_id=sid, group_id="nope") is None
        found = agent_activity_service.get_turn_group(conn, session_id=sid, group_id="e_t")
    assert found is not None and found["steps"] == 1
