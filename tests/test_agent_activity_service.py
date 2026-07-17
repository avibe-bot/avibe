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
    assert done["anchor_message_id"] == "m_r1"  # own terminal
    assert done["anchor_position"] == "before"  # chip hugs the reply from above
    assert done["open"] is False
    assert done["steps"] == 2  # assistant + tool_call
    assert done["duration_ms"] == 3000  # 10:00:00 → 10:00:03 (turn start → terminal)

    failed = groups[1]
    assert failed["anchor_message_id"] == "m_er3"  # own terminal
    assert failed["anchor_position"] == "before"
    assert failed["open"] is False
    assert failed["steps"] == 1

    interrupted = groups[2]
    # Anchored AFTER its OWN trigger (m_u4), NOT the next turn's opener — never a
    # future message. It is not the last turn, so not ``open``.
    assert interrupted["anchor_message_id"] == "m_u4"
    assert interrupted["anchor_position"] == "after"
    assert interrupted["open"] is False
    assert interrupted["steps"] == 1

    trailing = groups[3]
    # The last un-terminated turn: anchored AFTER its OWN trigger (m_u5), never null
    # / the tail; ``open`` so the frontend may promote it into the live card.
    assert trailing["anchor_message_id"] == "m_u5"
    assert trailing["anchor_position"] == "after"
    assert trailing["open"] is True
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


def test_same_second_tool_call_stays_in_completed_turn(isolated_state):
    """A fast turn emits a tool_call and its terminal result in the SAME whole
    second (both tables store second precision). The phase tiebreak must keep the
    tool call inside the done group, not orphan it after the terminal."""
    engine = create_sqlite_engine()
    sid = "ses_ss"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q", source="user")
        # tool_call and result both at :05Z — the tie the fix resolves.
        _evt(conn, scope, sid, eid="e_t", created_at="2026-06-01T10:00:05Z", text="🔧 `Bash`")
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:05Z", text="answer")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    groups = summary["groups"]
    assert len(groups) == 1
    assert groups[0]["status"] == "done"
    assert groups[0]["anchor_message_id"] == "m_r"
    assert groups[0]["steps"] == 1  # the tool_call belongs to THIS turn, not orphaned


def _clock_id(prefix: str, micros: int) -> str:
    """Realistic row id: ``<pfx>_<15-hex microsecond epoch><uuid8>`` (matches
    messages_service / agent_events_service), so the grouping decodes emission order."""
    return f"{prefix}_{micros:015x}{'0' * 8}"


def test_back_to_back_turns_same_second_keep_done_status(isolated_state):
    """Turn A's result and turn B's opener land in the SAME whole second. Ordering
    by the id microsecond keeps A ``done`` (its result precedes B's opener) instead
    of flipping A to ``interrupted`` anchored on B's prompt."""
    engine = create_sqlite_engine()
    sid = "ses_b2b"
    base = 1_800_000_000_000_000  # arbitrary microsecond epoch
    result_id = _clock_id("msg", base + 2_000_000)
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid=_clock_id("msg", base), mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q1", source="user")
        _evt(conn, scope, sid, eid=_clock_id("evt", base + 1_000_000), created_at="2026-06-01T10:00:01Z", text="🔧 `Bash`")
        # A's result and B's opener both at :02Z; the result was emitted ~1ms first.
        _msg(conn, scope, sid, mid=result_id, mtype="result", author="agent", created_at="2026-06-01T10:00:02Z", text="answer 1")
        _msg(conn, scope, sid, mid=_clock_id("msg", base + 2_001_000), mtype="user", author="user", created_at="2026-06-01T10:00:02Z", text="q2", source="user")
        _evt(conn, scope, sid, eid=_clock_id("evt", base + 3_000_000), created_at="2026-06-01T10:00:03Z", text="🔧 `Read`")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    groups = summary["groups"]
    # A stays done (result precedes B's opener); B trails with its own tool call.
    assert [g["status"] for g in groups] == ["done", "interrupted"]
    assert groups[0]["anchor_message_id"] == result_id
    assert groups[0]["steps"] == 1  # the Bash tool_call belongs to A, not orphaned to B


