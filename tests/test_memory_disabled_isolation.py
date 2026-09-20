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
from core import memory_legacy_cleanup
from core.controller import Controller
from avibe_memory import CaptureAccepted, CaptureRequest, CaptureSkipped
from core.memory_adapter import DisabledMemoryAdapter
from vibe.memory_contract import (
    MemoryImplementationIncompatibleError,
    MemoryImplementationUnavailableError,
    MemoryStoreUnavailableError,
)


ROOT = Path(__file__).resolve().parents[1]


class _NoRetainedRootOwnership:
    def release_retained_root_ownership(self) -> None:
        return None


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


def _controller_with_memory(memory=None) -> Controller:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=memory or _disabled_app_config().memory
    )
    controller.memory_adapter = DisabledMemoryAdapter()
    controller.memory_runtime = None
    controller.memory_module = None
    controller._memory_reconcile_task = None
    controller._memory_disabled_cleanup_task = None
    controller._memory_disabled_cleanup_unproved = False
    controller._memory_implementation_error = None
    return controller


def _capture_request() -> CaptureRequest:
    return CaptureRequest(
        source_message_id="message-1",
        session_id="session-1",
        principal_id="u-11111111111111111111111111111111",
        project_id="default",
        provenance="agent",
        text="remember this",
        occurred_at_ms=1,
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
        # Every config writer uses this legacy-named lock, not just Memory.
        and not (
            path.relative_to(home) == Path("config/memory-config.tx.lock")
            and path.is_file()
            and not path.is_symlink()
        )
    ]


def test_memory_state_inventory_excludes_only_the_shared_config_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.default()
    config.memory = replace(config.memory, enabled=False)
    config.save()

    assert (tmp_path / "config" / "memory-config.tx.lock").is_file()
    assert _memory_state_entries(tmp_path) == []


@pytest.mark.parametrize(
    "relative_path,kind",
    [
        ("memory", "directory"),
        ("everos", "directory"),
        ("config/memory-status.json", "file"),
        ("config/memory-config.tx.lock.extra", "file"),
        ("other/memory-config.tx.lock", "file"),
        ("config/memory-config.tx.lock", "directory"),
        ("config/memory-config.tx.lock", "symlink"),
    ],
)
def test_memory_state_inventory_keeps_runtime_assets_and_lock_lookalikes(
    tmp_path: Path, relative_path: str, kind: str,
) -> None:
    candidate = tmp_path / relative_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        candidate.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.touch()
        candidate.symlink_to(target)
    else:
        candidate.touch()

    assert Path(relative_path) in _memory_state_entries(tmp_path)


