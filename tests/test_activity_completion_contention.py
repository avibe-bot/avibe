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
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

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


@pytest.mark.parametrize("grace", [0, 60])
async def test_failed_native_completion_becomes_recoverable_output_without_another_event(
    activity_store, monkeypatch, tmp_path, grace,
):
    from core.runtime_work import RuntimeWorkLane
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore, _ScheduledRuntimeWorkHandler
    from tests.test_claude_agent_initiated_turn import _build_agent

    agent, service = _build_agent()
    registry = service.activities = SessionActivityRegistry(activity_store)
    _start(registry)
    agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = grace
    supervisor = SimpleNamespace(notify=Mock(), run_sync=None)
    agent.controller.runtime_work_supervisor = supervisor
    original = activity_store.commit_activity_completion
    failed = True

    def commit(**kwargs):
        if failed:
            raise OperationalError("test completion", {}, sqlite3.OperationalError("database is locked"))
        return original(**kwargs)

    monkeypatch.setattr(activity_store, "commit_activity_completion", commit)
    with pytest.raises(OperationalError):
        await agent._handle_activity_message(
            SimpleNamespace(subtype="task_notification", task_id="task", status="completed", data={"summary": "finished"}),
            "runtime", MessageContext(user_id="user", channel_id="channel", platform="discord"),
        )
    supervisor.notify.assert_any_call(RuntimeWorkLane.ACTIVITY_OUTPUTS, reset_cursor=True)
    failed = False
    sched = ScheduledTaskService(
        controller=SimpleNamespace(agent_service=service),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "requests"),
    )
    deliveries = []

    async def deliver(activity):
        deliveries.append(activity)
        registry.ack_completed_output(activity)

    monkeypatch.setattr(sched, "_deliver_recovered_activity_output", deliver)
    monkeypatch.setattr(sched, "_schedule_runtime_work_wake", Mock())
    handler = _ScheduledRuntimeWorkHandler(sched, RuntimeWorkLane.ACTIVITY_OUTPUTS)
    items, _ = handler.scan(limit=4, occupied=frozenset(), cursor=None)
    assert [item.observation for item in items if not item.cursor_only] == [("claude", "runtime")]
    assert await handler.process(items[0])
    if grace:
        assert deliveries == []
        assert registry.has_recovered_output("claude", "runtime")
        sched._schedule_runtime_work_wake.assert_called()
        agent.ACTIVITY_OUTPUT_FLUSH_GRACE_SECONDS = 0
        due, _ = handler.scan(limit=4, occupied=frozenset(), cursor=None)
        output = next(item for item in due if not item.cursor_only)
        assert await handler.process(output)
    assert len(deliveries) == 1
    assert deliveries[0].id == "task"
    assert deliveries[0].status == "completed"
    # SDK completion receipts are not assistant text, even after recovery.
    assert "summary" not in deliveries[0].metadata
    assert not registry.has_completed_output("claude", "runtime")
    assert not registry.completion_persistence_recovery("claude", "runtime")
    assert await handler.process(items[0])
    assert len(deliveries) == 1


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
    busy_seen = threading.Event()

    @event.listens_for(activity_store.engine, "before_cursor_execute")
    def write_thread(_conn, _cursor, statement, *_):
        if statement.startswith("UPDATE runtime_records"):
            write_threads.append(threading.get_ident())

    @event.listens_for(activity_store.engine, "handle_error")
    def contended(context):
        if str(context.original_exception) == "database is locked":
            busy.append(context.statement)
            busy_seen.set()

    holder = _holder()
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
        assert await asyncio.to_thread(busy_seen.wait, 12)
        await asyncio.to_thread(holder.execute, "COMMIT")
        await asyncio.wait_for(handled.wait(), 5)
        await ticker
        assert len(busy) == 1, "the first full 5-second writer wait must actually exhaust"
        assert len(write_threads) == 2 and all(thread != loop_thread for thread in write_threads)
        assert len(ticks) > 100
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 1
        assert not registry.has_active("claude", "runtime")
        assert registry.session_state("session")["pending_activity_output_count"] == 1
        assert activity_store.list_activities()[0]["phase"] == "awaiting_output"
    finally:
        if holder.in_transaction:
            holder.rollback()
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


