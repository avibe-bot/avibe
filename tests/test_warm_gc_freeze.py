"""The warm freeze: armed once, fires at the first natural full collection after
the warm-up whatever is streaming, and rolls back without restart.
Real GC operations are confined to disposable child interpreters."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import loop_watchdog as wd


@pytest.mark.parametrize("case", ["default", "before_startup", "before_watchdog", "after_arm"])
def test_real_interpreter_freeze_ownership(case):
    subprocess.run(
        [sys.executable, "-I", "-S", str(Path(__file__).with_name("warm_gc_child.py")), case],
        check=True, timeout=20,
    )


def test_freeze_fires_on_the_first_warm_full_collection_even_with_open_streams(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="core.loop_watchdog")
    calls = []
    monkeypatch.setattr(wd.gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(wd.gc, "unfreeze", lambda: calls.append("unfreeze"))
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 0)
    monkeypatch.setattr(wd, "_active_requests", 3)
    clock = [0.0]
    w = wd.EventLoopWatchdog(SimpleNamespace(), monotonic=lambda: clock[0])

    def collect(generation=2):
        info = dict(generation=generation, collected=0, uncollectable=0)
        w._observe_gc("start", info)
        w._observe_gc("stop", info)
        w._answer()

    collect()
    assert not calls  # not armed
    assert w.arm_warm_freeze() == "armed"
    clock[0] = 7199
    collect()  # before the warm-up
    clock[0] = 7201
    collect(1)  # not a full collection
    assert not calls
    collect()  # three model streams open: it fires anyway
    assert calls == ["freeze"] and w._freeze_state == "frozen"
    assert "Warm GC trial frozen: open_streams=3" in caplog.text
    collect()
    assert calls == ["freeze"]  # one shot
    assert w.arm_warm_freeze() == "frozen"
    assert w.rollback_warm_freeze() == "retired" and calls == ["freeze", "unfreeze"]
    assert w.arm_warm_freeze() == "retired" and w.rollback_warm_freeze() == "retired"
    assert calls == ["freeze", "unfreeze"]


def test_rollback_before_the_freeze_retires_the_arm(monkeypatch):
    calls = []
    monkeypatch.setattr(wd.gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 0)
    monkeypatch.setattr(wd, "_active_requests", 0)
    w = wd.EventLoopWatchdog(SimpleNamespace(), monotonic=lambda: 10_000.0)
    w._watchdog_birth = 0.0
    w.arm_warm_freeze()
    assert w.rollback_warm_freeze() == "retired"
    info = dict(generation=2, collected=0, uncollectable=0)
    w._observe_gc("start", info)
    w._observe_gc("stop", info)
    w._answer()
    assert not calls


def test_an_existing_freeze_by_someone_else_is_left_alone(monkeypatch):
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 5)
    w = wd.EventLoopWatchdog(SimpleNamespace())
    assert w.arm_warm_freeze() == "off"



def test_a_heartbeat_answered_after_stop_does_not_freeze(monkeypatch):
    calls = []
    monkeypatch.setattr(wd.gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: 0)
    monkeypatch.setattr(wd, "_active_requests", 0)
    w = wd.EventLoopWatchdog(SimpleNamespace(), monotonic=lambda: 10_000.0)
    w._watchdog_birth = 0.0
    w.arm_warm_freeze()
    info = dict(generation=2, collected=0, uncollectable=0)
    w._observe_gc("start", info)
    w._observe_gc("stop", info)
    w.stop()  # a queued answer may still run after this
    w._answer()
    assert not calls


def test_a_foreign_freeze_after_arm_is_not_taken_over(monkeypatch):
    calls, frozen = [], [0]
    monkeypatch.setattr(wd.gc, "freeze", lambda: calls.append("freeze"))
    monkeypatch.setattr(wd.gc, "unfreeze", lambda: calls.append("unfreeze"))
    monkeypatch.setattr(wd.gc, "get_freeze_count", lambda: frozen[0])
    monkeypatch.setattr(wd, "_active_requests", 0)
    w = wd.EventLoopWatchdog(SimpleNamespace(), monotonic=lambda: 10_000.0)
    w._watchdog_birth = 0.0
    assert w.arm_warm_freeze() == "armed"
    frozen[0] = 1234  # another component froze the heap meanwhile
    info = dict(generation=2, collected=0, uncollectable=0)
    w._observe_gc("start", info)
    w._observe_gc("stop", info)
    w._answer()
    assert w._freeze_state == "armed" and not calls
    assert w.rollback_warm_freeze() == "retired" and not calls  # never unfreezes what it did not freeze
