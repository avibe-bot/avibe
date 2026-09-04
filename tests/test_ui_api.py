import asyncio
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.v2_config import (
    AgentsConfig,
    CodexConfig,
    PlatformsConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from config import paths
from config.v2_settings import ChannelSettings, SettingsStore
from core.vibe_agents import VibeAgentStore
from core import chat_discovery
from vibe import api, backend_model_catalog
from vibe.opencode_config import parse_jsonc_object


class _PopenFromRun:
    def __init__(self, cmd, *args, **kwargs):
        result = api.subprocess.run(cmd, **kwargs)
        self.args = cmd
        self.returncode = result.returncode
        self._stdout = result.stdout
        self._stderr = result.stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr


def test_opencode_options_closes_server_http_session(monkeypatch):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        def __init__(self):
            self.closed = 0
            self.closed_loop = None

        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return [{"name": "build", "mode": "primary", "hidden": False}]

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {
                        "id": "openai",
                        "models": {
                            "gpt-5": {},
                        },
                    }
                ]
            }

        async def get_providers(self):
            return {"all": [{"id": "openai", "name": "OpenAI"}], "connected": ["openai"]}

        async def get_default_config(self, directory):
            return {"model": "openai/gpt-5"}

        async def close_http_session(self, *, loop=None):
            self.closed += 1
            self.closed_loop = loop

    fake_manager = _FakeManager()

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return fake_manager

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("~/workspace"))

    assert result["ok"] is True
    assert result["data"]["defaults"] == {"model": "openai/gpt-5"}
    assert fake_manager.closed == 1
    assert fake_manager.closed_loop is not None


def test_opencode_options_passes_resource_governor_from_v2_runtime(monkeypatch):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    captured_kwargs = {}

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {"providers": []}

        async def get_providers(self):
            return {"all": [], "connected": []}

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            captured_kwargs.update(kwargs)
            return _FakeManager()

    v2_config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(),
        agents=AgentsConfig(),
        runtime=RuntimeConfig(
            default_cwd=".",
            resource_governance={
                "mode": "enabled",
                "agent_group_name": "ui-agents",
            },
        ),
    )
    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: v2_config))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("~/workspace"))

    assert result["ok"] is True
    governor = captured_kwargs["resource_governor"]
    assert governor.mode == "enabled"
    assert governor.config["agent_group_name"] == "ui-agents"


def test_opencode_options_cache_tracks_current_model_hub_projection(monkeypatch):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    projections = []

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory, *, model_hub_models=None):
            projections.append(model_hub_models)
            return {"providers": []}

        async def get_providers(self):
            return {"all": [], "connected": []}

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    manager = _FakeManager()

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return manager

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [],
    )
    first = {"custom/first": {"id": "custom/first"}}
    second = {"custom/second": {"id": "custom/second"}}

    asyncio.run(
        api.opencode_options_async(
            "/tmp/workspace",
            model_hub_models=first,
        )
    )
    cached = asyncio.run(
        api.opencode_options_async(
            "/tmp/workspace",
            model_hub_models=first,
        )
    )
    asyncio.run(
        api.opencode_options_async(
            "/tmp/workspace",
            model_hub_models=second,
        )
    )

    assert cached["cached"] is True
    assert projections == [first, second]


def test_opencode_options_does_not_fall_back_across_model_hub_projections(
    monkeypatch,
):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            raise RuntimeError("daemon unavailable")

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    stale_projection = {"custom/old": {"id": "custom/old"}}
    current_projection = {"custom/new": {"id": "custom/new"}}
    monkeypatch.setattr(
        api,
        "_OPENCODE_OPTIONS_CACHE",
        {
            "/tmp/workspace": {
                "data": {"models": {"providers": []}},
                "updated_at": time.monotonic(),
                "model_hub_projection": json.dumps(
                    stale_projection,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        },
    )
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)

    result = asyncio.run(
        api.opencode_options_async(
            "/tmp/workspace",
            model_hub_models=current_projection,
        )
    )

    assert result == {"ok": False, "error": "daemon unavailable"}


def test_sync_opencode_options_loads_persisted_model_hub_projection(monkeypatch):
    from core.handlers import model_hub

    projection = {"custom/current": {"id": "custom/current"}}
    calls = []

    async def options(cwd, *, model_hub_models=None):
        calls.append((cwd, model_hub_models))
        return {"ok": True, "data": {"models": {"providers": []}}}

    monkeypatch.setattr(model_hub, "load_opencode_public_models", lambda: projection)
    monkeypatch.setattr(api, "opencode_options_async", options)

    result = api.opencode_options("/tmp/workspace")

    assert result["ok"] is True
    assert calls == [("/tmp/workspace", projection)]


def test_opencode_options_empty_hub_menu_stops_without_relaunching(
    monkeypatch,
):
    import config.v2_compat as v2_compat
    import core.resource_governance as resource_governance
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        stop_for_empty_model_hub_menu = AsyncMock()
        ensure_running = AsyncMock()

        async def close_http_session(self, *, loop=None):
            pass

    fake_manager = _FakeManager()

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return fake_manager

    v2_config = SimpleNamespace(
        model_hub=SimpleNamespace(
            agents={"opencode": SimpleNamespace(mode="hub")}
        )
    )
    monkeypatch.setattr(
        api,
        "_OPENCODE_OPTIONS_CACHE",
        {
            "/tmp/workspace": {
                "data": {"models": {"providers": [{"id": "raw-provider"}]}},
                "updated_at": time.monotonic(),
                "model_hub_projection": "{}",
            }
        },
    )
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: v2_config))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(resource_governance, "config_from_runtime", lambda config: object())
    monkeypatch.setattr(resource_governance, "AgentResourceGovernor", lambda config: object())
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)

    result = asyncio.run(
        api.opencode_options_async(
            "/tmp/workspace",
            model_hub_models={},
        )
    )

    assert result == {"ok": False, "error": "mapping_target_unavailable"}
    fake_manager.stop_for_empty_model_hub_menu.assert_awaited_once_with()
    fake_manager.ensure_running.assert_not_awaited()


def test_opencode_options_nonempty_hub_menu_adopts_without_relaunching(
    monkeypatch,
):
    import config.v2_compat as v2_compat
    import core.resource_governance as resource_governance
    import modules.agents.opencode as opencode_module

    projection = {"menu-model": {"id": "menu-model"}}

    class _FakeManager:
        adopt_running_model_hub_overlay = AsyncMock()
        ensure_running = AsyncMock()

        async def get_available_agents(self, cwd):
            return []

        async def get_available_models(self, cwd, *, model_hub_models=None):
            assert model_hub_models == projection
            return {"providers": []}

        async def get_default_config(self, cwd):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    fake_manager = _FakeManager()

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return fake_manager

    v2_config = SimpleNamespace(
        model_hub=SimpleNamespace(
            agents={"opencode": SimpleNamespace(mode="hub")}
        )
    )
    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: v2_config))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(resource_governance, "config_from_runtime", lambda config: object())
    monkeypatch.setattr(resource_governance, "AgentResourceGovernor", lambda config: object())
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [],
    )

    result = asyncio.run(
        api.opencode_options_async(
            "/tmp/workspace",
            model_hub_models=projection,
        )
    )

    assert result["ok"] is True
    fake_manager.adopt_running_model_hub_overlay.assert_awaited_once_with()
    fake_manager.ensure_running.assert_not_awaited()


def test_opencode_get_server_passes_resource_governor_from_v2_runtime(monkeypatch):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    captured_kwargs = {}
    fake_manager = SimpleNamespace(ensure_running=AsyncMock())

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            captured_kwargs.update(kwargs)
            return fake_manager

    v2_config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(),
        agents=AgentsConfig(),
        runtime=RuntimeConfig(
            default_cwd=".",
            resource_governance={
                "mode": "enabled",
                "agent_group_name": "provider-ui-agents",
            },
        )
    )
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: v2_config))
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)

    server = asyncio.run(api._opencode_get_server())

    assert server is fake_manager
    fake_manager.ensure_running.assert_awaited_once()
    governor = captured_kwargs["resource_governor"]
    assert governor.mode == "enabled"
    assert governor.config["agent_group_name"] == "provider-ui-agents"


def test_opencode_options_filters_unconfigured_provider_models(monkeypatch, tmp_path):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "openai", "models": {"gpt-5": {}}},
                    {"id": "poe", "models": {"claude-opus-4": {}}},
                    {"id": "alibaba-cn", "models": {"qwen-max": {}}},
                    {
                        "id": "custom",
                        "models": {
                            "first-model": {
                                "vibe_remote": {"model_hub_projected": True}
                            },
                            "native-model": {},
                        },
                    },
                ],
                "default": {
                    "openai": "gpt-5",
                    "poe": "claude-opus-4",
                    "alibaba-cn": "qwen-max",
                },
            }

        async def get_providers(self):
            return {
                "all": [
                    {"id": "openai", "name": "OpenAI"},
                    {"id": "poe", "name": "Poe"},
                    {"id": "alibaba-cn", "name": "Alibaba (China)"},
                ],
                "connected": ["openai", "poe", "alibaba-cn"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"openai": {"type": "api", "key": "sk-test"}}))

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    providers = result["data"]["models"]["providers"]
    assert [p["id"] for p in providers] == ["openai", "custom"]
    custom = next(provider for provider in providers if provider["id"] == "custom")
    assert set(custom["models"]) == {"first-model"}
    assert result["data"]["models"]["default"] == {"openai": "gpt-5"}


