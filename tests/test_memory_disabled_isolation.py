from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from core.memory_adapter import DisabledMemoryAdapter


ROOT = Path(__file__).resolve().parents[1]


def _disabled_app_config():
    return to_app_config(
        V2Config.from_payload(
            {
                "platform": "avibe",
                "platforms": {"enabled": [], "primary": "avibe"},
                "mode": "self_host",
                "version": "v2",
                "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
                "agents": {
                    "default_backend": "opencode",
                    "opencode": {"enabled": True, "cli_path": "opencode"},
                },
                "memory": {"enabled": False},
                "setup_completed": True,
            }
        )
    )


def _memory_state_entries(home: Path) -> list[Path]:
    if not home.exists():
        return []
    return [
        path.relative_to(home)
        for path in home.rglob("*")
        if any(
            "memory" in part.casefold() or "everos" in part.casefold()
            for part in path.relative_to(home).parts
        )
    ]


def test_memory_indep_013_disabled_fresh_startup_has_no_runtime_side_effects(
    tmp_path: Path,
) -> None:
    script = r'''
import asyncio
import sys
from pathlib import Path

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from core.memory_adapter import DisabledMemoryAdapter
from modules.im import MessageContext

config = to_app_config(V2Config.from_payload({
    "platform": "avibe",
    "platforms": {"enabled": [], "primary": "avibe"},
    "mode": "self_host",
    "version": "v2",
    "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
    "agents": {
        "default_backend": "opencode",
        "opencode": {"enabled": True, "cli_path": "opencode"},
    },
    "memory": {"enabled": False},
    "setup_completed": True,
}))
controller = Controller(config)
home = paths.get_vibe_remote_dir()

assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
assert controller.memory_runtime is None
assert controller.memory_module is None
assert "core.memory.runtime" not in sys.modules
assert "core.memory.process" not in sys.modules
assert not [
    path for path in home.rglob("*")
    if any("memory" in part.casefold() or "everos" in part.casefold()
           for part in path.relative_to(home).parts)
]
controller._schedule_disabled_memory_cleanup()
assert controller._memory_reconcile_task is None
assert "core.memory.process" not in sys.modules


async def offer_capture() -> None:
    offered = []

    class RecordingDisabledAdapter(DisabledMemoryAdapter):
        def offer(self, event, /) -> None:
            offered.append(event)

    controller.memory_adapter = RecordingDisabledAdapter()
    context = MessageContext(
        user_id="user-1",
        channel_id="session-1",
        platform="avibe",
        message_id="message-1",
        is_ordinary_text=True,
    )
    before = set(asyncio.all_tasks())
    controller.message_handler._schedule_text_only_memory_capture(
        context,
        "remember nothing",
        "session-1",
        expected_snapshot=None,
    )
    created = set(asyncio.all_tasks()).difference(before)
    assert offered == [(context, "remember nothing", "session-1")]
    assert not any(task.get_name().startswith("memory-") for task in created)


asyncio.run(offer_capture())
'''
    home = tmp_path / "home"
    environment = os.environ.copy()
    environment["AVIBE_HOME"] = str(home)
    environment["HOME"] = str(tmp_path)
    environment["PYTHONPATH"] = str(ROOT)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert _memory_state_entries(home) == []


