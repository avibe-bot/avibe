from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import types
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from core.memory import CaptureRequest, CaptureSkipped
from core.memory_adapter import DisabledMemoryAdapter, EnabledMemoryAdapter, TurnAccepted
from vibe.memory_contract import (
    MemoryPluginUnavailableError,
    MemoryRuntimeCloseUnprovedError,
    MemoryStoreUnavailableError,
)


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


def test_memory_cli_session_keeps_authenticated_boundary_when_plugin_is_unavailable() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=types.SimpleNamespace(enabled=True),
    )
    controller._memory_plugin_error = MemoryPluginUnavailableError("injected")
    controller._memory_scopes_by_session = {}
    controller._memory_cli_facts_by_session = {}
    controller._memory_plugin_cli_sessions = set()
    controller._memory_admission = lambda: pytest.fail(
        "plugin failure must be projected before admission imports"
    )
    controller._memory_turn_facts = lambda _context: pytest.fail(
        "plugin failure must be projected before facts imports"
    )
    context = types.SimpleNamespace(
        platform_specific={
            "agent_session_target": {"id": "session-plugin"},
        },
        platform="avibe",
    )

    assert Controller.configure_memory_cli_session(
        controller,
        context,
        admitted=True,
    )
    assert Controller.memory_scope_for_cli_session(controller, "session-plugin") == (
        "__memory_plugin_error__",
        "default",
    )


async def _start_disabled_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    reconcile_orphans: Callable[[], Awaitable[None]],
) -> tuple[Controller, asyncio.Task[None], Path]:
    memory_dir = paths.get_vibe_remote_dir() / "memory"
    record_path = memory_dir / ".rt" / "everos.sidecar.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}", encoding="utf-8")

    class _Reconciler:
        def __init__(self, **_kwargs) -> None:
            pass

        async def reconcile_orphans(self) -> None:
            await reconcile_orphans()

    process_module = types.ModuleType("core.memory.process")
    process_module.ReleasedEverOSOrphanReconciler = _Reconciler
    monkeypatch.setitem(sys.modules, "core.memory.process", process_module)

    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller._memory_disabled_cleanup_task = None
    await controller._schedule_disabled_memory_cleanup()
    cleanup_task = controller._memory_disabled_cleanup_task
    assert cleanup_task is not None
    return controller, cleanup_task, record_path


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
from core.memory_adapter import DisabledMemoryAdapter, TurnAccepted
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
asyncio.run(controller._schedule_disabled_memory_cleanup())
assert controller._memory_disabled_cleanup_task is None
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
        is_original_human_text=True,
    )
    before = set(asyncio.all_tasks())
    controller.memory_adapter.offer(
        TurnAccepted(context, "remember nothing", "session-1", None)
    )
    created = set(asyncio.all_tasks()).difference(before)
    assert offered == [
        TurnAccepted(context, "remember nothing", "session-1", None)
    ]
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
import asyncio
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
from core import internal_server
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
internal_server.create_app(controller, memory_ui_secret="test-secret")
status = asyncio.run(controller.memory_status_payload())
assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
assert controller.memory_runtime is None
assert status == {
    "status": "ok",
    "source": {"status": "unavailable", "observed_at": None, "reason": "memory_disabled"},
    "health": None,
    "state": "disabled",
    "reason": None,
    "attachment_capture": {"status": "not_configured"},
}
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
    assert _memory_state_entries(tmp_path / "home") == []