def test_events_before_message_window_are_dropped(isolated_state):
    """An event that predates the scanned message window (its turn boundary was not
    fetched) must not be grouped, or it would anchor a bogus interrupted chip to the
    first visible turn. Here the only event predates the oldest message."""
    engine = create_sqlite_engine()
    sid = "ses_win"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        # Event an hour before the oldest scanned message → outside the window.
        _evt(conn, scope, sid, eid="e_old", created_at="2026-06-01T09:00:00Z", text="🔧 `Bash`")
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q", source="user")
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:01Z", text="answer")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    # The stale pre-window event is dropped; the user+result turn has no activity.
    assert summary["groups"] == []


def test_same_second_pre_window_event_dropped_by_id(isolated_state):
    """A tool event in the SAME whole second as the oldest scanned message, but
    emitted BEFORE it (smaller microsecond id), is outside the window and must be
    dropped — the whole-second cutoff alone would wrongly keep it."""
    engine = create_sqlite_engine()
    sid = "ses_winid"
    base = 1_800_000_000_000_000
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        # Event emitted a millisecond BEFORE the oldest message, same second.
        _evt(conn, scope, sid, eid=_clock_id("evt", base + 1_000), created_at="2026-06-01T10:00:00Z", text="🔧 `Bash`")
        _msg(conn, scope, sid, mid=_clock_id("msg", base + 5_000), mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q", source="user")
        _msg(conn, scope, sid, mid=_clock_id("msg", base + 10_000), mtype="result", author="agent", created_at="2026-06-01T10:00:00Z", text="answer")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    # The pre-window event is dropped → the user+result turn has no activity.
    assert summary["groups"] == []


def test_duration_measured_from_turn_opener(isolated_state):
    """The chip duration spans the turn opener → terminal (what the history endpoint
    reports), not first-activity → terminal — so live and reloaded chips agree."""
    engine = create_sqlite_engine()
    sid = "ses_dur"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q", source="user")
        _evt(conn, scope, sid, eid="e_t", created_at="2026-06-01T10:00:05Z", text="🔧 `Bash`")  # first activity 5s in
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:10Z", text="answer")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    assert len(summary["groups"]) == 1
    # 10s opener→terminal, NOT 5s first-activity→terminal.
    assert summary["groups"][0]["duration_ms"] == 10_000


def test_show_page_user_marks_do_not_split_a_turn(isolated_state):
    """A Show-Page annotation persists as a user row mid-turn; it must NOT be treated
    as a turn opener (which would mark the turn interrupted and orphan its terminal)."""
    engine = create_sqlite_engine()
    sid = "ses_spuser"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q", source="user")
        _evt(conn, scope, sid, eid="e_1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash`")
        # A Show-Page user mark lands WHILE the turn is still producing activity.
        _msg(
            conn, scope, sid, mid="m_sp", mtype="user", author="user",
            created_at="2026-06-01T10:00:02Z", text="pinned an element", source="user",
            metadata={"source": "show_page"},
        )
        _evt(conn, scope, sid, eid="e_2", created_at="2026-06-01T10:00:03Z", text="🔧 `Read`")
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:04Z", text="answer")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    groups = summary["groups"]
    # One done group with BOTH tool calls — the Show-Page mark did not split it.
    assert [g["status"] for g in groups] == ["done"]
    assert groups[0]["anchor_message_id"] == "m_r"
    assert groups[0]["steps"] == 2


def test_interrupted_followed_by_running_turn_anchors_to_own_trigger(isolated_state):
    """(a) Interrupted turn followed by a still-RUNNING next turn — the P1 repro.
    The interrupted chip must anchor AFTER its OWN trigger (above the newer turn),
    NEVER forward to the next turn's opener nor to the tail."""
    engine = create_sqlite_engine()
    sid = "ses_ia"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u1", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q1", source="user")
        _evt(conn, scope, sid, eid="e_1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash`")
        _msg(conn, scope, sid, mid="m_r1", mtype="result", author="agent", created_at="2026-06-01T10:00:02Z", text="a1")
        _msg(conn, scope, sid, mid="m_u2", mtype="user", author="user", created_at="2026-06-01T10:01:00Z", text="q2", source="user")
        _evt(conn, scope, sid, eid="e_2", created_at="2026-06-01T10:01:01Z", text="🔧 `Read`")  # turn2 (interrupted)
        _msg(conn, scope, sid, mid="m_u3", mtype="user", author="user", created_at="2026-06-01T10:02:00Z", text="q3", source="user")
        _evt(conn, scope, sid, eid="e_3", created_at="2026-06-01T10:02:01Z", text="🔧 `Bash`")  # turn3 running (no terminal)

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert [g["status"] for g in groups] == ["done", "interrupted", "interrupted"]
    turn1, turn2, turn3 = groups
    assert turn1["anchor_message_id"] == "m_r1" and turn1["anchor_position"] == "before"
    # turn2 anchors to its OWN trigger (m_u2) — above m_u3, never the future opener.
    assert turn2["anchor_message_id"] == "m_u2"
    assert turn2["anchor_position"] == "after"
    assert turn2["open"] is False
    # turn3 is the open (running-candidate) turn: own trigger, open (→ live card).
    assert turn3["anchor_message_id"] == "m_u3" and turn3["open"] is True