def test_opencode_options_keeps_legacy_config_api_key_provider(monkeypatch, tmp_path):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "poe", "models": {"claude-opus-4": {}}},
                    {"id": "alibaba-cn", "models": {"qwen-max": {}}},
                ],
                "default": {
                    "poe": "claude-opus-4",
                    "alibaba-cn": "qwen-max",
                },
            }

        async def get_providers(self):
            return {
                "all": [
                    {"id": "poe", "name": "Poe"},
                    {"id": "alibaba-cn", "name": "Alibaba (China)"},
                ],
                "connected": ["poe", "alibaba-cn"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "poe": {"options": {"apiKey": "sk-poe"}},
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    providers = result["data"]["models"]["providers"]
    assert [p["id"] for p in providers] == ["poe"]
    assert result["data"]["models"]["default"] == {"poe": "claude-opus-4"}


def test_opencode_options_does_not_readd_unconfigured_user_model_provider(
    monkeypatch, tmp_path
):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "openai", "models": {"gpt-5": {}}},
                ],
                "default": {"openai": "gpt-5"},
            }

        async def get_providers(self):
            return {
                "all": [
                    {"id": "openai", "name": "OpenAI"},
                    {"id": "deepseek", "name": "DeepSeek"},
                ],
                "connected": ["openai", "deepseek"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"openai": {"type": "api", "key": "sk-test"}}))
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "deepseek": {
                        "models": {
                            "manual-deepseek-chat": {
                                "name": "manual-deepseek-chat",
                                "variants": {"high": {"reasoningEffort": "high"}},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    providers = result["data"]["models"]["providers"]
    assert [p["id"] for p in providers] == ["openai"]


def test_opencode_options_filters_catalog_provider_with_only_stale_user_model(
    monkeypatch, tmp_path
):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "openai", "models": {"gpt-5": {}}},
                    {"id": "zai", "models": {"glm-5.2": {}}},
                ],
                "default": {
                    "openai": "gpt-5",
                    "zai": "glm-5.2",
                },
            }

        async def get_providers(self):
            return {
                "all": [
                    {"id": "openai", "name": "OpenAI"},
                    {"id": "zai", "name": "Z.AI"},
                ],
                "connected": ["openai", "zai"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"openai": {"type": "api", "key": "sk-test"}}))
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "zai": {
                        "options": {"baseURL": "https://relay.example"},
                        "models": {
                            "glm-5.2": {
                                "id": "glm-5.2",
                                "name": "glm-5.2",
                                "vibe_remote": {"user_model": True},
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    assert result["ok"] is True
    providers = result["data"]["models"]["providers"]
    assert [p["id"] for p in providers] == ["openai"]
    assert result["data"]["models"]["default"] == {"openai": "gpt-5"}


def test_opencode_options_preserves_models_when_provider_catalog_fails(
    monkeypatch, tmp_path
):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "ollama", "models": {"llama3.1": {}}},
                ],
                "default": {"ollama": "llama3.1"},
            }

        async def get_providers(self):
            raise RuntimeError("provider endpoint unavailable")

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)
    monkeypatch.setattr(
        opencode_module,
        "build_reasoning_effort_options",
        lambda models, model_key: [{"value": "__default__"}],
    )

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    providers = result["data"]["models"]["providers"]
    assert [p["id"] for p in providers] == ["ollama"]
    assert providers[0]["models"] == {"llama3.1": {}}


def test_opencode_options_overlays_user_configured_models(monkeypatch, tmp_path):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "deepseek", "models": {"deepseek-chat": {}}},
                ],
                "default": {"deepseek": "deepseek-chat"},
            }

        async def get_providers(self):
            return {
                "all": [{"id": "deepseek", "name": "DeepSeek"}],
                "connected": ["deepseek"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"deepseek": {"type": "api", "key": "sk-test"}}))
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "deepseek": {
                        "models": {
                            "manual-regression-model": {
                                "name": "manual-regression-model",
                                "variants": {
                                    "low": {"effort": "low"},
                                    "high": {"effort": "high"},
                                },
                            }
                        }
                    }
                }
            }
        )
    )

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    provider = result["data"]["models"]["providers"][0]
    assert provider["id"] == "deepseek"
    assert sorted(provider["models"]) == ["deepseek-chat", "manual-regression-model"]
    reasoning_values = [
        entry["value"]
        for entry in result["data"]["reasoning_options"]["deepseek/manual-regression-model"]
    ]
    assert reasoning_values == ["__default__", "low", "high"]


def test_opencode_options_includes_custom_provider_models(monkeypatch, tmp_path):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "openai", "models": {"gpt-5": {}}},
                ],
                "default": {"openai": "gpt-5"},
            }

        async def get_providers(self):
            return {
                "all": [{"id": "openai", "name": "OpenAI"}],
                "connected": ["openai"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps(
            {
                "openai": {"type": "api", "key": "sk-openai"},
                "my-relay": {"type": "api", "key": "sk-relay"},
            }
        )
    )
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "my-relay": {
                        "name": "My Relay",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "https://relay.example/v1"},
                        "vibe_remote": {
                            "custom": True,
                            "adapter": "openai-compatible",
                        },
                        "models": {
                            "relay-chat": {
                                "name": "relay-chat",
                                "variants": {"high": {"effort": "high"}},
                            }
                        },
                    }
                }
            }
        )
    )

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    providers = result["data"]["models"]["providers"]
    ids = [provider["id"] for provider in providers]
    assert ids == ["openai", "my-relay"]
    relay = next(provider for provider in providers if provider["id"] == "my-relay")
    assert sorted(relay["models"]) == ["relay-chat"]
    reasoning_values = [
        entry["value"]
        for entry in result["data"]["reasoning_options"]["my-relay/relay-chat"]
    ]
    assert reasoning_values == ["__default__", "high"]


def test_opencode_options_includes_keyless_custom_provider_models(monkeypatch, tmp_path):
    import config.v2_compat as v2_compat
    import modules.agents.opencode as opencode_module

    class _FakeManager:
        async def ensure_running(self):
            return "http://127.0.0.1:4096"

        async def get_available_agents(self, directory):
            return []

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {"id": "openai", "models": {"gpt-5": {}}},
                ],
                "default": {"openai": "gpt-5"},
            }

        async def get_providers(self):
            return {
                "all": [{"id": "openai", "name": "OpenAI"}],
                "connected": ["openai"],
            }

        async def get_default_config(self, directory):
            return {}

        async def close_http_session(self, *, loop=None):
            pass

    class _FakeServerManager:
        @staticmethod
        async def get_instance(**kwargs):
            return _FakeManager()

    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"openai": {"type": "api", "key": "sk-openai"}}))
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "llama.cpp": {
                        "name": "llama.cpp",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "http://127.0.0.1:8080/v1"},
                        "vibe_remote": {
                            "custom": True,
                            "adapter": "openai-compatible",
                        },
                        "models": {
                            "local-model": {
                                "name": "local-model",
                                "vibe_remote": {"user_model": True},
                            }
                        },
                    }
                }
            }
        )
    )

    monkeypatch.setattr(api, "_OPENCODE_OPTIONS_CACHE", {})
    monkeypatch.setattr(api.V2Config, "load", staticmethod(lambda: object()))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        v2_compat,
        "to_app_config",
        lambda config: SimpleNamespace(
            opencode=SimpleNamespace(
                binary="opencode",
                port=4096,
                request_timeout_seconds=10,
            )
        ),
    )
    monkeypatch.setattr(opencode_module, "OpenCodeServerManager", _FakeServerManager)

    result = asyncio.run(api.opencode_options_async("/tmp/workspace"))

    providers = result["data"]["models"]["providers"]
    ids = [provider["id"] for provider in providers]
    assert ids == ["openai", "llama.cpp"]
    assert result["data"]["models"]["default"] == {
        "openai": "gpt-5",
        "llama.cpp": "local-model",
    }
    local = next(provider for provider in providers if provider["id"] == "llama.cpp")
    assert local["models"] == {"local-model": {"name": "local-model", "vibe_remote": {"user_model": True}}}


def test_opencode_provider_catalog_uses_native_models_for_provider_probes(
    monkeypatch, tmp_path
):
    class _FakeServer:
        async def get_providers(self):
            return {
                "all": [{"id": "openai", "name": "OpenAI"}],
                "connected": ["openai"],
            }

        async def get_provider_auth(self):
            return {}

        async def get_available_models(self, directory):
            raise AssertionError("provider probes must not use the projected catalog")

        async def get_native_available_models(self, directory):
            return {
                "providers": [{"id": "openai", "models": {"gpt-5": {}}}],
                "default": {"openai": "gpt-5"},
            }

        async def close_http_session(self, *, loop=None):
            pass

    async def _fake_get_server():
        return _FakeServer()

    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "openai": {
                        "models": {
                            "gpt-5": {
                                "options": {"textVerbosity": "low"},
                                "variants": {"high": {"reasoningEffort": "high"}},
                            }
                        }
                    }
                }
            }
        )
    )

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)

    result = asyncio.run(api.get_opencode_providers_async())

    entry = result["providers"][0]["model_entries"][0]
    assert result["providers"][0]["models"] == ["gpt-5"]
    assert entry["id"] == "gpt-5"
    assert entry["reasoning_efforts"] == ["high"]
    assert entry["user_managed"] is False


@pytest.mark.parametrize(
    ("available_models", "runtime_model", "expected_model"),
    [
        ({"gpt-5.3-chat-latest": {}, "gpt-5.4": {}}, None, "gpt-5.3-chat-latest"),
        ({"gpt-5.3-chat-latest": {}}, None, "gpt-5.3-chat-latest"),
        (
            {"gpt-5.3-chat-latest": {}, "gpt-5.4": {}, "gpt-5.4-runtime": {}},
            "openai/gpt-5.4-runtime",
            "gpt-5.4-runtime",
        ),
    ],
)
def test_opencode_provider_catalog_prefers_runtime_agent_model(
    monkeypatch,
    tmp_path,
    available_models,
    runtime_model,
    expected_model,
):
    class _FakeServer:
        async def get_providers(self):
            return {
                "all": [{"id": "openai", "name": "OpenAI"}],
                "connected": ["openai"],
            }

        async def get_provider_auth(self):
            return {}

        async def get_available_models(self, directory):
            return {
                "providers": [
                    {
                        "id": "openai",
                        "models": available_models,
                    }
                ],
                "default": {"openai": "gpt-5.3-chat-latest"},
            }

        get_native_available_models = get_available_models

        async def close_http_session(self, *, loop=None):
            pass

        def get_default_agent_from_config(self):
            return "build"

        def get_agent_model_from_config(self, agent_name):
            return runtime_model if agent_name == "build" else None

    async def _fake_get_server():
        return _FakeServer()

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)
    monkeypatch.setattr(
        api,
        "load_config",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(
                opencode=SimpleNamespace(
                    default_provider="openai",
                )
            )
        ),
    )

    result = asyncio.run(api.get_opencode_providers_async())

    provider = next(provider for provider in result["providers"] if provider["id"] == "openai")
    assert provider["default_model"] == expected_model


def test_opencode_provider_catalog_marks_keyless_custom_provider_configured(
    monkeypatch, tmp_path
):
    class _FakeServer:
        async def get_providers(self):
            return {
                "all": [{"id": "openai", "name": "OpenAI"}],
                "connected": ["openai"],
            }

        async def get_provider_auth(self):
            return {}

        async def get_available_models(self, directory):
            return {
                "providers": [{"id": "openai", "models": {"gpt-5": {}}}],
                "default": {"openai": "gpt-5"},
            }

        get_native_available_models = get_available_models

        async def close_http_session(self, *, loop=None):
            pass

    async def _fake_get_server():
        return _FakeServer()

    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "llama.cpp": {
                        "name": "llama.cpp",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "http://127.0.0.1:8080/v1"},
                        "vibe_remote": {
                            "custom": True,
                            "adapter": "openai-compatible",
                        },
                        "models": {
                            "local-model": {
                                "name": "local-model",
                                "vibe_remote": {"user_model": True},
                            }
                        },
                    }
                }
            }
        )
    )

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)

    result = asyncio.run(api.get_opencode_providers_async())

    provider = next(provider for provider in result["providers"] if provider["id"] == "llama.cpp")
    assert provider["configured"] is True
    assert provider["has_auth"] is False
    assert provider["custom"] is True