def test_enabled_controller_degrades_when_runtime_attribute_probe_fails(
    tmp_path: Path,
) -> None:
    script = r'''
import sys
import types

from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from core.memory_adapter import DisabledMemoryAdapter
from vibe.memory_contract import MemoryPluginUnavailableError


class BrokenRuntime(types.ModuleType):
    def __getattr__(self, name):
        if name in {"MEMORY_RUNTIME_PROTOCOL_VERSION", "create_memory_runtime"}:
            raise RuntimeError("broken optional runtime attribute")
        raise AttributeError(name)


sys.modules["core.memory.runtime"] = BrokenRuntime("core.memory.runtime")
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
    "memory": {
        "enabled": True,
        "processing": {
            "llm": {
                "base_url": "https://llm.example.test/v1",
                "model": "chat",
                "api_key": "llm-key",
            },
            "embedding": {
                "base_url": "https://embed.example.test/v1",
                "model": "embed",
                "api_key": "embed-key",
            },
        },
    },
    "setup_completed": True,
}))
controller = Controller(config)
assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
assert controller.memory_runtime is None
assert isinstance(controller._memory_plugin_error, MemoryPluginUnavailableError)
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


def test_host_surfaces_import_with_evacuated_memory_leaves_blocked(
    tmp_path: Path,
) -> None:
    script = r'''
import importlib.abc
import sys

ALLOWED_HOST_LEAVES = {"core.memory.admission_metadata"}


class BlockMemoryImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        del path, target
        if (
            fullname.startswith("core.memory.")
            and fullname not in ALLOWED_HOST_LEAVES
        ):
            raise ImportError(f"blocked optional Memory implementation: {fullname}")
        return None


sys.meta_path.insert(0, BlockMemoryImplementation())

from config.v2_config import V2Config
from core import internal_server, system_prompt_injection
from vibe import cli, internal_client, ui_memory_routes

assert V2Config is not None
assert internal_server is not None
assert system_prompt_injection is not None
assert cli is not None
assert internal_client is not None
assert ui_memory_routes is not None
loaded_memory_children = {
    name for name in sys.modules if name.startswith("core.memory.")
}
assert loaded_memory_children == ALLOWED_HOST_LEAVES
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
@pytest.mark.parametrize(
    "record_relative_path",
    [
        Path(".rt/everos.sidecar.json"),
        Path(".avibe-memory-locks/cascade-sync-owned.json"),
    ],
)
async def test_disabled_cleanup_reaps_only_preexisting_everos_ownership(
    monkeypatch: pytest.MonkeyPatch,
    record_relative_path: Path,
) -> None:
    memory_dir = paths.get_vibe_remote_dir() / "memory"
    record_path = memory_dir / record_relative_path
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}", encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    class _Reconciler:
        def __init__(self, *, provider_root, effective_home) -> None:
            self._paths = (provider_root, effective_home)

        async def reconcile_orphans(self) -> None:
            calls.append(self._paths)

    process_module = types.ModuleType("core.memory.process")
    process_module.ReleasedEverOSOrphanReconciler = _Reconciler
    monkeypatch.setitem(sys.modules, "core.memory.process", process_module)

    controller = Controller.__new__(Controller)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller._memory_disabled_cleanup_task = None

    await controller._schedule_disabled_memory_cleanup()

    assert controller._memory_disabled_cleanup_task is not None
    await controller._memory_disabled_cleanup_task
    assert calls == [
        (
            memory_dir / "everos-root",
            paths.get_vibe_remote_dir(),
        )
    ]


@pytest.mark.asyncio
async def test_disabled_cleanup_pending_projects_degraded_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def reconcile_orphans() -> None:
        cleanup_started.set()
        await cleanup_release.wait()
        record_path.unlink()

    controller, cleanup_task, record_path = await _start_disabled_cleanup(
        monkeypatch,
        reconcile_orphans,
    )
    await cleanup_started.wait()

    try:
        status = await asyncio.wait_for(
            controller.memory_status_payload(),
            timeout=0.1,
        )
        assert status["state"] == "degraded"
        assert status["reason"] == "memory_runtime_busy"
        assert status["source"]["reason"] == "memory_runtime_busy"
    finally:
        cleanup_release.set()
        await cleanup_task


