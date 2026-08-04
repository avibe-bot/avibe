"""Focused tests for storage.messages_service behaviours that are easy to
regress: pagination cursor and the ``mark_session_read`` boundary check.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import message_deliveries, messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_runs, agent_sessions, messages, scopes
from storage.pagination import PageRequest
from storage.settings_service import upsert_scope
from vibe.message_identity import HARNESS_TYPE


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_scope(conn) -> str:
    now = messages_service._utc_now_iso()
    return upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_test", now=now)


def _seed_session(conn, scope_id: str, session_id: str) -> None:
    now = messages_service._utc_now_iso()
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
            agent_backend="claude",
            agent_variant="default",
            session_anchor="anchor_" + session_id,
            native_session_id="",
            status="active",
            metadata_json="{}",
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )


def test_native_message_identity_is_scoped_to_conversation(isolated_state):
    engine = create_sqlite_engine(isolated_state / "state" / "vibe.sqlite")
    with engine.begin() as conn:
        now = messages_service._utc_now_iso()
        first_scope = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="chat-1",
            now=now,
        )
        second_scope = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="chat-2",
            now=now,
        )
        _seed_session(conn, first_scope, "ses_chat_1")
        _seed_session(conn, second_scope, "ses_chat_2")
        messages_service.append(
            conn,
            scope_id=first_scope,
            session_id="ses_chat_1",
            platform="telegram",
            author="user",
            text="first chat",
            native_message_id="1",
        )

        assert messages_service.native_message_exists(
            conn,
            platform="telegram",
            scope_id=first_scope,
            native_message_id="1",
        )
        assert not messages_service.native_message_exists(
            conn,
            platform="telegram",
            scope_id=second_scope,
            native_message_id="1",
        )
        messages_service.append(
            conn,
            scope_id=second_scope,
            session_id="ses_chat_2",
            platform="telegram",
            author="user",
            text="second chat",
            native_message_id="1",
        )
        first_delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_delivery_chat_1",
            session_id="ses_chat_1",
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=first_scope,
                session_id="ses_chat_1",
                platform="telegram",
                author="user",
                source="user",
                message_type="user",
                text="queued first chat",
                native_message_id="2",
            ),
            dispatch_text="queued first chat",
            dedupe_key=message_deliveries.native_dedupe_key(
                "telegram",
                "2",
                scope_id=first_scope,
            ),
        )
        second_delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_delivery_chat_2",
            session_id="ses_chat_2",
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=second_scope,
                session_id="ses_chat_2",
                platform="telegram",
                author="user",
                source="user",
                message_type="user",
                text="queued second chat",
                native_message_id="2",
            ),
            dispatch_text="queued second chat",
            dedupe_key=message_deliveries.native_dedupe_key(
                "telegram",
                "2",
                scope_id=second_scope,
            ),
        )
        assert first_delivery["id"] != second_delivery["id"]

    assert message_deliveries.native_dedupe_key(
        "telegram",
        "1",
        scope_id=first_scope,
    ) != message_deliveries.native_dedupe_key(
        "telegram",
        "1",
        scope_id=second_scope,
    )


def test_native_message_lookup_returns_canonical_result_payload(isolated_state):
    engine = create_sqlite_engine(isolated_state / "state" / "vibe.sqlite")
    with engine.begin() as conn:
        now = messages_service._utc_now_iso()
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C-results",
            now=now,
        )
        _seed_session(conn, scope_id, "ses-results")
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses-results",
            platform="slack",
            author="agent",
            message_type="result",
            text="Exact accepted assistant result",
            content={"result_footer": "3.4s | 812 tok"},
            metadata={
                "activity_ids": ["task-a", "task-b"],
                "run_ids": ["run-a", "run-b"],
            },
            native_message_id="activity-batch-receipt",
        )

        accepted = messages_service.get_native_message(
            conn,
            platform="slack",
            scope_id=scope_id,
            native_message_id="activity-batch-receipt",
        )

    assert accepted is not None
    assert accepted["text"] == "Exact accepted assistant result"
    assert accepted["content"]["result_footer"] == "3.4s | 812 tok"
    assert accepted["metadata"]["activity_ids"] == ["task-a", "task-b"]


def _list_production_transcript(conn, session_id: str):
    return messages_service.list_session_messages(
        conn,
        session_id=session_id,
        limit=50,
        types=messages_service.TRANSCRIPT_TYPES,
        tail=True,
    )


def _insert_harness_msg(conn, scope_id, session_id, *, author_name, native_message_id,
                        author_id=None, msg_id, created_at):
    conn.execute(
        messages.insert().values(
            id=msg_id, scope_id=scope_id, session_id=session_id, platform="avibe",
            author="harness", type=HARNESS_TYPE, source="harness",
            author_name=author_name, author_id=author_id,
            native_message_id=native_message_id,
            content_text="prompt", content_json="{}", metadata_json="{}",
            created_at=created_at, updated_at=created_at,
        )
    )


def _seed_titled_agent_session(conn, scope_id, session_id, *, title, agent_name):
    now = messages_service._utc_now_iso()
    conn.execute(
        agent_sessions.insert().values(
            id=session_id, scope_id=scope_id, agent_name=agent_name,
            agent_backend="claude", agent_variant="default",
            session_anchor="anchor_" + session_id, native_session_id="",
            status="active", title=title, metadata_json="{}",
            created_at=now, updated_at=now, last_active_at=now,
        )
    )


def _insert_agent_run(conn, run_id, *, session_id, source_kind, source_actor=None, parent_run_id=None):
    now = messages_service._utc_now_iso()
    conn.execute(
        agent_runs.insert().values(
            id=run_id, run_type="agent_run", status="succeeded", cancel_requested=0,
            source_kind=source_kind, source_actor=source_actor, parent_run_id=parent_run_id,
            session_id=session_id, created_at=now, updated_at=now, metadata_json="{}",
        )
    )


def test_agent_run_message_provenance_enrichment(isolated_state):
    # A9a read-side enrichment resolves the SOURCE session per run kind:
    #  - source_kind='agent'    → source_actor is the caller session.
    #  - source_kind='callback' → source_actor is a run id; the source is the
    #    PARENT (delegated) run's session.
    # A non-agent_run harness message (task trigger) is left untouched.
    from core.vibe_agents import VibeAgentStore

    store = VibeAgentStore()
    store.create(name="archive-fallback", backend="claude")
    caller_agent = store.create(name="pm", backend="claude")
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_target")
        _seed_titled_agent_session(conn, scope_id, "ses_caller", title=None, agent_name="pm")
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == "ses_caller")
            .values(agent_id=caller_agent.id)
        )
        _seed_titled_agent_session(conn, scope_id, "ses_delegated", title="Delegated 审计", agent_name="evm")
        # Agent spawn: source_actor is the caller session.
        _insert_agent_run(conn, "execAgent", session_id="ses_target",
                          source_kind="agent", source_actor="ses_caller")
        # Callback: source_actor is a decorated run id; parent run ran in ses_delegated.
        _insert_agent_run(conn, "parentRun", session_id="ses_delegated", source_kind="agent",
                          source_actor="ses_caller")
        _insert_agent_run(conn, "execCb", session_id="ses_target", source_kind="callback",
                          source_actor="parentRun:terminal:succeeded", parent_run_id="parentRun")
        _insert_harness_msg(conn, scope_id, "ses_target", author_name="agent_run",
                            native_message_id="agent_run:execAgent", msg_id="msg_agent",
                            created_at="2026-05-30T10:00:00Z")
        _insert_harness_msg(conn, scope_id, "ses_target", author_name="agent_run",
                            native_message_id="agent_run:execCb", msg_id="msg_cb",
                            created_at="2026-05-30T10:00:01Z")
        _insert_harness_msg(conn, scope_id, "ses_target", author_name="scheduled",
                            author_id="def_1", native_message_id="scheduled:def_1:execB",
                            msg_id="msg_task", created_at="2026-05-30T10:00:02Z")

    archived = store.archive("pm")
    assert archived is not None
    store.close()

    with engine.connect() as conn:
        result = messages_service.list_session_messages(conn, session_id="ses_target")
    by_id = {m["id"]: m for m in result["messages"]}
    assert by_id["msg_agent"]["source_session_id"] == "ses_caller"
    assert by_id["msg_agent"]["source_session_title"] is None
    assert by_id["msg_agent"]["source_session_agent_name"] == "pm"
    # Callback resolves through the parent run's session, not the run-id source_actor.
    assert by_id["msg_cb"]["source_session_id"] == "ses_delegated"
    assert by_id["msg_cb"]["source_session_title"] == "Delegated 审计"
    # A non-agent_run harness message gets no source fields.
    assert "source_session_id" not in by_id["msg_task"]


def test_agent_run_provenance_skips_missing_source_session(isolated_state):
    # Dead-link guard: if the resolved source session no longer has an
    # agent_sessions row (stale/imported/deleted), the enrichment must NOT attach
    # source_session_id — a /chat/<missing id> link would only show the fallback.
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_target")
        # source_actor points at a session that was never seeded (deleted/stale).
        _insert_agent_run(conn, "execGhost", session_id="ses_target",
                          source_kind="agent", source_actor="ses_ghost")
        _insert_harness_msg(conn, scope_id, "ses_target", author_name="agent_run",
                            native_message_id="agent_run:execGhost", msg_id="msg_ghost",
                            created_at="2026-05-30T10:00:00Z")

    with engine.connect() as conn:
        result = messages_service.list_session_messages(conn, session_id="ses_target")
    by_id = {m["id"]: m for m in result["messages"]}
    assert "source_session_id" not in by_id["msg_ghost"]


@pytest.mark.parametrize(
    ("native_message_id", "expected_kind", "expected_definition_id"),
    [
        ("watch:def_watch:run_1", "watch", "def_watch"),
        ("watch:run_legacy", "watch", None),
        ("scheduled:def_task:run_2", "scheduled", "def_task"),
        ("scheduled:run_legacy", "scheduled", None),
        ("webhook:def_hook:run_3", "webhook", "def_hook"),
        ("webhook:run_legacy", "webhook", None),
        ("hook:run_4", "hook", None),
        ("agent_run:run_5", "agent_run", None),
    ],
)
def test_legacy_queued_harness_message_recovers_native_provenance(
    isolated_state,
    native_message_id,
    expected_kind,
    expected_definition_id,
):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_target")
        _insert_harness_msg(
            conn,
            scope_id,
            "ses_target",
            author_name=None,
            native_message_id=native_message_id,
            msg_id="msg_trigger",
            created_at="2026-08-04T00:00:00Z",
        )

    with engine.connect() as conn:
        [message] = messages_service.list_session_messages(
            conn,
            session_id="ses_target",
        )["messages"]

    assert message["author_name"] == expected_kind
    assert message.get("author_id") == expected_definition_id


def test_transcript_orders_queued_input_at_acceptance_across_all_cursors(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_order")
        _insert_msg(
            conn,
            scope_id,
            "ses_order",
            "user",
            "first prompt",
            "2026-08-04T00:00:00Z",
            msg_id="msg_001",
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_order",
            "agent",
            "reply before queued input starts",
            "2026-08-04T00:00:02Z",
            msg_id="msg_003",
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_order",
            "user",
            "submitted while busy",
            "2026-08-04T00:00:01Z",
            msg_id="msg_002",
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == "msg_002")
            .values(delivered_at="2026-08-04T00:00:03.500000+00:00")
        )

    with engine.connect() as conn:
        all_rows = messages_service.list_session_messages(
            conn,
            session_id="ses_order",
        )["messages"]
        tail = messages_service.list_session_messages(
            conn,
            session_id="ses_order",
            tail=True,
            limit=2,
        )["messages"]
        before = messages_service.list_session_messages(
            conn,
            session_id="ses_order",
            before_id="msg_002",
            limit=2,
        )["messages"]
        around = messages_service.list_session_messages(
            conn,
            session_id="ses_order",
            around_id="msg_003",
            limit=1,
        )["messages"]

    assert [row["id"] for row in all_rows] == ["msg_001", "msg_003", "msg_002"]
    assert [row["id"] for row in tail] == ["msg_003", "msg_002"]
    assert [row["id"] for row in before] == ["msg_001", "msg_003"]
    assert [row["id"] for row in around] == ["msg_001", "msg_003", "msg_002"]


def test_append_keeps_subsecond_transcript_order_for_direct_reply(
    isolated_state,
    monkeypatch,
):
    monkeypatch.setattr(
        messages_service,
        "_utc_now_iso",
        lambda: "2026-08-04T00:00:00.750000Z",
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_subsecond")
        prompt = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_subsecond",
            platform="avibe",
            author="user",
            text="prompt",
            delivered_at="2026-08-04T00:00:00.500000Z",
        )
        reply = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_subsecond",
            platform="avibe",
            author="agent",
            text="reply",
        )

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses_subsecond",
        )["messages"]

    assert reply["created_at"] == "2026-08-04T00:00:00.750000Z"
    assert [row["text"] for row in transcript] == ["prompt", "reply"]


def test_mark_session_read_ties_break_on_id(isolated_state):
    """When ``until_message_id`` points at a message whose ``created_at``
    is shared by newer messages (second precision), only rows at-or-before
    the anchor *by id* should be marked read. Otherwise the user's still-
    unread newest reply gets cleared.
    """
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_test")
        # Use a fixed timestamp so all three rows share the same created_at
        # (mimicking the second-precision collision).
        fixed_now = "2026-05-26T13:00:00Z"
        for content in ("first", "second", "third"):
            payload = {
                "id": messages_service._new_message_id(),
                "scope_id": scope_id,
                "session_id": "ses_test",
                "platform": "avibe",
                "author": "agent",
                "author_id": None,
                "author_name": None,
                "native_message_id": None,
                "parent_native_message_id": None,
                "content_text": content,
                "content_json": "{}",
                "metadata_json": "{}",
                "created_at": fixed_now,
                "updated_at": fixed_now,
                "delivered_at": None,
                "read_at": None,
            }
            conn.execute(messages.insert().values(**payload))

    with engine.connect() as conn:
        ordered = conn.execute(
            select(messages.c.id, messages.c.content_text)
            .where(messages.c.session_id == "ses_test")
            .order_by(messages.c.id.asc())
        ).all()
        # Take the middle row as the anchor: its lexicographically-smaller
        # id puts "first" before it and one row after it.
        anchor_id = ordered[1][0]
        anchor_text = ordered[1][1]

    with engine.begin() as conn:
        updated = messages_service.mark_session_read(conn, "ses_test", until_message_id=anchor_id)

    assert updated == 2, "should mark only the anchor + the row with smaller id"

    with engine.connect() as conn:
        rows = conn.execute(
            select(messages.c.content_text, messages.c.read_at)
            .where(messages.c.session_id == "ses_test")
            .order_by(messages.c.id.asc())
        ).all()
    read_states = {text: (read_at is not None) for text, read_at in rows}
    # The two rows up to and including the anchor are marked read…
    assert read_states[anchor_text] is True
    # …and the row with the larger id (same timestamp) stays unread.
    unread = [text for text, read in read_states.items() if not read]
    assert len(unread) == 1, "exactly one row after the anchor must remain unread"


def test_mark_session_read_without_anchor_marks_all(isolated_state):
    """No ``until_message_id`` → mark every unread agent row."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_all")
        for _ in range(3):
            messages_service.append(
                conn,
                scope_id=scope_id,
                session_id="ses_all",
                platform="avibe",
                author="agent",
                text="payload",
            )
            time.sleep(0.001)

    with engine.begin() as conn:
        updated = messages_service.mark_session_read(conn, "ses_all")
    assert updated == 3


