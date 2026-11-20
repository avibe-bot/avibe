"""Exercise freeze-worker races without touching the process heap or any service."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from core import loop_watchdog as wd
from core.session_turns import SessionTurnManager


@pytest.mark.parametrize("ledger_fails", [False, True])
def test_cancel_pending_worker_cannot_freeze(monkeypatch, ledger_fails, caplog):
    entered, allow = threading.Event(), threading.Event()
    calls = []

    def ledger(apply):
        entered.set()
        assert allow.wait(3)
        apply()
        if ledger_fails:
            raise RuntimeError("private-ledger-message")

    manager = SimpleNamespace(in_flight={}, with_idle_turn_ledger=ledger)
    monkeypatch.setattr(wd.gc, "freeze", lambda: calls.append("frozen"))
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 0)

    async def run():
        loop = asyncio.get_running_loop()
        w = wd.EventLoopWatchdog(loop)
        w.arm_warm_freeze(manager)
        receipt = w._try_warm_freeze(manager)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert receipt.cancel() and not receipt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await receipt
            assert w._freeze_task is receipt  # slot still occupied by the worker
        finally:
            allow.set()
        for _ in range(300):
            if w._freeze_task is None:
                break
            await asyncio.sleep(0.01)
        assert w._freeze_task is None and not calls
        assert "private-ledger-message" not in caplog.text
        assert ("quiescence unavailable" in caplog.text) == ledger_fails

    asyncio.run(run())


@pytest.mark.parametrize("admit_turn", [False, True])
def test_freeze_instruction_excludes_transient_publication(monkeypatch, admit_turn):
    manager = SessionTurnManager()
    manager.with_idle_turn_ledger = lambda apply: apply()
    entered, attempting = threading.Event(), threading.Event()
    observed = []
    monkeypatch.setattr(wd, "_active_requests", 0)
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 0)

    def freeze():
        entered.set()
        assert attempting.wait(3)
        observed.append((bool(manager.in_flight), wd._active_requests, wd.transient_admission_lock.locked()))

    monkeypatch.setattr(wd.gc, "freeze", freeze)

    def admit():
        assert entered.wait(3)
        attempting.set()
        if admit_turn:
            manager._publish_local_turn("test", object())
        else:
            wd.model_stream_opened()

    async def run():
        w = wd.EventLoopWatchdog(asyncio.get_running_loop())
        w.arm_warm_freeze(manager)
        await asyncio.gather(w._try_warm_freeze(manager), asyncio.to_thread(admit))

    asyncio.run(run())
    assert observed == [(False, 0, True)]
    assert bool(manager.in_flight) if admit_turn else wd._active_requests == 1