@pytest.mark.asyncio
async def test_disabled_cleanup_failure_remains_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reconcile_orphans() -> None:
        raise RuntimeError("cleanup failed")

    controller, cleanup_task, _record_path = await _start_disabled_cleanup(
        monkeypatch,
        reconcile_orphans,
    )
    await cleanup_task

    status = await controller.memory_status_payload()

    assert status["state"] == "degraded"
    assert status["reason"] == "memory_runtime_busy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remove_ownership", "expected_state"),
    [(False, "degraded"), (True, "disabled")],
)
async def test_disabled_cleanup_success_rechecks_released_ownership(
    monkeypatch: pytest.MonkeyPatch,
    remove_ownership: bool,
    expected_state: str,
) -> None:
    async def reconcile_orphans() -> None:
        if remove_ownership:
            record_path.unlink()

    controller, cleanup_task, record_path = await _start_disabled_cleanup(
        monkeypatch,
        reconcile_orphans,
    )
    await cleanup_task

    status = await controller.memory_status_payload()

    assert status["state"] == expected_state
    assert status["reason"] == (
        "memory_runtime_busy" if expected_state == "degraded" else None
    )


@pytest.mark.asyncio
async def test_memory_reconcile_lazily_enters_and_leaves_enabled_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []
    capture_started = asyncio.Event()
    capture_stopped = asyncio.Event()

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.available = True
            self.closed = False

        async def reconcile(self, config) -> dict[str, object]:
            return {
                "ok": True,
                "state": "running" if config.enabled else "disabled",
            }

        async def close(self) -> None:
            assert capture_stopped.is_set()
            self.closed = True

    runtime = _Runtime()

    def create_memory_runtime(config, **_kwargs):
        created.append(config)
        return runtime

    runtime_module = types.ModuleType("core.memory.runtime")
    runtime_module.MEMORY_RUNTIME_PROTOCOL_VERSION = 1
    runtime_module.create_memory_runtime = create_memory_runtime
    monkeypatch.setitem(sys.modules, "core.memory.runtime", runtime_module)

    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_disabled_cleanup_unproved = True

    enabled = replace(controller.config.memory, enabled=True)
    assert await controller.reconcile_memory(enabled) == {
        "ok": True,
        "state": "running",
    }
    assert created == [enabled]
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert isinstance(controller.memory_adapter, EnabledMemoryAdapter)
    assert controller.config.memory.enabled is True

    async def capture_user_memory(*_args, **_kwargs) -> None:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            capture_stopped.set()

    controller.reserve_memory_capture_capacity = lambda *_args: None
    controller.capture_user_memory = capture_user_memory
    controller.memory_adapter.offer(
        TurnAccepted(object(), "remember this", "session-1", 0)
    )
    await capture_started.wait()

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
    assert controller._memory_disabled_cleanup_unproved is False


@pytest.mark.asyncio
async def test_disabled_preflight_uses_one_temporary_runtime() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    candidate = replace(controller.config.memory, enabled=True)
    calls: list[object] = []

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False

        async def preflight(self, config) -> dict[str, object]:
            calls.append(config)
            return {"ok": True}

        async def close(self) -> None:
            self.closed = True

    runtime = _Runtime()
    controller._create_memory_runtime = lambda config, **_kwargs: runtime

    assert await controller.preflight_memory(candidate) == {"ok": True}
    assert calls == [candidate]
    assert runtime.closed is True
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.parametrize("operation", ("preflight", "install"))
@pytest.mark.asyncio
async def test_explicit_recovery_retries_after_cached_plugin_failure(
    operation: str,
) -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_plugin_error = MemoryPluginUnavailableError("startup failed")
    candidate = replace(controller.config.memory, enabled=True)
    calls: list[tuple[str, object]] = []

    class _Runtime:
        module = object()
        closed = False
        retired = False

        async def preflight(self, config) -> dict[str, object]:
            calls.append(("preflight", config))
            return {"ok": True}

        async def install_artifact(self) -> dict[str, object]:
            calls.append(("install", controller.config.memory))
            return {"ok": True}

        async def close(self) -> None:
            self.closed = True

    runtime = _Runtime()
    controller._create_memory_runtime = lambda config, **_kwargs: runtime

    if operation == "preflight":
        result = await controller.preflight_memory(candidate)
        expected_calls = [("preflight", candidate)]
    else:
        result = await controller.install_memory_runtime()
        expected_calls = [("install", controller.config.memory)]

    assert result == {"ok": True}
    assert calls == expected_calls
    assert runtime.closed is True
    assert controller._memory_plugin_error is None
    assert controller.memory_runtime is None