def test_list_session_messages_cursor_uses_clamped_limit(isolated_state):
    """Regression: callers that pass ``limit > 500`` must still get a
    cursor when the result is a full clamped page, so they can paginate
    past the 500 mark instead of silently truncating at the cap.
    """
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_page")
        # 501 rows so the clamped 500-row page returns full and a
        # follow-up cursor is needed.
        for _ in range(501):
            messages_service.append(
                conn,
                scope_id=scope_id,
                session_id="ses_page",
                platform="avibe",
                author="agent",
                text="row",
            )

    with engine.connect() as conn:
        page = messages_service.list_session_messages(conn, session_id="ses_page", limit=1000)
    # Pre-fix this returned ``next_after_id=None`` even though there were
    # 501 rows total. The clamp-aware fix emits a cursor.
    assert len(page["messages"]) == 500
    assert page["next_after_id"] is not None


def test_list_session_messages_full_after_page_without_extra_row_has_no_cursor(isolated_state):
    """A full after_id page only gets a next cursor when an extra row exists."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_after_exact")
        for index in range(4):
            _insert_msg(
                conn,
                scope_id,
                "ses_after_exact",
                "agent",
                f"row {index}",
                f"2026-06-01T10:00:0{index}Z",
                msg_type="result",
            )

    with engine.connect() as conn:
        first = messages_service.list_session_messages(conn, session_id="ses_after_exact", limit=1)
        exact = messages_service.list_session_messages(
            conn,
            session_id="ses_after_exact",
            after_id=first["messages"][0]["id"],
            limit=3,
        )
        partial = messages_service.list_session_messages(
            conn,
            session_id="ses_after_exact",
            after_id=first["messages"][0]["id"],
            limit=2,
        )

    assert [m["text"] for m in exact["messages"]] == ["row 1", "row 2", "row 3"]
    assert exact["next_after_id"] is None
    assert [m["text"] for m in partial["messages"]] == ["row 1", "row 2"]
    assert partial["next_after_id"] == partial["messages"][-1]["id"]


def test_list_session_messages_filters_to_user_facing_types(isolated_state):
    """The chat transcript scopes to user-facing types so the intermediate
    assistant / notify rows stay out of the
    dialogue view (they're the process log, not the conversation)."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_tx")
        # Distinct timestamps so chronological order is deterministic (append's
        # second-resolution now would tie and fall back to random id order).
        _insert_msg(conn, scope_id, "ses_tx", "user", "q", "2026-05-30T10:00:00Z", msg_type="user")
        _insert_msg(conn, scope_id, "ses_tx", "agent", "thinking", "2026-05-30T10:00:01Z", msg_type="assistant")
        _insert_msg(conn, scope_id, "ses_tx", "agent", "progress", "2026-05-30T10:00:03Z", msg_type="notify")
        _insert_msg(conn, scope_id, "ses_tx", "agent", "final", "2026-05-30T10:00:04Z", msg_type="result")

    with engine.connect() as conn:
        every = messages_service.list_session_messages(conn, session_id="ses_tx")
        dialogue = messages_service.list_session_messages(
            conn, session_id="ses_tx", types=("user", "result")
        )

    assert [m["type"] for m in every["messages"]] == ["user", "assistant", "notify", "result"]
    assert [m["text"] for m in dialogue["messages"]] == ["q", "final"]


def test_error_terminal_in_transcript_but_not_unread(isolated_state):
    """A terminal FAILED result (type='error') is part of the conversation
    (transcript / inbox) but must NOT count as an unread agent reply — only a
    successful 'result' bumps the unread badge, so a failed turn doesn't look
    like an answer arrived (Codex P2)."""
    from storage.messages_service import TRANSCRIPT_TYPES

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_err")
        _insert_msg(conn, scope_id, "ses_err", "user", "q", "2026-05-30T10:00:00Z", msg_type="user")
        _insert_msg(conn, scope_id, "ses_err", "agent", "❌ boom", "2026-05-30T10:00:01Z", read=False, msg_type="error")

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(conn, session_id="ses_err", types=TRANSCRIPT_TYPES)
        unread = messages_service.unread_counts(conn)

    # The error is visible in the conversation...
    assert [m["type"] for m in transcript["messages"]] == ["user", "error"]
    # ...but it is NOT an unread agent reply (unread is result-only).
    assert unread.get(scope_id, 0) == 0


def test_harness_prompt_is_visible_but_not_treated_as_user_text(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_harness")
        _insert_msg(
            conn,
            scope_id,
            "ses_harness",
            "harness",
            "scheduled input",
            "2026-05-30T10:00:00Z",
            msg_type=messages_service.HARNESS_TYPE,
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_harness",
            "user",
            "human input",
            "2026-05-30T10:00:01Z",
            msg_type="user",
        )

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses_harness",
            types=messages_service.TRANSCRIPT_TYPES,
        )
        first_user = messages_service.first_user_text(conn, "ses_harness")

    assert [message["type"] for message in transcript["messages"]] == ["harness", "user"]
    assert first_user == "human input"


def test_append_normalizes_legacy_harness_identity(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_harness_append")
        row = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_harness_append",
            platform="avibe",
            author="user",
            message_type="user",
            source="harness",
            text="scheduled input",
        )

    assert row["author"] == messages_service.HARNESS_TYPE
    assert row["type"] == messages_service.HARNESS_TYPE
    assert row["source"] == messages_service.HARNESS_TYPE


@pytest.mark.parametrize(
    ("author", "source", "message_type", "expected"),
    [
        ("user", "user", None, "user"),
        ("user", "harness", None, "harness"),
        ("harness", "harness", "harness", "harness"),
        ("harness", "harness", "annotation", "annotation"),
    ],
)
def test_delivery_snapshot_preserves_accepted_message_identity(
    author,
    source,
    message_type,
    expected,
):
    snapshot = message_deliveries.message_snapshot(
        scope_id="scope",
        session_id="session",
        platform="avibe",
        author=author,
        source=source,
        message_type=message_type,
        text="input",
    )
    assert snapshot["type"] == expected


def test_show_annotation_reservation_flushes_as_annotation(isolated_state):
    engine = create_sqlite_engine()
    legacy_prompt = (
        "[show-annotation] comment\n\nLegacy body\n\n"
        "Anchor: #hero\n\nShow event id: evt_legacy"
    )
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_legacy_annotation")
        queued = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_legacy_annotation",
            author="harness",
            source="harness",
            author_name="show_annotation",
            message_type=messages_service.ANNOTATION_TYPE,
            text=legacy_prompt,
        )
        turn_id = message_deliveries.new_turn_id()
        message_deliveries.insert_turn(
            conn,
            turn_id=turn_id,
            session_id="ses_legacy_annotation",
            initial_delivery_id=queued["id"],
            state="starting",
            backend="opencode",
        )
        delivery = message_deliveries.get_delivery(conn, queued["id"])
        assert delivery is not None
        attempt_id = message_deliveries.new_attempt_id()
        claimed = message_deliveries.open_start_attempt(
            conn,
            queued["id"],
            expected_version=delivery["version"],
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        assert claimed is not None
        turn = message_deliveries.get_turn(conn, turn_id)
        assert turn is not None
        assert message_deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            runtime_key="annotation-runtime",
            runtime_turn_id="annotation-runtime-turn",
            native_turn_id="annotation-native-turn",
        ) is not None
        message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "native_start"},
        )
        visible = messages_service.get_message(conn, queued["id"])

    assert visible is not None
    assert visible["type"] == messages_service.ANNOTATION_TYPE
    assert visible["text"] == legacy_prompt
    assert "annotation" not in visible["content"]