def test_memory_cli_session_keeps_authenticated_boundary_when_implementation_is_unavailable() -> None:
    controller = Controller.__new__(Controller)
    controller.config = types.SimpleNamespace(
        memory=types.SimpleNamespace(enabled=True),
    )
    controller._memory_implementation_error = MemoryImplementationUnavailableError("injected")
    controller._memory_scopes_by_session = {}
    controller._memory_cli_facts_by_session = {}
    controller._memory_implementation_cli_sessions = set()
    controller._memory_admission = lambda: pytest.fail(
        "implementation failure must be projected before admission imports"
    )
    controller._memory_turn_facts = lambda _context: pytest.fail(
        "implementation failure must be projected before facts imports"
    )
    context = types.SimpleNamespace(
        platform_specific={
            "agent_session_target": {"id": "session-implementation"},
        },
        platform="avibe",
    )

    assert Controller.configure_memory_cli_session(
        controller,
        context,
        admitted=True,
    )
    assert Controller.memory_scope_for_cli_session(controller, "session-implementation") == (
        "__memory_implementation_error__",
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

    monkeypatch.setattr(
        memory_legacy_cleanup,
        "ReleasedEverOSOrphanReconciler",
        _Reconciler,
    )

    controller = _controller_with_memory()
    await controller._schedule_disabled_memory_cleanup()
    cleanup_task = controller._memory_disabled_cleanup_task
    assert cleanup_task is not None
    return controller, cleanup_task, record_path


@pytest.mark.parametrize("model_hub_env", [None, "0"])
def test_memory_indep_013_disabled_fresh_startup_has_no_runtime_side_effects(
    tmp_path: Path, model_hub_env: str | None,
) -> None:
    script = r'''
import asyncio
import sys
from pathlib import Path

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config, is_model_hub_enabled
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

assert (controller.model_hub_service is not None) == is_model_hub_enabled()
assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
assert controller.memory_runtime is None
assert controller.memory_module is None
assert "avibe_memory.runtime" not in sys.modules
assert "avibe_memory.process" not in sys.modules
assert not [
    path for path in home.rglob("*")
    if any("memory" in part.casefold() or "everos" in part.casefold()
           for part in path.relative_to(home).parts)
    and not (
        path.relative_to(home) == Path("config/memory-config.tx.lock")
        and path.is_file()
        and not path.is_symlink()
    )
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
    environment.pop("VIBE_MODEL_HUB_ENABLED", None)
    if model_hub_env is not None:
        environment["VIBE_MODEL_HUB_ENABLED"] = model_hub_env

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


@pytest.mark.parametrize("model_hub_env", [None, "0"])
def test_memory_indep_014_disabled_controller_starts_with_runtime_imports_blocked(
    tmp_path: Path, model_hub_env: str | None,
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
from config.v2_config import V2Config, is_model_hub_enabled
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
assert (controller.model_hub_service is not None) == is_model_hub_enabled()
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
    environment.pop("VIBE_MODEL_HUB_ENABLED", None)
    if model_hub_env is not None:
        environment["VIBE_MODEL_HUB_ENABLED"] = model_hub_env

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


def test_disabled_cleanup_consumes_released_record_without_optional_package(
    tmp_path: Path,
) -> None:
    script = r'''
import asyncio
import importlib.abc
import json
import sys


class BlockMemoryImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        del path, target
        if fullname == "avibe_memory" or fullname.startswith("avibe_memory."):
            raise ImportError(f"blocked optional Memory implementation: {fullname}")
        return None


sys.meta_path.insert(0, BlockMemoryImplementation())

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.controller import Controller

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
memory_dir = home / "memory"
record_path = memory_dir / ".rt" / "everos.sidecar.json"
record_path.parent.mkdir(mode=0o700, parents=True)
record_path.write_text(json.dumps({
    "pid": 99999999,
    "create_time": 1.0,
    "provider_root": str(memory_dir / "everos-root"),
    "socket_path": str(memory_dir / ".rt" / "everos.sock"),
    "role": "sidecar",
    "python": "/runtime/bin/python",
}), encoding="utf-8")
record_path.chmod(0o600)


async def cleanup() -> None:
    await controller._schedule_disabled_memory_cleanup()
    task = controller._memory_disabled_cleanup_task
    assert task is not None
    await task


asyncio.run(cleanup())
assert not record_path.exists()
assert not any(
    name == "avibe_memory" or name.startswith("avibe_memory.")
    for name in sys.modules
)
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
from vibe.memory_contract import MemoryImplementationUnavailableError


class BrokenRuntime(types.ModuleType):
    def __getattr__(self, name):
        if name == "create_memory_runtime":
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
assert isinstance(controller._memory_implementation_error, MemoryImplementationUnavailableError)
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

    monkeypatch.setattr(
        memory_legacy_cleanup,
        "ReleasedEverOSOrphanReconciler",
        _Reconciler,
    )

    controller = _controller_with_memory()

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
async def test_enabled_operation_bounds_disabled_cleanup_wait_and_remains_retryable(
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
    controller.config.memory = replace(controller.config.memory, enabled=True)
    controller._memory_disabled_cleanup_wait_seconds = 0.01

    class _Runtime:
        async def wake(self) -> dict[str, object]:
            return {"ok": True, "state": "running"}

    controller.memory_runtime = _Runtime()

    try:
        with pytest.raises(
            MemoryStoreUnavailableError,
            match="Disabled Memory cleanup is still in progress",
        ):
            await asyncio.wait_for(controller.wake_memory(), timeout=0.1)

        class _Python310AsyncioTimeoutError(Exception):
            pass

        async def raise_python310_timeout(
            _future: object,
            *,
            timeout: float,
        ) -> None:
            assert timeout == 0.01
            raise _Python310AsyncioTimeoutError

        with monkeypatch.context() as python310:
            python310.setattr(
                asyncio,
                "TimeoutError",
                _Python310AsyncioTimeoutError,
            )
            python310.setattr(asyncio, "wait_for", raise_python310_timeout)
            with pytest.raises(
                MemoryStoreUnavailableError,
                match="Disabled Memory cleanup is still in progress",
            ):
                await controller.wake_memory()
        assert cleanup_task.done() is False
        assert cleanup_task.cancelled() is False

        cleanup_release.set()
        await cleanup_task
        assert await controller.wake_memory() == {"ok": True, "state": "running"}
    finally:
        cleanup_release.set()
        await cleanup_task


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [("failure", "degraded"), ("retained", "degraded"), ("released", "disabled")],
)
@pytest.mark.asyncio
async def test_disabled_cleanup_terminal_status_tracks_ownership(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_state: str,
) -> None:
    async def reconcile_orphans() -> None:
        if outcome == "failure":
            raise RuntimeError("cleanup failed")
        if outcome == "released":
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


@pytest.mark.parametrize("active_operation", ("preflight", "install"))
@pytest.mark.asyncio
async def test_memory_reconcile_lazily_enters_and_leaves_enabled_runtime(
    active_operation: str,
) -> None:
    controller = _controller_with_memory()
    enabled = replace(controller.config.memory, enabled=True)
    offers: list[object] = []
    operation_started = asyncio.Event()
    operation_release = asyncio.Event()

    class _CaptureAdapter:
        def offer(self, event: object) -> None:
            offers.append(event)

    class _Runtime(_NoRetainedRootOwnership):
        def __init__(self) -> None:
            self.module = object()
            self.available = True
            self.closed = False
            self.capture_adapter = _CaptureAdapter()
            self.begin_close_calls = 0

        def start_capture_adapter(self, **_options: object) -> bool:
            return True

        async def reconcile(self, _config) -> dict[str, object]:
            return {"ok": True, "state": "running"}

        async def preflight(self, _config) -> dict[str, object]:
            operation_started.set()
            await operation_release.wait()
            return {"ok": True}

        async def install_artifact(self, **_options: object) -> dict[str, object]:
            return await self.preflight(enabled)

        async def close(self) -> None:
            self.closed = True

        def begin_close(self) -> None:
            assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
            self.begin_close_calls += 1

    runtime = _Runtime()
    controller._create_memory_runtime = lambda _config: runtime
    assert await controller.reconcile_memory(enabled) == {
        "ok": True,
        "state": "running",
    }
    assert controller.memory_runtime is runtime
    assert controller.memory_module is runtime.module
    assert controller.memory_adapter is runtime.capture_adapter
    assert controller.config.memory.enabled is True

    event = object()
    controller.memory_adapter.offer(event)
    assert offers == [event]

    operation = asyncio.create_task(
        controller.preflight_memory(enabled)
        if active_operation == "preflight"
        else controller.install_memory_runtime()
    )
    await operation_started.wait()

    disabled = replace(enabled, enabled=False)
    assert await controller.reconcile_memory(disabled) == {
        "ok": False,
        "state": "disabled",
        "error": "memory_operation_in_progress",
    }
    assert controller.config.memory is enabled
    assert controller.memory_runtime is runtime
    assert controller.memory_adapter is runtime.capture_adapter
    assert runtime.begin_close_calls == 0

    operation_release.set()
    assert await operation == {"ok": True}
    assert await controller.reconcile_memory(disabled) == {
        "ok": True,
        "state": "disabled",
    }
    assert runtime.closed is True
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert controller.config.memory.enabled is False


@pytest.mark.parametrize("overlap", ("preflight", "install"))
@pytest.mark.asyncio
async def test_overlapping_unpublished_operations_return_busy(
    overlap: str,
) -> None:
    controller = _controller_with_memory()
    candidate = replace(controller.config.memory, enabled=True)
    preflight_started = asyncio.Event()
    preflight_release = asyncio.Event()
    construction_attempts: list[object] = []

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

    def create_runtime(config, **_kwargs):
        construction_attempts.append(config)
        return runtime

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

    assert construction_attempts == [candidate]
    assert controller.memory_runtime is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)

    preflight_release.set()
    assert await first == {"ok": True}
    assert runtime.closed is True