@pytest.mark.asyncio
async def test_disabled_install_owns_runtime_until_successful_close() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    events: list[str] = []
    loader_flags: list[bool] = []

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False

        async def install_artifact(self) -> dict[str, object]:
            assert controller.memory_runtime is self
            events.append("install")
            return {"ok": True}

        def retire(self) -> None:
            self.retired = True
            events.append("retire")

        async def close(self) -> None:
            events.append("close")
            self.closed = True

    runtime = _Runtime()

    def create_runtime(config, **kwargs):
        loader_flags.append(bool(kwargs["allow_disabled"]))
        return runtime

    controller._create_memory_runtime = create_runtime

    assert await controller.install_memory_runtime() == {"ok": True}
    assert loader_flags == [True]
    assert events == ["install", "retire", "close"]
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_cancelled_install_does_not_cancel_shared_disabled_cleanup() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    created: list[object] = []

    async def cleanup() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False

        async def install_artifact(self) -> dict[str, object]:
            return {"ok": True}

        def retire(self) -> None:
            self.retired = True

        async def close(self) -> None:
            self.closed = True

    cleanup_task = asyncio.create_task(cleanup())
    controller._memory_disabled_cleanup_task = cleanup_task
    controller._create_memory_runtime = (
        lambda config, **_kwargs: created.append(config) or _Runtime()
    )

    first = asyncio.create_task(controller.install_memory_runtime())
    await cleanup_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert cleanup_task.done() is False
    second = asyncio.create_task(controller.install_memory_runtime())
    await asyncio.sleep(0)
    assert created == []

    cleanup_release.set()
    assert await second == {"ok": True}
    assert cleanup_task.done() is True
    assert created == [controller.config.memory]


@pytest.mark.asyncio
async def test_temporary_close_failure_retains_fenced_controller_ownership() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    created: list[object] = []

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False

        async def install_artifact(self) -> dict[str, object]:
            assert controller.memory_runtime is self
            return {"ok": True}

        def retire(self) -> None:
            self.retired = True

        async def close(self) -> None:
            raise RuntimeError("close failed")

    runtime = _Runtime()

    def create(config, **_kwargs):
        created.append(config)
        return runtime

    controller._create_memory_runtime = create

    with pytest.raises(RuntimeError, match="close failed"):
        await controller.install_memory_runtime()

    assert created == [controller.config.memory]
    assert runtime.retired is True
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)

    with pytest.raises(RuntimeError, match="remains fenced"):
        await controller.install_memory_runtime()
    assert created == [controller.config.memory]
    assert controller.memory_runtime is runtime


@pytest.mark.asyncio
async def test_disabled_status_reports_retained_fenced_runtime_truthfully() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_module = object()
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None

    runtime = types.SimpleNamespace(
        module=controller.memory_module,
        closed=False,
        retired=True,
    )
    controller.memory_runtime = runtime
    controller._create_memory_runtime = lambda _config, **_kwargs: pytest.fail(
        "disabled status must not construct Memory"
    )

    status = await controller.memory_status_payload()

    assert status["state"] == "degraded"
    assert status["reason"] == "memory_runtime_busy"
    assert status["source"]["reason"] == "memory_runtime_busy"
    assert controller.memory_runtime is runtime