@pytest.mark.parametrize(
    ("blocked_stage", "next_owner"),
    [("replacement", "same"), ("tagged_cleanup", "gone"),
     ("untagged_cleanup", "gone"), ("replacement", "successor"),
     ("replacement", "provenance_retry")],
)
async def test_terminal_ack_resumes_committed_compensation_without_reinserting(
    activity_store, monkeypatch, blocked_stage, next_owner,
):
    """A later BUSY must not erase the receipt for an earlier committed stage.

    Ordinary ack tests never replace the loop snapshot during deletion or fail
    the CAS after restoration. Exercise every committed compensation payload,
    then prove another acknowledgement can still remove the exact durable row.
    """
    _no_wait(activity_store)
    registry = SessionActivityRegistry(activity_store)
    _start(registry)
    completed = registry.complete(
        backend="claude", runtime_key="runtime", activity_id="task",
        status="failed", retain_terminal_snapshot=True,
    )
    replacement = replace(completed, metadata={"classification": "new"})
    restore = activity_store.restore_activity_if_missing
    restorations = []

    def record_restore(**kwargs):
        result = restore(**kwargs)
        restorations.append(result)
        return result

    monkeypatch.setattr(activity_store, "restore_activity_if_missing", record_restore)
    busy = []

    @event.listens_for(activity_store.engine, "handle_error")
    def contended(context):
        if str(context.original_exception) == "database is locked":
            busy.append(context.statement)

    holder = None
    steps = 0

    async def run_sync(operation):
        nonlocal holder, steps
        result = await asyncio.to_thread(operation)
        steps += 1
        if steps == 1:
            # Inject only the loop-owned snapshot change while deletion lands.
            # Persistence/restore/replacement/cleanup still use the real store.
            with registry._lock:
                registry._terminal_snapshots[("claude", "runtime")]["task"] = replacement
        if (steps == 2 and blocked_stage != "untagged_cleanup") or (
            steps == 3 and blocked_stage == "untagged_cleanup"
        ):
            if blocked_stage != "replacement":
                with registry._lock:
                    registry._remove_recovered_terminal_locked(("claude", "runtime", "task"))
            holder = _holder()
        return result

    try:
        with pytest.raises(OperationalError):
            await registry.ack_recovered_terminal_async(completed, run_sync=run_sync)
        assert len(busy) == 3
        assert restorations == [True]
        [landed] = activity_store.list_activities()
        assert ("_terminal_ack_restore_id" in landed["activity"]["metadata"]) == (
            blocked_stage != "untagged_cleanup"
        )
    finally:
        if holder is not None:
            holder.close()

    if next_owner == "successor":
        replacement = replace(replacement, metadata={"classification": "successor"})
        with registry._lock:
            registry._terminal_snapshots[("claude", "runtime")]["task"] = replacement
        activity_store.upsert_activity(replacement.to_dict(), phase="terminal")
    elif next_owner == "provenance_retry":
        with registry._lock:
            registry._record_provenance_persistence_recovery(
                replacement, phase="terminal", error=RuntimeError("classification write failed"),
            )
        # This public scan retries provenance and overwrites our tag with the
        # same loop snapshot. A refused CAS means lost ownership, not BUSY.
        assert registry.terminal_snapshots_for_runtime("claude", "runtime") == [replacement]
        with pytest.raises(RuntimeError, match="compensation lost its owner"):
            await registry.ack_recovered_terminal_async(completed)

    assert await registry.ack_recovered_terminal_async(completed) is False
    if next_owner == "provenance_retry":
        assert restorations == [True, False], "lost ownership must be rechecked without overwriting the winner"
    else:
        assert restorations == [True], "a committed insert is not a replayable stage"
    assert registry._pending_terminal_acks == {}
    if next_owner == "gone":
        assert activity_store.list_activities() == []
    else:
        [durable] = activity_store.list_activities()
        assert durable["activity"] == replacement.to_dict()
        assert await registry.ack_recovered_terminal_async(replacement) is True
        assert activity_store.list_activities() == []
    assert registry.terminal_snapshots_for_runtime("claude", "runtime") == []


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