@pytest.mark.parametrize("recovery", ("preflight", "install"))
@pytest.mark.asyncio
async def test_cached_implementation_failure_only_retries_for_explicit_recovery(
    recovery: str,
) -> None:
    controller = _controller_with_memory(
        replace(_disabled_app_config().memory, enabled=True)
    )
    failure = MemoryImplementationUnavailableError("startup failed")
    controller._memory_implementation_error = failure
    controller._create_memory_runtime = lambda *_args, **_kwargs: pytest.fail(
        "ordinary reads must not retry a cached implementation failure"
    )

    with pytest.raises(MemoryImplementationUnavailableError) as raised:
        await controller.memory_projects_payload(
            verified_user_key=None,
            cli_scope=("u-11111111111111111111111111111111", "default"),
        )

    assert raised.value is failure
    events: list[str] = []

    class _Runtime(_NoRetainedRootOwnership):
        def __init__(self, label: str, *, fail_close: bool = False) -> None:
            self.label = label
            self.module = object()
            self.closing = False
            self.fail_close = fail_close

        async def install_artifact(self, **_options: object) -> dict[str, object]:
            assert controller.memory_runtime is None
            events.append(f"{self.label}:install")
            return {"ok": True}

        async def preflight(self, _config: object) -> dict[str, object]:
            events.append(f"{self.label}:preflight")
            return {"ok": True}

        def begin_close(self) -> None:
            self.closing = True
            events.append(f"{self.label}:begin-close")

        async def close(self) -> None:
            events.append(f"{self.label}:close")
            if self.fail_close:
                self.fail_close = False
                raise RuntimeError("close failed")

    retained = _Runtime("retained", fail_close=True)
    fresh = _Runtime("fresh")
    runtimes = iter((retained, fresh))
    controller._create_memory_runtime = lambda *_args, **_kwargs: next(runtimes)

    with pytest.raises(RuntimeError, match="close failed"):
        await controller.install_memory_runtime()
    assert controller.memory_runtime is retained
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)

    if recovery == "preflight":
        result = await controller.preflight_memory(controller.config.memory)
    else:
        result = await controller.install_memory_runtime()
    assert result == {"ok": True}
    assert events == [
        "retained:install",
        "retained:begin-close",
        "retained:close",
        "retained:close",
        f"fresh:{recovery}",
        "fresh:begin-close",
        "fresh:close",
    ]
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert controller._memory_implementation_error is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)

    retry_failure = MemoryImplementationIncompatibleError("still incompatible")

    def fail_retry(_config, **_kwargs):
        raise retry_failure

    controller._create_memory_runtime = fail_retry
    with pytest.raises(MemoryImplementationIncompatibleError) as raised:
        await controller.preflight_memory(controller.config.memory)
    assert raised.value is retry_failure
    assert controller._memory_implementation_error is retry_failure