def test_same_second_messages_order_by_insertion(isolated_state):
    """Rows sharing a (second-resolution) created_at still order by insertion in
    the transcript: the monotonic message id breaks the ``(created_at, id)`` tie,
    so a fast avibe turn never renders the agent result before the user prompt
    (nor lets the inbox pick the wrong 'last' row)."""
    engine = create_sqlite_engine()
    fixed = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_fast")
        # Identical created_at for both rows; ids come from _new_message_id() in
        # insertion order (the DB round-trip between calls separates microseconds).
        for author, mtype, text in (("user", "user", "prompt"), ("agent", "result", "answer")):
            conn.execute(
                messages.insert().values(
                    id=messages_service._new_message_id(),
                    scope_id=scope_id,
                    session_id="ses_fast",
                    platform="avibe",
                    author=author,
                    type=mtype,
                    content_text=text,
                    content_json="{}",
                    metadata_json="{}",
                    created_at=fixed,
                    updated_at=fixed,
                    read_at=None,
                )
            )

    with engine.connect() as conn:
        page = messages_service.list_session_messages(conn, session_id="ses_fast")
    assert [m["text"] for m in page["messages"]] == ["prompt", "answer"]


def test_transcript_visibility_depends_only_on_message_type(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_mark")
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_mark", platform="avibe", author="user", text="q"
        )
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_mark", platform="avibe",
            author="agent", message_type="assistant", text="thinking",
            metadata={"source": "show_page"},
        )
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_mark", platform="avibe",
            author="harness", source="harness", author_name="show_annotation",
            message_type=messages_service.ANNOTATION_TYPE, text="annotation",
            metadata={"source": "show_page"},
        )
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_mark",
            text="queued",
        )
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_mark", platform="avibe",
            author="agent", message_type="result", text="final",
        )

    with engine.connect() as conn:
        page = _list_production_transcript(conn, "ses_mark")
    texts = [m["text"] for m in page["messages"]]
    assert texts == ["q", "annotation", "final"]
    assert messages_service.ANNOTATION_TYPE in messages_service.TRANSCRIPT_TYPES
    assert messages_service.ANNOTATION_TYPE in messages_service.INBOX_ACTIVITY_TYPES