@pytest.mark.asyncio
async def test_temporary_close_without_closed_proof_retains_ownership() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False

        async def preflight(self, _config) -> dict[str, object]:
            return {"ok": True}

        def retire(self) -> None:
            self.retired = True

        async def close(self) -> None:
            return None

    runtime = _Runtime()
    controller._create_memory_runtime = lambda _config, **_kwargs: runtime

    with pytest.raises(RuntimeError, match="closed proof"):
        await controller.preflight_memory(
            replace(controller.config.memory, enabled=True)
        )

    assert runtime.retired is True
    assert runtime.closed is False
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_disable_close_failure_fences_rollback_and_destructive_reuse() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    controller.memory_adapter = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    created: list[object] = []

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False
            self.reconciled: list[object] = []

        async def reconcile(self, config) -> dict[str, object]:
            self.reconciled.append(config)
            return {
                "ok": True,
                "state": "running" if config.enabled else "disabled",
            }

        async def close(self) -> None:
            raise RuntimeError("close failed")

        def retire(self) -> None:
            self.retired = True

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module

    def create(config, **_kwargs):
        created.append(config)
        raise AssertionError("rollback must not construct a second runtime")

    controller._create_memory_runtime = create

    with pytest.raises(RuntimeError, match="close failed"):
        await controller.reconcile_memory(disabled)

    assert controller.config.memory == disabled
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert runtime.retired is True

    with pytest.raises(MemoryRuntimeCloseUnprovedError, match="fenced"):
        await controller.reconcile_memory(enabled)
    with pytest.raises(MemoryRuntimeCloseUnprovedError, match="fenced"):
        async with controller._destructive_memory_runtime():
            pytest.fail("destructive operations must not reuse a closing runtime")

    assert runtime.reconciled == [disabled]
    assert created == []
    assert controller.memory_runtime is runtime
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_disabled_status_preserves_legacy_repair_fence_without_runtime() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=replace(
            _disabled_app_config().memory,
            legacy_needs_repair=True,
        )
    )
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None

    status = await controller.memory_status_payload()

    assert status["state"] == "needs_repair"
    assert status["reason"] == "memory_legacy_recovery_required"
    assert status["source"]["reason"] == "memory_disabled"


@pytest.mark.asyncio
async def test_disabled_cleanup_fence_precedes_legacy_repair_projection() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=replace(
            _disabled_app_config().memory,
            legacy_needs_repair=True,
        )
    )
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_disabled_cleanup_task = None
    controller._memory_disabled_cleanup_unproved = True

    status = await controller.memory_status_payload()

    assert status["state"] == "degraded"
    assert status["reason"] == "memory_runtime_busy"
    assert status["source"]["reason"] == "memory_runtime_busy"


@pytest.mark.asyncio
async def test_disabled_dashboard_reads_do_not_construct_runtime() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    factory_calls: list[object] = []

    def create_runtime(config, **_kwargs):
        factory_calls.append(config)
        raise AssertionError("disabled dashboard reads must not construct Memory")

    controller._create_memory_runtime = create_runtime

    processing, maintenance = await asyncio.gather(
        controller.memory_processing_record_payload(verified_user_key="user-1"),
        controller.memory_maintenance_payload(verified_user_key="user-1"),
    )

    unavailable = {
        "status": "unavailable",
        "observed_at": None,
        "reason": "memory_disabled",
    }
    assert processing == {
        "status": "ok",
        "runtime": {"source": unavailable, "health": None},
        "sources": {
            "memcells": unavailable,
            "runs": unavailable,
            "semantic": unavailable,
        },
        "anomalies": {"source": unavailable, "items": []},
        "maintenance": {
            "source": unavailable,
            "data_exists": True,
            "can_delete_data": True,
        },
    }
    assert maintenance == {
        "status": "ok",
        "data_exists": True,
        "can_delete_data": True,
    }
    assert factory_calls == []