def test_interrupted_followed_by_completed_turn_ordering(isolated_state):
    """(b) Interrupted turn followed by a COMPLETED next turn — same ordering:
    interrupted after its trigger, the next turn done at its own reply."""
    engine = create_sqlite_engine()
    sid = "ses_ib"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u2", mtype="user", author="user", created_at="2026-06-01T10:01:00Z", text="q2", source="user")
        _evt(conn, scope, sid, eid="e_2", created_at="2026-06-01T10:01:01Z", text="🔧 `Read`")  # interrupted turn
        _msg(conn, scope, sid, mid="m_u3", mtype="user", author="user", created_at="2026-06-01T10:02:00Z", text="q3", source="user")
        _evt(conn, scope, sid, eid="e_3", created_at="2026-06-01T10:02:01Z", text="🔧 `Bash`")
        _msg(conn, scope, sid, mid="m_r3", mtype="result", author="agent", created_at="2026-06-01T10:02:02Z", text="a3")

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert [g["status"] for g in groups] == ["interrupted", "done"]
    interrupted, done = groups
    assert interrupted["anchor_message_id"] == "m_u2"
    assert interrupted["anchor_position"] == "after"
    assert interrupted["open"] is False
    assert done["anchor_message_id"] == "m_r3"
    assert done["anchor_position"] == "before"
    assert done["open"] is False


def test_failed_turn_anchors_to_its_own_terminal(isolated_state):
    """(c) A failed turn anchors to its OWN error terminal (rendered before it),
    never forward — even when a later turn follows."""
    engine = create_sqlite_engine()
    sid = "ses_ic"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u1", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q1", source="user")
        _evt(conn, scope, sid, eid="e_1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash`")
        _msg(conn, scope, sid, mid="m_err", mtype="error", author="agent", created_at="2026-06-01T10:00:02Z", text="boom")
        _msg(conn, scope, sid, mid="m_u2", mtype="user", author="user", created_at="2026-06-01T10:01:00Z", text="q2", source="user")

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert [g["status"] for g in groups] == ["failed"]
    assert groups[0]["anchor_message_id"] == "m_err"  # own terminal, not the later m_u2
    assert groups[0]["anchor_position"] == "before"
    assert groups[0]["open"] is False


def test_send_while_busy_override_interrupt_anchors_backward(isolated_state):
    """(d) Owner's exact repro: quick-reply send then a second send OVERRIDES the
    running turn. The overridden turn is interrupted; its chip must anchor after its
    OWN trigger (above the override message + the new running turn), never forward to
    the override message nor to the tail. Microsecond-encoded ids under tight
    (same-second) override timing."""
    engine = create_sqlite_engine()
    sid = "ses_id"
    base = 1_800_000_000_000_000
    trig2 = _clock_id("msg", base + 0)  # turn2 trigger (quick-reply send)
    over3 = _clock_id("msg", base + 2_000_000)  # override send → turn3 trigger
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid=trig2, mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q2", source="user")
        _evt(conn, scope, sid, eid=_clock_id("evt", base + 1_000_000), created_at="2026-06-01T10:00:00Z", text="🔧 `Bash`")  # turn2 activity
        _msg(conn, scope, sid, mid=over3, mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q3 override", source="user")
        _evt(conn, scope, sid, eid=_clock_id("evt", base + 3_000_000), created_at="2026-06-01T10:00:01Z", text="🔧 `Read`")  # turn3 running

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert [g["status"] for g in groups] == ["interrupted", "interrupted"]
    turn2, turn3 = groups
    # The overridden turn anchors to its OWN trigger, NOT the override message, NOT null.
    assert turn2["anchor_message_id"] == trig2
    assert turn2["anchor_message_id"] != over3
    assert turn2["anchor_position"] == "after"
    assert turn2["open"] is False
    assert turn3["anchor_message_id"] == over3 and turn3["open"] is True


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