def test_transcript_keeps_notify_terminal_marker(isolated_state):
    """The chat transcript keeps a terminal ``notify`` (e.g. an agent run that
    failed and stopped without a result) while still hiding the intermediate
    assistant / tool_call process rows."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_n")
        for author, mtype, text in (
            ("user", "user", "go"),
            ("agent", "assistant", "thinking"),
            ("agent", "notify", "Agent run failed and stopped."),
        ):
            messages_service.append(
                conn, scope_id=scope_id, session_id="ses_n", platform="avibe",
                author=author, message_type=mtype, text=text,
            )

    with engine.connect() as conn:
        page = _list_production_transcript(conn, "ses_n")
    texts = [m["text"] for m in page["messages"]]
    assert texts == ["go", "Agent run failed and stopped."]  # notify kept; assistant/tool_call hidden


def test_append_defaults_type_from_author(isolated_state):
    """Callers that omit message_type (e.g. show-page transcript annotations)
    get a type derived from author — a human row must be 'user' so the
    user+result transcript filter keeps it, not mis-typed 'assistant'."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_def")
        user_row = messages_service.append(
            conn, scope_id=scope_id, session_id="ses_def", platform="avibe", author="user", text="hi"
        )
        agent_row = messages_service.append(
            conn, scope_id=scope_id, session_id="ses_def", platform="avibe", author="agent", text="yo"
        )
    assert user_row["type"] == "user"
    assert agent_row["type"] == "assistant"


def test_unread_counts_by_session_splits_within_a_scope(isolated_state):
    """Two sessions in one project report distinct per-session unread counts,
    counting unread agent *result* messages only. Intermediate assistant /
    tool_call rows (persisted for avibe but not user-facing) must NOT inflate
    the badge past what the inbox card shows."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_a")
        _seed_session(conn, scope_id, "ses_b")
        for _ in range(2):
            messages_service.append(
                conn, scope_id=scope_id, session_id="ses_a", platform="avibe",
                author="agent", message_type="result", text="a",
            )
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_b", platform="avibe",
            author="agent", message_type="result", text="b",
        )
        # An unread assistant (intermediate) and a user message
        # must NOT count toward the unread badge.
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_b", platform="avibe",
            author="agent", message_type="assistant", text="thinking",
        )
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_b", platform="avibe",
            author="user", message_type="user", text="hi",
        )

    with engine.connect() as conn:
        by_session = messages_service.unread_counts_by_session(conn, platform="avibe")
        by_scope = messages_service.unread_counts(conn, platform="avibe")

    assert by_session == {"ses_a": 2, "ses_b": 1}
    # Scope aggregate still lumps both sessions together (result-only).
    assert by_scope == {scope_id: 3}


def test_total_unread_sums_all_sessions(isolated_state):
    """total_unread is the global sum of per-session unread results — the number
    the Inbox nav badge and the installed PWA's app-icon badge both show."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_a")
        _seed_session(conn, scope_id, "ses_b")
        for _ in range(2):
            messages_service.append(
                conn, scope_id=scope_id, session_id="ses_a", platform="avibe",
                author="agent", message_type="result", text="a",
            )
        messages_service.append(
            conn, scope_id=scope_id, session_id="ses_b", platform="avibe",
            author="agent", message_type="result", text="b",
        )

    with engine.connect() as conn:
        assert messages_service.total_unread(conn, platform="avibe") == 3


