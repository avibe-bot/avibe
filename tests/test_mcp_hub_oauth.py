"""Hermetic MCP Hub OAuth contracts owned by the runtime/storage boundary."""

import asyncio
import json

import pytest

from core.mcp_hub import oauth, repository
from core.mcp_hub.definition import definition_from_provider
from core.mcp_hub.prompt import build_mcp_prompt_blocks
from core.mcp_hub.runtime import McpRuntime
from tests.ui_server_test_helpers import save_config


def test_redirect_mode_obeys_oauth_loopback_rule():
    cases = [
        ("http://127.0.0.1:5123", "direct", "http://127.0.0.1:5123/api/mcp/oauth/callback"),
        ("http://localhost:5123", "direct", "http://localhost:5123/api/mcp/oauth/callback"),
        ("https://cloud.example", "direct", "https://cloud.example/api/mcp/oauth/callback"),
        ("http://192.168.1.20:5123", "paste", "http://127.0.0.1:5123/api/mcp/oauth/callback"),
        ("http://host.lan:5123", "paste", "http://127.0.0.1:5123/api/mcp/oauth/callback"),
        (None, "paste", "http://127.0.0.1:5123/api/mcp/oauth/callback"),
    ]
    for origin, mode, redirect in cases:
        assert oauth.redirect_mode(origin) == (mode, redirect)


def test_definition_accepts_only_phase_one_oauth_options():
    definition = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"clientId": "ignored", "scopes": "offline_access", "secret": "ignored"}})
    assert definition["oauth"] == {"clientId": "ignored", "scopes": "offline_access"}


def test_oauth_storage_is_url_bound_and_private(tmp_path, monkeypatch):
    config = save_config(tmp_path)
    config.save()
    storage = oauth.AvibeTokenStorage("suno", "https://api.example/mcp")
    from mcp.shared.auth import OAuthToken

    asyncio.run(storage.set_tokens(OAuthToken(access_token="secret", refresh_token="refresh", expires_in=60)))
    path = oauth.oauth_path("suno")
    assert path.stat().st_mode & 0o777 == 0o600
    assert storage.raw()["serverUrl"] == "https://api.example/mcp"
    assert oauth.AvibeTokenStorage("suno", "https://changed.example/mcp").raw() == {}


def test_needs_login_is_listed_but_omitted_from_prompt(tmp_path):
    config = save_config(tmp_path)
    config.save()
    definition = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"scopes": "offline_access"}})
    repository.write_definition(definition)
    repository.publish("suno", definition=definition, manifest={"description": "", "auth": "needs_login"}, tools=[], commands=[])
    assert repository.list_packages()[0]["auth"] == "needs_login"
    assert "suno" not in "".join(block.text for block in build_mcp_prompt_blocks())


def test_callback_state_is_single_use_on_runtime_loop(tmp_path):
    config = save_config(tmp_path)
    config.save()
    runtime = McpRuntime()
    try:
        async def exercise():
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            runtime._pending_logins["state"] = {"callback": future}
            first = json.loads(await runtime._oauth_callback({"state": "state", "code": "code"}))
            assert first["ok"]
            with pytest.raises(Exception, match="unknown or already used"):
                await runtime._oauth_callback({"state": "state", "code": "again"})

        asyncio.run(exercise())
    finally:
        runtime.stop()


def test_callback_url_validation_rejects_wrong_path_or_missing_code():
    with pytest.raises(Exception, match="not an Avibe"):
        oauth.parse_callback_url("https://example.test/wrong?code=x&state=y")
    with pytest.raises(Exception, match="missing OAuth"):
        oauth.parse_callback_url("https://example.test/api/mcp/oauth/callback?state=y")