@pytest.mark.parametrize("ordering", ("reaper-first", "enable-first"))
@pytest.mark.asyncio
async def test_disabled_reaper_enable_and_shutdown_share_root_mutation_gate(
    ordering: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller_with_memory()
    controller._memory_disabled_cleanup_unproved = True
    enabled = replace(controller.config.memory, enabled=True)
    lease_held = False
    reaper_started = asyncio.Event()
    reaper_release = asyncio.Event()
    attach_started = asyncio.Event()
    attach_release = asyncio.Event()
    reaper_calls = 0
    construction_calls = 0

    class _Lease:
        def release(self) -> None:
            nonlocal lease_held
            lease_held = False

    async def try_lease(_effective_home=None):
        nonlocal lease_held
        if lease_held:
            return None
        lease_held = True
        return _Lease()

    class _Reconciler:
        def __init__(self, **_kwargs) -> None:
            pass

        async def reconcile_orphans(self) -> None:
            nonlocal reaper_calls
            reaper_calls += 1
            reaper_started.set()
            await reaper_release.wait()

    monkeypatch.setattr(
        memory_legacy_cleanup,
        "ReleasedEverOSOrphanReconciler",
        _Reconciler,
    )
    controller._try_memory_operation_lease = try_lease
    controller._disabled_memory_ownership_exists = lambda _path: True

    class _Runtime:
        module = object()
        capture_adapter = object()
        closing = False

        async def reconcile(self, _config) -> dict[str, object]:
            return {"ok": True, "state": "running"}

        def start_capture_adapter(self, **_kwargs) -> bool:
            return True

        def begin_close(self) -> None:
            self.closing = True

        async def close(self, **_options: object) -> None:
            return None

    runtime = _Runtime()

    def create_runtime(_config, **_kwargs):
        nonlocal construction_calls
        construction_calls += 1
        return runtime

    controller._create_memory_runtime = create_runtime
    memory_dir = Path("unused-memory-root")

    if ordering == "reaper-first":
        cleanup = asyncio.create_task(
            controller._cleanup_disabled_memory_process(memory_dir)
        )
        await reaper_started.wait()
        assert await controller.reconcile_memory(enabled) == {
            "ok": False,
            "error": "memory_operation_in_progress",
        }
        assert construction_calls == 0
        reaper_release.set()
        await cleanup
    else:
        attach = controller._attach_memory_runtime

        async def blocked_attach(*args, **kwargs) -> None:
            attach_started.set()
            await attach_release.wait()
            await attach(*args, **kwargs)

        controller._attach_memory_runtime = blocked_attach
        reconcile = asyncio.create_task(controller.reconcile_memory(enabled))
        await attach_started.wait()
        await controller._cleanup_disabled_memory_process(memory_dir)
        assert reaper_calls == 0
        shutdown = asyncio.create_task(
            controller._close_memory_runtime_for_shutdown(timeout_seconds=0.5)
        )
        await asyncio.sleep(0.02)
        assert shutdown.done() is False
        assert controller.memory_runtime is None
        attach_release.set()
        assert await reconcile == {"ok": True, "state": "running"}
        await shutdown
        assert controller.memory_runtime is None

    assert controller._memory_replacement_lock().locked() is False


@pytest.mark.asyncio
async def test_disable_publishes_disabled_before_failed_close() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    disabled = replace(enabled, enabled=False)
    controller = _controller_with_memory(enabled)
    original_adapter = object()
    controller.memory_adapter = original_adapter

    class _Runtime:
        def __init__(self) -> None:
            self.module = object()
            self.fail_close = True
            self.close_calls = 0
            self.root_released = False

        def begin_close(self) -> None:
            assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)

        async def close(self, **_options: object) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise RuntimeError("close failed")

        def release_retained_root_ownership(self) -> None:
            self.root_released = True

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module

    assert await controller.reconcile_memory(disabled) == {
        "ok": False,
        "state": "disabled",
        "error": "memory_operation_in_progress",
    }

    assert controller.config.memory == disabled
    assert controller.memory_runtime is runtime
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)

    runtime.fail_close = False
    assert await controller.reconcile_memory(disabled) == {
        "ok": True,
        "state": "disabled",
    }
    assert runtime.close_calls == 2
    assert runtime.root_released is True
    assert controller.memory_runtime is None