def test_list_inbox_sessions_unread_count_matches_session_badges(isolated_state):
    """Inbox cards and sidebar badges use the same result-only unread semantics
    for every visible, non-archived session."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_a", "A")
        _seed_titled_session(conn, scope_id, "ses_b", "B")
        _seed_titled_session(conn, scope_id, "ses_notify", "Notify")
        _seed_titled_session(conn, scope_id, "ses_archived", "Archived")
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == "ses_archived").values(status="archived"))

        _insert_msg(conn, scope_id, "ses_a", "agent", "A result", "2026-05-30T10:00:00Z", read=False)
        _insert_msg(conn, scope_id, "ses_a", "agent", "thinking", "2026-05-30T10:01:00Z", read=False, msg_type="assistant")
        _insert_msg(conn, scope_id, "ses_b", "agent", "B1", "2026-05-30T10:02:00Z", read=False)
        _insert_msg(conn, scope_id, "ses_b", "agent", "B2", "2026-05-30T10:03:00Z", read=False)
        _insert_msg(conn, scope_id, "ses_notify", "agent", "failed", "2026-05-30T10:04:00Z", read=False, msg_type="notify")
        _insert_msg(conn, scope_id, "ses_archived", "agent", "old", "2026-05-30T10:05:00Z", read=False)

    with engine.connect() as conn:
        feed = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]
        by_session = messages_service.unread_counts_by_session(conn, platform="avibe")

    feed_counts = {row["session_id"]: row["unread_count"] for row in feed}
    assert feed_counts == {"ses_notify": 0, "ses_b": 2, "ses_a": 1}
    assert by_session == {"ses_a": 1, "ses_b": 2}
    assert all(row["unread_count"] == by_session.get(row["session_id"], 0) for row in feed)


def _seed_titled_session(conn, scope_id: str, session_id: str, title: str) -> None:
    now = messages_service._utc_now_iso()
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
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


def _insert_msg(conn, scope_id, session_id, author, text, created_at, *, read=True, msg_type=None, msg_id=None):
    """Direct insert so the test controls created_at (second-resolution) + read_at.

    Agent rows default to type='result' (the user-facing reply the inbox
    previews); pass ``msg_type`` to insert an intermediate type (assistant /
    tool_call) that must NOT drive the inbox preview. Pass ``msg_id`` to control
    the (time-sortable) id when a test needs a deterministic same-second order.
    """
    resolved_type = msg_type or ("user" if author == "user" else "result")
    conn.execute(
        messages.insert().values(
            id=msg_id or f"msg_{session_id}_{created_at[-9:]}_{author}_{resolved_type}",
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author=author,
            type=resolved_type,
            content_text=text,
            content_json="{}",
            metadata_json="{}",
            created_at=created_at,
            updated_at=created_at,
            read_at=created_at if (read and author == "agent") else None,
        )
    )


def test_list_inbox_sessions_per_session_feed(isolated_state):
    """One card per session, sorted by last activity (any author) desc, preview =
    latest agent reply, replied = awaiting the agent (the user's latest message is
    newer than the agent's latest reply), with unread counts."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        conn.execute(scopes.update().where(scopes.c.id == scope_id).values(display_name="My Project"))
        _seed_titled_session(conn, scope_id, "ses_a", "Alpha")
        _seed_titled_session(conn, scope_id, "ses_b", "Beta")
        _seed_titled_session(conn, scope_id, "ses_c", "Gamma")
        # ses_a: agent reply (read), then a newer user message → awaiting the
        # agent (replied=True), no unread.
        _insert_msg(conn, scope_id, "ses_a", "agent", "A1", "2026-05-30T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_a", "user", "AU", "2026-05-30T10:05:00Z")
        # ses_b: two result replies, the second unread → most recent activity, unread=1.
        _insert_msg(conn, scope_id, "ses_b", "agent", "B1", "2026-05-30T10:01:00Z")
        _insert_msg(conn, scope_id, "ses_b", "agent", "B2", "2026-05-30T10:10:00Z", read=False)
        # An intermediate assistant message arrives LAST — it must bump the
        # activity clock (sort key) but NOT become the preview (preview = result).
        _insert_msg(
            conn, scope_id, "ses_b", "agent", "thinking…", "2026-05-30T10:11:00Z",
            read=False, msg_type="assistant",
        )
        # ses_c: only a user message, no agent reply → excluded from the feed.
        _insert_msg(conn, scope_id, "ses_c", "user", "CU", "2026-05-30T10:20:00Z")

    with engine.connect() as conn:
        feed = messages_service.list_inbox_sessions(conn, platform="avibe")

    rows = feed["sessions"]
    # ses_c excluded (no agent reply); ses_b before ses_a (10:10 > 10:05).
    assert [r["session_id"] for r in rows] == ["ses_b", "ses_a"]

    b, a = rows[0], rows[1]
    assert b["title"] == "Beta" and b["project_name"] == "My Project" and b["project_id"] == "proj_test"
    assert b["preview_text"] == "B2" and b["unread_count"] == 1 and b["unread"] is True
    assert b["replied"] is False  # agent replied last → not awaiting
    # ses_a: preview is the latest AGENT reply (A1), not the user's last message.
    assert a["preview_text"] == "A1" and a["unread_count"] == 0
    assert a["replied"] is True  # user's message is newer than the agent's reply → awaiting

    # Unread filter drops the fully-read ses_a.
    with engine.connect() as conn:
        unread_feed = messages_service.list_inbox_sessions(conn, platform="avibe", unread_only=True)
    assert [r["session_id"] for r in unread_feed["sessions"]] == ["ses_b"]

    # The sidebar badge map agrees with the feed cards' result-only unread_count
    # — the unread intermediate 'thinking' assistant row at 10:11 must NOT make
    # the two sources disagree (1 vs 2).
    with engine.connect() as conn:
        by_session = messages_service.unread_counts_by_session(conn, platform="avibe")
    assert by_session == {"ses_b": 1}
    assert b["unread_count"] == by_session["ses_b"]


def test_inbox_and_unread_queries_exclude_background_sessions(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_foreground", "Foreground")
        _seed_titled_session(conn, scope_id, "ses_background", "Background")
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == "ses_background")
            .values(visibility="background")
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_foreground",
            "agent",
            "visible",
            "2026-05-30T10:00:00Z",
            read=False,
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_background",
            "agent",
            "hidden",
            "2026-05-30T10:01:00Z",
            read=False,
        )

    with engine.connect() as conn:
        feed = messages_service.list_inbox_sessions(conn, platform="avibe")
        unread = messages_service.unread_counts_by_session(conn, platform="avibe")
        hidden = messages_service.get_inbox_session(conn, "ses_background")

    assert [row["session_id"] for row in feed["sessions"]] == ["ses_foreground"]
    assert unread == {"ses_foreground": 1}
    assert hidden is None


def test_list_inbox_sessions_only_session_returns_row_past_window(isolated_state):
    """``only_session`` fetches one specific foreground session regardless of how
    many other sessions are more recently active — the path the Inbox foreground
    reconcile (contract A6) uses to re-add a restored session that sorts past the
    paged window."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_old", "Old")
        _insert_msg(conn, scope_id, "ses_old", "agent", "older", "2026-05-30T09:00:00Z", read=True)
        # Two newer sessions that fill a small window ahead of ses_old.
        for idx, ts in enumerate(("2026-05-30T10:00:00Z", "2026-05-30T11:00:00Z")):
            sid = f"ses_new{idx}"
            _seed_titled_session(conn, scope_id, sid, f"New{idx}")
            _insert_msg(conn, scope_id, sid, "agent", "newer", ts, read=True)

    with engine.connect() as conn:
        windowed = messages_service.list_inbox_sessions(conn, platform="avibe", limit=1)
        targeted = messages_service.list_inbox_sessions(
            conn, platform="avibe", only_session="ses_old", limit=1
        )

    assert "ses_old" not in [row["session_id"] for row in windowed["sessions"]]
    assert [row["session_id"] for row in targeted["sessions"]] == ["ses_old"]


def test_list_inbox_sessions_includes_notify_only_failed_turn(isolated_state):
    """A turn that fails before producing any ``result`` persists only a terminal
    ``notify``; that failed conversation must still surface in the inbox (with the
    error as preview) instead of vanishing once the user leaves the Chat page."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_fail", "Failed")
        _insert_msg(conn, scope_id, "ses_fail", "user", "do the thing", "2026-05-30T10:00:00Z")
        # No ``result`` ever lands — only a terminal failure notify.
        _insert_msg(
            conn, scope_id, "ses_fail", "agent", "❌ Claude error: boom",
            "2026-05-30T10:00:05Z", read=False, msg_type="notify",
        )

    with engine.connect() as conn:
        rows = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]

    assert [r["session_id"] for r in rows] == ["ses_fail"]
    row = rows[0]
    # Preview is the failure notify so the user sees WHY the turn ended.
    assert row["preview_text"] == "❌ Claude error: boom"
    # The agent's notify reply (10:00:05) is newer than the user's message
    # (10:00:00) → the agent has responded, so the session is not awaiting.
    assert row["replied"] is False