@pytest.mark.asyncio
async def test_disabled_wake_and_remember_are_host_derived_without_runtime() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._create_memory_runtime = lambda _config, **_kwargs: pytest.fail(
        "disabled outcomes must not construct Memory"
    )

    assert await controller.wake_memory() == {
        "ok": False,
        "state": "disabled",
        "error": "memory_disabled",
    }
    assert await controller.capture_memory(
        CaptureRequest(
            source_message_id="message-1",
            session_id="session-1",
            principal_id="u-11111111111111111111111111111111",
            project_id="default",
            provenance="agent",
            text="remember this",
            occurred_at_ms=1,
        )
    ) == CaptureSkipped(reason="memory_disabled")


@pytest.mark.asyncio
async def test_plugin_failure_rechecked_after_blocked_capture_lock() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=types.SimpleNamespace(enabled=True))
    controller._memory_plugin_error = None
    controller.memory_runtime = None
    controller._memory_runtime_generation = 1
    request = CaptureRequest(
        source_message_id="message-1",
        session_id="session-1",
        principal_id="u-11111111111111111111111111111111",
        project_id="default",
        provenance="agent",
        text="remember this",
        occurred_at_ms=1,
    )

    gate = controller._memory_replacement_lock()
    await gate.acquire()
    capture = asyncio.create_task(controller.capture_memory(request))
    await asyncio.sleep(0)
    assert not capture.done()
    controller._memory_plugin_error = MemoryPluginUnavailableError("injected")
    gate.release()

    with pytest.raises(MemoryPluginUnavailableError):
        await capture


@pytest.mark.asyncio
async def test_disabled_store_backed_read_is_gated_before_runtime_construction() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None

    controller._create_memory_runtime = lambda _config, **_kwargs: pytest.fail(
        "disabled reads must not construct Memory"
    )

    with pytest.raises(MemoryStoreUnavailableError, match="Memory is disabled"):
        await controller.memory_projects_payload(
            verified_user_key=None,
            cli_scope=("u-11111111111111111111111111111111", "default"),
        )

    assert controller.memory_runtime is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_enabled_status_does_not_wait_for_startup_wake() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=replace(_disabled_app_config().memory, enabled=True)
    )
    controller.memory_adapter = None
    controller._memory_disabled_cleanup_task = None
    wake_release = asyncio.Event()

    async def startup_wake() -> None:
        await wake_release.wait()

    controller._memory_reconcile_task = asyncio.create_task(startup_wake())

    class _Runtime:
        module = object()
        closed = False
        retired = False

        async def status_payload(self) -> dict[str, object]:
            return {"status": "ok", "state": "starting"}

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module

    try:
        assert await asyncio.wait_for(
            controller.memory_status_payload(),
            timeout=0.1,
        ) == {"status": "ok", "state": "starting"}
    finally:
        wake_release.set()
        await controller._memory_reconcile_task


@pytest.mark.asyncio
async def test_enabled_status_reprojects_when_disabled_before_borrow() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    controller.memory_adapter = None
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    borrow_started = asyncio.Event()
    borrow_release = asyncio.Event()

    async def await_cleanup() -> None:
        borrow_started.set()
        await borrow_release.wait()

    controller._await_disabled_memory_cleanup = await_cleanup
    controller._create_memory_runtime = lambda _config, **_kwargs: pytest.fail(
        "status must re-project disabled state before constructing Memory"
    )
    status_task = asyncio.create_task(controller.memory_status_payload())
    await borrow_started.wait()

    condition = controller._memory_runtime_condition()
    async with condition:
        controller.config.memory = disabled
    borrow_release.set()

    status = await status_task

    assert status["status"] == "ok"
    assert status["state"] == "disabled"
    assert status["reason"] is None
    assert status["source"]["reason"] == "memory_disabled"