def test_opencode_provider_catalog_keeps_custom_provider_without_vibe_meta(
    monkeypatch, tmp_path
):
    class _FakeServer:
        async def get_providers(self):
            return {
                "all": [{"id": "openai", "name": "OpenAI"}],
                "connected": ["openai"],
            }

        async def get_provider_auth(self):
            return {}

        async def get_available_models(self, directory):
            return {
                "providers": [{"id": "openai", "models": {"gpt-5": {}}}],
                "default": {"openai": "gpt-5"},
            }

        get_native_available_models = get_available_models

        async def close_http_session(self, *, loop=None):
            pass

    async def _fake_get_server():
        return _FakeServer()

    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "gptg": {
                        "name": "Gptg",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "https://relay.example/v1",
                            "apiKey": "sk-relay",
                        },
                        "models": {},
                    }
                }
            }
        )
    )

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(api, "_opencode_get_server", _fake_get_server)

    result = asyncio.run(api.get_opencode_providers_async())

    provider = next(provider for provider in result["providers"] if provider["id"] == "gptg")
    assert provider["name"] == "Gptg"
    assert provider["configured"] is True
    assert provider["has_auth"] is True
    assert provider["custom"] is True
    assert provider["adapter"] == "openai-compatible"
    assert provider["api_key_masked"] == "••••••elay"


def test_normalize_backend_routing_payload_prefers_canonical_claude_overrides() -> None:
    result = api._normalize_backend_routing_payload(
        {
            "agent_backend": "claude",
            "model": "claude-opus-4-8",
            "reasoning_effort": "high",
            "claude_model": "claude-opus-4-8",
            "claude_reasoning_effort": "max",
        }
    )

    assert result["model"] == "claude-opus-4-8"
    assert result["reasoning_effort"] == "high"
    assert result["claude_model"] is None
    assert result["claude_reasoning_effort"] is None


def test_normalize_backend_routing_payload_prefers_canonical_over_round_trip_aliases() -> None:
    result = api._normalize_backend_routing_payload(
        {
            "agent_name": "claude",
            "model": "claude-sonnet-4-6",
            "reasoning_effort": "high",
            "claude_model": "claude-opus-4-8",
            "claude_reasoning_effort": "max",
        }
    )

    assert result["model"] == "claude-sonnet-4-6"
    assert result["reasoning_effort"] == "high"
    assert result["claude_model"] is None
    assert result["claude_reasoning_effort"] is None


def test_normalize_backend_routing_payload_lifts_aliases_for_builtin_agent_name() -> None:
    result = api._normalize_backend_routing_payload(
        {
            "agent_name": "claude",
            "claude_model": "claude-opus-4-8",
            "claude_reasoning_effort": "max",
        }
    )

    assert result["model"] == "claude-opus-4-8"
    assert result["reasoning_effort"] == "max"


def test_normalize_backend_routing_payload_preserves_explicit_canonical_clears() -> None:
    result = api._normalize_backend_routing_payload(
        {
            "agent_name": "claude",
            "model": None,
            "reasoning_effort": None,
            "claude_model": "claude-opus-4-8",
            "claude_reasoning_effort": "max",
        }
    )

    assert result["model"] is None
    assert result["reasoning_effort"] is None
    assert result["claude_model"] is None
    assert result["claude_reasoning_effort"] is None


def test_normalize_backend_routing_payload_ignores_deprecated_backend_for_alias_lifting() -> None:
    result = api._normalize_backend_routing_payload(
        {
            "agent_backend": "claude",
            "claude_model": "claude-opus-4-8",
            "claude_reasoning_effort": "max",
        }
    )

    assert result["model"] is None
    assert result["reasoning_effort"] is None
    assert result["claude_model"] == "claude-opus-4-8"
    assert result["claude_reasoning_effort"] == "max"


def test_normalize_backend_routing_payload_preserves_legacy_overrides_without_backend() -> None:
    result = api._normalize_backend_routing_payload(
        {
            "claude_model": "claude-opus-4-8",
            "claude_reasoning_effort": "max",
        }
    )

    assert result["model"] is None
    assert result["reasoning_effort"] is None
    assert result["claude_model"] == "claude-opus-4-8"
    assert result["claude_reasoning_effort"] == "max"


def test_sync_start_oauth_web_keeps_background_tasks_on_persistent_loop(monkeypatch):
    async def _start_web_setup(backend, *, force_reset=True, provider_id=None):
        async def _mark_completed():
            await asyncio.sleep(0.01)
            flow.state = "success"

        task = asyncio.create_task(_mark_completed())
        flow.waiter_task = task
        return flow

    flow = SimpleNamespace(
        flow_id="flow-sync",
        backend="codex",
        state="awaiting_code",
        url="https://auth.openai.com/codex/device",
        device_code="ABCD-EFGH",
        awaiting_code=False,
        provider=None,
        error=None,
        waiter_task=None,
    )
    service = SimpleNamespace(start_web_setup=AsyncMock(side_effect=_start_web_setup))
    monkeypatch.setattr(api, "_oauth_service", service)
    monkeypatch.setattr(api, "_oauth_loop", None)
    monkeypatch.setattr(api, "_oauth_loop_thread", None)

    result = api.start_oauth_web("codex")

    assert result["ok"] is True
    assert result["flow_id"] == "flow-sync"
    assert flow.waiter_task is not None
    deadline = time.time() + 1
    while flow.state != "success" and time.time() < deadline:
        time.sleep(0.01)
    assert flow.state == "success"
    assert flow.waiter_task.done()


def test_start_oauth_web_blocks_recovery_before_service_mutation(monkeypatch):
    fake_config = SimpleNamespace(load_warnings=("recovery required",), language="zh")
    service_calls = []
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service_calls.append(True))

    result = asyncio.run(api.start_oauth_web_async("codex"))

    assert result["ok"] is False
    assert result["error"] == "config_recovery"
    assert "配置加载时发生了恢复" in result["message"]
    assert service_calls == []


def test_start_oauth_web_localizes_native_login_conflict(monkeypatch):
    from core.agent_auth_service import BackendLoginInProgressError

    service = SimpleNamespace(
        start_web_setup=AsyncMock(
            side_effect=BackendLoginInProgressError("openai", "codex")
        )
    )
    monkeypatch.setattr(api, "load_config", lambda: SimpleNamespace(language="zh"))
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service)

    result = asyncio.run(api.start_oauth_web_async("codex"))

    assert result == {
        "ok": False,
        "error": "native_login_in_progress",
        "detail": "openai（codex）登录正在进行中，请先完成或取消后再重新发起。",
    }


def test_submit_oauth_web_code_rechecks_recovery_before_service_mutation(monkeypatch):
    fake_config = SimpleNamespace(load_warnings=("recovery required",), language="zh")
    service_calls = []
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service_calls.append(True))

    result = asyncio.run(api.submit_oauth_web_code_async("flow-1", "code-1"))

    assert result["ok"] is False
    assert result["error"] == "config_recovery"
    assert "配置加载时发生了恢复" in result["message"]
    assert service_calls == []


def test_remove_backend_auth_blocks_recovery_before_service_mutation(monkeypatch):
    fake_config = SimpleNamespace(load_warnings=("recovery required",), language="zh")
    service_calls = []
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service_calls.append(True))

    result = asyncio.run(api.remove_backend_auth_async("codex"))

    assert result["ok"] is False
    assert result["error"] == "config_recovery"
    assert "配置加载时发生了恢复" in result["message"]
    assert service_calls == []


def test_remove_claude_oauth_credentials_blocks_recovery_before_service_mutation(monkeypatch):
    fake_config = SimpleNamespace(load_warnings=("recovery required",), language="zh")
    service_calls = []
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "_get_oauth_service", lambda: service_calls.append(True))

    result = asyncio.run(api.remove_claude_oauth_credentials_async())

    assert result["ok"] is False
    assert result["error"] == "config_recovery"
    assert "配置加载时发生了恢复" in result["message"]
    assert service_calls == []


def test_detect_cli_prefers_claude_local(monkeypatch, tmp_path):
    claude_path = tmp_path / ".claude" / "local" / "claude"
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_text("#!/bin/sh\n")
    claude_path.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    result = api.detect_cli("claude")
    assert result["found"] is True
    assert result["path"] == str(claude_path)


def test_detect_cli_finds_opencode_installed_outside_path(monkeypatch, tmp_path):
    opencode_path = tmp_path / ".opencode" / "bin" / "opencode"
    opencode_path.parent.mkdir(parents=True, exist_ok=True)
    opencode_path.write_text("#!/bin/sh\n")
    opencode_path.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.detect_cli("opencode")

    assert result["found"] is True
    assert result["path"] == str(opencode_path)


def test_detect_cli_supports_explicit_path(monkeypatch, tmp_path):
    binary_path = tmp_path / "bin" / "custom-opencode"
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    binary_path.write_text("#!/bin/sh\n")
    binary_path.chmod(0o755)

    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.detect_cli(str(binary_path))

    assert result["found"] is True
    assert result["path"] == str(binary_path)


def test_resolve_cli_path_stops_before_npm_after_common_path_match(monkeypatch, tmp_path):
    codex_path = tmp_path / ".local" / "bin" / "codex"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("#!/bin/sh\n")
    codex_path.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(
        api,
        "_npm_global_binary_candidates",
        lambda _binary: pytest.fail("npm discovery must not run after a direct match"),
    )

    assert api.resolve_cli_path("codex") == str(codex_path)


def test_resolve_cli_path_fast_probe_never_queries_npm(monkeypatch, tmp_path):
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(
        api,
        "_npm_global_binary_candidates",
        lambda _binary: pytest.fail("fast presence probes must not query npm"),
    )

    assert api.resolve_cli_path("missing-cli", include_npm_global=False) is None


@pytest.fixture
def only_tmp_binaries(monkeypatch, tmp_path):
    """Make CLI discovery hermetic: only executables under ``tmp_path`` count as
    installed, so the runner's real ``/usr/local/bin/{npm,codex}`` (or a dev box's
    homebrew/nvm binaries) can't leak into detection. ``api._candidate_cli_paths``
    scans hardcoded common bin dirs (``/usr/local/bin``, ``/opt/homebrew/bin``)
    that these tests cannot otherwise neutralize; without this they pass locally
    but fail on a CI runner that has a real npm installed."""
    real_is_exec = api._is_executable_file
    monkeypatch.setattr(
        api,
        "_is_executable_file",
        lambda p: str(p).startswith(str(tmp_path)) and real_is_exec(p),
    )


