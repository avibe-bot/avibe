from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import tomllib
import types
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller
from avibe_memory import CaptureRequest, CaptureSkipped
from core.memory_adapter import DisabledMemoryAdapter, TurnAccepted
from vibe.memory_contract import (
    MemoryPluginIncompatibleError,
    MemoryPluginUnavailableError,
    MemoryRuntimeBusyError,
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

    process_module = types.ModuleType("avibe_memory.process")
    process_module.ReleasedEverOSOrphanReconciler = _Reconciler
    monkeypatch.setitem(sys.modules, "avibe_memory.process", process_module)

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
assert "avibe_memory.runtime" not in sys.modules
assert "avibe_memory.process" not in sys.modules
assert not [
    path for path in home.rglob("*")
    if any("memory" in part.casefold() or "everos" in part.casefold()
           for part in path.relative_to(home).parts)
]
asyncio.run(controller._schedule_disabled_memory_cleanup())
assert controller._memory_disabled_cleanup_task is None
assert "avibe_memory.process" not in sys.modules


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
    event = TurnAccepted(
        platform="avibe",
        user_id="user-1",
        message_id="message-1",
        session_id="session-1",
        text="remember nothing",
        files=(),
        is_dm=False,
        is_ordinary_text=True,
        is_ordinary_attachment=False,
        lifecycle_snapshot=None,
    )
    controller.memory_adapter.offer(event)
    created = set(asyncio.all_tasks()).difference(before)
    assert offered == [event]
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
    "avibe_memory.blocking",
    "avibe_memory.operation_lock",
    "avibe_memory.process",
    "avibe_memory.runtime",
    "avibe_memory.ui_access",
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


sys.modules["avibe_memory.runtime"] = BrokenRuntime("avibe_memory.runtime")
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


def test_host_surfaces_import_with_avibe_memory_blocked(
    tmp_path: Path,
) -> None:
    script = r'''
import importlib.abc
import sys

class BlockMemoryImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        del path, target
        if fullname == "avibe_memory" or fullname.startswith("avibe_memory."):
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
    name
    for name in sys.modules
    if name == "avibe_memory" or name.startswith("avibe_memory.")
}
assert loaded_memory_children == set()
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


def test_memory_indep_017_core_workflows_run_without_avibe_memory(
    tmp_path: Path,
) -> None:
    """Run representative core workflows with the implementation unavailable."""

    scenarios = (
        "tests/test_message_handler_typing.py::MessageHandlerTypingTests::test_im_human_input_enters_delivery_owner_before_backend",
        "tests/test_command_handler_user_names.py::CommandHandlerUserNameTests::test_new_command_sends_fresh_session_confirmation",
        "tests/test_session_archive.py::test_archive_reclaims_bound_resources",
        "tests/test_ui_server_fastapi.py::test_websocket_echo_smoke_when_enabled",
        "tests/test_internal_server.py::test_disabled_memory_status_route_uses_host_projection_without_runtime",
        "tests/test_vibe_cli.py::test_retention_help_reads_raw_config_without_loading_or_migrating",
        "tests/test_controller_dispatch_loop.py::test_cleanup_sync_stops_watch_service_on_stopped_loop",
    )
    environment = os.environ.copy()
    isolated_home = tmp_path / "core-only-home"
    environment.update(
        {
            "AVIBE_HOME": str(isolated_home / ".avibe"),
            "CLAUDE_CONFIG_DIR": str(isolated_home / ".claude"),
            "CODEX_HOME": str(isolated_home / ".codex"),
            "HOME": str(isolated_home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
            "XDG_CACHE_HOME": str(isolated_home / ".cache"),
            "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            "XDG_DATA_HOME": str(isolated_home / ".local" / "share"),
            "XDG_STATE_HOME": str(isolated_home / ".local" / "state"),
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.memory_import_blocker",
            *scenarios,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "7 passed" in completed.stdout
    assert (
        "MEMORY-INDEP-017 import tracking: attempted=[] loaded=[]"
        in completed.stdout
    )


def test_wave_3a_package_metadata_separates_avibe_memory_distribution() -> None:
    """Wave 3a keeps the implementation out of the core distribution."""

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = metadata["tool"]["hatch"]["build"]["targets"]
    memory_metadata = tomllib.loads(
        (ROOT / "packaging/avibe-memory/pyproject.toml").read_text(encoding="utf-8")
    )

    assert "avibe_memory" not in targets["wheel"]["packages"]
    assert "avibe_memory/**" not in targets["sdist"]["include"]
    assert "vibe/memory_runtime_manifest.json" in targets["wheel"]["exclude"]
    assert memory_metadata["project"]["name"] == "avibe-memory"


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

    process_module = types.ModuleType("avibe_memory.process")
    process_module.ReleasedEverOSOrphanReconciler = _Reconciler
    monkeypatch.setitem(sys.modules, "avibe_memory.process", process_module)

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
            self.capture_adapter = self._CaptureAdapter()

        class _CaptureAdapter:
            def __init__(self) -> None:
                self.task: asyncio.Task[None] | None = None

            def start(self) -> bool:
                return True

            def offer(self, _event: object) -> None:
                async def capture() -> None:
                    capture_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        capture_stopped.set()

                self.task = asyncio.create_task(capture())

            def quiesce_memory_capture_tasks(self) -> None:
                return None

            async def cancel_memory_capture_tasks(self) -> None:
                if self.task is None:
                    return
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

            def cancel_memory_capture_tasks_nowait(self) -> None:
                if self.task is not None:
                    self.task.cancel()

        def start_capture_adapter(self, **_options: object) -> bool:
            return self.capture_adapter.start()

        async def reconcile(self, config) -> dict[str, object]:
            return {
                "ok": True,
                "state": "running" if config.enabled else "disabled",
            }

        async def close(self) -> None:
            await self.capture_adapter.cancel_memory_capture_tasks()
            self.closed = True

        def begin_close(self) -> None:
            self.capture_adapter.quiesce_memory_capture_tasks()
            self.capture_adapter.cancel_memory_capture_tasks_nowait()

    runtime = _Runtime()

    def create_memory_runtime(config, **_kwargs):
        created.append(config)
        return runtime

    runtime_module = types.ModuleType("avibe_memory.runtime")
    runtime_module.MEMORY_RUNTIME_PROTOCOL_VERSION = 1
    runtime_module.create_memory_runtime = create_memory_runtime
    monkeypatch.setitem(sys.modules, "avibe_memory.runtime", runtime_module)

    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_disabled_cleanup_unproved = True
    controller.settings_manager = types.SimpleNamespace(
        is_enabled_user=lambda *_args, **_kwargs: True
    )
    controller.session_turns = types.SimpleNamespace(
        session_lifecycle_snapshot_matches=lambda *_args: True,
        acquire_lifecycle_admission=lambda *_args: asyncio.sleep(
            0,
            result=types.SimpleNamespace(release=lambda: None),
        ),
    )

    enabled = replace(controller.config.memory, enabled=True)
    assert await controller.reconcile_memory(enabled) == {
        "ok": True,
        "state": "running",
    }
    assert created == [enabled]
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert controller.memory_adapter is runtime.capture_adapter
    assert controller.config.memory.enabled is True

    controller.memory_adapter.offer(
        TurnAccepted(
            platform="slack",
            user_id="user-1",
            message_id="native-1",
            session_id="session-1",
            text="remember this",
            files=(),
            is_dm=True,
            is_ordinary_text=True,
            is_ordinary_attachment=False,
            lifecycle_snapshot=0,
        )
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
async def test_disabled_preflight_uses_one_unpublished_runtime() -> None:
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

        def begin_close(self) -> None:
            return None

    runtime = _Runtime()
    controller._create_memory_runtime = lambda config, **_kwargs: runtime

    assert await controller.preflight_memory(candidate) == {"ok": True}
    assert calls == [candidate]
    assert runtime.closed is True
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.parametrize("overlap", ("preflight", "install"))
@pytest.mark.asyncio
async def test_overlapping_unpublished_operations_return_busy(
    overlap: str,
) -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    controller._memory_plugin_error = None
    candidate = replace(controller.config.memory, enabled=True)
    preflight_started = asyncio.Event()
    preflight_release = asyncio.Event()
    construction_attempts = 0

    class _Runtime:
        module = object()

        def __init__(self) -> None:
            self.closed = False

        async def preflight(self, config) -> dict[str, object]:
            assert config is candidate
            preflight_started.set()
            await preflight_release.wait()
            return {"ok": True}

        def begin_close(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    runtime = _Runtime()

    def create_runtime(_config, **_kwargs):
        nonlocal construction_attempts
        construction_attempts += 1
        if construction_attempts == 1:
            return runtime
        raise MemoryRuntimeBusyError("provider root busy")

    controller._create_memory_runtime = create_runtime
    first = asyncio.create_task(controller.preflight_memory(candidate))
    await preflight_started.wait()

    if overlap == "preflight":
        busy = await controller.preflight_memory(candidate)
        assert busy == {"ok": False, "error": "memory_operation_in_progress"}
    else:
        busy = await controller.install_memory_runtime()
        assert busy == {
            "ok": False,
            "reason": "memory_operation_in_progress",
            "download_error": None,
        }

    assert construction_attempts == 2
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert controller._memory_plugin_error is None

    preflight_release.set()
    assert await first == {"ok": True}
    assert runtime.closed is True


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

        def begin_close(self) -> None:
            return None

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
async def test_ordinary_memory_read_keeps_cached_plugin_failure_without_retry() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=replace(_disabled_app_config().memory, enabled=True)
    )
    controller.memory_runtime = None
    controller._memory_reconcile_task = None
    failure = MemoryPluginUnavailableError("startup failed")
    controller._memory_plugin_error = failure
    controller._create_memory_runtime = lambda *_args, **_kwargs: pytest.fail(
        "ordinary reads must not retry a cached plugin failure"
    )

    with pytest.raises(MemoryPluginUnavailableError) as raised:
        await controller.memory_projects_payload(
            verified_user_key=None,
            cli_scope=("u-11111111111111111111111111111111", "default"),
        )

    assert raised.value is failure


@pytest.mark.asyncio
async def test_explicit_recovery_caches_typed_retry_failure() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=_disabled_app_config().memory)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_plugin_error = MemoryPluginUnavailableError("startup failed")
    candidate = replace(controller.config.memory, enabled=True)
    retry_failure = MemoryPluginIncompatibleError("still incompatible")

    def create_runtime(_config, **_kwargs):
        raise retry_failure

    controller._create_memory_runtime = create_runtime

    with pytest.raises(MemoryPluginIncompatibleError) as raised:
        await controller.preflight_memory(candidate)

    assert raised.value is retry_failure
    assert controller._memory_plugin_error is retry_failure


@pytest.mark.asyncio
async def test_disabled_install_keeps_runtime_unpublished_until_close() -> None:
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
            assert controller.memory_runtime is None
            events.append("install")
            return {"ok": True}

        def begin_close(self) -> None:
            self.retired = True
            events.append("begin-close")

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
    assert events == ["install", "begin-close", "close"]
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

        def begin_close(self) -> None:
            return None

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
async def test_disable_publishes_disabled_before_failed_close() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    original_adapter = object()
    controller.memory_adapter = original_adapter
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.retired = False

        def begin_close(self) -> None:
            assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
            self.retired = True

        async def close(self) -> None:
            raise RuntimeError("close failed")

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module

    with pytest.raises(RuntimeError, match="close failed"):
        await controller.reconcile_memory(disabled)

    assert controller.config.memory == disabled
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert runtime.retired is True


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
async def test_enabled_status_reprojects_when_disabled_before_snapshot() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    controller.memory_adapter = None
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    snapshot_started = asyncio.Event()
    snapshot_release = asyncio.Event()

    async def await_cleanup() -> None:
        snapshot_started.set()
        await snapshot_release.wait()

    controller._await_disabled_memory_cleanup = await_cleanup
    controller._create_memory_runtime = lambda _config, **_kwargs: pytest.fail(
        "status must re-project disabled state before constructing Memory"
    )
    status_task = asyncio.create_task(controller.memory_status_payload())
    await snapshot_started.wait()

    async with controller._memory_replacement_lock():
        controller.config.memory = disabled
    snapshot_release.set()

    status = await status_task

    assert status["status"] == "ok"
    assert status["state"] == "disabled"
    assert status["reason"] is None
    assert status["source"]["reason"] == "memory_disabled"


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
async def test_disable_does_not_wait_for_long_runtime_read() -> None:
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
            self.retired = False
            self.close_calls = 0

        async def status_payload(self) -> dict[str, object]:
            assert controller._memory_replacement_lock().locked() is False
            read_started.set()
            await read_release.wait()
            return {"status": "ok", "state": "running"}

        def begin_close(self) -> None:
            self.retired = True

        async def close(self) -> None:
            self.close_calls += 1

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    status = asyncio.create_task(controller.memory_status_payload())
    await read_started.wait()
    assert await asyncio.wait_for(
        controller.reconcile_memory(disabled),
        timeout=0.1,
    ) == {"ok": True, "state": "disabled"}

    assert runtime.close_calls == 1
    assert controller.memory_runtime is None
    assert controller._memory_replacement_lock().locked() is False
    read_release.set()
    assert await status == {"status": "ok", "state": "running"}


@pytest.mark.asyncio
async def test_reconcile_provider_work_does_not_hold_pointer_lock() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    candidate = replace(enabled)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=enabled)
    controller.memory_adapter = None
    controller._memory_disabled_cleanup_task = None
    reconcile_started = asyncio.Event()
    reconcile_release = asyncio.Event()

    class _Runtime:
        module = object()
        retired = False
        capture_adapter = object()

        async def reconcile(self, config) -> dict[str, object]:
            assert config is candidate
            assert controller._memory_replacement_lock().locked() is False
            reconcile_started.set()
            await reconcile_release.wait()
            return {"ok": True, "state": "running"}

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    reconcile = asyncio.create_task(controller.reconcile_memory(candidate))
    await reconcile_started.wait()

    async with asyncio.timeout(0.1):
        async with controller._memory_replacement_lock():
            assert controller.memory_runtime is runtime

    reconcile_release.set()
    assert await reconcile == {"ok": True, "state": "running"}
    assert controller.config.memory is candidate


@pytest.mark.asyncio
async def test_disable_revokes_unpublished_enable_before_it_can_publish() -> None:
    disabled = _disabled_app_config().memory
    enabled = replace(disabled, enabled=True)
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(memory=disabled)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_disabled_cleanup_task = None
    reconcile_started = asyncio.Event()
    reconcile_release = asyncio.Event()

    async def no_cleanup() -> None:
        return None

    controller._await_disabled_memory_cleanup = no_cleanup
    controller._recheck_disabled_memory_cleanup = no_cleanup

    class _Runtime:
        module = object()
        capture_adapter = object()
        retired = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def reconcile(self, config) -> dict[str, object]:
            assert config is enabled
            assert controller.memory_runtime is self
            assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
            assert controller._memory_replacement_lock().locked() is False
            reconcile_started.set()
            await reconcile_release.wait()
            return {"ok": True, "state": "running"}

        def begin_close(self) -> None:
            assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
            self.retired = True

        async def close(self) -> None:
            self.close_calls += 1

    runtime = _Runtime()
    controller._create_memory_runtime = lambda _config: runtime
    reconcile = asyncio.create_task(controller.reconcile_memory(enabled))
    await reconcile_started.wait()

    assert await controller.reconcile_memory(disabled) == {
        "ok": True,
        "state": "disabled",
    }
    assert controller.memory_runtime is None
    assert controller.config.memory is disabled
    assert runtime.retired is True
    assert runtime.close_calls == 1

    reconcile_release.set()
    assert await reconcile == {
        "ok": False,
        "state": "disabled",
        "error": "memory_operation_in_progress",
    }
    assert controller.memory_runtime is None
    assert controller.config.memory is disabled
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_memory_indep_015_shutdown_bounds_all_memory_stages_under_one_budget() -> None:
    controller = Controller.__new__(Controller)
    controller._memory_reconcile_task = None
    controller._memory_shutdown_budget_seconds = 0.06
    controller._shutdown_tainted = False
    controller._memory_destructive_quiescing = False

    started: list[str] = []

    class _MemoryAdapter:
        def quiesce_memory_capture_tasks(self) -> None:
            started.append("capture-registration")

    class _MemoryRuntime:
        def begin_close(self) -> None:
            assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
            started.append("runtime-revoke")

        async def close(self) -> None:
            started.append("runtime")

    controller.memory_adapter = _MemoryAdapter()
    controller.memory_runtime = _MemoryRuntime()

    async def join_destructive_transactions() -> None:
        started.append("destructive")

    controller._join_memory_destructive_transactions = join_destructive_transactions

    started_at = time.monotonic()
    await controller._shutdown_memory_stack()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert started == [
        "capture-registration",
        "destructive",
        "runtime-revoke",
        "runtime",
    ]
    assert controller._shutdown_tainted is False


@pytest.mark.asyncio
async def test_shutdown_attempts_runtime_close_after_destructive_timeout() -> None:
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

        def begin_close(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    runtime = _MemoryRuntime()
    controller.memory_runtime = runtime

    started_at = time.monotonic()
    await controller._shutdown_memory_stack()

    assert time.monotonic() - started_at < 0.2
    assert destructive.done() is False
    assert runtime.closed is True
    assert controller.memory_runtime is None
    assert controller._shutdown_tainted is True

    release.set()
    await destructive
    await asyncio.sleep(0)
