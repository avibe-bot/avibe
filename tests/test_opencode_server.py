import asyncio
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psutil
import pytest

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
        # Dataclasses resolve their annotations through the module registry.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_aiohttp is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = previous_aiohttp


SERVER_MODULE = _load_server_module()
OpenCodeRuntimeConfigInvalidError = SERVER_MODULE.OpenCodeRuntimeConfigInvalidError
OpenCodeServerClient = SERVER_MODULE.OpenCodeServerClient


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
    def setUp(self):
        from core.services.settings import default_config

        config_home = self.enterContext(tempfile.TemporaryDirectory(prefix="opencode-server-test-"))
        self.enterContext(patch.dict(os.environ, {"AVIBE_HOME": config_home}))
        config = default_config()
        # These server lifecycle cases exercise native Direct launches.
        # Hub-specific cases explicitly supply the Controller-owned overlay.
        config.model_hub.agents["opencode"].mode = "direct"
        config.save()

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

        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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

        manager = OpenCodeServerClient(
            "http://127.0.0.1:4096",
            model_hub_provider_ids=runtime_ids,
        )
        session = _CatalogSession()
        manager._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

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

    async def test_prompt_async_percent_encodes_directory_header(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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

        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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

        manager = OpenCodeServerClient("http://127.0.0.1:4096")
        fake_session = _HealthSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        with patch.object(SERVER_MODULE.aiohttp, "ClientTimeout", return_value=object()):
            version = await manager.get_version()

        self.assertEqual(version, "1.18.5")
        self.assertEqual(fake_session.gets[0]["url"], "http://127.0.0.1:4096/global/health")

    async def test_prompt_async_includes_tools_when_provided(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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

            manager = OpenCodeServerClient("http://127.0.0.1:4096")
            with patch("vibe.opencode_config.Path.home", return_value=tmp_home):
                config = manager._load_opencode_user_config()

            self.assertEqual(
                config,
                {
                    "model": "openai/gpt-5",
                    "reasoningEffort": "high",
                },
            )

    async def test_explicit_subagent_model_is_independent_of_native_defaults(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
        for native_model in (None, "openai/native-one", "anthropic/native-two"):
            for declared in (None, "", "   ", {"id": "invalid"}, " provider/reviewer "):
                with self.subTest(native_model=native_model, declared=declared):
                    config = {
                        "model": native_model,
                        "agent": {
                            "build": {"model": native_model},
                            "reviewer": {"model": declared},
                        },
                    }
                    with patch.object(manager, "_load_opencode_user_config", return_value=config):
                        expected = (declared.strip() or None) if isinstance(declared, str) else None
                        self.assertEqual(manager.get_explicit_subagent_model("reviewer"), expected)
                        self.assertIsNone(manager.get_explicit_subagent_model("missing"))
                        self.assertIsNone(manager.get_explicit_subagent_model(""))

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

                    manager = OpenCodeServerClient("http://127.0.0.1:4096")
                    with patch("vibe.opencode_config.Path.home", return_value=tmp_home):
                        resolved = manager.get_agent_reasoning_effort_from_config(None)

                    self.assertEqual(resolved, effort)

    async def test_close_http_session_skips_session_owned_by_another_loop(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
        fake_session = _FakeSession()
        manager._http_session = fake_session
        manager._http_session_loop = object()

        await manager.close_http_session(loop=asyncio.get_running_loop())

        self.assertFalse(fake_session.closed)
        self.assertIs(manager._http_session, fake_session)

    async def test_close_http_session_closes_session_for_matching_loop(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
        fake_session = _FakeSession()
        current_loop = asyncio.get_running_loop()
        manager._http_session = fake_session
        manager._http_session_loop = current_loop

        await manager.close_http_session(loop=current_loop)

        self.assertTrue(fake_session.closed)
        self.assertIsNone(manager._http_session)

    async def test_set_api_key_auth_uses_official_auth_endpoint(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
        fake_session = _FakeSession()

        async def _fake_get_http_session():
            return fake_session

        manager._get_http_session = _fake_get_http_session  # type: ignore[method-assign]

        await manager.set_api_key_auth("opencode", "sk-test-key")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

            with patch.object(manager, "_opencode_log_dirs", return_value=[log_dir]):
                summary = manager._recent_session_error_sync(
                    "ses_test",
                    since=SERVER_MODULE.datetime(2026, 6, 19, 4, 10, 3, 500000).timestamp(),
                )

        self.assertEqual(summary, "AI_APICallError (ECONNRESET) while calling /messages")
        self.assertNotIn("current-secret", summary or "")

    async def test_prompt_async_records_prompt_start_time_for_log_correlation(self):
        manager = OpenCodeServerClient("http://127.0.0.1:4096")
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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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

        detail = OpenCodeServerClient._diagnostic_payload_message(payload)

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")

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
            manager = OpenCodeServerClient("http://127.0.0.1:4096")
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


# Starts its tool the way OpenCode's shell tool does (``detached``, so setsid):
# the tool leads a new session and process group, out of the server's reach.
_SERVER_WITH_DETACHED_TOOL = """
import subprocess, sys
tool = subprocess.Popen(["/bin/sh", "-c", sys.argv[1], "tool", sys.argv[2]], start_new_session=True)
print(tool.pid, flush=True)
tool.wait()
"""
# Records SIGTERM and keeps going, starting a new child each second, so only
# SIGKILL ends it and some of its children postdate any single walk.
_TERM_SURVIVING_TOOL = 'trap \'echo TERM > "$1"\' TERM; while :; do sleep 1; done'


def _exited(process: psutil.Process) -> bool:
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _running_group_members(pgid: int) -> list[int]:
    members = []
    for process in psutil.process_iter():
        with suppress(OSError, psutil.Error):
            if os.getpgid(process.pid) == pgid and not _exited(process):
                members.append(process.pid)
    return members


@pytest.mark.skipif(os.name == "nt", reason="POSIX sessions and process groups")
def test_stopping_the_server_stops_tool_commands_in_their_own_session(tmp_path):
    term_marker = tmp_path / "tool-received-sigterm"
    # Avibe starts the server as the leader of its own session, as here.
    server = subprocess.Popen(
        [sys.executable, "-c", _SERVER_WITH_DETACHED_TOOL, _TERM_SURVIVING_TOOL, str(term_marker)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    bystander = subprocess.Popen(["/bin/sh", "-c", "while :; do sleep 1; done"], start_new_session=True)
    tool_pid = None
    try:
        tool_pid = int(server.stdout.readline())
        tool = psutil.Process(tool_pid)
        assert os.getsid(tool_pid) == tool_pid != os.getsid(server.pid)
        # Reaps the server the moment it exits, as Avibe's asyncio child watcher does.
        threading.Thread(target=server.wait, daemon=True).start()

        assert SERVER_MODULE.terminate_pid_tree_sync(server.pid, timeout=2.0)

        assert _exited(tool)
        assert _running_group_members(tool_pid) == []
        # SIGTERM came first, and only then SIGKILL.
        assert term_marker.read_text().strip() == "TERM"
        assert server.wait(timeout=5) == -signal.SIGTERM
        assert bystander.poll() is None
    finally:
        if tool_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(tool_pid, signal.SIGKILL)
        for process in (server, bystander):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        server.stdout.close()


# A server whose tools run in their own sessions, as OpenCode's do: one keeps
# working, the other stops the server from inside the tree, as `vibe stop` run
# from an OpenCode shell tool does.
_SERVER_WITH_STOPPING_TOOL = """
import os, subprocess, sys
marker, done = sys.argv[1], sys.argv[2]
worker = subprocess.Popen(["/bin/sh", "-c", "while :; do sleep 1; done", "tool", marker], start_new_session=True)
print(worker.pid, flush=True)
stopper = (
    "import sys\\n"
    "from modules.agents.opencode.server import terminate_pid_tree_sync\\n"
    "stopped = terminate_pid_tree_sync(int(sys.argv[1]), timeout=2.0)\\n"
    "open(sys.argv[2], 'w').write(str(stopped))\\n"
)
subprocess.Popen([sys.executable, "-c", stopper, str(os.getpid()), done, marker], start_new_session=True)
worker.wait()
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX sessions and process groups")
def test_a_stop_run_from_inside_the_tree_survives_to_finish_it(tmp_path):
    # Every tree member outside the caller's group was signalled one by one, so
    # a stop running inside the tree killed itself before it finished.
    done = tmp_path / "stop-finished"
    server = subprocess.Popen(
        [sys.executable, "-c", _SERVER_WITH_STOPPING_TOOL, str(tmp_path / "marker"), str(done)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    worker_pid = None
    try:
        worker_pid = int(server.stdout.readline())
        worker = psutil.Process(worker_pid)
        threading.Thread(target=server.wait, daemon=True).start()
        deadline = time.monotonic() + 20
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert done.read_text() == "True"
        assert _exited(worker)
        assert server.wait(timeout=5) == -signal.SIGTERM
    finally:
        if worker_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(worker_pid, signal.SIGKILL)
        if server.poll() is None:
            server.kill()
        server.wait(timeout=5)
        server.stdout.close()


if __name__ == "__main__":
    unittest.main()