def test_detect_cli_finds_npm_in_nvm(monkeypatch, tmp_path, only_tmp_binaries):
    npm_path = tmp_path / ".nvm" / "versions" / "node" / "v22.18.0" / "bin" / "npm"
    npm_path.parent.mkdir(parents=True, exist_ok=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.detect_cli("npm")

    assert result["found"] is True
    assert result["path"] == str(npm_path)


def test_detect_cli_skips_non_version_entries_in_nvm(monkeypatch, tmp_path, only_tmp_binaries):
    # Real-world nvm dir on macOS can contain a .DS_Store file (Finder/iCloud)
    # and a "system" alias dir, alongside real vX.Y.Z version directories.
    # Mixed-type sort keys used to crash with TypeError; this regression test
    # locks in that we filter and sort robustly.
    nvm_node = tmp_path / ".nvm" / "versions" / "node"
    nvm_node.mkdir(parents=True, exist_ok=True)
    (nvm_node / ".DS_Store").write_bytes(b"\x00\x01")
    (nvm_node / "system").mkdir()
    (nvm_node / "v18.20.0" / "bin").mkdir(parents=True)
    older_npm = nvm_node / "v18.20.0" / "bin" / "npm"
    older_npm.write_text("#!/bin/sh\n")
    older_npm.chmod(0o755)
    newer_npm = nvm_node / "v22.18.0" / "bin" / "npm"
    newer_npm.parent.mkdir(parents=True)
    newer_npm.write_text("#!/bin/sh\n")
    newer_npm.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.detect_cli("npm")

    assert result["found"] is True
    # Newer version must win over older one after filtering out junk entries.
    assert result["path"] == str(newer_npm)


def test_detect_cli_prefers_released_over_prerelease_in_nvm(monkeypatch, tmp_path, only_tmp_binaries):
    # When both a released and a pre-release of the same major.minor.patch exist,
    # the released version must win. Locks in the released > prerelease ranking
    # in _nvm_version_sort_key (released maps to is_released=True).
    nvm_node = tmp_path / ".nvm" / "versions" / "node"
    nvm_node.mkdir(parents=True, exist_ok=True)
    rc_npm = nvm_node / "v22.0.0-rc.1" / "bin" / "npm"
    rc_npm.parent.mkdir(parents=True)
    rc_npm.write_text("#!/bin/sh\n")
    rc_npm.chmod(0o755)
    released_npm = nvm_node / "v22.0.0" / "bin" / "npm"
    released_npm.parent.mkdir(parents=True)
    released_npm.write_text("#!/bin/sh\n")
    released_npm.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.detect_cli("npm")

    assert result["found"] is True
    assert result["path"] == str(released_npm)


def test_detect_cli_sorts_prerelease_numerically_in_nvm(monkeypatch, tmp_path, only_tmp_binaries):
    # "-rc.10" > "-rc.2" numerically, but lexicographic string comparison
    # would put "-rc.10" before "-rc.2" (because "1" < "2"). Locks in that
    # suffix tokens are split and the numeric segment compares as int.
    nvm_node = tmp_path / ".nvm" / "versions" / "node"
    nvm_node.mkdir(parents=True, exist_ok=True)
    rc2 = nvm_node / "v22.0.0-rc.2" / "bin" / "npm"
    rc2.parent.mkdir(parents=True)
    rc2.write_text("#!/bin/sh\n")
    rc2.chmod(0o755)
    rc10 = nvm_node / "v22.0.0-rc.10" / "bin" / "npm"
    rc10.parent.mkdir(parents=True)
    rc10.write_text("#!/bin/sh\n")
    rc10.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.detect_cli("npm")

    assert result["found"] is True
    # rc.10 must beat rc.2 even though "10" < "2" lexicographically.
    assert result["path"] == str(rc10)


def test_detect_cli_finds_codex_in_npm_global_prefix(monkeypatch, tmp_path, only_tmp_binaries):
    npm_path = tmp_path / "tools" / "npm"
    npm_path.parent.mkdir(parents=True, exist_ok=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)

    codex_path = tmp_path / ".npm-global" / "bin" / "codex"
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text("#!/bin/sh\n")
    codex_path.chmod(0o755)

    class CompletedProcess:
        returncode = 0
        stdout = f"{tmp_path / '.npm-global'}\n"
        stderr = ""

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: str(npm_path) if binary == "npm" else None)
    monkeypatch.setattr(api.subprocess, "run", lambda *args, **kwargs: CompletedProcess())

    result = api.detect_cli("codex")

    assert result["found"] is True
    assert result["path"] == str(codex_path)


def test_resolve_cli_paths_queries_npm_prefix_once_for_a_backend_batch(
    monkeypatch,
    tmp_path,
    only_tmp_binaries,
):
    npm_path = tmp_path / "tools" / "npm"
    npm_path.parent.mkdir(parents=True, exist_ok=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)
    inactive_npm = (
        tmp_path
        / ".nvm"
        / "versions"
        / "node"
        / "v18.20.0"
        / "bin"
        / "npm"
    )
    inactive_npm.parent.mkdir(parents=True, exist_ok=True)
    inactive_npm.write_text("#!/bin/sh\n")
    inactive_npm.chmod(0o755)

    prefix_path = tmp_path / ".npm-global"
    expected = {}
    for binary in ("claude", "codex", "opencode"):
        path = prefix_path / "bin" / binary
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        expected[binary] = str(path)

    calls = []

    class CompletedProcess:
        returncode = 0
        stdout = f"{prefix_path}\n"
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return CompletedProcess()

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        api.shutil,
        "which",
        lambda binary: str(npm_path) if binary == "npm" else None,
    )
    monkeypatch.setattr(api.subprocess, "run", fake_run)

    assert api.resolve_cli_paths(list(expected)) == expected
    assert calls == [[str(npm_path), "config", "get", "prefix"]]


def test_resolve_cli_paths_finds_backend_in_inactive_nvm_version_without_npm_query(
    monkeypatch,
    tmp_path,
    only_tmp_binaries,
):
    active_npm = (
        tmp_path
        / ".nvm"
        / "versions"
        / "node"
        / "v22.18.0"
        / "bin"
        / "npm"
    )
    active_npm.parent.mkdir(parents=True, exist_ok=True)
    active_npm.write_text("#!/bin/sh\n")
    active_npm.chmod(0o755)
    inactive_codex = (
        tmp_path
        / ".nvm"
        / "versions"
        / "node"
        / "v18.20.0"
        / "bin"
        / "codex"
    )
    inactive_codex.parent.mkdir(parents=True, exist_ok=True)
    inactive_codex.write_text("#!/bin/sh\n")
    inactive_codex.chmod(0o755)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        api.shutil,
        "which",
        lambda binary: str(active_npm) if binary == "npm" else None,
    )
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "an NVM bin match must not require npm prefix discovery"
        ),
    )

    assert api.resolve_cli_paths(["codex"]) == {"codex": str(inactive_codex)}


def test_resolve_cli_paths_preserves_completed_paths_when_other_probes_fail(
    monkeypatch,
):
    def resolve(binary, *, include_npm_global):
        assert include_npm_global is False
        if binary == "codex":
            raise OSError("NVM inventory changed during discovery")
        return f"/resolved/{binary}" if binary == "claude" else None

    def fail_prefix_probe():
        raise OSError("npm disappeared")

    monkeypatch.setattr(api, "resolve_cli_path", resolve)
    monkeypatch.setattr(api, "_npm_global_prefixes", fail_prefix_probe)

    assert api.resolve_cli_paths(["claude", "codex", "opencode"]) == {
        "claude": "/resolved/claude",
        "codex": None,
        "opencode": None,
    }


def test_install_agent_returns_resolved_path(monkeypatch):
    class CompletedProcess:
        returncode = 0
        stdout = "installed"
        stderr = ""

    monkeypatch.setattr(
        api.shutil,
        "which",
        lambda binary: f"/usr/bin/{binary}" if binary in {"curl", "bash"} else None,
    )
    monkeypatch.setattr(api.subprocess, "run", lambda *args, **kwargs: CompletedProcess())
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", lambda binary: "/Users/test/.opencode/bin/opencode")

    result = api.install_agent("opencode")

    assert result["ok"] is True
    assert result["path"] == "/Users/test/.opencode/bin/opencode"


def test_install_codex_fresh_install_uses_resolved_npm(monkeypatch, tmp_path):
    # Fresh-install path: codex is not yet on disk, but npm is resolvable.
    # install_agent must shell out to `npm install -g @openai/codex` and
    # use a user-owned npm prefix so backend lifecycle installs do not write
    # into system-owned global node directories.
    calls = []

    class CompletedProcess:
        returncode = 0
        stdout = "installed"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        return CompletedProcess()

    # Codex is missing before install, present after.
    codex_resolve = iter([None, "/Users/test/.nvm/versions/node/v22.18.0/bin/codex"])

    def fake_resolve(binary):
        if binary == "npm":
            return "/Users/test/.nvm/versions/node/v22.18.0/bin/npm"
        if binary == "codex":
            return next(codex_resolve)
        return None

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.install_agent("codex")

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == ["/Users/test/.nvm/versions/node/v22.18.0/bin/npm", "install", "-g", "@openai/codex"]
    assert calls[0][1]["NPM_CONFIG_PREFIX"] == str(api.Path.home() / ".local")
    assert calls[0][1]["PATH"].split(api.os.pathsep)[0] == str(api.Path.home() / ".local" / "bin")
    assert result["path"] == "/Users/test/.nvm/versions/node/v22.18.0/bin/codex"


def test_install_codex_npm_install_runs_npm_upgrade(monkeypatch, tmp_path):
    # Npm-owned Codex installs should upgrade through npm, not through a
    # different installer and not by blindly invoking the CLI self-updater. The
    # upgrade must stay in the npm prefix that owns the existing package.
    calls = []
    npm_path = tmp_path / ".nvm" / "versions" / "node" / "v22.18.0" / "bin" / "npm"
    npm_path.parent.mkdir(parents=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)
    codex_path = npm_path.parent.parent / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("#!/usr/bin/env node\n")
    codex_path.chmod(0o755)

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(npm_path), "config", "get", "prefix"]:
            return CompletedProcess(stdout=f"{npm_path.parent.parent}\n")
        if cmd == [str(npm_path), "install", "-g", "@openai/codex"]:
            return CompletedProcess(stdout="updated")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "npm":
            return str(npm_path)
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.install_agent("codex")

    assert result["ok"] is True
    update_calls = [c for c in calls if c[0] == [str(npm_path), "install", "-g", "@openai/codex"]]
    assert len(update_calls) == 1
    assert update_calls[0][1]["NPM_CONFIG_PREFIX"] == str(npm_path.parent.parent)
    assert update_calls[0][1]["PATH"].split(api.os.pathsep)[0] == str(npm_path.parent)
    assert result["path"] == str(codex_path)


