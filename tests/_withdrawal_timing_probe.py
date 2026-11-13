"""Temporary native diagnostic; removed before the fix-forward PR is opened."""
import asyncio
import inspect
import json
import os
import threading
import time
from pathlib import Path

import pytest

from tests import test_model_hub_engine_admission as original_test


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["poll-native", "signal-native", "poll-legacy", "signal-legacy"])
@pytest.mark.parametrize("trial", range(3))
async def test_withdrawal_timing(tmp_path, monkeypatch, mode, trial):
    from config import atomic_io
    from core.handlers.model_hub import async_owner
    from storage.lock import MigrationFileLock
    from tests.engine_channel_support import LocalVerifier
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
    from vibe.model_hub_runtime.engine_channel import EngineChannel
    from vibe.model_hub_runtime.lifecycle import EngineLifecycle

    original = original_test.test_known_withdrawal_gates_and_expires_while_metadata_publication_is_stuck
    state = {"mode": mode, "trial": trial, "spins": 0, "spans": []}
    worker = threading.local()
    gated = asyncio.Event()
    begin = time.perf_counter()

    def now():
        return time.perf_counter() - begin

    def instrument(target, name, label):
        real = getattr(target, name)

        def measured(*args, **kwargs):
            if not getattr(worker, "checking", False):
                return real(*args, **kwargs)
            start = now()
            try:
                return real(*args, **kwargs)
            finally:
                state["spans"].append({"name": label, "start": start, "duration": now() - start})

        monkeypatch.setattr(target, name, measured)

    real_check = EngineLifecycle.check

    def check(self, **kwargs):
        if threading.current_thread() is threading.main_thread():
            return real_check(self, **kwargs)
        state["worker_entered"] = now()
        worker.checking = True
        notification = self.on_withdrawal

        def notified():
            state["withdrawal_notified"] = now()
            return notification()

        self.on_withdrawal = notified
        try:
            return real_check(self, **kwargs)
        finally:
            state["worker_finished"] = now()
            worker.checking = False
            if self.on_withdrawal is notified:
                self.on_withdrawal = notification

    monkeypatch.setattr(EngineLifecycle, "check", check)
    real_run = async_owner.run_owned_in_thread

    async def run(function, *args, **kwargs):
        if getattr(function, "__func__", None) is check:
            state["submitted"] = now()
        return await real_run(function, *args, **kwargs)

    monkeypatch.setattr(async_owner, "run_owned_in_thread", run)
    real_gate = CLIProxyEngineAdapter._gate_known_withdrawal

    def gate(self):
        result = real_gate(self)
        if self._engine_admission.reason == "engine_withdrawn":
            state.setdefault("gate_closed", now())
            gated.set()
        return result

    monkeypatch.setattr(CLIProxyEngineAdapter, "_gate_known_withdrawal", gate)
    real_sleep = asyncio.sleep

    async def sleep(delay, *args, **kwargs):
        if delay == 0:
            state["spins"] += 1
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    if mode.endswith("legacy"):
        monkeypatch.setattr(atomic_io, "replace_snapshot", os.replace)
    for target, name, label in (
        (EngineChannel, "refresh", "refresh"),
        (EngineChannel, "resolve", "resolve"),
        (LocalVerifier, "verify", "signature_verify"),
        (atomic_io, "open_snapshot", "open_snapshot"),
        (atomic_io, "replace_snapshot", "replace_snapshot"),
        (atomic_io, "write_atomic", "write_atomic"),
        (Path, "read_bytes", "read_bytes"),
        (os, "fsync", "fsync"),
        (MigrationFileLock, "__enter__", "state_lock"),
    ):
        instrument(target, name, label)

    source = inspect.getsource(original).replace("@pytest.mark.asyncio\n", "", 1)
    source = source.replace(
        'monkeypatch.setattr(owner, "publish_status", lambda: publication_allowed.wait(5))',
        'def blocked_publish():\n        state["publication_entered"] = now()\n        return publication_allowed.wait(5)\n    monkeypatch.setattr(owner, "publish_status", blocked_publish)',
    )
    if mode.startswith("signal"):
        source = source.replace("await asyncio.wait_for(wait_gate(), 1)", "await asyncio.wait_for(gated.wait(), 30)")
    namespace = {**vars(original_test), "state": state, "now": now, "gated": gated}
    exec(compile(source, original.__code__.co_filename, "exec"), namespace)
    try:
        await namespace[original.__name__](tmp_path, monkeypatch)
        state["outcome"] = "pass"
    except Exception as exc:
        state["outcome"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["finished"] = now()
        print("WITHDRAWAL_TIMING " + json.dumps(state, sort_keys=True))
