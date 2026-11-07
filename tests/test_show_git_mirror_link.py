"""Show's synchronous hooks must not bring mirror writer waits onto the loop.

Existing checkpoint tests hold git, not the inbound-link transaction. These cases
hold/fail that real UPDATE and check its ordering, completion signal, and loop
ownership without changing the shared turn lifecycle API.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from config import paths
from core.git_binary import ResolvedGit
from core.inbox_events import InboxEventBus
from core.message_mirror import mirror_inbound, scope_id_for_context
from core.show_git import POST_TURN, PRE_TURN, ShowGitCheckpointService
from modules.im import MessageContext
from storage.agent_session_rows import create_agent_session_row
from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures", "cancel_at"), [(0, None), (1, None), (3, None), (0, "link"), (0, "git")]
)
async def test_link_write_is_ordered_before_checkpoint_and_waiter_observes_exhaustion(
    monkeypatch, tmp_path, caplog, failures, cancel_at
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    session_id = "ses_show_link"
    context_writes = []

    class ObservedContext(MessageContext):
        def __setattr__(self, name, value):
            if name == "platform_specific":
                context_writes.append(threading.get_ident())
            super().__setattr__(name, value)

    context = ObservedContext(
        user_id="user", channel_id="channel", platform="slack", message_id="input-1",
        platform_specific={"agent_session_id": session_id},
    )
    assert await asyncio.to_thread(mirror_inbound, context, "edit the page")
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        create_agent_session_row(
            conn, session_id=session_id, scope_id=scope_id_for_context(context), session_anchor=None,
            agent_backend="claude", workdir=str(tmp_path),
        )
    paths.get_show_page_dir(session_id).mkdir(parents=True)
    service = ShowGitCheckpointService(ResolvedGit(path=tmp_path / "git", source="system"))
    ordered, publications, observed_links = [], [], []
    entered, release = threading.Event(), threading.Event()
    git_entered, git_release = threading.Event(), threading.Event()
    attempts = 0

    def hold_update(_conn, _cursor, statement, _parameters, _ctx, _many):
        nonlocal attempts
        if not statement.startswith("UPDATE messages "):
            return
        attempts += 1
        ordered.append(("link", threading.get_ident()))
        entered.set()
        assert release.wait(5)
        if attempts <= failures:
            raise OperationalError(statement, {}, sqlite3.OperationalError("database is locked"))

    class Repository:
        def checkpoint(self, kind, **_kwargs):
            with engine.connect() as conn:
                linked = conn.execute(select(messages.c.session_id)).scalar_one()
            observed_links.append(linked)
            ordered.append((kind, threading.get_ident()))
            if kind == PRE_TURN and cancel_at == "git":
                git_entered.set()
                git_release.wait(5)
            return True

    event.listen(engine, "before_cursor_execute", hold_update)
    monkeypatch.setattr(service, "_repository", lambda _session_id: Repository())
    bus = InboxEventBus()
    bus.subscribe_callback(lambda topic, _row: publications.append((topic, threading.get_ident())))
    service.start(bus)
    controller = SimpleNamespace(_session_id_from_context=lambda _ctx: session_id)
    loop_thread = threading.get_ident()
    try:
        start = time.monotonic()
        service.begin_turn(controller, context)
        assert time.monotonic() - start < 0.5
        assert await asyncio.to_thread(entered.wait, 3)
        waiter = asyncio.create_task(service.wait_for_turn_start(session_id))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        assert [name for name, _ in ordered] == ["link"]
        if cancel_at == "link":
            waiter.cancel()
            await asyncio.sleep(0.02)
            assert not waiter.done(), "Stop must land an already-started link write"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 3)
        elif cancel_at == "git":
            release.set()
            assert await asyncio.to_thread(git_entered.wait, 3)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 0.5)
            assert not git_release.is_set(), "Stop must not wait for a blocked git job"
            git_release.set()
        else:
            release.set()
            assert await asyncio.wait_for(waiter, 5) is (failures < 3)
        service.end_turn(context)
    finally:
        release.set()
        git_release.set()
        service.stop()
        event.remove(engine, "before_cursor_execute", hold_update)
    assert [name for name, _ in ordered] == ["link"] * min(failures + 1, 3) + [PRE_TURN, POST_TURN]
    assert observed_links == [None if failures == 3 else session_id] * 2
    assert all(thread != loop_thread for _, thread in ordered)
    assert publications == [("turn.start", loop_thread), ("turn.end", loop_thread)]
    assert context_writes and all(thread == loop_thread for thread in context_writes)
    if failures == 3:
        assert "Show checkpoint inbound message link failed" in caplog.text
        assert "Show checkpoint turn-start work failed" in caplog.text