def test_install_codex_homebrew_install_runs_brew_upgrade(monkeypatch, tmp_path):
    brew_path = tmp_path / "homebrew" / "bin" / "brew"
    brew_path.parent.mkdir(parents=True)
    brew_path.write_text("#!/bin/sh\n")
    brew_path.chmod(0o755)
    codex_path = tmp_path / "homebrew" / "bin" / "codex"
    codex_path.write_text("#!/bin/sh\n")
    codex_path.chmod(0o755)
    calls = []

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(brew_path), "list", "--cask", "codex"]:
            return CompletedProcess()
        if cmd == [str(brew_path), "--prefix"]:
            return CompletedProcess(stdout=f"{tmp_path / 'homebrew'}\n")
        if cmd == [str(brew_path), "--prefix", "--cask"]:
            return CompletedProcess(stdout=f"{tmp_path / 'homebrew' / 'Caskroom'}\n")
        if cmd == [str(brew_path), "upgrade", "--cask", "codex"]:
            return CompletedProcess(stdout="upgraded")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "brew":
            return str(brew_path)
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.install_agent("codex")

    assert result["ok"] is True
    assert result["path"] == str(codex_path)
    assert [str(brew_path), "upgrade", "--cask", "codex"] in [call[0] for call in calls]


def test_install_codex_prefers_homebrew_when_npm_shares_prefix(monkeypatch, tmp_path):
    prefix = tmp_path / "homebrew"
    brew_path = prefix / "bin" / "brew"
    npm_path = prefix / "bin" / "npm"
    codex_path = prefix / "bin" / "codex"
    for path in (brew_path, npm_path, codex_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    calls = []

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(brew_path), "list", "--cask", "codex"]:
            return CompletedProcess()
        if cmd == [str(brew_path), "--prefix"]:
            return CompletedProcess(stdout=f"{prefix}\n")
        if cmd == [str(brew_path), "--prefix", "--cask"]:
            return CompletedProcess(stdout=f"{prefix / 'Caskroom'}\n")
        if cmd == [str(npm_path), "config", "get", "prefix"]:
            return CompletedProcess(stdout=f"{prefix}\n")
        if cmd == [str(brew_path), "upgrade", "--cask", "codex"]:
            return CompletedProcess(stdout="upgraded")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "brew":
            return str(brew_path)
        if binary == "npm":
            return str(npm_path)
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.install_agent("codex")

    assert result["ok"] is True
    assert result["path"] == str(codex_path)
    commands = [call[0] for call in calls]
    assert [str(brew_path), "upgrade", "--cask", "codex"] in commands
    assert [str(npm_path), "install", "-g", "@openai/codex"] not in commands


def test_install_codex_unknown_install_falls_back_to_cli_update(monkeypatch, tmp_path, only_tmp_binaries):
    codex_path = tmp_path / "bin" / "codex"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("#!/bin/sh\n")
    codex_path.chmod(0o755)
    calls = []

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(codex_path), "--help"]:
            return CompletedProcess(stdout="Commands:\n  update          Update Codex to the latest version\n")
        if cmd == [str(codex_path), "update"]:
            return CompletedProcess(stdout="updated")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.install_agent("codex")

    assert result["ok"] is True
    assert [call[0] for call in calls] == [[str(codex_path), "--help"], [str(codex_path), "update"]]
    assert result["path"] == str(codex_path)


def test_install_codex_unknown_install_without_update_command_fails(monkeypatch, tmp_path):
    codex_path = tmp_path / "bin" / "codex"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("#!/bin/sh\n")
    codex_path.chmod(0o755)

    class CompletedProcess:
        returncode = 0
        stdout = "Commands:\n  exec            Run Codex non-interactively\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        if cmd == [str(codex_path), "--help"]:
            return CompletedProcess()
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)

    result = api.install_agent("codex")

    assert result["ok"] is False
    assert "does not expose an update command" in result["message"]


def test_install_codex_npm_install_without_npm_fails(monkeypatch, tmp_path):
    codex_path = tmp_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("#!/usr/bin/env node\n")
    codex_path.chmod(0o755)

    def fake_resolve(binary):
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)

    result = api.install_agent("codex")

    assert result["ok"] is False
    assert "npm was not found" in result["message"]


def test_discord_list_channels_rejects_empty_guild_id(monkeypatch):
    monkeypatch.setattr(
        chat_discovery,
        "channels_response",
        lambda *args, **kwargs: pytest.fail("channels_response should not be called without guild_id"),
    )

    result = api.discord_list_channels("token", "")

    assert result["ok"] is False
    assert result["channels"] == []
    assert result["error"] == "Discord guild_id is required"


def test_install_codex_detects_existing_install_via_npm_prefix_and_upgrades_with_npm(monkeypatch, tmp_path, only_tmp_binaries):
    # Codex already installed under the npm global prefix; resolve_cli_path
    # must discover it (via `npm config get prefix`) and install_agent must
    # still upgrade by rerunning npm install -g @openai/codex in that same
    # prefix instead of moving it to ~/.local.
    npm_path = tmp_path / "node" / "bin" / "npm"
    npm_path.parent.mkdir(parents=True, exist_ok=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)

    prefix_path = tmp_path / ".npm-global"
    codex_path = prefix_path / "bin" / "codex"
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text("#!/bin/sh\n")
    codex_path.chmod(0o755)
    calls = []

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(npm_path), "config", "get", "prefix"]:
            return CompletedProcess(stdout=f"{prefix_path}\n")
        if cmd == [str(npm_path), "install", "-g", "@openai/codex"]:
            return CompletedProcess(stdout="updated")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: str(npm_path) if binary == "npm" else None)
    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)

    result = api.install_agent("codex")

    assert result["ok"] is True
    assert result["path"] == str(codex_path)
    update_calls = [c for c in calls if c[0] == [str(npm_path), "install", "-g", "@openai/codex"]]
    assert len(update_calls) == 1
    assert update_calls[0][1]["NPM_CONFIG_PREFIX"] == str(prefix_path)
    assert update_calls[0][1]["PATH"].split(api.os.pathsep)[0] == str(prefix_path / "bin")


def test_install_codex_symlinked_npm_install_upgrades_in_real_prefix(monkeypatch, tmp_path, only_tmp_binaries):
    # Regression coverage for the Incus layout: ~/.local/bin/codex is a shim to
    # ~/.npm-global/bin/codex. Running npm with prefix ~/.local tries to replace
    # the shim and fails with EEXIST, so upgrades must follow the real package.
    npm_path = tmp_path / "node" / "bin" / "npm"
    npm_path.parent.mkdir(parents=True, exist_ok=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)

    prefix_path = tmp_path / ".npm-global"
    package_bin = prefix_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    package_bin.parent.mkdir(parents=True, exist_ok=True)
    package_bin.write_text("#!/usr/bin/env node\n")
    package_bin.chmod(0o755)
    npm_bin = prefix_path / "bin" / "codex"
    npm_bin.parent.mkdir(parents=True, exist_ok=True)
    npm_bin.symlink_to(package_bin)

    local_bin = tmp_path / ".local" / "bin" / "codex"
    local_bin.parent.mkdir(parents=True, exist_ok=True)
    local_bin.symlink_to(npm_bin)
    calls = []

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(npm_path), "config", "get", "prefix"]:
            return CompletedProcess(stdout=f"{prefix_path}\n")
        if cmd == [str(npm_path), "install", "-g", "@openai/codex"]:
            return CompletedProcess(stdout="updated")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "npm":
            return str(npm_path)
        if binary == "codex":
            return str(local_bin)
        return None

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)
    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)

    result = api.install_agent("codex")

    assert result["ok"] is True
    assert result["path"] == str(local_bin)
    update_calls = [c for c in calls if c[0] == [str(npm_path), "install", "-g", "@openai/codex"]]
    assert len(update_calls) == 1
    assert update_calls[0][1]["NPM_CONFIG_PREFIX"] == str(prefix_path)
    assert update_calls[0][1]["PATH"].split(api.os.pathsep)[0] == str(prefix_path / "bin")


def test_install_codex_project_local_node_modules_does_not_become_prefix(
    monkeypatch,
    tmp_path,
    only_tmp_binaries,
):
    npm_path = tmp_path / "node" / "bin" / "npm"
    npm_path.parent.mkdir(parents=True, exist_ok=True)
    npm_path.write_text("#!/bin/sh\n")
    npm_path.chmod(0o755)

    project_path = tmp_path / "project"
    codex_path = project_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text("#!/usr/bin/env node\n")
    codex_path.chmod(0o755)
    global_prefix = tmp_path / ".npm-global"
    calls = []

    class CompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd == [str(npm_path), "config", "get", "prefix"]:
            return CompletedProcess(stdout=f"{global_prefix}\n")
        if cmd == [str(npm_path), "install", "-g", "@openai/codex"]:
            return CompletedProcess(stdout="updated")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_resolve(binary):
        if binary == "npm":
            return str(npm_path)
        if binary == "codex":
            return str(codex_path)
        return None

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.shutil, "which", lambda binary: None)
    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api.subprocess, "Popen", _PopenFromRun)
    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)

    result = api.install_agent("codex")

    assert result["ok"] is True
    update_calls = [c for c in calls if c[0] == [str(npm_path), "install", "-g", "@openai/codex"]]
    assert len(update_calls) == 1
    assert update_calls[0][1].get("NPM_CONFIG_PREFIX") != str(project_path)
    assert update_calls[0][1]["NPM_CONFIG_PREFIX"] == str(tmp_path / ".local")
    assert update_calls[0][1]["PATH"].split(api.os.pathsep)[0] == str(tmp_path / ".local" / "bin")


