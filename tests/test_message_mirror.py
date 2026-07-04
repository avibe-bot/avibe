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
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_mirror import mirror_harness_inbound, mirror_inbound, persist_agent_message
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, agent_sessions, messages, scopes
from storage.settings_service import upsert_scope


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


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
            # Drain both events (order: message.new, then inbox.session.updated).
            for _ in range(2):
                event_type, data = await asyncio.wait_for(queue.get(), timeout=1.0)
                events[event_type] = data
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
        seen = []
        try:
            persist_agent_message(ctx, "assistant", "thinking out loud")
            # No events at all — assistant is process log: not streamed, not inbox.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.1)
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return seen

    asyncio.run(scenario())
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
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.1)
        finally:
            inbox_events.bus.unsubscribe(sub_id)

    asyncio.run(scenario())
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
        events: dict[str, dict] = {}
        try:
            persist_agent_message(ctx, "notify", "❌ Claude error: boom")
            for _ in range(2):  # message.new + inbox.session.updated, any order
                evt = await asyncio.wait_for(queue.get(), timeout=1.0)
                events[evt[0]] = evt[1]
        finally:
            inbox_events.bus.unsubscribe(sub_id)
        return events

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
    """A scheduled/watch turn on an avibe session lands an author='user',
    source='harness' row attributed to the session — with author_name = the
    trigger kind and author_id = the run-definition id (the provenance spec).
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
    assert row["author"] == "user"  # agent reads it as user input
    assert row["source"] == "harness"  # but origin is the harness
    assert row["author_name"] == "watch"
    assert row["author_id"] == "def_42"
    assert row["type"] == "user"
    assert row["content_text"] == "the watched condition fired"


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
    assert row["author"] == "user"
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
