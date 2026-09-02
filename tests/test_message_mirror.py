"""Unit tests for the cross-platform message mirror + unified agent persist.

Covers the contract that ``MessageHandler`` / ``ConsolidatedMessageDispatcher``
rely on:

* a fresh ``(platform, channel_id)`` auto-upserts as a 'channel'-typed scope on
  first inbound mirror, writing an author='user', type='user' row,
* ``persist_agent_message`` lands an author='agent' row (typed) on the same
  scope for the live reply,
* repeated inbound mirror calls with the same ``native_message_id`` are
  idempotent,
* ``mirror_inbound`` is a no-op for ``platform='avibe'`` (the workbench REST
  writer owns the user row), while ``persist_agent_message`` DOES persist avibe
  agent output (unified store).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_mirror import (
    agent_message_exists,
    mirror_harness_inbound,
    mirror_inbound,
    persist_agent_message,
    persist_silent_terminal,
)
from modules.im import MessageContext
from storage import messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, agent_sessions, media_objects, messages, scopes
from storage.settings_service import upsert_scope


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


# The topics that fan message CONTENT out to an open browser: the Chat transcript
# row and the Inbox card preview. Named once as a class so a test can assert "this
# row did not stream" by naming what must not appear, instead of asserting the bus
# stayed silent — ``persist_agent_message`` also publishes contentless events on
# the same path for unrelated concerns (the session-list rank), and a test that
# reads silence as its property fails on the next one of those to be added while
# proving nothing more about content.
CONTENT_TOPICS = frozenset({"message.new", "inbox.session.updated"})


async def _drain_published(queue: asyncio.Queue, *, quiet: float = 0.1) -> dict[str, dict]:
    """Everything published to ``queue``, keyed by topic.

    Drains until the bus goes quiet rather than reading a fixed count: a count
    silently re-points a test's assertions at whichever events happen to arrive
    first, so ``for _ in range(2)`` starts failing when an unrelated third topic
    is published — not because the asserted property broke, but because the test
    stopped looking at it. ``bus.publish`` enqueues synchronously, so the quiet
    window is paid once and only after every event is already in the queue.
    """

    events: dict[str, dict] = {}
    while True:
        try:
            topic, payload = await asyncio.wait_for(queue.get(), timeout=quiet)
        except asyncio.TimeoutError:
            return events
        events[topic] = payload


def _slack_ctx(message_id="m_001") -> MessageContext:
    return MessageContext(
        user_id="U_alice",
        channel_id="C_general",
        platform="slack",
        thread_id=None,
        message_id=message_id,
    )


def test_inbound_creates_scope_and_user_row(isolated_state):
    mirror_inbound(_slack_ctx(), "hello there")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        scope_row = conn.execute(
            select(scopes).where(scopes.c.platform == "slack", scopes.c.native_id == "C_general")
        ).mappings().first()
        assert scope_row is not None
        assert scope_row["scope_type"] == "channel"

        message_rows = conn.execute(
            select(messages).where(messages.c.platform == "slack")
        ).mappings().all()
        assert len(message_rows) == 1
        assert message_rows[0]["author"] == "user"
        assert message_rows[0]["type"] == "user"
        assert message_rows[0]["content_text"] == "hello there"
        assert message_rows[0]["author_id"] == "U_alice"


def test_telegram_dm_mirror_uses_user_scope_when_chat_id_equals_user_id(isolated_state):
    ctx = MessageContext(
        user_id="58181121",
        channel_id="58181121",
        platform="telegram",
        message_id="101",
        platform_specific={"is_dm": True, "platform": "telegram"},
    )

    mirror_inbound(ctx, "hello")
    persist_agent_message(ctx, "result", "hi")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        scope_row = conn.execute(
            select(scopes).where(scopes.c.platform == "telegram", scopes.c.native_id == "58181121")
        ).mappings().one()
        rows = conn.execute(select(messages).where(messages.c.platform == "telegram")).mappings().all()

    assert scope_row["scope_type"] == "user"
    assert {row["scope_id"] for row in rows} == {"telegram::user::58181121"}


def test_persist_agent_writes_typed_agent_row_on_same_scope(isolated_state):
    ctx = _slack_ctx()
    mirror_inbound(ctx, "ping")
    persist_agent_message(ctx, "result", "pong")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(messages).where(messages.c.platform == "slack")
        ).mappings().all()
    # Two separate-second-resolution writes can tie on created_at, so assert by
    # author rather than row order.
    assert {row["author"] for row in rows} == {"user", "agent"}
    agent_row = next(r for r in rows if r["author"] == "agent")
    user_row = next(r for r in rows if r["author"] == "user")
    assert agent_row["content_text"] == "pong"
    assert agent_row["type"] == "result"
    # No session resolved on this synthetic context -> falls back to the
    # channel scope auto-created on first inbound; both rows share it.
    assert agent_row["scope_id"] == user_row["scope_id"]


def test_persist_agent_keeps_result_footer_as_structured_content(isolated_state):
    ctx = _slack_ctx()
    mirror_inbound(ctx, "ping")
    persist_agent_message(
        ctx,
        "result",
        "pong\n\n✅ ⏱️ 5s · 🪙 1.2k tok",
        result_footer="✅ ⏱️ 5s · 🪙 1.2k tok",
    )

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(messages).where(messages.c.author == "agent")
        ).mappings().one()

    content = json.loads(row["content_json"])
    assert content["kind"] == "result"
    assert content["result_footer"] == "✅ ⏱️ 5s · 🪙 1.2k tok"


def test_agent_message_receipt_lookup_returns_text_footer_and_batch(isolated_state):
    ctx = _slack_ctx()
    mirror_inbound(ctx, "ping")
    persist_agent_message(
        ctx,
        "result",
        "Exact accepted assistant result",
        result_footer="3.4s | 812 tok",
        metadata={
            "activity_ids": ["task-a", "task-b"],
            "run_ids": ["run-a", "run-b"],
        },
        native_message_id="activity-batch-receipt",
    )

    accepted = agent_message_exists(ctx, "activity-batch-receipt")

    assert accepted is not None
    assert accepted["text"] == "Exact accepted assistant result"
    assert accepted["content"]["result_footer"] == "3.4s | 812 tok"
    assert accepted["metadata"]["activity_ids"] == ["task-a", "task-b"]


def test_agent_output_provenance_is_hidden_metadata_and_deduplicated(isolated_state):
    ctx = _slack_ctx()
    mirror_inbound(ctx, "ping")

    first = persist_agent_message(
        ctx,
        "result",
        "background result",
        metadata={"activity_id": "task-1", "detached": True},
        native_message_id="agent-output:claude:task-1:completion",
    )
    duplicate = persist_agent_message(
        ctx,
        "result",
        "background result",
        metadata={"activity_id": "task-1", "detached": True},
        native_message_id="agent-output:claude:task-1:completion",
    )

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(messages).where(messages.c.author == "agent")
        ).mappings().all()

    assert first is not None
    assert duplicate is None
    assert len(rows) == 1
    assert rows[0]["content_text"] == "background result"
    assert json.loads(rows[0]["metadata_json"]) == {
        "activity_id": "task-1",
        "detached": True,
    }


def test_visible_replay_promotes_suppressed_nonterminal_output(isolated_state):
    native_message_id = "agent-output:codex:run-1:primary"
    suppressed = MessageContext(
        user_id="U_alice",
        channel_id="C_general",
        platform="slack",
        platform_specific={"suppress_delivery": True},
    )
    visible = MessageContext(
        user_id="U_alice",
        channel_id="C_general",
        platform="slack",
    )

    local = persist_agent_message(
        suppressed,
        "output",
        "local history",
        metadata={"delivery_suppressed": True, "run_id": "run-1"},
        native_message_id=native_message_id,
    )
    promoted = persist_agent_message(
        visible,
        "output",
        "visible reply",
        metadata={"run_id": "run-1"},
        native_message_id=native_message_id,
    )

    assert local is not None
    assert promoted is not None
    assert promoted["id"] == local["id"]
    assert promoted["type"] == "output"
    assert promoted["text"] == "visible reply"
    assert promoted["metadata"] == {"run_id": "run-1"}


def test_persist_agent_reuses_cached_sqlite_engine(isolated_state, monkeypatch):
    import storage.db as sqlite_db

    sqlite_db.dispose_cached_sqlite_engines()
    create_calls = 0
    real_create = sqlite_db.create_sqlite_engine

    def counting_create(db_path=None):
        nonlocal create_calls
        create_calls += 1
        return real_create(db_path)

    monkeypatch.setattr(sqlite_db, "create_sqlite_engine", counting_create)
    ctx = _slack_ctx()

    persist_agent_message(ctx, "assistant", "first stream chunk")
    persist_agent_message(ctx, "assistant", "second stream chunk")

    assert create_calls == 1

    engine = real_create()
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    select(messages)
                    .where(messages.c.platform == "slack", messages.c.author == "agent")
                    .order_by(messages.c.created_at, messages.c.id)
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    assert [row["content_text"] for row in rows] == ["first stream chunk", "second stream chunk"]


def test_persist_agent_toolcall_writes_event_not_message(isolated_state):
    ctx = _slack_ctx()
    mirror_inbound(ctx, "ping")
    persist_agent_message(ctx, "toolcall", "ran a tool")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        agent_row = conn.execute(
            select(messages).where(messages.c.author == "agent")
        ).mappings().first()
        event_row = conn.execute(select(agent_events)).mappings().first()
    assert agent_row is None
    assert event_row["event_type"] == "tool_call"
    assert event_row["visibility"] == "trace"
    assert event_row["platform"] == "slack"
    assert event_row["session_id"] is None
    assert event_row["content_text"] == "ran a tool"


def test_persist_agent_im_uses_delivery_scope_not_session(isolated_state):
    """A routed IM reply (the delivery target differs from the source session's
    channel) is attributed to the DELIVERY channel scope with no session_id, so
    cross-platform history points at where the reply was actually sent — not the
    originating session's channel. (``emit_agent_message`` hands us the
    post-routing target context.)"""
    from storage.models import agent_sessions

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        # Source session lives under channel C_source.
        scope_source = upsert_scope(
            conn, platform="slack", scope_type="channel", native_id="C_source", now=now
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_im",
                scope_id=scope_source,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_im",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    # Delivery target = C_delivery, but agent_session_id still rides along.
    target_ctx = MessageContext(
        user_id="U",
        channel_id="C_delivery",
        platform="slack",
        platform_specific={"agent_session_id": "ses_im"},
    )
    persist_agent_message(target_ctx, "result", "routed answer")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(select(messages).where(messages.c.author == "agent")).mappings().first()
        delivery_scope = conn.execute(
            select(scopes.c.id).where(scopes.c.platform == "slack", scopes.c.native_id == "C_delivery")
        ).scalar_one()
    assert row["scope_id"] == delivery_scope  # delivery channel, NOT C_source
    # IM rows are SCOPE-keyed to the delivery channel but ALSO carry the SOURCE
    # session_id, so a routed reply is queryable both ways. Here they differ:
    # scope = C_delivery, session = ses_im (anchored under C_source).
    assert row["session_id"] == "ses_im"
    assert row["content_text"] == "routed answer"


def test_silent_im_terminal_is_trace_evidence_not_transcript(isolated_state):
    engine = create_sqlite_engine()
    now = "2026-06-01T10:00:00Z"
    with engine.begin() as conn:
        source_scope = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C_source_terminal",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_im_terminal",
                scope_id=source_scope,
                agent_name="codex",
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_ses_im_terminal",
                native_session_id="",
                status="active",
                visibility="foreground",
                pinned=0,
                agent_status="running",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    context = MessageContext(
        user_id="U_terminal",
        channel_id="C_delivery_terminal",
        platform="slack",
        platform_specific={
            "agent_session_id": "ses_im_terminal",
            "turn_token": "turn-im-terminal",
        },
    )

    persist_silent_terminal(context, is_error=True)

    with engine.connect() as conn:
        event = conn.execute(
            select(agent_events).where(
                agent_events.c.session_id == "ses_im_terminal"
            )
        ).mappings().one()
        transcript_row = conn.execute(
            select(messages).where(messages.c.session_id == "ses_im_terminal")
        ).first()
    assert event["event_type"] == "silent_terminal"
    assert json.loads(event["metadata_json"])["terminal_outcome"] == "failed"
    assert transcript_row is None


def test_duplicate_native_message_id_is_swallowed(isolated_state):
    ctx = _slack_ctx(message_id="dup_id")
    mirror_inbound(ctx, "first")
    mirror_inbound(ctx, "duplicate delivery")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(messages).where(messages.c.platform == "slack")).mappings().all()
    # Unique (platform, native_message_id) constraint keeps the second
    # write from materializing.
    assert len(rows) == 1
    assert rows[0]["content_text"] == "first"


def test_persist_agent_publishes_message_and_inbox_for_avibe(isolated_state):
    """An avibe agent ``result`` on a resolved session persists AND publishes
    two bus events: a session-scoped ``message.new`` (the full row, incl.
    source='agent' — feeds an open Chat page) and ``inbox.session.updated`` (the
    card bump). Both ride the controller→browser bridge.
    """
    from core import inbox_events

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_x", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_pub",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_pub",
                native_session_id="",
                title="Published",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench",
        channel_id="ses_pub",
        platform="avibe",
        platform_specific={"agent_session_id": "ses_pub", "vibe_agent_name": "Atlas"},
    )

    notifications = []

    def fake_notify(message, inbox_row):
        notifications.append((message, inbox_row))

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        events = {}
        try:
            from unittest.mock import patch

            with patch("core.web_push_notifications.maybe_notify_inbox_message", fake_notify):
                persist_agent_message(ctx, "result", "final answer")
            events = await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return events

    events = asyncio.run(scenario())

    assert "message.new" in events
    msg = events["message.new"]
    assert msg["session_id"] == "ses_pub"
    assert msg["source"] == "agent"
    assert msg["author_name"] == "Atlas"
    assert msg["text"] == "final answer"

    assert "inbox.session.updated" in events
    card = events["inbox.session.updated"]
    assert card["session_id"] == "ses_pub"
    assert card["preview_text"] == "final answer"
    assert card["title"] == "Published"
    assert notifications[0][0]["text"] == "final answer"
    assert notifications[0][1]["session_id"] == "ses_pub"

    # The row was persisted too (publish is in addition to, not instead of).
    with engine.connect() as conn:
        agent_rows = conn.execute(
            select(messages).where(messages.c.author == "agent", messages.c.session_id == "ses_pub")
        ).mappings().all()
    assert len(agent_rows) == 1 and agent_rows[0]["type"] == "result"


def test_persist_agent_intermediate_persisted_but_not_streamed(isolated_state):
    """An intermediate ``assistant`` (process-log) message is PERSISTED for
    history/debugging, but publishes NEITHER ``message.new`` NOR
    ``inbox.session.updated`` — the live stream carries only transcript types
    (user/result/notify), exactly matching what the history fetch returns, and
    process log is neither streamed nor inbox-eligible (user request).
    """
    from core import inbox_events
    from storage import messages_service

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_y", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_noresult",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_noresult",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench",
        channel_id="ses_noresult",
        platform="avibe",
        platform_specific={"agent_session_id": "ses_noresult"},
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "assistant", "thinking out loud")
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    published = asyncio.run(scenario())
    # Assistant is process log: neither streamed to the transcript nor inbox-eligible.
    assert not (CONTENT_TOPICS & set(published))
    # ...but the row IS still persisted (for history / debugging).
    with engine.connect() as conn:
        every = messages_service.list_session_messages(conn, session_id="ses_noresult", types=("assistant",))
    assert [m["type"] for m in every["messages"]] == ["assistant"]


def test_persist_agent_toolcall_avibe_writes_event_without_streaming(isolated_state):
    """A tool-call stream item is trace data only: it is saved to agent_events,
    not messages, and does not publish chat/inbox updates."""
    from core import inbox_events

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_tool", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_tool",
                scope_id=scope_id,
                agent_name="Atlas",
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_tool",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench",
        channel_id="ses_tool",
        platform="avibe",
        platform_specific={
            "agent_session_id": "ses_tool",
            "turn_token": "turn_123",
            "task_execution_id": "run_123",
        },
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "tool_call", "Tool input failed to parse")
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    published = asyncio.run(scenario())
    assert not (CONTENT_TOPICS & set(published))
    with engine.connect() as conn:
        message_row = conn.execute(select(messages).where(messages.c.session_id == "ses_tool")).first()
        event_row = conn.execute(select(agent_events).where(agent_events.c.session_id == "ses_tool")).mappings().one()
    assert message_row is None
    assert event_row["scope_id"] == scope_id
    assert event_row["agent_name"] == "Atlas"
    assert event_row["backend"] == "claude"
    assert event_row["turn_id"] == "turn_123"
    assert event_row["run_id"] == "run_123"
    assert event_row["content_text"] == "Tool input failed to parse"


def test_persist_agent_toolcall_publishes_when_activity_enabled(isolated_state, monkeypatch):
    """With ``show_agent_activity`` on, a ``tool_call`` fans out a synthesized
    ``message.new`` (type='tool_call') so an open Chat page's activity panel shows
    the step live — while still landing ONLY in ``agent_events`` (not ``messages``)
    and never bumping the inbox."""
    import core.message_mirror as mm
    from core import inbox_events

    monkeypatch.setattr(mm, "_activity_streaming_enabled", lambda: True)

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_ta", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_ta", scope_id=scope_id, agent_name="Atlas", agent_backend="claude",
                agent_variant="default", session_anchor="anchor_ses_ta", native_session_id="",
                status="active", metadata_json="{}", created_at=now, updated_at=now, last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench", channel_id="ses_ta", platform="avibe",
        platform_specific={"agent_session_id": "ses_ta", "turn_token": "turn_9"},
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "tool_call", "🔧 `Bash` `{\"command\":\"ls\"}`")
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    events = asyncio.run(scenario())
    assert "message.new" in events and "inbox.session.updated" not in events
    msg = events["message.new"]
    assert msg["type"] == "tool_call"
    assert msg["session_id"] == "ses_ta"
    assert msg["author"] == "agent"
    assert msg["text"] == "🔧 `Bash` `{\"command\":\"ls\"}`"

    # Still a trace event only — no messages row.
    with engine.connect() as conn:
        assert conn.execute(select(messages).where(messages.c.session_id == "ses_ta")).first() is None
        assert conn.execute(select(agent_events).where(agent_events.c.session_id == "ses_ta")).first() is not None


def test_persist_agent_assistant_publishes_when_activity_enabled(isolated_state, monkeypatch):
    """With ``show_agent_activity`` on, an interim ``assistant`` row streams as
    ``message.new`` (type='assistant') for the activity panel, but still does NOT
    bump the inbox (process log, not a reply)."""
    import core.message_mirror as mm
    from core import inbox_events
    from storage import messages_service

    monkeypatch.setattr(mm, "_activity_streaming_enabled", lambda: True)

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_ia", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_ia", scope_id=scope_id, agent_backend="claude", agent_variant="default",
                session_anchor="anchor_ses_ia", native_session_id="", status="active",
                metadata_json="{}", created_at=now, updated_at=now, last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench", channel_id="ses_ia", platform="avibe",
        platform_specific={"agent_session_id": "ses_ia"},
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "assistant", "thinking out loud")
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    events = asyncio.run(scenario())
    assert "message.new" in events and "inbox.session.updated" not in events
    assert events["message.new"]["type"] == "assistant"
    assert events["message.new"]["text"] == "thinking out loud"

    # Persisted as an assistant row (unchanged from the off case).
    with engine.connect() as conn:
        every = messages_service.list_session_messages(conn, session_id="ses_ia", types=("assistant",))
    assert [m["type"] for m in every["messages"]] == ["assistant"]


def test_persist_agent_output_is_visible_without_activity_streaming(
    isolated_state,
    monkeypatch,
):
    import core.message_mirror as mm
    from core import inbox_events

    monkeypatch.setattr(mm, "_activity_streaming_enabled", lambda: False)

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_output",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_output",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_output",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench",
        channel_id="ses_output",
        platform="avibe",
        platform_specific={"agent_session_id": "ses_output"},
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "output", "primary answer")
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    events = asyncio.run(scenario())
    assert events["message.new"]["type"] == "output"
    assert events["message.new"]["text"] == "primary answer"
    assert events["inbox.session.updated"]["preview_text"] == "primary answer"
    assert events["inbox.session.updated"]["replied"] is False

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses_output",
        )
    assert [(row["type"], row["text"]) for row in transcript["messages"]] == [
        ("output", "primary answer")
    ]


def test_persist_system_message_is_not_persisted(isolated_state):
    """A canonical ``system`` message (init banner / status line — generated by
    us, not the agent) is NOT persisted at all and publishes nothing (user
    request). Replaces the earlier system→assistant mapping."""
    from core import inbox_events
    from storage import messages_service

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_sys", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_sys", scope_id=scope_id, agent_backend="claude", agent_variant="default",
                session_anchor="anchor_ses_sys", native_session_id="", status="active",
                metadata_json="{}", created_at=now, updated_at=now, last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench", channel_id="ses_sys", platform="avibe",
        platform_specific={"agent_session_id": "ses_sys"},
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "system", "🔧 System init\n✨ Ready to work!")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.1)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    asyncio.run(scenario())
    with engine.connect() as conn:
        every = messages_service.list_session_messages(conn, session_id="ses_sys")
    assert every["messages"] == [], "system messages must not be persisted"


def test_persist_agent_terminal_notify_updates_inbox(isolated_state):
    """A terminal ``notify`` (a turn that failed before any ``result``) DOES
    publish ``inbox.session.updated`` so the failed conversation surfaces on the
    inbox in realtime, with the error as preview — not only after a reload."""
    from core import inbox_events

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_z", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_failpub",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_failpub",
                native_session_id="",
                title="Boom",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="workbench",
        channel_id="ses_failpub",
        platform="avibe",
        platform_specific={"agent_session_id": "ses_failpub"},
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, "notify", "❌ Claude error: boom")
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    events = asyncio.run(scenario())
    assert "inbox.session.updated" in events
    card = events["inbox.session.updated"]
    assert card["session_id"] == "ses_failpub"
    assert card["preview_text"] == "❌ Claude error: boom"


def test_inbound_sets_source_user(isolated_state):
    """Human IM turns carry source='user' (origin), distinct from author role."""
    mirror_inbound(_slack_ctx(), "hello there")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(select(messages).where(messages.c.platform == "slack")).mappings().first()
    assert row["source"] == "user"


def test_persist_agent_sets_source_and_agent_name(isolated_state):
    """Agent replies carry source='agent' and author_name = the session's agent,
    read from the dispatch context (vibe_agent_name)."""
    ctx = MessageContext(
        user_id="U",
        channel_id="C_general",
        platform="slack",
        platform_specific={"vibe_agent_name": "Atlas"},
    )
    mirror_inbound(ctx, "ping")
    persist_agent_message(ctx, "result", "pong")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        agent_row = conn.execute(
            select(messages).where(messages.c.author == "agent")
        ).mappings().first()
    assert agent_row["source"] == "agent"
    assert agent_row["author_name"] == "Atlas"


def test_harness_inbound_avibe_session_scoped(isolated_state):
    """A scheduled/watch turn on an avibe session lands an author/type='harness'
    row attributed to the session — with author_name = the trigger kind and
    author_id = the run-definition id (the provenance spec).
    No REST endpoint writes this, so the mirror must cover avibe here."""
    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_h", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_harness",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_harness",
                native_session_id="",
                title="Scheduled",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="scheduled",
        channel_id="ses_harness",
        platform="avibe",
        message_id="watch:def_42:exec_1",
        platform_specific={
            "agent_session_id": "ses_harness",
            "task_trigger_kind": "watch",
            "task_definition_id": "def_42",
        },
    )
    mirror_harness_inbound(ctx, "the watched condition fired")

    with engine.connect() as conn:
        row = conn.execute(
            select(messages).where(messages.c.session_id == "ses_harness")
        ).mappings().first()
    assert row is not None
    assert row["author"] == "harness"
    assert row["source"] == "harness"
    assert row["author_name"] == "watch"
    assert row["author_id"] == "def_42"
    assert row["type"] == "harness"
    assert row["content_text"] == "the watched condition fired"


def test_harness_inbound_mirrors_vault_callback_provenance(isolated_state):
    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_vault", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_vault_callback",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_vault_callback",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="scheduled",
        channel_id="ses_vault_callback",
        platform="avibe",
        message_id="agent_run:exec_vault_callback",
        platform_specific={
            "agent_session_id": "ses_vault_callback",
            "task_trigger_kind": "agent_run",
            "source_kind": "callback",
            "source_actor": "vault:vrq_1",
            "vault_request_type": "access",
            "vault_request_status": "denied",
        },
    )
    mirror_harness_inbound(ctx, "The user declined your vault access request.")

    with engine.connect() as conn:
        row = conn.execute(
            select(messages).where(messages.c.session_id == "ses_vault_callback")
        ).mappings().one()
    assert json.loads(row["metadata_json"]) == {
        "source_kind": "callback",
        "source_actor": "vault:vrq_1",
        "vault_request_type": "access",
        "vault_request_status": "denied",
    }


def test_background_standalone_persists_full_turn_without_realtime_delivery(isolated_state):
    from core import inbox_events

    engine = create_sqlite_engine()
    now = "2026-07-23T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses_background",
                scope_id=None,
                visibility="background",
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_background",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    context = MessageContext(
        user_id="scheduled",
        channel_id="ses_background",
        platform="avibe",
        message_id="scheduled:def_bg:exec_1",
        platform_specific={
            "agent_session_id": "ses_background",
            "task_trigger_kind": "scheduled",
            "task_definition_id": "def_bg",
            "suppress_delivery": True,
        },
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            mirror_harness_inbound(context, "background prompt")
            persist_agent_message(context, "result", "background result")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.1)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    asyncio.run(scenario())
    with engine.connect() as conn:
        rows = conn.execute(
            select(messages).where(messages.c.session_id == "ses_background").order_by(messages.c.author)
        ).mappings().all()
    assert [(row["author"], row["content_text"]) for row in rows] == [
        ("agent", "background result"),
        ("harness", "background prompt"),
    ]
    assert {row["scope_id"] for row in rows} == {None}


def test_background_standalone_rewrites_agent_file_links(isolated_state, tmp_path):
    local_file = tmp_path / "report.txt"
    local_file.write_text("standalone artifact", encoding="utf-8")
    engine = create_sqlite_engine()
    now = "2026-07-23T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses_background_media",
                scope_id=None,
                visibility="background",
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_background_media",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    context = MessageContext(
        user_id="agent",
        channel_id="ses_background_media",
        platform="avibe",
        platform_specific={
            "agent_session_id": "ses_background_media",
            "suppress_delivery": True,
        },
    )
    persist_agent_message(context, "result", f"[report]({local_file.as_uri()})")

    with engine.connect() as conn:
        message = conn.execute(
            select(messages).where(messages.c.session_id == "ses_background_media")
        ).mappings().one()
        media = conn.execute(
            select(media_objects).where(media_objects.c.session_id == "ses_background_media")
        ).mappings().one()

    assert "file://" not in message["content_text"]
    assert "/api/media/" in message["content_text"]
    assert media["scope_id"] is None
    assert media["local_path"] == str(local_file)


def test_harness_inbound_im_scope_keyed(isolated_state):
    """A harness turn delivered to an IM channel with NO source session resolved
    falls back to a scope-keyed row (null session_id), tagged source='harness'.
    When ``agent_session_id`` IS present it rides along — see
    ``test_session_linkage`` for that case."""
    ctx = MessageContext(
        user_id="scheduled",
        channel_id="C_cron",
        platform="slack",
        message_id="scheduled:def_7:exec_9",
        platform_specific={
            "task_trigger_kind": "scheduled",
            "task_definition_id": "def_7",
        },
    )
    mirror_harness_inbound(ctx, "daily standup reminder")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(select(messages).where(messages.c.platform == "slack")).mappings().first()
    assert row["source"] == "harness"
    assert row["author"] == "harness"
    assert row["type"] == "harness"
    assert row["author_name"] == "scheduled"
    assert row["author_id"] == "def_7"
    assert row["session_id"] is None


def test_harness_inbound_avibe_publishes_message_new(isolated_state):
    """A harness turn on an avibe session fans a session-scoped ``message.new``
    onto the bus, so an open Chat page shows the triggering prompt live before
    the agent reply arrives (the whole point of recording harness turns)."""
    from core import inbox_events

    engine = create_sqlite_engine()
    now = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_hp", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_hp",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_hp",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    ctx = MessageContext(
        user_id="scheduled",
        channel_id="ses_hp",
        platform="avibe",
        message_id="scheduled:def_3:exec_5",
        platform_specific={
            "agent_session_id": "ses_hp",
            "task_trigger_kind": "scheduled",
            "task_definition_id": "def_3",
        },
    )

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            mirror_harness_inbound(ctx, "nightly digest")
            return await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    event_type, data = asyncio.run(scenario())
    assert event_type == "message.new"
    assert data["session_id"] == "ses_hp"
    assert data["source"] == "harness"
    assert data["author"] == "harness"
    assert data["type"] == "harness"
    assert data["author_name"] == "scheduled"
    assert data["text"] == "nightly digest"


def test_avibe_inbound_is_noop(isolated_state):
    """avibe user messages are written by the workbench REST endpoint, so the
    inbound mirror stays a no-op (agent output is persisted via
    persist_agent_message, which is exercised in the messages_service tests)."""
    avibe_ctx = MessageContext(
        user_id="U_alice",
        channel_id="avibe-channel",
        platform="avibe",
        message_id="avibe_001",
    )
    mirror_inbound(avibe_ctx, "this should not land")

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(messages).where(messages.c.author == "user")).mappings().all()
    assert rows == []


# --- Session-list rank (agent output, no user input) ------------------


def _seed_ranked_session(
    session_id: str, *, last_active_at: str, native_id: str, status: str = "active"
) -> str:
    engine = create_sqlite_engine()
    seeded = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id=native_id, now=seeded
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor=f"anchor_{session_id}",
                native_session_id="",
                status=status,
                metadata_json="{}",
                created_at=seeded,
                updated_at=seeded,
                last_active_at=last_active_at,
            )
        )
    return scope_id


RANK_NOW = "2026-09-02T12:00:00Z"


def _freeze_rank_clock(monkeypatch) -> None:
    """Pin the rank helper's clock. Its stamp is both the value written and the
    one the throttle compares against, so an unfrozen clock lets a second
    boundary between the write and the assertion decide the result.
    """

    import storage.workbench_sessions_service as storage_sessions

    monkeypatch.setattr(storage_sessions, "_utc_now_iso", lambda: RANK_NOW)


def _publish_agent_message(
    ctx: MessageContext, msg_type: str, text: str, **kwargs
) -> dict[str, dict]:
    from core import inbox_events

    async def scenario():
        sub_id, queue = inbox_events.bus.subscribe()
        try:
            persist_agent_message(ctx, msg_type, text, **kwargs)
            return await _drain_published(queue)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    return asyncio.run(scenario())


@pytest.mark.parametrize("msg_type,text", [("tool_call", "🔧 `Bash`"), ("assistant", "still going")])
def test_agent_output_ranks_the_session_and_announces_the_reorder(
    isolated_state, monkeypatch, msg_type, text
):
    """Agent output alone moves the session's list rank and tells an open
    browser to re-sort — the point of the change: nobody replied, and both a
    chat row and a bare tool-call trace count as the agent working.
    """
    _freeze_rank_clock(monkeypatch)
    sid = f"ses_rank_{msg_type}"
    scope_id = _seed_ranked_session(sid, last_active_at="2020-01-01T00:00:00Z", native_id=f"p_{msg_type}")
    ctx = MessageContext(
        user_id="workbench",
        channel_id=sid,
        platform="avibe",
        platform_specific={"agent_session_id": sid},
    )

    published = _publish_agent_message(ctx, msg_type, text)

    assert published["session.activity"] == {
        "session_id": sid,
        "scope_id": scope_id,
        "event": "agent_activity",
    }
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == sid)).mappings().one()
    assert row["last_active_at"] == RANK_NOW


def test_agent_output_reorder_event_follows_the_rank_throttle(isolated_state, monkeypatch):
    """The realtime reorder is published exactly when the rank moved, so the
    burst of messages inside one throttle interval costs no events at all.
    Gating on the write's own answer is what keeps the two in step without the
    publisher tracking any state of its own.
    """
    _freeze_rank_clock(monkeypatch)
    sid = "ses_rank_fresh"
    _seed_ranked_session(sid, last_active_at=RANK_NOW, native_id="p_fresh")
    ctx = MessageContext(
        user_id="workbench",
        channel_id=sid,
        platform="avibe",
        platform_specific={"agent_session_id": sid},
    )

    published = _publish_agent_message(ctx, "tool_call", "🔧 `Bash`")

    assert "session.activity" not in published


def test_background_session_output_is_not_ranked(isolated_state):
    """A background session is absent from every activity-ordered surface, so
    ranking it is a write with no reader.
    """
    sid = "ses_rank_bg"
    stale = "2020-01-01T00:00:00Z"
    _seed_ranked_session(sid, last_active_at=stale, native_id="p_bg")
    ctx = MessageContext(
        user_id="workbench",
        channel_id=sid,
        platform="avibe",
        platform_specific={"agent_session_id": sid, "suppress_delivery": True},
    )

    published = _publish_agent_message(ctx, "result", "done quietly")

    assert "session.activity" not in published
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == sid)).mappings().one()
    assert row["last_active_at"] == stale


def test_replayed_output_that_persisted_nothing_does_not_rank(isolated_state, monkeypatch):
    """The rank follows what materialized, not what was attempted.

    A retried terminal output keeps its ``native_message_id``, so the append hits
    the unique constraint and nothing new lands. Ranking there would let a replay
    of one logical output lift a long-finished session back to the top of the
    list on no new work at all.

    The seeded stamp is stale, so the throttle would have allowed this write —
    the only thing that can hold the rank is the materialized-row gate, which is
    what makes this a test of that gate rather than of the throttle.
    """
    _freeze_rank_clock(monkeypatch)
    sid = "ses_rank_replay"
    stale = "2020-01-01T00:00:00Z"
    scope_id = _seed_ranked_session(sid, last_active_at=stale, native_id="p_replay")
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=sid,
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="done",
            native_message_id="receipt_1",
        )
    ctx = MessageContext(
        user_id="workbench",
        channel_id=sid,
        platform="avibe",
        platform_specific={"agent_session_id": sid},
    )

    published = _publish_agent_message(ctx, "result", "done", native_message_id="receipt_1")

    assert "session.activity" not in published
    with engine.connect() as conn:
        row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == sid)).mappings().one()
    assert row["last_active_at"] == stale


def test_im_agent_output_ranks_the_row_without_publishing(isolated_state, monkeypatch):
    """``vibe session list``, the run graph and the running-agents view read the
    same rank for every platform, so the row is ranked there too — while the
    SSE stays avibe-only like every other publish on this path, an IM session
    having no open browser consumer.
    """
    _freeze_rank_clock(monkeypatch)
    engine = create_sqlite_engine()
    seeded = "2026-05-30T12:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="slack", scope_type="channel", native_id="C_rank", now=seeded
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_rank_im",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="anchor_ses_rank_im",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=seeded,
                updated_at=seeded,
                last_active_at="2020-01-01T00:00:00Z",
            )
        )

    ctx = MessageContext(
        user_id="U_alice",
        channel_id="C_rank",
        platform="slack",
        platform_specific={"agent_session_id": "ses_rank_im"},
    )
    published = _publish_agent_message(ctx, "result", "done")

    assert "session.activity" not in published
    with engine.connect() as conn:
        row = (
            conn.execute(select(agent_sessions).where(agent_sessions.c.id == "ses_rank_im"))
            .mappings()
            .one()
        )
    assert row["last_active_at"] == RANK_NOW