def test_claude_models_merge_catalog_and_settings(monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus[1m]",
                "env": {
                    "ANTHROPIC_MODEL": "claude-sonnet-4-6",
                    "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5-20251001",
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(backend_model_catalog.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(backend_model_catalog, "load_cached_remote_catalog", lambda **kwargs: {})

    result = api.claude_models(schedule_refresh=False)

    assert result["ok"] is True
    assert result["models"][0] == "claude-fable-5-1"
    assert result["models"][1] == "claude-fable-5"
    assert result["models"][2] == "claude-opus-5"
    assert "opus" in result["models"]
    assert "sonnet" in result["models"]
    assert "haiku" in result["models"]
    assert "opus[1m]" in result["models"]
    assert "claude-haiku-4-5-20251001" in result["models"]
    assert result["models"].count("claude-sonnet-4-6") == 1
    assert result["model_labels"]["claude-opus-5"] == "claude-opus-5 [1M]"
    assert result["model_labels"]["claude-opus-4-6"] == "claude-opus-4-6 [1M]"
    assert result["model_labels"]["claude-sonnet-4-6"] == "claude-sonnet-4-6 [1M]"
    assert result["model_labels"]["opus"] == "opus [1M]"
    assert result["model_labels"]["sonnet"] == "sonnet [1M]"
    assert result["model_labels"]["opus[1m]"] == "opus[1m] [1M]"
    assert "claude-opus-4-5" not in result["model_labels"]
    assert [item["value"] for item in result["reasoning_options"]["opus"]] == [
        "__default__",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_codex_models_merges_cli_cache_and_filters_hidden_models(monkeypatch, tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.1", "visibility": "hide", "priority": 9},
                    {"slug": "gpt-5.3-codex-spark", "visibility": "list", "priority": 6},
                    {"slug": "gpt-5.4-mini", "visibility": "list", "priority": 3},
                    {"slug": "gpt-5.4", "visibility": "list", "priority": 1},
                    {"slug": "gpt-5.1-codex-mini", "visibility": "list", "priority": 19},
                ]
            }
        ),
        encoding="utf-8",
    )
    (codex_dir / "config.toml").write_text(
        '\n'.join(
            [
                'model = "gpt-5.1"',
                "[notice.model_migrations]",
                '"gpt-5.2" = "gpt-5.4"',
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.setattr(backend_model_catalog, "load_cached_remote_catalog", lambda **kwargs: {})

    result = api.codex_models(schedule_refresh=False)

    assert result["ok"] is True
    assert result["models"][:3] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    assert result["models"].index("gpt-5.4") < result["models"].index("gpt-5.4-mini")
    assert "gpt-5.3-codex-spark" in result["models"]
    assert "gpt-5.1-codex-mini" in result["models"]
    assert "gpt-5.1" not in result["models"]
    assert "gpt-5.2" in result["models"]
    assert result["models"].count("gpt-5.4") == 1


def test_codex_models_falls_back_when_cli_cache_missing(monkeypatch, tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "config.toml").write_text(
        '\n'.join(
            [
                'model = "custom-codex-model"',
                "[notice.model_migrations]",
                '"legacy-codex" = "gpt-5.4"',
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.setattr(backend_model_catalog, "load_cached_remote_catalog", lambda **kwargs: {})

    result = api.codex_models(schedule_refresh=False)

    assert result["ok"] is True
    assert result["models"][:3] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    assert "custom-codex-model" in result["models"]
    assert "legacy-codex" in result["models"]
    assert "gpt-5.1-codex-max" in result["models"]
    assert "gpt-5.1-codex-mini" in result["models"]


def test_codex_models_includes_static_reasoning(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(backend_model_catalog, "load_cached_remote_catalog", lambda **kwargs: {})
    result = api.codex_models(schedule_refresh=False)
    assert result["ok"] is True
    expected = ["__default__", "minimal", "low", "medium", "high", "xhigh"]
    # static set, surfaced under the default "" key and per-model
    assert [o["value"] for o in result["reasoning_options"][""]] == expected
    assert [o["value"] for o in result["reasoning_options"]["gpt-5.1-codex-max"]] == expected


def test_agent_model_options_claude_strips_default_and_marks_default(monkeypatch):
    monkeypatch.setattr(
        api,
        "claude_models",
        lambda: {
            "ok": True,
            "models": ["claude-opus-4-8", "claude-sonnet-4-6"],
            "model_labels": {"claude-opus-4-8": "claude-opus-4-8 [1M]"},
            "reasoning_options": {
                "claude-opus-4-8": [
                    {"value": "__default__", "label": "(Default)"},
                    {"value": "low", "label": "Low"},
                    {"value": "max", "label": "Max"},
                ],
                "claude-sonnet-4-6": [
                    {"value": "__default__", "label": "(Default)"},
                    {"value": "high", "label": "High"},
                ],
            },
        },
    )
    result = api.agent_model_options("claude")

    assert result["ok"] is True
    assert result["backend"] == "claude"
    assert result["live"] is False
    by_value = {m["value"]: m for m in result["models"]}
    # the UI "__default__" sentinel is stripped from the CLI-facing list
    assert by_value["claude-opus-4-8"]["reasoning_efforts"] == ["low", "max"]
    assert by_value["claude-opus-4-8"]["label"] == "claude-opus-4-8 [1M]"
    assert by_value["claude-sonnet-4-6"]["label"] == "claude-sonnet-4-6"
    assert "default" not in by_value["claude-opus-4-8"]
    assert "default" not in by_value["claude-sonnet-4-6"]


def test_agent_model_options_unknown_backend():
    result = api.agent_model_options("bogus")
    assert result["ok"] is False
    assert "bogus" in result.get("error", "")


def test_agent_model_options_opencode_overlay_and_provider_filter(monkeypatch):
    import vibe.opencode_config as opencode_config

    fake_opencode = {
        "ok": True,
        "data": {
            "models": {
                "providers": [
                    {"id": "anthropic", "name": "Anthropic", "models": {"claude-x": {}}},
                    {
                        "id": "deepseek",
                        "name": "DeepSeek",
                        "models": {"deepseek-chat": {"vibe_remote": {"user_model": True}}},
                    },
                    {
                        "id": "avibe-openai",
                        "name": "Avibe · OpenAI",
                        "models": {
                            "gpt-5": {
                                "vibe_remote": {"model_hub_projected": True},
                            }
                        },
                    },
                ],
                "default": {"anthropic": "claude-x"},
            },
            "reasoning_options": {
                "anthropic/claude-x": [{"value": "__default__"}, {"value": "low"}, {"value": "high"}],
                "deepseek/deepseek-chat": [{"value": "low"}],
                "gpt-5": [{"value": "high"}],
            },
        },
    }
    monkeypatch.setattr(api, "opencode_options", lambda cwd: fake_opencode)
    monkeypatch.setattr(opencode_config, "read_opencode_custom_providers", lambda **kw: {"deepseek": {}})

    result = api.agent_model_options("opencode")

    assert result["ok"] is True
    assert result["live"] is True
    providers = {p["id"]: p for p in result["providers"]}
    assert providers["deepseek"]["custom"] is True
    assert providers["anthropic"]["custom"] is False
    assert "avibe-openai" not in providers
    by_value = {m["value"]: m for m in result["models"]}
    # custom-provider models + reasoning + source annotation flow through unchanged
    assert by_value["anthropic/claude-x"]["reasoning_efforts"] == ["low", "high"]
    assert by_value["anthropic/claude-x"]["default"] is True
    assert by_value["anthropic/claude-x"]["source"] == "catalog"
    assert by_value["deepseek/deepseek-chat"]["source"] == "user"
    assert by_value["gpt-5"] == {
        "value": "gpt-5",
        "default": False,
        "source": "catalog",
        "reasoning_efforts": ["high"],
    }

    filtered = api.agent_model_options("opencode", provider="deepseek")
    assert [p["id"] for p in filtered["providers"]] == ["deepseek"]
    assert all(m["provider"] == "deepseek" for m in filtered["models"])


def test_codex_agents_merges_global_and_project(monkeypatch, tmp_path):
    global_agent_dir = tmp_path / ".codex" / "agents"
    global_agent_dir.mkdir(parents=True)
    (global_agent_dir / "reviewer.toml").write_text(
        '\n'.join(
            [
                'name = "reviewer"',
                'description = "Global reviewer"',
                'developer_instructions = "Review carefully."',
                'model = "gpt-5.4-mini"',
                'model_reasoning_effort = "medium"',
            ]
        ),
        encoding="utf-8",
    )

    project_root = tmp_path / "repo"
    project_agent_dir = project_root / ".codex" / "agents"
    project_agent_dir.mkdir(parents=True)
    (project_agent_dir / "reviewer.toml").write_text(
        '\n'.join(
            [
                'name = "reviewer"',
                'description = "Project reviewer"',
                'developer_instructions = "Focus on local changes."',
                'model = "gpt-5.4"',
                'model_reasoning_effort = "high"',
            ]
        ),
        encoding="utf-8",
    )
    (project_agent_dir / "triage.toml").write_text(
        '\n'.join(
            [
                'name = "triage"',
                'description = "Project triage"',
                'developer_instructions = "Classify issues first."',
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.codex_agents(str(project_root))

    assert result["ok"] is True
    assert [agent["id"] for agent in result["agents"]] == ["reviewer", "triage"]
    assert result["agents"][0]["source"] == "project"
    assert result["agents"][0]["description"] == "Project reviewer"
    assert result["agents"][0]["path"] == str(project_agent_dir / "reviewer.toml")
def test_setup_opencode_permission_preserves_existing_json_fields(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "model": "openai/gpt-5",
                "agent": {"build": {"model": "anthropic/claude-sonnet-4-5"}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated = parse_jsonc_object(config_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["config_path"] == str(config_path)
    assert updated == {
        "model": "openai/gpt-5",
        "agent": {"build": {"model": "anthropic/claude-sonnet-4-5"}},
        "permission": "allow",
    }


def test_opencode_permission_status_reports_allow_when_set(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"model": "openai/gpt-5", "permission": "allow"}), encoding="utf-8"
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result == {"ok": True, "permission_allowed": True, "config_path": str(config_path)}


def test_opencode_permission_status_reports_not_allowed_when_unset(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"permission": "prompt"}), encoding="utf-8")

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result["ok"] is True
    assert result["permission_allowed"] is False
    assert result["config_path"] == str(config_path)


def test_opencode_permission_status_is_read_only_when_no_config(monkeypatch, tmp_path):
    # The setup wizard polls this before any opencode.json exists: it must report
    # "not allowed" without creating the file (a read, unlike the write-side
    # setup_opencode_permission).
    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result["ok"] is True
    assert result["permission_allowed"] is False
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()


def test_opencode_permission_status_reports_allow_for_global_object(monkeypatch, tmp_path):
    # OpenCode's object form ``{"permission": {"*": "allow"}}`` is an allow-all
    # config and must register as allowed — otherwise the wizard gate blocks and
    # the callout nags users who already have a working config to overwrite it.
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"permission": {"*": "allow"}}), encoding="utf-8")

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result["ok"] is True
    assert result["permission_allowed"] is True


def test_opencode_permission_status_object_with_tool_override_is_not_allowed(monkeypatch, tmp_path):
    # An object permission that narrows a tool to ask/deny still prompts on that
    # tool (OpenCode resolves the last matching rule), which Vibe Remote can't
    # answer — so it must NOT count as granted, keeping the write-allow button.
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"permission": {"*": "allow", "bash": "ask"}}), encoding="utf-8"
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result["ok"] is True
    assert result["permission_allowed"] is False


def test_opencode_permission_status_nested_allow_object_is_granted(monkeypatch, tmp_path):
    # A granular all-allow tree (nested rule objects) avoids every approval
    # prompt, so it must register as granted — don't nag users to overwrite a
    # config that already works.
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"permission": {"*": "allow", "bash": {"*": "allow"}}}), encoding="utf-8"
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result["ok"] is True
    assert result["permission_allowed"] is True


def test_opencode_permission_status_returns_unknown_for_invalid_config(monkeypatch, tmp_path):
    # A malformed existing config can't be auto-fixed (setup refuses to overwrite
    # invalid files), so status reports unknown (ok: False) and the wizard gate
    # fails open rather than trapping the user behind an unsatisfiable Continue.
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{ not valid json", encoding="utf-8")

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.opencode_permission_status()

    assert result["ok"] is False
    assert result["permission_allowed"] is False
    assert result["config_path"] == str(config_path)


def test_setup_opencode_permission_accepts_jsonc_config(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """{
  // Global defaults should be preserved.
  "model": "openai/gpt-5",
  "agent": {
    "build": {
      "model": "anthropic/claude-sonnet-4-5",
    },
  },
}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated_text = config_path.read_text(encoding="utf-8")
    updated = parse_jsonc_object(updated_text)

    assert result["ok"] is True
    assert result["config_path"] == str(config_path)
    assert "// Global defaults should be preserved." in updated_text
    assert '"permission": "allow",' in updated_text
    assert '"model": "anthropic/claude-sonnet-4-5",' in updated_text
    assert updated == {
        "model": "openai/gpt-5",
        "agent": {"build": {"model": "anthropic/claude-sonnet-4-5"}},
        "permission": "allow",
    }


def test_setup_opencode_permission_preserves_existing_permission_node(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {
            "model": "openai/gpt-5",
            "permission": "allow",
        },
        indent=2,
    ) + "\n"
    config_path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()

    assert result == {
        "ok": True,
        "message": "Permission already set",
        "config_path": str(config_path),
    }
    assert config_path.read_text(encoding="utf-8") == original


def test_setup_opencode_permission_does_not_overwrite_invalid_existing_config(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = '{\n  "model": "openai/gpt-5",\n'
    config_path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()

    assert result["ok"] is False
    assert result["config_path"] == str(config_path)
    assert "could not be parsed" in result["message"]
    assert "File left unchanged." in result["message"]
    assert config_path.read_text(encoding="utf-8") == original


def test_setup_opencode_permission_preserves_comments_when_updating_existing_permission(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """{
  "model": "openai/gpt-5",
  "permission": /* keep this block comment */ "prompt", // keep this inline comment
}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated_text = config_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["config_path"] == str(config_path)
    assert '/* keep this block comment */ "allow", // keep this inline comment' in updated_text
    assert parse_jsonc_object(updated_text) == {
        "model": "openai/gpt-5",
        "permission": "allow",
    }


def test_setup_opencode_permission_handles_multiline_object_with_inline_closing_brace(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """{
  "model": "openai/gpt-5"}""",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated_text = config_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["config_path"] == str(config_path)
    assert updated_text == """{
  "model": "openai/gpt-5",
  "permission": "allow"
}"""
    assert parse_jsonc_object(updated_text) == {
        "model": "openai/gpt-5",
        "permission": "allow",
    }


def test_setup_opencode_permission_updates_last_duplicate_permission_entry(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """{
  "permission": "prompt",
  "model": "openai/gpt-5",
  "permission": "deny"
}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated_text = config_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["config_path"] == str(config_path)
    assert updated_text == """{
  "permission": "prompt",
  "model": "openai/gpt-5",
  "permission": "allow"
}
"""
    assert parse_jsonc_object(updated_text) == {
        "permission": "allow",
        "model": "openai/gpt-5",
    }


def test_setup_opencode_permission_preserves_leading_bom_when_inserting_multiline_property(
    monkeypatch, tmp_path
):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\ufeff{\n  \"model\": \"openai/gpt-5\"\n}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated_text = config_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["config_path"] == str(config_path)
    assert updated_text.startswith("\ufeff{\n")
    assert updated_text.count("\ufeff") == 1
    assert parse_jsonc_object(updated_text) == {
        "model": "openai/gpt-5",
        "permission": "allow",
    }


def test_setup_opencode_permission_skips_comment_only_file_and_uses_next_valid_path(monkeypatch, tmp_path):
    xdg_path = tmp_path / ".config" / "opencode" / "opencode.json"
    legacy_path = tmp_path / ".opencode" / "opencode.json"
    xdg_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    xdg_path.write_text("// placeholder only\n", encoding="utf-8")
    legacy_path.write_text(
        """{
  "model": "openai/gpt-5",
}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)

    result = api.setup_opencode_permission()
    updated = parse_jsonc_object(legacy_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["config_path"] == str(legacy_path)
    assert xdg_path.read_text(encoding="utf-8") == "// placeholder only\n"
    assert updated == {
        "model": "openai/gpt-5",
        "permission": "allow",
    }


def test_setup_opencode_permission_returns_error_when_existing_config_update_fails(monkeypatch, tmp_path):
    config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('{"model": "openai/gpt-5"}\n', encoding="utf-8")

    original_write_text = api.Path.write_text

    def failing_write_text(self, data, encoding=None, errors=None, newline=None):
        if self == config_path:
            raise OSError("read-only file system")
        return original_write_text(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(api.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(api.Path, "write_text", failing_write_text)

    result = api.setup_opencode_permission()

    assert result == {
        "ok": False,
        "message": "read-only file system",
        "config_path": str(config_path),
    }
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"model": "openai/gpt-5"}


def test_parse_jsonc_object_preserves_comment_markers_inside_strings():
    parsed = parse_jsonc_object(
        """{
  "line": "https://example.com // keep",
  "block": "value /* keep */ text"
}"""
    )

    assert parsed == {
        "line": "https://example.com // keep",
        "block": "value /* keep */ text",
    }


def test_parse_jsonc_object_accepts_inline_block_comments_before_values():
    parsed = parse_jsonc_object(
        """{
  "model": /* keep this comment */ "openai/gpt-5",
  "agent": {
    "build": /* another comment */ {
      "reasoningEffort": "high",
    },
  },
}"""
    )

    assert parsed == {
        "model": "openai/gpt-5",
        "agent": {
            "build": {
                "reasoningEffort": "high",
            }
        },
    }


def test_parse_jsonc_object_rejects_invalid_jsonc():
    with pytest.raises(json.JSONDecodeError):
        parse_jsonc_object(
            """{
  "model": "openai/gpt-5",
  "agent": {
    "build":
  }
}"""
        )


def test_telegram_auth_test_returns_response(monkeypatch):
    async def fake_get_me(bot_token: str, proxy_url: str | None = None):
        assert bot_token == "123456:test-token"
        assert proxy_url is None
        return {"id": 1, "username": "vibe_remote_bot"}

    monkeypatch.setattr(api, "_telegram_get_me", fake_get_me)
    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda proxy_url: proxy_url)

    result = api.telegram_auth_test("123456:test-token")

    assert result["ok"] is True
    assert result["response"]["username"] == "vibe_remote_bot"


def test_telegram_auth_test_uses_stored_token_when_request_omits_secret(monkeypatch):
    async def fake_get_me(bot_token: str, proxy_url: str | None = None):
        assert bot_token == "123456:stored-token"
        assert proxy_url is None
        return {"id": 1, "username": "stored_bot"}

    monkeypatch.setattr(api, "_telegram_get_me", fake_get_me)
    monkeypatch.setattr(api, "_stored_platform_secret", lambda platform, field: "123456:stored-token")
    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda proxy_url: proxy_url)

    result = api.telegram_auth_test("")

    assert result["ok"] is True
    assert result["response"]["username"] == "stored_bot"


def test_telegram_list_chats_returns_discovered_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    chat_discovery.remember_chat("telegram", "-1001", name="Core Group", native_type="supergroup")
    chat_discovery.remember_chat("telegram", "42", name="Alex", native_type="private", is_private=True)

    result = api.telegram_list_chats()

    assert result["ok"] is True
    assert [chat["id"] for chat in result["channels"]] == ["-1001"]
    assert result["summary"]["visible_count"] == 1
    assert result["summary"]["hidden_private_count"] == 1


def test_telegram_topic_settings_api_and_discovery_payload(tmp_path, monkeypatch):
    # Scenario: TELEGRAM-TOPIC-001
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    SettingsStore.reset_instance()
    agent_store = VibeAgentStore()
    try:
        agent_store.create(name="reviewer", backend="codex")
        store = SettingsStore.get_instance()
        store.update_channel("-1001", ChannelSettings(enabled=True, require_mention=True), platform="telegram")
        chat_discovery.remember_chat(
            "telegram", "-1001", name="Engineering", native_type="supergroup", supports_threads=True
        )
        chat_discovery.remember_thread(
            "telegram", "-1001", "42", name="Releases", native_type="forum_topic"
        )

        saved = api.save_thread_settings(
            {
                "platform": "telegram",
                "channel_id": "-1001",
                "thread_id": "42",
                "settings": {
                    "enabled": True,
                    "require_mention": False,
                    "require_bind": True,
                    "show_message_types": ["assistant", "toolcall"],
                    "routing": {"agent_name": "reviewer", "model": "gpt-5.4"},
                },
            }
        )
        settings = api.get_settings("telegram")
        chats = api.telegram_list_chats()

        assert saved["ok"] is True
        assert settings["threads"]["-1001"]["42"]["require_mention"] is False
        assert settings["threads"]["-1001"]["42"]["require_bind"] is True
        assert settings["threads"]["-1001"]["42"]["expected_agent_name"] == "reviewer"
        group = next(channel for channel in chats["channels"] if channel["id"] == "-1001")
        assert group["topics"][0]["name"] == "Releases"
        assert group["topics"][0]["configured"] is True

        deleted = api.delete_thread_settings("telegram", "-1001", "42")
        assert deleted["removed"] is True
        assert api.get_settings("telegram")["threads"] == {}
    finally:
        SettingsStore.reset_instance()
        agent_store.close()


def test_telegram_topic_settings_materialize_inherited_mention_default(tmp_path, monkeypatch):
    # Scenario: TELEGRAM-TOPIC-001
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))
    monkeypatch.setattr(api, "_stored_platform_config", lambda _platform: SimpleNamespace(require_mention=False))
    SettingsStore.reset_instance()
    try:
        store = SettingsStore.get_instance()
        store.update_channel("-1001", ChannelSettings(enabled=True, require_mention=None), platform="telegram")

        saved = api.save_thread_settings(
            {
                "platform": "telegram",
                "channel_id": "-1001",
                "thread_id": "42",
                "settings": {
                    "enabled": True,
                    "require_mention": None,
                },
            }
        )

        assert saved["ok"] is True
        assert saved["settings"]["require_mention"] is False
        assert store.find_thread("-1001", "42", platform="telegram").require_mention is False
    finally:
        SettingsStore.reset_instance()


def test_vibe_agent_api_crud_and_settings_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))

    created = api.create_vibe_agent(
        {
            "name": "reviewer",
            "backend": "codex",
            "description": "Review releases",
            "model": "gpt-5.4",
            "reasoning_effort": "high",
            "system_prompt": "Review carefully.",
        }
    )
    default_result = api.set_default_vibe_agent("reviewer")
    listed = api.get_vibe_agents()
    settings = api.get_settings("slack")
    updated = api.update_vibe_agent("reviewer", {"model": "gpt-5.5"})

    assert created["ok"] is True
    assert default_result["default_agent_name"] == "reviewer"
    assert "reviewer" in [agent["name"] for agent in listed["agents"]]
    assert settings["agent_catalog"]["default_agent_name"] == "reviewer"
    assert any(agent["backend"] == "codex" for agent in settings["agent_catalog"]["agents"])
    assert updated["agent"]["model"] == "gpt-5.5"


def test_vibe_agent_api_delete_archives_and_hides_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    api.create_vibe_agent({"name": "reviewer", "backend": "codex"})
    api.create_vibe_agent({"name": "zz-fallback", "backend": "codex"})

    removed = api.remove_vibe_agent("reviewer")

    assert removed["ok"] is True
    assert removed["removed_agent"] == "reviewer"
    assert removed["archived_agent"]["name"].startswith("_reviewer-")
    assert removed["archived_agent"]["display_name"] == "reviewer"
    assert removed["archived_agent"]["archived"] is True
    assert "reviewer" not in [agent["name"] for agent in api.get_vibe_agents(include_disabled=True)["agents"]]
    archived = api.get_vibe_agents(include_disabled=True, include_archived=True)["agents"]
    assert removed["archived_agent"]["name"] in [agent["name"] for agent in archived]


def test_vibe_agent_api_localizes_archive_refusal(tmp_path, monkeypatch):
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    store.create(name="only-agent", backend="codex")
    store.set_default_agent_name("only-agent")
    monkeypatch.setattr(api, "VibeAgentStore", lambda: store)
    monkeypatch.setattr(
        api.V2Config,
        "load",
        staticmethod(lambda: type("Config", (), {"language": "zh"})()),
    )

    result = api.remove_vibe_agent("only-agent")

    assert result["ok"] is False
    assert result["code"] == "agent_no_default_replacement"
    assert result["message"] == "没有其他已启用 Agent 时，无法归档默认 Agent `only-agent`。"


def test_vibe_agent_api_localizes_invalid_reference_metadata_on_rename(monkeypatch):
    class RefusingStore:
        def rename(self, _name, _new_name, *, user_context=None):
            raise api.AgentReferenceRewriteError()

        def close(self):
            return None

    monkeypatch.setattr(api, "VibeAgentStore", RefusingStore)
    monkeypatch.setattr(
        api.V2Config,
        "load",
        staticmethod(lambda: type("Config", (), {"language": "zh"})()),
    )

    result = api.update_vibe_agent("worker", {"name": "renamed-worker"})

    assert result == {
        "ok": False,
        "code": "agent_reference_metadata_invalid",
        "message": "任务或监控包含无效元数据，Avibe 无法更新 Agent 引用。",
        "hint": "请修复或删除元数据异常的任务或监控，然后重试。",
    }


def test_vibe_agent_api_localizes_archived_edit_refusal(tmp_path, monkeypatch):
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    store.create(name="archive-fallback", backend="codex")
    store.create(name="worker", backend="codex")
    archived = store.archive("worker")
    assert archived is not None
    monkeypatch.setattr(api, "VibeAgentStore", lambda: store)
    monkeypatch.setattr(
        api.V2Config,
        "load",
        staticmethod(lambda: type("Config", (), {"language": "zh"})()),
    )

    result = api.update_vibe_agent(archived.archived_name, {"description": "changed"})

    assert result == {
        "ok": False,
        "code": "agent_archived_read_only",
        "message": f"Agent `{archived.archived_name}` 已归档，无法编辑。",
        "hint": "已归档 Agent 为只读状态，仅供现有持久引用继续使用。",
    }


def test_vibe_agent_api_localizes_reserved_create_and_rename_names(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    monkeypatch.setattr(
        api.V2Config,
        "load",
        staticmethod(lambda: type("Config", (), {"language": "zh"})()),
    )

    create_result = api.create_vibe_agent({"name": "_hidden", "backend": "codex"})
    api.create_vibe_agent({"name": "worker", "backend": "codex"})
    rename_result = api.update_vibe_agent("worker", {"name": "review/team"})

    assert create_result == {
        "ok": False,
        "code": "agent_name_reserved",
        "message": "Agent 名称不能以下划线 `_` 开头；该命名空间由 Avibe 保留。",
        "hint": "请选择不以下划线 `_` 开头的 Agent 名称。",
    }
    assert rename_result["ok"] is False
    assert rename_result["code"] == "agent_name_path_separator"
    assert rename_result["message"] == "Agent 名称不能包含 `/` 或 `\\`。"


def test_vibe_agent_catalog_ensures_builtin_defaults_for_enabled_backends(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))

    listed = api.get_vibe_agents()
    names = {agent["name"] for agent in listed["agents"]}

    assert {"opencode", "claude"}.issubset(names)
    assert api.remove_vibe_agent("opencode")["code"] == "agent_builtin"


def test_builtin_default_agent_enabled_state_follows_backend_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents(["opencode", "claude"])
        assert store.require("opencode").enabled is True
        assert store.require("claude").enabled is True

        store.ensure_builtin_default_agents(["opencode"])

        assert store.require("opencode").enabled is True
        assert store.require("claude").enabled is False
        assert "claude" not in [agent.name for agent in store.list_agents(include_disabled=False)]
        assert "claude" in [agent.name for agent in store.list_agents(include_disabled=True)]
    finally:
        store.close()


def test_enabled_agent_backends_treat_missing_enabled_field_as_schema_default():
    @dataclass
    class LegacyCodexConfig:
        cli_path: str = "codex"

    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        runtime=RuntimeConfig(default_cwd="/tmp/work"),
        agents=AgentsConfig(codex=LegacyCodexConfig()),  # type: ignore[arg-type]
        ui=UiConfig(),
    )
    config.agents.opencode.enabled = False
    config.agents.claude.enabled = False

    assert api._enabled_agent_backends_from_config(config) == ["codex"]


def test_enabled_agent_backends_respect_explicit_disabled_backend():
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        runtime=RuntimeConfig(default_cwd="/tmp/work"),
        agents=AgentsConfig(codex=CodexConfig(enabled=False)),
        ui=UiConfig(),
    )
    config.agents.opencode.enabled = False
    config.agents.claude.enabled = False

    assert api._enabled_agent_backends_from_config(config) == []


def test_user_can_disable_builtin_default_agent_without_catalog_reenabling_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents(["opencode"])
        store.set_enabled("opencode", False)

        assert "opencode" not in [agent["name"] for agent in api.get_vibe_agents()["agents"]]
        assert store.require("opencode").enabled is False
    finally:
        store.close()


def test_disabled_agent_cannot_be_set_as_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    store = VibeAgentStore()
    try:
        store.create(name="reviewer", backend="codex", enabled=False)
        with pytest.raises(ValueError, match="disabled"):
            store.set_default_agent_name("reviewer")
    finally:
        store.close()


def test_vibe_agent_api_rejects_non_boolean_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))

    with pytest.raises(ValueError, match="Agent enabled must be a JSON boolean"):
        api.create_vibe_agent({"name": "reviewer", "backend": "codex", "enabled": "false"})

    api.create_vibe_agent({"name": "reviewer", "backend": "codex"})
    with pytest.raises(ValueError, match="Agent enabled must be a JSON boolean"):
        api.update_vibe_agent("reviewer", {"enabled": "false"})

    assert api.update_vibe_agent("reviewer", {"enabled": False})["agent"]["enabled"] is False