def test_list_inbox_sessions_awaiting_reply_persists_through_agent_stream(isolated_state):
    """``replied`` is the persistent "awaiting the agent" flag: it stays True for
    the whole agent turn — even after the agent starts streaming intermediate
    ``assistant`` / ``tool_call`` rows — and clears only when an agent *reply*
    (result / notify) lands after the user's latest message. A "who literally
    spoke last" check would wrongly flip to False the instant streaming begins."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_wait", "Waiting")
        # turn 1: user asks, agent replies (read).
        _insert_msg(conn, scope_id, "ses_wait", "user", "first", "2026-05-30T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_wait", "agent", "R1", "2026-05-30T10:01:00Z")
        # turn 2: user follows up; the agent is mid-stream (assistant chunk) but
        # has NOT produced a new result yet.
        _insert_msg(conn, scope_id, "ses_wait", "user", "second", "2026-05-30T10:05:00Z")
        _insert_msg(
            conn, scope_id, "ses_wait", "agent", "thinking…", "2026-05-30T10:06:00Z",
            read=False, msg_type="assistant",
        )

    with engine.connect() as conn:
        row = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"][0]

    # Awaiting the agent: the user's latest message (10:05) is newer than the
    # agent's latest *reply* (R1 @10:01) — the streaming chunk doesn't count.
    assert row["replied"] is True
    # …even though the agent's streaming row is literally the last message,
    assert row["last_message_author"] == "agent"
    # …and the preview is still the last completed reply, not the stream chunk.
    assert row["preview_text"] == "R1"

    # Once the agent's new result lands after the user's message, it clears.
    with engine.begin() as conn:
        _insert_msg(conn, scope_id, "ses_wait", "agent", "R2", "2026-05-30T10:07:00Z", read=False)
    with engine.connect() as conn:
        row2 = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"][0]
    assert row2["replied"] is False
    assert row2["preview_text"] == "R2"


def test_list_inbox_sessions_silent_completion_clears_awaiting(isolated_state):
    """A terminal Turn snapshot settles Inbox state without a pseudo Message."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_sil", "Silent")
        _insert_msg(conn, scope_id, "ses_sil", "user", "first", "2026-05-30T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_sil", "agent", "R1", "2026-05-30T10:01:00Z")  # visible reply
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_silent_turn",
            session_id="ses_sil",
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id="ses_sil",
                platform="avibe",
                author="user",
                source="user",
                text="second",
            ),
            dispatch_text="second",
            now="2026-05-30T10:05:00Z",
        )
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id="trn_silent_turn",
            session_id="ses_sil",
            backend="opencode",
            deliveries=[delivery],
            dispatch_text="second",
            attempt_id="atm_silent_turn",
        )
        assert message_deliveries.bind_native_start(
            conn,
            "trn_silent_turn",
            expected_version=int(claimed["turn"]["version"]),
            runtime_key="silent-runtime",
            runtime_turn_id="silent-runtime-turn",
            native_turn_id="silent-native-turn",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(
            conn,
            turn_id="trn_silent_turn",
            evidence={"kind": "native_start"},
        )
        message_deliveries.terminalize_turn(
            conn,
            "trn_silent_turn",
            outcome="completed",
            settled_by="terminal_result",
            evidence_kind="result",
        )

    with engine.connect() as conn:
        row = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"][0]
    assert row["replied"] is False  # the silent completion answered the follow-up
    assert row["preview_text"] == "R1"  # preview stays the last VISIBLE reply


def test_list_inbox_sessions_counts_harness_prompt_as_pending_input(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_harness", "Automated")
        _insert_msg(conn, scope_id, "ses_harness", "agent", "R1", "2026-05-30T10:00:00Z")
        _insert_msg(
            conn,
            scope_id,
            "ses_harness",
            "harness",
            "scheduled follow-up",
            "2026-05-30T10:01:00Z",
            msg_type=messages_service.HARNESS_TYPE,
        )

    with engine.connect() as conn:
        pending = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"][0]

    assert pending["replied"] is True
    assert pending["last_message_author"] == "harness"
    assert pending["preview_text"] == "R1"

    with engine.begin() as conn:
        _insert_msg(conn, scope_id, "ses_harness", "agent", "R2", "2026-05-30T10:02:00Z")
    with engine.connect() as conn:
        completed = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"][0]

    assert completed["replied"] is False
    assert completed["preview_text"] == "R2"


def test_list_inbox_sessions_counts_dispatching_annotation_as_pending_input(
    isolated_state,
):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_annotation", "Show review")
        _insert_msg(
            conn,
            scope_id,
            "ses_annotation",
            "agent",
            "R1",
            "2026-05-30T10:00:00Z",
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_annotation",
            "harness",
            "review this heading",
            "2026-05-30T10:01:00Z",
            msg_type=messages_service.ANNOTATION_TYPE,
        )

    with engine.connect() as conn:
        pending = messages_service.list_inbox_sessions(
            conn,
            platform="avibe",
        )["sessions"][0]

    assert pending["replied"] is True
    assert pending["last_message_author"] == "harness"


def test_list_inbox_sessions_same_second_followup_uses_id_tiebreaker(isolated_state):
    """``created_at`` is second-resolution, so a follow-up sent in the SAME second
    as the prior agent reply ties on time; the time-sortable message id breaks the
    tie (real ids carry a microsecond-clock prefix). A later user id ⇒ still
    awaiting the agent; a later agent-reply id ⇒ already replied. A plain
    timestamp ``>`` would miss the awaiting case."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        # ses_wait_tie: agent reply, then the user's same-second follow-up (later id).
        _seed_titled_session(conn, scope_id, "ses_wait_tie", "WaitTie")
        _insert_msg(conn, scope_id, "ses_wait_tie", "agent", "R", "2026-05-30T10:00:00Z", msg_id="msg_00000001")
        _insert_msg(conn, scope_id, "ses_wait_tie", "user", "again", "2026-05-30T10:00:00Z", msg_id="msg_00000002")
        # ses_done_tie: user asks, then the agent's same-second reply (later id).
        _seed_titled_session(conn, scope_id, "ses_done_tie", "DoneTie")
        _insert_msg(conn, scope_id, "ses_done_tie", "user", "ask", "2026-05-30T10:00:00Z", msg_id="msg_00000003")
        _insert_msg(conn, scope_id, "ses_done_tie", "agent", "R", "2026-05-30T10:00:00Z", msg_id="msg_00000004")

    with engine.connect() as conn:
        rows = {
            r["session_id"]: r
            for r in messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]
        }

    # Same second, user's message has the later id → still awaiting the agent.
    assert rows["ses_wait_tie"]["replied"] is True
    # Same second, the agent reply has the later id → already replied.
    assert rows["ses_done_tie"]["replied"] is False


def test_list_inbox_sessions_pagination(isolated_state):
    """Keyset 'load more' walks sessions in last-activity order."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        for i in range(3):
            sid = f"ses_{i}"
            _seed_titled_session(conn, scope_id, sid, f"S{i}")
            _insert_msg(conn, scope_id, sid, "agent", f"reply {i}", f"2026-05-30T1{i}:00:00Z")

    with engine.connect() as conn:
        page1 = messages_service.list_inbox_sessions(conn, platform="avibe", limit=2)
        assert [r["session_id"] for r in page1["sessions"]] == ["ses_2", "ses_1"]
        assert page1["next_cursor"]
        page2 = messages_service.list_inbox_sessions(
            conn, platform="avibe", limit=2, before=page1["next_cursor"]
        )
    assert [r["session_id"] for r in page2["sessions"]] == ["ses_0"]


# --- Send-while-busy queue + per-session draft ------------------------------


def test_enqueue_list_and_retire_queued(isolated_state):
    """Queued Deliveries persist FIFO, stay out of transcript, and retire in place."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_q")
        first = message_deliveries.enqueue_queued(conn, scope_id=scope_id, session_id="ses_q", text="first")
        time.sleep(0.001)
        message_deliveries.enqueue_queued(conn, scope_id=scope_id, session_id="ses_q", text="second")

    with engine.connect() as conn:
        queued = message_deliveries.list_queued(conn, "ses_q")
        # Queued rows never appear in the user/result/notify transcript.
        transcript = messages_service.list_session_messages(
            conn, session_id="ses_q", types=("user", "result", "notify")
        )
    assert [q["text"] for q in queued] == ["first", "second"]
    assert all(q["state"] == "queued" for q in queued)
    assert transcript["messages"] == []

    with engine.begin() as conn:
        assert message_deliveries.retire_queued(conn, "ses_q", first["id"])
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, "ses_q")] == ["second"]
        assert message_deliveries.get_delivery(conn, first["id"])["state"] == "retired"


def test_remove_queued_targets_only_queued(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_rm")
        a = message_deliveries.enqueue_queued(conn, scope_id=scope_id, session_id="ses_rm", text="a")
        message_deliveries.enqueue_queued(conn, scope_id=scope_id, session_id="ses_rm", text="b")
        # A real user message must NOT be removable through remove_queued.
        user_row = messages_service.append(
            conn, scope_id=scope_id, session_id="ses_rm", platform="avibe", author="user", text="real"
        )

    with engine.begin() as conn:
        assert message_deliveries.retire_queued_with_run(conn, "ses_rm", a["id"]) is True
        # Wrong session id must NOT delete the row (scoped delete).
        assert message_deliveries.retire_queued_with_run(conn, "ses_other", a["id"]) is False
        # A real user message is not removable through remove_queued.
        assert message_deliveries.retire_queued_with_run(conn, "ses_rm", user_row["id"]) is False
    with engine.connect() as conn:
        assert [q["text"] for q in message_deliveries.list_queued(conn, "ses_rm")] == ["b"]


def test_remove_queued_cancels_only_its_exact_agent_run(isolated_state):
    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import (
        attach_agent_run_delivery_in_connection,
        claim_agent_runs_for_turn_in_connection,
        run_update_event_transaction,
    )

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_rm_run")

    store = TaskExecutionStore()
    requests = [
        store.enqueue_agent_run(
            session_id="ses_rm_run",
            message=f"obsolete delegated work {index}",
            agent_name="worker",
        )
        for index in range(2)
    ]
    primary, sibling = requests
    deliveries = []
    with engine.begin() as conn:
        for request in requests:
            delivery = message_deliveries.enqueue_queued(
                conn,
                scope_id=scope_id,
                session_id="ses_rm_run",
                author="harness",
                source="harness",
                text=request.message or "",
                native_message_id=f"agent_run:{request.id}",
            )
            assert attach_agent_run_delivery_in_connection(
                conn,
                request.id,
                session_id="ses_rm_run",
                delivery_id=str(delivery["id"]),
            )
            deliveries.append(delivery)
        assert claim_agent_runs_for_turn_in_connection(conn, [primary.id]) == [
            primary.id
        ]

    with run_update_event_transaction(engine) as conn:
        assert (
            message_deliveries.retire_queued_with_run(
                conn,
                "ses_rm_run",
                deliveries[0]["id"],
            )
            is True
        )

    canceled = store.get_run(primary.id)
    retained = store.get_run(sibling.id)
    assert canceled["status"] == "canceled"
    assert canceled["cancel_requested"] is True
    assert canceled["completed_at"]
    assert retained["status"] == "queued"
    assert retained["cancel_requested"] is False
    with engine.connect() as conn:
        assert [
            row["id"] for row in message_deliveries.list_queued(conn, "ses_rm_run")
        ] == [deliveries[1]["id"]]


def test_bulk_delivery_run_cancellation_defers_updated_rows(isolated_state):
    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import (
        attach_agent_run_delivery_in_connection,
        cancel_agent_runs_for_retired_deliveries_in_connection,
        pop_deferred_run_event_rows_from_connection,
    )

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_bulk_cancel")
    store = TaskExecutionStore()
    request = store.enqueue_agent_run(
        session_id="ses_bulk_cancel",
        message="retired delegated work",
        agent_name="worker",
    )
    with engine.begin() as conn:
        delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_bulk_cancel",
            author="harness",
            source="harness",
            text=request.message or "",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            request.id,
            session_id="ses_bulk_cancel",
            delivery_id=str(delivery["id"]),
        )
        assert cancel_agent_runs_for_retired_deliveries_in_connection(
            conn,
            session_id="ses_bulk_cancel",
            delivery_ids=[str(delivery["id"])],
        ) == [request.id]
        deferred = pop_deferred_run_event_rows_from_connection(conn)

    assert [(row["id"], row["status"]) for row in deferred] == [
        (request.id, "canceled")
    ]


def test_remove_queued_refuses_an_agent_run_no_longer_owned_by_queue(
    isolated_state,
):
    from core.scheduled_tasks import TaskExecutionStore
    from storage.background import (
        attach_agent_run_delivery_in_connection,
        claim_agent_runs_for_turn_in_connection,
    )

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_rm_claimed")

    store = TaskExecutionStore()
    request = store.enqueue_agent_run(
        session_id="ses_rm_claimed",
        message="already claimed work",
        agent_name="worker",
    )
    with engine.begin() as conn:
        queued = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_rm_claimed",
            author="harness",
            source="harness",
            text=request.message or "",
            native_message_id=f"agent_run:{request.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            request.id,
            session_id="ses_rm_claimed",
            delivery_id=str(queued["id"]),
        )
        assert claim_agent_runs_for_turn_in_connection(conn, [request.id]) == [
            request.id
        ]
        message_deliveries.claim_start_batch(
            conn,
            turn_id=message_deliveries.new_turn_id(),
            session_id="ses_rm_claimed",
            backend="worker",
            deliveries=[queued],
            dispatch_text=request.message or "",
        )

    with engine.begin() as conn:
        assert (
            message_deliveries.retire_queued_with_run(
                conn,
                "ses_rm_claimed",
                queued["id"],
            )
            is False
        )

    stored = store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["cancel_requested"] is False
    with engine.connect() as conn:
        claimed = message_deliveries.get_delivery(conn, queued["id"])
    assert claimed is not None and claimed["state"] == "claimed"


def test_list_queued_page_is_bounded_and_fifo(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_page")
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_page",
            text="first",
        )
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_page",
            text="second",
        )
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_page",
            text="third",
        )

    with engine.connect() as conn:
        page1 = message_deliveries.list_queued_page(
            conn,
            "ses_page",
            page_request=PageRequest(page=1, limit=2),
        )
        page2 = message_deliveries.list_queued_page(
            conn,
            "ses_page",
            page_request=PageRequest(page=2, limit=2),
        )

    assert [row["text"] for row in page1.items] == ["first", "second"]
    assert page1.page == 1
    assert page1.limit == 2
    assert page1.has_more is True
    assert [row["text"] for row in page2.items] == ["third"]
    assert page2.page == 2
    assert page2.limit == 2
    assert page2.has_more is False


def test_draft_upsert_get_and_clear(isolated_state):
    """A session keeps exactly one draft row; setting replaces it, blank clears,
    and the draft never shows in the transcript."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_d")
        message_deliveries.set_draft(conn, "ses_d", "half typed")

    with engine.connect() as conn:
        draft = message_deliveries.get_draft(conn, "ses_d")
    assert draft is not None and draft["text"] == "half typed"

    # Setting again replaces in place (still exactly one draft row).
    with engine.begin() as conn:
        message_deliveries.set_draft(conn, "ses_d", "rewritten")
    with engine.connect() as conn:
        rows = conn.execute(select(messages).where(messages.c.session_id == "ses_d")).all()
        draft = message_deliveries.get_draft(conn, "ses_d")
    assert rows == [] and draft["text"] == "rewritten"

    # Blank text clears the draft.
    with engine.begin() as conn:
        assert message_deliveries.set_draft(conn, "ses_d", "   ") is True
    with engine.connect() as conn:
        assert message_deliveries.get_draft(conn, "ses_d") is None

    # clear_draft is idempotent.
    with engine.begin() as conn:
        message_deliveries.set_draft(conn, "ses_d", "again")
    with engine.begin() as conn:
        message_deliveries.set_draft(conn, "ses_d", None)
    with engine.connect() as conn:
        assert message_deliveries.get_draft(conn, "ses_d") is None


def test_inbox_ignores_draft_and_queued_delivery_activity(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_inbox")
        _insert_msg(conn, scope_id, "ses_inbox", "user", "hi", "2026-05-30T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_inbox", "agent", "reply", "2026-05-30T10:00:01Z")
        message_deliveries.set_draft(conn, "ses_inbox", "typing")
        message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id="ses_inbox",
            text="queued",
            now="2026-05-30T10:06:00Z",
        )

    with engine.connect() as conn:
        rows = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]
    assert len(rows) == 1
    row = rows[0]
    # Activity clock = the agent reply, NOT the later draft/queued rows.
    assert row["last_activity_at"] == "2026-05-30T10:00:01Z"
    assert row["last_message_author"] == "agent"
    assert row["replied"] is False


