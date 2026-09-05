"""Unit tests for turn-grouped agent activity (Chat Activity panel history read).

Covers the grouping contract in ``storage/agent_activity_service.py``:

* a turn with ≥1 activity row + a terminal reply → a ``done`` / ``failed`` group
  anchored at the terminal message,
* interim ``assistant`` rows and ``tool_call`` events are merged into one group
  ordered by PARSED timestamp (the two tables store different ISO precisions),
* a turn whose activity is followed by a NEW turn (no terminal) → ``interrupted``
  anchored at the next turn's opening message; a trailing one → anchor ``None``,
* a turn with no activity rows produces no group,
* agent-authored annotation rows are not activity,
* detail mode returns the ordered rows; an unknown group id returns ``None``.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select  # noqa: F401  (kept parallel to sibling tests)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import agent_activity_service, message_deliveries, messages_service
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


def _evt(
    conn,
    scope_id,
    session_id,
    *,
    eid,
    created_at,
    text,
    event_type="tool_call",
    metadata=None,
    turn_id=None,
):
    conn.execute(
        agent_events.insert().values(
            id=eid,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            event_type=event_type,
            visibility="trace",
            content_text=text,
            content_json=json.dumps({"kind": "tool_call", "text": text}),
            metadata_json=json.dumps(metadata or {}),
            source="agent",
            turn_id=turn_id,
            created_at=created_at,
            updated_at=created_at,
        )
    )


def _accept_start(conn, turn_id: str) -> list[dict]:
    turn = message_deliveries.get_turn(conn, turn_id)
    assert turn is not None
    assert message_deliveries.bind_native_start(
        conn,
        turn_id,
        expected_version=int(turn["version"]),
        runtime_key=f"runtime:{turn_id}",
        runtime_turn_id=f"runtime-turn:{turn_id}",
        native_turn_id=f"native:{turn_id}",
    ) is not None
    return message_deliveries.materialize_start_acceptance(
        conn,
        turn_id=turn_id,
        evidence={"kind": "test_native_acceptance"},
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


def test_agent_annotation_marks_are_not_activity(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_sp"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q", source="user")
        _msg(
            conn, scope, sid, mid="m_sp",
            mtype=messages_service.ANNOTATION_TYPE, author="agent",
            created_at="2026-06-01T10:00:01.000000+00:00", text="show page mark",
            metadata={"source": "show_page"},
        )
        _msg(conn, scope, sid, mid="m_r", mtype="result", author="agent", created_at="2026-06-01T10:00:02.000000+00:00", text="done")

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)
    # The annotation is display-only, not an interim assistant activity row.
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


def test_migrated_terminal_uses_original_message_clock_for_same_second_order(
    isolated_state,
):
    engine = create_sqlite_engine()
    sid = "ses_legacy_terminal_order"
    base = int(
        datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        * 1_000_000
    )
    timestamp = "2026-06-01T10:00:00Z"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid=_clock_id("msg", base + 1_000),
            mtype="user",
            author="user",
            created_at=timestamp,
            text="q1",
            source="user",
        )
        _evt(
            conn,
            scope,
            sid,
            eid=_clock_id("evt", base + 2_000),
            created_at=timestamp,
            text="tool",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="evt_legacy_without_clock",
            created_at=timestamp,
            text="",
            event_type="silent_terminal",
            metadata={"legacy_message_id": _clock_id("msg", base + 3_000)},
        )
        _msg(
            conn,
            scope,
            sid,
            mid=_clock_id("msg", base + 4_000),
            mtype="user",
            author="user",
            created_at=timestamp,
            text="q2",
            source="user",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]
    assert groups[0]["status"] == "done"
    assert groups[0]["anchor_message_id"] == _clock_id("msg", base + 1_000)


def test_detail_exposes_the_same_order_contract_consumed_by_ui(isolated_state):
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "ui/src/lib/agentActivity.order.fixture.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    wire_keys = ("id", "kind", "text", "created_at", "order_micros")
    expected_rows = [{key: row[key] for key in wire_keys} for row in fixture["rows"]]
    engine = create_sqlite_engine()
    sid = "ses_activity_order_contract"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn, scope, sid, mid="fixture-prompt", mtype="user", author="user",
            created_at="2026-09-04T23:59:59Z", source="user", text="start",
        )
        for row in fixture["rows"]:
            if row["kind"] == "assistant":
                _msg(
                    conn, scope, sid, mid=row["id"], mtype="assistant", author="agent",
                    created_at=row["created_at"], text=row["text"],
                )
            else:
                assert row["kind"] == "tool_call"
                metadata = {"legacy_message_id": row["legacy_message_id"]} if "legacy_message_id" in row else {}
                _evt(
                    conn, scope, sid, eid=row["id"], created_at=row["created_at"],
                    text=row["text"], metadata=metadata,
                )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
        assert len(groups) == 1
        detail = agent_activity_service.get_turn_group(conn, session_id=sid, group_id=groups[0]["id"])
    assert detail is not None
    assert detail["open"] is True
    assert detail["rows"] == expected_rows


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


def test_non_dispatching_user_annotations_do_not_split_a_turn(isolated_state):
    """A non-dispatching annotation is display-only, even though a user authored it."""
    engine = create_sqlite_engine()
    sid = "ses_spuser"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00Z", text="q", source="user")
        _evt(conn, scope, sid, eid="e_1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash`")
        # A Show-Page user mark lands WHILE the turn is still producing activity.
        _msg(
            conn, scope, sid, mid="m_sp",
            mtype=messages_service.ANNOTATION_TYPE, author="user",
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


def test_dispatching_annotations_do_not_group_as_activity_turns(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_annotation_turn"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_annotation",
            mtype=messages_service.ANNOTATION_TYPE,
            author="harness",
            created_at="2026-06-01T10:00:00Z",
            text="review this heading",
            source="harness",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_annotation_tool",
            created_at="2026-06-01T10:00:01Z",
            text="🔧 `Read`",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:02Z",
            text="done",
        )

    with engine.connect() as conn:
        summary = agent_activity_service.list_turn_groups(conn, session_id=sid)

    assert summary["groups"] == []


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


def test_activity_after_nonterminal_output_anchors_to_that_output(isolated_state):
    """A delayed-steer answer is the visible boundary for later interrupted work."""

    engine = create_sqlite_engine()
    sid = "ses_output_boundary"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_u1",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="primary",
            source="user",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_output",
            mtype="output",
            author="agent",
            created_at="2026-06-01T10:00:01Z",
            text="primary answer",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_after_output",
            created_at="2026-06-01T10:00:02Z",
            text="continued work",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_u2",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:03Z",
            text="next turn",
            source="user",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert len(groups) == 1
    assert groups[0]["status"] == "interrupted"
    assert groups[0]["anchor_message_id"] == "m_output"
    assert groups[0]["anchor_position"] == "after"
    assert groups[0]["open"] is False


def test_nonterminal_output_completes_prior_activity_and_anchors_later_work(
    isolated_state,
):
    engine = create_sqlite_engine()
    sid = "ses_output_completion_boundary"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_u1",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="primary",
            source="user",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_before_output",
            created_at="2026-06-01T10:00:01Z",
            text="primary work",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_output",
            mtype="output",
            author="agent",
            created_at="2026-06-01T10:00:02Z",
            text="primary answer",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_after_output",
            created_at="2026-06-01T10:00:03Z",
            text="steered work",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_u2",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:04Z",
            text="next turn",
            source="user",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert len(groups) == 2
    assert groups[0]["status"] == "done"
    assert groups[0]["anchor_message_id"] == "m_output"
    assert groups[0]["anchor_position"] == "before"
    assert groups[0]["open"] is False
    assert groups[1]["status"] == "interrupted"
    assert groups[1]["anchor_message_id"] == "m_output"
    assert groups[1]["anchor_position"] == "after"
    assert groups[1]["open"] is False


@pytest.mark.parametrize(
    ("message_type", "metadata", "expected_status"),
    [
        ("result", {"detached": True}, "done"),
        ("error", {"detached": True}, "failed"),
        (
            "notify",
            {"detached": True, "event": "backend_failure"},
            "failed",
        ),
    ],
)
def test_detached_completion_closes_only_activity_with_matching_turn_provenance(
    isolated_state,
    message_type,
    metadata,
    expected_status,
):
    engine = create_sqlite_engine()
    sid = f"ses_detached_{message_type}"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_u1",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="primary",
            source="user",
            metadata={"turn_id": "turn-background"},
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_before",
            created_at="2026-06-01T10:00:01Z",
            text="background work",
            turn_id="turn-background",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_detached",
            mtype=message_type,
            author="agent",
            created_at="2026-06-01T10:00:02Z",
            text="background completed",
            metadata={**metadata, "turn_id": "turn-background"},
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert [group["status"] for group in groups] == [expected_status]
    assert groups[0]["anchor_message_id"] == "m_detached"
    assert groups[0]["anchor_position"] == "before"


@pytest.mark.parametrize(
    ("message_type", "metadata", "expected_status"),
    [
        ("result", {"detached": True}, "done"),
        ("error", {"detached": True}, "failed"),
        (
            "notify",
            {"detached": True, "event": "backend_failure"},
            "failed",
        ),
    ],
)
def test_provenance_free_detached_completion_closes_unambiguous_activity(
    isolated_state,
    message_type,
    metadata,
    expected_status,
):
    engine = create_sqlite_engine()
    sid = f"ses_detached_unowned_{message_type}"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_u1",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="legacy activity",
            source="user",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_before",
            created_at="2026-06-01T10:00:01Z",
            text="recovered background work",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_detached",
            mtype=message_type,
            author="agent",
            created_at="2026-06-01T10:00:02Z",
            text="background completed",
            metadata=metadata,
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert [group["status"] for group in groups] == [expected_status]
    assert groups[0]["anchor_message_id"] == "m_detached"
    assert groups[0]["anchor_position"] == "before"
    assert groups[0]["open"] is False


def test_provenance_free_detached_completion_does_not_guess_after_interleaving(
    isolated_state,
):
    engine = create_sqlite_engine()
    sid = "ses_detached_unowned_interleaved"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_u1",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="background origin",
            source="user",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_background",
            created_at="2026-06-01T10:00:01Z",
            text="background work",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_u2",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:02Z",
            text="new turn",
            source="user",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_current",
            created_at="2026-06-01T10:00:03Z",
            text="current work",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_detached",
            mtype="error",
            author="agent",
            created_at="2026-06-01T10:00:04Z",
            text="background failed",
            metadata={"detached": True},
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_current_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:05Z",
            text="current completed",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert [group["status"] for group in groups] == ["interrupted", "done"]
    assert groups[0]["anchor_message_id"] == "m_u1"
    assert groups[1]["anchor_message_id"] == "m_current_result"


@pytest.mark.parametrize(
    ("message_type", "metadata", "expected_status"),
    [
        ("result", {"detached": True}, "done"),
        ("error", {"detached": True}, "failed"),
        (
            "notify",
            {"detached": True, "event": "backend_failure"},
            "failed",
        ),
    ],
)
def test_detached_completion_repairs_its_origin_without_consuming_newer_activity(
    isolated_state,
    message_type,
    metadata,
    expected_status,
):
    engine = create_sqlite_engine()
    sid = f"ses_detached_interleaved_{message_type}"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_u1",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="background origin",
            source="user",
            metadata={"turn_id": "turn-background"},
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_background",
            created_at="2026-06-01T10:00:01Z",
            text="background work",
            turn_id="turn-background",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_u2",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:02Z",
            text="new turn",
            source="user",
            metadata={"turn_id": "turn-current"},
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_current",
            created_at="2026-06-01T10:00:03Z",
            text="current work",
            turn_id="turn-current",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_detached",
            mtype=message_type,
            author="agent",
            created_at="2026-06-01T10:00:04Z",
            text="background completed",
            metadata={**metadata, "turn_id": "turn-background"},
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_current_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:05Z",
            text="current completed",
            metadata={"turn_id": "turn-current"},
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert [group["status"] for group in groups] == [expected_status, "done"]
    assert groups[0]["anchor_message_id"] == "m_detached"
    assert groups[1]["anchor_message_id"] == "m_current_result"


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


# ===== Terminal taxonomy (silent completions, notify, error, Stop) =====
# result / notify / silent-marker = done; backend_failure notify / error = failed;
# no terminal at all (cancel/Stop) = interrupted.


@pytest.mark.parametrize("ending", ["silent", "interrupted"])
def test_hidden_agent_started_turn_keeps_transcript_visible_activity_anchor(
    isolated_state,
    ending,
):
    engine = create_sqlite_engine()
    sid = f"ses_hidden_start_{ending}"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="m_previous",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:00Z",
            text="previous answer",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="m_hidden",
            mtype="agent_initiated",
            author="harness",
            source="harness",
            created_at="2026-06-01T10:00:01Z",
            text="internal continuation",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="e_hidden",
            created_at="2026-06-01T10:00:02Z",
            text="tool",
        )
        if ending == "silent":
            _evt(
                conn,
                scope,
                sid,
                eid="e_terminal",
                created_at="2026-06-01T10:00:03Z",
                text="",
                event_type="silent_terminal",
                metadata={"terminal_outcome": "completed"},
            )
        else:
            _msg(
                conn,
                scope,
                sid,
                mid="m_next",
                mtype="user",
                author="user",
                source="user",
                created_at="2026-06-01T10:00:03Z",
                text="next question",
            )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]

    assert len(groups) == 1
    assert groups[0]["anchor_message_id"] == "m_previous"
    assert groups[0]["anchor_message_id"] != "m_hidden"
    assert groups[0]["anchor_position"] == "after"


def test_silent_completion_marks_turn_done(isolated_state):
    """A reply-less terminal Turn closes activity without a pseudo Message."""
    engine = create_sqlite_engine()
    sid = "ses_silent"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        turn_id = "trn_silent"
        attempt_id = "atm_silent"
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="m_h1",
            session_id=sid,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="watch fired",
            ),
            dispatch_text="watch fired",
            now="2026-06-01T10:00:00.000000+00:00",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id=turn_id,
            session_id=sid,
            initial_delivery_id=delivery["id"],
            state="starting",
            backend="codex",
            start_attempt_id=attempt_id,
            dispatch_text="watch fired",
            now="2026-06-01T10:00:00.000000+00:00",
        )
        assert message_deliveries.open_start_attempt(
            conn,
            delivery["id"],
            expected_version=int(delivery["version"]),
            turn_id=turn_id,
            attempt_id=attempt_id,
        ) is not None
        assert _accept_start(conn, turn_id)
        _evt(conn, scope, sid, eid="e_s1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash` `{\"command\":\"a\"}`")
        _evt(conn, scope, sid, eid="e_s2", created_at="2026-06-01T10:00:02Z", text="🔧 `Read` `{\"file_path\":\"b\"}`")
        message_deliveries.terminalize_turn(
            conn,
            turn_id,
            outcome="completed",
            settled_by="terminal_result",
            evidence_kind="test_replyless_completion",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert len(groups) == 1
    g = groups[0]
    assert g["status"] == "done"
    assert g["anchor_message_id"] == "m_h1"
    assert g["anchor_position"] == "after"
    assert g["open"] is False
    assert g["steps"] == 2


def test_replyless_terminal_keeps_subsecond_order_after_last_activity(isolated_state):
    """A Turn terminal is emitted after its final tool event, even in one second."""

    engine = create_sqlite_engine()
    sid = "ses_precise_terminal"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        turn_id = "trn_precise_terminal"
        attempt_id = "atm_precise_terminal"
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_precise_terminal",
            session_id=sid,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="watch fired",
            ),
            dispatch_text="watch fired",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id=turn_id,
            session_id=sid,
            initial_delivery_id=delivery["id"],
            state="starting",
            backend="codex",
            start_attempt_id=attempt_id,
            dispatch_text="watch fired",
        )
        assert message_deliveries.open_start_attempt(
            conn,
            delivery["id"],
            expected_version=int(delivery["version"]),
            turn_id=turn_id,
            attempt_id=attempt_id,
        ) is not None
        assert _accept_start(conn, turn_id)
        emitted_micros = int(time.time() * 1_000_000)
        emitted_at = datetime.fromtimestamp(
            emitted_micros / 1_000_000,
            timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _evt(
            conn,
            scope,
            sid,
            eid=f"evt_{emitted_micros:015x}deadbeef",
            created_at=emitted_at,
            text="final tool call",
        )
        terminal = message_deliveries.terminalize_turn(
            conn,
            turn_id,
            outcome="completed",
            settled_by="terminal_result",
            evidence_kind="test_replyless_completion",
        )

    terminal_at = str(terminal["turn"]["terminal_at"])
    assert "." in terminal_at and terminal_at.endswith("Z")
    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert len(groups) == 1
    assert groups[0]["status"] == "done"
    assert groups[0]["steps"] == 1


def test_not_written_successor_does_not_close_active_turn_activity(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_not_written_activity"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        active_delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_active_initial",
            session_id=sid,
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="active input",
            ),
            dispatch_text="active input",
            now="2026-06-01T10:00:00Z",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id="trn_active",
            session_id=sid,
            initial_delivery_id=active_delivery["id"],
            state="starting",
            backend="codex",
            start_attempt_id="atm_active_initial",
            dispatch_text="active input",
            now="2026-06-01T10:00:00Z",
        )
        assert message_deliveries.open_start_attempt(
            conn,
            active_delivery["id"],
            expected_version=int(active_delivery["version"]),
            turn_id="trn_active",
            attempt_id="atm_active_initial",
        ) is not None
        assert _accept_start(conn, "trn_active")
        _evt(
            conn,
            scope,
            sid,
            eid="evt_active_tool",
            created_at="2026-06-01T10:00:01Z",
            text="active tool",
        )
        successor = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_refused_successor",
            session_id=sid,
            priority="p0",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="replacement",
            ),
            dispatch_text="replacement",
            now="2026-06-01T10:00:02Z",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id="trn_refused_successor",
            session_id=sid,
            initial_delivery_id="msg_refused_successor",
            state="waiting",
            backend="codex",
            now="2026-06-01T10:00:02Z",
        )
        assert message_deliveries.cas_delivery(
            conn,
            successor["id"],
            expected_version=int(successor["version"]),
            expected_states=("reserved",),
            values={
                "state": "interrupt_waiting",
                "turn_id": "trn_refused_successor",
                "turn_role": "initial",
                "turn_position": 0,
            },
        ) is not None
        message_deliveries.terminalize_turn(
            conn,
            "trn_refused_successor",
            outcome="not_written",
            settled_by="interrupt_refused",
            evidence_kind="definitive_stop_receipt",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert len(groups) == 1
    assert groups[0]["status"] == "interrupted"
    assert groups[0]["open"] is True
    assert groups[0]["steps"] == 1


def test_queued_initial_message_opens_at_its_accepted_turn(
    isolated_state,
    monkeypatch,
):
    """Transcript entry and execution grouping both start at Delivery acceptance."""

    engine = create_sqlite_engine()
    sid = "ses_queued_boundary"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(
            conn,
            scope,
            sid,
            mid="msg_first_input",
            mtype="user",
            author="user",
            created_at="2026-06-01T10:00:00Z",
            text="first",
            source="user",
        )
        _evt(
            conn,
            scope,
            sid,
            eid="event_first_tool",
            created_at="2026-06-01T10:00:10Z",
            text="first tool",
        )
        waiting_delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_queued_input",
            session_id=sid,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="queued while first turn runs",
            ),
            dispatch_text="queued while first turn runs",
            now="2026-06-01T10:00:15Z",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="msg_first_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:20Z",
            text="first done",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id="trn_queued_input",
            session_id=sid,
            initial_delivery_id="msg_queued_input",
            state="waiting",
            backend="codex",
            now="2026-06-01T10:00:15Z",
        )
        waiting_delivery = message_deliveries.cas_delivery(
            conn,
            waiting_delivery["id"],
            expected_version=int(waiting_delivery["version"]),
            expected_states=("reserved",),
            values={
                "state": "interrupt_waiting",
                "turn_id": "trn_queued_input",
                "turn_role": "initial",
                "turn_position": 0,
            },
        )
        assert waiting_delivery is not None
        monkeypatch.setattr(
            message_deliveries,
            "turn_now_iso",
            lambda: "2026-06-01T10:00:21.000001Z",
        )
        waiting_turn = message_deliveries.get_turn(conn, "trn_queued_input")
        waiting_delivery = message_deliveries.get_delivery(conn, "msg_queued_input")
        assert waiting_turn is not None
        assert waiting_delivery is not None
        assert message_deliveries.activate_waiting_successor(
            conn,
            turn=waiting_turn,
            delivery=waiting_delivery,
        ) is not None
        assert _accept_start(conn, "trn_queued_input")
        _evt(
            conn,
            scope,
            sid,
            eid="event_second_tool",
            created_at="2026-06-01T10:00:22Z",
            text="second tool",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="msg_second_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:23Z",
            text="second done",
        )

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id=sid,
            types=("user", "result"),
        )["messages"]
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert [row["id"] for row in transcript] == [
        "msg_first_input",
        "msg_first_result",
        "msg_queued_input",
        "msg_second_result",
    ]
    assert [group["status"] for group in groups] == ["done", "done"]
    assert [group["anchor_message_id"] for group in groups] == [
        "msg_first_result",
        "msg_second_result",
    ]


def test_accepted_steer_participant_does_not_open_a_second_turn(isolated_state):
    engine = create_sqlite_engine()
    sid = "ses_steer_participant"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        initial_delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_initial",
            session_id=sid,
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="initial",
            ),
            dispatch_text="initial",
            now="2026-06-01T10:00:00Z",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id="trn_shared",
            session_id=sid,
            initial_delivery_id=initial_delivery["id"],
            state="starting",
            backend="codex",
            start_attempt_id="atm_initial",
            dispatch_text="initial",
            now="2026-06-01T10:00:00Z",
        )
        assert message_deliveries.open_start_attempt(
            conn,
            initial_delivery["id"],
            expected_version=int(initial_delivery["version"]),
            turn_id="trn_shared",
            attempt_id="atm_initial",
        ) is not None
        assert _accept_start(conn, "trn_shared")
        _evt(
            conn,
            scope,
            sid,
            eid="event_before_steer",
            created_at="2026-06-01T10:00:01Z",
            text="before steer",
        )
        message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_steer",
            session_id=sid,
            priority="p1",
            state="steering",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope,
                session_id=sid,
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="steer participant",
            ),
            dispatch_text="steer participant",
            current_attempt_id="atm_steer",
            current_attempt_kind="steer",
            current_target_turn_id="trn_shared",
            current_expected_native_turn_id="native-shared",
            now="2026-06-01T10:00:02Z",
        )
        assert message_deliveries.materialize_acceptance(
            conn,
            delivery_id="msg_steer",
            expected_attempt_id="atm_steer",
            turn_id="trn_shared",
            evidence={"kind": "test_steer_acceptance"},
        ) is not None
        _evt(
            conn,
            scope,
            sid,
            eid="event_after_steer",
            created_at="2026-06-01T10:00:03Z",
            text="after steer",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="msg_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:04Z",
            text="done",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert len(groups) == 1
    assert groups[0]["status"] == "done"
    assert groups[0]["steps"] == 2
    assert groups[0]["anchor_message_id"] == "msg_result"


def test_merged_initial_deliveries_keep_one_turn_start(isolated_state, monkeypatch):
    engine = create_sqlite_engine()
    sid = "ses_merged_initial"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        deliveries = [
            message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=sid,
                priority="p3",
                state="queued",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=scope,
                    session_id=sid,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type="user",
                    text=text,
                ),
                dispatch_text=text,
                now=created_at,
            )
            for delivery_id, text, created_at in (
                ("msg_merged_initial", "first", "2026-06-01T10:00:00Z"),
                ("msg_merged_second", "second", "2026-06-01T10:00:01Z"),
            )
        ]
        monkeypatch.setattr(
            message_deliveries,
            "turn_now_iso",
            lambda: "2026-06-01T10:00:02Z",
        )
        message_deliveries.claim_start_batch(
            conn,
            turn_id="trn_merged",
            session_id=sid,
            backend="codex",
            deliveries=deliveries,
            dispatch_text="first\nsecond",
            attempt_id="atm_merged",
        )
        assert _accept_start(conn, "trn_merged")
        _evt(
            conn,
            scope,
            sid,
            eid="event_merged_tool",
            created_at="2026-06-01T10:00:03Z",
            text="merged tool",
        )
        _msg(
            conn,
            scope,
            sid,
            mid="msg_merged_result",
            mtype="result",
            author="agent",
            created_at="2026-06-01T10:00:04Z",
            text="done",
        )

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)[
            "groups"
        ]

    assert len(groups) == 1
    assert groups[0]["status"] == "done"
    assert groups[0]["steps"] == 1
    assert groups[0]["anchor_message_id"] == "msg_merged_result"


def test_midturn_notify_does_not_split_or_close_a_turn(isolated_state):
    """A plain (non-backend-failure) ``notify`` is NOT terminal: agents emit mid-turn
    notify rows that keep the turn going (e.g. Claude's model-refusal fallback). It must
    not close the pending group or split the turn — the turn still closes on its real
    terminal, keeping all steps in ONE group. (A genuine notify-only completion is
    closed by the silent marker instead — see test_silent_completion_marks_turn_done.)"""
    engine = create_sqlite_engine()
    sid = "ses_notify"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q", source="user")
        _evt(conn, scope, sid, eid="e_n1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash` `{\"command\":\"x\"}`")
        _msg(conn, scope, sid, mid="m_n1", mtype="notify", author="agent", created_at="2026-06-01T10:00:02.000000+00:00", text="refusal fallback")
        _evt(conn, scope, sid, eid="e_n2", created_at="2026-06-01T10:00:03Z", text="🔧 `Read` `{\"file_path\":\"y\"}`")
        _msg(conn, scope, sid, mid="m_r1", mtype="result", author="agent", created_at="2026-06-01T10:00:04.000000+00:00", text="answer")

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    # ONE done group spanning both tool steps — the mid-turn notify neither closed nor
    # split it; the turn closed on its real terminal.
    assert [g["status"] for g in groups] == ["done"]
    assert groups[0]["steps"] == 2
    assert groups[0]["anchor_message_id"] == "m_r1"
    assert groups[0]["anchor_position"] == "before"


def test_backend_failure_notify_is_failed(isolated_state):
    """A ``backend_failure`` notify stays a FAILED terminal (not done)."""
    engine = create_sqlite_engine()
    sid = "ses_bf"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q", source="user")
        _evt(conn, scope, sid, eid="e_b1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash` `{\"command\":\"x\"}`")
        _msg(conn, scope, sid, mid="m_bf", mtype="notify", author="agent", created_at="2026-06-01T10:00:02.000000+00:00", text="backend died", metadata={"event": "backend_failure"})

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert [g["status"] for g in groups] == ["failed"]


def test_stop_without_terminal_stays_interrupted(isolated_state):
    """Cancel/Stop writes NO marker (no visible terminal, no silent marker), so a
    turn with activity and no terminal before the next turn stays ``interrupted``."""
    engine = create_sqlite_engine()
    sid = "ses_stop"
    with engine.begin() as conn:
        scope = _seed_session(conn, session_id=sid)
        _msg(conn, scope, sid, mid="m_u1", mtype="user", author="user", created_at="2026-06-01T10:00:00.000000+00:00", text="q1", source="user")
        _evt(conn, scope, sid, eid="e_st1", created_at="2026-06-01T10:00:01Z", text="🔧 `Bash` `{\"command\":\"x\"}`")
        # User stopped it, then started a new turn — no terminal for turn 1.
        _msg(conn, scope, sid, mid="m_u2", mtype="user", author="user", created_at="2026-06-01T10:01:00.000000+00:00", text="q2", source="user")

    with engine.connect() as conn:
        groups = agent_activity_service.list_turn_groups(conn, session_id=sid)["groups"]
    assert [g["status"] for g in groups] == ["interrupted"]
    assert groups[0]["anchor_message_id"] == "m_u1"  # anchored to its own trigger
    assert groups[0]["anchor_position"] == "after"