@pytest.mark.asyncio
async def test_enabled_status_reprojects_after_failed_disable_close() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    controller.memory_adapter = None
    controller._memory_disabled_cleanup_task = None
    status_waiting = asyncio.Event()
    status_release = asyncio.Event()

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False

        async def status_payload(self) -> dict[str, object]:
            pytest.fail("status must re-project before reading a retired runtime")

        async def reconcile(self, config) -> dict[str, object]:
            return {"ok": True, "state": "disabled"}

        async def close(self) -> None:
            raise RuntimeError("close failed")

        def retire(self) -> None:
            self.retired = True

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module

    async def hold_status_before_borrow() -> None:
        status_waiting.set()
        await status_release.wait()

    controller._await_disabled_memory_cleanup = hold_status_before_borrow
    status_task = asyncio.create_task(controller.memory_status_payload())
    await status_waiting.wait()

    async def cleanup_ready() -> None:
        return None

    controller._await_disabled_memory_cleanup = cleanup_ready
    try:
        with pytest.raises(RuntimeError, match="close failed"):
            await controller.reconcile_memory(disabled)
    finally:
        status_release.set()

    status = await status_task

    assert runtime.retired is True
    assert controller.memory_runtime is runtime
    assert status["status"] == "ok"
    assert status["state"] == "degraded"
    assert status["reason"] == "memory_runtime_busy"
    assert status["source"]["reason"] == "memory_runtime_busy"


@pytest.mark.asyncio
async def test_concurrent_enabled_memory_reads_overlap() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=replace(_disabled_app_config().memory, enabled=True)
    )
    controller.memory_adapter = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    started: set[str] = set()
    release = asyncio.Event()

    class _Runtime:
        module = object()
        closed = False
        retired = False

        async def status_payload(self) -> dict[str, object]:
            started.add("status")
            await release.wait()
            return {"status": "ok", "state": "running"}

        async def processing_record_payload(
            self,
            *,
            verified_user_key: str | None,
        ) -> dict[str, object]:
            assert verified_user_key == "user-1"
            started.add("processing")
            await release.wait()
            return {"status": "ok"}

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    status = asyncio.create_task(controller.memory_status_payload())
    processing = asyncio.create_task(
        controller.memory_processing_record_payload(
            verified_user_key="user-1",
        )
    )

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert started == {"status", "processing"}
    finally:
        release.set()
        await asyncio.gather(status, processing)


@pytest.mark.asyncio
async def test_disable_waits_for_outstanding_read_lease_then_closes_once() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    controller.memory_adapter = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    read_started = asyncio.Event()
    read_release = asyncio.Event()

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False
            self.close_calls = 0

        async def status_payload(self) -> dict[str, object]:
            read_started.set()
            await read_release.wait()
            return {"status": "ok", "state": "running"}

        async def reconcile(self, config) -> dict[str, object]:
            return {"ok": True, "state": "running" if config.enabled else "disabled"}

        async def close(self) -> None:
            self.close_calls += 1
            self.closed = True

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    status = asyncio.create_task(controller.memory_status_payload())
    await read_started.wait()
    disable = asyncio.create_task(controller.reconcile_memory(disabled))

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert controller._memory_replacement_lock().locked() is False
        assert disable.done() is False
        assert runtime.close_calls == 0
    finally:
        read_release.set()

    assert await status == {"status": "ok", "state": "running"}
    assert await disable == {"ok": True, "state": "disabled"}
    assert runtime.close_calls == 1
    assert controller.memory_runtime is None


