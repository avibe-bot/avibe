"""Mirror retry ownership, beyond successful-write/projection coverage.

Real rollback and contention catch swallowed scope/media errors and a worker that
publishes on the wrong thread. Cancellation/snapshot cases protect caller state;
raw bus calls (not coalesced topic maps) catch duplicate publication.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import Counter

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from core import inbox_events, message_mirror as mirror, workbench_media
from core.citations import CitationSource, body_digest, register_citations
from modules.im import MessageContext
from storage import agent_events_service, media_service, messages_service, settings_service
from storage.agent_session_rows import create_agent_session_row
from storage.db import _is_sqlite_busy, get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, media_objects, messages, scopes


def busy() -> OperationalError:
    return OperationalError("test write", {}, sqlite3.OperationalError("database is locked"))


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        scope = settings_service.upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="mirror", now="2020-01-01T00:00:00Z"
        )
        create_agent_session_row(
            conn, session_id="ses_mirror_retry", scope_id=scope, session_anchor=None,
            agent_backend="claude", workdir=str(tmp_path), now="2020-01-01T00:00:00Z",
        )
    return engine


def context(platform="avibe"):
    return MessageContext(
        user_id="user", channel_id="mirror", platform=platform, message_id="input-1",
        platform_specific={"agent_session_id": "ses_mirror_retry", "turn_token": "turn-1"},
    )


@pytest.mark.parametrize("kind", ["agent", "human", "harness", "silent"])
@pytest.mark.parametrize("failure_at", ["scope", "message"])
def test_scope_and_message_retry_as_one_transaction(state, monkeypatch, kind, failure_at):
    """Partial scope/message writes must roll back, not turn into a successful skip."""
    actual_scope = settings_service.upsert_scope
    scope_existed = []
    attempts = []

    def upsert(conn, **kwargs):
        scope_existed.append(conn.execute(select(scopes.c.id).where(scopes.c.platform == "slack")).first())
        result = actual_scope(conn, **kwargs)
        if failure_at == "scope" and len(scope_existed) == 1:
            raise busy()
        return result

    owner = agent_events_service if kind == "silent" else messages_service
    actual_append = owner.append

    def append(conn, **kwargs):
        attempts.append(kwargs)
        result = actual_append(conn, **kwargs)
        if failure_at == "message" and len(attempts) == 1:
            raise busy()
        return result

    monkeypatch.setattr(settings_service, "upsert_scope", upsert)
    monkeypatch.setattr(owner, "append", append)
    ctx = context("slack")
    if kind == "agent":
        result = mirror.persist_agent_message(ctx, "result", "answer")
    elif kind == "human":
        result = mirror.mirror_inbound(ctx, "question")
    elif kind == "harness":
        result = mirror.mirror_harness_inbound(ctx, "prompt")
    else:
        result = mirror.persist_silent_terminal(ctx, is_error=False)
    assert scope_existed == [None, None]
    assert result
    with state.connect() as conn:
        assert len(conn.execute(select(scopes).where(scopes.c.platform == "slack")).all()) == 1
        table = agent_events if kind == "silent" else messages
        assert len(conn.execute(select(table)).all()) == 1


@pytest.mark.asyncio
async def test_real_writer_over_busy_timeout_keeps_loop_live_and_publishes_once(state, monkeypatch):
    """A second SQLite connection holds the writer longer than busy_timeout=5000."""
    bus = inbox_events.InboxEventBus()
    monkeypatch.setattr(inbox_events, "bus", bus)
    publications, subscribers, writes, ticks = [], [], [], []
    write_entered, busy_returned = threading.Event(), threading.Event()
    timeouts, busy_times = [], []
    original_publish = bus.publish

    def publish(topic, row):
        publications.append((topic, threading.get_ident()))
        return original_publish(topic, row)

    monkeypatch.setattr(bus, "publish", publish)
    bus.subscribe_callback(lambda topic, _row: subscribers.append((topic, threading.get_ident())))

    def before_write(_conn, _cursor, statement, _parameters, _ctx, _many):
        if statement.startswith("INSERT INTO messages "):
            timeouts.append(_conn.exec_driver_sql("PRAGMA busy_timeout").scalar())
            writes.append(threading.get_ident())
            write_entered.set()

    def handle_error(error):
        original = error.original_exception
        if _is_sqlite_busy(original):
            busy_times.append(time.monotonic())
            busy_returned.set()

    event.listen(state, "before_cursor_execute", before_write)
    event.listen(state, "handle_error", handle_error)
    held, release = threading.Event(), threading.Event()

    def hold_writer():
        writer = sqlite3.connect(state.url.database)
        try:
            writer.execute("BEGIN IMMEDIATE")
            held.set()
            release.wait(20)
            writer.commit()
        finally:
            writer.close()

    owner = threading.Thread(target=hold_writer)
    owner.start()
    assert await asyncio.to_thread(held.wait, 3)
    running = True

    async def heartbeat():
        while running:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    heartbeat_task = asyncio.create_task(heartbeat())
    loop_thread = threading.get_ident()
    try:
        persist = asyncio.create_task(mirror.persist_agent_message_async(context(), "result", "answer"))
        assert await asyncio.to_thread(write_entered.wait, 3)
        # busy_timeout counts SQLite handler sleeps, not a strict wall-clock
        # deadline. Keep the writer until this connection actually exhausts it.
        assert await asyncio.to_thread(busy_returned.wait, 12), (sqlite3.sqlite_version, timeouts)
        release.set()
        row = await asyncio.wait_for(persist, 10)
        assert row and row["text"] == "answer"
    finally:
        release.set()
        await asyncio.to_thread(owner.join, 3)
        running = False
        await heartbeat_task
        event.remove(state, "before_cursor_execute", before_write)
        event.remove(state, "handle_error", handle_error)
    assert timeouts == [5000, 5000]
    assert len(busy_times) == 1
    assert len(ticks) > 100
    assert max(right - left for left, right in zip(ticks, ticks[1:])) < 1.0
    assert len(writes) == 2 and all(thread != loop_thread for thread in writes)
    assert Counter(topic for topic, _ in publications) == {
        "session.activity": 1, "message.new": 1, "inbox.session.updated": 1,
    }
    assert subscribers == publications
    assert all(thread == loop_thread for _, thread in publications)


@pytest.mark.asyncio
async def test_cancelled_write_lands_frozen_receipt_before_caller_cancellation(state, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = messages_service.append
    ctx = context()
    metadata = {"nested": {"value": "original"}}
    receipts, publications = [], []
    loop_thread = threading.get_ident()

    def append(conn, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return original(conn, **kwargs)

    monkeypatch.setattr(messages_service, "append", append)
    monkeypatch.setattr(inbox_events.bus, "publish", lambda topic, _row: publications.append((topic, threading.get_ident())))

    async def caller():
        receipts.append(await mirror.persist_agent_message_async(ctx, "result", "original", metadata=metadata))
        await asyncio.sleep(0)

    task = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        ctx.platform_specific["suppress_delivery"] = True
        ctx.platform_specific["agent_session_id"] = "ses_changed"
        metadata["nested"]["value"] = "changed"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert len(receipts) == 1
    assert receipts[0]["session_id"] == "ses_mirror_retry"
    assert receipts[0]["metadata"]["nested"] == {"value": "original"}
    assert Counter(topic for topic, _ in publications)["message.new"] == 1
    assert all(thread == loop_thread for _, thread in publications)


@pytest.mark.parametrize("kind", ["agent", "harness", "human", "silent", "link"])
def test_exhaustion_is_visible_and_does_not_commit_partial_scope(state, monkeypatch, caplog, kind):
    attempts = []

    def fail(*_args, **_kwargs):
        attempts.append(1)
        raise busy()

    ctx = context("slack")
    errors = []
    if kind == "link":
        # The UPDATE's failure must remain observable to its ordered-worker caller.
        def before_update(_conn, _cursor, statement, _parameters, _ctx, _many):
            if statement.startswith("UPDATE messages "):
                fail()
        event.listen(state, "before_cursor_execute", before_update)
        try:
            result = mirror.link_inbound_message_session(platform="slack", native_message_id="input-1", session_id="ses_mirror_retry")
        finally:
            event.remove(state, "before_cursor_execute", before_update)
    else:
        monkeypatch.setattr(settings_service, "upsert_scope", fail)
        if kind == "agent":
            result = mirror.persist_agent_message(ctx, "result", "answer", error_sink=errors)
            assert len(errors) == 1 and isinstance(errors[0], OperationalError)
        elif kind == "harness":
            result = mirror.mirror_harness_inbound(ctx, "prompt")
        elif kind == "human":
            result = mirror.mirror_inbound(ctx, "question")
        else:
            result = mirror.persist_silent_terminal(ctx, is_error=False)
    assert not result and len(attempts) == 3
    assert "Message mirror transaction failed" in caplog.text
    with state.connect() as conn:
        assert conn.execute(select(messages)).first() is None
        assert conn.execute(select(scopes).where(scopes.c.platform == "slack")).first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "harness"])
async def test_publication_failure_never_invalidates_or_retries_committed_receipt(state, monkeypatch, kind):
    calls = []

    def broken_publish(topic, _row):
        calls.append(topic)
        raise busy()  # Even a subscriber's BUSY must not retry the committed transaction.

    monkeypatch.setattr(inbox_events.bus, "publish", broken_publish)
    errors = []
    if kind == "agent":
        result = await mirror.persist_agent_message_async(context(), "result", "answer", error_sink=errors)
    else:
        result = await mirror.mirror_harness_inbound_async(context(), "prompt")
    assert result and not errors
    assert all(count == 1 for count in Counter(calls).values())
    with state.connect() as conn:
        assert len(conn.execute(select(messages)).all()) == 1


def test_retry_rebuilds_media_and_citations_from_original_body(state, monkeypatch, tmp_path):
    image = tmp_path / "answer.png"
    image.write_bytes(b"test image")
    source = CitationSource(ref_id="turn0view0", title="Source", url="https://example.com/source")
    raw, bundle = register_citations(
        f"![answer]({image.as_uri()}) Citation\ue200cite\ue202turn0view0\ue201",
        {source.ref_id: source}, unresolved_label="unavailable",
    )
    original_append = messages_service.append
    bodies = []
    original_rewrite = workbench_media.rewrite_agent_media

    def rewrite(conn, **kwargs):
        bodies.append(kwargs["text"])
        return original_rewrite(conn, **kwargs)

    def append(conn, **kwargs):
        row = original_append(conn, **kwargs)
        if len(bodies) == 1:
            raise busy()  # Both the media token and the message now need rollback.
        return row

    monkeypatch.setattr(workbench_media, "rewrite_agent_media", rewrite)
    monkeypatch.setattr(messages_service, "append", append)
    row = mirror.persist_agent_message(context(), "result", raw, citations=bundle)
    assert bodies == [raw, raw]
    with state.connect() as conn:
        token = conn.execute(select(media_objects.c.token)).scalar_one()
        assert len(conn.execute(select(messages)).all()) == 1
    assert f"/api/media/{token}" in row["text"]
    citation = row["content"]["citations"][0]
    assert citation["body_sha256"] == body_digest(row["text"])
    start, end = citation["spans"][0]
    assert row["text"][start:end] == "[example.com](https://example.com/source)"


@pytest.mark.parametrize("site", ["register", "dimensions", "hosted"])
def test_nested_media_database_failure_reaches_transaction_retry(state, monkeypatch, tmp_path, site):
    image = tmp_path / "answer.png"
    image.write_bytes(b"test image")
    owner, name = (media_service, "get_by_token") if site == "dimensions" else (workbench_media, "register_agent_reply_media")
    original = getattr(owner, name)
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        value = original(*args, **kwargs)
        if len(calls) == 1:
            raise busy()
        return value

    monkeypatch.setattr(owner, name, fail_once)
    if site == "hosted":
        mirror.persist_agent_message(context(), "tool_call", "image", hosted={"images": [{"path": str(image)}]})
    else:
        row = mirror.persist_agent_message(context(), "result", f"![answer]({image.as_uri()})")
        assert "/api/media/" in row["text"]
    assert len(calls) == 2
    with state.connect() as conn:
        assert len(conn.execute(select(media_objects)).all()) == 1
