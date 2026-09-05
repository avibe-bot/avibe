import asyncio
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import message_deliveries as delivery_store
from vibe.opencode_config import OPENCODE_REASONING_VARIANTS


MODULE_PATH = Path(__file__).resolve().parents[1] / "modules" / "agents" / "opencode" / "server.py"


def _load_server_module():
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.ClientTimeout = object
    previous_aiohttp = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = aiohttp_stub
    try:
        spec = importlib.util.spec_from_file_location("opencode_server_for_test", MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_aiohttp is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = previous_aiohttp


SERVER_MODULE = _load_server_module()
OpenCodeManagedPolicyRefreshPendingError = (
    SERVER_MODULE.OpenCodeManagedPolicyRefreshPendingError
)
OpenCodeRuntimeConfigInvalidError = SERVER_MODULE.OpenCodeRuntimeConfigInvalidError
OpenCodeServerManager = SERVER_MODULE.OpenCodeServerManager


def _model_hub_overlay(path: str, model_id: str | None):
    models = {} if model_id is None else {model_id: {"id": model_id}}
    content = json.dumps(
        {
            "enabled_providers": ["avibe-openai"],
            "provider": {
                "avibe-openai": {
                    "models": models,
                }
            }
        },
        sort_keys=True,
    )
    composed = SERVER_MODULE._managed_runtime_config_content(content)
    return types.SimpleNamespace(
        path=Path(path),
        content_hash=hashlib.sha256(composed.encode()).hexdigest(),
        content=content,
        provider_ids=("avibe-openai",),
    )


class _FakeResponse:
    def __init__(self, *, status: int = 204, text: str = "", json_data=None, headers=None):
        self.status = status
        self._text = text
        self._json_data = json_data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text

    async def read(self):
        return self._text.encode()

    async def json(self):
        return self._json_data if self._json_data is not None else {}


class _FakeUrlOpenResponse:
    def __init__(self, *, text: str = "", headers=None):
        self._text = text
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self._text.encode() if size is None or size < 0 else self._text.encode()[:size]


class _FakeSession:
    def __init__(self):
        self.gets = []
        self.posts = []
        self.puts = []
        self.patches = []
        self.closed = False

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return _FakeResponse(status=200)

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse()

    def put(self, url, json=None, headers=None):
        self.puts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(status=200)

    def patch(self, url, json=None, headers=None):
        self.patches.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(status=200)

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False


class OpenCodeServerTests(unittest.IsolatedAsyncioTestCase):
    def test_managed_runtime_config_accepts_jsonc_and_disables_native_skill(self):
        content = SERVER_MODULE._managed_runtime_config_content(
            b'''\xef\xbb\xbf{
              // OpenCode accepts JSONC in this inherited override.
              "permission": "ask",
              "tools": {"bash": true,},
            }'''
        )

        self.assertEqual(
            json.loads(content),
            {
                "permission": {"*": "ask", "skill": "deny"},
                "tools": {"bash": True, "skill": False},
            },
        )

    def test_managed_runtime_config_uses_typed_validation_errors(self):
        for content in ("{invalid", "[]", '{"permission":[]}'):
            with self.subTest(content=content):
                with self.assertRaises(OpenCodeRuntimeConfigInvalidError):
                    SERVER_MODULE._managed_runtime_config_content(content)

    async def test_start_server_reaps_a_live_process_after_cold_start_timeout(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        process = types.SimpleNamespace(pid=4321, returncode=None)
        manager._is_healthy = AsyncMock(return_value=False)  # type: ignore[method-assign]
        manager._write_pid_file = Mock()  # type: ignore[method-assign]
        manager._clear_pid_file = Mock()  # type: ignore[method-assign]
        manager._apply_resource_governance = Mock()  # type: ignore[method-assign]
        terminate = AsyncMock()
        create_process = AsyncMock(return_value=process)
        user_config = '{"permission":"ask"}'

        with (
            patch.object(
                SERVER_MODULE.asyncio,
                "create_subprocess_exec",
                create_process,
            ),
            patch.object(SERVER_MODULE.asyncio, "sleep", AsyncMock()),
            patch.object(SERVER_MODULE.time, "monotonic", side_effect=[0.0, 0.0, 61.0]),
            patch.object(SERVER_MODULE, "server_environment", return_value={}),
            patch.object(SERVER_MODULE, "terminate_process_tree", terminate),
            patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": user_config}),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to start within 60s"):
                await manager._start_server()

        terminate.assert_awaited_once_with(
            process,
            SERVER_MODULE.logger,
            "OpenCode server after startup timeout",
            terminate_timeout=5,
        )
        self.assertEqual(manager._clear_pid_file.call_count, 2)
        self.assertIsNone(manager._process)
        self.assertIsNone(manager._process_loop)
        self.assertEqual(
            json.loads(create_process.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"]),
            {
                "permission": {"*": "ask", "skill": "deny"},
                "tools": {"skill": False},
            },
        )
        self.assertEqual(
            create_process.await_args.kwargs["env"]["OPENCODE_DISABLE_EXTERNAL_SKILLS"],
            "1",
        )

    def test_terminate_instance_sync_stops_unadopted_managed_server(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "opencode_server.json"
            pid_file.write_text(json.dumps({"pid": 4321, "port": 4096}), encoding="utf-8")
            terminate = Mock(return_value=True)
            previous = OpenCodeServerManager._instance
            OpenCodeServerManager._instance = None
            try:
                with (
                    patch.object(SERVER_MODULE.paths, "get_logs_dir", return_value=Path(tmp_dir)),
                    patch.object(
                        SERVER_MODULE.runtime,
                        "get_process_command",
                        return_value="opencode serve --port=4096",
                    ),
                    patch.object(OpenCodeServerManager, "_terminate_pid_tree_sync", terminate),
                ):
                    OpenCodeServerManager.terminate_instance_sync()
            finally:
                OpenCodeServerManager._instance = previous

        terminate.assert_called_once_with(4321)
        self.assertFalse(pid_file.exists())

    def test_terminate_instance_sync_trusts_pid_file_port_owner_without_command(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "opencode_server.json"
            pid_file.write_text(json.dumps({"pid": 4321, "port": 4096}), encoding="utf-8")
            terminate = Mock(return_value=True)
            previous = OpenCodeServerManager._instance
            OpenCodeServerManager._instance = None
            try:
                with (
                    patch.object(SERVER_MODULE.paths, "get_logs_dir", return_value=Path(tmp_dir)),
                    patch.object(SERVER_MODULE.runtime, "get_process_command", return_value=None),
                    patch.object(OpenCodeServerManager, "_pid_owns_listening_port", return_value=True),
                    patch.object(OpenCodeServerManager, "_terminate_pid_tree_sync", terminate),
                ):
                    OpenCodeServerManager.terminate_instance_sync()
            finally:
                OpenCodeServerManager._instance = previous

        terminate.assert_called_once_with(4321)
        self.assertFalse(pid_file.exists())

    def test_percent_encode_path_preserves_round_trip_sensitive_paths(self):
        self.assertEqual(
            SERVER_MODULE._percent_encode_path("/tmp/小说"),
            "/tmp/%E5%B0%8F%E8%AF%B4",
        )
        self.assertEqual(
            SERVER_MODULE._percent_encode_path("/tmp/a b"),
            "/tmp/a%20b",
        )
        self.assertEqual(
            SERVER_MODULE._percent_encode_path("/tmp/a%20b"),
            "/tmp/a%2520b",
        )

    async def test_user_catalog_projects_current_hub_models_before_overlay_start(self):
        class _CatalogSession(_FakeSession):
            def get(self, url, headers=None, timeout=None):
                self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                return _FakeResponse(
                    status=200,
                    json_data={
                        "providers": [
                            {"id": "openai", "models": {"native-model": {}}},
                        ],
                        "default": {"openai": "native-model"},
                    },
                )

        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._get_http_session = AsyncMock(return_value=_CatalogSession())  # type: ignore[method-assign]

        models = await manager.get_available_models(
            "/tmp/work",
            model_hub_models={
                "current-model": {
                    "id": "current-model",
                    "name": "Current model",
                    "native_protocol": "openai_responses",
                },
            },
        )

        model_index = {row["id"]: row["models"] for row in models["providers"]}
        self.assertEqual(set(model_index), {"openai", "avibe-openai"})
        self.assertEqual(set(model_index["openai"]), {"native-model"})
        self.assertEqual(
            model_index["avibe-openai"]["current-model"],
            {
                "id": "current-model",
                "name": "Current model",
                "vibe_remote": {"model_hub_projected": True},
            },
        )

    async def test_user_catalog_boundary_excludes_model_hub_runtime_provider(self):
        runtime_ids = ("avibe-openai", "avibe-anthropic")
        legacy_custom_id = "avibe-model-hub-fedcba9876543210fedcba98"

        class _CatalogSession(_FakeSession):
            def get(self, url, headers=None, timeout=None):
                self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                if url.endswith("/config/providers"):
                    payload = {
                        "providers": [
                            {
                                "id": "avibe-openai",
                                "models": {
                                    "gpt-5": {
                                        "id": "gpt-5",
                                        "variants": {"high": {}},
                                    },
                                },
                            },
                            {
                                "id": "avibe-anthropic",
                                "models": {"claude-opus-5": {"id": "claude-opus-5"}},
                            },
                            {"id": legacy_custom_id, "models": {"relay-model": {}}},
                            {"id": "custom", "models": {"native-model": {}}},
                            {
                                "id": "openai",
                                "models": [
                                    {"id": "gpt-4", "name": "GPT-4"},
                                    {
                                        "id": "gpt-5",
                                        "name": "GPT-5",
                                        "capabilities": {"tools": True},
                                    },
                                ],
                            },
                        ],
                        "default": {
                            "avibe-openai": "gpt-5",
                            "avibe-anthropic": "claude-opus-5",
                            legacy_custom_id: "relay-model",
                            "openai": "gpt-5",
                        },
                    }
                elif url.endswith("/provider"):
                    payload = {
                        "all": {
                            "avibe-openai": {"id": "avibe-openai"},
                            "avibe-anthropic": {"id": "avibe-anthropic"},
                            legacy_custom_id: {"id": legacy_custom_id},
                            "openai": {"id": "openai"},
                        },
                        "connected": [*runtime_ids, legacy_custom_id, "openai"],
                    }
                else:
                    payload = {
                        "model": "avibe-openai/gpt-5",
                        "provider": {
                            "avibe-openai": {"options": {"apiKey": "private"}},
                            "avibe-anthropic": {"options": {"apiKey": "private"}},
                            legacy_custom_id: {},
                            "openai": {},
                        },
                    }
                return _FakeResponse(status=200, json_data=payload)

        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._model_hub_overlay_provider_ids = runtime_ids
        session = _CatalogSession()
        manager._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        manager.ensure_running = AsyncMock()  # type: ignore[method-assign]

        models = await manager.get_available_models(
            "/tmp/work",
            model_hub_models={
                "gpt-5": {
                    "id": "gpt-5",
                    "native_protocol": "openai_responses",
                    "variants": {"high": {}},
                },
                "claude-opus-5": {
                    "id": "claude-opus-5",
                    "native_protocol": "anthropic",
                },
            },
        )
        native_models = await manager.get_native_available_models("/tmp/work")
        providers = await manager.get_providers()
        config = await manager.get_default_config("/tmp/work")

        self.assertEqual(
            [row["id"] for row in models["providers"]],
            [legacy_custom_id, "custom", "openai", *runtime_ids],
        )
        self.assertEqual(
            models["default"],
            {
                legacy_custom_id: "relay-model",
                "openai": "gpt-5",
            },
        )
        self.assertEqual(set(providers["all"]), {legacy_custom_id, "openai"})
        self.assertEqual(providers["connected"], [legacy_custom_id, "openai"])
        self.assertNotIn("model", config)
        self.assertEqual(set(config["provider"]), {legacy_custom_id, "openai"})
        native_model_index = {
            row["id"]: row["models"] for row in native_models["providers"]
        }
        self.assertEqual(set(native_model_index), {legacy_custom_id, "custom", "openai"})
        self.assertEqual(
            {entry["id"] for entry in native_model_index["openai"]},
            {"gpt-4", "gpt-5"},
        )
        self.assertEqual(set(native_model_index["custom"]), {"native-model"})
        public_models = {
            row["id"]: row["models"] for row in models["providers"]
        }
        public_openai = {entry["id"]: entry for entry in public_models["openai"]}
        self.assertEqual(set(public_openai), {"gpt-4", "gpt-5"})
        self.assertEqual(public_openai["gpt-5"]["name"], "GPT-5")
        self.assertEqual(
            public_openai["gpt-5"]["capabilities"],
            {"tools": True},
        )
        self.assertEqual(set(public_models["custom"]), {"native-model"})
        self.assertEqual(
            public_models["avibe-openai"]["gpt-5"],
            {
                "id": "gpt-5",
                "variants": {"high": {}},
                "vibe_remote": {"model_hub_projected": True},
            },
        )
        self.assertEqual(
            public_models["avibe-anthropic"]["claude-opus-5"],
            {
                "id": "claude-opus-5",
                "vibe_remote": {"model_hub_projected": True},
            },
        )

    async def test_ensure_running_restarts_healthy_server_when_caller_context_plugin_changes(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        restarted = []
        started = []
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock(side_effect=lambda: restarted.append(True))  # type: ignore[method-assign]
        manager._start_server = AsyncMock(side_effect=lambda: started.append(True))  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {"pid": 123, "port": 4096, "caller_context_path": manager._caller_context_path(), "owner_pid": SERVER_MODULE._CURRENT_OWNER_PID, "runtime_policy_revision": SERVER_MODULE._MANAGED_RUNTIME_POLICY_REVISION}  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=True),
        ):
            base_url = await manager.ensure_running()

        self.assertEqual(base_url, "http://127.0.0.1:4096")
        self.assertEqual(restarted, [True])
        self.assertEqual(started, [True])
        self.assertFalse(manager._caller_context_plugin_refresh_pending)

    async def test_ensure_running_defers_plugin_restart_while_run_active(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._active_run_sessions.add("ses-active")
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {"pid": 123, "port": 4096, "caller_context_path": manager._caller_context_path(), "owner_pid": SERVER_MODULE._CURRENT_OWNER_PID, "runtime_policy_revision": SERVER_MODULE._MANAGED_RUNTIME_POLICY_REVISION}  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "caller-context plugin refresh is pending"):
                await manager.ensure_running()

        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()
        self.assertTrue(manager._caller_context_plugin_refresh_pending)

    async def test_ensure_running_restarts_adopted_server_without_caller_context_env(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        restarted = []
        started = []
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock(side_effect=lambda: restarted.append(True))  # type: ignore[method-assign]
        manager._start_server = AsyncMock(side_effect=lambda: started.append(True))  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {"pid": 123, "port": 4096, "active_run_sessions": []}  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            base_url = await manager.ensure_running()

        self.assertEqual(base_url, "http://127.0.0.1:4096")
        self.assertEqual(restarted, [True])
        self.assertEqual(started, [True])
        self.assertFalse(manager._caller_context_plugin_refresh_pending)

    async def test_ensure_running_restarts_idle_adopted_server_with_stale_runtime_policy(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 123,
            "port": 4096,
            "caller_context_path": manager._caller_context_path(),
            "active_run_sessions": [],
            "runtime_policy_revision": "previous-policy",
        }
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            base_url = await manager.ensure_running()

        self.assertEqual(base_url, "http://127.0.0.1:4096")
        manager._restart_for_auth_refresh_locked.assert_awaited_once()
        manager._start_server.assert_awaited_once()

    async def test_ensure_running_reconciles_orphaned_adopted_run_before_policy_refresh(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = OpenCodeServerManager(binary="opencode", port=4096)
            manager._pid_file = Path(tmp_dir) / "opencode_server.json"
            manager._pid_file.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "port": 4096,
                        "caller_context_path": manager._caller_context_path(),
                        "active_run_sessions": ["ses-orphan"],
                        "runtime_policy_revision": "previous-policy",
                    }
                ),
                encoding="utf-8",
            )
            manager.set_active_poll_session_ids_provider(set)
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
            manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
            manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
            manager._start_server = AsyncMock()  # type: ignore[method-assign]
            manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

            with patch.object(
                SERVER_MODULE,
                "ensure_plugin_installed",
                return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
            ):
                base_url = await manager.ensure_running()

            self.assertEqual(base_url, "http://127.0.0.1:4096")
            manager._restart_for_auth_refresh_locked.assert_awaited_once()
            manager._start_server.assert_awaited_once()
            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_run_sessions"], [])

    async def test_ensure_running_keeps_adopted_run_with_durable_poll(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager.set_active_poll_session_ids_provider(lambda: {"ses-active"})
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 123,
            "port": 4096,
            "caller_context_path": manager._caller_context_path(),
            "active_run_sessions": ["ses-active"],
            "runtime_policy_revision": "previous-policy",
        }
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            with self.assertRaises(OpenCodeManagedPolicyRefreshPendingError):
                await manager.ensure_running()

        self.assertEqual(manager._active_run_sessions, {"ses-active"})
        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()

    async def test_ensure_running_keeps_adopted_run_when_poll_reconciliation_fails(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)

        def unavailable_polls() -> set[str]:
            raise OSError("sessions unavailable")

        manager.set_active_poll_session_ids_provider(unavailable_polls)
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 123,
            "port": 4096,
            "caller_context_path": manager._caller_context_path(),
            "active_run_sessions": ["ses-unknown"],
            "runtime_policy_revision": "previous-policy",
        }
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            with self.assertRaises(OpenCodeManagedPolicyRefreshPendingError):
                await manager.ensure_running()

        self.assertEqual(manager._active_run_sessions, set())
        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()

    async def test_ensure_running_defers_adopted_server_without_caller_context_env(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {"pid": 123, "port": 4096, "active_run_sessions": ["ses-active"]}  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            with self.assertRaisesRegex(
                OpenCodeManagedPolicyRefreshPendingError,
                "adopted or active server",
            ):
                await manager.ensure_running()

        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()
        self.assertTrue(manager._caller_context_plugin_refresh_pending)

    async def test_ensure_running_defers_adopted_server_with_unknown_active_state(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {"pid": 123, "port": 4096}  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "adopted or active server"):
                await manager.ensure_running()

        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()
        self.assertTrue(manager._caller_context_plugin_refresh_pending)

    async def test_ensure_running_preserves_an_adopted_absolute_caller_context_path(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        adopted_path = "/old-avibe-home/runtime/opencode_caller_context.json"
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 123,
            "port": 4096,
            "caller_context_path": adopted_path,
            "active_run_sessions": ["ses-active"],
            "runtime_policy_revision": SERVER_MODULE._MANAGED_RUNTIME_POLICY_REVISION,
        }
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
        manager._caller_context_path = lambda: "/new-avibe-home/runtime/opencode_caller_context.json"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            base_url = await manager.ensure_running()

        self.assertEqual(base_url, "http://127.0.0.1:4096")
        self.assertEqual(manager.caller_context_binding_path(), Path(adopted_path))
        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()
        self.assertFalse(manager._caller_context_plugin_refresh_pending)

    async def test_mark_run_active_persists_pid_file_active_sessions(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager._pid_file = Path(tmp_dir) / "opencode_server.json"
            manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
            manager._write_pid_file(123)

            await manager.mark_run_active("ses-active")

            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_run_sessions"], ["ses-active"])
            self.assertEqual(
                payload["runtime_policy_revision"],
                SERVER_MODULE._MANAGED_RUNTIME_POLICY_REVISION,
            )

            await manager.mark_run_inactive("ses-active")

            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_run_sessions"], [])

    async def test_mark_run_inactive_preserves_active_state_when_pid_write_fails(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager._pid_file = Path(tmp_dir) / "opencode_server.json"
            manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
            manager._write_pid_file(123)
            await manager.mark_run_active("ses-active")

            with patch.object(
                Path,
                "write_text",
                side_effect=OSError("read-only pid file"),
            ):
                with self.assertRaisesRegex(OSError, "read-only pid file"):
                    await manager.mark_run_inactive("ses-active")

            self.assertEqual(manager._active_run_sessions, {"ses-active"})
            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_run_sessions"], ["ses-active"])

    async def test_mark_run_inactive_preserves_other_adopted_active_sessions(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager._pid_file = Path(tmp_dir) / "opencode_server.json"
            manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
            manager._write_pid_file(123)
            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            payload["active_run_sessions"] = ["ses-completed", "ses-other-platform"]
            manager._pid_file.write_text(json.dumps(payload), encoding="utf-8")

            await manager.mark_run_inactive("ses-completed")

            self.assertEqual(manager._active_run_sessions, {"ses-other-platform"})
            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_run_sessions"], ["ses-other-platform"])

    async def test_mark_run_active_preserves_adopted_active_sessions(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager._pid_file = Path(tmp_dir) / "opencode_server.json"
            manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
            manager._write_pid_file(123)
            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            payload["active_run_sessions"] = ["ses-other-platform"]
            manager._pid_file.write_text(json.dumps(payload), encoding="utf-8")

            await manager.mark_run_active("ses-new")

            self.assertEqual(manager._active_run_sessions, {"ses-new", "ses-other-platform"})
            payload = json.loads(manager._pid_file.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["active_run_sessions"],
                ["ses-new", "ses-other-platform"],
            )

    async def test_cleanup_stale_managed_pid_does_not_inherit_caller_context_for_new_pid(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        pid_info = {"pid": 111, "port": 4096, "caller_context_path": manager._caller_context_path()}
        writes = []
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._read_pid_file = lambda: pid_info  # type: ignore[method-assign]
        manager._find_opencode_serve_pids = lambda port: [222]  # type: ignore[method-assign]

        def _write_pid_file(
            pid,
            *,
            caller_context_path=SERVER_MODULE._USE_CURRENT_CALLER_CONTEXT_PATH,
            owner_pid=SERVER_MODULE._CURRENT_OWNER_PID,
            runtime_policy_revision=SERVER_MODULE._MANAGED_RUNTIME_POLICY_REVISION,
        ):
            writes.append((pid, caller_context_path, owner_pid, runtime_policy_revision))
            pid_info.clear()
            pid_info.update({"pid": pid, "port": 4096})
            if isinstance(caller_context_path, str) and caller_context_path:
                pid_info["caller_context_path"] = caller_context_path

        manager._write_pid_file = _write_pid_file  # type: ignore[method-assign]

        await manager._cleanup_orphaned_managed_server()

        self.assertEqual(writes, [(222, None, None, None)])
        self.assertNotIn("caller_context_path", pid_info)

    async def test_ensure_running_rejects_unmanaged_healthy_server(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
        manager._start_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: None  # type: ignore[method-assign]
        manager._write_pid_file = lambda pid: self.fail("unmanaged server must not be adopted as Avibe-managed")  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "not managed by Avibe"):
                await manager.ensure_running()

        manager._restart_for_auth_refresh_locked.assert_not_awaited()
        manager._start_server.assert_not_awaited()

    async def test_ensure_running_retires_generation_before_process_replacement(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        events = []
        manager._runtime_generation_token = (111, 1.0)
        manager.set_runtime_activation_retire(
            lambda force, native_turns_drained: events.append(
                ("retire", force, native_turns_drained)
            )
            or True
        )
        manager._is_healthy = AsyncMock(return_value=False)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._is_port_available = lambda: True  # type: ignore[method-assign]
        manager._start_server = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda: events.append(("start", True))
        )

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            await manager.ensure_running()

        self.assertEqual(events, [("retire", True, False), ("start", True)])

    async def test_ensure_running_retires_generation_when_adopted_pid_changes(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        retired = []
        manager._runtime_generation_token = (111, 1.0)
        manager.set_runtime_activation_retire(
            lambda force, native_turns_drained: retired.append(
                (force, native_turns_drained)
            )
            or True
        )
        manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
        manager._cleanup_orphaned_managed_server = AsyncMock()  # type: ignore[method-assign]
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 222,
            "port": 4096,
            "started_at": 2.0,
            "caller_context_path": manager._caller_context_path(),
            "runtime_policy_revision": SERVER_MODULE._MANAGED_RUNTIME_POLICY_REVISION,
        }
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        with patch.object(
            SERVER_MODULE,
            "ensure_plugin_installed",
            return_value=types.SimpleNamespace(path=Path("/tmp/plugin.js"), changed=False),
        ):
            await manager.ensure_running()

        self.assertEqual(retired, [(True, False)])
        self.assertEqual(manager._runtime_generation_token, (222, 2.0))

    async def test_prompt_async_percent_encodes_directory_header(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        await manager.prompt_async(
            session_id="ses-1",
            directory="/tmp/小说/a%20b",
            text="hello",
        )

        self.assertEqual(len(fake_session.posts), 1)
        self.assertEqual(
            fake_session.posts[0]["headers"],
            {"x-opencode-directory": "/tmp/%E5%B0%8F%E8%AF%B4/a%2520b"},
        )

    async def test_get_session_status_uses_installed_status_map_shape(self):
        class _StatusSession(_FakeSession):
            def get(self, url, headers=None, timeout=None):
                self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                return _FakeResponse(
                    status=200,
                    json_data={"ses-active": {"type": "busy"}, "ses-idle": {"type": "idle"}},
                )

        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _StatusSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        status = await manager.get_session_status("ses-active", "/tmp/小说")
        missing = await manager.get_session_status("ses-missing", "/tmp/小说")

        self.assertEqual(status, {"type": "busy"})
        self.assertIsNone(missing)
        self.assertEqual(fake_session.gets[0]["url"], "http://127.0.0.1:4096/session/status")
        self.assertEqual(
            fake_session.gets[0]["headers"],
            {"x-opencode-directory": "/tmp/%E5%B0%8F%E8%AF%B4"},
        )

    async def test_get_version_uses_health_endpoint(self):
        class _HealthSession(_FakeSession):
            def get(self, url, headers=None, timeout=None):
                self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                return _FakeResponse(
                    status=200,
                    json_data={"healthy": True, "version": "1.18.5"},
                )

        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _HealthSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        with patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()):
            version = await manager.get_version()

        self.assertEqual(version, "1.18.5")
        self.assertEqual(fake_session.gets[0]["url"], "http://127.0.0.1:4096/global/health")

    async def test_prompt_async_includes_tools_when_provided(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        await manager.prompt_async(
            session_id="ses-1",
            directory="/tmp/work",
            text="hello",
            tools={"question": False},
        )

        self.assertEqual(len(fake_session.posts), 1)
        body = fake_session.posts[0]["json"]
        self.assertEqual(body["tools"], {"question": False})

    async def test_prompt_async_uses_opencode_native_attempt_part(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        await manager.prompt_async(
            session_id="ses-1",
            directory="/tmp/work",
            text="hello",
            attempt_id="atm_1234567890abcdef1234567890abcdef",
        )

        body = fake_session.posts[0]["json"]
        self.assertNotIn("messageID", body)
        self.assertEqual(
            body["parts"],
            [
                {
                    "type": "text",
                    "text": "hello",
                    "id": "prt_1234567890abcdef1234567890abcdef",
                }
            ],
        )

    def test_durable_attempt_maps_to_opencode_part_evidence(self):
        attempt_id = delivery_store.new_attempt_id()

        self.assertRegex(attempt_id, r"^atm_[0-9a-f]{32}$")
        self.assertEqual(
            SERVER_MODULE.native_part_id_for_attempt(attempt_id),
            f"prt_{attempt_id.removeprefix('atm_')}",
        )

    def test_unreleased_ordered_attempt_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            SERVER_MODULE.native_part_id_for_attempt(
                "atm_1234567890000123456789abcd"
            )

    async def test_prompt_async_exposes_definitive_http_rejection(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        def _post(url, json=None, headers=None):
            fake_session.posts.append({"url": url, "json": json, "headers": headers})
            return _FakeResponse(status=409, text="active input refused")

        fake_session.post = _post

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        with self.assertRaises(SERVER_MODULE.OpenCodePromptRejectedError) as raised:
            await manager.prompt_async(
                session_id="ses-1",
                directory="/tmp/work",
                text="hello",
            )

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.response_text, "active input refused")

    async def test_prompt_async_omits_default_variant(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        await manager.prompt_async(
            session_id="ses-1",
            directory="/tmp/work",
            text="hello",
            reasoning_effort="default",
        )

        self.assertEqual(len(fake_session.posts), 1)
        body = fake_session.posts[0]["json"]
        self.assertNotIn("variant", body)

    async def test_fork_session_sends_message_id_when_provided(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        def _post(url, json=None, headers=None):
            fake_session.posts.append({"url": url, "json": json, "headers": headers})
            return _FakeResponse(status=200, json_data={"id": "oc-fork"})

        fake_session.post = _post

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        result = await manager.fork_session("oc-source", directory="/tmp/work", message_id="oc-msg-prev")

        self.assertEqual(result, {"id": "oc-fork"})
        self.assertEqual(len(fake_session.posts), 1)
        self.assertEqual(fake_session.posts[0]["json"], {"messageID": "oc-msg-prev"})
        self.assertEqual(
            fake_session.posts[0]["headers"],
            {"x-opencode-directory": "/tmp/work"},
        )

    async def test_load_opencode_user_config_supports_jsonc(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                """{
  // Preserve defaults from JSONC config.
  "model": "openai/gpt-5",
  "reasoningEffort": "high",
}
""",
                encoding="utf-8",
            )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            with patch("vibe.opencode_config.Path.home", return_value=tmp_home):
                config = manager._load_opencode_user_config()

            self.assertEqual(
                config,
                {
                    "model": "openai/gpt-5",
                    "reasoningEffort": "high",
                },
            )

    async def test_agent_reasoning_effort_reads_back_every_savable_variant(self):
        # A tier the save path can write must never be dropped here as unknown
        # (#1840: catalog-declared `ultra` was rejected by both halves).
        for effort in OPENCODE_REASONING_VARIANTS:
            with self.subTest(effort=effort):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_home = Path(tmp_dir)
                    config_path = tmp_home / ".config" / "opencode" / "opencode.json"
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    config_path.write_text(
                        json.dumps({"reasoningEffort": effort}),
                        encoding="utf-8",
                    )

                    manager = OpenCodeServerManager(binary="opencode", port=4096)
                    with patch("vibe.opencode_config.Path.home", return_value=tmp_home):
                        resolved = manager.get_agent_reasoning_effort_from_config(None)

                    self.assertEqual(resolved, effort)

    async def test_refresh_global_config_patches_live_server(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"deepseek":{"options":{"baseURL":"https://api.deepseek.com"}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "deepseek": {
                                    "options": {
                                        "apiKey": "sk-live",
                                        "baseURL": "https://stale.example",
                                    }
                                },
                                "openai": {"options": {"apiKey": "sk-openai"}},
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertTrue(fake_session.closed)
            self.assertEqual(len(fake_session.gets), 1)
            self.assertEqual(len(fake_session.patches), 1)
            self.assertEqual(
                fake_session.patches[0]["url"],
                "http://127.0.0.1:4096/global/config",
            )
            self.assertEqual(
                fake_session.patches[0]["json"],
                {
                    "provider": {
                        "deepseek": {
                            "options": {
                                "baseURL": "https://api.deepseek.com",
                            }
                        }
                    }
                },
            )

    async def test_refresh_global_config_preserves_auth_json_api_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"anthropic":{"options":{"baseURL":"https://relay.example/v1"}}}}',
                encoding="utf-8",
            )
            auth_path = tmp_home / ".local" / "share" / "opencode" / "auth.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                '{"anthropic":{"type":"api","key":"sk-auth-json"}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "anthropic": {
                                    "options": {
                                        "apiKey": "sk-auth-json",
                                        "baseURL": "https://stale.example",
                                    }
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["anthropic"]["options"],
                {
                    "apiKey": "sk-auth-json",
                    "baseURL": "https://relay.example/v1",
                },
            )

    async def test_refresh_global_config_does_not_resurrect_deleted_provider_options(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"openai":{"options":{"apiKey":"sk-config"}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "openai": {
                                    "options": {
                                        "apiKey": "sk-config",
                                        "baseURL": "https://deleted.example/v1",
                                    }
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["openai"]["options"],
                {"apiKey": "sk-config"},
            )

    async def test_refresh_global_config_oauth_entry_clears_stale_api_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"openai":{"options":{"baseURL":"https://relay.example/v1"}}}}',
                encoding="utf-8",
            )
            auth_path = tmp_home / ".local" / "share" / "opencode" / "auth.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                '{"openai":{"type":"oauth","refresh":"oauth-refresh"}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "openai": {
                                    "options": {
                                        "apiKey": "sk-stale",
                                        "baseURL": "https://old.example/v1",
                                    }
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["openai"]["options"],
                {"baseURL": "https://relay.example/v1"},
            )

    async def test_refresh_global_config_drops_live_provider_missing_from_user_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"anthropic":{"options":{"baseURL":"https://relay.example/v1"}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "anthropic": {"options": {"baseURL": "https://relay.example/v1"}},
                                "openai": {
                                    "options": {
                                        "apiKey": "sk-stale",
                                        "baseURL": "https://deleted.example/v1",
                                    }
                                },
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                set(fake_session.patches[0]["json"]["provider"].keys()),
                {"anthropic"},
            )

    async def test_refresh_global_config_preserves_new_provider_options(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"claude-relay":{"name":"Claude Relay","npm":"@ai-sdk/anthropic","options":{"baseURL":"https://relay.example/v1","apiKey":"sk-new"},"models":{"claude-opus-4.8":{"id":"claude-opus-4.8"}}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(status=200, json_data={"provider": {}})

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            provider = fake_session.patches[0]["json"]["provider"]["claude-relay"]
            self.assertEqual(
                provider["options"],
                {"baseURL": "https://relay.example/v1", "apiKey": "sk-new"},
            )
            self.assertIn("claude-opus-4.8", provider["models"])

    async def test_refresh_global_config_uses_auth_json_api_key_over_stale_live_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"openai":{"options":{"baseURL":"https://relay.example/v1"}}}}',
                encoding="utf-8",
            )
            auth_path = tmp_home / ".local" / "share" / "opencode" / "auth.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                '{"openai":{"type":"api","key":"sk-new-auth"}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "openai": {
                                    "options": {
                                        "apiKey": "sk-old-live",
                                        "baseURL": "https://old.example/v1",
                                    }
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["openai"]["options"],
                {"baseURL": "https://relay.example/v1", "apiKey": "sk-new-auth"},
            )

    async def test_refresh_global_config_drops_live_options_when_user_options_section_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"deepseek":{"models":{"deepseek-v4-flash":{"id":"deepseek-v4-flash"}}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "deepseek": {
                                    "options": {
                                        "apiKey": "sk-stale",
                                        "baseURL": "https://stale.example",
                                    },
                                    "models": {"deepseek-v4-flash": {"id": "deepseek-v4-flash"}},
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            provider = fake_session.patches[0]["json"]["provider"]["deepseek"]
            self.assertNotIn("options", provider)
            self.assertIn("deepseek-v4-flash", provider["models"])

    async def test_refresh_global_config_preserves_auth_key_when_only_models_configured(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"deepseek":{"models":{"deepseek-v4-flash":{"id":"deepseek-v4-flash"}}}}}',
                encoding="utf-8",
            )
            auth_path = tmp_home / ".local" / "share" / "opencode" / "auth.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                '{"deepseek":{"type":"api","key":"sk-auth-json"}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "deepseek": {
                                    "options": {
                                        "apiKey": "sk-stale-live",
                                        "baseURL": "https://stale.example",
                                    },
                                    "models": {"deepseek-v4-flash": {"id": "deepseek-v4-flash"}},
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            provider = fake_session.patches[0]["json"]["provider"]["deepseek"]
            self.assertEqual(provider["options"], {"apiKey": "sk-auth-json"})
            self.assertIn("deepseek-v4-flash", provider["models"])

    async def test_refresh_global_config_drops_deleted_user_models(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"deepseek":{"options":{"baseURL":"https://api.deepseek.com"},"models":{"keep-model":{"id":"keep-model"}}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "deepseek": {
                                    "options": {"baseURL": "https://api.deepseek.com"},
                                    "models": {
                                        "keep-model": {"id": "keep-model"},
                                        "deleted-model": {"id": "deleted-model"},
                                    },
                                }
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["deepseek"]["models"],
                {"keep-model": {"id": "keep-model"}},
            )

    async def test_refresh_global_config_keeps_auth_backed_provider_absent_from_user_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"claude-relay":{"options":{"baseURL":"https://relay.example/v1"}}}}',
                encoding="utf-8",
            )
            auth_path = tmp_home / ".local" / "share" / "opencode" / "auth.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                '{"openai":{"type":"oauth","refresh":"oauth-refresh"}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "openai": {"options": {"baseURL": "https://api.openai.com/v1"}},
                                "claude-relay": {"options": {"baseURL": "https://old.example/v1"}},
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            self.assertEqual(
                set(fake_session.patches[0]["json"]["provider"].keys()),
                {"claude-relay", "openai"},
            )
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["openai"]["options"],
                {"baseURL": "https://api.openai.com/v1"},
            )
            self.assertEqual(
                fake_session.patches[0]["json"]["provider"]["claude-relay"]["options"],
                {"baseURL": "https://relay.example/v1"},
            )

    async def test_refresh_global_config_preserves_local_provider_absent_from_user_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"claude-relay":{"options":{"baseURL":"https://relay.example/v1"}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "provider": {
                                "ollama": {
                                    "name": "Ollama",
                                    "options": {"baseURL": "http://localhost:11434/v1"},
                                    "models": {"llama3.1": {"id": "llama3.1"}},
                                },
                                "claude-relay": {"options": {"baseURL": "https://old.example/v1"}},
                            }
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            providers = fake_session.patches[0]["json"]["provider"]
            self.assertEqual(set(providers.keys()), {"claude-relay", "ollama"})
            self.assertEqual(
                providers["ollama"]["models"],
                {"llama3.1": {"id": "llama3.1"}},
            )
            self.assertEqual(
                providers["claude-relay"]["options"],
                {"baseURL": "https://relay.example/v1"},
            )

    async def test_refresh_global_config_drops_removed_top_level_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '{"provider":{"deepseek":{"options":{"baseURL":"https://api.deepseek.com"}}}}',
                encoding="utf-8",
            )

            class _SnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    return _FakeResponse(
                        status=200,
                        json_data={
                            "permission": "allow",
                            "model": "openai/gpt-5",
                            "provider": {
                                "deepseek": {"options": {"baseURL": "https://old.example"}}
                            },
                        },
                    )

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _SnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertTrue(refreshed)
            patched_config = fake_session.patches[0]["json"]
            self.assertNotIn("permission", patched_config)
            self.assertNotIn("model", patched_config)
            self.assertEqual(
                patched_config["provider"]["deepseek"]["options"],
                {"baseURL": "https://api.deepseek.com"},
            )

    async def test_refresh_global_config_returns_false_when_global_endpoint_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('{"provider":{"deepseek":{"models":{}}}}', encoding="utf-8")

            class _UnavailableSession(_FakeSession):
                def patch(self, url, json=None, headers=None):
                    self.patches.append({"url": url, "json": json, "headers": headers})
                    return _FakeResponse(status=404)

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _UnavailableSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertFalse(refreshed)
            self.assertEqual(
                [call["url"] for call in fake_session.patches],
                ["http://127.0.0.1:4096/global/config"],
            )

    async def test_refresh_global_config_returns_false_when_snapshot_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('{"provider":{"deepseek":{"models":{}}}}', encoding="utf-8")

            class _UnavailableSnapshotSession(_FakeSession):
                def get(self, url, headers=None, timeout=None):
                    self.gets.append({"url": url, "headers": headers, "timeout": timeout})
                    if url.endswith("/global/config"):
                        return _FakeResponse(status=404)
                    return _FakeResponse(status=200, json_data={"healthy": True})

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _UnavailableSnapshotSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertFalse(refreshed)
            self.assertEqual(fake_session.patches, [])

    async def test_refresh_global_config_defers_when_request_active(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('{"provider":{"deepseek":{"models":{}}}}', encoding="utf-8")

            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _FakeSession()
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
            manager._active_requests = 1

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
            ):
                refreshed = await manager.refresh_global_config()

            self.assertFalse(refreshed)
            self.assertEqual(fake_session.patches, [])

    async def test_refresh_global_config_blocks_new_request_scope_while_patching(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('{"provider":{"deepseek":{"models":{}}}}', encoding="utf-8")

            class _BlockingResponse(_FakeResponse):
                def __init__(self, entered: asyncio.Event, release: asyncio.Event):
                    super().__init__(status=200)
                    self._entered = entered
                    self._release = release

                async def __aenter__(self):
                    self._entered.set()
                    await self._release.wait()
                    return self

            class _BlockingSession(_FakeSession):
                def __init__(self, entered: asyncio.Event, release: asyncio.Event):
                    super().__init__()
                    self._entered = entered
                    self._release = release

                def patch(self, url, json=None, headers=None):
                    self.patches.append({"url": url, "json": json, "headers": headers})
                    return _BlockingResponse(self._entered, self._release)

            entered = asyncio.Event()
            release = asyncio.Event()
            manager = OpenCodeServerManager(binary="opencode", port=4096)
            fake_session = _BlockingSession(entered, release)
            manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.aiohttp, "ClientSession", return_value=fake_session),
                patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()),
            ):
                refresh_task = asyncio.create_task(manager.refresh_global_config())
                await entered.wait()
                request_scope = manager._request_scope()
                request_task = asyncio.create_task(request_scope.__aenter__())
                await asyncio.sleep(0)

                self.assertFalse(request_task.done())
                release.set()
                self.assertTrue(await refresh_task)
                await request_task
                self.assertEqual(manager._active_requests, 1)
                await request_scope.__aexit__(None, None, None)
                self.assertEqual(manager._active_requests, 0)

    async def test_find_opencode_serve_pids_windows_uses_netstat_and_command_lookup(self):
        netstat_output = """
  TCP    127.0.0.1:4096     0.0.0.0:0      LISTENING       1234
  TCP    127.0.0.1:7777     0.0.0.0:0      LISTENING       7777
"""

        with patch.object(SERVER_MODULE.os, "name", "nt"):
            with patch.object(
                SERVER_MODULE.subprocess,
                "run",
                return_value=types.SimpleNamespace(stdout=netstat_output),
            ):
                with patch.object(
                    SERVER_MODULE.runtime,
                    "get_process_command",
                    side_effect=lambda pid: "opencode serve --port=4096" if pid == 1234 else "python app.py",
                ):
                    pids = OpenCodeServerManager._find_opencode_serve_pids(4096)

        self.assertEqual(pids, [1234])

    async def test_restart_for_auth_refresh_stops_known_server_and_clears_state(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()
        manager._process = object()
        manager._base_url = "http://127.0.0.1:4096"
        manager._read_pid_file = lambda: {"pid": 321}  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 321  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
        terminated = []
        manager._terminate_pid = lambda pid, reason: terminated.append((pid, reason)) or _async_none()  # type: ignore[method-assign]
        manager._clear_pid_file = lambda: terminated.append(("cleared", ""))  # type: ignore[method-assign]

        await manager.restart_for_auth_refresh()

        self.assertTrue(fake_session.closed)
        self.assertIn((321, "auth refresh"), terminated)
        self.assertIn(("cleared", ""), terminated)
        self.assertIsNone(manager._process)
        self.assertIsNone(manager._base_url)

    async def test_restart_for_auth_refresh_trusts_pid_file_when_command_lookup_unavailable(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()
        manager._process = object()
        manager._base_url = "http://127.0.0.1:4096"
        manager._read_pid_file = lambda: {"pid": 654, "port": 4096}  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 654  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: None  # type: ignore[method-assign]
        manager._pid_owns_listening_port = lambda pid, port: pid == 654 and port == 4096  # type: ignore[method-assign]
        terminated = []
        manager._terminate_pid = lambda pid, reason: terminated.append((pid, reason)) or _async_none()  # type: ignore[method-assign]
        manager._clear_pid_file = lambda: terminated.append(("cleared", ""))  # type: ignore[method-assign]

        await manager.restart_for_auth_refresh()

        self.assertTrue(fake_session.closed)
        self.assertIn((654, "auth refresh"), terminated)
        self.assertIn(("cleared", ""), terminated)

    async def test_restart_for_auth_refresh_does_not_trust_pid_file_without_port_ownership(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()
        manager._process = object()
        manager._base_url = "http://127.0.0.1:4096"
        manager._read_pid_file = lambda: {"pid": 654, "port": 4096}  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 654  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: None  # type: ignore[method-assign]
        manager._pid_owns_listening_port = lambda pid, port: False  # type: ignore[method-assign]
        manager._find_opencode_serve_pids = lambda port: []  # type: ignore[method-assign]
        terminated = []
        manager._terminate_pid = lambda pid, reason: terminated.append((pid, reason)) or _async_none()  # type: ignore[method-assign]
        manager._clear_pid_file = lambda: terminated.append(("cleared", ""))  # type: ignore[method-assign]

        await manager.restart_for_auth_refresh()

        self.assertTrue(fake_session.closed)
        self.assertNotIn((654, "auth refresh"), terminated)
        self.assertEqual(terminated, [("cleared", "")])

    async def test_restart_for_auth_refresh_defers_while_requests_are_active(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()
        manager._process = object()
        manager._base_url = "http://127.0.0.1:4096"
        manager._active_requests = 2
        terminated = []
        manager._terminate_pid = lambda pid, reason: terminated.append((pid, reason)) or _async_none()  # type: ignore[method-assign]
        manager._clear_pid_file = lambda: terminated.append(("cleared", ""))  # type: ignore[method-assign]

        await manager.restart_for_auth_refresh()

        self.assertFalse(fake_session.closed)
        self.assertEqual(terminated, [])
        self.assertTrue(manager._auth_refresh_pending)
        self.assertIsNotNone(manager._process)
        self.assertEqual(manager._base_url, "http://127.0.0.1:4096")

    async def test_restart_for_auth_refresh_defers_while_runs_are_active(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()
        manager._process = object()
        manager._base_url = "http://127.0.0.1:4096"
        manager._active_run_sessions.add("sess-1")
        terminated = []
        manager._terminate_pid = lambda pid, reason: terminated.append((pid, reason)) or _async_none()  # type: ignore[method-assign]
        manager._clear_pid_file = lambda: terminated.append(("cleared", ""))  # type: ignore[method-assign]

        await manager.restart_for_auth_refresh()

        self.assertFalse(fake_session.closed)
        self.assertEqual(terminated, [])
        self.assertTrue(manager._auth_refresh_pending)
        self.assertIsNotNone(manager._process)
        self.assertEqual(manager._base_url, "http://127.0.0.1:4096")

    async def test_restart_for_auth_refresh_force_clears_stale_activity(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._active_requests = 2
        manager._active_run_sessions.add("sess-stale")
        manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]

        await manager.restart_for_auth_refresh(force=True)

        self.assertEqual(manager._active_requests, 2)
        self.assertEqual(manager._active_run_sessions, set())
        manager._restart_for_auth_refresh_locked.assert_awaited_once()

    def test_runtime_has_active_turns_reads_adopted_pid_state(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 321,
            "port": 4096,
            "active_run_sessions": ["sess-live"],
        }
        manager._pid_exists = lambda pid: pid == 321  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]

        self.assertTrue(manager.runtime_has_active_turns())

    def test_runtime_ignores_active_runs_from_reused_pid(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._read_pid_file = lambda: {  # type: ignore[method-assign]
            "pid": 321,
            "port": 4096,
            "active_run_sessions": ["sess-stale"],
        }
        manager._pid_exists = lambda pid: pid == 321  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "python unrelated.py"  # type: ignore[method-assign]

        self.assertFalse(manager.runtime_has_active_turns())

    def test_terminate_sync_falls_back_to_tracked_process_without_pid_file(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._process = types.SimpleNamespace(pid=654, returncode=None)
        manager._read_pid_file = lambda: None  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 654  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
        manager._clear_pid_file = Mock()  # type: ignore[method-assign]
        manager._terminate_pid_tree_sync = Mock(return_value=True)  # type: ignore[method-assign]

        manager.terminate_sync()

        manager._terminate_pid_tree_sync.assert_called_once_with(654)
        self.assertIsNone(manager._process)

    def test_terminate_sync_does_not_kill_reused_tracked_pid(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._process = types.SimpleNamespace(pid=654, returncode=None)
        manager._read_pid_file = lambda: None  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 654  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "python unrelated.py"  # type: ignore[method-assign]
        manager._clear_pid_file = Mock()  # type: ignore[method-assign]
        manager._terminate_pid_tree_sync = Mock(return_value=True)  # type: ignore[method-assign]

        manager.terminate_sync()

        manager._terminate_pid_tree_sync.assert_not_called()
        self.assertIsNone(manager._process)

    def test_terminate_sync_trusts_pid_file_port_owner_without_command(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._read_pid_file = lambda: {"pid": 654, "port": 4096}  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 654  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: None  # type: ignore[method-assign]
        manager._pid_owns_listening_port = lambda pid, port: pid == 654 and port == 4096  # type: ignore[method-assign]
        manager._clear_pid_file = Mock()  # type: ignore[method-assign]
        manager._terminate_pid_tree_sync = Mock(return_value=True)  # type: ignore[method-assign]

        manager.terminate_sync()

        manager._terminate_pid_tree_sync.assert_called_once_with(654)

    async def test_request_scope_does_not_restart_pending_auth_refresh_while_run_active(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        manager._auth_refresh_pending = True
        manager._active_run_sessions.add("sess-1")
        restarted = []
        manager._restart_for_auth_refresh_locked = lambda: restarted.append(True) or _async_none()  # type: ignore[method-assign]

        async with manager._request_scope():
            self.assertEqual(manager._active_requests, 1)

        self.assertEqual(restarted, [])
        self.assertEqual(manager._active_requests, 0)
        self.assertTrue(manager._auth_refresh_pending)

    async def test_reload_runtime_config_updates_singleton_binary(self):
        manager = OpenCodeServerManager(binary="/old/opencode", port=4096, request_timeout_seconds=60)

        await manager.reload_runtime_config(
            binary="/new/opencode",
            port=4100,
            request_timeout_seconds=15,
        )

        self.assertEqual(manager.binary, "/new/opencode")
        self.assertEqual(manager.port, 4100)
        self.assertEqual(manager.request_timeout_seconds, 15)

    def test_apply_resource_governance_moves_managed_server_pid(self):
        calls = []
        governor = types.SimpleNamespace(
            apply_to_pid=lambda pid, label="agent": calls.append((pid, label)) or True
        )
        manager = OpenCodeServerManager(binary="opencode", port=4096, resource_governor=governor)

        manager._apply_resource_governance(1357)

        self.assertEqual(calls, [(1357, "opencode serve")])

    async def test_get_instance_attaches_resource_governor_when_existing_params_differ(self):
        first = OpenCodeServerManager(binary="/old/opencode", port=4096, request_timeout_seconds=60)
        governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        previous = OpenCodeServerManager._instance
        OpenCodeServerManager._instance = first

        try:
            manager = await OpenCodeServerManager.get_instance(
                binary="/new/opencode",
                port=4100,
                request_timeout_seconds=15,
                resource_governor=governor,
            )
        finally:
            OpenCodeServerManager._instance = previous

        self.assertIs(manager, first)
        self.assertIs(first.resource_governor, governor)
        self.assertEqual(first.binary, "/old/opencode")
        self.assertEqual(first.port, 4096)

    async def test_get_instance_preserves_controller_owned_resource_governor(self):
        from core.resource_governance import mark_controller_resource_governor

        controller_governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        ui_governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        mark_controller_resource_governor(controller_governor)
        first = OpenCodeServerManager(
            binary="opencode",
            port=4096,
            request_timeout_seconds=60,
            resource_governor=controller_governor,
        )
        previous = OpenCodeServerManager._instance
        OpenCodeServerManager._instance = first

        try:
            manager = await OpenCodeServerManager.get_instance(
                binary="opencode",
                port=4096,
                request_timeout_seconds=60,
                resource_governor=ui_governor,
            )
        finally:
            OpenCodeServerManager._instance = previous

        self.assertIs(manager, first)
        self.assertIs(first.resource_governor, controller_governor)

    async def test_get_instance_allows_controller_resource_governor_to_take_over(self):
        from core.resource_governance import mark_controller_resource_governor

        ui_governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        controller_governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        mark_controller_resource_governor(controller_governor)
        first = OpenCodeServerManager(
            binary="opencode",
            port=4096,
            request_timeout_seconds=60,
            resource_governor=ui_governor,
        )
        previous = OpenCodeServerManager._instance
        OpenCodeServerManager._instance = first

        try:
            manager = await OpenCodeServerManager.get_instance(
                binary="opencode",
                port=4096,
                request_timeout_seconds=60,
                resource_governor=controller_governor,
            )
        finally:
            OpenCodeServerManager._instance = previous

        self.assertIs(manager, first)
        self.assertIs(first.resource_governor, controller_governor)

    async def test_get_instance_if_managed_server_exists_allows_controller_governor_takeover(self):
        from core.resource_governance import mark_controller_resource_governor

        ui_governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        controller_governor = types.SimpleNamespace(apply_to_pid=lambda pid, label="agent": True)
        mark_controller_resource_governor(controller_governor)
        first = OpenCodeServerManager(
            binary="opencode",
            port=4096,
            request_timeout_seconds=60,
            resource_governor=ui_governor,
        )
        previous = OpenCodeServerManager._instance
        OpenCodeServerManager._instance = first

        try:
            manager = await OpenCodeServerManager.get_instance_if_managed_server_exists(
                binary="opencode",
                port=4096,
                request_timeout_seconds=60,
                resource_governor=controller_governor,
            )
        finally:
            OpenCodeServerManager._instance = previous

        self.assertIs(manager, first)
        self.assertIs(first.resource_governor, controller_governor)

    async def test_pending_detach_defers_runtime_reload_until_old_port_cleanup(self):
        manager = OpenCodeServerManager(binary="/old/opencode", port=4096, request_timeout_seconds=60)
        terminated = []
        manager._active_run_sessions.add("sess-1")
        manager._read_pid_file = lambda: {"pid": 654, "port": 4096}  # type: ignore[method-assign]
        manager._pid_exists = lambda pid: pid == 654  # type: ignore[method-assign]
        manager._get_pid_command = lambda pid: "opencode serve --port=4096"  # type: ignore[method-assign]
        manager._terminate_pid = lambda pid, reason: terminated.append((pid, reason)) or _async_none()  # type: ignore[method-assign]

        await manager.detach_after_deferred_refresh()
        await manager.reload_runtime_config(
            binary="/new/opencode",
            port=4100,
            request_timeout_seconds=15,
        )

        self.assertEqual(manager.binary, "/old/opencode")
        self.assertEqual(manager.port, 4096)
        self.assertEqual(manager.request_timeout_seconds, 60)

        manager._active_run_sessions.clear()
        await manager._restart_for_auth_refresh_locked()

        self.assertEqual(terminated, [(654, "auth refresh")])
        self.assertFalse(manager._auth_refresh_pending)
        self.assertIsNone(manager._auth_refresh_pending_port)
        self.assertEqual(manager.binary, "/new/opencode")
        self.assertEqual(manager.port, 4100)
        self.assertEqual(manager.request_timeout_seconds, 15)
        self.assertIsNone(manager._pending_runtime_config)

    async def test_close_http_session_skips_session_owned_by_another_loop(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()

        await manager.close_http_session(loop=asyncio.get_running_loop())

        self.assertFalse(fake_session.closed)
        self.assertIs(manager._http_session, fake_session)

    async def test_close_http_session_closes_session_for_matching_loop(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()
        current_loop = asyncio.get_running_loop()
        manager._http_session = fake_session
        manager._http_session_loop = current_loop

        await manager.close_http_session(loop=current_loop)

        self.assertTrue(fake_session.closed)
        self.assertIsNone(manager._http_session)

    async def test_get_instance_if_managed_server_exists_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            logs_dir = Path(tmp_dir)
            pid_file = logs_dir / "opencode_server.json"
            pid_file.write_text('{"pid": 654, "port": 4096}', encoding="utf-8")

            previous = OpenCodeServerManager._instance
            OpenCodeServerManager._instance = None
            try:
                with (
                    patch.object(SERVER_MODULE.paths, "get_logs_dir", return_value=logs_dir),
                    patch.object(SERVER_MODULE.runtime, "pid_alive", return_value=True),
                    patch.object(SERVER_MODULE.runtime, "get_process_command", return_value="python app.py"),
                ):
                    manager = await OpenCodeServerManager.get_instance_if_managed_server_exists(
                        binary="opencode",
                        port=4096,
                    )
            finally:
                OpenCodeServerManager._instance = previous

            self.assertIsNone(manager)

    async def test_set_api_key_auth_uses_official_auth_endpoint(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]
        manager.ensure_running = AsyncMock()  # type: ignore[method-assign]
        manager._base_url = "http://127.0.0.1:4096"

        await manager.set_api_key_auth("opencode", "sk-test-key")

        manager.ensure_running.assert_awaited_once()
        self.assertEqual(
            fake_session.puts,
            [
                {
                    "url": "http://127.0.0.1:4096/auth/opencode",
                    "json": {"type": "api", "key": "sk-test-key"},
                    "headers": None,
                }
            ],
        )

    def test_recent_session_error_summarizes_provider_failure_without_request_body(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            line_payload = {
                "error": {
                    "name": "AI_APICallError",
                    "cause": {
                        "code": "ECONNRESET",
                        "path": "https://user:secret@relay.example/messages?api_key=hidden",
                    },
                    "url": "https://relay.example/messages",
                    "requestBodyValues": {
                        "system": [{"text": "secret system prompt"}],
                        "apiKey": "sk-secret",
                    },
                }
            }
            (log_dir / "2026-06-19T040950.log").write_text(
                "INFO unrelated\n"
                + f"ERROR service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(line_payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync("ses_test")

        self.assertEqual(
            summary,
            "AI_APICallError (ECONNRESET) while calling https://relay.example/messages",
        )
        self.assertNotIn("secret system prompt", summary or "")
        self.assertNotIn("sk-secret", summary or "")

    def test_recent_session_error_redacts_freeform_error_message(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            line_payload = {
                "error": {
                    "name": "AI_APICallError",
                    "data": {
                        "message": (
                            "invalid api_key=sk-secret-123 at "
                            "https://relay.example/messages?api_key=sk-query-secret"
                        )
                    },
                }
            }
            (log_dir / "2026-06-19T040950.log").write_text(
                f"ERROR 2026-06-19T04:10:03 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(line_payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync("ses_test")

        self.assertIn("api_key=[redacted]", summary or "")
        self.assertIn("https://relay.example/messages", summary or "")
        self.assertNotIn("sk-secret", summary or "")
        self.assertNotIn("sk-query-secret", summary or "")

    def test_recent_session_error_extracts_typed_response_body(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            line_payload = {
                "error": {
                    "name": "AI_APICallError",
                    "url": "https://relay.example/v1/responses",
                    "statusCode": 404,
                    "responseBody": json.dumps(
                        {
                            "error": {
                                "message": (
                                    'Model "gpt-5.3-chat-latest" is not supported by '
                                    "any configured account in this group"
                                ),
                                "code": "model_not_found",
                                "type": "invalid_request_error",
                            }
                        }
                    ),
                }
            }
            (log_dir / "2026-07-20T064420.log").write_text(
                "ERROR 2026-07-20T06:48:14 +1ms service=llm "
                f"session.id=ses_test error={SERVER_MODULE.json.dumps(line_payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync("ses_test")

        self.assertEqual(
            summary,
            "AI_APICallError (model_not_found; invalid_request_error; HTTP 404) while calling "
            'https://relay.example/v1/responses: Model "gpt-5.3-chat-latest" is not '
            "supported by any configured account in this group",
        )

    def test_recent_session_error_reads_only_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            line_payload = {"error": {"name": "AI_APICallError", "cause": {"code": "ECONNRESET"}}}
            log_path = log_dir / "2026-06-19T040950.log"
            log_path.write_bytes(
                b"x" * (SERVER_MODULE.OPENCODE_LOG_TAIL_BYTES + 1024)
                + b"\n"
                + f"ERROR 2026-06-19T04:10:03 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(line_payload)} stream error\n".encode(
                    "utf-8"
                )
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with (
                patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]),
                patch.object(SERVER_MODULE.Path, "read_text", side_effect=AssertionError("must not read full log")),
            ):
                summary = manager._recent_session_error_sync("ses_test")

        self.assertEqual(summary, "AI_APICallError (ECONNRESET)")

    def test_recent_session_error_uses_current_prompt_window_and_strips_relative_query(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            stale_payload = {
                "error": {
                    "name": "AI_APICallError",
                    "cause": {
                        "code": "ECONNRESET",
                        "path": "/messages?api_key=stale-secret",
                    },
                }
            }
            current_payload = {
                "error": {
                    "name": "AI_APICallError",
                    "cause": {
                        "code": "ECONNRESET",
                        "path": "/messages?api_key=current-secret#frag",
                    },
                }
            }
            (log_dir / "2026-06-19T040950.log").write_text(
                f"ERROR 2026-06-19T04:09:49 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(stale_payload)} stream error\n"
                f"ERROR 2026-06-19T04:10:03 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(current_payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync(
                    "ses_test",
                    since=SERVER_MODULE.datetime(2026, 6, 19, 4, 10, 0).timestamp(),
                )

        self.assertEqual(
            summary,
            "AI_APICallError (ECONNRESET) while calling /messages",
        )
        self.assertNotIn("api_key", summary or "")
        self.assertNotIn("secret", summary or "")

    def test_recent_session_error_ignores_old_log_entries_for_current_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            payload = {
                "error": {
                    "name": "AI_APICallError",
                    "cause": {"code": "ECONNRESET", "path": "/messages?api_key=old-secret"},
                }
            }
            (log_dir / "2026-06-19T040950.log").write_text(
                f"ERROR 2026-06-19T04:09:49 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync(
                    "ses_test",
                    since=SERVER_MODULE.datetime(2026, 6, 19, 4, 10, 0).timestamp(),
                )

        self.assertIsNone(summary)

    def test_recent_session_error_ignores_pre_prompt_log_inside_short_window(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            payload = {
                "error": {
                    "name": "AI_APICallError",
                    "cause": {"code": "ECONNRESET", "path": "/messages?api_key=old-secret"},
                }
            }
            (log_dir / "2026-06-19T040950.log").write_text(
                f"ERROR 2026-06-19T04:09:59 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync(
                    "ses_test",
                    since=SERVER_MODULE.datetime(2026, 6, 19, 4, 10, 0).timestamp(),
                )

        self.assertIsNone(summary)

    def test_recent_session_error_keeps_same_second_current_prompt_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir)
            payload = {
                "error": {
                    "name": "AI_APICallError",
                    "cause": {"code": "ECONNRESET", "path": "/messages?api_key=current-secret"},
                }
            }
            (log_dir / "2026-06-19T041003.log").write_text(
                f"ERROR 2026-06-19T04:10:03 +1ms service=llm session.id=ses_test error={SERVER_MODULE.json.dumps(payload)} stream error\n",
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync(
                    "ses_test",
                    since=SERVER_MODULE.datetime(2026, 6, 19, 4, 10, 3, 500000).timestamp(),
                )

        self.assertEqual(summary, "AI_APICallError (ECONNRESET) while calling /messages")
        self.assertNotIn("current-secret", summary or "")

    async def test_prompt_async_records_prompt_start_time_for_log_correlation(self):
        manager = OpenCodeServerManager(binary="opencode", port=4096)
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        with patch.object(SERVER_MODULE.time, "time", return_value=1234.5):
            await manager.prompt_async(
                session_id="ses-1",
                directory="/tmp/work",
                text="hello",
            )

        self.assertEqual(manager.get_last_prompt_started_at("ses-1"), 1234.5)

    def test_provider_api_diagnostic_detects_html_base_url(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "glm": {
                                "npm": "@ai-sdk/anthropic",
                                "options": {
                                    "baseURL": "https://relay.example",
                                    "apiKey": "sk-secret",
                                },
                                "vibe_remote": {
                                    "custom": True,
                                    "adapter": "anthropic-compatible",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            class _UrlOpen:
                def __call__(self, request, timeout=None):
                    self.request = request
                    return _FakeUrlOpenResponse(
                        text="<!doctype html><html>Relay UI</html>",
                        headers={"content-type": "text/html; charset=utf-8"},
                    )

            fake_urlopen = _UrlOpen()
            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.urllib.request, "urlopen", fake_urlopen),
            ):
                detail = manager._provider_api_diagnostic_sync("glm", "glm-5.2")

        self.assertIn("returned an HTML page", detail or "")
        self.assertIn("https://relay.example/v1", detail or "")
        self.assertNotIn("sk-secret", detail or "")
        self.assertEqual(fake_urlopen.request.full_url, "https://relay.example/messages")

    def test_provider_api_diagnostic_reports_json_api_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "glm": {
                                "npm": "@ai-sdk/anthropic",
                                "options": {
                                    "baseURL": "https://relay.example/v1",
                                    "apiKey": "sk-secret",
                                },
                                "vibe_remote": {
                                    "custom": True,
                                    "adapter": "anthropic-compatible",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            def _raise_http_error(request, timeout=None):
                response = io.BytesIO(
                    b'{"error":{"message":"No available accounts: no available accounts","type":"api_error"}}'
                )
                raise SERVER_MODULE.urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "Service Unavailable",
                    {"content-type": "application/json; charset=utf-8"},
                    response,
                )

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.urllib.request, "urlopen", _raise_http_error),
            ):
                detail = manager._provider_api_diagnostic_sync("glm", "glm-5.2")

        self.assertEqual(
            detail,
            "Provider API returned HTTP 503: No available accounts: no available accounts",
        )
        self.assertNotIn("sk-secret", detail or "")

    def test_provider_api_diagnostic_reports_transport_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "glm": {
                                "npm": "@ai-sdk/anthropic",
                                "options": {
                                    "baseURL": "https://relay.example/v1?api_key=sk-query-secret",
                                    "apiKey": "sk-secret",
                                },
                                "vibe_remote": {
                                    "custom": True,
                                    "adapter": "anthropic-compatible",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            def _raise_url_error(request, timeout=None):
                raise SERVER_MODULE.urllib.error.URLError("timed out with api_key=sk-url-secret")

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.urllib.request, "urlopen", _raise_url_error),
            ):
                detail = manager._provider_api_diagnostic_sync("glm", "glm-5.2")

        self.assertIn("Provider API request failed", detail or "")
        self.assertIn("timed out", detail or "")
        self.assertNotIn("sk-secret", detail or "")
        self.assertNotIn("sk-url-secret", detail or "")
        self.assertNotIn("sk-query-secret", detail or "")

    def test_provider_api_diagnostic_redacts_json_api_error(self):
        payload = {
            "error": {
                "message": (
                    "bad Authorization: Bearer relay-token and "
                    "https://relay.example/messages?api_key=sk-query-secret"
                )
            }
        }

        detail = OpenCodeServerManager._diagnostic_payload_message(payload)

        self.assertIn("Bearer [redacted]", detail)
        self.assertIn("https://relay.example/messages", detail)
        self.assertNotIn("relay-token", detail)
        self.assertNotIn("sk-query-secret", detail)

    def test_provider_api_diagnostic_uses_auth_json_api_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            auth_path = tmp_home / ".local" / "share" / "opencode" / "auth.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "glm": {
                                "npm": "@ai-sdk/anthropic",
                                "options": {
                                    "baseURL": "https://relay.example/v1",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            auth_path.write_text('{"glm":{"type":"api","key":"sk-auth-json"}}', encoding="utf-8")
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            class _UrlOpen:
                def __call__(self, request, timeout=None):
                    self.request = request
                    return _FakeUrlOpenResponse(text='{"ok":true}', headers={"content-type": "application/json"})

            fake_urlopen = _UrlOpen()
            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.urllib.request, "urlopen", fake_urlopen),
            ):
                detail = manager._provider_api_diagnostic_sync("glm", "glm-5.2")

        self.assertIsNone(detail)
        self.assertEqual(fake_urlopen.request.headers.get("X-api-key"), "sk-auth-json")
        self.assertEqual(fake_urlopen.request.full_url, "https://relay.example/v1/messages")

    def test_provider_api_diagnostic_probes_builtin_anthropic_as_anthropic(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "anthropic": {
                                "options": {
                                    "baseURL": "https://relay.example/v1",
                                    "apiKey": "sk-secret",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)

            class _UrlOpen:
                def __call__(self, request, timeout=None):
                    self.request = request
                    return _FakeUrlOpenResponse(text='{"ok":true}', headers={"content-type": "application/json"})

            fake_urlopen = _UrlOpen()
            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.urllib.request, "urlopen", fake_urlopen),
            ):
                detail = manager._provider_api_diagnostic_sync("anthropic", "claude-opus-4")

        self.assertIsNone(detail)
        self.assertEqual(fake_urlopen.request.full_url, "https://relay.example/v1/messages")
        self.assertEqual(fake_urlopen.request.headers.get("X-api-key"), "sk-secret")
        self.assertNotIn("Authorization", fake_urlopen.request.headers)

    def test_provider_api_diagnostic_skips_unsupported_reserved_provider(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            config_path = tmp_home / ".config" / "opencode" / "opencode.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "google": {
                                "options": {
                                    "baseURL": "https://generativelanguage.googleapis.com/v1beta",
                                    "apiKey": "sk-secret",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenCodeServerManager(binary="opencode", port=4096)
            calls = []

            def _unexpected_urlopen(request, timeout=None):
                calls.append(request.full_url)
                raise AssertionError(f"unexpected diagnostic request to {request.full_url}")

            with (
                patch("vibe.opencode_config.Path.home", return_value=tmp_home),
                patch.object(SERVER_MODULE.urllib.request, "urlopen", _unexpected_urlopen),
            ):
                detail = manager._provider_api_diagnostic_sync("google", "gemini-2.5-pro")

        self.assertIsNone(detail)
        self.assertEqual(calls, [])


async def _async_none():
    return None


def test_mh_runtime_002_matching_overlay_does_not_wait_for_an_unrelated_active_run():
    """MH-RUNTIME-002: unchanged direct mode cannot head-of-line block another Session."""

    manager = OpenCodeServerManager(binary="opencode", port=4096)
    manager._active_run_sessions.add("sess-unrelated")
    manager._read_pid_file = lambda: {  # type: ignore[method-assign]
        "pid": 321,
        "port": 4096,
        "active_run_sessions": ["sess-unrelated"],
    }
    manager._pid_file_references_current_server = Mock(return_value=True)  # type: ignore[method-assign]
    manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]

    reservation = asyncio.run(
        asyncio.wait_for(
            manager.configure_model_hub_overlay(None),
            timeout=0.1,
        )
    )
    asyncio.run(manager.release_model_hub_overlay_reservation(reservation))

    assert manager._model_hub_overlay_path is None
    assert manager._model_hub_overlay_hash is None
    assert manager._model_hub_overlay_content is None
    manager._is_healthy.assert_not_awaited()
    manager._restart_for_auth_refresh_locked.assert_not_awaited()


def test_changed_overlay_still_waits_for_active_run_before_restart():
    manager = OpenCodeServerManager(binary="opencode", port=4096)
    manager._model_hub_overlay_path = "/tmp/old-overlay.json"
    manager._model_hub_overlay_hash = "old-hash"
    manager._active_run_sessions.add("sess-active")
    manager._read_pid_file = lambda: {}  # type: ignore[method-assign]
    manager._pid_file_references_current_server = Mock(return_value=False)  # type: ignore[method-assign]
    manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
    overlay = _model_hub_overlay("/tmp/new-overlay.json", "new-model")

    async def exercise():
        configuring = asyncio.create_task(manager.configure_model_hub_overlay(overlay))
        await asyncio.sleep(0.01)
        assert not configuring.done()
        manager._active_run_sessions.clear()
        reservation = await asyncio.wait_for(configuring, timeout=0.2)
        await manager.release_model_hub_overlay_reservation(reservation)

    asyncio.run(exercise())

    assert manager._model_hub_overlay_path == str(overlay.path)
    assert manager._model_hub_overlay_hash == overlay.content_hash
    manager._restart_for_auth_refresh_locked.assert_awaited_once()


def test_empty_model_hub_overlay_uses_the_normal_change_path():
    manager = OpenCodeServerManager(binary="opencode", port=4096)
    manager._model_hub_overlay_path = "/tmp/old-overlay.json"
    manager._model_hub_overlay_hash = "old-hash"
    manager._read_pid_file = lambda: {}  # type: ignore[method-assign]
    manager._pid_file_references_current_server = Mock(return_value=False)  # type: ignore[method-assign]
    manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
    overlay = _model_hub_overlay("/tmp/empty-overlay.json", None)

    reservation = asyncio.run(manager.configure_model_hub_overlay(overlay))
    asyncio.run(manager.release_model_hub_overlay_reservation(reservation))

    assert json.loads(overlay.content)["provider"]["avibe-openai"]["models"] == {}
    assert manager._model_hub_overlay_path == str(overlay.path)
    assert manager._model_hub_overlay_hash == overlay.content_hash
    manager._restart_for_auth_refresh_locked.assert_awaited_once_with(
        native_turns_drained=True,
    )


def test_changed_overlay_passes_completed_persisted_drain_to_retirement():
    manager = OpenCodeServerManager(binary="opencode", port=4096)
    manager._model_hub_overlay_path = "/tmp/old-overlay.json"
    manager._model_hub_overlay_hash = "old-hash"
    manager._model_hub_overlay_drain_timeout_seconds = 0
    manager._read_pid_file = lambda: {  # type: ignore[method-assign]
        "pid": 321,
        "port": 4096,
        "active_run_sessions": ["sess-stale"],
    }
    manager._pid_file_references_current_server = Mock(return_value=True)  # type: ignore[method-assign]
    manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
    overlay = _model_hub_overlay("/tmp/new-overlay.json", "new-model")

    reservation = asyncio.run(manager.configure_model_hub_overlay(overlay))
    asyncio.run(manager.release_model_hub_overlay_reservation(reservation))

    manager._restart_for_auth_refresh_locked.assert_awaited_once_with(
        native_turns_drained=True,
    )


def test_mh_runtime_003_pending_overlay_transition_blocks_new_turns_on_the_old_overlay():
    """MH-RUNTIME-003: a queued change drains without old-overlay starvation."""

    old_overlay = _model_hub_overlay("/tmp/old-overlay.json", "old-model")
    new_overlay = _model_hub_overlay("/tmp/new-overlay.json", "new-model")
    manager = OpenCodeServerManager(binary="opencode", port=4096)
    manager._model_hub_overlay_path = "/tmp/old-overlay.json"
    manager._model_hub_overlay_hash = old_overlay.content_hash
    manager._active_run_sessions.add("sess-active")
    manager._read_pid_file = lambda: {}  # type: ignore[method-assign]
    manager._pid_file_references_current_server = Mock(return_value=False)  # type: ignore[method-assign]
    manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
    async def exercise():
        configuring = asyncio.create_task(
            manager.configure_model_hub_overlay(new_overlay)
        )
        await asyncio.sleep(0.01)
        matching_old_turn = asyncio.create_task(
            manager.configure_model_hub_overlay(old_overlay)
        )
        await asyncio.sleep(0.01)
        assert not matching_old_turn.done()
        matching_old_turn.cancel()
        await asyncio.gather(matching_old_turn, return_exceptions=True)
        manager._active_run_sessions.clear()
        reservation = await asyncio.wait_for(configuring, timeout=0.2)
        await manager.release_model_hub_overlay_reservation(reservation)

    asyncio.run(exercise())

    assert manager._model_hub_overlay_path == str(new_overlay.path)
    assert manager._model_hub_overlay_hash == new_overlay.content_hash
    assert manager._model_hub_overlay_transition is None
    manager._restart_for_auth_refresh_locked.assert_awaited_once()


def test_mh_runtime_004_overlay_reservation_promotes_atomically_to_active_run():
    """MH-RUNTIME-004: selection stays leased through active-run registration."""

    old_overlay = _model_hub_overlay("/tmp/old-overlay.json", "old-model")
    new_overlay = _model_hub_overlay("/tmp/new-overlay.json", "new-model")
    manager = OpenCodeServerManager(binary="opencode", port=4096)
    manager._model_hub_overlay_path = "/tmp/old-overlay.json"
    manager._model_hub_overlay_hash = old_overlay.content_hash
    manager._read_pid_file = lambda: {}  # type: ignore[method-assign]
    manager._pid_file_references_current_server = Mock(return_value=False)  # type: ignore[method-assign]
    manager._is_healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._restart_for_auth_refresh_locked = AsyncMock()  # type: ignore[method-assign]
    async def exercise():
        reservation = await manager.configure_model_hub_overlay(new_overlay)
        reverting = asyncio.create_task(
            manager.configure_model_hub_overlay(old_overlay)
        )
        await asyncio.sleep(0.01)
        assert not reverting.done()

        await manager.mark_run_active(
            "sess-new",
            overlay_reservation=reservation,
        )
        await asyncio.sleep(0.01)
        assert not reverting.done()

        await manager.mark_run_inactive("sess-new")
        old_reservation = await asyncio.wait_for(reverting, timeout=0.2)
        await manager.release_model_hub_overlay_reservation(old_reservation)

    asyncio.run(exercise())

    assert manager._model_hub_overlay_path == str(old_overlay.path)
    assert manager._model_hub_overlay_hash == old_overlay.content_hash
    assert manager._model_hub_overlay_reservations == {}
    assert manager._active_run_sessions == set()
    assert manager._restart_for_auth_refresh_locked.await_count == 2


if __name__ == "__main__":
    unittest.main()