@pytest.mark.asyncio
async def test_concurrent_temporary_borrows_share_runtime_until_final_release() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    candidate = replace(controller.config.memory, enabled=True)
    started = 0
    release = asyncio.Event()
    created: list[object] = []

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.closed = False
            self.retired = False
            self.close_calls = 0

        async def preflight(self, config) -> dict[str, object]:
            nonlocal started
            assert config is candidate
            assert controller.memory_runtime is self
            started += 1
            await release.wait()
            return {"ok": True}

        def retire(self) -> None:
            self.retired = True

        async def close(self) -> None:
            self.close_calls += 1
            self.closed = True

    def create_runtime(_config, **_kwargs) -> _Runtime:
        runtime = _Runtime()
        created.append(runtime)
        return runtime

    controller._create_memory_runtime = create_runtime
    first = asyncio.create_task(controller.preflight_memory(candidate))
    second = asyncio.create_task(controller.preflight_memory(candidate))

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert started == 2
        assert len(created) == 1
        assert created[0].close_calls == 0
    finally:
        release.set()

    assert await asyncio.gather(first, second) == [{"ok": True}, {"ok": True}]
    assert created[0].close_calls == 1
    assert controller.memory_runtime is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_temporary_borrows_do_not_share_different_configs() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    candidate = replace(controller.config.memory, enabled=True)
    candidate_started = asyncio.Event()
    candidate_release = asyncio.Event()
    install_started = asyncio.Event()
    created: list[object] = []

    class _Runtime:
        def __init__(self, config) -> None:
            self.config = config
            self.module = object()
            self.closed = False
            self.retired = False

        async def preflight(self, config) -> dict[str, object]:
            assert self.config == candidate
            assert config == candidate
            candidate_started.set()
            await candidate_release.wait()
            return {"ok": True}

        async def install_artifact(self) -> dict[str, object]:
            assert self.config == controller.config.memory
            install_started.set()
            return {"ok": True}

        def retire(self) -> None:
            self.retired = True

        async def close(self) -> None:
            self.closed = True

    def create_runtime(config, **_kwargs) -> _Runtime:
        created.append(config)
        return _Runtime(config)

    controller._create_memory_runtime = create_runtime
    preflight = asyncio.create_task(controller.preflight_memory(candidate))
    await candidate_started.wait()
    install = asyncio.create_task(controller.install_memory_runtime())
    await asyncio.sleep(0)

    assert install_started.is_set() is False
    assert created == [candidate]

    candidate_release.set()
    assert await preflight == {"ok": True}
    assert await install == {"ok": True}
    assert created == [candidate, controller.config.memory]
    assert controller.memory_runtime is None


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

    class _MemoryAdapter:
        def quiesce_memory_capture_tasks(self) -> None:
            started.append("capture-registration")

        async def cancel_memory_capture_tasks(self) -> None:
            started.append("capture")

    class _MemoryRuntime:
        async def close(self) -> None:
            await stubborn_stage("runtime")

    controller.memory_adapter = _MemoryAdapter()
    controller.memory_runtime = _MemoryRuntime()

    async def join_destructive_transactions() -> None:
        started.append("destructive")

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
    assert cancelled == ["runtime"]
    assert controller._shutdown_tainted is True

    release.set()
    leftovers = set(asyncio.all_tasks()).difference(before)
    if leftovers:
        await asyncio.gather(*leftovers, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_does_not_close_runtime_over_live_destructive_transaction() -> None:
    controller = Controller.__new__(Controller)
    controller._memory_reconcile_task = None
    controller._memory_shutdown_budget_seconds = 0.04
    controller._shutdown_tainted = False
    controller._memory_destructive_quiescing = False
    controller.message_handler = types.SimpleNamespace(
        quiesce_memory_capture_tasks=lambda: None,
    )
    release = asyncio.Event()

    async def transaction() -> dict[str, object]:
        await release.wait()
        return {"ok": True}

    destructive = asyncio.create_task(transaction())
    controller._memory_destructive_tasks = {destructive}

    class _MemoryRuntime:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    runtime = _MemoryRuntime()
    controller.memory_runtime = runtime

    started_at = time.monotonic()
    await controller._shutdown_memory_stack()

    assert time.monotonic() - started_at < 0.2
    assert destructive.done() is False
    assert runtime.closed is False
    assert controller._shutdown_tainted is True

    release.set()
    await destructive
    await asyncio.sleep(0)
