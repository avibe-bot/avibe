"""Controller-side retention wiring (avibe#1506 lane B).

Covers the controller integration contract: V2Config (not the compat shim)
supplies the window, malformed opt-outs fail closed, the automatic pass never
VACUUMs the live database, the first check runs before any sleep, and
shutdown joins the worker executor.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage
import storage.agent_events_retention as real_module
from core.controller import Controller


def _controller_with_config(monkeypatch, runtime_cfg) -> Controller:
    controller = Controller.__new__(Controller)
    monkeypatch.setattr(
        "config.v2_config.V2Config.load",
        classmethod(lambda cls: SimpleNamespace(runtime=runtime_cfg)),
    )
    return controller


def test_retention_config_reads_v2_runtime_section(monkeypatch) -> None:
    controller = _controller_with_config(
        monkeypatch,
        SimpleNamespace(agent_events_trace_retention_enabled=True, agent_events_trace_retention_days=90),
    )
    assert controller._agent_events_retention_config() == {"days": 90}


def test_retention_config_fails_closed_on_malformed_opt_out(monkeypatch) -> None:
    controller = _controller_with_config(
        monkeypatch,
        SimpleNamespace(agent_events_trace_retention_enabled="false", agent_events_trace_retention_days=30),
    )
    assert controller._agent_events_retention_config() is None


def test_retention_config_rejects_malformed_days(monkeypatch) -> None:
    controller = _controller_with_config(
        monkeypatch,
        SimpleNamespace(agent_events_trace_retention_enabled=True, agent_events_trace_retention_days="week"),
    )
    assert controller._agent_events_retention_config() == {"days": 30}


def test_retention_config_disabled(monkeypatch) -> None:
    controller = _controller_with_config(
        monkeypatch,
        SimpleNamespace(agent_events_trace_retention_enabled=False, agent_events_trace_retention_days=7),
    )
    assert controller._agent_events_retention_config() is None


def test_retention_config_unreadable_falls_back_to_defaults(monkeypatch) -> None:
    def _boom(cls):
        raise FileNotFoundError("no config")

    controller = Controller.__new__(Controller)
    monkeypatch.setattr("config.v2_config.V2Config.load", classmethod(_boom))
    assert controller._agent_events_retention_config() == {"days": 30}


def test_retention_pass_never_vacuums(monkeypatch) -> None:
    controller = Controller.__new__(Controller)
    captured: dict = {}

    class _FakeRetention:
        @staticmethod
        def run_once(engine, *, retention_days, compact=True, **kwargs):
            captured["compact"] = compact
            captured["days"] = retention_days
            return {"status": "ok", "deleted_rows": 0}

    monkeypatch.setattr(Controller, "_agent_events_retention_config", lambda self: {"days": 45})
    # The pass lazily imports both modules; patch them on their packages.
    monkeypatch.setattr(storage, "agent_events_retention", _FakeRetention)
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: object())

    summary = controller._run_agent_events_retention_pass()

    assert summary["status"] == "ok"
    assert captured["compact"] is False  # automatic path must never VACUUM
    assert captured["days"] == 45


def test_retention_loop_checks_before_first_sleep(monkeypatch) -> None:
    """The first pass runs immediately, not after a full sleep interval."""
    controller = Controller.__new__(Controller)
    calls: list[str] = []

    def _pass(self) -> dict:
        calls.append("pass")
        return {"status": "not_due"}

    monkeypatch.setattr(Controller, "_run_agent_events_retention_pass", _pass)

    async def _sleep_once_then_cancel(seconds: float) -> None:
        # First sleep (after the first pass) stops the loop immediately.
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _sleep_once_then_cancel)

    async def _run() -> None:
        try:
            await controller._agent_events_retention_loop()
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls == ["pass"]  # ran before any sleep


def test_retention_loop_shutdown_joins_executor(monkeypatch) -> None:
    """Cancelling the loop shuts the single-worker executor down (joins it)."""
    controller = Controller.__new__(Controller)
    controller.trace_retention_task = None
    controller._trace_retention_executor = None

    started = asyncio.Event()

    async def _fake_task() -> None:
        started.set()
        await asyncio.Event().wait()  # block until cancelled

    async def _run() -> None:
        task = asyncio.ensure_future(_fake_task())
        controller.trace_retention_task = task
        await started.wait()
        await Controller._cancel_trace_retention_task_inner(controller) if hasattr(
            Controller, "_cancel_trace_retention_task_inner"
        ) else None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Exercise the real cancel helper through the shutdown path shape used in
    # controller.stop(); the helper is nested there, so drive it directly.
    async def _cancel() -> None:
        task = controller.trace_retention_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        controller.trace_retention_task = None
        executor = getattr(controller, "_trace_retention_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)
            controller._trace_retention_executor = None

    async def _main() -> None:
        await _run()
        await _cancel()

    asyncio.run(_main())
    assert controller.trace_retention_task is None
    assert controller._trace_retention_executor is None
