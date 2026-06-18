"""Contract tests for ``save_opencode_provider_auth`` payload parsing.

These pin the three-state semantics for the optional ``base_url`` field
that Codex flagged in PR #282 round 3:

  * key absent             → leave the stored value untouched
  * key present + blank    → clear the stored value
  * key present + non-blank → upsert (after http(s):// validation)

Without these, re-saving just the API key would silently wipe the
``baseURL`` override.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from vibe import api


class _FakeServer:
    """Stand-in for the OpenCode HTTP daemon used by the save flow.

    The real ``set_api_key_auth`` PUTs to the OpenCode HTTP server; we
    only need it to succeed so the JSON-write side effects fire.
    """

    def __init__(self, home: Path | None = None) -> None:
        self.set_calls: List[Tuple[str, str]] = []
        self.remove_calls: List[str] = []
        self.closes: int = 0
        self.remove_error: Exception | None = None
        self.home = home

    def _auth_path(self) -> Path | None:
        if self.home is None:
            return None
        return self.home / ".local" / "share" / "opencode" / "auth.json"

    def _read_auth(self) -> dict:
        path = self._auth_path()
        if path is None or not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_auth(self, data: dict) -> None:
        path = self._auth_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    async def set_api_key_auth(self, provider_id: str, api_key: str) -> None:
        self.set_calls.append((provider_id, api_key))
        auth = self._read_auth()
        auth[provider_id] = {"type": "api", "key": api_key}
        self._write_auth(auth)

    async def remove_provider_auth(self, provider_id: str) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.remove_calls.append(provider_id)
        auth = self._read_auth()
        auth.pop(provider_id, None)
        self._write_auth(auth)

    async def get_providers(self):
        return {"all": [{"id": "openai", "name": "OpenAI"}], "connected": ["openai"]}

    async def get_provider_auth(self):
        return {}

    async def get_available_models(self, directory):
        return {"providers": [{"id": "openai", "models": {"gpt-5": {}}}]}

    async def get_available_agents(self, directory):
        return []

    async def get_default_config(self, directory):
        return {}

    async def close_http_session(self, loop) -> None:  # type: ignore[override]
        self.closes += 1


class _FakeModelServer:
    def __init__(self, models=None) -> None:
        self.models = models or {
            "providers": [
                {
                    "id": "deepseek",
                    "models": {
                        "deepseek-chat": {},
                    },
                }
            ]
        }
        self.models_error: Exception | None = None

    async def get_available_models(self, directory):
        if self.models_error is not None:
            raise self.models_error
        return self.models

    async def close_http_session(self, loop) -> None:  # type: ignore[override]
        pass


@pytest.fixture()
def fake_save_env(monkeypatch, tmp_path):
    """Wire ``save_opencode_provider_auth`` to a temp HOME + fake server."""
    server = _FakeServer(tmp_path)

    async def _fake_get_server():
        return server

    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "restart_backend", lambda backend: {"ok": True})
    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    return server, tmp_path


@pytest.fixture()
def fake_model_env(monkeypatch, tmp_path):
    server = _FakeModelServer()

    async def _fake_get_server():
        return server

    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "restart_backend", lambda backend: {"ok": True})
    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {"x": {"data": {}, "updated_at": 1}})
    return server, tmp_path


def _save(provider_id: str, payload: dict) -> dict:
    return api.save_opencode_provider_auth(provider_id, payload)


def _read_opencode_config(home: Path) -> dict:
    from vibe.opencode_config import get_opencode_config_paths

    path = get_opencode_config_paths(home)[0]
    return json.loads(path.read_text(encoding="utf-8"))


def _save_model(provider_id: str, payload: dict) -> dict:
    return asyncio.run(api.save_opencode_provider_model_async(provider_id, payload))


def _delete_model(provider_id: str, model_id: str) -> dict:
    return asyncio.run(api.delete_opencode_provider_model_async(provider_id, model_id))


def _save_custom(payload: dict) -> dict:
    return asyncio.run(api.save_opencode_custom_provider_async(payload))


def _delete_custom(provider_id: str) -> dict:
    return asyncio.run(api.delete_opencode_custom_provider_async(provider_id))


def test_base_url_absent_leaves_existing_value_untouched(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_provider_base_url,
    )

    server, home = fake_save_env
    upsert_opencode_provider_base_url("openai", "https://existing.example", home=home)

    # Caller re-saves just the api_key: this used to wipe baseURL because
    # the server treated "absent" the same as "empty".
    result = _save("openai", {"api_key": "sk-new"})
    assert result.get("ok") is True
    assert server.set_calls == []
    config = _read_opencode_config(home)
    assert config["provider"]["openai"]["options"]["apiKey"] == "sk-new"
    assert (
        read_opencode_provider_base_url("openai", home=home)
        == "https://existing.example"
    )


def test_save_provider_auth_persists_api_key_in_provider_options(fake_save_env) -> None:
    """Keys entered in Vibe Remote are OpenCode provider options."""

    server, home = fake_save_env

    result = _save("deepseek", {"api_key": "sk-deepseek"})

    assert result.get("ok") is True
    assert server.set_calls == []
    config = _read_opencode_config(home)
    assert config["provider"]["deepseek"]["options"]["apiKey"] == "sk-deepseek"


def test_save_provider_auth_cleans_stale_api_auth_entry(fake_save_env) -> None:
    from vibe.opencode_config import read_opencode_provider_auth_entries

    server, home = fake_save_env
    server._write_auth({"deepseek": {"type": "api", "key": "sk-stale"}})

    result = _save("deepseek", {"api_key": "sk-deepseek"})

    assert result.get("ok") is True
    assert server.remove_calls == ["deepseek"]
    assert read_opencode_provider_auth_entries(home=home) == {}
    config = _read_opencode_config(home)
    assert config["provider"]["deepseek"]["options"]["apiKey"] == "sk-deepseek"


def test_save_provider_auth_replaces_oauth_auth_entry(fake_save_env) -> None:
    from vibe.opencode_config import read_opencode_provider_auth_entries

    server, home = fake_save_env
    server._write_auth({"openai": {"type": "oauth", "access_token": "old-token"}})

    result = _save("openai", {"api_key": "sk-openai"})

    assert result.get("ok") is True
    assert server.remove_calls == ["openai"]
    assert read_opencode_provider_auth_entries(home=home) == {}
    config = _read_opencode_config(home)
    assert config["provider"]["openai"]["options"]["apiKey"] == "sk-openai"


def test_save_provider_auth_returns_refreshed_catalog_and_clears_options_cache(
    fake_save_env, monkeypatch
) -> None:
    _server, _home = fake_save_env
    api._OPENCODE_OPTIONS_CACHE["/tmp/workspace"] = {"data": {"stale": True}, "updated_at": 1}
    calls = []

    async def _fake_refresh(provider_id: str) -> dict:
        calls.append(provider_id)
        return {
            "ok": True,
            "provider_id": provider_id,
            "catalog": {
                "ok": True,
                "providers": [
                    {
                        "id": "deepseek",
                        "models": ["deepseek-chat", "deepseek-reasoner"],
                    }
                ],
            },
        }

    monkeypatch.setattr(api, "_refresh_opencode_provider_catalog_async", _fake_refresh)

    result = _save("deepseek", {"api_key": "sk-deepseek"})

    assert result["ok"] is True
    assert api._OPENCODE_OPTIONS_CACHE == {}
    assert calls == ["deepseek"]
    assert result["catalog_refresh"]["catalog"]["providers"][0]["models"] == [
        "deepseek-chat",
        "deepseek-reasoner",
    ]


def test_save_provider_auth_still_succeeds_when_catalog_refresh_lags(
    fake_save_env, monkeypatch
) -> None:
    async def _fake_refresh(provider_id: str) -> dict:
        return {
            "ok": False,
            "provider_id": provider_id,
            "message": "Provider saved, but model catalog has not refreshed yet",
            "catalog": {"ok": True, "providers": [{"id": provider_id, "models": []}]},
        }

    monkeypatch.setattr(api, "_refresh_opencode_provider_catalog_async", _fake_refresh)

    result = _save("deepseek", {"api_key": "sk-deepseek"})

    assert result["ok"] is True
    assert result["catalog_refresh"]["ok"] is False
    assert "not refreshed" in result["catalog_refresh"]["message"]


def test_save_provider_auth_clears_options_cache(fake_save_env) -> None:
    api._OPENCODE_OPTIONS_CACHE["/repo"] = {"data": {"stale": True}, "updated_at": 1}

    result = _save("openai", {"api_key": "sk-new"})

    assert result.get("ok") is True
    assert api._OPENCODE_OPTIONS_CACHE == {}


def test_base_url_only_save_accepts_opencode_json_options_key(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_provider_api_key,
    )

    server, home = fake_save_env
    upsert_opencode_provider_api_key("poe", "sk-legacy", home=home)

    result = _save("poe", {"base_url": "https://poe-relay.example/v1"})

    assert result["ok"] is True
    assert server.set_calls == []
    assert read_opencode_provider_base_url("poe", home=home) == "https://poe-relay.example/v1"


def test_base_url_only_save_repairs_missing_provider_options_key(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        read_opencode_provider_auth_entries,
    )

    server, home = fake_save_env
    # Simulate a provider saved by the older path: OpenCode auth.json has
    # the key, but opencode.json is missing provider.<id>.options.apiKey,
    # so some providers fail at invocation time with 401.
    server._write_auth({"deepseek": {"type": "api", "key": "sk-deepseek"}})

    result = _save("deepseek", {"base_url": "https://api.deepseek.com"})

    assert result["ok"] is True
    assert server.set_calls == []
    assert server.remove_calls == ["deepseek"]
    assert read_opencode_provider_auth_entries(home=home) == {}
    assert read_opencode_provider_base_url("deepseek", home=home) == "https://api.deepseek.com"
    config = _read_opencode_config(home)
    assert config["provider"]["deepseek"]["options"]["apiKey"] == "sk-deepseek"


def test_base_url_only_save_keeps_existing_config_key_over_auth_key(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        read_opencode_provider_auth_entries,
        upsert_opencode_provider_api_key,
    )

    server, home = fake_save_env
    upsert_opencode_provider_api_key("deepseek", "sk-config-active", home=home)
    server._write_auth({"deepseek": {"type": "api", "key": "sk-stale-auth"}})

    result = _save("deepseek", {"base_url": "https://api.deepseek.com"})

    assert result["ok"] is True
    assert server.set_calls == []
    assert server.remove_calls == ["deepseek"]
    assert read_opencode_provider_auth_entries(home=home) == {}
    assert read_opencode_provider_base_url("deepseek", home=home) == "https://api.deepseek.com"
    config = _read_opencode_config(home)
    assert config["provider"]["deepseek"]["options"]["apiKey"] == "sk-config-active"


def test_base_url_only_save_accepts_keyless_custom_provider(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_custom_provider,
    )

    server, home = fake_save_env
    upsert_opencode_custom_provider(
        "llama.cpp",
        "llama.cpp",
        "openai-compatible",
        "http://127.0.0.1:8080/v1",
        home=home,
    )

    result = _save("llama.cpp", {"base_url": "http://127.0.0.1:8081/v1"})

    assert result["ok"] is True
    assert server.set_calls == []
    assert (
        read_opencode_provider_base_url("llama.cpp", home=home)
        == "http://127.0.0.1:8081/v1"
    )


def test_delete_provider_auth_removes_opencode_json_options_key(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        read_opencode_provider_keys,
        upsert_opencode_provider_api_key,
        upsert_opencode_provider_base_url,
    )

    server, home = fake_save_env
    upsert_opencode_provider_api_key("poe", "sk-legacy", home=home)
    upsert_opencode_provider_base_url("poe", "https://poe-relay.example/v1", home=home)

    result = api.delete_opencode_provider_auth("poe")

    assert result["ok"] is True
    assert server.remove_calls == []
    assert "poe" not in read_opencode_provider_keys(home=home)
    assert read_opencode_provider_base_url("poe", home=home) == "https://poe-relay.example/v1"


def test_delete_provider_auth_clears_options_cache(fake_save_env) -> None:
    from vibe.opencode_config import upsert_opencode_provider_api_key

    _server, home = fake_save_env
    upsert_opencode_provider_api_key("poe", "sk-legacy", home=home)
    api._OPENCODE_OPTIONS_CACHE["/repo"] = {"data": {"stale": True}, "updated_at": 1}

    result = api.delete_opencode_provider_auth("poe")

    assert result["ok"] is True
    assert api._OPENCODE_OPTIONS_CACHE == {}


def test_delete_provider_auth_noop_keeps_matching_default_provider(fake_save_env) -> None:
    from config.v2_config import V2Config

    server, home = fake_save_env
    config = V2Config.from_payload(
        {
            "mode": "self_host",
            "version": "2",
            "platform": "avibe",
            "platforms": {"enabled": ["avibe"], "primary": "avibe"},
            "runtime": {"default_cwd": str(home), "log_level": "INFO"},
            "agents": {
                "default_backend": "opencode",
                "opencode": {"enabled": True, "default_provider": "llama.cpp"},
                "claude": {"enabled": False},
                "codex": {"enabled": False},
            },
        }
    )
    config.save()

    result = api.delete_opencode_provider_auth("llama.cpp")

    assert result["ok"] is True
    assert server.remove_calls == []
    assert V2Config.load().agents.opencode.default_provider == "llama.cpp"


def test_save_provider_model_rejects_builtin_duplicate(fake_model_env) -> None:
    result = _save_model("deepseek", {"model_id": "deepseek-chat"})
    assert result == {"ok": False, "message": "model_id already exists"}


def test_save_provider_model_returns_json_error_when_catalog_fails(fake_model_env) -> None:
    server, _home = fake_model_env
    server.models_error = RuntimeError("catalog unavailable")

    result = _save_model("deepseek", {"model_id": "deepseek-v4-flash"})

    assert result == {"ok": False, "message": "catalog unavailable"}


def test_save_provider_model_fails_closed_when_catalog_is_empty(fake_model_env) -> None:
    server, _home = fake_model_env
    server.models = {"providers": [], "default": {}}

    result = _save_model("deepseek", {"model_id": "deepseek-chat"})

    assert result == {"ok": False, "message": "provider model catalog is unavailable"}


def test_save_provider_model_rejects_builtin_duplicate_from_list_models(monkeypatch, tmp_path) -> None:
    async def _fake_get_server():
        return _FakeModelServer(
            {
                "providers": [
                    {
                        "id": "openrouter",
                        "models": [
                            {"id": "anthropic/claude-sonnet-4"},
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "restart_backend", lambda backend: {"ok": True})
    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})

    result = _save_model("openrouter", {"model_id": "anthropic/claude-sonnet-4"})

    assert result == {"ok": False, "message": "model_id already exists"}


def test_save_provider_model_allows_user_model_when_provider_catalog_loaded(fake_model_env) -> None:
    from vibe.opencode_config import read_opencode_provider_user_models

    _server, home = fake_model_env

    result = _save_model("deepseek", {"model_id": "deepseek-v4-flash"})

    assert result["ok"] is True
    assert "deepseek-v4-flash" in read_opencode_provider_user_models("deepseek", home=home)


def test_save_provider_model_allows_config_only_custom_provider(fake_model_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_user_models,
        upsert_opencode_custom_provider,
    )

    server, home = fake_model_env
    server.models = {"providers": [{"id": "openai", "models": {"gpt-5": {}}}], "default": {}}
    upsert_opencode_custom_provider(
        "my-relay",
        "My Relay",
        "openai-compatible",
        "https://relay.example/v1",
        home=home,
    )

    result = _save_model("my-relay", {"model_id": "relay-chat"})

    assert result["ok"] is True
    assert "relay-chat" in read_opencode_provider_user_models("my-relay", home=home)


def test_save_custom_provider_persists_config_and_key(fake_save_env) -> None:
    from vibe.opencode_config import read_opencode_custom_providers

    server, home = fake_save_env
    result = _save_custom(
        {
            "provider_id": "my-relay",
            "name": "My Relay",
            "adapter": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
        }
    )

    assert result["ok"] is True
    assert result["provider_id"] == "my-relay"
    assert server.set_calls == []
    assert "my-relay" in read_opencode_custom_providers(home=home)
    config = _read_opencode_config(home)
    assert config["provider"]["my-relay"]["options"]["apiKey"] == "sk-relay"


def test_save_custom_provider_rejects_clearing_base_url(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_custom_provider,
    )

    server, home = fake_save_env
    upsert_opencode_custom_provider(
        "my-relay",
        "My Relay",
        "openai-compatible",
        "https://relay.example/v1",
        home=home,
    )

    result = _save("my-relay", {"base_url": ""})

    assert result == {"ok": False, "message": "base_url is required for custom providers"}
    assert server.set_calls == []
    assert read_opencode_provider_base_url("my-relay", home=home) == "https://relay.example/v1"


def test_save_custom_provider_rejects_builtin_id(fake_save_env) -> None:
    result = _save_custom(
        {
            "provider_id": "openai",
            "name": "OpenAI Relay",
            "adapter": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
        }
    )

    assert result == {"ok": False, "message": "provider_id already exists"}


def test_save_custom_provider_rejects_reserved_id_when_catalog_unavailable(monkeypatch, tmp_path) -> None:
    async def _fail_get_server():
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(api, "_opencode_get_server", _fail_get_server)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = _save_custom(
        {
            "provider_id": "openai",
            "name": "OpenAI Shadow",
            "adapter": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
        }
    )

    assert result == {"ok": False, "message": "provider_id already exists"}


def test_delete_custom_provider_removes_config_and_auth(fake_save_env) -> None:
    from vibe.opencode_config import read_opencode_custom_providers

    server, home = fake_save_env
    _save_custom(
        {
            "provider_id": "my-relay",
            "name": "My Relay",
            "adapter": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
        }
    )

    result = _delete_custom("my-relay")

    assert result["ok"] is True
    assert server.remove_calls == []
    assert read_opencode_custom_providers(home=home) == {}


def test_delete_custom_provider_clears_matching_default_provider(fake_save_env) -> None:
    from config.v2_config import V2Config

    server, home = fake_save_env
    config = V2Config.from_payload(
        {
            "mode": "self_host",
            "version": "2",
            "platform": "avibe",
            "platforms": {"enabled": ["avibe"], "primary": "avibe"},
            "runtime": {"default_cwd": str(home), "log_level": "INFO"},
            "agents": {
                "default_backend": "opencode",
                "opencode": {"enabled": True, "default_provider": "my-relay"},
                "claude": {"enabled": False},
                "codex": {"enabled": False},
            },
        }
    )
    config.agents.opencode.default_provider = "my-relay"
    config.save()
    _save_custom(
        {
            "provider_id": "my-relay",
            "name": "My Relay",
            "adapter": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
        }
    )

    result = _delete_custom("my-relay")

    assert result["ok"] is True
    assert server.remove_calls == []
    assert V2Config.load().agents.opencode.default_provider is None


def test_delete_custom_provider_removes_config_without_auth_entry(fake_save_env) -> None:
    from vibe.opencode_config import read_opencode_custom_providers

    server, home = fake_save_env
    _save_custom(
        {
            "provider_id": "my-relay",
            "name": "My Relay",
            "adapter": "openai-compatible",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
        }
    )
    result = _delete_custom("my-relay")

    assert result["ok"] is True
    assert server.remove_calls == []
    assert read_opencode_custom_providers(home=home) == {}


def test_save_provider_model_persists_user_model_and_clears_cache(fake_model_env) -> None:
    from vibe.opencode_config import read_opencode_provider_user_models

    _server, home = fake_model_env
    result = _save_model(
        "deepseek",
        {"model_id": "deepseek-v4-flash", "reasoning_efforts": ["low", "high"]},
    )

    assert result["ok"] is True
    assert api._OPENCODE_OPTIONS_CACHE == {}
    model = read_opencode_provider_user_models("deepseek", home=home)["deepseek-v4-flash"]
    assert model["variants"] == {
        "low": {"reasoningEffort": "low"},
        "high": {"reasoningEffort": "high"},
    }


def test_delete_provider_model_only_removes_user_managed_models(fake_model_env) -> None:
    _server, home = fake_model_env
    _save_model("deepseek", {"model_id": "deepseek-v4-flash"})

    result = _delete_model("deepseek", "deepseek-v4-flash")

    assert result["ok"] is True
    from vibe.opencode_config import read_opencode_provider_user_models

    assert read_opencode_provider_user_models("deepseek", home=home) == {}


def test_delete_provider_model_keeps_empty_models_tombstone(fake_model_env) -> None:
    _server, home = fake_model_env
    _save_model("deepseek", {"model_id": "deepseek-v4-flash"})

    result = _delete_model("deepseek", "deepseek-v4-flash")

    assert result["ok"] is True
    config = _read_opencode_config(home)
    assert config["provider"]["deepseek"]["models"] == {}


def test_delete_provider_model_rejects_builtin_model(fake_model_env) -> None:
    result = _delete_model("deepseek", "deepseek-chat")
    assert result == {"ok": False, "message": "Only user-managed models can be removed"}


def test_base_url_explicit_empty_clears_stored_value(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_provider_base_url,
    )

    server, home = fake_save_env
    upsert_opencode_provider_base_url("openai", "https://stale.example", home=home)

    result = _save("openai", {"api_key": "sk-new", "base_url": ""})
    # Save now also triggers ``restart_backend("opencode")`` so the
    # daemon's in-memory cache picks up the new auth; ignore the
    # ``restart`` key for the per-field assertions below.
    assert result.get("ok") is True
    assert read_opencode_provider_base_url("openai", home=home) is None


def test_base_url_explicit_whitespace_clears_stored_value(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_provider_base_url,
    )

    server, home = fake_save_env
    upsert_opencode_provider_base_url("openai", "https://stale.example", home=home)

    result = _save("openai", {"api_key": "sk-new", "base_url": "   "})
    # Save now also triggers ``restart_backend("opencode")`` so the
    # daemon's in-memory cache picks up the new auth; ignore the
    # ``restart`` key for the per-field assertions below.
    assert result.get("ok") is True
    assert read_opencode_provider_base_url("openai", home=home) is None


def test_base_url_persists_when_provided(fake_save_env) -> None:
    from vibe.opencode_config import read_opencode_provider_base_url

    server, home = fake_save_env
    result = _save(
        "openai",
        {"api_key": "sk-new", "base_url": "https://relay.example/v1"},
    )
    # Save now also triggers ``restart_backend("opencode")`` so the
    # daemon's in-memory cache picks up the new auth; ignore the
    # ``restart`` key for the per-field assertions below.
    assert result.get("ok") is True
    assert (
        read_opencode_provider_base_url("openai", home=home)
        == "https://relay.example/v1"
    )


@pytest.mark.parametrize(
    "bad_value",
    [
        "relay.example",
        "ftp://relay.example",
        "javascript:alert(1)",
        "//relay.example",
    ],
)
def test_base_url_must_be_http_or_https(fake_save_env, bad_value) -> None:
    server, home = fake_save_env
    result = _save(
        "openai",
        {"api_key": "sk-new", "base_url": bad_value},
    )
    assert result["ok"] is False
    assert "http://" in result["message"] and "https://" in result["message"]
    # The daemon call must not fire for a rejected payload.
    assert server.set_calls == []


def test_base_url_must_be_string(fake_save_env) -> None:
    server, _ = fake_save_env
    result = _save("openai", {"api_key": "sk-new", "base_url": 123})
    assert result["ok"] is False
    assert "string" in result["message"].lower()
    assert server.set_calls == []


def test_base_url_null_clears_stored_value(fake_save_env) -> None:
    from vibe.opencode_config import (
        read_opencode_provider_base_url,
        upsert_opencode_provider_base_url,
    )

    server, home = fake_save_env
    upsert_opencode_provider_base_url("openai", "https://stale.example", home=home)
    result = _save("openai", {"api_key": "sk-new", "base_url": None})
    # Save now also triggers ``restart_backend("opencode")`` so the
    # daemon's in-memory cache picks up the new auth; ignore the
    # ``restart`` key for the per-field assertions below.
    assert result.get("ok") is True
    assert read_opencode_provider_base_url("openai", home=home) is None


def test_missing_api_key_rejected_before_any_side_effect(fake_save_env) -> None:
    server, _ = fake_save_env
    result = _save("openai", {"base_url": "https://x.example"})
    assert result["ok"] is False
    assert "api_key" in result["message"]
    assert server.set_calls == []


def test_base_url_persist_failure_surfaces_to_caller(monkeypatch, tmp_path) -> None:
    """If the JSON write blows up after the daemon call succeeds, the
    response must say so — silently returning ``ok: True`` is the exact
    "save success, value lost on reload" bug we're fixing.
    """

    from vibe import opencode_config

    server = _FakeServer()

    async def _fake_get_server():
        return server

    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(opencode_config, "upsert_opencode_provider_base_url", _explode)

    result = _save(
        "openai",
        {"api_key": "sk-new", "base_url": "https://relay.example/v1"},
    )
    assert result["ok"] is False
    assert "disk full" in result["message"]
    # Credential persistence is config-file based; the daemon auth API
    # is not called for Settings-entered API keys.
    assert server.set_calls == []