def test_inbox_ignores_not_written_successor_as_terminal_evidence(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(
            conn,
            scope_id,
            "ses_not_written_inbox",
            "Not written",
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_not_written_inbox",
            "agent",
            "previous reply",
            "2026-05-30T10:00:00Z",
        )
        _insert_msg(
            conn,
            scope_id,
            "ses_not_written_inbox",
            "user",
            "still running",
            "2026-05-30T10:01:00Z",
        )
        successor = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_not_written_inbox",
            session_id="ses_not_written_inbox",
            priority="p0",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id="ses_not_written_inbox",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="refused replacement",
            ),
            dispatch_text="refused replacement",
            now="2026-05-30T10:02:00Z",
        )
        message_deliveries.insert_turn(
            conn,
            turn_id="trn_not_written_inbox",
            session_id="ses_not_written_inbox",
            initial_delivery_id="msg_not_written_inbox",
            state="waiting",
            backend="codex",
            now="2026-05-30T10:02:00Z",
        )
        assert message_deliveries.cas_delivery(
            conn,
            successor["id"],
            expected_version=int(successor["version"]),
            expected_states=("reserved",),
            values={
                "state": "interrupt_waiting",
                "turn_id": "trn_not_written_inbox",
                "turn_role": "initial",
                "turn_position": 0,
            },
        ) is not None
        message_deliveries.terminalize_turn(
            conn,
            "trn_not_written_inbox",
            outcome="not_written",
            settled_by="interrupt_refused",
            evidence_kind="definitive_stop_receipt",
        )

    with engine.connect() as conn:
        rows = messages_service.list_inbox_sessions(conn, platform="avibe")[
            "sessions"
        ]

    assert len(rows) == 1
    assert rows[0]["session_id"] == "ses_not_written_inbox"
    assert rows[0]["replied"] is True


