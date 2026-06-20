import asyncio
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
OpenCodeServerManager = SERVER_MODULE.OpenCodeServerManager


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


if __name__ == "__main__":
    unittest.main()