def test_memory_indep_014_disabled_controller_starts_with_runtime_imports_blocked(
    tmp_path: Path,
) -> None:
    script = r'''
import importlib.abc
import sys

BLOCKED = {
    "core.memory.blocking",
    "core.memory.operation_lock",
    "core.memory.process",
    "core.memory.runtime",
    "core.memory.ui_access",
}


class BlockMemoryImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        del path, target
        if fullname in BLOCKED:
            raise ImportError(f"blocked optional Memory implementation: {fullname}")
        return None


sys.meta_path.insert(0, BlockMemoryImplementation())

from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from core.memory_adapter import DisabledMemoryAdapter

config = to_app_config(V2Config.from_payload({
    "platform": "avibe",
    "platforms": {"enabled": [], "primary": "avibe"},
    "mode": "self_host",
    "version": "v2",
    "runtime": {"default_cwd": "_tmp", "log_level": "INFO"},
    "agents": {
        "default_backend": "opencode",
        "opencode": {"enabled": True, "cli_path": "opencode"},
    },
    "memory": {"enabled": False},
    "setup_completed": True,
}))
controller = Controller(config)
assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
assert controller.memory_runtime is None
assert not BLOCKED.intersection(sys.modules)
'''
    environment = os.environ.copy()
    environment["AVIBE_HOME"] = str(tmp_path / "home")
    environment["HOME"] = str(tmp_path)
    environment["PYTHONPATH"] = str(ROOT)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_disabled_cleanup_reaps_only_preexisting_everos_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = paths.get_vibe_remote_dir() / "memory"
    record_path = memory_dir / ".rt" / "everos.sidecar.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}", encoding="utf-8")
    calls: list[tuple[Path, Path, Path, bool]] = []

    class _Ownership:
        def __init__(self, *, record_path, socket_path, provider_root) -> None:
            self._paths = (record_path, socket_path, provider_root)

        async def reap(self, *, discover_missing: bool = False) -> None:
            calls.append((*self._paths, discover_missing))

    process_module = types.ModuleType("core.memory.process")
    process_module.SidecarOwnership = _Ownership
    monkeypatch.setitem(sys.modules, "core.memory.process", process_module)

    controller = Controller.__new__(Controller)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller._memory_reconcile_task = None

    controller._schedule_disabled_memory_cleanup()

    assert controller._memory_reconcile_task is not None
    await controller._memory_reconcile_task
    assert calls == [
        (
            record_path,
            memory_dir / ".rt" / "everos.sock",
            memory_dir / "everos-root",
            False,
        )
    ]


@pytest.mark.asyncio
async def test_memory_reconcile_lazily_enters_and_leaves_enabled_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False

        async def reconcile(self, config) -> dict[str, object]:
            return {
                "ok": True,
                "state": "running" if config.enabled else "disabled",
            }

        async def close(self) -> None:
            self.closed = True

    runtime = _Runtime()

    def create_memory_runtime(config, **_kwargs):
        created.append(config)
        return runtime

    runtime_module = types.ModuleType("core.memory.runtime")
    runtime_module.create_memory_runtime = create_memory_runtime
    monkeypatch.setitem(sys.modules, "core.memory.runtime", runtime_module)

    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None

    enabled = replace(controller.config.memory, enabled=True)
    assert await controller.reconcile_memory(enabled) == {
        "ok": True,
        "state": "running",
    }
    assert created == [enabled]
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert controller.memory_adapter is None
    assert controller.config.memory.enabled is True

    disabled = replace(enabled, enabled=False)
    assert await controller.reconcile_memory(disabled) == {
        "ok": True,
        "state": "disabled",
    }
    assert runtime.closed is True
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert controller.config.memory.enabled is False


@pytest.mark.asyncio
async def test_memory_indep_015_shutdown_bounds_all_memory_stages_under_one_budget() -> None:
    controller = Controller.__new__(Controller)
    controller._memory_reconcile_task = None
    controller._memory_shutdown_budget_seconds = 0.06
    controller._shutdown_tainted = False
    controller._memory_destructive_quiescing = False

    release = asyncio.Event()
    started: list[str] = []
    cancelled: list[str] = []

    async def stubborn_stage(name: str) -> None:
        started.append(name)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(name)
            await release.wait()

    class _MessageHandler:
        def quiesce_memory_capture_tasks(self) -> None:
            started.append("capture-registration")

        async def cancel_memory_capture_tasks(self) -> None:
            await stubborn_stage("capture")

    class _MemoryRuntime:
        async def close(self) -> None:
            await stubborn_stage("runtime")

    controller.message_handler = _MessageHandler()
    controller.memory_runtime = _MemoryRuntime()

    async def join_destructive_transactions() -> None:
        await stubborn_stage("destructive")

    controller._join_memory_destructive_transactions = join_destructive_transactions

    before = set(asyncio.all_tasks())
    started_at = time.monotonic()
    await controller._shutdown_memory_stack()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert started == [
        "capture-registration",
        "capture",
        "destructive",
        "runtime",
    ]
    assert cancelled == ["capture", "destructive", "runtime"]
    assert controller._shutdown_tainted is True

    release.set()
    leftovers = set(asyncio.all_tasks()).difference(before)
    if leftovers:
        await asyncio.gather(*leftovers, return_exceptions=True)
