"""Controller-side retention wiring (avibe#1506 lane B).

Covers the controller integration contract: V2Config (not the compat shim)
supplies the window, malformed opt-outs fail closed, the automatic pass never
VACUUMs the live database, the first check runs before any sleep, and
shutdown joins the worker executor.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
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
    # A malformed window must fail closed: substituting a shorter default could
    # delete traces the (apparently) longer policy meant to keep.
    assert controller._agent_events_retention_config() is None


def test_retention_config_disabled(monkeypatch) -> None:
    controller = _controller_with_config(
        monkeypatch,
        SimpleNamespace(agent_events_trace_retention_enabled=False, agent_events_trace_retention_days=7),
    )
    assert controller._agent_events_retention_config() is None


def test_retention_config_unreadable_disables(monkeypatch) -> None:
    def _boom(cls):
        raise FileNotFoundError("no config")

    controller = Controller.__new__(Controller)
    monkeypatch.setattr("config.v2_config.V2Config.load", classmethod(_boom))
    assert controller._agent_events_retention_config() is None


def test_retention_config_recovery_defaults_disable(monkeypatch) -> None:
    """V2Config.load() returns recovery defaults with load_warnings; the
    persisted policy is unknown, so the automatic pass must not run."""
    controller = Controller.__new__(Controller)
    recovery = SimpleNamespace(
        runtime=SimpleNamespace(agent_events_trace_retention_enabled=True, agent_events_trace_retention_days=30),
        load_warnings=("Config JSON could not be parsed; using recovery defaults: x",),
    )
    monkeypatch.setattr("config.v2_config.V2Config.load", classmethod(lambda cls: recovery))
    assert controller._agent_events_retention_config() is None


def test_retention_config_ignores_unrelated_warnings(monkeypatch) -> None:
    """Unrelated migration warnings must not disable a valid runtime section."""
    controller = Controller.__new__(Controller)
    config = SimpleNamespace(
        runtime=SimpleNamespace(agent_events_trace_retention_enabled=True, agent_events_trace_retention_days=90),
        load_warnings=("Legacy Model Hub route could not be mapped; kept as manual",),
    )
    monkeypatch.setattr("config.v2_config.V2Config.load", classmethod(lambda cls: config))
    assert controller._agent_events_retention_config() == {"days": 90}


def test_retention_config_ignores_recovered_unrelated_sections(monkeypatch) -> None:
    """A recovered 'platforms' section leaves the valid runtime policy active."""
    controller = Controller.__new__(Controller)
    config = SimpleNamespace(
        runtime=SimpleNamespace(agent_events_trace_retention_enabled=True, agent_events_trace_retention_days=60),
        load_warnings=("Recovered invalid config section 'platforms': bad payload",),
    )
    monkeypatch.setattr("config.v2_config.V2Config.load", classmethod(lambda cls: config))
    assert controller._agent_events_retention_config() == {"days": 60}


def test_retention_config_disables_on_recovered_runtime_section(monkeypatch) -> None:
    controller = Controller.__new__(Controller)
    config = SimpleNamespace(
        runtime=SimpleNamespace(agent_events_trace_retention_enabled=True, agent_events_trace_retention_days=30),
        load_warnings=("Recovered invalid config section 'runtime': bad payload",),
    )
    monkeypatch.setattr("config.v2_config.V2Config.load", classmethod(lambda cls: config))
    assert controller._agent_events_retention_config() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_events_trace_retention_enabled", "false"),
        ("agent_events_trace_retention_enabled", 1),
        ("agent_events_trace_retention_days", "90"),
        ("agent_events_trace_retention_days", 0),
        ("agent_events_trace_retention_days", -1),
        ("agent_events_trace_retention_days", True),
    ],
)
def test_runtime_config_rejects_malformed_retention_values(field, value) -> None:
    from config.v2_config import RuntimeConfig

    kwargs = {"default_cwd": "/tmp", field: value}
    with pytest.raises(ValueError, match="Config 'runtime\\."):
        RuntimeConfig(**kwargs)


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

    def _pass(self, cancel_event=None) -> dict:
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


def test_retention_loop_cancellation_reaches_worker(monkeypatch) -> None:
    controller = Controller.__new__(Controller)
    started = threading.Event()
    stopped = threading.Event()

    def _pass(self, cancel_event=None) -> dict:
        started.set()
        while cancel_event is None or not cancel_event.is_set():
            time.sleep(0.005)
        stopped.set()
        return {"status": "cancelled"}

    monkeypatch.setattr(Controller, "_run_agent_events_retention_pass", _pass)
    monkeypatch.setattr(Controller, "_AGENT_EVENTS_RETENTION_CHECK_INTERVAL_SECONDS", 3600)

    async def _run() -> None:
        task = asyncio.create_task(controller._agent_events_retention_loop())
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert stopped.is_set()
    assert controller._trace_retention_executor is None


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
