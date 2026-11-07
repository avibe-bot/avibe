"""Live completion owns only store work off-loop, not the native event or delivery.

The synchronous lifecycle suite checks immediate failures but cannot catch a
5-second loop stall, a committed worker losing its generation, or cancellation
splitting persistence from registry publication. These tests exercise the new
live boundary against an independent SQLite writer and the real recovery API.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from config.paths import get_sqlite_state_path
from core.runtime_activation import RuntimeActivationRegistry
from core.session_activities import SessionActivityRegistry, activity_completion_output
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.session_activities import SQLiteSessionActivityStore


@pytest.fixture
def activity_store():
    ensure_sqlite_state()
    engine = create_sqlite_engine(get_sqlite_state_path())
    try:
        yield SQLiteSessionActivityStore(engine)
    finally:
        engine.dispose()


def _start(registry, *, identity=None, runtime="runtime", **kwargs):
    return registry.start(
        backend="claude", runtime_key=runtime, session_id="session",
        activity_id="task", kind="background_task", activation_identity=identity,
        **kwargs,
    )


def _complete(registry, *, identity=None, **kwargs):
    return registry.complete_async(
        backend="claude", runtime_key="runtime", activity_id="task",
        status="completed", expects_output=True, activation_identity=identity, **kwargs,
    )


def _holder():
    conn = sqlite3.connect(get_sqlite_state_path(), isolation_level=None, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _no_wait(store):
    @event.listens_for(store.engine, "checkout")
    def configure(conn, *_):
        conn.execute("PRAGMA busy_timeout = 0")


async def test_completion_survives_more_than_five_seconds_of_contention_without_stalling_loop(activity_store, monkeypatch):
    """The old live completion blocks for five seconds, then loses its event."""
    from tests.test_claude_agent_initiated_turn import _build_agent

    agent, service = _build_agent()
    registry = service.activities = SessionActivityRegistry(activity_store)
    _start(registry)
    ready, deliver, handled, parked = (asyncio.Event() for _ in range(4))

    class Client:
        async def receive_messages(self):
            ready.set()
            await deliver.wait()
            yield SimpleNamespace(subtype="task_notification", task_id="task", status="completed", data={})
            handled.set()
            await parked.wait()

    # Output delivery is a separate owner; stop at the real native completion
    # boundary, before its later output batch is claimed.
    monkeypatch.setattr(agent, "_schedule_completed_activity_flush", lambda *_args, **_kwargs: None)
    receiver = asyncio.create_task(agent._receive_messages(
        Client(), "session", "/tmp/work",
        MessageContext(user_id="user", channel_id="channel", platform="discord"),
        composite_key="runtime",
    ))
    await asyncio.wait_for(ready.wait(), 2)
    loop_thread = threading.get_ident()
    write_threads = []
    busy = []

    @event.listens_for(activity_store.engine, "before_cursor_execute")
    def write_thread(_conn, _cursor, statement, *_):
        if statement.startswith("UPDATE runtime_records"):
            write_threads.append(threading.get_ident())

    @event.listens_for(activity_store.engine, "handle_error")
    def contended(context):
        if str(context.original_exception) == "database is locked":
            busy.append(context.statement)

    holder = _holder()
    release = threading.Timer(5.6, holder.execute, ("COMMIT",))
    release.start()
    ticks = []

    async def heartbeat():
        while not handled.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(heartbeat())
    try:
        deliver.set()
        await asyncio.sleep(0.1)
        assert registry.has_active("claude", "runtime")
        assert not registry.has_completed_output("claude", "runtime")
        assert not handled.is_set(), "completion must not abandon the event at the first busy timeout"
        await asyncio.wait_for(handled.wait(), 12)
        await ticker
        assert len(busy) == 1, "the first full 5-second writer wait must actually exhaust"
        assert len(write_threads) == 2 and all(thread != loop_thread for thread in write_threads)
        assert len(ticks) > 100
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 1
        assert not registry.has_active("claude", "runtime")
        assert registry.session_state("session")["pending_activity_output_count"] == 1
        assert activity_store.list_activities()[0]["phase"] == "awaiting_output"
    finally:
        release.cancel()
        release.join()
        holder.close()
        handled.set()
        receiver.cancel()
        await asyncio.gather(receiver, ticker, return_exceptions=True)




async def test_terminal_ack_offloads_delete_and_keeps_loop_responsive(activity_store):
    """Run-owner acknowledgement must not reintroduce synchronous SQLite waits."""
    registry = SessionActivityRegistry(activity_store)
    _start(registry)
    completed = registry.complete(
        backend="claude",
        runtime_key="runtime",
        activity_id="task",
        status="failed",
        retain_terminal_snapshot=True,
    )
    assert completed is not None
    assert activity_store.list_activities()[0]["phase"] == "terminal"
    loop_thread = threading.get_ident()
    delete_threads = []

    @event.listens_for(activity_store.engine, "before_cursor_execute")
    def delete_thread(_conn, _cursor, statement, *_):
        if statement.startswith("DELETE FROM runtime_records"):
            delete_threads.append(threading.get_ident())

    holder = _holder()
    release = threading.Timer(5.6, holder.execute, ("COMMIT",))
    release.start()
    ticks = []
    task = asyncio.create_task(registry.ack_recovered_terminal_async(completed))

    async def heartbeat():
        while not task.done():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
        result = await asyncio.wait_for(task, 12)
        await ticker
        assert result is True
        assert ticks and max(b - a for a, b in zip(ticks, ticks[1:])) < 1
        assert delete_threads and all(thread != loop_thread for thread in delete_threads)
        assert registry.terminal_snapshots_for_runtime("claude", "runtime") == []
        assert activity_store.list_activities() == []
    finally:
        release.cancel()
        release.join()
        holder.close()
        await asyncio.gather(task, ticker, return_exceptions=True)


async def test_exhausted_completion_reconciles_its_frozen_intent_and_sends_only_once(activity_store, monkeypatch):
    """A logged receiver error used to leave an active row, with no completion retry owner."""
    from tests.test_claude_agent_initiated_turn import (
        _ActivityDeliveryClient, _build_agent, _install_activity_dispatcher,
    )

    _no_wait(activity_store)
    agent, service = _build_agent()
    registry = service.activities = SessionActivityRegistry(activity_store)
    _start(registry)
    transport = _ActivityDeliveryClient()
    _install_activity_dispatcher(agent, transport)
    context = MessageContext(user_id="user", channel_id="channel", platform="discord")
    attempts = []
    notifications = []
    original_notify = agent.on_activity_completed

    async def notify(activity):
        notifications.append((activity, threading.get_ident()))
        await original_notify(activity)

    monkeypatch.setattr(agent, "on_activity_completed", notify)

    @event.listens_for(activity_store.engine, "handle_error")
    def failed(context):
        if str(context.original_exception) == "database is locked":
            attempts.append(context.statement)

    holder = _holder()
    try:
        with pytest.raises(OperationalError):
            await agent._handle_activity_message(
                SimpleNamespace(subtype="task_notification", task_id="task", status="completed", data={}),
                "runtime", context,
            )
        assert len(attempts) == 3
        assert registry.has_active("claude", "runtime")
        assert not registry.has_completed_output("claude", "runtime")
        assert registry.scan_pending_completion_runtimes(limit=1) == ([("claude", "runtime")], False)
        assert "OperationalError" in next(iter(registry.completion_persistence_recovery("claude", "runtime").values()))
        assert notifications == [] and transport.sent == []
    finally:
        holder.close()

    for completed in await registry.retry_pending_completions("claude", "runtime"):
        await agent.on_activity_completed(completed)
    assert len(notifications) == 1 and notifications[0][1] == threading.get_ident()
    claimed = registry.claim_completed_output("claude", "runtime")
    assert claimed is not None
    await agent.controller.emit_agent_message(
        context, "result", "one actual answer",
        output=activity_completion_output(claimed, detached=True, completes_turn=False),
    )
    assert await registry.retry_pending_completions("claude", "runtime") == []
    assert registry.scan_pending_completion_runtimes(limit=1) == ([], False)
    assert transport.sent == ["one actual answer"]
    assert not registry.has_completed_output("claude", "runtime")


async def test_retry_keeps_terminal_time_metadata_and_failed_provenance(activity_store, monkeypatch):
    """Classification can be newer in memory than disk; CAS must use the committed image."""
    _no_wait(activity_store)
    registry = SessionActivityRegistry(activity_store)
    _start(registry, metadata={"provenance_pending": True})
    holder = _holder()
    metadata = {"summary": "frozen", "facts": ["first"]}
    try:
        registry.classify_provisional_provenance(
            "claude", "runtime", activity_ids={"task"}, parent_activity_ids=set(),
            turn_id="turn", run_ids=["run"], detached=False,
        )
        assert registry.provenance_persistence_recovery("claude", "runtime")
        monkeypatch.setattr("core.session_activities._now_iso", lambda: "2026-11-07T01:00:00Z")
        with pytest.raises(OperationalError):
            await _complete(registry, metadata=metadata)
    finally:
        holder.close()
    metadata["facts"].append("not part of the native completion")
    monkeypatch.setattr("core.session_activities._now_iso", lambda: "2026-11-07T02:00:00Z")
    [completed] = await registry.retry_pending_completions("claude", "runtime")
    assert completed.completed_at == "2026-11-07T01:00:00Z"
    assert completed.metadata["facts"] == ["first"]
    assert completed.run_id == "run" and completed.turn_id == "turn"
    assert completed.metadata["provenance_human"]
    assert registry.provenance_persistence_recovery("claude", "runtime") == {}
    [durable] = activity_store.list_activities()
    assert durable["activity"] == completed.to_dict()


async def test_cancelled_completion_lands_before_loop_notification_and_cancellation(activity_store, monkeypatch):
    """Cancelling to_thread alone would leave a committed completion unexposed/unowned."""
    registry = SessionActivityRegistry(activity_store)
    _start(registry)
    entered, release = threading.Event(), threading.Event()
    original = activity_store.commit_activity_completion

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(activity_store, "commit_activity_completion", delayed)
    notifications = []

    async def caller():
        completed = await _complete(registry)
        notifications.append((completed, threading.get_ident()))
        await asyncio.sleep(0)  # the first cancellation point after the loop-owned tail

    task = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert registry.has_active("claude", "runtime")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(notifications) == 1
        assert notifications[0][1] == threading.get_ident()
        assert notifications[0][0].status == "completed"
        assert registry.session_state("session")["pending_activity_output_count"] == 1
        assert registry.completion_persistence_recovery("claude", "runtime") == {}
        assert activity_store.list_activities()[0]["phase"] == "awaiting_output"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["retire", "replace", "reincarnate", "progress"])
@pytest.mark.parametrize("pause", ["before", "after"])
async def test_worker_confirmation_cannot_publish_or_undo_a_changed_owner(activity_store, monkeypatch, change, pause):
    """Retirement may win after COMMIT but before its completion reaches the loop."""
    activations = RuntimeActivationRegistry()
    activations.bind_loop_thread()
    identity = activations.attach("claude", "runtime")
    registry = SessionActivityRegistry(activity_store, activation_registry=activations)
    _start(registry, identity=identity)
    committed, release = threading.Event(), threading.Event()
    original = activity_store.commit_activity_completion
    calls = []

    def delayed(**kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1 and pause == "before":
            committed.set()
            assert release.wait(5)
        result = original(**kwargs)
        if len(calls) == 1 and pause == "after":
            committed.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(activity_store, "commit_activity_completion", delayed)
    task = asyncio.create_task(_complete(registry, identity=identity))
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        if change == "retire":
            assert activations.retire_if_current(identity, lambda: True)
        elif change == "replace":
            identity = activations.attach("claude", "runtime")
            _start(registry, identity=identity, metadata={"owner": "new generation"})
        elif change == "reincarnate":
            registry.complete(backend="claude", runtime_key="runtime", activity_id="task", status="stopped", activation_identity=identity)
            _start(registry, identity=identity, metadata={"owner": "new activity"})
        else:
            registry.progress(backend="claude", runtime_key="runtime", session_id="session", activity_id="task", metadata={"latest": True}, activation_identity=identity)
        expected = activity_store.list_activities()
        release.set()
        assert await task is None
        assert not registry.has_completed_output("claude", "runtime")
        assert all(thread != threading.get_ident() for thread in calls)
        if change == "retire":
            assert activity_store.list_activities()[0]["phase"] == "active"
            assert registry.completion_persistence_recovery("claude", "runtime") == {}
        elif change == "progress":
            [completed] = await registry.retry_pending_completions("claude", "runtime")
            assert completed.metadata["latest"]
        else:
            assert activity_store.list_activities() == expected
            assert await registry.retry_pending_completions("claude", "runtime") == []
            assert registry.has_active("claude", "runtime")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("expects_output,retain", [(False, False), (False, True), (True, False)])
async def test_live_completion_keeps_each_existing_snapshot_policy(activity_store, expects_output, retain):
    registry = SessionActivityRegistry(activity_store)
    _start(registry)
    completed = await registry.complete_async(
        backend="claude", runtime_key="runtime", activity_id="task", status="completed",
        expects_output=expects_output, retain_terminal_snapshot=retain,
    )
    assert completed is not None
    assert not registry.has_active("claude", "runtime")
    rows = activity_store.list_activities()
    assert [row["phase"] for row in rows] == (["awaiting_output"] if expects_output else ["terminal"] if retain else [])
    assert registry.has_completed_output("claude", "runtime") is expects_output
    assert registry.completion_persistence_recovery("claude", "runtime") == {}