@pytest.mark.parametrize(
    ("cleanup_unproved", "expected_state", "expected_reason", "source_reason"),
    [
        (False, "needs_repair", "memory_legacy_recovery_required", "memory_disabled"),
        (True, "degraded", "memory_runtime_busy", "memory_runtime_busy"),
    ],
)
@pytest.mark.asyncio
async def test_disabled_status_projects_repair_and_cleanup_fences(
    cleanup_unproved: bool,
    expected_state: str,
    expected_reason: str,
    source_reason: str,
) -> None:
    controller = _controller_with_memory(
        replace(
            _disabled_app_config().memory,
            legacy_needs_repair=True,
        )
    )
    controller._memory_disabled_cleanup_unproved = cleanup_unproved

    status = await controller.memory_status_payload()

    assert status["state"] == expected_state
    assert status["reason"] == expected_reason
    assert status["source"]["reason"] == source_reason


@pytest.mark.asyncio
async def test_disabled_operations_do_not_construct_runtime() -> None:
    controller = _controller_with_memory()
    factory_calls: list[object] = []

    def create_runtime(config, **_kwargs):
        factory_calls.append(config)
        raise AssertionError("disabled operations must not construct Memory")

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
    assert await controller.wake_memory() == {
        "ok": False,
        "state": "disabled",
        "error": "memory_disabled",
    }
    assert await controller.capture_memory(
        _capture_request()
    ) == CaptureSkipped(reason="memory_disabled")
    with pytest.raises(MemoryStoreUnavailableError, match="Memory is disabled"):
        await controller.memory_projects_payload(
            verified_user_key=None,
            cli_scope=("u-11111111111111111111111111111111", "default"),
        )
    assert factory_calls == []


@pytest.mark.asyncio
async def test_implementation_failure_rechecked_after_blocked_capture_lock() -> None:
    controller = _controller_with_memory(
        replace(_disabled_app_config().memory, enabled=True)
    )
    request = _capture_request()

    gate = controller._memory_replacement_lock()
    await gate.acquire()
    capture = asyncio.create_task(controller.capture_memory(request))
    await asyncio.sleep(0)
    assert not capture.done()
    controller._memory_implementation_error = MemoryImplementationUnavailableError("injected")
    gate.release()

    with pytest.raises(MemoryImplementationUnavailableError):
        await capture