def test_builtin_default_agent_uses_first_enabled_backend_when_no_default_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents(["opencode", "claude", "codex"])
        assert store.get_default_agent_name() == "opencode"
    finally:
        store.close()


def test_api_builtin_default_agents_ignore_legacy_config_default_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        runtime=RuntimeConfig(default_cwd="/tmp/work"),
        agents=AgentsConfig(default_backend="codex"),
        ui=UiConfig(),
    )

    api._ensure_builtin_default_agents(config)
    store = VibeAgentStore()
    try:
        assert store.get_default_agent_name() == "opencode"
    finally:
        store.close()


def test_builtin_default_agent_does_not_reuse_conflicting_user_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    store = VibeAgentStore()
    try:
        store.create(name="opencode", backend="codex")
        with pytest.raises(ValueError, match="already exists with backend"):
            store.ensure_builtin_default_agent(backend="opencode")
    finally:
        store.close()


def test_builtin_default_agent_does_not_lock_existing_user_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    store = VibeAgentStore()
    try:
        created = store.create(name="opencode", backend="opencode")
        store.create(name="zz-fallback", backend="opencode")
        ensured = store.ensure_builtin_default_agent(backend="opencode")

        assert ensured.id == created.id
        assert ensured.source == "user"
        assert ensured.metadata == {}
        assert api.remove_vibe_agent("opencode")["ok"] is True
    finally:
        store.close()


