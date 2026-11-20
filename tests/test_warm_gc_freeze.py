import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine

from core import loop_watchdog as wd
from core.session_turns import SessionTurnManager


def test_freeze_requires_idle_live_and_durable_turns_then_rolls_back(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/turns.sqlite")
    with engine.begin() as conn:
        conn.exec_driver_sql("create table session_turns (session_id text, state text)")
        conn.exec_driver_sql("insert into session_turns values ('restored','active')")
    manager = SessionTurnManager(SimpleNamespace())
    monkeypatch.setattr(manager, "_sqlite_engine", lambda: engine)
    calls = []
    monkeypatch.setattr(wd.gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(wd.gc, "unfreeze", lambda: calls.append("unfreeze"))
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 0)
    monkeypatch.setattr(wd, "_active_requests", 0)

    async def run():
        clock = [0.0]
        w = wd.EventLoopWatchdog(asyncio.get_running_loop(), monotonic=lambda: clock[0])

        def collect(gen=2):
            info = dict(generation=gen, collected=0, uncollectable=0)
            w._observe_gc("start", info)
            w._observe_gc("stop", info)
            w._answer()
            return w._freeze_task

        assert collect() is None and w.arm_warm_freeze(manager) == "armed"
        clock[0] = 7199
        assert collect() is None
        clock[0] = 7201
        assert collect(1) is None
        monkeypatch.setattr(wd, "_active_requests", 1)
        assert collect() is None
        monkeypatch.setattr(wd, "_active_requests", 0)
        manager.in_flight["local"] = object()
        assert collect() is None
        manager.in_flight.clear()
        await collect()
        assert not calls  # restored owner, absent from in_flight
        with engine.begin() as conn:
            conn.exec_driver_sql("delete from session_turns")
        task = collect()
        manager.in_flight["just-admitted"] = object()
        await task
        assert not calls
        manager.in_flight.clear()
        await collect()
        assert calls == ["freeze"] and collect() is None
        assert w.rollback_warm_freeze() == "retired" and w.arm_warm_freeze(manager) == "retired"
        assert w.rollback_warm_freeze() == "retired" and calls == ["freeze", "unfreeze"]
        other = wd.EventLoopWatchdog(asyncio.get_running_loop(), monotonic=lambda: clock[0])
        other.arm_warm_freeze(manager)
        other.rollback_warm_freeze()
        await other._try_warm_freeze(manager)
        assert calls == ["freeze", "unfreeze"]

    try:
        asyncio.run(run())
    finally:
        engine.dispose()