@pytest.mark.asyncio
async def test_explicit_agent_capture_snapshots_semantic_name_before_waiting() -> None:
    controller = _controller_with_memory(replace(_disabled_app_config().memory, enabled=True))
    captures = []

    async def capture(request):
        captures.append(request)
        return CaptureAccepted()

    controller.memory_runtime = types.SimpleNamespace(
        available=True, module=types.SimpleNamespace(capture=capture)
    )
    request = replace(_capture_request(), sender_name="Not the human")
    gate = controller._memory_replacement_lock()
    await gate.acquire()
    pending = asyncio.create_task(controller.capture_memory(request))
    await asyncio.sleep(0)
    gate.release()
    await pending
    assert captures == [replace(request, sender_name="Agent")]


@pytest.mark.asyncio
async def test_enabled_status_does_not_wait_for_startup_wake() -> None:
    controller = _controller_with_memory(
        replace(_disabled_app_config().memory, enabled=True)
    )
    controller.memory_adapter = None
    wake_release = asyncio.Event()

    async def startup_wake() -> None:
        await wake_release.wait()

    controller._memory_reconcile_task = asyncio.create_task(startup_wake())

    class _Runtime:
        module = object()
        closed = False

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
async def test_reconcile_provider_work_does_not_hold_pointer_lock() -> None:
    enabled = replace(_disabled_app_config().memory, enabled=True)
    candidate = replace(enabled)
    controller = _controller_with_memory(enabled)
    controller.memory_adapter = None
    reconcile_started = asyncio.Event()
    reconcile_release = asyncio.Event()

    class _Runtime:
        module = object()
        capture_adapter = object()
        capture_starts = 0

        def start_capture_adapter(self, **_options: object) -> bool:
            self.capture_starts += 1
            return True

        async def reconcile(self, config) -> dict[str, object]:
            assert config is candidate
            assert controller._memory_replacement_lock().locked() is False
            reconcile_started.set()
            await reconcile_release.wait()
            return {"ok": True, "state": "running"}

        async def preflight(self, _config) -> dict[str, object]:
            pytest.fail("preflight overlapped enabled reconciliation")

    runtime = _Runtime()
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    reconcile = asyncio.create_task(controller.reconcile_memory(candidate))
    await reconcile_started.wait()

    async with asyncio.timeout(0.1):
        async with controller._memory_replacement_lock():
            assert controller.memory_runtime is runtime
    assert await controller.preflight_memory(candidate) == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }

    reconcile_release.set()
    assert await reconcile == {"ok": True, "state": "running"}
    assert controller.config.memory is candidate
    assert runtime.capture_starts == 1


@pytest.mark.asyncio
async def test_memory_indep_015_shutdown_uses_one_budget_after_stage_timeout() -> None:
    controller = Controller.__new__(Controller)
    controller._memory_reconcile_task = None
    controller._memory_shutdown_budget_seconds = 0.04
    controller._shutdown_tainted = False
    controller._memory_destructive_quiescing = False
    controller.message_handler = types.SimpleNamespace(
        quiesce_memory_capture_tasks=lambda: None,
    )
    release = asyncio.Event()
    close_deadlines: list[float] = []

    async def transaction() -> dict[str, object]:
        await release.wait()
        assert controller.memory_runtime is runtime
        assert runtime.closed is False
        return {"ok": True}

    destructive = asyncio.create_task(transaction())
    controller._memory_destructive_tasks = {destructive}

    class _MemoryRuntime:
        def __init__(self) -> None:
            self.closed = False

        def begin_close(self) -> None:
            return None

        async def close(self, **options: object) -> None:
            self.closed = True
            close_deadlines.append(float(options["timeout_seconds"]))

    runtime = _MemoryRuntime()
    controller.memory_runtime = runtime

    started_at = time.monotonic()
    await controller._shutdown_memory_stack()

    assert time.monotonic() - started_at < 0.2
    assert destructive.done() is False
    assert runtime.closed is False
    assert controller.memory_runtime is runtime
    assert controller._shutdown_tainted is True
    assert close_deadlines == []

    release.set()
    await destructive
    await controller._shutdown_memory_stack()
    assert runtime.closed is True
    assert controller.memory_runtime is None
    assert 0.0 <= close_deadlines[0] <= controller._memory_shutdown_budget_seconds