def test_vibe_agent_import_reports_unreadable_file_as_client_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    missing_file = tmp_path / "missing-agent.md"

    with pytest.raises(ValueError, match="Unable to read or parse agent import file"):
        api.import_vibe_agents({"file": str(missing_file), "backend": "codex"})


def test_vibe_agent_import_rejects_non_markdown_direct_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    secret_file = tmp_path / "config.json"
    secret_file.write_text('{"token":"secret"}', encoding="utf-8")

    with pytest.raises(ValueError, match=r"Markdown \(\.md\) file"):
        api.import_vibe_agents({"file": str(secret_file), "backend": "codex"})


def test_vibe_agent_import_rejects_markdown_without_agent_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    notes_file = tmp_path / "private-notes.md"
    notes_file.write_text("# Private notes\n\nnon-agent content\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter with a name field"):
        api.import_vibe_agents({"file": str(notes_file), "backend": "codex"})


def test_vibe_agent_import_allows_uppercase_markdown_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    agent_file = tmp_path / "Reviewer.MD"
    agent_file.write_text("---\nname: reviewer\n---\nReview carefully.\n", encoding="utf-8")

    result = api.import_vibe_agents({"file": str(agent_file), "backend": "codex"})

    assert result["ok"] is True
    assert result["imported"][0]["name"] == "reviewer"


def test_vibe_agent_import_skips_invalid_global_agent_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    valid_file = tmp_path / "valid.md"
    invalid_file = tmp_path / "invalid.md"
    valid_file.write_text("---\nname: reviewer\n---\nReview carefully.\n", encoding="utf-8")
    invalid_file.write_text("---\nname: [broken\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        api,
        "iter_global_agent_files",
        lambda source: [(invalid_file, "codex"), (valid_file, "codex")],
    )

    result = api.import_vibe_agents({"from": "codex", "all": True})

    assert result["ok"] is True
    assert [agent["name"] for agent in result["imported"]] == ["reviewer"]
    assert result["skipped"][0]["source_ref"] == str(invalid_file)
    assert result["skipped"][0]["reason"] == "invalid"
    assert "Unable to read or parse agent import file" in result["skipped"][0]["error"]


def test_vibe_agent_api_rejects_backend_update(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    api.create_vibe_agent({"name": "worker", "backend": "codex"})

    with pytest.raises(ValueError, match="backend is immutable"):
        api.update_vibe_agent("worker", {"backend": "claude"})