def test_inbox_awaiting_uses_active_accepted_turn_not_submission_order(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_titled_session(conn, scope_id, "ses_delayed_accept", "Delayed acceptance")
        _insert_msg(
            conn,
            scope_id,
            "ses_delayed_accept",
            "agent",
            "previous reply",
            "2026-05-30T10:02:00Z",
        )
        delivery_id = "msg_delayed_accept"
        turn_id = "trn_delayed_accept"
        attempt_id = "atm_delayed_accept"
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_delayed_accept",
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id="ses_delayed_accept",
                platform="avibe",
                author="user",
                source="user",
                text="submitted before the previous turn completed",
            ),
            dispatch_text="submitted before the previous turn completed",
            now="2026-05-30T10:01:00Z",
        )
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_delayed_accept",
            backend="codex",
            deliveries=[delivery],
            dispatch_text="submitted before the previous turn completed",
            attempt_id=attempt_id,
        )
        assert message_deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=int(claimed["turn"]["version"]),
            runtime_key="delayed-runtime",
            runtime_turn_id="delayed-runtime-turn",
            native_turn_id="delayed-native-turn",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "delayed_start_acceptance"},
        )

    with engine.connect() as conn:
        row = messages_service.list_inbox_sessions(
            conn,
            platform="avibe",
        )["sessions"][0]

    assert row["replied"] is True


def test_list_session_messages_tail_returns_recent_window(isolated_state):
    """``tail=True`` returns the most-recent ``limit`` rows in chronological
    order (not the oldest page), so the Chat page's gap recovery sees the latest
    messages even in a long session."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_tail")
        for i in range(5):
            _insert_msg(conn, scope_id, "ses_tail", "user", f"m{i}", f"2026-05-30T10:0{i}:00Z")

    with engine.connect() as conn:
        oldest = messages_service.list_session_messages(conn, session_id="ses_tail", limit=3)
        recent = messages_service.list_session_messages(conn, session_id="ses_tail", limit=3, tail=True)
    # Default page = oldest 3; tail = newest 3, still chronological.
    assert [m["text"] for m in oldest["messages"]] == ["m0", "m1", "m2"]
    assert [m["text"] for m in recent["messages"]] == ["m2", "m3", "m4"]
    assert recent["next_after_id"] is None


def test_list_session_messages_before_id_returns_older_window(isolated_state):
    """The chat page opens on the recent tail, then pages upward from the first
    loaded row to recover the older transcript without replacing the tail."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_before")
        for i in range(6):
            _insert_msg(conn, scope_id, "ses_before", "user", f"m{i}", f"2026-05-30T10:0{i}:00Z")

    with engine.connect() as conn:
        recent = messages_service.list_session_messages(conn, session_id="ses_before", limit=2, tail=True)
        older = messages_service.list_session_messages(
            conn,
            session_id="ses_before",
            limit=2,
            before_id=recent["next_before_id"],
        )
        oldest = messages_service.list_session_messages(
            conn,
            session_id="ses_before",
            limit=2,
            before_id=older["next_before_id"],
        )

    assert [m["text"] for m in recent["messages"]] == ["m4", "m5"]
    assert [m["text"] for m in older["messages"]] == ["m2", "m3"]
    assert [m["text"] for m in oldest["messages"]] == ["m0", "m1"]
    assert oldest["next_before_id"] is None


def test_quick_reply_choice_recorded_on_agent_message_once(isolated_state):
    """The chosen quick-reply is recorded on the AGENT message itself (the single
    source of truth for the locked/answered state), once, and only for offered
    options."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "sess_qr")
        row = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="sess_qr",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Pick one",
            content={"kind": "result", "quick_replies": ["Yes", "No"]},
        )
        mid = row["id"]

    with engine.begin() as conn:
        assert messages_service.get_quick_reply_chosen(conn, "sess_qr", mid) is None
        # An option that wasn't offered is rejected.
        assert messages_service.set_quick_reply_chosen(conn, "sess_qr", mid, "Maybe") is False
        # A real option records once.
        assert messages_service.set_quick_reply_chosen(conn, "sess_qr", mid, "Yes") is True
        assert messages_service.get_quick_reply_chosen(conn, "sess_qr", mid) == "Yes"
        # Set-once / idempotent: a second answer is rejected and does not overwrite.
        assert messages_service.set_quick_reply_chosen(conn, "sess_qr", mid, "No") is False
        assert messages_service.get_quick_reply_chosen(conn, "sess_qr", mid) == "Yes"
        # Scoped to the session: another session's id must not read/lock this row.
        assert messages_service.get_quick_reply_chosen(conn, "other_sess", mid) is None
        assert messages_service.set_quick_reply_chosen(conn, "other_sess", mid, "No") is False

    # The recorded choice is visible through the normal read path (so the UI locks
    # + highlights on reload).
    with engine.connect() as conn:
        loaded = messages_service.list_session_messages(conn, session_id="sess_qr")["messages"]
        assert loaded[0]["content"].get("quick_reply_chosen") == "Yes"
        # Unknown message id → no choice, no crash.
        assert messages_service.get_quick_reply_chosen(conn, "sess_qr", "does-not-exist") is None
